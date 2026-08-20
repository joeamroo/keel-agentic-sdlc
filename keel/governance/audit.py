"""The append-only audit log, which is also the replay cassette store.

Two requirements usually get built twice. Observability wants a durable record
of what the system did and why. Reproducibility wants a recording of every
model response so a run can be replayed without spending money or waiting on a
non-deterministic API. Building those separately guarantees they drift: the log
says one thing, the cassettes replay another, and the run report stops being
evidence of anything.

So there is one file. `runs/<run_id>/audit.jsonl` holds every event in the run,
and the `model_call` events carry the full response text keyed by
`AdapterRequest.cassette_key`. The replay adapter reads its cassettes straight
out of the audit log. What an auditor reads is exactly what a replay executes,
by construction rather than by discipline.

Three properties make the file trustworthy:

1. Append-only. Nothing here ever rewrites a line. Corrections are new events.
2. Durable per event. Every write flushes and fsyncs before returning. A crash
   mid-run costs the event in flight, never the trail that led up to it.
3. Self-checking. `verify_integrity` re-reads the bytes on disk and reports
   sequence gaps, backwards timestamps and malformed lines. An auditor should
   never have to take the writer's word for it, which is also why the check
   reads the file rather than the in-memory list.

Redaction runs at write time, not at read time. This file ships in a public
repository. A scrubber that only runs when something reads the log leaves the
credential sitting on disk, so payloads are scrubbed on the way in and the
secret never reaches the filesystem in the first place.
"""

from __future__ import annotations

import json
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

from keel.models import (
    AdapterRequest,
    AdapterResponse,
    AuditEvent,
    AuditEventType,
)

__all__ = ["AUDIT_FILENAME", "REDACTED", "AuditLog", "redact", "scrub"]

AUDIT_FILENAME = "audit.jsonl"
REDACTED = "<redacted>"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

# Credential shapes, matched by structure rather than by provenance. A prompt
# that quotes a shell command, a stack trace carrying an Authorization header
# and a model that helpfully echoes the key it was given all land in the same
# payload dict, so the only reliable filter is what the token looks like.
#
# The Anthropic pattern is first and deliberately broader than the generic
# `sk-` one, which stops at the hyphen in `sk-ant-`. Ordering matters: the
# specific pattern has to win.
#
# Every pattern that can start mid-word carries a left boundary. Without it
# `sk-[A-Za-z0-9_\-]{20,}` happily fires on the tail of an ordinary hyphenated
# phrase, and a scrubber that eats prose gets turned off.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic API keys
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI and lookalikes
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine grained PATs
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),  # Google API keys
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),  # Authorization headers
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

# Second line of defence, for secrets that have no recognizable shape. A field
# literally called `password` is redacted whatever it contains.
_SECRET_KEY_NAMES = re.compile(
    r"(?i)(?:^|_|-)(?:api[_-]?key|apikey|secret|password|passwd|token|credential|"
    r"credentials|authorization|auth|private[_-]?key|session[_-]?id)s?$"
)

# Token accounting fields read like credentials to the pattern above but are
# integers, and integers are never scrubbed, so this set only guards the odd
# case of a count that arrived as a string.
_COUNT_KEYS = frozenset({"input_tokens", "output_tokens", "total_tokens", "max_tokens"})


def scrub(text: str) -> str:
    """Replace every credential-shaped substring with `<redacted>`.

    Surrounding text survives, which matters: `"auth failed for sk-ant-..."`
    stays a readable diagnostic instead of collapsing into a placeholder.
    """
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _is_secret_name(key: str | None) -> bool:
    return bool(key) and key not in _COUNT_KEYS and _SECRET_KEY_NAMES.search(key or "") is not None


def redact(value: Any, key: str | None = None) -> Any:
    """Recursively scrub a payload and coerce it to JSON-native types.

    Coercion is part of the contract rather than a convenience. If unknown
    objects were handed to `json.dumps(default=str)` instead, their `repr`
    would be serialized after the scrubber had already run, and a dataclass
    holding an API key would write that key to disk untouched. Everything is
    therefore flattened here, where the scrubber still sees it.
    """
    # Enums first: the project's enums subclass `str`, and an unnormalized
    # member would serialize as itself rather than as its wire value.
    if isinstance(value, Enum):
        return redact(value.value, key)
    if isinstance(value, str):
        return REDACTED if _is_secret_name(key) else scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted by string form so a replayed run serializes identically.
        return [redact(item, key) for item in sorted(value, key=str)]
    return redact(str(value), key)


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _event_to_json(event: AuditEvent) -> dict[str, Any]:
    """One event as one JSON object, with `seq` first so the file greps well."""
    return {
        "seq": event.seq,
        "at": event.at,
        "run_id": event.run_id,
        "event_type": event.event_type.value,
        "node_id": event.node_id,
        "payload": event.payload,
    }


def _event_from_json(record: dict[str, Any]) -> AuditEvent:
    payload = record.get("payload")
    return AuditEvent(
        run_id=str(record["run_id"]),
        event_type=AuditEventType(record["event_type"]),
        payload=dict(payload) if isinstance(payload, dict) else {},
        node_id=record.get("node_id"),
        at=float(record["at"]),
        seq=int(record["seq"]),
    )


# --------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------


class AuditLog:
    """Append-only JSONL trail for one run, doubling as its cassette store.

    Opening a log whose file already exists resumes it: existing events are
    loaded and the sequence continues from the highest `seq` on disk. A crashed
    run that gets reopened therefore extends its trail instead of restarting
    the numbering and making the file look forged.
    """

    __slots__ = ("run_id", "root", "dir", "path", "_events", "_seq", "_last_at")

    def __init__(self, run_id: str, root: Path = Path("runs")) -> None:
        self.run_id = run_id
        self.root = Path(root)
        self.dir = self.root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / AUDIT_FILENAME
        self._events: list[AuditEvent] = []
        self._seq = 0
        self._last_at = 0.0
        if self.path.exists():
            self._load()
        else:
            # An empty file is a truthful statement that the run recorded
            # nothing. A missing file is indistinguishable from a deleted one,
            # so `verify_integrity` gets to treat absence as a problem.
            self.path.touch()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def emit(
        self,
        event_type: AuditEventType,
        payload: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> AuditEvent:
        """Append one event and return it.

        `seq` increases by exactly one per event, which is what makes a missing
        line detectable rather than merely suspicious. The timestamp is clamped
        to never go backwards: a wall clock that steps back under NTP would
        otherwise make an honest log fail its own integrity check.
        """
        self._seq += 1
        at = round(max(time.time(), self._last_at), 6)
        event = AuditEvent(
            run_id=self.run_id,
            event_type=event_type,
            payload=redact(payload or {}),
            node_id=node_id,
            at=at,
            seq=self._seq,
        )
        self._append(event)
        self._events.append(event)
        self._last_at = at
        return event

    def record_model_call(
        self,
        node_id: str,
        request: AdapterRequest,
        response: AdapterResponse,
    ) -> AuditEvent:
        """Record a model call. This event is the cassette.

        The payload carries everything a replay needs (the key, the text and
        the parsed object) and everything a reviewer needs (the prompt that
        produced it, the model, the tokens and the cost). Cost is stored rather
        than recomputed on read because `MODEL_PRICING` changes, and the
        question an auditor asks is what the run cost when it ran.
        """
        payload: dict[str, Any] = {
            "cassette_key": request.cassette_key,
            "skill_id": request.skill_id,
            "tier": request.tier.value,
            "model": response.model,
            "text": response.text,
            "parsed": response.parsed,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": round(response.cost_usd, 6),
            "latency_seconds": round(response.latency_seconds, 6),
            "from_replay": response.from_replay,
            "system": request.system,
            "prompt": request.prompt,
        }
        return self.emit(AuditEventType.MODEL_CALL, payload, node_id=node_id)

    def _append(self, event: AuditEvent) -> None:
        """Write one line, then force it to the platter.

        `flush` alone survives a process crash. `fsync` survives the machine
        losing power, which is the case the trail actually has to withstand.
        The cost is one syscall per event, against runs whose latency is
        dominated by model calls, so it is not a tradeoff worth optimizing.
        """
        line = json.dumps(_event_to_json(event), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @classmethod
    def read(cls, run_id: str, root: Path = Path("runs")) -> AuditLog:
        """Load an existing log for inspection or replay.

        Raises `FileNotFoundError` when there is nothing to read. Constructing
        `AuditLog` directly would silently create an empty run instead, which
        is the right default for a writer and the wrong one for an auditor.
        """
        path = Path(root) / run_id / AUDIT_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"no audit log at {path}")
        return cls(run_id, root)

    def _load(self) -> None:
        events: list[AuditEvent] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                events.append(_event_from_json(json.loads(line)))
            except (ValueError, TypeError, KeyError):
                # Skipped rather than raised: a damaged line must not stop an
                # auditor from reading the rest of the run. `verify_integrity`
                # is where damage gets reported.
                continue
        self._events = events
        self._seq = max((e.seq for e in events), default=0)
        self._last_at = max((e.at for e in events), default=0.0)

    def events(self, event_type: AuditEventType | None = None) -> list[AuditEvent]:
        """Every event in write order, optionally filtered by type."""
        if event_type is None:
            return list(self._events)
        return [e for e in self._events if e.event_type is event_type]

    def cassettes(self) -> dict[str, dict[str, Any]]:
        """Map `cassette_key` to the recorded response payload.

        This is the whole interface the replay adapter needs: look up the key
        it computed from its own request, rebuild an `AdapterResponse` from the
        payload, mark it `from_replay`.

        First recording wins. Two identical requests in one run have identical
        keys, and letting a later call overwrite an earlier one would mean the
        recorded history no longer explains the run that produced it.
        """
        found: dict[str, dict[str, Any]] = {}
        for event in self._events:
            if event.event_type is not AuditEventType.MODEL_CALL:
                continue
            key = event.payload.get("cassette_key")
            if isinstance(key, str) and key and key not in found:
                payload = dict(event.payload)
                # The node id lives on the event rather than in the payload,
                # but replay needs it to fall back when a prompt has drifted
                # since recording, so carry it through.
                payload.setdefault("node_id", event.node_id or "")
                found[key] = payload
        return found

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def verify_integrity(self) -> list[str]:
        """Re-read the file and report everything wrong with it.

        Returns human-readable problems, empty when the log is clean. Each
        finding names its line number so a reviewer can go straight to it.

        Deliberately reads the bytes on disk instead of the in-memory events:
        the question being answered is whether the file an auditor was handed
        is the file this process wrote, and only the file can answer that. Each
        class of damage reports once, so a single deleted line does not bury
        the report under a complaint about every line after it.
        """
        problems: list[str] = []
        if not self.path.exists():
            return [f"missing audit log: {self.path}"]

        expected_seq = 1
        last_at: float | None = None
        seen: set[int] = set()

        for number, raw in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip()
            if not line:
                problems.append(f"line {number}: blank line in an append-only log")
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"line {number}: malformed JSON ({exc.msg})")
                continue
            if not isinstance(record, dict):
                problems.append(
                    f"line {number}: expected a JSON object, found {type(record).__name__}"
                )
                continue

            missing = [f for f in ("seq", "at", "run_id", "event_type") if f not in record]
            if missing:
                problems.append(f"line {number}: missing field(s): {', '.join(missing)}")
                continue

            seq = record["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool):
                problems.append(f"line {number}: seq is not an integer: {seq!r}")
            else:
                if seq in seen:
                    problems.append(f"line {number}: duplicate seq {seq}")
                elif seq != expected_seq:
                    problems.append(
                        f"line {number}: sequence gap, expected seq {expected_seq}, found {seq}"
                    )
                seen.add(seq)
                expected_seq = max(expected_seq, seq) + 1

            at = record["at"]
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                problems.append(f"line {number}: timestamp is not a number: {at!r}")
            else:
                if last_at is not None and at < last_at:
                    problems.append(
                        f"line {number}: timestamp moves backwards ({at} < {last_at})"
                    )
                last_at = float(at)

            if record["run_id"] != self.run_id:
                problems.append(
                    f"line {number}: run_id {record['run_id']!r} does not belong to "
                    f"run {self.run_id!r}"
                )

            try:
                AuditEventType(record["event_type"])
            except ValueError:
                problems.append(f"line {number}: unknown event_type {record['event_type']!r}")

        return problems

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __repr__(self) -> str:
        return f"AuditLog(run_id={self.run_id!r}, events={len(self._events)}, path={str(self.path)!r})"
