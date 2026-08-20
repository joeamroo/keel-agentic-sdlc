"""Tests for the human-in-the-loop approval brokers.

The load-bearing tests are the ones that prove the interactive broker fails
closed. Every path that is not an explicit yes has to deny, because the failure
mode of an approval gate that defaults open is that nobody ever finds out it
was open.
"""

from __future__ import annotations

import pytest

from keel.governance.approvals import (
    ApprovalBroker,
    AutoApprovalBroker,
    InteractiveApprovalBroker,
    ScriptedApprovalBroker,
    broker_for,
    needs_approval,
)
from keel.models import ApprovalRequest, ImpactLevel, NodeSpec, StageKind


def req(
    node_id: str = "implement-api",
    impact: ImpactLevel = ImpactLevel.HIGH,
    reason: str = "public API surface changes",
    details: str = "",
) -> ApprovalRequest:
    return ApprovalRequest(node_id=node_id, reason=reason, impact=impact, details=details)


def node(node_id: str, impact: ImpactLevel) -> NodeSpec:
    return NodeSpec(id=node_id, kind=StageKind.IMPLEMENT, description="", impact=impact)


# ----------------------------------------------------------------------
# needs_approval
# ----------------------------------------------------------------------


def test_needs_approval_only_for_high_impact() -> None:
    assert needs_approval(node("a", ImpactLevel.HIGH)) is True
    assert needs_approval(node("b", ImpactLevel.MEDIUM)) is False
    assert needs_approval(node("c", ImpactLevel.LOW)) is False


def test_needs_approval_tracks_the_model_rather_than_reimplementing_it() -> None:
    spec = node("a", ImpactLevel.LOW)
    assert needs_approval(spec) == spec.needs_approval
    spec.impact = ImpactLevel.HIGH
    assert needs_approval(spec) == spec.needs_approval is True


# ----------------------------------------------------------------------
# The base contract
# ----------------------------------------------------------------------


def test_the_base_broker_is_abstract() -> None:
    with pytest.raises(TypeError):
        ApprovalBroker()  # type: ignore[abstract]


def test_every_broker_starts_with_an_empty_history() -> None:
    for broker in (
        AutoApprovalBroker(),
        ScriptedApprovalBroker({"a": True}),
        InteractiveApprovalBroker(),
    ):
        assert broker.history == []


def test_history_is_per_instance() -> None:
    first, second = AutoApprovalBroker(), AutoApprovalBroker()
    first.request(req("a"))

    assert len(first.history) == 1
    assert second.history == []


# ----------------------------------------------------------------------
# AutoApprovalBroker
# ----------------------------------------------------------------------


def test_auto_broker_approves_by_default() -> None:
    broker = AutoApprovalBroker()
    request = req("implement-api")
    decision = broker.request(request)

    assert decision.approved is True
    assert decision.request_id == request.request_id
    assert decision.decided_by == "auto"
    assert decision.note


def test_auto_broker_can_deny_everything() -> None:
    """The safe-stop path needs to be exercisable without a human typing n."""
    decision = AutoApprovalBroker(approve=False).request(req())
    assert decision.approved is False


def test_auto_broker_records_every_request() -> None:
    broker = AutoApprovalBroker()
    broker.request(req("a"))
    broker.request(req("b"))

    assert [request.node_id for request, _ in broker.history] == ["a", "b"]
    assert len(broker.approvals) == 2
    assert broker.denials == []


def test_denials_and_approvals_partition_the_history() -> None:
    broker = ScriptedApprovalBroker({"a": True, "b": False})
    broker.request(req("a"))
    broker.request(req("b"))

    assert [q.node_id for q, _ in broker.approvals] == ["a"]
    assert [q.node_id for q, _ in broker.denials] == ["b"]


# ----------------------------------------------------------------------
# ScriptedApprovalBroker
# ----------------------------------------------------------------------


def test_scripted_broker_answers_per_node() -> None:
    broker = ScriptedApprovalBroker({"implement-api": True, "release-db": False})

    assert broker.request(req("implement-api")).approved is True
    assert broker.request(req("release-db")).approved is False


def test_scripted_broker_is_reproducible() -> None:
    """Same script, same plan, same trace. That is the point of this broker."""
    script = {"a": True, "b": False, "c": True}
    runs = [
        [ScriptedApprovalBroker(script).request(req(n)).approved for n in ("a", "b", "c")]
        for _ in range(3)
    ]

    assert runs == [[True, False, True]] * 3


def test_scripted_broker_raises_on_an_unscripted_node() -> None:
    """A stale script must fail loudly instead of rubber-stamping."""
    broker = ScriptedApprovalBroker({"a": True})

    with pytest.raises(KeyError) as excinfo:
        broker.request(req("brand-new-node"))

    assert "brand-new-node" in str(excinfo.value)


def test_the_unscripted_error_names_what_it_does_know() -> None:
    broker = ScriptedApprovalBroker({"alpha": True, "beta": False})

    with pytest.raises(KeyError) as excinfo:
        broker.request(req("gamma"))

    message = str(excinfo.value)
    assert "alpha" in message and "beta" in message


def test_an_unscripted_node_is_not_recorded_as_a_decision() -> None:
    broker = ScriptedApprovalBroker({"a": True})
    with pytest.raises(KeyError):
        broker.request(req("z"))

    assert broker.history == []


def test_scripted_broker_copies_its_script() -> None:
    script = {"a": True}
    broker = ScriptedApprovalBroker(script)
    script["a"] = False

    assert broker.request(req("a")).approved is True


def test_scripted_decisions_are_attributed_to_the_script() -> None:
    decision = ScriptedApprovalBroker({"a": True}).request(req("a"))
    assert decision.decided_by == "script"


# ----------------------------------------------------------------------
# InteractiveApprovalBroker
# ----------------------------------------------------------------------


def test_interactive_broker_denies_on_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare Enter is not consent. This is the fail-closed guarantee."""
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    decision = broker.request(req())

    assert decision.approved is False
    assert "empty" in decision.note


def test_interactive_broker_denies_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-interactive stdin raises EOFError immediately. Still not consent."""

    def raise_eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    decision = broker.request(req())

    assert decision.approved is False
    assert "no input" in decision.note


def test_interactive_broker_denies_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    assert broker.request(req()).approved is False


def test_interactive_broker_denies_unrecognized_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo is not a yes."""
    monkeypatch.setattr("builtins.input", lambda _prompt="": "maybe later")
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    decision = broker.request(req())

    assert decision.approved is False
    assert "unrecognized" in decision.note


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "  YES  "])
def test_interactive_broker_approves_on_yes(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    decision = broker.request(req())

    assert decision.approved is True
    assert decision.decided_by == "human"


@pytest.mark.parametrize("answer", ["n", "N", "no", " No "])
def test_interactive_broker_denies_on_no(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)

    assert broker.request(req()).approved is False


def test_interactive_broker_shows_what_is_being_approved() -> None:
    """You cannot consent to a request you cannot read."""
    written: list[str] = []
    broker = InteractiveApprovalBroker(
        input_fn=lambda _prompt: "y", output_fn=written.append
    )
    broker.request(
        req(
            node_id="release-db-migration",
            impact=ImpactLevel.HIGH,
            reason="destructive schema change",
            details="drops column accounts.legacy_id",
        )
    )

    shown = "\n".join(written)
    assert "release-db-migration" in shown
    assert "HIGH" in shown
    assert "destructive schema change" in shown
    assert "drops column accounts.legacy_id" in shown


def test_interactive_broker_accepts_an_injected_input_function() -> None:
    broker = InteractiveApprovalBroker(input_fn=lambda _prompt: "y", output_fn=lambda _t: None)
    assert broker.request(req()).approved is True


def test_interactive_broker_records_history(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["y", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    broker = InteractiveApprovalBroker(output_fn=lambda _text: None)
    broker.request(req("a"))
    broker.request(req("b"))

    assert [decision.approved for _, decision in broker.history] == [True, False]


def test_the_prompt_advertises_the_default_as_deny() -> None:
    """The [y/N] capitalization is the contract the operator reads."""
    prompts: list[str] = []

    def capture(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    broker = InteractiveApprovalBroker(input_fn=capture, output_fn=lambda _t: None)
    broker.request(req())

    assert "[y/N]" in prompts[0]


# ----------------------------------------------------------------------
# broker_for
# ----------------------------------------------------------------------


def test_broker_for_prefers_a_script() -> None:
    assert isinstance(broker_for(interactive=True, scripted={"a": True}), ScriptedApprovalBroker)


def test_broker_for_returns_interactive_then_auto() -> None:
    assert isinstance(broker_for(interactive=True), InteractiveApprovalBroker)
    assert isinstance(broker_for(interactive=False), AutoApprovalBroker)
