"""Integration tests for the orchestration engine itself.

These drive the real Executor against a stub dispatcher, so they exercise the
governance behaviour (gates, retries, fallback, rollback, approval, safe stop,
re-planning) without a model or a network. That separation is deliberate: the
value of this project is the orchestration, so the orchestration is what gets
tested directly rather than inferred from a full run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


from keel.dispatch import StageOutcome
from keel.executor import Executor
from keel.governance.approvals import AutoApprovalBroker, ScriptedApprovalBroker
from keel.governance.audit import AuditLog
from keel.governance.lineage import LineageStore
from keel.governance.policy import PolicyEngine, SecretScanRule
from keel.models import PolicyViolation, Severity
from keel.models import (
    Artifact,
    AuditEventType,
    EngineeringProblem,
    ImpactLevel,
    ModelTier,
    NodeSpec,
    Plan,
    RetryPolicy,
    ScenarioKind,
    StageKind,
    TaskState,
)
from keel.planner import Planner
from keel.workspace import Workspace


class StubDispatcher:
    """Returns scripted outcomes per node, and counts how often it was asked."""

    def __init__(self, script: dict[str, list[StageOutcome]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, ModelTier]] = []

    async def dispatch(self, node_id, skill_id, tier, payload):
        self.calls.append((node_id, tier))
        queue = self.script.get(node_id)
        if not queue:
            return StageOutcome(state=TaskState.COMPLETED, parsed={"ok": True})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def count(self, node_id: str) -> int:
        return sum(1 for n, _ in self.calls if n == node_id)


def ok(name: str = "out.json", content: str = '{"ok": true}') -> StageOutcome:
    return StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[Artifact(name=name, content=content, produced_by="stub")],
        parsed={"ok": True},
    )


def fail(msg: str = "boom") -> StageOutcome:
    return StageOutcome(state=TaskState.FAILED, message=msg)


def build(tmp_path: Path, plan: Plan, script=None, approvals=None, policy=None):
    workspace = Workspace(tmp_path / "ws")
    dispatcher = StubDispatcher(script or {})
    audit = AuditLog("test-run", tmp_path / "runs")
    executor = Executor(
        run_id="test-run",
        dispatcher=dispatcher,
        workspace=workspace,
        audit=audit,
        policy=policy or PolicyEngine([]),
        lineage=LineageStore(),
        approvals=approvals or AutoApprovalBroker(approve=True),
        planner=Planner(),
    )
    return executor, dispatcher, audit, workspace


def node(nid, deps=(), **kw) -> NodeSpec:
    return NodeSpec(
        id=nid,
        kind=kw.pop("kind", StageKind.IMPLEMENT),
        description=nid,
        depends_on=list(deps),
        skill_id=kw.pop("skill_id", "implement"),
        **kw,
    )


PROBLEM = EngineeringProblem(
    raw_requirement="build a thing",
    intent="build a thing",
    confidence=0.9,
    scenario=ScenarioKind.GREENFIELD,
)


def test_linear_plan_runs_to_completion(tmp_path):
    plan = Plan(nodes=[node("a"), node("b", ["a"])])
    ex, disp, audit, _ = build(tmp_path, plan, {"a": [ok()], "b": [ok()]})

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED
    assert {n: r.state for n, r in result.results.items()} == {
        "a": TaskState.COMPLETED,
        "b": TaskState.COMPLETED,
    }
    assert [n for n, _ in disp.calls] == ["a", "b"]


def test_independent_nodes_dispatch_concurrently(tmp_path):
    """The two branches of a diamond must be in flight at the same time.

    Asserting on ordering alone would pass for a sequential executor, so this
    measures actual overlap instead.
    """
    plan = Plan(
        nodes=[node("root"), node("l", ["root"]), node("r", ["root"]), node("join", ["l", "r"])]
    )
    ex, disp, _, _ = build(tmp_path, plan)

    in_flight = 0
    peak = 0

    async def slow_dispatch(node_id, skill_id, tier, payload):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return ok()

    disp.dispatch = slow_dispatch
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED
    assert peak >= 2, "independent nodes did not overlap, so nothing ran in parallel"


def test_failure_is_retried_up_to_the_budget_then_fails(tmp_path):
    spec = node("a", retry=RetryPolicy(max_attempts=2, backoff_seconds=0.0))
    plan = Plan(nodes=[spec])
    ex, disp, audit, _ = build(tmp_path, plan, {"a": [fail()]})

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.FAILED
    # max_attempts=2 means the initial try plus two retries.
    assert disp.count("a") == 3
    assert result.results["a"].attempts == 3
    assert result.results["a"].used_fallback is True
    assert any(e.event_type is AuditEventType.RETRY for e in audit.events())
    assert any(e.event_type is AuditEventType.FALLBACK for e in audit.events())


def test_transient_failure_recovers_on_retry(tmp_path):
    spec = node("a", retry=RetryPolicy(max_attempts=2, backoff_seconds=0.0))
    plan = Plan(nodes=[spec])
    ex, disp, _, _ = build(tmp_path, plan, {"a": [fail(), ok()]})

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED
    assert result.results["a"].attempts == 2


def test_exit_gate_denial_rolls_the_workspace_back(tmp_path):
    """A node that trips policy must leave nothing behind."""
    leaked = 'ANTHROPIC_KEY = "' + "sk-ant" + '-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"'
    spec = node("a", retry=RetryPolicy(max_attempts=0, backoff_seconds=0.0))
    plan = Plan(nodes=[spec])

    outcome = StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[Artifact(name="app.py", content=leaked, produced_by="stub", path="app.py")],
        parsed={},
    )
    ex, _, audit, ws = build(
        tmp_path, plan, {"a": [outcome]}, policy=PolicyEngine([SecretScanRule()])
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.FAILED
    assert result.results["a"].rolled_back is True
    assert ws.list_files() == [], "the leaked file survived rollback"
    assert any(e.event_type is AuditEventType.ROLLBACK for e in audit.events())
    assert any(e.event_type is AuditEventType.POLICY_VIOLATION for e in audit.events())


def test_a_retry_is_told_why_the_previous_attempt_was_rejected(tmp_path):
    """A retry that repeats the same prompt gets the same answer.

    Observed live: the implement stage produced code missing its open-redirect
    guard, the exit gate correctly denied it, and the retry re-sent the original
    prompt unchanged. Without the gate's reasons the second attempt is a fresh
    roll of the dice at full cost, so the retry budget buys nothing.
    """
    leaked = 'KEY = "' + "sk-ant" + '-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"'
    spec = node("a", retry=RetryPolicy(max_attempts=1, backoff_seconds=0.0))
    plan = Plan(nodes=[spec])

    prompts: list[str] = []

    ex, disp, _, _ = build(tmp_path, plan, policy=PolicyEngine([SecretScanRule()]))

    async def capture(node_id, skill_id, tier, payload):
        prompts.append(payload["requirement"])
        return StageOutcome(
            state=TaskState.COMPLETED,
            artifacts=[Artifact(name="a.py", content=leaked, produced_by="a", path="a.py")],
            parsed={},
        )

    disp.dispatch = capture
    asyncio.run(ex.run(PROBLEM, plan))

    assert len(prompts) == 2, "the node did not retry"
    assert "security.secret_scan" not in prompts[0], "the first attempt was pre-poisoned"
    assert "security.secret_scan" in prompts[1], (
        "the retry was not told which rule rejected it"
    )
    assert "a.py" in prompts[1], "the retry was not told where the problem was"


def test_rollback_does_not_destroy_a_concurrent_sibling(tmp_path):
    """Regression: a failing node must not revert work a parallel node committed.

    Found in a live run. Three nodes ran in one level; the documentation node
    finished and wrote README.md, then the test-authoring node failed its exit
    gate and rolled back. Rollback restored the whole workspace snapshot, which
    had been taken before README.md existed, so the successful node's output
    was deleted by its neighbour's failure.
    """
    leaked = 'KEY = "' + "sk-ant" + '-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"'
    plan = Plan(
        nodes=[
            node("root"),
            node("good", ["root"]),
            node("bad", ["root"], retry=RetryPolicy(max_attempts=0, backoff_seconds=0.0)),
        ]
    )

    ex, disp, _, ws = build(tmp_path, plan, policy=PolicyEngine([SecretScanRule()]))

    # The two must genuinely interleave, or the bug hides. Both nodes have to
    # snapshot before either writes; the failing one then rolls back after the
    # succeeding one has already committed. Without the awaits below, gather
    # runs them to completion in turn, `bad` snapshots after README.md already
    # exists, and a global restore looks harmless.
    async def interleaved(node_id, skill_id, tier, payload):
        if node_id == "root":
            return ok()
        if node_id == "good":
            await asyncio.sleep(0.02)
            return StageOutcome(
                state=TaskState.COMPLETED,
                artifacts=[
                    Artifact(
                        name="README.md", content="# docs", produced_by="good", path="README.md"
                    )
                ],
                parsed={},
            )
        await asyncio.sleep(0.08)  # commits after `good` has landed
        return StageOutcome(
            state=TaskState.COMPLETED,
            artifacts=[
                Artifact(name="leak.py", content=leaked, produced_by="bad", path="leak.py")
            ],
            parsed={},
        )

    disp.dispatch = interleaved
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.results["bad"].rolled_back is True
    assert "leak.py" not in ws.list_files(), "the failing node's output survived"
    assert "README.md" in ws.list_files(), (
        "rollback deleted a concurrent sibling's committed output"
    )


def test_authoring_tests_is_not_asked_for_a_transcript_it_cannot_have(tmp_path):
    """Regression: the evidence rule must key on the declared contract.

    A plan has two nodes of kind TEST. One writes the suite, one runs it. Only
    the second can produce a transcript, so keying the rule on StageKind made
    the first fail a rule it could never satisfy, on every retry, forever.
    """
    from keel.governance.policy import TestEvidenceRule

    author = node(
        "author-tests",
        kind=StageKind.TEST,
        skill_id="test",
        exit_rules=["files_written"],
    )
    plan = Plan(nodes=[author])
    outcome = StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[
            Artifact(
                name="tests/test_x.py",
                content="def test_x():\n    assert True\n",
                produced_by="author-tests",
                path="tests/test_x.py",
            )
        ],
        parsed={},
    )
    ex, _, _, ws = build(
        tmp_path, plan, {"author-tests": [outcome]}, policy=PolicyEngine([TestEvidenceRule()])
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED
    assert "tests/test_x.py" in ws.list_files()


def test_a_node_that_claims_it_ran_tests_still_needs_a_transcript(tmp_path):
    """The other half of the same rule: declaring `tests_executed` obliges you."""
    from keel.governance.policy import TestEvidenceRule

    runner = node(
        "verify",
        kind=StageKind.TEST,
        skill_id="test",
        exit_rules=["tests_executed"],
        retry=RetryPolicy(max_attempts=0, backoff_seconds=0.0),
    )
    plan = Plan(nodes=[runner])
    outcome = StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[
            Artifact(
                name="verify.txt",
                content="all tests pass, the suite is green",
                produced_by="verify",
            )
        ],
        parsed={},
    )
    ex, _, _, _ = build(
        tmp_path, plan, {"verify": [outcome]}, policy=PolicyEngine([TestEvidenceRule()])
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.FAILED, "a claimed pass with no transcript was accepted"


def test_denied_approval_rejects_the_node_without_running_it(tmp_path):
    spec = node("a", impact=ImpactLevel.HIGH)
    plan = Plan(nodes=[spec])
    ex, disp, audit, _ = build(
        tmp_path, plan, {"a": [ok()]}, approvals=ScriptedApprovalBroker({"a": False})
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.results["a"].state is TaskState.REJECTED
    assert disp.count("a") == 0, "work ran despite the human declining it"
    assert any(e.event_type is AuditEventType.APPROVAL_REQUESTED for e in audit.events())


def test_input_required_parks_the_run_rather_than_failing(tmp_path):
    plan = Plan(nodes=[node("a"), node("b", ["a"])])
    parked = StageOutcome(state=TaskState.INPUT_REQUIRED, message="which auth model?")
    ex, disp, _, _ = build(tmp_path, plan, {"a": [parked]})

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.INPUT_REQUIRED
    assert result.pending_questions == ["which auth model?"]
    assert disp.count("b") == 0, "downstream work ran while waiting on a human"


def test_safe_stop_finishes_the_current_node_and_starts_no_more(tmp_path):
    plan = Plan(nodes=[node("a"), node("b", ["a"])])
    ex, disp, audit, _ = build(tmp_path, plan)

    async def stop_after_first(node_id, skill_id, tier, payload):
        ex.request_stop()
        return ok()

    disp.dispatch = stop_after_first
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.FAILED
    assert "stop requested" in result.stopped_reason
    assert "a" in result.results and "b" not in result.results
    assert any(e.event_type is AuditEventType.SAFE_STOP for e in audit.events())


def test_cheap_tier_is_actually_used_for_cheap_nodes(tmp_path):
    """The cost-routing decision has to show up in real dispatch calls."""
    plan = Plan(
        nodes=[
            node("deep", model_tier=ModelTier.DEEP),
            node("cheap", ["deep"], model_tier=ModelTier.FAST),
        ]
    )
    ex, disp, _, _ = build(tmp_path, plan)

    asyncio.run(ex.run(PROBLEM, plan))

    assert dict(disp.calls) == {"deep": ModelTier.DEEP, "cheap": ModelTier.FAST}


def test_audit_log_records_every_gate_decision(tmp_path):
    plan = Plan(nodes=[node("a")])
    ex, _, audit, _ = build(tmp_path, plan, {"a": [ok()]})

    asyncio.run(ex.run(PROBLEM, plan))

    gates = [e for e in audit.events() if e.event_type is AuditEventType.GATE_DECISION]
    assert {g.payload["gate"] for g in gates} == {"entry", "exit"}
    assert all("reason" in g.payload for g in gates), "a gate decision with no reason is not auditable"


def test_run_is_bookended_in_the_audit_log(tmp_path):
    plan = Plan(nodes=[node("a")])
    ex, _, audit, _ = build(tmp_path, plan, {"a": [ok()]})

    asyncio.run(ex.run(PROBLEM, plan))
    kinds = [e.event_type for e in audit.events()]

    assert kinds[0] is AuditEventType.RUN_STARTED
    assert kinds[-1] is AuditEventType.RUN_FINISHED
    assert AuditEventType.PLAN_CREATED in kinds


# --------------------------------------------------------------------------
# Repair loop: a failing test suite is evidence, not a verdict
# --------------------------------------------------------------------------

from keel.planner import IMPLEMENT, VERIFY  # noqa: E402


def _repair_plan() -> Plan:
    """The real shape: implement and author-tests in parallel, verify joins."""
    return Plan(
        nodes=[
            node("design", skill_id="design"),
            node(IMPLEMENT, ["design"]),
            node("author-tests", ["design"], kind=StageKind.TEST, skill_id="test"),
            node("document", ["design"], kind=StageKind.DOCUMENT, skill_id="document"),
            node(
                VERIFY,
                [IMPLEMENT, "author-tests"],
                kind=StageKind.TEST,
                skill_id="test",
                retry=RetryPolicy(max_attempts=0),
            ),
        ]
    )


def _failing_then_passing_verify(tmp_path, fail_times: int, max_repairs: int = 2):
    """Verify fails `fail_times`, then passes. Returns (result, dispatcher, audit)."""
    plan = _repair_plan()
    ex, disp, audit, _ = build(tmp_path, plan)
    ex.max_repairs = max_repairs

    state = {"verify_calls": 0}
    prompts: list[str] = []

    async def dispatch(node_id, skill_id, tier, payload):
        disp.calls.append((node_id, tier))
        if node_id == IMPLEMENT:
            prompts.append(payload["requirement"])
        return ok()

    # The verify node never reaches the dispatcher: the executor runs real
    # pytest for it. Stub that seam instead, so the test drives the outcome
    # without needing a generated service on disk.
    def fake_verify():
        state["verify_calls"] += 1
        if state["verify_calls"] <= fail_times:
            return StageOutcome(
                state=TaskState.FAILED,
                message="pytest exited 1",
                artifacts=[
                    Artifact(
                        name="verify.txt",
                        content="FAILED test_expiry.py::test_created_at_is_ignored",
                        produced_by=VERIFY,
                    )
                ],
            )
        return StageOutcome(state=TaskState.COMPLETED, parsed={"passed": True})

    disp.dispatch = dispatch
    ex._verify = fake_verify
    result = asyncio.run(ex.run(PROBLEM, plan))
    return result, disp, audit, prompts, state


def test_failed_verification_repairs_the_implementation_and_re_verifies(tmp_path):
    """The whole point: the run recovers instead of stopping at the gate."""
    result, disp, audit, prompts, state = _failing_then_passing_verify(tmp_path, fail_times=1)

    assert result.state is TaskState.COMPLETED, result.stopped_reason
    assert result.repairs == 1
    assert state["verify_calls"] == 2, "verification did not run again after the repair"
    assert disp.count(IMPLEMENT) == 2, "the implementation was not regenerated"
    assert any(e.event_type is AuditEventType.REPAIR_STARTED for e in audit.events())


def test_the_repaired_implementation_is_given_the_failing_transcript(tmp_path):
    """A repair that does not say what failed is just a re-roll at full cost."""
    _, _, _, prompts, _ = _failing_then_passing_verify(tmp_path, fail_times=1)

    assert len(prompts) == 2
    assert "test_created_at_is_ignored" not in prompts[0], "the first attempt was pre-poisoned"
    assert "test_created_at_is_ignored" in prompts[1], (
        "the repaired attempt was not told which test failed"
    )
    assert "tests are the specification" in prompts[1]


def test_repair_does_not_regenerate_stages_that_did_not_fail(tmp_path):
    """Documentation and test authoring are not at fault, so they are not redone."""
    _, disp, _, _, _ = _failing_then_passing_verify(tmp_path, fail_times=1)

    assert disp.count("author-tests") == 1, "the test suite was rewritten to fit the code"
    assert disp.count("document") == 1
    assert disp.count("design") == 1


def test_repair_is_bounded_and_the_run_stops_when_it_runs_out(tmp_path):
    """Unbounded self-repair is an infinite loop that bills by the token."""
    result, disp, audit, _, state = _failing_then_passing_verify(
        tmp_path, fail_times=99, max_repairs=2
    )

    assert result.state is TaskState.FAILED
    assert result.repairs == 2
    # One original attempt plus two repairs.
    assert state["verify_calls"] == 3
    assert disp.count(IMPLEMENT) == 3
    assert "verify failed" in result.stopped_reason


def test_a_failure_that_is_not_verification_does_not_trigger_a_repair(tmp_path):
    """Repair is scoped to the one failure it can actually address."""
    plan = Plan(nodes=[node("design", skill_id="design"), node(IMPLEMENT, ["design"])])
    ex, disp, audit, _ = build(
        tmp_path, plan, {"design": [ok()], IMPLEMENT: [fail("model refused")]}
    )
    ex.max_repairs = 2

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.FAILED
    assert result.repairs == 0
    assert not any(e.event_type is AuditEventType.REPAIR_STARTED for e in audit.events())


def test_an_approved_node_passes_its_own_change_control_gate(tmp_path):
    """Regression: approval was granted and then the gate denied the same node.

    The change-control rule holds an approval set fixed at construction, and the
    CLI built it empty. Nothing connected the approval broker to it, so every
    high-impact node was authorised by a human and then failed its exit gate for
    lacking authorisation. A live brownfield run recorded approved=true and a
    blocking change_control violation on the same node within seconds.

    The whole suite passed while that was true, which is why this test exists.
    """
    from keel.governance.policy import ChangeControlRule, PolicyEngine

    spec = node(
        "implement",
        impact=ImpactLevel.HIGH,
        retry=RetryPolicy(max_attempts=0, backoff_seconds=0.0),
    )
    plan = Plan(nodes=[spec])
    outcome = StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[
            Artifact(name="app.py", content="x = 1\n", produced_by="implement", path="app.py")
        ],
        parsed={},
    )
    # Constructed empty, exactly as the CLI does.
    engine = PolicyEngine([ChangeControlRule(approved_nodes=set())])
    ex, _, audit, ws = build(
        tmp_path,
        plan,
        {"implement": [outcome]},
        approvals=ScriptedApprovalBroker({"implement": True}),
        policy=engine,
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED, (
        f"an approved node was blocked by change control: {result.stopped_reason}"
    )
    assert "app.py" in ws.list_files()
    violations = [
        e for e in audit.events()
        if e.event_type is AuditEventType.POLICY_VIOLATION
        and e.payload.get("rule") == "change_control.approval_required"
    ]
    assert not violations, "change control fired on a node the human had approved"


def test_change_control_still_blocks_a_node_nobody_approved(tmp_path):
    """The other half: recording approvals must not disarm the rule."""
    from keel.governance.policy import ChangeControlRule, PolicyEngine

    spec = node(
        "implement",
        impact=ImpactLevel.HIGH,
        retry=RetryPolicy(max_attempts=0, backoff_seconds=0.0),
    )
    plan = Plan(nodes=[spec])
    outcome = StageOutcome(
        state=TaskState.COMPLETED,
        artifacts=[
            Artifact(name="app.py", content="x = 1\n", produced_by="implement", path="app.py")
        ],
        parsed={},
    )
    ex, _, _, ws = build(
        tmp_path,
        plan,
        {"implement": [outcome]},
        approvals=ScriptedApprovalBroker({"implement": False}),
        policy=PolicyEngine([ChangeControlRule(approved_nodes=set())]),
    )

    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.results["implement"].state is TaskState.REJECTED
    assert ws.list_files() == []


def test_two_unrelated_nodes_writing_the_same_file_is_reported(tmp_path):
    """Regression: concurrent stages silently clobbered each other.

    `document` and `implement` both wrote README.md. They have no dependency
    between them, so which content survived was whichever finished last. Both
    stages succeeded, both gates passed, and the workspace held one of two
    plausible answers with nothing recorded about it.
    """
    plan = Plan(
        nodes=[
            node("design", skill_id="design"),
            node("writer-a", ["design"]),
            node("writer-b", ["design"]),
        ]
    )
    ex, disp, audit, ws = build(tmp_path, plan)

    async def both_write_readme(node_id, skill_id, tier, payload):
        if node_id == "design":
            return ok()
        return StageOutcome(
            state=TaskState.COMPLETED,
            artifacts=[
                Artifact(
                    name="README.md",
                    content=f"written by {node_id}",
                    produced_by=node_id,
                    path="README.md",
                )
            ],
            parsed={},
        )

    disp.dispatch = both_write_readme
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.state is TaskState.COMPLETED
    conflicts = [
        e for e in audit.events()
        if e.event_type is AuditEventType.POLICY_VIOLATION
        and e.payload.get("rule") == "orchestration.concurrent_write"
    ]
    assert conflicts, "two unrelated nodes wrote the same path and nothing recorded it"
    assert "README.md" in conflicts[0].payload["message"]


def test_writing_over_a_dependency_output_is_not_a_conflict(tmp_path):
    """A later stage revising an earlier one's file is the pipeline working."""
    plan = Plan(nodes=[node("first"), node("second", ["first"])])
    ex, disp, audit, _ = build(tmp_path, plan)

    async def chain(node_id, skill_id, tier, payload):
        return StageOutcome(
            state=TaskState.COMPLETED,
            artifacts=[
                Artifact(
                    name="app.py",
                    content=f"# {node_id}",
                    produced_by=node_id,
                    path="app.py",
                )
            ],
            parsed={},
        )

    disp.dispatch = chain
    asyncio.run(ex.run(PROBLEM, plan))

    conflicts = [
        e for e in audit.events()
        if e.event_type is AuditEventType.POLICY_VIOLATION
        and e.payload.get("rule") == "orchestration.concurrent_write"
    ]
    assert not conflicts, "an ordered overwrite was reported as a race"


def test_entry_gate_does_not_judge_upstream_content(tmp_path):
    """Regression: a node was denied for a neighbour's truncated output.

    The entry gate was handed the dependencies' outputs as JSON, truncated to
    12k characters. A live run denied the verification node for an unguarded
    redirect because the handler appeared inside that summary and the SSRF
    guard had been truncated away. The node had not run, its inputs were fine,
    and the code it was about to test was correct.
    """
    from keel.governance.policy import OpenRedirectRule, PolicyEngine

    unguarded = (
        "from fastapi.responses import RedirectResponse\n"
        "def follow(code):\n"
        "    return RedirectResponse(lookup(code))\n"
    )
    plan = Plan(nodes=[node("producer"), node("consumer", ["producer"])])
    ex, disp, _, _ = build(tmp_path, plan, policy=PolicyEngine([OpenRedirectRule()]))

    async def dispatch(node_id, skill_id, tier, payload):
        disp.calls.append((node_id, tier))
        if node_id == "producer":
            # Output that would trip a content rule if the gate scanned it.
            return StageOutcome(state=TaskState.COMPLETED, parsed={"files": [], "src": unguarded})
        return ok()

    disp.dispatch = dispatch
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.results["consumer"].state is TaskState.COMPLETED, (
        f"consumer was denied for its upstream's content: {result.results['consumer'].error}"
    )


def test_entry_gate_still_denies_a_node_whose_input_never_arrived(tmp_path):
    """Scoping the gate must not make it vacuous.

    The scheduler will not offer a node until its dependencies complete, so in
    a healthy run this guard is unreachable. It is a backstop for the paths
    that do discard a completed result, such as a repair, which is exactly
    where an ordering assumption would otherwise go unchecked.
    """
    plan = Plan(nodes=[node("a"), node("b", ["a"])])
    ex, _, _, _ = build(tmp_path, plan)

    spec = plan.by_id("b")
    allowed = asyncio.run(ex._run_node(spec, {"problem": PROBLEM, "outputs": {"a": {}}}))
    denied = asyncio.run(ex._run_node(spec, {"problem": PROBLEM, "outputs": {}}))

    assert allowed.state is TaskState.COMPLETED
    assert denied.state is TaskState.REJECTED
    assert "required upstream output missing: a" in (denied.error or "")


def test_repair_does_not_fire_when_verification_was_never_run(tmp_path):
    """Regression: repair burned its budget on a gate rejection.

    A rejection means the node never executed, so regenerating the
    implementation cannot address it. A live run repaired twice against a
    verification that had been denied at its entry gate.
    """
    plan = _repair_plan()
    ex, disp, audit, _ = build(tmp_path, plan)
    ex.max_repairs = 2

    async def dispatch(node_id, skill_id, tier, payload):
        disp.calls.append((node_id, tier))
        return ok()

    disp.dispatch = dispatch

    # Deny verify at its entry gate, which is how the live failure happened:
    # the node never executed, so there is nothing for a repair to fix.
    class DenyVerifyEntry:
        rule_id = "test.deny_verify"
        severity = Severity.HIGH

        def evaluate(self, artifacts, spec):
            if spec.id != VERIFY:
                return []
            return [
                PolicyViolation(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    message="denied before running",
                )
            ]

    ex.policy = PolicyEngine([DenyVerifyEntry()])
    result = asyncio.run(ex.run(PROBLEM, plan))

    assert result.repairs == 0, "repair fired on a node that never ran"
    assert not any(e.event_type is AuditEventType.REPAIR_STARTED for e in audit.events())
    assert disp.count(IMPLEMENT) == 1, "the implementation was regenerated for nothing"
