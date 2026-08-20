"""Tests for the A2A layer.

The fast tests use `InProcessTransport`, so the bulk of the suite never binds a
port. One test class, marked `http`, boots a real stage agent on an ephemeral
port and exercises the protocol end to end: card discovery, a task run to
completion, a task parked in INPUT_REQUIRED, a cancellation, and bearer
enforcement. Deselect it with `-m "not http"` where sockets are unavailable.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from google.protobuf import json_format

from a2a.client import A2AClientError
from a2a.types import a2a_pb2 as pb

from keel.a2a.cards import (
    AGENT_CARD_PATH,
    ALL_STAGES,
    AUTH_TOKEN_ENV,
    BEARER_SCHEME_NAME,
    all_cards,
    card_for,
    default_port,
    skill_ids,
    stage_for_skill,
)
from keel.a2a.client import StageClient, discover
from keel.a2a.server import (
    StageAgentExecutor,
    parse_stage_result,
    pick_free_port,
    serve,
)
from keel.a2a.transport import (
    A2ADispatcher,
    CancelNotAllowed,
    HttpTransport,
    InProcessTransport,
    Transport,
)
from keel.models import ModelTier, StageKind, TaskState


# ---------------------------------------------------------------------------
# Stage handlers used as test doubles
# ---------------------------------------------------------------------------


async def completing_handler(text: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "artifacts": [{"name": "analysis", "content": f"analyzed: {text}"}],
    }


async def parking_handler(text: str) -> dict[str, Any]:
    return {"status": "input_required", "message": "Secure against what, exactly?"}


async def failing_handler(text: str) -> dict[str, Any]:
    return {"status": "failed", "error": "compiler exploded"}


async def rejecting_handler(text: str) -> dict[str, Any]:
    return {"status": "rejected", "reason": "out of scope for this agent"}


async def crashing_handler(text: str) -> dict[str, Any]:
    raise RuntimeError("handler blew up")


async def routing_handler(text: str) -> dict[str, Any]:
    """Mirrors the real stage shape: parks when the requirement is vague."""
    if "vague" in text.lower():
        return {"status": "input_required", "message": "Which auth model?"}
    return {
        "status": "completed",
        "artifacts": [{"name": "analysis", "content": "analysis complete"}],
    }


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def test_all_stages_covered() -> None:
    assert ALL_STAGES == tuple(StageKind)
    assert len(ALL_STAGES) == 7


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_skill_id_matches_stage_kind(kind: StageKind) -> None:
    """The planner sets skill_id=StageKind.X.value, so the card must match."""
    card = card_for(kind, "http://127.0.0.1:9000")
    assert skill_ids(card) == [kind.value]
    assert stage_for_skill(kind.value) is kind


def test_card_declares_bearer_scheme() -> None:
    card = card_for(StageKind.ANALYZE, "http://127.0.0.1:9000")
    scheme = card.security_schemes[BEARER_SCHEME_NAME]
    assert scheme.http_auth_security_scheme.scheme == "bearer"
    assert [BEARER_SCHEME_NAME] == [
        name for req in card.security_requirements for name in req.schemes
    ]


def test_card_survives_json_round_trip() -> None:
    """The card is only useful if it serializes; the SDK sends it as JSON."""
    card = card_for(StageKind.REVIEW, "http://127.0.0.1:9000")
    revived = json_format.ParseDict(json_format.MessageToDict(card), pb.AgentCard())
    assert revived.name == card.name
    assert skill_ids(revived) == [StageKind.REVIEW.value]
    assert BEARER_SCHEME_NAME in revived.security_schemes


def test_card_interface_points_at_the_rpc_root() -> None:
    card = card_for(StageKind.TEST, "http://127.0.0.1:9123")
    assert card.supported_interfaces[0].url == "http://127.0.0.1:9123/"
    assert card.capabilities.streaming is True


def test_default_ports_are_unique() -> None:
    ports = [default_port(kind) for kind in ALL_STAGES]
    assert len(set(ports)) == len(ports)
    assert len(all_cards()) == 7


def test_stage_for_unknown_skill_raises() -> None:
    with pytest.raises(KeyError):
        stage_for_skill("deploy_to_prod")


# ---------------------------------------------------------------------------
# Handler result parsing
# ---------------------------------------------------------------------------


def test_parse_completed_result() -> None:
    result = parse_stage_result(
        {"status": "completed", "artifacts": [{"name": "a", "content": "b"}]}
    )
    assert result.state is TaskState.COMPLETED
    assert result.artifacts == [("a", "b")]


def test_parse_message_key_aliases() -> None:
    """A handler should not have to remember which key names the message."""
    assert parse_stage_result({"status": "failed", "error": "boom"}).message == "boom"
    assert parse_stage_result({"status": "rejected", "reason": "no"}).message == "no"
    assert (
        parse_stage_result({"status": "input_required", "message": "q?"}).message == "q?"
    )


def test_parse_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        parse_stage_result({"status": "mostly_fine"})


def test_parse_rejects_states_a_handler_may_not_report() -> None:
    """Only the agent decides it is working or cancelled, never the handler."""
    with pytest.raises(ValueError):
        parse_stage_result({"status": "working"})
    with pytest.raises(ValueError):
        parse_stage_result({"status": "canceled"})


def test_parse_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        parse_stage_result(["completed"])


def test_non_string_artifact_content_is_stable_json() -> None:
    """Artifact hashes drive lineage staleness, so encoding must be ordered."""
    one = parse_stage_result(
        {"status": "completed", "artifacts": [{"name": "n", "content": {"b": 1, "a": 2}}]}
    )
    two = parse_stage_result(
        {"status": "completed", "artifacts": [{"name": "n", "content": {"a": 2, "b": 1}}]}
    )
    assert one.artifacts == two.artifacts
    assert json.loads(one.artifacts[0][1]) == {"a": 2, "b": 1}


# ---------------------------------------------------------------------------
# InProcessTransport
# ---------------------------------------------------------------------------


def build_in_process() -> InProcessTransport:
    return InProcessTransport(
        {
            StageKind.ANALYZE: routing_handler,
            StageKind.DESIGN: completing_handler,
            StageKind.IMPLEMENT: failing_handler,
            StageKind.REVIEW: rejecting_handler,
            StageKind.TEST: crashing_handler,
        }
    )


def test_both_transports_satisfy_the_protocol() -> None:
    assert isinstance(build_in_process(), Transport)
    assert isinstance(HttpTransport(), Transport)


async def test_in_process_completion() -> None:
    transport = build_in_process()
    outcome = await transport.invoke(
        StageKind.ANALYZE, "analyze", "Build a URL shortener", node_id="n1"
    )
    assert outcome.state is TaskState.COMPLETED
    assert outcome.ok is True
    assert [a.name for a in outcome.artifacts] == ["analysis"]
    assert outcome.artifacts[0].produced_by == "n1"


async def test_in_process_parks_on_ambiguity() -> None:
    transport = build_in_process()
    outcome = await transport.invoke(
        StageKind.ANALYZE, "analyze", "make it secure, this is vague"
    )
    assert outcome.state is TaskState.INPUT_REQUIRED
    assert outcome.state.is_interrupted is True
    assert outcome.message == "Which auth model?"


async def test_in_process_failure_and_rejection() -> None:
    transport = build_in_process()
    failed = await transport.invoke(StageKind.IMPLEMENT, "implement", "go")
    rejected = await transport.invoke(StageKind.REVIEW, "review", "go")
    assert failed.state is TaskState.FAILED
    assert failed.message == "compiler exploded"
    assert rejected.state is TaskState.REJECTED
    assert rejected.message == "out of scope for this agent"


async def test_in_process_handler_crash_becomes_failure() -> None:
    """A stage that raises must not take the orchestrator down with it."""
    transport = build_in_process()
    outcome = await transport.invoke(StageKind.TEST, "test", "go")
    assert outcome.state is TaskState.FAILED
    assert "handler blew up" in outcome.message


async def test_in_process_unknown_stage() -> None:
    transport = build_in_process()
    with pytest.raises(KeyError):
        await transport.invoke(StageKind.RELEASE_CHECK, "release_check", "go")


async def test_in_process_cancel_paths() -> None:
    transport = build_in_process()
    await transport.invoke(StageKind.ANALYZE, "analyze", "this one is vague")
    parked_id = next(
        tid for tid, state in transport._task_states.items()
        if state is TaskState.INPUT_REQUIRED
    )
    assert await transport.cancel(StageKind.ANALYZE, parked_id) is TaskState.CANCELED

    with pytest.raises(KeyError):
        await transport.cancel(StageKind.ANALYZE, "no-such-task")


async def test_in_process_cannot_cancel_a_finished_task() -> None:
    transport = build_in_process()
    await transport.invoke(StageKind.DESIGN, "design", "go")
    done_id = next(
        tid for tid, state in transport._task_states.items()
        if state is TaskState.COMPLETED
    )
    with pytest.raises(CancelNotAllowed):
        await transport.cancel(StageKind.DESIGN, done_id)


async def test_dispatcher_adapts_transport_to_dispatch_protocol() -> None:
    """`keel.dispatch` promises an A2A dispatcher lives in the transport module."""
    from keel.dispatch import StageDispatcher

    dispatcher = A2ADispatcher(build_in_process())
    assert isinstance(dispatcher, StageDispatcher)

    outcome = await dispatcher.dispatch(
        node_id="n7",
        skill_id=StageKind.DESIGN.value,
        tier=ModelTier.DEEP,
        payload={"requirement": "a URL shortener"},
    )
    assert outcome.state is TaskState.COMPLETED
    assert outcome.artifacts[0].produced_by == "n7"


async def test_dispatcher_rejects_unknown_skill() -> None:
    dispatcher = A2ADispatcher(build_in_process())
    with pytest.raises(KeyError):
        await dispatcher.dispatch("n1", "deploy", ModelTier.FAST, {})


async def test_in_process_close_is_a_noop() -> None:
    transport = build_in_process()
    assert await transport.close() is None


# ---------------------------------------------------------------------------
# Real HTTP: the protocol itself
# ---------------------------------------------------------------------------


@pytest.mark.http
class TestOverHttp:
    """Boots a stage agent on an ephemeral port and speaks real A2A to it."""

    @pytest.fixture
    async def agent(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(AUTH_TOKEN_ENV, "test-token")
        port = pick_free_port()
        card = card_for(StageKind.ANALYZE, f"http://127.0.0.1:{port}")
        served = serve(card, StageAgentExecutor(routing_handler, skill_id="analyze"), port)
        try:
            await served.wait_until_ready()
            yield served
        finally:
            served.stop()

    async def test_card_is_served_over_http(self, agent) -> None:
        raw = await httpx.AsyncClient(timeout=10).get(
            f"{agent.base_url}{AGENT_CARD_PATH}"
        )
        assert raw.status_code == 200
        assert raw.json()["capabilities"]["streaming"] is True

        card = await discover(agent.base_url)
        assert skill_ids(card) == ["analyze"]
        assert BEARER_SCHEME_NAME in card.security_schemes

    async def test_task_runs_to_completion(self, agent) -> None:
        client = StageClient(agent.base_url, node_id="n1")
        try:
            outcome = await client.invoke("analyze", "Build a URL shortener")
        finally:
            await client.close()

        assert outcome.state is TaskState.COMPLETED
        assert outcome.state.is_terminal is True
        assert [a.name for a in outcome.artifacts] == ["analysis"]
        assert outcome.artifacts[0].content == "analysis complete"
        assert outcome.artifacts[0].produced_by == "n1"

    async def test_task_parks_in_input_required(self, agent) -> None:
        """The ambiguous path must park for a human, not fail the run."""
        client = StageClient(agent.base_url)
        try:
            outcome = await client.invoke("analyze", "make it secure, this is vague")
        finally:
            await client.close()

        assert outcome.state is TaskState.INPUT_REQUIRED
        assert outcome.state.is_interrupted is True
        assert outcome.message == "Which auth model?"

    async def test_cancel_a_parked_task(self, agent) -> None:
        client = StageClient(agent.base_url)
        try:
            seen: list[str] = []
            outcome = await client.invoke(
                "analyze", "this one is vague", on_task_id=seen.append
            )
            assert outcome.state is TaskState.INPUT_REQUIRED
            assert seen, "task id should be reported while the stream is open"

            state = await client.cancel(seen[0])
        finally:
            await client.close()

        assert state is TaskState.CANCELED

    async def test_unknown_skill_is_refused_before_dispatch(self, agent) -> None:
        client = StageClient(agent.base_url)
        try:
            with pytest.raises(KeyError):
                await client.invoke("release_check", "go")
        finally:
            await client.close()

    async def test_http_transport_end_to_end(self, agent) -> None:
        transport = HttpTransport({StageKind.ANALYZE: agent.base_url})
        try:
            outcome = await transport.invoke(
                StageKind.ANALYZE, "analyze", "Build a URL shortener", node_id="n2"
            )
        finally:
            await transport.close()
        assert outcome.state is TaskState.COMPLETED
        assert outcome.artifacts[0].produced_by == "n2"


@pytest.mark.http
class TestBearerEnforcement:
    """Proves the token on the card is actually presented and actually checked."""

    @pytest.fixture
    async def guarded(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(AUTH_TOKEN_ENV, "correct-horse")
        port = pick_free_port()
        card = card_for(StageKind.DESIGN, f"http://127.0.0.1:{port}")
        served = serve(
            card,
            StageAgentExecutor(completing_handler, skill_id="design"),
            port,
            require_auth=True,
        )
        try:
            await served.wait_until_ready()
            yield served
        finally:
            served.stop()

    async def test_card_discovery_stays_open(self, guarded) -> None:
        """A client cannot know to authenticate until it has read the card."""
        card = await discover(guarded.base_url)
        assert BEARER_SCHEME_NAME in card.security_schemes

    async def test_rpc_without_a_token_is_rejected(self, guarded) -> None:
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.post(
                f"{guarded.base_url}/",
                json={"jsonrpc": "2.0", "method": "SendMessage", "params": {}, "id": "1"},
            )
        assert response.status_code == 401

    async def test_client_presents_the_token_from_the_environment(self, guarded) -> None:
        client = StageClient(guarded.base_url)
        try:
            outcome = await client.invoke("design", "design the thing")
        finally:
            await client.close()
        assert outcome.state is TaskState.COMPLETED

    @pytest.mark.parametrize("token", ["wrong-token", ""], ids=["wrong", "absent"])
    async def test_a_bad_token_does_not_get_through(self, guarded, token: str) -> None:
        """Asserts the specific failure, so this cannot pass for another reason.

        An empty token means no interceptor is installed at all, which is what
        proves the passing case above depends on the interceptor rather than on
        the guard being lenient.
        """
        client = StageClient(guarded.base_url, token=token)
        try:
            with pytest.raises(A2AClientError, match="401"):
                await client.invoke("design", "design the thing")
        finally:
            await client.close()
