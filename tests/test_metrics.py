"""Tests for the reliability metric computation.

The interesting tests here are the MTTR ones. MTTR is the metric most easily
faked, so these pin down the definition: it is undefined with no failures, it
measures from the first failure of a run of failures, and it ignores an outage
that never ended rather than averaging it in as a recovery.
"""

from __future__ import annotations

import json

import pytest

from keel.governance.metrics import UNKNOWN_STAGE, MetricsCollector
from keel.models import (
    AuditEvent,
    AuditEventType,
    ModelTier,
    NodeResult,
    NodeSpec,
    StageKind,
    TaskState,
)

RUN = "run-test-0001"


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


class EventLog:
    """Builds an audit log with monotonically increasing sequence numbers."""

    def __init__(self, run_id: str = RUN) -> None:
        self.run_id = run_id
        self.events: list[AuditEvent] = []

    def add(
        self,
        event_type: AuditEventType,
        at: float,
        node_id: str | None = None,
        **payload: object,
    ) -> EventLog:
        self.events.append(
            AuditEvent(
                run_id=self.run_id,
                event_type=event_type,
                payload=dict(payload),
                node_id=node_id,
                at=at,
                seq=len(self.events),
            )
        )
        return self

    def started(self, at: float) -> EventLog:
        return self.add(AuditEventType.RUN_STARTED, at)

    def finished(self, at: float) -> EventLog:
        return self.add(AuditEventType.RUN_FINISHED, at)

    def node_failed(self, node_id: str, at: float) -> EventLog:
        return self.add(
            AuditEventType.NODE_FINISHED, at, node_id, state=TaskState.FAILED.value
        )

    def node_ok(self, node_id: str, at: float) -> EventLog:
        return self.add(
            AuditEventType.NODE_FINISHED, at, node_id, state=TaskState.COMPLETED.value
        )

    def retry(self, node_id: str, at: float) -> EventLog:
        return self.add(AuditEventType.RETRY, at, node_id)


def result(
    node_id: str,
    state: TaskState = TaskState.COMPLETED,
    **kwargs: object,
) -> NodeResult:
    return NodeResult(node_id=node_id, state=state, **kwargs)  # type: ignore[arg-type]


def node(node_id: str, kind: StageKind) -> NodeSpec:
    return NodeSpec(id=node_id, kind=kind, description=f"{kind.value} work")


# ----------------------------------------------------------------------
# Success rate and counts
# ----------------------------------------------------------------------


def test_success_rate_and_terminal_counts() -> None:
    results = [
        result("a"),
        result("b"),
        result("c"),
        result("d", TaskState.FAILED),
    ]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.total_nodes == 4
    assert metrics.succeeded == 3
    assert metrics.failed == 1
    assert metrics.success_rate == pytest.approx(0.75)


def test_canceled_node_lowers_the_rate_without_counting_as_a_failure() -> None:
    """A canceled node did not deliver, so it must not be quietly excluded.

    It is also not evidence that the system broke, so it does not inflate the
    failure count either.
    """
    results = [result("a"), result("b", TaskState.CANCELED)]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.total_nodes == 2
    assert metrics.succeeded == 1
    assert metrics.failed == 0
    assert metrics.success_rate == pytest.approx(0.5)


def test_rejected_counts_as_failed() -> None:
    metrics = MetricsCollector().from_results(RUN, [result("a", TaskState.REJECTED)], [])
    assert metrics.failed == 1
    assert metrics.success_rate == pytest.approx(0.0)


def test_empty_run_is_all_zeros_and_undefined_mttr() -> None:
    metrics = MetricsCollector().from_results(RUN, [], [])

    assert metrics.total_nodes == 0
    assert metrics.success_rate == 0.0
    assert metrics.e2e_latency_seconds == 0.0
    assert metrics.mttr_seconds is None


def test_repeated_node_id_collapses_to_the_last_result() -> None:
    """A re-planned node must count once, or it doubles the cost total."""
    results = [
        result("a", TaskState.FAILED, cost_usd=0.10),
        result("a", TaskState.COMPLETED, cost_usd=0.20),
    ]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.total_nodes == 1
    assert metrics.succeeded == 1
    assert metrics.failed == 0
    assert metrics.cost_usd == pytest.approx(0.20)


# ----------------------------------------------------------------------
# MTTR
# ----------------------------------------------------------------------


def test_mttr_is_none_when_nothing_ever_failed() -> None:
    """No failures means undefined, not zero. This is the whole point."""
    log = EventLog().started(0.0).node_ok("a", 5.0).node_ok("b", 9.0).finished(10.0)
    metrics = MetricsCollector().from_results(RUN, [result("a"), result("b")], log.events)

    assert metrics.mttr_seconds is None
    assert metrics.failed == 0


def test_mttr_measures_failure_to_subsequent_success() -> None:
    log = EventLog().started(0.0).node_failed("a", 10.0).node_ok("a", 14.0).finished(20.0)
    collector = MetricsCollector()

    assert collector.mttr(log.events) == pytest.approx(4.0)


def test_mttr_averages_across_multiple_recovered_nodes() -> None:
    """Two independent outages, 4s and 10s, so the mean is 7s."""
    log = (
        EventLog()
        .started(0.0)
        .node_failed("a", 10.0)
        .node_ok("a", 14.0)
        .node_failed("b", 20.0)
        .node_ok("b", 30.0)
        .finished(40.0)
    )
    collector = MetricsCollector()
    intervals = collector.recovery_intervals(log.events)

    assert [i.seconds for i in intervals] == pytest.approx([4.0, 10.0])
    assert collector.mttr(log.events) == pytest.approx(7.0)


def test_mttr_measures_from_the_first_failure_not_the_last_attempt() -> None:
    """Fail, retry, fail again, then succeed.

    The node was broken from t=10 to t=40, so MTTR is 30s. Measuring from the
    last failed attempt would report 10s and understate the outage.
    """
    log = (
        EventLog()
        .started(0.0)
        .node_failed("a", 10.0)
        .retry("a", 20.0)
        .node_failed("a", 30.0)
        .node_ok("a", 40.0)
        .finished(50.0)
    )
    collector = MetricsCollector()
    intervals = collector.recovery_intervals(log.events)

    assert len(intervals) == 1
    assert intervals[0].failed_at == pytest.approx(10.0)
    assert collector.mttr(log.events) == pytest.approx(30.0)


def test_a_node_can_recover_twice_and_both_outages_count() -> None:
    log = (
        EventLog()
        .started(0.0)
        .node_failed("a", 10.0)
        .node_ok("a", 12.0)
        .node_failed("a", 20.0)
        .node_ok("a", 26.0)
    )
    collector = MetricsCollector()

    assert [i.seconds for i in collector.recovery_intervals(log.events)] == pytest.approx(
        [2.0, 6.0]
    )
    assert collector.mttr(log.events) == pytest.approx(4.0)


def test_failure_that_never_recovered_is_not_an_interval() -> None:
    """An outage that never ended is not a recovery of length zero.

    Node b recovered in 4s; node a never came back. MTTR reports the one real
    recovery rather than averaging in a fictional one.
    """
    log = (
        EventLog()
        .started(0.0)
        .node_failed("a", 10.0)
        .node_failed("b", 12.0)
        .node_ok("b", 16.0)
        .finished(20.0)
    )
    collector = MetricsCollector()
    intervals = collector.recovery_intervals(log.events)

    assert [i.node_id for i in intervals] == ["b"]
    assert collector.mttr(log.events) == pytest.approx(4.0)


def test_mttr_is_none_when_the_only_failure_never_recovered() -> None:
    log = EventLog().started(0.0).node_failed("a", 10.0).finished(20.0)
    assert MetricsCollector().mttr(log.events) is None


def test_retry_event_alone_marks_the_failure() -> None:
    """Some orchestrators log a failed attempt as RETRY and nothing else."""
    log = EventLog().started(0.0).retry("a", 10.0).node_ok("a", 25.0)
    assert MetricsCollector().mttr(log.events) == pytest.approx(15.0)


def test_success_before_any_failure_starts_no_clock() -> None:
    log = EventLog().node_ok("a", 5.0).node_failed("b", 6.0).node_ok("b", 8.0)
    intervals = MetricsCollector().recovery_intervals(log.events)

    assert [i.node_id for i in intervals] == ["b"]


def test_state_payload_accepts_enum_name_and_value() -> None:
    """The orchestrator may serialize the state in any of three shapes."""
    log = EventLog()
    log.add(AuditEventType.NODE_FINISHED, 10.0, "a", state=TaskState.FAILED)
    log.add(AuditEventType.NODE_FINISHED, 12.0, "a", state="COMPLETED")
    log.add(AuditEventType.NODE_FINISHED, 20.0, "b", task_state="failed")
    log.add(AuditEventType.NODE_FINISHED, 23.0, "b", state="completed")

    assert MetricsCollector().mttr(log.events) == pytest.approx(2.5)


def test_events_from_another_run_never_leak_into_mttr() -> None:
    ours = EventLog(RUN).started(0.0).node_ok("a", 5.0).finished(6.0)
    theirs = EventLog("run-other").node_failed("a", 1.0).node_ok("a", 99.0)
    metrics = MetricsCollector().from_results(RUN, [result("a")], ours.events + theirs.events)

    assert metrics.mttr_seconds is None
    assert metrics.e2e_latency_seconds == pytest.approx(6.0)


# ----------------------------------------------------------------------
# Retry, rollback, fallback frequency
# ----------------------------------------------------------------------


def test_frequencies_read_from_node_results() -> None:
    results = [
        result("a", attempts=3),
        result("b", attempts=1, rolled_back=True),
        result("c", attempts=2, used_fallback=True, rolled_back=True),
    ]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.retries == 3  # 2 from a, 1 from c
    assert metrics.rollbacks == 2
    assert metrics.fallbacks == 1


def test_frequencies_take_the_larger_of_the_two_sources() -> None:
    """A node that retried and then safe-stopped never produced a result.

    The audit log saw those retries, so the audit log wins here. Trusting the
    results alone would report zero retries for a run that visibly struggled.
    """
    log = (
        EventLog()
        .retry("a", 1.0)
        .retry("a", 2.0)
        .add(AuditEventType.ROLLBACK, 3.0, "a")
        .add(AuditEventType.FALLBACK, 4.0, "a")
    )
    metrics = MetricsCollector().from_results(RUN, [], log.events)

    assert metrics.retries == 2
    assert metrics.rollbacks == 1
    assert metrics.fallbacks == 1


def test_results_win_when_the_audit_log_is_thinner() -> None:
    metrics = MetricsCollector().from_results(
        RUN, [result("a", attempts=4)], EventLog().retry("a", 1.0).events
    )
    assert metrics.retries == 3


def test_approvals_and_plan_revisions_come_from_the_audit_log() -> None:
    log = (
        EventLog()
        .add(AuditEventType.APPROVAL_REQUESTED, 1.0, "a")
        .add(AuditEventType.APPROVAL_DECIDED, 2.0, "a")
        .add(AuditEventType.APPROVAL_REQUESTED, 3.0, "b")
        .add(AuditEventType.PLAN_REVISED, 4.0)
    )
    metrics = MetricsCollector().from_results(RUN, [], log.events)

    assert metrics.approvals_requested == 2
    assert metrics.plan_revisions == 1


# ----------------------------------------------------------------------
# Latency, cost, tokens
# ----------------------------------------------------------------------


def test_e2e_latency_spans_run_started_to_run_finished() -> None:
    log = EventLog().started(100.0).node_ok("a", 105.0).finished(112.5)
    metrics = MetricsCollector().from_results(RUN, [result("a")], log.events)

    assert metrics.e2e_latency_seconds == pytest.approx(12.5)


def test_e2e_latency_falls_back_to_the_event_span_when_the_run_never_finished() -> None:
    log = EventLog().started(100.0).node_ok("a", 108.0)
    metrics = MetricsCollector().from_results(RUN, [result("a")], log.events)

    assert metrics.e2e_latency_seconds == pytest.approx(8.0)


def test_e2e_latency_falls_back_to_the_result_span_with_no_events() -> None:
    results = [
        result("a", started_at=10.0, ended_at=15.0),
        result("b", started_at=15.0, ended_at=22.0),
    ]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.e2e_latency_seconds == pytest.approx(12.0)


def test_cost_and_tokens_are_summed_from_results() -> None:
    results = [
        result("a", input_tokens=1000, output_tokens=500, cost_usd=0.0175),
        result("b", input_tokens=200, output_tokens=100, cost_usd=0.0007),
    ]
    metrics = MetricsCollector().from_results(RUN, results, [])

    assert metrics.input_tokens == 1200
    assert metrics.output_tokens == 600
    assert metrics.cost_usd == pytest.approx(0.0182)


# ----------------------------------------------------------------------
# Per-stage breakdown
# ----------------------------------------------------------------------


def test_per_stage_uses_the_plan_nodes_when_available() -> None:
    nodes = [
        node("n1", StageKind.IMPLEMENT),
        node("n2", StageKind.IMPLEMENT),
        node("n3", StageKind.REVIEW),
    ]
    results = [
        result("n1", started_at=0.0, ended_at=4.0, cost_usd=0.10, input_tokens=100),
        result("n2", TaskState.FAILED, started_at=4.0, ended_at=6.0, cost_usd=0.05, attempts=2),
        result("n3", started_at=6.0, ended_at=7.0, cost_usd=0.90, output_tokens=70),
    ]
    stages = MetricsCollector(nodes).per_stage(results)

    assert stages["implement"]["nodes"] == 2
    assert stages["implement"]["succeeded"] == 1
    assert stages["implement"]["failed"] == 1
    assert stages["implement"]["retries"] == 1
    assert stages["implement"]["latency_seconds"] == pytest.approx(6.0)
    assert stages["implement"]["cost_usd"] == pytest.approx(0.15)
    assert stages["implement"]["input_tokens"] == 100
    assert stages["review"]["cost_usd"] == pytest.approx(0.90)
    assert stages["review"]["output_tokens"] == 70


def test_per_stage_keys_follow_sdlc_order() -> None:
    nodes = [
        node("r", StageKind.REVIEW),
        node("a", StageKind.ANALYZE),
        node("t", StageKind.TEST),
    ]
    stages = MetricsCollector(nodes).per_stage([result("r"), result("a"), result("t")])

    assert list(stages) == ["analyze", "test", "review"]


def test_per_stage_infers_the_stage_from_the_node_id() -> None:
    """No plan supplied, so the id has to carry the stage."""
    results = [
        result("implement-api"),
        result("02_test_suite"),
        result("release_check"),
        result("design.schema"),
    ]
    stages = MetricsCollector().per_stage(results)

    assert set(stages) == {"design", "implement", "test", "release_check"}


def test_per_stage_buckets_unresolvable_ids_as_unknown() -> None:
    """Better an honest unknown bucket than a confident wrong attribution."""
    stages = MetricsCollector().per_stage([result("node-7"), result("latest")])

    assert list(stages) == [UNKNOWN_STAGE]
    assert stages[UNKNOWN_STAGE]["nodes"] == 2


def test_per_stage_nodes_argument_overrides_the_constructor_index() -> None:
    collector = MetricsCollector([node("n1", StageKind.IMPLEMENT)])
    stages = collector.per_stage([result("n1")], nodes=[node("n1", StageKind.DOCUMENT)])

    assert list(stages) == ["document"]


def test_per_stage_evidences_the_tiered_routing_decision() -> None:
    """The table has to make a cheap stage visibly cheap, or it proves nothing."""
    nodes = [
        NodeSpec(id="rev", kind=StageKind.REVIEW, description="", model_tier=ModelTier.DEEP),
        NodeSpec(
            id="gate",
            kind=StageKind.RELEASE_CHECK,
            description="",
            model_tier=ModelTier.FAST,
        ),
    ]
    results = [result("rev", cost_usd=1.20), result("gate", cost_usd=0.02)]
    stages = MetricsCollector(nodes).per_stage(results)

    assert stages["review"]["cost_usd"] > stages["release_check"]["cost_usd"] * 10


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def test_to_json_carries_success_rate_and_a_null_mttr() -> None:
    metrics = MetricsCollector().from_results(RUN, [result("a"), result("b")], [])
    payload = json.loads(MetricsCollector.to_json(metrics))

    assert payload["run_id"] == RUN
    assert payload["success_rate"] == pytest.approx(1.0)
    # null, not 0. The distinction has to survive serialization.
    assert payload["mttr_seconds"] is None
    assert "cost_usd" in payload


def test_to_json_keeps_a_real_mttr_as_a_number() -> None:
    log = EventLog().started(0.0).node_failed("a", 1.0).node_ok("a", 4.0).finished(5.0)
    metrics = MetricsCollector().from_results(RUN, [result("a")], log.events)
    payload = json.loads(MetricsCollector.to_json(metrics))

    assert payload["mttr_seconds"] == pytest.approx(3.0)


def test_format_table_spells_out_an_undefined_mttr() -> None:
    metrics = MetricsCollector().from_results(RUN, [result("a")], [])
    table = MetricsCollector.format_table(metrics)

    assert "n/a" in table
    assert "0.000s" not in table.split("MTTR")[1].split("\n")[0]


def test_format_table_shows_the_headline_numbers() -> None:
    results = [result("a", cost_usd=0.5, input_tokens=1500), result("b", TaskState.FAILED)]
    log = EventLog().started(0.0).finished(3.0)
    metrics = MetricsCollector().from_results(RUN, results, log.events)
    table = MetricsCollector.format_table(metrics)

    assert RUN in table
    assert "50.0%" in table
    assert "3.000s" in table
    assert "$0.5000" in table
    assert "1,500" in table


def test_format_stage_table_lists_every_stage() -> None:
    nodes = [node("a", StageKind.ANALYZE), node("i", StageKind.IMPLEMENT)]
    collector = MetricsCollector(nodes)
    table = collector.format_stage_table(collector.per_stage([result("a"), result("i")]))

    assert "analyze" in table
    assert "implement" in table
    assert table.count("\n") >= 5


def test_a_short_row_does_not_silently_drop_a_table_column() -> None:
    """Regression: transposing with bare zip truncates to the shortest row.

    A single row with fewer cells than the header would drop a column from the
    whole table with no error, which is a bad failure for output whose only job
    is reporting numbers accurately.
    """
    from keel.governance.metrics import MetricsCollector
    from keel.models import RunMetrics

    metrics = RunMetrics(run_id="short-row", total_nodes=1, succeeded=1)
    rendered = MetricsCollector.format_table(metrics)

    # Every rendered row must have the same number of column separators as the
    # rule line, or a column went missing somewhere.
    lines = [ln for ln in rendered.splitlines() if ln.startswith("|")]
    assert lines, "nothing rendered"
    widths = {ln.count("|") for ln in lines}
    assert len(widths) == 1, f"rows disagree on column count: {sorted(widths)}"
