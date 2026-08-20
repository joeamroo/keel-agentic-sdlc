"""The offline HTML run report.

What the live view is for the person watching, this is for the person asking
afterwards what happened and whether to trust it. It renders one run into a
single HTML string: the four reliability metrics plus cost, the plan DAG, a
per-node timeline, every gate decision with its reason, every policy violation,
the approval log, and the full audit trail.

Two constraints drive every decision in this module.

The report is self-contained. No stylesheet link, no CDN, no web font, no
script, no image URL, nothing that touches the network. It gets attached to a
message and opened on a laptop with no connectivity, from a file:// path, and it
has to look exactly as intended. That is also why the mermaid source is printed
rather than drawn: pulling a rendering library off a CDN would make the diagram
the one part of the report that only works online, so the readable fallback
table is the thing that actually renders, and the mermaid block is kept beside
it as source anyone can paste into a renderer that already has the library.

Everything dynamic is escaped with `html.escape`. The content here is model
output, file paths, generated code, error strings and rule messages. A reviewer
opening a report must not be able to be attacked by the code the run generated,
so escaping is applied at every interpolation rather than trusted upstream. The
document also contains no `<script>` element at all, which means there is no
in-page execution context for injected markup to reach even if an escape were
missed.
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Sequence
from pathlib import Path

from ..models import (
    AuditEvent,
    AuditEventType,
    GateDecision,
    NodeResult,
    Plan,
    PolicyViolation,
    RunMetrics,
    Severity,
    TaskState,
)

# Shared with the terminal view on purpose: two views of one run that disagree
# about what "level 2" means, or about how a state is spelled, are worse than
# having only one view.
from .live import STATE_LABEL, plan_levels

__all__ = ["build_report", "write_report"]


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------


def _esc(value: object) -> str:
    """Escape any value for HTML text or attribute context.

    `quote=True` is the default and is kept, because several call sites put
    escaped text into `title="..."` attributes and an unescaped quote there is
    an injection point.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _esc_join(values: Sequence[object], sep: str = ", ", empty: str = "-") -> str:
    return sep.join(_esc(v) for v in values) if values else empty


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def _seconds(value: float) -> str:
    return f"{value:.2f}s"


def _usd(value: float) -> str:
    """Four decimal places: a single node can cost less than a cent."""
    return f"${value:.4f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _per_node(count: int, total: int) -> str:
    return f"{count / total:.2f}" if total else "0.00"


def _count(n: int, singular: str, plural: str | None = None) -> str:
    """Pluralize a counted noun.

    Small thing, but this report is read by people deciding whether to trust the
    system that wrote it, and "1 rollbacks" reads like nobody checked.
    """
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


def _clock(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else "unknown"


def _rel(at: float, origin: float) -> str:
    """Offsets, not wall clock, because a run is read relative to its own start."""
    if not at:
        return "-"
    return f"+{max(0.0, at - origin):.2f}s"


# --------------------------------------------------------------------------
# Small markup builders
# --------------------------------------------------------------------------


def _chip(text: str, css_class: str) -> str:
    """A coloured pill. `text` is escaped here; `css_class` is always a literal."""
    return f'<span class="chip {css_class}">{_esc(text)}</span>'


def _state_chip(state: TaskState | str) -> str:
    if isinstance(state, TaskState):
        label = STATE_LABEL.get(state, state.value)
        return _chip(label, f"s-{state.value.replace('_', '-')}")
    return _chip(str(state), "s-unknown")


def _severity_chip(value: object) -> str:
    """Severity pill that survives a payload carrying an unknown severity.

    An unrecognized value is shown verbatim with neutral styling rather than
    coerced into a known level, because quietly relabelling a critical finding
    as informational is the worst thing this report could do.
    """
    try:
        severity = value if isinstance(value, Severity) else Severity(str(value))
    except ValueError:
        return _chip(str(value), "sev-unknown")
    return _chip(severity.value, f"sev-{severity.value}")


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    """Render a table.

    Cells are inserted verbatim so callers can pass chips and other markup, so
    every caller is responsible for having escaped its own text with `_esc`.
    Header labels are escaped here since they are short literals either way.
    """
    if not rows:
        return f'<p class="empty">{_esc(empty)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _card(label: str, value: str, sub: str) -> str:
    return (
        '<div class="card">'
        f'<div class="card-label">{_esc(label)}</div>'
        f'<div class="card-value">{_esc(value)}</div>'
        f'<div class="card-sub">{_esc(sub)}</div>'
        "</div>"
    )


# --------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------

# One inline stylesheet, no external font and no url() of any kind. The type
# stack is whatever the reading machine already has.
_STYLE = """
*,*::before,*::after{box-sizing:border-box}
:root{
  color-scheme:light dark;
  --bg:#fbfbfd;--panel:#ffffff;--ink:#141a21;--muted:#5b6672;--line:#e2e6ec;--grid:#f4f6f9;
  --ok:#136c37;--bad:#a81b0f;--warn:#8a5300;--info:#1b4fc4;--accent:#4b34c9;--bar:#4b34c9;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0d1117;--panel:#151b23;--ink:#e6ecf3;--muted:#9aa7b4;--line:#242e3a;--grid:#1a212a;
    --ok:#4cd07d;--bad:#ff7b72;--warn:#e3b341;--info:#7aa7ff;--accent:#a99bff;--bar:#7c6bff;
  }
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;
}
main{max-width:1180px;margin:0 auto;padding:40px 24px 80px}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
.eyebrow{letter-spacing:.14em;text-transform:uppercase;font-size:12px;color:var(--muted);font-weight:700}
h1{font-size:30px;line-height:1.2;margin:6px 0 8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h2{font-size:19px;margin:0 0 4px}
.meta{color:var(--muted);font-size:14px;margin:0}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:22px 0}
.lede{color:var(--muted);font-size:14px;margin:0 0 14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:14px}
.card{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--grid)}
.card-label{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700}
.card-value{font-size:27px;font-weight:700;margin:6px 0 2px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.card-sub{font-size:13px;color:var(--muted)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
tbody tr:nth-child(odd){background:var(--grid)}
td.num{text-align:right;white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
td.id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:600;white-space:nowrap}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;
  border:1px solid currentColor;white-space:nowrap}
.s-completed{color:var(--ok)}
.s-working{color:var(--warn)}
.s-submitted,.s-canceled,.s-unknown{color:var(--muted)}
.s-failed,.s-rejected{color:var(--bad)}
.s-input-required,.s-auth-required{color:var(--accent)}
.sev-info{color:var(--info)}
.sev-low{color:var(--muted)}
.sev-medium{color:var(--warn)}
.sev-high,.sev-critical{color:var(--bad)}
.sev-unknown{color:var(--muted)}
.allow{color:var(--ok)}
.deny{color:var(--bad)}
.bar{position:relative;height:10px;min-width:150px;background:var(--grid);
  border:1px solid var(--line);border-radius:5px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--bar);border-radius:5px}
pre.mermaid{margin:0;padding:14px;background:var(--grid);border:1px solid var(--line);
  border-radius:10px;overflow-x:auto;font-size:13px;line-height:1.45}
.err{color:var(--bad);font-size:13px;white-space:pre-wrap;word-break:break-word;max-width:44ch}
.wrap{white-space:pre-wrap;word-break:break-word}
.empty{color:var(--muted);font-size:14px;font-style:italic;margin:4px 0 0}
details{border:1px solid var(--line);border-radius:10px;padding:10px 14px;background:var(--grid)}
summary{cursor:pointer;font-weight:700}
footer{color:var(--muted);font-size:13px;margin-top:26px;text-align:center}
@media print{
  body{background:#fff}
  section{break-inside:avoid;border-color:#ccc}
}
"""


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _summary_section(metrics: RunMetrics) -> str:
    """The four named reliability metrics, plus cost.

    Retries and rollbacks are shown as a per-node frequency as well as a raw
    count, because the raw count means nothing without knowing whether the run
    had five nodes or fifty.
    """
    mttr = _seconds(metrics.mttr_seconds) if metrics.mttr_seconds is not None else "n/a"
    mttr_sub = (
        "mean time from failure to recovery"
        if metrics.mttr_seconds is not None
        else "nothing failed and recovered"
    )
    cards = [
        _card(
            "success rate",
            _pct(metrics.success_rate),
            f"{metrics.succeeded} of {metrics.total_nodes} nodes completed, {metrics.failed} failed",
        ),
        _card(
            "retry frequency",
            f"{_per_node(metrics.retries, metrics.total_nodes)} per node",
            f"{_count(metrics.retries, 'retry', 'retries')}, "
            f"{_count(metrics.fallbacks, 'tier fallback')}",
        ),
        _card(
            "rollback frequency",
            f"{_per_node(metrics.rollbacks, metrics.total_nodes)} per node",
            _count(metrics.rollbacks, "workspace rollback"),
        ),
        _card("mttr", mttr, mttr_sub),
        _card(
            "end to end latency",
            _seconds(metrics.e2e_latency_seconds),
            "first node start to last node finish",
        ),
        _card(
            "cost",
            _usd(metrics.cost_usd),
            f"{metrics.input_tokens:,} in / {metrics.output_tokens:,} out tokens",
        ),
    ]
    # Governance counters, kept out of the cards on purpose. They describe how
    # the run was supervised rather than how well it went, and giving them the
    # same visual weight as the success rate would misrepresent both.
    extra = " · ".join(
        [
            _count(metrics.approvals_requested, "approval requested", "approvals requested"),
            _count(metrics.plan_revisions, "plan revision"),
            _count(metrics.total_nodes, "node in the plan", "nodes in the plan"),
        ]
    )
    return (
        "<section>"
        "<h2>Run summary</h2>"
        '<p class="lede">Reliability and cost for this run.</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<p class="lede" style="margin:16px 0 0">{_esc(extra)}</p>'
        "</section>"
    )


def _dag_section(plan: Plan, mermaid: str | None) -> str:
    """The plan graph, as mermaid source and as a table anyone can read offline."""
    levels = plan_levels(plan)
    level_of = {
        node.id: depth for depth, level in enumerate(levels, start=1) for node in level
    }
    rows: list[list[str]] = []
    for depth, level in enumerate(levels, start=1):
        for node in level:
            rows.append(
                [
                    f'<span class="mono">{_esc(depth)}</span>',
                    f'<span class="id">{_esc(node.id)}</span>',
                    _esc(node.kind.value),
                    _esc(node.model_tier.value),
                    _chip(node.impact.value, f"sev-{_impact_class(node.impact.value)}"),
                    _esc_join([f"{d} (L{level_of.get(d, '?')})" for d in node.depends_on]),
                    _esc_join(node.produces),
                    _esc(node.description),
                ]
            )
    table = _table(
        ["level", "node", "stage", "tier", "impact", "depends on", "produces", "description"],
        rows,
        "the plan has no nodes",
    )

    if mermaid:
        diagram = f'<pre class="mermaid">{_esc(mermaid)}</pre>'
        note = (
            "The diagram source is printed rather than drawn. This report loads no "
            "scripts and makes no network requests, so it renders identically offline; "
            "paste the block below into any mermaid renderer for the picture, or read "
            "the table, which carries the same graph."
        )
    else:
        diagram = '<p class="empty">no mermaid source was supplied for this run</p>'
        note = "No diagram source was supplied. The table below carries the full graph."

    return (
        "<section>"
        "<h2>Plan DAG</h2>"
        f'<p class="lede">{_esc(note)}</p>'
        f"{diagram}"
        f'<p class="lede" style="margin-top:16px">Nodes grouped by topological level. '
        f"Every node in a level may run in parallel, because its dependencies were "
        f'satisfied by an earlier level.</p>'
        f"{table}"
        "</section>"
    )


def _impact_class(value: str) -> str:
    """Reuse the severity palette for impact, since both mean escalating risk."""
    return {"low": "low", "medium": "medium", "high": "high"}.get(value, "unknown")


def _timeline_section(results: Sequence[NodeResult], origin: float, span: float) -> str:
    """Per-node timeline with a pure CSS bar, so overlap is visible at a glance."""
    rows: list[list[str]] = []
    for result in sorted(results, key=lambda r: (r.started_at or 0.0, r.node_id)):
        rows.append(
            [
                f'<span class="id">{_esc(result.node_id)}</span>',
                _state_chip(result.state),
                f'<span class="mono">{_esc(result.attempts)}</span>',
                _esc(_rel(result.started_at, origin)),
                _esc(_seconds(result.duration)),
                _bar(result, origin, span),
                _esc(f"{result.input_tokens:,} / {result.output_tokens:,}"),
                _esc(_usd(result.cost_usd)),
                _esc("yes" if result.rolled_back else "no"),
                _esc_join([f"{a.name} ({a.sha})" for a in result.artifacts]),
                f'<div class="err">{_esc(result.error)}</div>' if result.error else "-",
            ]
        )
    table = _table(
        [
            "node",
            "state",
            "attempts",
            "start",
            "duration",
            "span",
            "tokens in / out",
            "cost",
            "rolled back",
            "artifacts",
            "error",
        ],
        rows,
        "no nodes executed",
    )
    return (
        "<section>"
        "<h2>Node timeline</h2>"
        '<p class="lede">Offsets are relative to the first node start. Bars that overlap '
        "ran at the same time.</p>"
        f"{table}"
        "</section>"
    )


def _bar(result: NodeResult, origin: float, span: float) -> str:
    """One timeline bar.

    The inline style carries only numbers this function computed, never anything
    derived from run content, so there is nothing here to escape.
    """
    if not result.started_at or span <= 0:
        return '<span class="empty">not timed</span>'
    left = max(0.0, min(100.0, (result.started_at - origin) / span * 100.0))
    width = max(0.8, min(100.0 - left, result.duration / span * 100.0))
    return (
        f'<div class="bar" title="{_esc(_seconds(result.duration))}">'
        f'<span style="margin-left:{left:.3f}%;width:{width:.3f}%"></span></div>'
    )


def _collect_gates(
    results: Sequence[NodeResult], events: Sequence[AuditEvent]
) -> list[GateDecision]:
    """Every gate decision in the run, typed records first.

    A denied entry gate can mean the node never produced a `NodeResult` at all,
    so the audit trail is also mined for decisions the results do not carry.
    Recovering them matters more than the small duplication risk: a report that
    silently omits the gate that stopped the run answers the wrong question.
    """
    collected: list[GateDecision] = []
    seen: set[tuple[str, str, bool, str]] = set()
    for result in results:
        for decision in result.gate_decisions:
            seen.add((decision.gate, decision.node_id, decision.allowed, decision.reason))
            collected.append(decision)
    for event in events:
        if event.event_type is not AuditEventType.GATE_DECISION:
            continue
        payload = event.payload or {}
        decision = GateDecision(
            gate=str(payload.get("gate", "")),
            node_id=str(payload.get("node_id") or event.node_id or ""),
            allowed=bool(payload.get("allowed", False)),
            reason=str(payload.get("reason", "")),
        )
        key = (decision.gate, decision.node_id, decision.allowed, decision.reason)
        if key in seen:
            continue
        seen.add(key)
        collected.append(decision)
    return collected


def _gates_section(gates: Sequence[GateDecision]) -> str:
    rows = [
        [
            _esc(decision.gate),
            f'<span class="id">{_esc(decision.node_id)}</span>',
            (
                '<strong class="allow">allow</strong>'
                if decision.allowed
                else '<strong class="deny">deny</strong>'
            ),
            _esc(decision.reason),
            f'<span class="mono">{_esc(len(decision.violations))}</span>',
        ]
        for decision in gates
    ]
    return (
        "<section>"
        "<h2>Gate decisions</h2>"
        '<p class="lede">Every entry and exit gate, with the reason it gave. A gate '
        "never records a bare boolean.</p>"
        f'{_table(["gate", "node", "decision", "reason", "violations"], rows, "no gates were evaluated")}'
        "</section>"
    )


def _collect_violations(
    gates: Sequence[GateDecision], events: Sequence[AuditEvent]
) -> list[tuple[str, PolicyViolation | None, dict[str, object]]]:
    """Violations from gate decisions, plus any recorded only as audit events."""
    found: list[tuple[str, PolicyViolation | None, dict[str, object]]] = []
    seen: set[tuple[str, str, str]] = set()
    for decision in gates:
        for violation in decision.violations:
            seen.add((decision.node_id, violation.rule_id, violation.message))
            found.append((decision.node_id, violation, {}))
    for event in events:
        if event.event_type is not AuditEventType.POLICY_VIOLATION:
            continue
        payload = dict(event.payload or {})
        node_id = str(payload.get("node_id") or event.node_id or "")
        key = (node_id, str(payload.get("rule_id", "")), str(payload.get("message", "")))
        if key in seen:
            continue
        seen.add(key)
        found.append((node_id, None, payload))
    return found


def _blocks_text(payload: dict[str, object]) -> str:
    """Whether a violation known only from an audit event blocks a gate.

    Taken from the payload when it says, and otherwise derived from the
    severity, because `Severity.blocks` is the contract's own answer. Printing a
    dash here would make the reader go look up a rule the system already knows.
    """
    if "blocks" in payload:
        return "yes" if payload["blocks"] else "no"
    try:
        return "yes" if Severity(str(payload.get("severity", ""))).blocks else "no"
    except ValueError:
        return "-"


def _violations_section(
    violations: Sequence[tuple[str, PolicyViolation | None, dict[str, object]]],
) -> str:
    rows: list[list[str]] = []
    for node_id, violation, payload in violations:
        if violation is not None:
            rows.append(
                [
                    _severity_chip(violation.severity),
                    _esc(violation.rule_id),
                    f'<span class="id">{_esc(node_id)}</span>',
                    f'<span class="wrap">{_esc(violation.message)}</span>',
                    _esc(violation.location or "-"),
                    _esc("yes" if violation.blocks else "no"),
                ]
            )
        else:
            rows.append(
                [
                    _severity_chip(payload.get("severity", "")),
                    _esc(payload.get("rule_id", "")),
                    f'<span class="id">{_esc(node_id)}</span>',
                    f'<span class="wrap">{_esc(payload.get("message", ""))}</span>',
                    _esc(payload.get("location") or "-"),
                    _esc(_blocks_text(payload)),
                ]
            )
    return (
        "<section>"
        "<h2>Policy violations</h2>"
        '<p class="lede">Severity is the colour. High and critical are the two that '
        "block a gate.</p>"
        f'{_table(["severity", "rule", "node", "message", "location", "blocking"], rows, "no policy violations were raised")}'
        "</section>"
    )


def _approvals_section(events: Sequence[AuditEvent], origin: float) -> str:
    """The human-in-the-loop log, reconstructed from the audit trail.

    Approvals are read from events rather than taken as an argument because the
    audit log is the record of record for them. A request and its decision are
    separate events, so a request with no matching decision stays visible as a
    request, which is exactly the state an unattended run parks in.
    """
    kinds = {
        AuditEventType.APPROVAL_REQUESTED: "requested",
        AuditEventType.APPROVAL_DECIDED: "decided",
    }
    rows: list[list[str]] = []
    for event in _ordered(events):
        label = kinds.get(event.event_type)
        if label is None:
            continue
        payload = event.payload or {}
        if event.event_type is AuditEventType.APPROVAL_DECIDED:
            approved = payload.get("approved")
            outcome = (
                '<strong class="allow">approved</strong>'
                if approved
                else '<strong class="deny">rejected</strong>'
            )
            detail = str(payload.get("note") or payload.get("reason") or "")
        else:
            outcome = _chip(str(payload.get("impact", "impact unknown")), "sev-medium")
            detail = str(payload.get("reason") or payload.get("details") or "")
        rows.append(
            [
                _esc(_rel(event.at, origin)),
                _esc(label),
                f'<span class="id">{_esc(payload.get("node_id") or event.node_id or "-")}</span>',
                f'<span class="mono">{_esc(payload.get("request_id", "-"))}</span>',
                outcome,
                f'<span class="wrap">{_esc(detail)}</span>',
                _esc(payload.get("decided_by", "-")),
            ]
        )
    return (
        "<section>"
        "<h2>Approval log</h2>"
        '<p class="lede">Every point where the run stopped and asked a human.</p>'
        f'{_table(["time", "event", "node", "request", "outcome", "detail", "decided by"], rows, "no approvals were requested")}'
        "</section>"
    )


def _audit_section(events: Sequence[AuditEvent], origin: float) -> str:
    """The full trail, collapsed by default because it is long by design."""
    rows: list[list[str]] = []
    for event in _ordered(events):
        rows.append(
            [
                f'<span class="mono">{_esc(event.seq)}</span>',
                _esc(_rel(event.at, origin)),
                _esc(event.event_type.value),
                f'<span class="id">{_esc(event.node_id or "-")}</span>',
                f'<code class="wrap">{_esc(_payload_json(event.payload))}</code>',
            ]
        )
    count = len(rows)
    return (
        "<section>"
        "<h2>Audit trail</h2>"
        '<p class="lede">Append only, in sequence order. The model call events double '
        "as replay cassettes, so this is also what makes the run reproducible.</p>"
        "<details><summary>"
        f"{_esc(_count(count, 'event'))}"
        "</summary>"
        f'<div style="margin-top:12px">'
        f'{_table(["seq", "time", "event", "node", "payload"], rows, "no audit events were recorded")}'
        "</div></details>"
        "</section>"
    )


def _payload_json(payload: object) -> str:
    """Serialize a payload for display, never failing on an odd value."""
    try:
        return json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(payload)


def _ordered(events: Sequence[AuditEvent]) -> list[AuditEvent]:
    """Sequence order, with the timestamp as the tiebreak for unsequenced events."""
    return sorted(events, key=lambda e: (e.seq, e.at))


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def build_report(
    run_id: str,
    plan: Plan,
    results: Sequence[NodeResult],
    metrics: RunMetrics,
    events: Sequence[AuditEvent],
    mermaid: str | None = None,
    *,
    generated_at: float | None = None,
) -> str:
    """Render one run as a complete, self-contained HTML document.

    Returns the whole document, not a fragment, because the artifact this
    produces is a file someone opens, not something to be embedded. Nothing in
    the result references the network.
    """
    generated = generated_at if generated_at is not None else time.time()
    origin, span = _window(results, events)

    gates = _collect_gates(results, events)
    violations = _collect_violations(gates, events)
    levels = plan_levels(plan)

    title = f"keel run report {run_id}"
    header_meta = " · ".join(
        [
            f"plan v{plan.version}",
            f"{len(plan.nodes)} nodes",
            f"{len(levels)} levels",
            f"started {_clock(origin)}",
            f"generated {_clock(generated)}",
        ]
    )
    rationale = (
        f'<p class="lede">{_esc(plan.rationale)}</p>' if plan.rationale else ""
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n<main>\n"
        '<header><div class="eyebrow">keel run report</div>'
        f"<h1>{_esc(run_id)}</h1>"
        f'<p class="meta">{_esc(header_meta)}</p>'
        f"{rationale}</header>\n"
        f"{_summary_section(metrics)}\n"
        f"{_dag_section(plan, mermaid)}\n"
        f"{_timeline_section(results, origin, span)}\n"
        f"{_gates_section(gates)}\n"
        f"{_violations_section(violations)}\n"
        f"{_approvals_section(events, origin)}\n"
        f"{_audit_section(events, origin)}\n"
        "<footer>Generated by keel. This file is self contained: no scripts, no "
        "external styles, no network requests.</footer>\n"
        "</main>\n</body>\n</html>\n"
    )


def _window(
    results: Sequence[NodeResult], events: Sequence[AuditEvent]
) -> tuple[float, float]:
    """The run's time window as (origin, span).

    Derived from the data rather than taken as an argument so a partial run, or
    one assembled from a replayed cassette, still lays out correctly. A span of
    zero is returned when nothing is timed, and callers treat that as "draw no
    bars" rather than dividing by it.
    """
    stamps = [r.started_at for r in results if r.started_at] + [
        e.at for e in events if e.at
    ]
    ends = [r.ended_at for r in results if r.ended_at] + [e.at for e in events if e.at]
    if not stamps:
        return 0.0, 0.0
    origin = min(stamps)
    span = max(ends, default=origin) - origin
    return origin, max(0.0, span)


def write_report(
    path: str | Path,
    run_id: str,
    plan: Plan,
    results: Sequence[NodeResult],
    metrics: RunMetrics,
    events: Sequence[AuditEvent],
    mermaid: str | None = None,
    *,
    generated_at: float | None = None,
) -> Path:
    """Build the report and write it to `path`, returning the path written.

    Parent directories are created because the caller's run directory may not
    exist yet, and the encoding is pinned to UTF-8 so the report is byte
    identical on a machine with a different locale.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        build_report(
            run_id,
            plan,
            results,
            metrics,
            events,
            mermaid,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    return target
