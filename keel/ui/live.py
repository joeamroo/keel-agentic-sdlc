"""The live terminal view of a run.

A keel run spends most of its wall clock waiting on models, so the question
worth answering while it happens is not "what did the log say" but "what is
running right now, what is blocked on a human, and is anything bleeding". This
module answers that in one self-updating frame: the plan DAG grouped by
topological level, one row per node, and a footer carrying the totals.

Three decisions here are worth defending in review.

Rows are grouped by topological level rather than by start time. The level is
the parallelism: every node in a level had its dependencies satisfied by an
earlier level, so three sibling rows spinning at once is not the executor being
clever, it is the plan's shape. Sorting by start time would hide exactly the
property the view exists to show.

The view degrades to plain lines when the output stream is not a terminal.
`rich.Live` repaints by moving the cursor, which turns a piped or CI log into
unreadable escape soup, so the non-TTY path writes one plain timestamped line
per event and never emits a control sequence.

The view never raises at its caller. Unknown node ids, a result for a node that
was dropped by a re-plan, events before `start()`: all are recorded as notes
rather than exceptions. An orchestrator must not die of a display bug.

State is mutated from whichever task calls the hooks. The executor drives those
from a single event loop, so no lock is taken here; `rich.Live` does its own
locking around the repaint.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..models import (
    ApprovalRequest,
    GateDecision,
    ModelTier,
    NodeResult,
    NodeSpec,
    Plan,
    Severity,
    StageKind,
    TaskState,
)

__all__ = [
    "STATE_GLYPH",
    "STATE_LABEL",
    "STATE_STYLE",
    "LiveView",
    "plan_levels",
    "state_style",
]


# --------------------------------------------------------------------------
# State presentation
# --------------------------------------------------------------------------

# Every `TaskState` is mapped explicitly rather than through a default, so
# adding a state to the frozen contract fails the coverage test in tests/test_ui.py
# instead of silently rendering as unstyled text.
STATE_STYLE: dict[TaskState, str] = {
    TaskState.SUBMITTED: "dim",
    TaskState.WORKING: "bold yellow",
    TaskState.COMPLETED: "bold green",
    TaskState.FAILED: "bold red",
    TaskState.CANCELED: "grey50",
    TaskState.INPUT_REQUIRED: "bold magenta",
    TaskState.REJECTED: "bold red",
    # Blocked on credentials rather than on a decision, but from the watcher's
    # seat it is the same situation: the run stopped and needs a human.
    TaskState.AUTH_REQUIRED: "bold magenta",
}

# Colour alone is not a channel. A projector washes out yellow against white and
# roughly one viewer in twelve cannot separate red from green, so each state also
# carries a glyph that survives both.
STATE_GLYPH: dict[TaskState, str] = {
    TaskState.SUBMITTED: "○",  # hollow circle: queued
    TaskState.WORKING: "●",  # filled circle, replaced by a spinner when live
    TaskState.COMPLETED: "✓",
    TaskState.FAILED: "✗",
    TaskState.CANCELED: "⊘",
    TaskState.INPUT_REQUIRED: "⏸",
    TaskState.REJECTED: "✖",
    TaskState.AUTH_REQUIRED: "⚿",
}

# Shown instead of the raw enum value, because "input required" reads at a glance
# from the back of a room and "input_required" does not.
STATE_LABEL: dict[TaskState, str] = {
    TaskState.SUBMITTED: "pending",
    TaskState.WORKING: "working",
    TaskState.COMPLETED: "completed",
    TaskState.FAILED: "failed",
    TaskState.CANCELED: "canceled",
    TaskState.INPUT_REQUIRED: "input required",
    TaskState.REJECTED: "rejected",
    TaskState.AUTH_REQUIRED: "auth required",
}

# Column widths are derived from the contract, not guessed, so a new stage kind
# or task state cannot silently start truncating the board. The node column is
# sized per plan and only capped here, since node ids are free text.
_STAGE_WIDTH = max(len(kind.value) for kind in StageKind)
_STATE_WIDTH = max(len(label) for label in STATE_LABEL.values())
_NODE_WIDTH_CAP = 32

_TIER_STYLE: dict[ModelTier, str] = {
    ModelTier.DEEP: "bold cyan",
    ModelTier.FAST: "dim cyan",
}

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def state_style(state: TaskState) -> str:
    """The `rich` style for a task state.

    Falls back to plain text for a state the map has not been taught, which can
    only happen if the contract gains a member without this module following. A
    missing colour is a cosmetic bug; raising here would abort a live run over
    one.
    """
    return STATE_STYLE.get(state, "")


def plan_levels(plan: Plan) -> list[list[NodeSpec]]:
    """Group a plan's nodes into topological levels for display.

    Delegates to `PlanGraph` so the terminal view and the HTML report lay the
    graph out identically. Two views of one run that disagree about what "level
    2" means are worse than no second view.

    A display surface has to survive input the executor would reject, so a plan
    with a cycle or a dangling dependency degrades to a single flat level rather
    than propagating `GraphError` into the render loop.
    """
    try:
        from ..graph import PlanGraph

        return PlanGraph(plan).levels()
    except Exception:  # GraphError, or the graph module being unavailable
        return [list(plan.nodes)]


@dataclass(slots=True)
class _NodeView:
    """Display state for one node. Not part of any contract, hence private."""

    spec: NodeSpec
    level: int
    state: TaskState = TaskState.SUBMITTED
    attempts: int = 0
    started_at: float | None = None  # monotonic, so a clock change cannot skew it
    frozen_elapsed: float | None = None  # set once the node reaches a terminal state

    def elapsed(self, now: float) -> float:
        if self.frozen_elapsed is not None:
            return self.frozen_elapsed
        if self.started_at is None:
            return 0.0
        return max(0.0, now - self.started_at)


class LiveView:
    """A live, colour-coded board of the plan DAG.

    Construct it, `start()` it, feed it the executor's lifecycle hooks, `stop()`
    it. Also usable as a context manager, which is the safer form because it
    stops the `rich.Live` even when the run raises.

    Whether the board renders or the events are logged as plain lines is decided
    once, at construction, from the output stream.
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        console: Console | None = None,
        file: TextIO | None = None,
        force_plain: bool | None = None,
        refresh_per_second: float = 8.0,
        max_notes: int = 6,
    ) -> None:
        """Wire up the view.

        `force_plain` overrides TTY detection in both directions, which is what
        makes both paths testable and gives an operator a way to demand plain
        output from an interactive shell when piping a transcript to a colleague.
        """
        self._stream: TextIO = file if file is not None else sys.stdout
        if force_plain is not None:
            plain = force_plain
        elif console is not None:
            plain = not console.is_terminal
        else:
            plain = not _is_tty(self._stream)
        self._plain = plain
        # Built even in plain mode so a caller can hand a console in and get the
        # same object back, but never written to while plain.
        self._console = console or Console(file=self._stream, highlight=False)
        self._refresh_per_second = refresh_per_second

        self._run_id = run_id
        self._views: dict[str, _NodeView] = {}
        self._levels: list[list[str]] = []
        self._plan_version = 0
        self._notes: deque[str] = deque(maxlen=max(1, max_notes))

        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._retries = 0
        self._rollbacks = 0
        self._fallbacks = 0
        self._gate_denials = 0
        self._approvals = 0
        self._cost_usd = 0.0

        self._live: Live | None = None
        # One shared spinner instance for every working row. A fresh Spinner per
        # render would reset its own start time each frame and never advance, and
        # sharing it also makes the working rows tick in step, which reads as one
        # wave of parallel work rather than noise.
        self._spinner = Spinner("dots", text=Text("working", style="bold yellow"), style="yellow")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_plain(self) -> bool:
        """True when this view logs plain lines instead of painting a board.

        Public because a caller usually wants to know: a plain view is the right
        moment to also raise the log level, and a test needs to assert which of
        the two paths it is exercising.
        """
        return self._plain

    def start(self) -> None:
        """Begin the run clock and, on a TTY, take over the bottom of the screen."""
        if self._started_at is None:
            self._started_at = time.monotonic()
        self._stopped_at = None
        if self._plain:
            self._write(
                f"keel: run {self._run_id or '(unnamed)'} started, "
                f"{len(self._views)} nodes, plain output because stdout is not a terminal"
            )
            return
        if self._live is not None:
            return
        # `get_renderable` rather than a fixed renderable: the frame has to be
        # rebuilt on every refresh or the elapsed columns would freeze between
        # events, which is the one number a watcher checks constantly.
        self._live = Live(
            console=self._console,
            get_renderable=self._render,
            refresh_per_second=self._refresh_per_second,
            transient=False,
        )
        self._live.start(refresh=True)

    def stop(self) -> None:
        """Release the terminal and print the closing totals.

        The final frame is left on screen rather than erased, so the board is
        still there to talk over once the run ends.
        """
        if self._stopped_at is None:
            self._stopped_at = time.monotonic()
        summary = f"keel: run {self._run_id or '(unnamed)'} finished, {self._summary_text()}"
        if self._plain:
            self._write(summary)
            return
        if self._live is not None:
            live, self._live = self._live, None
            live.stop()
        self._console.print(Text(summary, style="bold"))

    def __enter__(self) -> LiveView:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Executor hooks
    # ------------------------------------------------------------------

    def set_plan(self, plan: Plan) -> None:
        """Adopt a plan, or replace one after a re-plan.

        Nodes that survive a re-plan keep their state, attempt count and elapsed
        time. Resetting them would make the board claim work is starting over
        when it is not, and re-planning is precisely the moment a watcher needs
        to trust what the board says.
        """
        previous = self._views
        views: dict[str, _NodeView] = {}
        levels: list[list[str]] = []
        for depth, level in enumerate(plan_levels(plan), start=1):
            ids: list[str] = []
            for spec in level:
                view = _NodeView(spec=spec, level=depth)
                old = previous.get(spec.id)
                if old is not None:
                    view.state = old.state
                    view.attempts = old.attempts
                    view.started_at = old.started_at
                    view.frozen_elapsed = old.frozen_elapsed
                views[spec.id] = view
                ids.append(spec.id)
            levels.append(ids)

        first_plan = self._plan_version == 0
        self._views = views
        self._levels = levels
        self._plan_version = plan.version
        carried = len(previous.keys() & views.keys()) if previous else 0
        detail = f"v{plan.version}: {len(views)} nodes in {len(levels)} levels"
        if not first_plan:
            detail += f", {carried} carried over"
        self._event("plan" if first_plan else "replan", detail)

    def on_node_start(self, node_id: str) -> None:
        """A node began its first attempt."""
        view = self._views.get(node_id)
        if view is None:
            self._unknown("start", node_id)
            return
        view.state = TaskState.WORKING
        view.attempts = max(view.attempts, 1)
        view.started_at = time.monotonic()
        view.frozen_elapsed = None
        self._event(
            "start",
            f"{node_id} ({view.spec.kind.value}, {view.spec.model_tier.value})",
        )

    def on_node_end(self, result: NodeResult) -> None:
        """A node reached a terminal state. Totals move here, not at start."""
        now = time.monotonic()
        view = self._views.get(result.node_id)
        if view is None:
            self._unknown("end", result.node_id)
        else:
            view.state = result.state
            view.attempts = max(view.attempts, result.attempts)
            # The executor's own measurement wins when it has one, because it
            # brackets the actual work rather than the view's notification of it.
            view.frozen_elapsed = result.duration or view.elapsed(now)

        self._cost_usd += result.cost_usd
        if result.rolled_back:
            self._rollbacks += 1
        if result.used_fallback:
            self._fallbacks += 1

        duration = result.duration or (view.elapsed(now) if view is not None else 0.0)
        plural = "" if result.attempts == 1 else "s"
        detail = (
            f"{result.node_id} {STATE_LABEL.get(result.state, result.state.value)} "
            f"in {duration:.2f}s after {result.attempts} attempt{plural}"
        )
        if result.rolled_back:
            detail += ", rolled back"
        if result.used_fallback:
            detail += ", used fallback tier"
        if result.error:
            detail += f": {_first_line(result.error)}"
        self._event("end", detail)

    def on_retry(self, node_id: str, attempt: int) -> None:
        """A node is being retried. `attempt` is the number about to run."""
        self._retries += 1
        view = self._views.get(node_id)
        if view is None:
            self._unknown("retry", node_id)
        else:
            view.state = TaskState.WORKING
            view.attempts = max(view.attempts, attempt)
            view.started_at = time.monotonic()
            view.frozen_elapsed = None
        self._event("retry", f"{node_id} attempt {attempt}")

    def on_gate(self, decision: GateDecision) -> None:
        """Record an entry or exit gate decision and its reason.

        The reason is shown, never just the verdict. A gate that says no without
        saying why is the failure mode this whole governance plane exists to
        avoid, and the view should not be the place that reintroduces it.
        """
        if not decision.allowed:
            self._gate_denials += 1
        verdict = "allow" if decision.allowed else "DENY"
        detail = f"{decision.gate} {decision.node_id} {verdict}: {decision.reason}"
        if decision.violations:
            worst = max(decision.violations, key=lambda v: _SEVERITY_RANK.get(v.severity, 0))
            plural = "" if len(decision.violations) == 1 else "s"
            detail += f" ({len(decision.violations)} violation{plural}, worst {worst.severity.value})"
        self._event("gate", detail)

    def on_approval_request(self, req: ApprovalRequest) -> None:
        """A node is parked waiting on a human.

        The node is flipped to `INPUT_REQUIRED` here rather than waiting for a
        result, because until the human answers there is no result coming and a
        row still claiming to be working would be a lie.
        """
        self._approvals += 1
        view = self._views.get(req.node_id)
        if view is None:
            self._unknown("approval", req.node_id)
        else:
            view.state = TaskState.INPUT_REQUIRED
            view.frozen_elapsed = view.elapsed(time.monotonic())
        self._event(
            "approval",
            f"{req.node_id} needs {req.impact.value} impact sign off "
            f"[{req.request_id}]: {req.reason}",
        )

    def note(self, msg: str) -> None:
        """Free-text line for anything the typed hooks do not cover."""
        self._event("note", msg)

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds since `start()`, frozen once `stop()` has been called."""
        if self._started_at is None:
            return 0.0
        end = self._stopped_at if self._stopped_at is not None else time.monotonic()
        return max(0.0, end - self._started_at)

    @property
    def cost_usd(self) -> float:
        """Running cost, accumulated from the results reported so far."""
        return self._cost_usd

    def _counts(self) -> tuple[int, int, int]:
        completed = sum(1 for v in self._views.values() if v.state is TaskState.COMPLETED)
        failed = sum(
            1
            for v in self._views.values()
            if v.state in (TaskState.FAILED, TaskState.REJECTED)
        )
        return completed, failed, len(self._views)

    def _summary_text(self) -> str:
        """The closing line.

        Carries everything the footer does, plus the two counters the footer
        leaves out. Fallbacks and gate denials are worth a line at the end and
        not worth a column while the run is moving, since both are already
        called out in the feed as they happen.
        """
        completed, failed, total = self._counts()
        parts = [
            f"completed {completed}/{total}",
            f"failed {failed}",
            f"retries {self._retries}",
            f"rollbacks {self._rollbacks}",
        ]
        if self._fallbacks:
            parts.append(f"fallbacks {self._fallbacks}")
        if self._gate_denials:
            parts.append(f"gate denials {self._gate_denials}")
        if self._approvals:
            parts.append(f"approvals {self._approvals}")
        parts.append(f"cost ${self._cost_usd:.4f}")
        parts.append(f"elapsed {self.elapsed:.2f}s")
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _event(self, kind: str, detail: str) -> None:
        """One state change, routed to whichever output mode is active."""
        if self._started_at is None:
            # Tolerate hooks that fire before start() so a caller cannot lose
            # events to an ordering mistake in its own setup.
            self._started_at = time.monotonic()
        if self._plain:
            self._write(f"{self.elapsed:8.2f}s  {kind:<9}{detail}")
            return
        self._notes.append(f"{self.elapsed:7.2f}s  {kind:<9}{detail}")
        if self._live is not None:
            self._live.refresh()

    def _unknown(self, kind: str, node_id: str) -> None:
        """Record a hook for a node the view has never heard of.

        Usually a stale id left over from a previous plan version. Worth saying
        out loud, not worth raising over.
        """
        self._event(kind, f"{node_id} is not in the current plan, ignoring")

    def _write(self, line: str) -> None:
        """Write one plain line. No styling, no escape sequences, ever."""
        self._stream.write(line + "\n")
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> RenderableType:
        """Build the whole frame. Called on every refresh, so it stays cheap."""
        now = time.monotonic()
        node_width = min(max(self._widest_id(), len("NODE")), _NODE_WIDTH_CAP)

        blocks: list[RenderableType] = [self._table(node_width, header=True)]
        if not self._levels:
            blocks.append(Text("  no plan yet", style="dim"))
        for depth, ids in enumerate(self._levels, start=1):
            if depth > 1:
                blocks.append(Text(""))  # one blank line, so levels read as batches
            blocks.append(self._level_caption(depth, ids))
            table = self._table(node_width, header=False)
            for nid in ids:
                table.add_row(*self._node_row(self._views[nid], now))
            blocks.append(table)

        if self._notes:
            blocks.append(Rule(Text(" recent ", style="dim"), style="dim", align="left"))
            # Cropped rather than wrapped: the feed must stay exactly as tall as
            # the note count, or the frame jumps every time an error is long.
            blocks.append(
                Text("\n".join(self._notes), style="dim", no_wrap=True, overflow="ellipsis")
            )
        blocks.append(Rule(style="dim"))
        blocks.append(self._footer())

        completed, _failed, total = self._counts()
        subtitle = f"plan v{self._plan_version} · {completed}/{total} complete"
        return Panel(
            Group(*blocks),
            title=Text(f"keel  {self._run_id or 'run'}", style="bold"),
            subtitle=Text(subtitle, style="dim"),
            border_style="cyan",
            padding=(0, 1),
        )

    def _widest_id(self) -> int:
        return max((len(v.spec.id) for v in self._views.values()), default=len("NODE"))

    def _table(self, node_width: int, *, header: bool) -> Table:
        """One block of rows, or the shared column header when `header` is set.

        Every column is given an explicit width. Two reasons: the level captions
        live outside the table so they cannot stretch a column, and a live view
        whose columns resize as elapsed times grow digits is unreadable while it
        moves. Fixed widths mean the only thing that changes between frames is
        the data.
        """
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=header,
            show_edge=False,
            pad_edge=False,
            expand=False,
            header_style="bold dim",
        )
        table.add_column(" ", width=3, no_wrap=True)
        table.add_column("NODE", width=node_width, no_wrap=True, overflow="ellipsis")
        table.add_column("STAGE", width=_STAGE_WIDTH, no_wrap=True, overflow="ellipsis")
        table.add_column("STATE", width=_STATE_WIDTH, no_wrap=True, overflow="ellipsis")
        table.add_column("TRY", width=4, justify="right", no_wrap=True)
        table.add_column("ELAPSED", width=8, justify="right", no_wrap=True)
        table.add_column("TIER", width=4, no_wrap=True)
        return table

    def _level_caption(self, depth: int, ids: Sequence[str]) -> Text:
        """The line that makes parallelism legible without reading the rows."""
        running = sum(1 for nid in ids if self._views[nid].state is TaskState.WORKING)
        done = sum(1 for nid in ids if self._views[nid].state.is_terminal)
        hint = f"{len(ids)} nodes may run in parallel" if len(ids) > 1 else "1 node"
        caption = Text.assemble(
            (f"level {depth}", "bold cyan"),
            ("  ·  ", "dim"),
            (hint, "dim"),
        )
        if running:
            caption.append("  ·  ", style="dim")
            caption.append(f"{running} running now", style="yellow")
        elif done == len(ids):
            caption.append("  ·  ", style="dim")
            caption.append("done", style="green")
        return caption

    def _node_row(self, view: _NodeView, now: float) -> tuple[RenderableType, ...]:
        style = state_style(view.state)
        working = view.state is TaskState.WORKING
        state_cell: RenderableType = (
            self._spinner if working else Text(STATE_LABEL[view.state], style=style)
        )
        attempts = view.attempts or 0
        limit = view.spec.retry.max_attempts
        attempt_cell = Text(
            "-" if not attempts else f"{attempts}/{limit}",
            style="yellow" if attempts > 1 else "dim",
        )
        # The id is the label a viewer reads first, so it stays legible: bold
        # while running, dim while queued, and otherwise coloured by outcome.
        if working:
            id_style = "bold"
        elif view.state is TaskState.SUBMITTED:
            id_style = "dim"
        else:
            id_style = style
        return (
            Text(f"  {STATE_GLYPH[view.state]}", style=style),
            Text(view.spec.id, style=id_style),
            Text(view.spec.kind.value, style="dim"),
            state_cell,
            attempt_cell,
            Text(f"{view.elapsed(now):.1f}s", style="dim" if not working else "yellow"),
            Text(view.spec.model_tier.value, style=_TIER_STYLE.get(view.spec.model_tier, "")),
        )

    def _footer(self) -> Text:
        """The one line a watcher reads if they read nothing else."""
        completed, failed, total = self._counts()
        sep = ("  ·  ", "dim")
        line = Text(no_wrap=True)
        line.append("completed ", style="dim")
        line.append(f"{completed}/{total}", style="bold green" if completed else "bold")
        if failed:
            line.append(*sep)
            line.append("failed ", style="dim")
            line.append(str(failed), style="bold red")
        line.append(*sep)
        line.append("retries ", style="dim")
        line.append(str(self._retries), style="yellow" if self._retries else "bold")
        line.append(*sep)
        line.append("rollbacks ", style="dim")
        line.append(str(self._rollbacks), style="yellow" if self._rollbacks else "bold")
        line.append(*sep)
        line.append("elapsed ", style="dim")
        line.append(f"{self.elapsed:.1f}s", style="bold")
        line.append(*sep)
        line.append("cost ", style="dim")
        line.append(f"${self._cost_usd:.4f}", style="bold")
        return line


def _is_tty(stream: TextIO) -> bool:
    """True only for a real terminal.

    Deliberately asks the stream rather than trusting environment hints: the
    thing that breaks when this is wrong is a piped log full of escape codes, so
    the conservative answer is the right default.
    """
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except ValueError:  # closed stream
        return False


def _first_line(text: str) -> str:
    """First line of an error, for a one-line log entry.

    Model output and tracebacks are both multi-line, and a board that grows by
    forty lines when one node fails stops being a board.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    first = stripped.splitlines()[0]
    return first if len(first) <= 160 else first[:157] + "..."
