"""Reliability metrics for a run.

§4.4 names four reliability metrics explicitly: success rate, retry/rollback
frequency, MTTR, and end-to-end latency. This module computes exactly those,
plus the cost and token totals that justify the tiered-model routing decision.
Nothing more, because a metrics module that invents numbers to look complete is
worse than one that admits what it does not know.

Two sources feed the computation, and both are things the orchestrator already
produces: the list of `NodeResult` objects (what happened to each node) and the
append-only `AuditEvent` log (when it happened, in order).

Three honesty rules govern everything here:

1. MTTR is `None` when nothing ever failed. Reporting 0.0 would read as "we
   recover instantly", which is a completely different claim from "we were
   never asked to recover". No failures means undefined, not zero.
2. Counts are never invented and never silently dropped. Retry, rollback and
   fallback frequency each appear in two places, and each place is only a
   lower bound: a node that retried twice and then hit a safe stop may never
   have produced a `NodeResult` at all, while a run whose audit log was
   truncated still has its results. We take the larger of the two.
3. A node that was canceled counts in the denominator of the success rate but
   in neither the succeeded nor the failed bucket. It did not deliver, so
   flattering the rate by excluding it would be dishonest, but calling it a
   failure would overstate how often the system actually breaks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any

from keel.models import (
    AuditEvent,
    AuditEventType,
    NodeResult,
    NodeSpec,
    RunMetrics,
    StageKind,
    TaskState,
)

# A node in one of these states did not deliver its artifacts and said so.
# CANCELED is deliberately absent: see honesty rule 3 in the module docstring.
_FAILED_STATES: frozenset[TaskState] = frozenset({TaskState.FAILED, TaskState.REJECTED})

# Where a NODE_FINISHED event is expected to carry the node's terminal state.
# Both spellings are accepted so the orchestrator and this module can be
# written independently without a coordination round trip.
_STATE_KEYS: tuple[str, ...] = ("state", "task_state")

# Bucket for results whose stage cannot be established. Named rather than
# guessed, so a reviewer can see how much of the breakdown is unattributed.
UNKNOWN_STAGE = "unknown"

_SEPARATORS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class RecoveryInterval:
    """One observed failure-to-recovery span for a single node.

    Exposed rather than kept private because MTTR is a mean, and a mean with
    no visible sample is exactly the kind of number a reviewer is right to
    distrust. `MetricsCollector.recovery_intervals` hands back the raw spans
    so the aggregate can be checked by hand against the audit log.
    """

    node_id: str
    failed_at: float
    recovered_at: float

    @property
    def seconds(self) -> float:
        # Clamped because event ordering is by sequence number first, and a
        # clock that stepped backwards must not produce a negative recovery.
        return max(0.0, self.recovered_at - self.failed_at)


class MetricsCollector:
    """Turns raw run output into `RunMetrics`.

    Constructed with the plan's nodes when they are available, because
    `NodeResult` deliberately does not carry its `StageKind` and the frozen
    contract is not ours to widen. With the nodes in hand the per-stage
    breakdown is exact; without them the stage is inferred from the node id,
    which is good enough for display and honest about what it could not
    resolve.

    Stateless apart from that index, so one collector can serve many runs.
    """

    def __init__(self, nodes: Iterable[NodeSpec] | None = None) -> None:
        self._stage_by_node: dict[str, StageKind] = {
            n.id: n.kind for n in (nodes or ())
        }

    # ------------------------------------------------------------------
    # The headline entry point
    # ------------------------------------------------------------------

    def from_results(
        self,
        run_id: str,
        results: Sequence[NodeResult],
        events: Sequence[AuditEvent],
    ) -> RunMetrics:
        """Compute the §4.4 metric set for one run.

        Events belonging to other runs are filtered out rather than trusted,
        so a shared audit log cannot leak one run's failures into another's
        MTTR.
        """
        own_events = [e for e in events if e.run_id == run_id]
        final = _final_results(results)

        succeeded = sum(1 for r in final if r.state is TaskState.COMPLETED)
        failed = sum(1 for r in final if r.state in _FAILED_STATES)

        return RunMetrics(
            run_id=run_id,
            total_nodes=len(final),
            succeeded=succeeded,
            failed=failed,
            # Each pair is (from results, from audit log). Both are lower
            # bounds on the truth, so the larger is the safer report.
            retries=max(
                sum(max(0, r.attempts - 1) for r in final),
                _count(own_events, AuditEventType.RETRY),
            ),
            rollbacks=max(
                sum(1 for r in final if r.rolled_back),
                _count(own_events, AuditEventType.ROLLBACK),
            ),
            fallbacks=max(
                sum(1 for r in final if r.used_fallback),
                _count(own_events, AuditEventType.FALLBACK),
            ),
            approvals_requested=_count(own_events, AuditEventType.APPROVAL_REQUESTED),
            plan_revisions=_count(own_events, AuditEventType.PLAN_REVISED),
            e2e_latency_seconds=self.e2e_latency(own_events, final),
            mttr_seconds=self.mttr(own_events),
            cost_usd=sum(r.cost_usd for r in final),
            input_tokens=sum(r.input_tokens for r in final),
            output_tokens=sum(r.output_tokens for r in final),
        )

    # ------------------------------------------------------------------
    # MTTR
    # ------------------------------------------------------------------

    def recovery_intervals(self, events: Sequence[AuditEvent]) -> list[RecoveryInterval]:
        """Pair every node failure with the success that repaired it.

        Walks the log in append order and keeps one pending failure per node.
        Two details are deliberate:

        * The FIRST failure in a run of consecutive failures starts the clock.
          A node that fails, retries, fails again and then succeeds took the
          whole span to recover, not just the span since the last attempt.
          Measuring from the last attempt would quietly understate MTTR.
        * A RETRY event counts as a failure marker on its own, because a retry
          only ever follows a failed attempt. That keeps the measurement
          working even if the orchestrator logs per-attempt failures as RETRY
          rather than as an intermediate NODE_FINISHED.

        A failure with no later success is not an interval. It is an outage
        that never ended, and averaging it in as if it had recovered would be
        the most flattering possible lie.
        """
        pending: dict[str, float] = {}
        intervals: list[RecoveryInterval] = []

        for event in _ordered(events):
            if event.node_id is None:
                continue
            if _is_failure_marker(event):
                # setdefault, not assignment: keep the first failure.
                pending.setdefault(event.node_id, event.at)
            elif _is_success_marker(event):
                failed_at = pending.pop(event.node_id, None)
                if failed_at is not None:
                    intervals.append(
                        RecoveryInterval(
                            node_id=event.node_id,
                            failed_at=failed_at,
                            recovered_at=event.at,
                        )
                    )
        return intervals

    def mttr(self, events: Sequence[AuditEvent]) -> float | None:
        """Mean time to recovery, or `None` when nothing ever recovered.

        `None` covers two genuinely different situations that share one
        correct answer: nothing failed, and nothing that failed came back.
        In both cases the sample is empty, and the mean of an empty sample is
        undefined. Zero is not a stand-in for undefined.
        """
        intervals = self.recovery_intervals(events)
        if not intervals:
            return None
        return fmean(i.seconds for i in intervals)

    # ------------------------------------------------------------------
    # End-to-end latency
    # ------------------------------------------------------------------

    def e2e_latency(
        self,
        events: Sequence[AuditEvent],
        results: Sequence[NodeResult] = (),
    ) -> float:
        """Wall clock from RUN_STARTED to RUN_FINISHED.

        Those two markers are the definition. The fallbacks below exist only
        so a crashed or in-flight run still reports something defensible
        instead of a bare zero, and each one measures strictly less than the
        real end-to-end time, never more.
        """
        ordered = _ordered(events)
        started = next(
            (e.at for e in ordered if e.event_type is AuditEventType.RUN_STARTED), None
        )
        finished = next(
            (e.at for e in reversed(ordered) if e.event_type is AuditEventType.RUN_FINISHED),
            None,
        )
        if started is not None and finished is not None:
            return max(0.0, finished - started)

        # No clean pair of run markers. Fall back to the widest span actually
        # observed, preferring the audit log because it brackets the node work
        # on both sides (intake before the first node, teardown after the last).
        if ordered:
            return max(0.0, ordered[-1].at - ordered[0].at)

        timed = [r for r in results if r.ended_at or r.started_at]
        if timed:
            return max(0.0, max(r.ended_at for r in timed) - min(r.started_at for r in timed))
        return 0.0

    # ------------------------------------------------------------------
    # Per-stage breakdown: the evidence for tiered-model routing
    # ------------------------------------------------------------------

    def per_stage(
        self,
        results: Sequence[NodeResult],
        nodes: Iterable[NodeSpec] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Break latency, cost and tokens down by `StageKind`.

        This is the table that settles the routing argument. Claiming "we send
        the cheap stages to Haiku" is an assertion; showing that REVIEW burned
        eighty percent of the spend while DOCUMENT burned two is the evidence
        for it, and the same table is what catches a stage that was routed to
        the fast tier and quietly started failing.

        Keys are `StageKind` values in SDLC order, with `unknown` last, so the
        rendered table reads top to bottom the way the pipeline ran.
        """
        index = {n.id: n.kind for n in nodes} if nodes is not None else self._stage_by_node
        buckets: dict[str, dict[str, Any]] = {}

        for result in _final_results(results):
            stage = index.get(result.node_id) or _infer_stage(result.node_id)
            key = stage.value if stage is not None else UNKNOWN_STAGE
            bucket = buckets.setdefault(
                key,
                {
                    "nodes": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "retries": 0,
                    "latency_seconds": 0.0,
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            bucket["nodes"] += 1
            bucket["succeeded"] += int(result.state is TaskState.COMPLETED)
            bucket["failed"] += int(result.state in _FAILED_STATES)
            bucket["retries"] += max(0, result.attempts - 1)
            bucket["latency_seconds"] += result.duration
            bucket["cost_usd"] += result.cost_usd
            bucket["input_tokens"] += result.input_tokens
            bucket["output_tokens"] += result.output_tokens

        return {k: buckets[k] for k in sorted(buckets, key=_stage_sort_key)}

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def to_json(metrics: RunMetrics) -> str:
        """Serialize for the run record.

        `success_rate` is a property rather than a field, so `asdict` drops it.
        It is added back explicitly because it is the headline number and a
        consumer should not have to rederive it. `mttr_seconds` serializes to
        JSON `null` when undefined, which preserves the distinction from 0.0
        across the wire.
        """
        payload: dict[str, Any] = asdict(metrics)
        payload["success_rate"] = metrics.success_rate
        return json.dumps(payload, indent=2)

    @staticmethod
    def format_table(metrics: RunMetrics) -> str:
        """Render the metric set for a terminal."""
        mttr = (
            f"{metrics.mttr_seconds:.3f}s"
            if metrics.mttr_seconds is not None
            # Spelled out rather than shown as 0.000s, because the two mean
            # opposite things and a reader skimming a table will not stop to
            # wonder which one a zero was.
            else "n/a (no recovered failures)"
        )
        rows = [
            ("nodes", str(metrics.total_nodes)),
            ("succeeded", str(metrics.succeeded)),
            ("failed", str(metrics.failed)),
            ("success rate", f"{metrics.success_rate * 100:.1f}%"),
            ("retries", str(metrics.retries)),
            ("rollbacks", str(metrics.rollbacks)),
            ("fallbacks", str(metrics.fallbacks)),
            ("approvals requested", str(metrics.approvals_requested)),
            ("plan revisions", str(metrics.plan_revisions)),
            ("e2e latency", f"{metrics.e2e_latency_seconds:.3f}s"),
            ("MTTR", mttr),
            ("cost", f"${metrics.cost_usd:.4f}"),
            ("tokens in", f"{metrics.input_tokens:,}"),
            ("tokens out", f"{metrics.output_tokens:,}"),
        ]
        return _render(f"run metrics: {metrics.run_id}", ("metric", "value"), rows)

    @staticmethod
    def format_stage_table(stages: dict[str, dict[str, Any]]) -> str:
        """Render the per-stage breakdown for a terminal."""
        rows = [
            (
                stage,
                str(data["nodes"]),
                f"{data['succeeded']}/{data['nodes']}",
                f"{data['latency_seconds']:.2f}s",
                f"${data['cost_usd']:.4f}",
                f"{data['input_tokens']:,}",
                f"{data['output_tokens']:,}",
            )
            for stage, data in stages.items()
        ]
        return _render(
            "per-stage breakdown",
            ("stage", "nodes", "ok", "latency", "cost", "tok in", "tok out"),
            rows,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _final_results(results: Sequence[NodeResult]) -> list[NodeResult]:
    """Collapse to one result per node, keeping the last.

    Node ids are unique within a `Plan`, so a repeated id means the node was
    re-run or re-planned. Counting it twice would inflate the denominator of
    the success rate and double the cost total.
    """
    latest: dict[str, NodeResult] = {}
    for result in results:
        latest[result.node_id] = result
    return list(latest.values())


def _ordered(events: Sequence[AuditEvent]) -> list[AuditEvent]:
    """Sort into append order.

    `seq` is authoritative because it is assigned on write; `at` breaks ties
    when the orchestrator left `seq` at its default of 0.
    """
    return sorted(events, key=lambda e: (e.seq, e.at))


def _count(events: Sequence[AuditEvent], event_type: AuditEventType) -> int:
    return sum(1 for e in events if e.event_type is event_type)


def _read_state(payload: dict[str, Any]) -> TaskState | None:
    """Pull a `TaskState` out of an event payload, whatever shape it took."""
    for key in _STATE_KEYS:
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, TaskState):
            return raw
        if isinstance(raw, str):
            try:
                return TaskState(raw.lower())
            except ValueError:
                try:
                    return TaskState[raw.upper()]
                except KeyError:
                    return None
    return None


def _is_failure_marker(event: AuditEvent) -> bool:
    if event.event_type is AuditEventType.RETRY:
        return True
    if event.event_type is AuditEventType.NODE_FINISHED:
        return _read_state(event.payload) in _FAILED_STATES
    return False


def _is_success_marker(event: AuditEvent) -> bool:
    return (
        event.event_type is AuditEventType.NODE_FINISHED
        and _read_state(event.payload) is TaskState.COMPLETED
    )


def _infer_stage(node_id: str) -> StageKind | None:
    """Best-effort stage recovery from a node id.

    Used only when the plan's nodes were not supplied. Matching is on whole
    segments of the normalized id, so `test_api` resolves to TEST while
    `latest_check` resolves to nothing at all. Longest stage value first, so
    `release_check_1` does not get claimed by a shorter name.
    """
    norm = _SEPARATORS.sub("_", node_id.lower()).strip("_")
    for stage in sorted(StageKind, key=lambda s: len(s.value), reverse=True):
        value = stage.value
        if (
            norm == value
            or norm.startswith(f"{value}_")
            or norm.endswith(f"_{value}")
            or f"_{value}_" in norm
        ):
            return stage
    return None


def _stage_sort_key(stage: str) -> tuple[int, str]:
    """SDLC declaration order, with the unattributed bucket last."""
    order = [s.value for s in StageKind]
    return (order.index(stage), stage) if stage in order else (len(order), stage)


def _render(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> str:
    """Minimal fixed-width table.

    Stdlib only and deliberately so: metrics are read from CI logs and from
    piped output at least as often as from an interactive terminal, and a
    rendering dependency here would drag a UI concern into the governance
    plane.
    """
    # Rows are padded to the header width before transposing. A bare
    # `zip(*rows)` truncates to the shortest row, so a single short row would
    # silently drop a column from the entire table, which is a bad failure for
    # something whose job is to report numbers accurately.
    width = len(headers)
    grid = [list(headers)] + [list(r)[:width] + [""] * (width - len(list(r)[:width])) for r in rows]

    columns = list(zip(*grid, strict=True))
    widths = [max(len(str(cell)) for cell in column) for column in columns]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells: Sequence[str]) -> str:
        cells = list(cells)[:width]
        cells += [""] * (width - len(cells))
        padded = [f" {str(c):<{w}} " for c, w in zip(cells, widths, strict=True)]
        return "|" + "|".join(padded) + "|"

    out = [title, rule, line(headers), rule]
    out.extend(line(r) for r in rows)
    out.append(rule)
    return "\n".join(out)
