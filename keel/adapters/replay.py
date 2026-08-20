"""Replay adapter: run the whole system off a recorded transcript.

This is what makes the project reviewable. A reader clones the repo with no
API key, no billing account and no network, runs the orchestrator in replay
mode, and gets the same plan, the same artifacts, the same gate decisions and
the same audit log the live run produced. Determinism is the point: the
cassette key is a hash of the semantic inputs (skill, tier, system, prompt)
and contains no timestamps, so a run that asks the same question twice reads
the same recording twice.

Recording and replay share one artifact. The audit log's `model_call` events
carry the cassette payload, so observability and reproducibility are the same
file rather than two things that can drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from keel.models import MODEL_FOR_TIER, AdapterRequest, AdapterResponse, RunMode

__all__ = ["ReplayAdapter", "CassetteMiss", "load_cassettes", "cassette_from_response"]

AUDIT_FILENAME = "audit.jsonl"

_RERECORD_HINT = (
    "Re-record it by running the same scenario live: "
    "KEEL_AGENT_MODE=live (with ANTHROPIC_API_KEY set), then replay the new run id."
)


class CassetteMiss(LookupError):
    """Asked to replay a call that was never recorded.

    Deliberately fatal in strict mode. Silently inventing an answer would let
    a replay run diverge from the run it claims to reproduce, which destroys
    the only property replay exists to provide.
    """


class ReplayAdapter:
    """`AgentAdapter` that serves recorded responses.

    Args:
        cassettes: `cassette_key -> payload` mapping. Payload keys are the
            ones `cassette_from_response` writes; every one is optional.
        strict: True (default) raises `CassetteMiss` on an unknown key. False
            returns a clearly marked synthetic stub instead, which is useful
            for exercising orchestration paths in tests without recording a
            cassette for every node first.
    """

    mode: RunMode = RunMode.REPLAY

    STUB_PREFIX = "[keel:replay-stub]"

    def __init__(self, cassettes: Mapping[str, dict], *, strict: bool = True) -> None:
        self.cassettes: dict[str, dict] = dict(cassettes)
        self.strict = strict

    # -- construction ------------------------------------------------------

    @classmethod
    def from_run(
        cls,
        run_id: str,
        root: Path = Path("runs"),
        *,
        strict: bool = True,
    ) -> "ReplayAdapter":
        """Load every cassette recorded by a previous run."""
        return cls(load_cassettes(run_id, root), strict=strict)

    # -- adapter -----------------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        cassette = self.cassettes.get(request.cassette_key)

        if cassette is None:
            if self.strict:
                raise CassetteMiss(
                    f"No cassette for node {request.node_id!r} "
                    f"(skill {request.skill_id!r}, tier {request.tier.value}, "
                    f"key {request.cassette_key}). {_RERECORD_HINT}"
                )
            return self._stub(request)

        text = str(cassette.get("text") or cassette.get("response_text") or "")
        parsed = cassette.get("parsed")

        return AdapterResponse(
            text=text,
            parsed=parsed if isinstance(parsed, dict) else None,
            model=str(cassette.get("model") or MODEL_FOR_TIER[request.tier]),
            # Zeroed on purpose. A replayed call spends no tokens, and
            # `AdapterResponse.cost_usd` is derived from these, so reporting
            # the recorded counts would bill the reviewer's free run at live
            # rates. The original counts stay in the audit log, which is where
            # the cost of the *recording* run belongs.
            input_tokens=0,
            output_tokens=0,
            latency_seconds=float(cassette.get("latency_seconds") or 0.0),
            from_replay=True,
        )

    def _stub(self, request: AdapterRequest) -> AdapterResponse:
        """Obviously-fake response for non-strict mode.

        Marked in the text itself so a stub that escapes into an artifact is
        recognizable on sight rather than mistaken for model output.
        """
        return AdapterResponse(
            text=(
                f"{self.STUB_PREFIX} no cassette for node {request.node_id!r} "
                f"(skill {request.skill_id!r}, key {request.cassette_key}). "
                "This is synthetic filler, not model output."
            ),
            parsed=None,
            model=MODEL_FOR_TIER[request.tier],
            input_tokens=0,
            output_tokens=0,
            latency_seconds=0.0,
            from_replay=True,
        )

    # -- conveniences ------------------------------------------------------

    def __contains__(self, key: object) -> bool:
        return key in self.cassettes

    def __len__(self) -> int:
        return len(self.cassettes)


def cassette_from_response(response: AdapterResponse) -> dict[str, Any]:
    """Serialize a live response into a cassette payload.

    The audit writer merges this into a `model_call` event alongside
    `cassette_key`; `load_cassettes` reads it back.
    """
    return {
        "text": response.text,
        "parsed": response.parsed,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_seconds": response.latency_seconds,
    }


def load_cassettes(run_id: str, root: Path | str = Path("runs")) -> dict[str, dict]:
    """Read `runs/<run_id>/audit.jsonl` into a `cassette_key -> payload` map.

    Prefers `keel.governance.audit` if it exposes a loader, and imports it
    lazily inside this function so the adapter package stays importable while
    the governance plane is still being built. Falls back to parsing the
    JSONL directly, which is also the contract the audit writer has to keep:
    one JSON object per line, `event_type == "model_call"`, cassette key at
    `payload["cassette_key"]`.
    """
    root_path = Path(root)

    delegated = _delegate_to_audit_module(run_id, root_path)
    if delegated is not None:
        return delegated

    path = root_path / run_id / AUDIT_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"No audit log at {path}. Replay needs a recorded run; "
            f"pass a run id from {root_path}/ or record one. {_RERECORD_HINT}"
        )

    with path.open("r", encoding="utf-8") as handle:
        return _cassettes_from_lines(handle)


def _cassettes_from_lines(lines: Iterable[str]) -> dict[str, dict]:
    cassettes: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A truncated final line (killed mid-write) should not cost us the
            # cassettes that were flushed before it.
            continue
        if not isinstance(event, dict):
            continue
        if _event_type(event) != "model_call":
            continue

        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        key = payload.get("cassette_key")
        if isinstance(key, str) and key:
            cassettes[key] = payload

    return cassettes


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("event_type")
    # Tolerates both a plain string and an enum serialized as {"value": ...}.
    if isinstance(value, Mapping):
        value = value.get("value")
    return str(value or "")


def _delegate_to_audit_module(run_id: str, root: Path) -> dict[str, dict] | None:
    """Use the governance loader if it exists, otherwise return None."""
    try:
        from keel.governance import audit as audit_module  # noqa: PLC0415
    except ImportError:
        return None

    loader = getattr(audit_module, "load_cassettes", None)
    if not callable(loader):
        return None

    for call in (lambda: loader(run_id, root=root), lambda: loader(run_id, root)):
        try:
            result = call()
        except TypeError:
            continue
        if isinstance(result, Mapping):
            return dict(result)
    return None
