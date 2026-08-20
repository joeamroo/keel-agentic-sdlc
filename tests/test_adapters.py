"""Adapter tests.

Every Claude test runs against a fake client injected into `ClaudeAdapter`.
No test in this file opens a socket, needs an API key, or costs a cent - the
same property the replay adapter gives a reviewer running the whole system.

The load-bearing assertions are the ones about request shape: the parameters
that 400 on `claude-opus-5` are checked by scanning the entire serialized
body, so a future edit that nests `budget_tokens` or `temperature` somewhere
new still trips the test instead of production.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from keel.adapters.base import extract_json, load_env
from keel.adapters.claude import (
    FORBIDDEN_PARAMS,
    ClaudeAdapter,
    MissingAPIKey,
    ModelRefusal,
    ResponseTruncated,
    collect_text,
)
from keel.adapters.replay import (
    CassetteMiss,
    ReplayAdapter,
    cassette_from_response,
    load_cassettes,
)
from keel.models import (
    MODEL_FOR_TIER,
    AdapterRequest,
    AdapterResponse,
    AgentAdapter,
    ModelTier,
    RunMode,
)

# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block(thinking: str = "considering the options") -> SimpleNamespace:
    # Note: no `.text` attribute at all, exactly like the real block.
    return SimpleNamespace(type="thinking", thinking=thinking)


def fake_message(
    *blocks,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5",
    input_tokens: int = 1_000,
    output_tokens: int = 500,
    stop_details=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model=model,
        stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeStreamCtx:
    def __init__(self, messages: "FakeMessages") -> None:
        self._messages = messages
        self._message = None

    async def __aenter__(self) -> "_FakeStreamCtx":
        self._message = self._messages.take()
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def get_final_message(self):
        return self._message


class FakeMessages:
    """Scripted stand-in for `client.messages`.

    The last scripted item is sticky, so `[error, error, message]` covers a
    retry sequence without having to count the successes.
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def take(self):
        if not self.script:
            raise AssertionError("fake client ran out of scripted responses")
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.create_calls.append(kwargs)
        return self.take()

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        self.stream_calls.append(kwargs)
        return _FakeStreamCtx(self)


class FakeClient:
    def __init__(self, *script) -> None:
        self.messages = FakeMessages(script)


def adapter_for(*script, **kwargs) -> ClaudeAdapter:
    """A ClaudeAdapter wired to a fake client, with retries that never sleep."""
    kwargs.setdefault("retry_base_delay", 0.0)
    return ClaudeAdapter(client=FakeClient(*script), **kwargs)


def api_error(cls, status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


def request_for(
    tier: ModelTier = ModelTier.DEEP,
    *,
    node_id: str = "n1",
    skill_id: str = "keel.implement",
    system: str = "You are a careful engineer.",
    prompt: str = "Implement the parser.",
    json_schema: dict | None = None,
) -> AdapterRequest:
    return AdapterRequest(
        node_id=node_id,
        skill_id=skill_id,
        tier=tier,
        system=system,
        prompt=prompt,
        json_schema=json_schema,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# base.py helpers
# ---------------------------------------------------------------------------


def test_load_env_parses_and_never_overwrites(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "ANTHROPIC_API_KEY=sk-ant-from-file",
                "export KEEL_AGENT_MODE=replay",
                'QUOTED="value with spaces"',
                "SINGLE='single quoted'",
                "WITH_COMMENT=plain  # trailing note",
                "malformed line without equals",
            ]
        ),
        encoding="utf-8",
    )
    environ = {"ANTHROPIC_API_KEY": "sk-ant-already-exported"}

    parsed = load_env(env_file, environ=environ)

    assert parsed["ANTHROPIC_API_KEY"] == "sk-ant-from-file"
    assert parsed["KEEL_AGENT_MODE"] == "replay"
    assert parsed["QUOTED"] == "value with spaces"
    assert parsed["SINGLE"] == "single quoted"
    assert parsed["WITH_COMMENT"] == "plain"
    assert "malformed line without equals" not in parsed

    # Real environment wins over the file, always.
    assert environ["ANTHROPIC_API_KEY"] == "sk-ant-already-exported"
    assert environ["KEEL_AGENT_MODE"] == "replay"


def test_load_env_missing_file_is_not_an_error(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    assert load_env(tmp_path / "nope.env", environ=environ) == {}
    assert environ == {}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"ok": true}', {"ok": True}),
        ('```json\n{"ok": true}\n```', {"ok": True}),
        ("```\n{\"ok\": true}\n```", {"ok": True}),
        ('Here you go:\n```json\n{"a": {"b": 1}}\n```\nHope that helps.', {"a": {"b": 1}}),
        ('Sure: {"a": 1} and that is all', {"a": 1}),
        ('{"brace": "} not the end", "n": 2}', {"brace": "} not the end", "n": 2}),
    ],
)
def test_extract_json_tolerates_fences_and_prose(raw, expected) -> None:
    assert extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "no json here", "[1, 2, 3]", "{broken"])
def test_extract_json_returns_none_rather_than_raising(raw) -> None:
    assert extract_json(raw) is None


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_both_adapters_satisfy_the_agent_adapter_protocol() -> None:
    live = adapter_for(fake_message(text_block("hi")))
    replay = ReplayAdapter({})

    assert isinstance(live, AgentAdapter)
    assert isinstance(replay, AgentAdapter)
    assert live.mode is RunMode.LIVE
    assert replay.mode is RunMode.REPLAY


# ---------------------------------------------------------------------------
# Tier routing and request shape
# ---------------------------------------------------------------------------


def test_tier_maps_to_the_right_model() -> None:
    deep = adapter_for(fake_message(text_block("deep")))
    fast = adapter_for(fake_message(text_block("fast"), model="claude-haiku-4-5"))

    run(deep.invoke(request_for(ModelTier.DEEP)))
    run(fast.invoke(request_for(ModelTier.FAST)))

    assert deep._client.messages.calls[0]["model"] == "claude-opus-5"
    assert fast._client.messages.calls[0]["model"] == "claude-haiku-4-5"
    # And the mapping is the frozen one, not a copy that can drift.
    assert MODEL_FOR_TIER[ModelTier.DEEP] == "claude-opus-5"
    assert MODEL_FOR_TIER[ModelTier.FAST] == "claude-haiku-4-5"


def test_deep_tier_sends_adaptive_thinking_and_high_effort() -> None:
    adapter = adapter_for(fake_message(text_block("ok")))
    run(adapter.invoke(request_for(ModelTier.DEEP)))
    sent = adapter._client.messages.calls[0]

    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["effort"] == "high"
    assert sent["max_tokens"] >= 16_000
    assert sent["system"] == "You are a careful engineer."
    assert sent["messages"] == [{"role": "user", "content": "Implement the parser."}]


def test_fast_tier_sends_no_thinking_and_no_output_config() -> None:
    adapter = adapter_for(fake_message(text_block("ok"), model="claude-haiku-4-5"))
    run(adapter.invoke(request_for(ModelTier.FAST)))
    sent = adapter._client.messages.calls[0]

    # haiku-4-5 is an older generation: effort is unsupported and its thinking
    # shape differs, so the simplest correct thing is to send neither.
    assert "thinking" not in sent
    assert "output_config" not in sent


@pytest.mark.parametrize("tier", list(ModelTier))
def test_forbidden_parameters_are_never_sent(tier: ModelTier) -> None:
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    adapter = adapter_for(
        fake_message(
            thinking_block(),
            text_block('{"verdict": "ok"}'),
            model=MODEL_FOR_TIER[tier],
        )
    )
    run(adapter.invoke(request_for(tier, json_schema=schema)))
    sent = adapter._client.messages.calls[0]
    body = json.dumps(sent, default=str)

    for name in FORBIDDEN_PARAMS:  # temperature, top_p, top_k
        assert name not in sent
        assert f'"{name}"' not in body
    # budget_tokens is removed on opus-5 and 400s; check nested too.
    assert "budget_tokens" not in body
    assert sent.get("thinking", {"type": "adaptive"}) == {"type": "adaptive"}


def test_large_max_tokens_uses_streaming_to_dodge_http_timeouts() -> None:
    deep = adapter_for(fake_message(text_block("streamed")))
    run(deep.invoke(request_for(ModelTier.DEEP)))

    assert len(deep._client.messages.stream_calls) == 1
    assert deep._client.messages.create_calls == []

    fast = adapter_for(fake_message(text_block("created"), model="claude-haiku-4-5"))
    run(fast.invoke(request_for(ModelTier.FAST)))

    assert fast._client.messages.stream_calls == []
    assert len(fast._client.messages.create_calls) == 1


def test_json_schema_is_hardened_and_parsed_back() -> None:
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        },
    }
    adapter = adapter_for(
        fake_message(
            thinking_block(),
            text_block('Result:\n```json\n{"verdict": "pass", "findings": []}\n```'),
        )
    )

    response = run(adapter.invoke(request_for(ModelTier.DEEP, json_schema=schema)))
    fmt = adapter._client.messages.calls[0]["output_config"]["format"]

    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert sorted(fmt["schema"]["required"]) == ["findings", "verdict"]
    # Nested objects are hardened too, or the API rejects the whole schema.
    assert fmt["schema"]["properties"]["findings"]["items"]["additionalProperties"] is False
    assert response.parsed == {"verdict": "pass", "findings": []}


def test_schema_and_effort_share_one_output_config() -> None:
    adapter = adapter_for(fake_message(text_block("{}")))
    run(
        adapter.invoke(
            request_for(ModelTier.DEEP, json_schema={"type": "object", "properties": {}})
        )
    )
    output_config = adapter._client.messages.calls[0]["output_config"]

    assert set(output_config) == {"effort", "format"}


def test_empty_system_prompt_is_omitted() -> None:
    adapter = adapter_for(fake_message(text_block("ok")))
    run(adapter.invoke(request_for(ModelTier.DEEP, system="")))

    assert "system" not in adapter._client.messages.calls[0]


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


def test_leading_thinking_block_does_not_break_text_extraction() -> None:
    adapter = adapter_for(
        fake_message(
            thinking_block("first I will read the spec"),
            text_block("Here is the plan."),
            text_block("Second paragraph."),
        )
    )

    response = run(adapter.invoke(request_for(ModelTier.DEEP)))

    assert response.text == "Here is the plan.\nSecond paragraph."
    # The naive implementation would have raised AttributeError on block zero.
    assert not hasattr(adapter._client.messages.script[0].content[0], "text")


def test_collect_text_on_a_thinking_only_response_is_empty() -> None:
    assert collect_text(fake_message(thinking_block())) == ""


def test_usage_latency_and_cost_are_populated() -> None:
    adapter = adapter_for(
        fake_message(text_block("done"), input_tokens=2_000, output_tokens=1_000)
    )

    response = run(adapter.invoke(request_for(ModelTier.DEEP)))

    assert response.input_tokens == 2_000
    assert response.output_tokens == 1_000
    assert response.model == "claude-opus-5"
    assert response.from_replay is False
    assert response.latency_seconds >= 0.0
    # 2000 * $5/M + 1000 * $25/M
    assert response.cost_usd == pytest.approx(0.035)


def test_refusal_raises_with_the_category_and_skips_on_call() -> None:
    seen: list[tuple] = []
    adapter = adapter_for(
        fake_message(
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber", explanation="no"),
        ),
        on_call=lambda req, resp: seen.append((req, resp)),
    )

    with pytest.raises(ModelRefusal) as excinfo:
        run(adapter.invoke(request_for(ModelTier.DEEP)))

    assert "cyber" in str(excinfo.value)
    assert seen == []  # nothing to record; there is no response


def test_max_tokens_truncation_raises_instead_of_returning_a_partial() -> None:
    adapter = adapter_for(fake_message(text_block("half a fi"), stop_reason="max_tokens"))

    with pytest.raises(ResponseTruncated):
        run(adapter.invoke(request_for(ModelTier.DEEP)))


def test_on_call_receives_the_request_and_response() -> None:
    seen: list[tuple[AdapterRequest, AdapterResponse]] = []
    adapter = adapter_for(
        fake_message(text_block("recorded")),
        on_call=lambda req, resp: seen.append((req, resp)),
    )
    request = request_for(ModelTier.DEEP)

    response = run(adapter.invoke(request))

    assert len(seen) == 1
    assert seen[0][0] is request
    assert seen[0][1] is response
    # The audit log can build a cassette straight from this pair.
    assert cassette_from_response(seen[0][1])["text"] == "recorded"


def test_async_on_call_is_awaited() -> None:
    seen: list[str] = []

    async def record(request, response):
        seen.append(response.text)

    adapter = adapter_for(fake_message(text_block("async")), on_call=record)
    run(adapter.invoke(request_for(ModelTier.DEEP)))

    assert seen == ["async"]


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


def test_rate_limit_is_retried_then_succeeds() -> None:
    adapter = adapter_for(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.RateLimitError, 429),
        fake_message(text_block("eventually")),
    )

    response = run(adapter.invoke(request_for(ModelTier.DEEP)))

    assert response.text == "eventually"
    assert len(adapter._client.messages.calls) == 3


def test_server_error_is_retried() -> None:
    adapter = adapter_for(
        api_error(anthropic.InternalServerError, 500),
        fake_message(text_block("recovered"), model="claude-haiku-4-5"),
    )

    assert run(adapter.invoke(request_for(ModelTier.FAST))).text == "recovered"
    assert len(adapter._client.messages.calls) == 2


def test_client_errors_propagate_without_retry() -> None:
    """A 400 is the orchestrator's problem: retrying it just burns the budget."""
    adapter = adapter_for(api_error(anthropic.BadRequestError, 400))

    with pytest.raises(anthropic.BadRequestError):
        run(adapter.invoke(request_for(ModelTier.FAST)))

    assert len(adapter._client.messages.calls) == 1


def test_retries_are_bounded() -> None:
    adapter = adapter_for(api_error(anthropic.RateLimitError, 429), max_retries=2)

    with pytest.raises(anthropic.RateLimitError):
        run(adapter.invoke(request_for(ModelTier.FAST)))

    assert len(adapter._client.messages.calls) == 3  # initial + 2 retries


def test_missing_api_key_is_an_actionable_error(monkeypatch) -> None:
    # Neutralize the real .env sitting in the checkout.
    monkeypatch.setattr("keel.adapters.claude.load_env", lambda *a, **k: {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(MissingAPIKey) as excinfo:
        ClaudeAdapter()

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "replay" in message


# ---------------------------------------------------------------------------
# Cassette keys
# ---------------------------------------------------------------------------


def test_cassette_key_is_stable_across_identical_requests() -> None:
    first = request_for(ModelTier.DEEP)
    second = request_for(ModelTier.DEEP)

    assert first.cassette_key == second.cassette_key
    assert len(first.cassette_key) == 16


def test_cassette_key_ignores_node_id_but_tracks_semantic_inputs() -> None:
    base = request_for(ModelTier.DEEP)

    # node ids are plan-version specific; a re-plan must not orphan cassettes.
    assert request_for(ModelTier.DEEP, node_id="different").cassette_key == base.cassette_key

    assert request_for(ModelTier.DEEP, prompt="other").cassette_key != base.cassette_key
    assert request_for(ModelTier.DEEP, system="other").cassette_key != base.cassette_key
    assert request_for(ModelTier.DEEP, skill_id="other").cassette_key != base.cassette_key
    assert request_for(ModelTier.FAST).cassette_key != base.cassette_key


# ---------------------------------------------------------------------------
# Replay adapter
# ---------------------------------------------------------------------------


def test_replay_hit_returns_the_recording_at_zero_cost() -> None:
    request = request_for(ModelTier.DEEP)
    adapter = ReplayAdapter(
        {
            request.cassette_key: {
                "text": "recorded answer",
                "parsed": {"verdict": "pass"},
                "model": "claude-opus-5",
                "input_tokens": 2_000,
                "output_tokens": 1_000,
                "latency_seconds": 12.5,
            }
        }
    )

    response = run(adapter.invoke(request))

    assert response.text == "recorded answer"
    assert response.parsed == {"verdict": "pass"}
    assert response.model == "claude-opus-5"
    assert response.from_replay is True
    assert response.latency_seconds == 12.5
    # A replayed call spends nothing, so it must not inflate the run's cost.
    assert response.cost_usd == 0.0
    assert response.input_tokens == 0
    assert response.output_tokens == 0


def test_replay_round_trips_a_live_response() -> None:
    request = request_for(ModelTier.FAST)
    live = adapter_for(fake_message(text_block("live output"), model="claude-haiku-4-5"))
    recorded = run(live.invoke(request))

    replayed = run(
        ReplayAdapter({request.cassette_key: cassette_from_response(recorded)}).invoke(request)
    )

    assert replayed.text == recorded.text
    assert replayed.model == recorded.model
    assert replayed.from_replay is True


def test_replay_miss_tells_you_how_to_re_record() -> None:
    request = request_for(ModelTier.DEEP)
    adapter = ReplayAdapter({"some-other-key": {"text": "nope"}})

    with pytest.raises(CassetteMiss) as excinfo:
        run(adapter.invoke(request))

    message = str(excinfo.value)
    assert "KEEL_AGENT_MODE=live" in message
    assert request.cassette_key in message
    assert request.node_id in message


def test_replay_non_strict_returns_a_marked_stub() -> None:
    request = request_for(ModelTier.FAST)
    adapter = ReplayAdapter({}, strict=False)

    response = run(adapter.invoke(request))

    assert response.text.startswith(ReplayAdapter.STUB_PREFIX)
    assert "synthetic" in response.text
    assert response.from_replay is True
    assert response.cost_usd == 0.0
    assert response.model == "claude-haiku-4-5"


def test_replay_tolerates_a_sparse_cassette() -> None:
    request = request_for(ModelTier.DEEP)
    adapter = ReplayAdapter({request.cassette_key: {}})

    response = run(adapter.invoke(request))

    assert response.text == ""
    assert response.parsed is None
    assert response.model == "claude-opus-5"  # falls back to the tier's model


# ---------------------------------------------------------------------------
# Loading cassettes from a recorded run
# ---------------------------------------------------------------------------


def write_audit_log(root: Path, run_id: str, lines: list[str]) -> Path:
    path = root / run_id / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_from_run_reads_model_call_events(tmp_path: Path) -> None:
    request = request_for(ModelTier.DEEP)
    write_audit_log(
        tmp_path,
        "run-1",
        [
            json.dumps({"run_id": "run-1", "event_type": "run_started", "payload": {}}),
            json.dumps(
                {
                    "run_id": "run-1",
                    "event_type": "model_call",
                    "node_id": "n1",
                    "payload": {
                        "cassette_key": request.cassette_key,
                        "text": "from the audit log",
                        "model": "claude-opus-5",
                    },
                }
            ),
            json.dumps({"run_id": "run-1", "event_type": "model_call", "payload": {}}),
            "{ this line is truncated garbage",
            "",
        ],
    )

    adapter = ReplayAdapter.from_run("run-1", root=tmp_path)

    assert len(adapter) == 1
    assert request.cassette_key in adapter
    assert run(adapter.invoke(request)).text == "from the audit log"


def test_load_cassettes_accepts_a_serialized_enum_event_type(tmp_path: Path) -> None:
    write_audit_log(
        tmp_path,
        "run-2",
        [
            json.dumps(
                {
                    "event_type": {"value": "model_call"},
                    "payload": {"cassette_key": "abc123", "text": "enum shaped"},
                }
            )
        ],
    )

    assert load_cassettes("run-2", tmp_path)["abc123"]["text"] == "enum shaped"


def test_from_run_on_a_missing_log_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        ReplayAdapter.from_run("run-does-not-exist", root=tmp_path)

    assert "KEEL_AGENT_MODE=live" in str(excinfo.value)
