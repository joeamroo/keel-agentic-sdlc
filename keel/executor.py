"""The engine: gates, concurrency, retry, fallback, rollback, safe stop, re-plan.

Everything the governance plane promises happens here, around each node:

    entry gate -> snapshot -> dispatch -> exit gate -> commit
                                |            |
                                |            +-- deny -> retry -> fallback -> rollback
                                +-- fail ----+

Two structural choices worth defending.

First, the scheduler recomputes the ready set every round instead of walking a
precomputed list of levels. Levels are fine until the plan changes underneath
you, and re-planning is a hard requirement here, so the loop asks the graph what
is runnable now rather than assuming what was runnable at the start.

Second, everything a node touches is snapshotted before it runs. A stage that
fails halfway through writing six files has left the workspace in a state no
later stage was designed for, and the cheapest correct answer is to put it back.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from keel.dispatch import StageDispatcher, StageOutcome
from keel.graph import PlanGraph
from keel.models import (
    ApprovalRequest,
    Artifact,
    AuditEventType,
    EngineeringProblem,
    NodeResult,
    NodeSpec,
    Plan,
    TaskState,
)
from keel.planner import VERIFY, Planner

if TYPE_CHECKING:  # imported for typing only, so the engine stays unit-testable
    from keel.governance.approvals import ApprovalBroker
    from keel.governance.audit import AuditLog
    from keel.governance.lineage import LineageStore
    from keel.governance.policy import PolicyEngine
    from keel.ui.live import LiveView
    from keel.workspace import Workspace


@dataclass(slots=True)
class RunResult:
    run_id: str
    plan: Plan
    results: dict[str, NodeResult] = field(default_factory=dict)
    state: TaskState = TaskState.WORKING
    stopped_reason: str = ""
    plan_revisions: int = 0
    pending_questions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state is TaskState.COMPLETED


class SafeStop(Exception):
    """Raised internally to unwind the run without leaving work half-applied."""


class Executor:
    def __init__(
        self,
        *,
        run_id: str,
        dispatcher: StageDispatcher,
        workspace: Workspace,
        audit: AuditLog,
        policy: PolicyEngine,
        lineage: LineageStore,
        approvals: ApprovalBroker,
        planner: Planner | None = None,
        view: LiveView | None = None,
        max_plan_revisions: int = 3,
    ):
        self.run_id = run_id
        self.dispatcher = dispatcher
        self.workspace = workspace
        self.audit = audit
        self.policy = policy
        self.lineage = lineage
        self.approvals = approvals
        self.planner = planner or Planner()
        self.view = view
        self.max_plan_revisions = max_plan_revisions

        # Set this to stop cleanly at the next node boundary. Cancelling
        # mid-node would be a worse guarantee than finishing the node and
        # refusing to start another.
        self.stop_requested = asyncio.Event()

    # -- public ----------------------------------------------------------

    async def run(self, problem: EngineeringProblem, plan: Plan) -> RunResult:
        graph = PlanGraph(plan)
        result = RunResult(run_id=self.run_id, plan=plan)

        self.audit.emit(
            AuditEventType.RUN_STARTED,
            {"requirement": problem.raw_requirement, "scenario": problem.scenario.value},
        )
        self.audit.emit(
            AuditEventType.PLAN_CREATED,
            {"version": plan.version, "nodes": [n.id for n in plan.nodes], "rationale": plan.rationale},
        )
        if self.view:
            self.view.set_plan(plan)

        completed: set[str] = set()
        context: dict[str, Any] = {"problem": problem, "outputs": {}}

        try:
            while True:
                if self.stop_requested.is_set():
                    raise SafeStop("stop requested")

                ready = [n for n in graph.ready(completed) if n.id not in result.results]
                if not ready:
                    break

                batch = await asyncio.gather(
                    *(self._run_node(node, context) for node in ready),
                    return_exceptions=True,
                )

                for node, outcome in zip(ready, batch, strict=True):
                    if isinstance(outcome, SafeStop):
                        raise outcome
                    if isinstance(outcome, BaseException):
                        outcome = NodeResult(
                            node_id=node.id,
                            state=TaskState.FAILED,
                            error=f"{type(outcome).__name__}: {outcome}",
                        )
                    result.results[node.id] = outcome

                    if outcome.state is TaskState.COMPLETED:
                        completed.add(node.id)
                    elif outcome.state is TaskState.INPUT_REQUIRED:
                        result.state = TaskState.INPUT_REQUIRED
                        result.pending_questions.append(outcome.error or "input required")
                        raise SafeStop(f"{node.id} needs human input")
                    else:
                        raise SafeStop(f"{node.id} failed: {outcome.error}")

                revised = self._maybe_replan(graph, context, result)
                if revised is not None:
                    graph = revised

            result.state = TaskState.COMPLETED

        except SafeStop as stop:
            if result.state is TaskState.WORKING:
                result.state = TaskState.FAILED
            result.stopped_reason = str(stop)
            self.audit.emit(AuditEventType.SAFE_STOP, {"reason": str(stop)})

        self.audit.emit(
            AuditEventType.RUN_FINISHED,
            {"state": result.state.value, "reason": result.stopped_reason},
        )
        return result

    def request_stop(self) -> None:
        """Ask the run to stop at the next node boundary."""
        self.stop_requested.set()

    # -- per node --------------------------------------------------------

    async def _run_node(self, node: NodeSpec, context: dict[str, Any]) -> NodeResult:
        started = time.time()
        result = NodeResult(node_id=node.id, state=TaskState.WORKING, started_at=started)
        self.audit.emit(AuditEventType.NODE_STARTED, {"kind": node.kind.value}, node_id=node.id)
        if self.view:
            self.view.on_node_start(node.id)

        # Approval comes before the entry gate: asking a human to sign off on
        # work whose preconditions have not been checked wastes their time.
        if node.needs_approval and not await self._approve(node, result):
            result.state = TaskState.REJECTED
            result.error = "human declined the change"
            return self._finish(node, result)

        entry = self.policy.decide("entry", node, self._inputs_for(node, context))
        result.gate_decisions.append(entry)
        self.audit.emit(
            AuditEventType.GATE_DECISION,
            {"gate": "entry", "allowed": entry.allowed, "reason": entry.reason},
            node_id=node.id,
        )
        if self.view:
            self.view.on_gate(entry)
        if not entry.allowed:
            result.state = TaskState.REJECTED
            result.error = f"entry gate: {entry.reason}"
            return self._finish(node, result)

        snapshot = self.workspace.snapshot() if node.rollback else None
        # Paths this node wrote, accumulated across attempts. Rollback is
        # scoped to these rather than to the whole workspace, because siblings
        # in the same level are writing concurrently and a global restore would
        # delete their committed output.
        written_paths: set[str] = set()
        # Why the previous attempt failed, fed into the next one. A retry that
        # repeats the same prompt gets the same answer, so an unfed retry burns
        # a full generation to learn nothing. This is what makes the retry
        # budget worth spending.
        last_failure: str = ""

        attempts = node.retry.max_attempts + 1
        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            if attempt > 1:
                delay = node.retry.delay_for(attempt - 1)
                self.audit.emit(
                    AuditEventType.RETRY, {"attempt": attempt, "delay": delay}, node_id=node.id
                )
                if self.view:
                    self.view.on_retry(node.id, attempt)
                await asyncio.sleep(delay)

            use_fallback = attempt == attempts and attempt > 1
            if use_fallback:
                result.used_fallback = True
                self.audit.emit(
                    AuditEventType.FALLBACK,
                    {"hint": node.retry.fallback_hint, "tier": (node.retry.fallback_tier or node.model_tier).value},
                    node_id=node.id,
                )

            outcome = await self._dispatch(node, context, use_fallback, last_failure)

            result.input_tokens += outcome.input_tokens
            result.output_tokens += outcome.output_tokens
            result.cost_usd += outcome.cost_usd

            if outcome.state is TaskState.INPUT_REQUIRED:
                result.state = TaskState.INPUT_REQUIRED
                result.error = outcome.message
                return self._finish(node, result)

            if outcome.state is not TaskState.COMPLETED:
                result.error = outcome.message or "stage failed"
                last_failure = result.error
                continue

            written = self._persist(node, outcome)
            written_paths.update(a.path for a in written if a.path)
            exit_gate = self.policy.decide("exit", node, written)
            result.gate_decisions.append(exit_gate)
            self.audit.emit(
                AuditEventType.GATE_DECISION,
                {
                    "gate": "exit",
                    "allowed": exit_gate.allowed,
                    "reason": exit_gate.reason,
                    "violations": [v.rule_id for v in exit_gate.violations],
                },
                node_id=node.id,
            )
            if self.view:
                self.view.on_gate(exit_gate)

            for violation in exit_gate.violations:
                self.audit.emit(
                    AuditEventType.POLICY_VIOLATION,
                    {"rule": violation.rule_id, "severity": violation.severity.value,
                     "message": violation.message, "location": violation.location},
                    node_id=node.id,
                )

            if exit_gate.allowed:
                result.state = TaskState.COMPLETED
                result.artifacts = written
                context["outputs"][node.id] = outcome.parsed or {}
                for artifact in written:
                    self.lineage.record_production(artifact)
                return self._finish(node, result)

            result.error = f"exit gate: {exit_gate.reason}"
            last_failure = _failure_brief(exit_gate.reason, exit_gate.violations)
            if snapshot is not None:
                self._rollback(node, snapshot, result, written_paths)

        result.state = TaskState.FAILED
        if snapshot is not None and not result.rolled_back:
            self._rollback(node, snapshot, result, written_paths)
        return self._finish(node, result)

    # -- helpers ---------------------------------------------------------

    async def _dispatch(
        self,
        node: NodeSpec,
        context: dict[str, Any],
        use_fallback: bool,
        last_failure: str = "",
    ) -> StageOutcome:
        """Run the stage, or run the real verification for the verify node."""
        if node.id == VERIFY:
            return self._verify()

        tier = (node.retry.fallback_tier or node.model_tier) if use_fallback else node.model_tier
        return await self.dispatcher.dispatch(
            node_id=node.id,
            skill_id=node.skill_id,
            tier=tier,
            payload=self._payload(node, context, use_fallback, last_failure),
        )

    def _verify(self) -> StageOutcome:
        """Actually execute the generated test suite.

        This is the difference between a system that claims its output works and
        one that knows. The exit code is the evidence and it is recorded verbatim.
        """
        # sys.executable, not "python". A bare `python` is absent on macOS and
        # on most modern distributions, and the failure is silent in the worst
        # way: exit 127 means the command never ran, so a gate that treated any
        # non-zero code as "tests failed" would report a red suite that was
        # never executed. Using the running interpreter also guarantees the
        # generated tests see the same environment the orchestrator does.
        code, out, err = self.workspace.run_command(
            [sys.executable, "-m", "pytest", "-q", "--no-header"], timeout=180
        )
        if code == 127:
            return StageOutcome(
                state=TaskState.FAILED,
                message=f"pytest could not be executed at all ({sys.executable})",
                artifacts=[
                    Artifact(name="verify.txt", content=f"exit=127\n{err}", produced_by=VERIFY)
                ],
                parsed={"exit_code": code, "passed": False, "executed": False},
            )
        transcript = f"exit={code}\n\n--- stdout ---\n{out}\n\n--- stderr ---\n{err}"
        return StageOutcome(
            state=TaskState.COMPLETED if code == 0 else TaskState.FAILED,
            artifacts=[Artifact(name="verify.txt", content=transcript, produced_by=VERIFY)],
            parsed={"exit_code": code, "passed": code == 0, "output": out[-4000:]},
            message="tests passed" if code == 0 else f"pytest exited {code}",
        )

    def _payload(
        self,
        node: NodeSpec,
        context: dict[str, Any],
        use_fallback: bool,
        last_failure: str = "",
    ) -> dict[str, Any]:
        """Build the template variables every stage might ask for.

        The full union is supplied rather than a per-stage subset, because a
        stage that quietly adds a variable would otherwise fail at dispatch
        time with a bare KeyError, several minutes and several dollars into a
        run. Unused keys are ignored by `render`, so over-supplying is free.
        """
        problem: EngineeringProblem = context["problem"]
        outputs: dict[str, Any] = context["outputs"]
        source = self._existing_code()

        payload = {
            "requirement": problem.raw_requirement,
            "intent": problem.intent or problem.raw_requirement,
            "scenario": problem.scenario.value,
            "acceptance_criteria": "\n".join(f"- {c}" for c in problem.acceptance_criteria)
            or "(none stated)",
            "constraints": "\n".join(f"- {c}" for c in problem.constraints) or "(none stated)",
            "existing_code": source,
            "prior_answers": "\n".join(
                f"- {a.question}: {a.answer}" for a in problem.ambiguities if a.resolved
            )
            or "(none)",
            "design": _as_text(outputs.get("design")),
            "impact": _as_text(outputs.get("impact-analysis")),
            "implementation": source,
            "artifacts": source,
            "source": source,
            "test_results": _as_text(outputs.get(VERIFY)),
            "review": _as_text(outputs.get("review")),
            "review_findings": _as_text(outputs.get("review")),
        }
        if last_failure:
            payload["requirement"] += (
                "\n\nA previous attempt at this stage was rejected by the quality gate. "
                "Fix the cause rather than working around the check.\n"
                f"{last_failure}"
            )
        if use_fallback and node.retry.fallback_hint:
            payload["requirement"] += (
                f"\n\nThis is the final attempt. Adjust approach: {node.retry.fallback_hint}."
            )
        return payload

    def _existing_code(self) -> str:
        files = self.workspace.list_files()
        if not files:
            return "(empty workspace, this is a new system)"
        chunks = []
        for path in files[:40]:
            try:
                chunks.append(f"--- {path} ---\n{self.workspace.read(path)}")
            except Exception:  # noqa: BLE001 - unreadable file should not kill the run
                continue
        return "\n\n".join(chunks)

    def _inputs_for(self, node: NodeSpec, context: dict[str, Any]) -> list[Artifact]:
        """Artifacts this node consumes, used by the entry gate and lineage."""
        consumed: list[Artifact] = []
        for dep in node.depends_on:
            parsed = context["outputs"].get(dep)
            if parsed is None:
                continue
            artifact = Artifact(
                name=f"{dep}.json", content=_as_text(parsed), produced_by=dep
            )
            consumed.append(artifact)
            self.lineage.record_consumption(node.id, artifact)
        return consumed

    def _persist(self, node: NodeSpec, outcome: StageOutcome) -> list[Artifact]:
        """Write file artifacts into the workspace so later stages can use them."""
        written: list[Artifact] = []
        for artifact in outcome.artifacts:
            if artifact.path:
                stored = self.workspace.write(artifact.path, artifact.content)
                stored.produced_by = node.id
                written.append(stored)
                self.audit.emit(
                    AuditEventType.ARTIFACT_WRITTEN,
                    {"path": artifact.path, "sha": stored.sha},
                    node_id=node.id,
                )
            else:
                written.append(artifact)
        return written

    def _rollback(
        self, node: NodeSpec, snapshot: Any, result: NodeResult, paths: set[str]
    ) -> None:
        """Undo only what this node wrote.

        Scoped rather than global. Nodes in the same topological level run
        concurrently against one workspace, so restoring the whole snapshot
        would revert a sibling that has already committed.
        """
        changed = self.workspace.restore_paths(snapshot, sorted(paths))
        result.rolled_back = True
        self.audit.emit(
            AuditEventType.ROLLBACK,
            {"restored": changed, "scope": sorted(paths)},
            node_id=node.id,
        )

    async def _approve(self, node: NodeSpec, result: NodeResult) -> bool:
        request = ApprovalRequest(
            node_id=node.id,
            reason=f"{node.kind.value} stage modifies an existing public surface",
            impact=node.impact,
            details=node.description,
        )
        self.audit.emit(
            AuditEventType.APPROVAL_REQUESTED,
            {"impact": node.impact.value, "reason": request.reason},
            node_id=node.id,
        )
        if self.view:
            self.view.on_approval_request(request)

        decision = self.approvals.request(request)
        if asyncio.iscoroutine(decision):
            decision = await decision

        self.audit.emit(
            AuditEventType.APPROVAL_DECIDED,
            {"approved": decision.approved, "by": decision.decided_by, "note": decision.note},
            node_id=node.id,
        )
        return decision.approved

    def _maybe_replan(
        self, graph: PlanGraph, context: dict[str, Any], result: RunResult
    ) -> PlanGraph | None:
        """Ask the planner whether new evidence invalidates the current plan."""
        if result.plan_revisions >= self.max_plan_revisions:
            return None

        evidence = context["outputs"].get("impact-analysis")
        if not isinstance(evidence, dict):
            return None

        revised = self.planner.revise_from_evidence(graph.plan, evidence)
        if revised is None:
            return None

        result.plan = revised
        result.plan_revisions += 1
        self.audit.emit(
            AuditEventType.PLAN_REVISED,
            {
                "version": revised.version,
                "supersedes": revised.supersedes,
                "rationale": revised.rationale,
                "nodes": [n.id for n in revised.nodes],
            },
        )
        if self.view:
            self.view.set_plan(revised)
            self.view.note(f"plan revised to v{revised.version}: {revised.rationale}")
        return PlanGraph(revised)

    def _finish(self, node: NodeSpec, result: NodeResult) -> NodeResult:
        result.ended_at = time.time()
        self.audit.emit(
            AuditEventType.NODE_FINISHED,
            {
                "state": result.state.value,
                "attempts": result.attempts,
                "duration": round(result.duration, 3),
                "cost_usd": round(result.cost_usd, 6),
                "rolled_back": result.rolled_back,
                "used_fallback": result.used_fallback,
                "error": result.error,
            },
            node_id=node.id,
        )
        if self.view:
            self.view.on_node_end(result)
        return result


def _failure_brief(reason: str, violations: list) -> str:
    """Turn a gate denial into something a model can act on.

    The rule id alone is useless to the stage that has to fix it, so the
    message and the location go too. Truncated because a retry prompt that
    carries every detail of the last failure crowds out the requirement.
    """
    lines = [f"Gate result: {reason}"]
    for violation in violations[:8]:
        where = f" at {violation.location}" if violation.location else ""
        lines.append(f"- [{violation.severity.value}] {violation.rule_id}{where}: {violation.message}")
    return "\n".join(lines)[:4000]


def _as_text(value: Any) -> str:
    if value is None:
        return "(not available)"
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, indent=2, sort_keys=True)[:12000]
