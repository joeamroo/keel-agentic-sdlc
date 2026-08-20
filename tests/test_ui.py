"""Tests for the two presentation surfaces.

The bar for a display layer is different from the bar for the governance plane.
These tests hold it to three things: it must never take the run down with it, it
must stay readable when nobody is watching a terminal, and the report it writes
must be safe to open. The last one is the sharp edge. A run report renders model
output, generated code, file paths and error strings, so escaping is a security
property here, not a formatting preference.
"""

from __future__ import annotations

import io
import re
import time

import pytest
from rich.console import Console

from keel.models import (
    Artifact,
    ApprovalRequest,
    AuditEvent,
    AuditEventType,
    GateDecision,
    ImpactLevel,
    ModelTier,
    NodeResult,
    NodeSpec,
    Plan,
    PolicyViolation,
    RunMetrics,
    Severity,
    StageKind,
    TaskState,
)
from keel.ui.live import (
    STATE_GLYPH,
    STATE_LABEL,
    STATE_STYLE,
    LiveView,
    plan_levels,
    state_style,
)
from keel.ui.report import build_report, write_report

XSS = "<script>alert(1)</script>"
XSS_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"

RUN_ID = "run-20260819-101500-ab12"
T0 = 1_760_000_000.0


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_plan() -> Plan:
    """A diamond: one root, three siblings that fan out, one join.

    The three siblings are the point. They share a level, so both views have to
    show them as one parallel batch.
    """
    return Plan(
        version=1,
        rationale="fan out the design work, then join on implement",
        nodes=[
            NodeSpec(
                id="analyze",
                kind=StageKind.ANALYZE,
                description="normalize the requirement",
                model_tier=ModelTier.FAST,
            ),
            NodeSpec(
                id="design_api",
                kind=StageKind.DESIGN,
                description="public surface",
                depends_on=["analyze"],
            ),
            NodeSpec(
                id="design_db",
                kind=StageKind.DESIGN,
                description="schema",
                depends_on=["analyze"],
            ),
            NodeSpec(
                id="design_ui",
                kind=StageKind.DESIGN,
                description="screens",
                depends_on=["analyze"],
                model_tier=ModelTier.FAST,
            ),
            NodeSpec(
                id="implement",
                kind=StageKind.IMPLEMENT,
                description="write the code",
                depends_on=["design_api", "design_db", "design_ui"],
                impact=ImpactLevel.HIGH,
                produces=["diff.patch"],
            ),
        ],
    )


def make_results() -> list[NodeResult]:
    return [
        NodeResult(
            node_id="analyze",
            state=TaskState.COMPLETED,
            attempts=1,
            started_at=T0,
            ended_at=T0 + 2.0,
            input_tokens=900,
            output_tokens=300,
            cost_usd=0.0021,
            artifacts=[Artifact(name="problem.json", content="{}", produced_by="analyze")],
            gate_decisions=[
                GateDecision.allow("entry", "analyze", "no dependencies to check"),
                GateDecision.allow("exit", "analyze", "confidence 0.82 above threshold"),
            ],
        ),
        NodeResult(
            node_id="design_api",
            state=TaskState.COMPLETED,
            attempts=2,
            started_at=T0 + 2.1,
            ended_at=T0 + 6.4,
            input_tokens=2100,
            output_tokens=1400,
            cost_usd=0.0455,
        ),
        NodeResult(
            node_id="design_db",
            state=TaskState.COMPLETED,
            attempts=1,
            started_at=T0 + 2.1,
            ended_at=T0 + 5.0,
            cost_usd=0.0312,
        ),
        NodeResult(
            node_id="design_ui",
            state=TaskState.FAILED,
            attempts=2,
            started_at=T0 + 2.1,
            ended_at=T0 + 4.5,
            error="schema validation failed on screen list",
            rolled_back=True,
            used_fallback=True,
            cost_usd=0.0044,
        ),
        NodeResult(
            node_id="implement",
            state=TaskState.INPUT_REQUIRED,
            attempts=1,
            started_at=T0 + 6.5,
            ended_at=T0 + 12.5,
            cost_usd=0.0402,
        ),
    ]


def make_metrics() -> RunMetrics:
    return RunMetrics(
        run_id=RUN_ID,
        total_nodes=5,
        succeeded=4,
        failed=1,
        retries=2,
        rollbacks=1,
        fallbacks=1,
        approvals_requested=1,
        plan_revisions=1,
        e2e_latency_seconds=12.5,
        mttr_seconds=3.25,
        cost_usd=0.1234,
        input_tokens=12345,
        output_tokens=6789,
    )


def make_events() -> list[AuditEvent]:
    return [
        AuditEvent(run_id=RUN_ID, event_type=AuditEventType.RUN_STARTED, seq=1, at=T0),
        AuditEvent(
            run_id=RUN_ID,
            event_type=AuditEventType.GATE_DECISION,
            seq=2,
            at=T0 + 6.4,
            node_id="implement",
            payload={
                "gate": "entry",
                "node_id": "implement",
                "allowed": False,
                "reason": "high impact node needs sign off",
            },
        ),
        AuditEvent(
            run_id=RUN_ID,
            event_type=AuditEventType.APPROVAL_REQUESTED,
            seq=3,
            at=T0 + 6.5,
            node_id="implement",
            payload={
                "node_id": "implement",
                "request_id": "a1b2c3d4",
                "impact": "high",
                "reason": "writes to the public API surface",
            },
        ),
        AuditEvent(
            run_id=RUN_ID,
            event_type=AuditEventType.POLICY_VIOLATION,
            seq=4,
            at=T0 + 6.6,
            node_id="design_ui",
            payload={
                "rule_id": "no-secrets",
                "severity": "critical",
                "message": "possible credential in generated config",
                "location": "config/app.yml:12",
            },
        ),
        AuditEvent(
            run_id=RUN_ID,
            event_type=AuditEventType.RUN_FINISHED,
            seq=5,
            at=T0 + 12.5,
            payload={"outcome": "stopped for approval"},
        ),
    ]


def build_default_report(mermaid: str | None = "graph TD\n    analyze --> design_api") -> str:
    return build_report(
        RUN_ID,
        make_plan(),
        make_results(),
        make_metrics(),
        make_events(),
        mermaid,
        generated_at=T0 + 13.0,
    )


def assert_self_contained(doc: str) -> None:
    """No network, no execution context. Asserted the same way everywhere."""
    lowered = doc.lower()
    assert re.search(r'(?:src|href)\s*=\s*["\'][^"\']*://', doc) is None
    assert "<script" not in lowered
    assert "<iframe" not in lowered
    assert "<link" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered  # rules out a remote font or background image
    assert "srcset" not in lowered


# --------------------------------------------------------------------------
# State presentation
# --------------------------------------------------------------------------


def test_state_maps_cover_every_task_state() -> None:
    """A new state in the frozen contract must fail here, not render blank."""
    for state in TaskState:
        assert state in STATE_STYLE, f"no style for {state}"
        assert state in STATE_GLYPH, f"no glyph for {state}"
        assert state in STATE_LABEL, f"no label for {state}"
        assert state_style(state), f"empty style for {state}"


def test_state_colours_match_the_briefed_semantics() -> None:
    assert state_style(TaskState.SUBMITTED) == "dim"
    assert "yellow" in state_style(TaskState.WORKING)
    assert "green" in state_style(TaskState.COMPLETED)
    assert "red" in state_style(TaskState.FAILED)
    assert "red" in state_style(TaskState.REJECTED)
    assert "magenta" in state_style(TaskState.INPUT_REQUIRED)
    assert "grey" in state_style(TaskState.CANCELED)


def test_glyphs_are_distinct_so_colour_is_not_the_only_channel() -> None:
    glyphs = [STATE_GLYPH[s] for s in TaskState]
    assert len(set(glyphs)) == len(glyphs)


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------


def test_levels_expose_the_parallel_batch() -> None:
    levels = plan_levels(make_plan())
    assert [len(level) for level in levels] == [1, 3, 1]
    assert [n.id for n in levels[1]] == ["design_api", "design_db", "design_ui"]


def test_levels_degrade_instead_of_raising_on_an_unrunnable_plan() -> None:
    """A cycle is fatal to the executor. It must not be fatal to the display."""
    cyclic = Plan(
        nodes=[
            NodeSpec(id="a", kind=StageKind.DESIGN, description="", depends_on=["b"]),
            NodeSpec(id="b", kind=StageKind.DESIGN, description="", depends_on=["a"]),
        ]
    )
    levels = plan_levels(cyclic)
    assert len(levels) == 1
    assert [n.id for n in levels[0]] == ["a", "b"]

    stream = io.StringIO()
    view = LiveView(file=stream, run_id=RUN_ID)
    view.start()
    view.set_plan(cyclic)  # must not raise
    view.stop()
    assert "2 nodes in 1 levels" in stream.getvalue()


# --------------------------------------------------------------------------
# LiveView, non-TTY fallback
# --------------------------------------------------------------------------


def drive(view: LiveView) -> None:
    """One representative run through every hook the executor calls."""
    view.set_plan(make_plan())
    view.start()
    view.on_node_start("analyze")
    view.on_gate(GateDecision.allow("entry", "analyze", "no dependencies to check"))
    view.on_node_end(make_results()[0])
    for node_id in ("design_api", "design_db", "design_ui"):
        view.on_node_start(node_id)
    view.on_retry("design_ui", 2)
    for result in make_results()[1:4]:
        view.on_node_end(result)
    view.on_gate(
        GateDecision.deny(
            "entry",
            "implement",
            "high impact node needs sign off",
            [PolicyViolation("approval-required", Severity.HIGH, "human sign off missing")],
        )
    )
    view.on_approval_request(
        ApprovalRequest(
            node_id="implement",
            reason="writes to the public API surface",
            impact=ImpactLevel.HIGH,
        )
    )
    view.note("parked waiting on a human")
    view.stop()


def test_non_tty_output_is_plain_text_with_no_escape_codes() -> None:
    stream = io.StringIO()
    view = LiveView(file=stream, run_id=RUN_ID)
    assert view.is_plain is True
    drive(view)
    out = stream.getvalue()

    assert "\x1b" not in out, "ANSI escape leaked into a non-terminal stream"
    assert "\r" not in out, "cursor control leaked into a non-terminal stream"
    assert out.endswith("\n")
    assert len(out.splitlines()) > 10


def test_non_tty_output_records_every_hook() -> None:
    stream = io.StringIO()
    drive(LiveView(file=stream, run_id=RUN_ID))
    out = stream.getvalue()

    assert RUN_ID in out
    assert "5 nodes in 3 levels" in out
    assert "analyze (analyze, fast)" in out
    assert "design_ui" in out and "attempt 2" in out
    assert "entry implement DENY: high impact node needs sign off" in out
    assert "worst high" in out
    assert "needs high impact sign off" in out
    assert "parked waiting on a human" in out
    assert "schema validation failed on screen list" in out


def test_non_tty_footer_carries_the_running_totals() -> None:
    stream = io.StringIO()
    drive(LiveView(file=stream, run_id=RUN_ID))
    last = stream.getvalue().strip().splitlines()[-1]

    assert "completed 3/5" in last  # analyze, design_api, design_db
    assert "failed 1" in last
    assert "retries 1" in last
    assert "rollbacks 1" in last
    assert "cost $0.0832" in last  # sum of the four reported results
    assert "elapsed" in last
    # Counters the live footer leaves out but the closing line should carry.
    assert "fallbacks 1" in last
    assert "gate denials 1" in last
    assert "approvals 1" in last


def test_live_view_survives_unknown_node_ids() -> None:
    """A stale id from a superseded plan must be a note, not an exception."""
    stream = io.StringIO()
    view = LiveView(file=stream, run_id=RUN_ID)
    view.set_plan(make_plan())
    view.start()
    view.on_node_start("ghost")
    view.on_retry("ghost", 2)
    view.on_node_end(NodeResult(node_id="ghost", state=TaskState.FAILED, cost_usd=0.5))
    view.on_approval_request(ApprovalRequest(node_id="ghost", reason="x", impact=ImpactLevel.LOW))
    view.stop()
    out = stream.getvalue()

    assert out.count("is not in the current plan, ignoring") == 4
    # The cost of an unknown node still counts. It was really spent.
    assert "cost $0.5000" in out


def test_replanning_keeps_the_state_of_surviving_nodes() -> None:
    stream = io.StringIO()
    view = LiveView(file=stream, run_id=RUN_ID)
    view.set_plan(make_plan())
    view.start()
    view.on_node_end(make_results()[0])

    revised = make_plan()
    revised.version = 2
    revised.nodes = revised.nodes[:2]
    view.set_plan(revised)
    view.stop()
    out = stream.getvalue()

    assert "replan" in out
    assert "v2: 2 nodes in 2 levels, 2 carried over" in out
    # analyze stays completed across the revision, so the board reads 1/2 not 0/2.
    assert "completed 1/2" in out.strip().splitlines()[-1]


def test_hooks_before_start_are_not_lost() -> None:
    stream = io.StringIO()
    view = LiveView(file=stream, run_id=RUN_ID)
    view.set_plan(make_plan())
    view.note("intake finished")
    view.stop()
    assert "intake finished" in stream.getvalue()


# --------------------------------------------------------------------------
# LiveView, terminal path
# --------------------------------------------------------------------------


def test_terminal_path_renders_the_dag_with_levels() -> None:
    """Exercise the real rich render, not just the fallback."""
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=140, color_system="truecolor")
    view = LiveView(console=console, run_id=RUN_ID)
    assert view.is_plain is False
    try:
        drive(view)
    finally:
        if not view.is_plain:  # never leave rich holding the terminal
            view.stop()
    out = stream.getvalue()

    assert "\x1b[" in out, "the terminal path should emit styling"
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert "level 1" in plain and "level 2" in plain and "level 3" in plain
    assert "3 nodes may run in parallel" in plain
    for node_id in ("analyze", "design_api", "design_db", "design_ui", "implement"):
        assert node_id in plain
    assert "ELAPSED" in plain and "TIER" in plain
    assert "completed" in plain and "rollbacks" in plain


def test_context_manager_stops_the_live_even_when_the_run_raises() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=120)
    view = LiveView(console=console, run_id=RUN_ID)
    with pytest.raises(RuntimeError):
        with view:
            view.set_plan(make_plan())
            raise RuntimeError("node exploded")
    assert "finished" in re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())


# --------------------------------------------------------------------------
# Report: content
# --------------------------------------------------------------------------


def test_report_shows_the_named_metrics_with_their_values() -> None:
    doc = build_default_report()

    assert "80.0%" in doc  # success rate, 4 of 5
    assert "0.40 per node" in doc  # retry frequency, 2 over 5
    assert "0.20 per node" in doc  # rollback frequency, 1 over 5
    assert "3.25s" in doc  # mttr
    assert "12.50s" in doc  # end to end latency
    assert "$0.1234" in doc  # cost
    assert "12,345" in doc and "6,789" in doc  # token counts
    for label in ("success rate", "retry frequency", "rollback frequency", "mttr", "cost"):
        assert label in doc

    # Counted nouns agree with their counts. A report that says "1 rollbacks"
    # reads like nothing in it was checked.
    assert "2 retries" in doc
    assert "2 retries, 1 tier fallback<" in doc and "tier fallbacks" not in doc
    assert "1 workspace rollback<" in doc
    assert "1 approval requested · 1 plan revision · 5 nodes in the plan" in doc


def test_report_reports_an_absent_mttr_honestly() -> None:
    metrics = make_metrics()
    metrics.mttr_seconds = None
    doc = build_report(RUN_ID, make_plan(), make_results(), metrics, make_events(), None)
    assert "n/a" in doc
    assert "nothing failed and recovered" in doc


def test_report_carries_the_mermaid_source_and_a_readable_fallback() -> None:
    mermaid = 'graph TD\n    analyze["analyze<br/>analyze"] --> design_api'
    doc = build_default_report(mermaid)

    assert '<pre class="mermaid">' in doc
    assert "graph TD" in doc
    # The source is escaped, because it is generated text like everything else.
    assert 'analyze[&quot;analyze&lt;br/&gt;analyze&quot;]' in doc
    # The fallback table is the thing that actually renders offline.
    for node_id in ("analyze", "design_api", "design_db", "design_ui", "implement"):
        assert node_id in doc
    assert "depends on" in doc
    assert "diff.patch" in doc


def test_report_dag_table_groups_by_topological_level() -> None:
    """The fallback table has to show the parallelism, not just the edges.

    Each dependency is annotated with the level it sits on, so three nodes
    citing the same L1 parent and one node citing three L2 parents is the
    diamond, visible without rendering the diagram.
    """
    doc = build_default_report()
    assert doc.count("(L1)") == 3  # the three design nodes all wait on analyze
    assert doc.count("(L2)") == 3  # implement waits on all three of them
    assert "analyze (L1)" in doc
    assert "design_api (L2)" in doc
    assert "Every node in a level may run in parallel" in doc


def test_report_without_mermaid_still_renders_the_graph() -> None:
    doc = build_default_report(None)
    assert "no mermaid source was supplied" in doc
    assert "design_db" in doc
    assert_self_contained(doc)


def test_report_timeline_covers_every_node() -> None:
    doc = build_default_report()
    assert "Node timeline" in doc
    assert "+0.00s" in doc  # the first node anchors the window
    assert "+2.10s" in doc  # the parallel batch starts together
    assert doc.count("+2.10s") == 3
    assert "input required" in doc  # the terminal state of the last node


def test_report_shows_every_gate_decision_with_its_reason() -> None:
    doc = build_default_report()
    assert "no dependencies to check" in doc
    assert "confidence 0.82 above threshold" in doc
    # This one exists only as an audit event, because the denied node never
    # produced a result. It still has to appear.
    assert "high impact node needs sign off" in doc
    assert ">deny<" in doc
    assert ">allow<" in doc


def test_report_colours_violations_by_severity() -> None:
    doc = build_default_report()
    assert "no-secrets" in doc
    assert "possible credential in generated config" in doc
    assert 'class="chip sev-critical"' in doc
    assert ".sev-high,.sev-critical{color:var(--bad)}" in doc
    # Blocking is derived from the severity when the event did not say, because
    # the contract already defines which severities block a gate.
    assert "config/app.yml:12</td><td>yes</td>" in doc


def test_report_includes_the_approval_log() -> None:
    doc = build_default_report()
    assert "Approval log" in doc
    assert "a1b2c3d4" in doc
    assert "writes to the public API surface" in doc


def test_report_includes_the_full_audit_trail_collapsed() -> None:
    doc = build_default_report()
    assert "<details>" in doc
    assert "5 events" in doc
    for event_type in ("run_started", "gate_decision", "approval_requested", "run_finished"):
        assert event_type in doc
    assert "stopped for approval" in doc


def test_report_is_theme_aware() -> None:
    doc = build_default_report()
    assert "@media (prefers-color-scheme:dark)" in doc
    assert "color-scheme:light dark" in doc


def test_report_renders_every_task_state() -> None:
    """Any state the executor can end a node in must render as a chip."""
    plan = Plan(
        nodes=[
            NodeSpec(id=f"n{i}", kind=StageKind.TEST, description="")
            for i, _ in enumerate(TaskState)
        ]
    )
    results = [
        NodeResult(node_id=f"n{i}", state=state, started_at=T0, ended_at=T0 + 1)
        for i, state in enumerate(TaskState)
    ]
    doc = build_report(RUN_ID, plan, results, make_metrics(), [], None)
    for state in TaskState:
        assert f's-{state.value.replace("_", "-")}"' in doc


def test_report_handles_an_empty_run() -> None:
    doc = build_report("run-empty", Plan(nodes=[]), [], RunMetrics(run_id="run-empty"), [], None)
    assert "0.0%" in doc
    assert "the plan has no nodes" in doc
    assert "no nodes executed" in doc
    assert "no gates were evaluated" in doc
    assert "no policy violations were raised" in doc
    assert "no approvals were requested" in doc
    assert_self_contained(doc)


# --------------------------------------------------------------------------
# Report: safety
# --------------------------------------------------------------------------


def test_report_is_self_contained_and_makes_no_network_requests() -> None:
    doc = build_default_report()
    assert_self_contained(doc)
    assert "://" not in doc
    assert doc.startswith("<!doctype html>")
    assert doc.rstrip().endswith("</html>")
    assert doc.count("<style>") == 1


def test_report_escapes_a_script_payload_in_generated_content() -> None:
    """The report renders model output. It must not execute it.

    The payload is planted everywhere untrusted text reaches the page: an
    artifact name, a policy violation message, a node description, an error
    string and an audit payload.
    """
    plan = make_plan()
    plan.nodes[0].description = XSS

    results = make_results()
    results[0].artifacts = [Artifact(name=XSS, content="x", produced_by="analyze")]
    results[0].gate_decisions = [
        GateDecision.deny(
            "exit",
            "analyze",
            f"rule tripped {XSS}",
            [PolicyViolation("xss-rule", Severity.CRITICAL, XSS, location=XSS)],
        )
    ]
    results[3].error = XSS

    events = make_events()
    events[0].payload = {"note": XSS}

    doc = build_report(RUN_ID, plan, results, make_metrics(), events, f"graph TD\n {XSS}")

    assert XSS not in doc, "an unescaped script tag reached the report"
    assert "<script" not in doc.lower()
    assert doc.count(XSS_ESCAPED) >= 6  # every planted copy survived as inert text
    assert "alert(1)" in doc  # escaped, not silently dropped
    assert_self_contained(doc)


def test_report_escapes_quotes_and_attribute_breakers() -> None:
    payload = '" onmouseover="alert(2)'
    results = make_results()
    results[1].error = payload
    results[1].artifacts = [Artifact(name=payload, content="", produced_by="design_api")]
    doc = build_report(RUN_ID, make_plan(), results, make_metrics(), make_events(), None)

    assert 'onmouseover="alert(2)"' not in doc
    assert "&quot; onmouseover=&quot;alert(2)" in doc


def test_report_survives_an_unknown_severity_without_downgrading_it() -> None:
    events = make_events()
    events[3].payload["severity"] = "catastrophic"
    doc = build_report(RUN_ID, make_plan(), make_results(), make_metrics(), events, None)
    assert "catastrophic" in doc
    assert 'class="chip sev-unknown"' in doc


# --------------------------------------------------------------------------
# Report: writing
# --------------------------------------------------------------------------


def test_write_report_creates_parents_and_writes_utf8(tmp_path) -> None:
    target = tmp_path / "runs" / RUN_ID / "report.html"
    written = write_report(
        target,
        RUN_ID,
        make_plan(),
        make_results(),
        make_metrics(),
        make_events(),
        "graph TD",
        generated_at=T0,
    )

    assert written == target
    doc = target.read_text(encoding="utf-8")
    assert doc.startswith("<!doctype html>")
    assert RUN_ID in doc
    assert_self_contained(doc)


def test_report_generation_is_deterministic_for_a_fixed_timestamp() -> None:
    """Two reports over the same run must be byte identical, so they can be diffed."""
    first = build_default_report()
    second = build_default_report()
    assert first == second
    assert time.strftime("%Y", time.localtime(T0)) in first
