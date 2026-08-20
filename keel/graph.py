"""The dependency graph over a `Plan`.

A plan is only useful if something can answer three questions cheaply: what may
run right now, what has to wait, and what stopped being trustworthy when an
upstream node changed its output. That is this module. `PlanGraph` validates a
plan once, up front, and then answers those questions for the rest of the run.

Validation is deliberately eager. A plan with a cycle or a dangling dependency
is unrunnable, and finding that out halfway through a run means rolling back
work that never should have started, so the constructor refuses to build a
graph it cannot execute.

Every ordering here is deterministic and derived from the order nodes were
declared in the plan. Two runs over the same plan therefore produce identical
levels, identical ready sets and identical mermaid source, which is what lets a
replayed run be diffed line by line against the live run it came from.

Nothing in this module mutates the plan it was given.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from .models import NodeSpec, Plan

__all__ = ["GraphError", "PlanGraph"]


class GraphError(ValueError):
    """The plan cannot be turned into an executable graph.

    Subclasses `ValueError` because every case is a malformed input: a duplicate
    id, a dependency on a node that does not exist, or a cycle. Callers that
    already treat bad input uniformly keep working, and the message always names
    the offending ids so the failure is actionable without a debugger.
    """


# DFS colors for cycle detection. Gray means "on the current path", which is the
# only state that proves a back edge.
_WHITE = 0
_GRAY = 1
_BLACK = 2

_MERMAID_UNSAFE = re.compile(r"[^0-9A-Za-z_]")


class PlanGraph:
    """A validated view of a `Plan` as a directed acyclic graph.

    Edges run in execution order: an edge points from a dependency to the node
    that waits on it. `NodeSpec.depends_on` is the reverse of that, so the graph
    keeps both directions materialized rather than scanning the node list on
    every query.
    """

    __slots__ = ("_plan", "_nodes", "_order", "_index", "_deps", "_dependents", "_level_ids")

    def __init__(self, plan: Plan) -> None:
        """Build and validate the graph.

        Raises `GraphError` on duplicate node ids, on a dependency that names an
        unknown node, and on any cycle. The cycle message spells out the path so
        the planner can be told exactly which chain to break.
        """
        self._plan = plan
        self._nodes: dict[str, NodeSpec] = {}
        for node in plan.nodes:
            if node.id in self._nodes:
                raise GraphError(f"duplicate node id: {node.id!r}")
            self._nodes[node.id] = node

        self._order: tuple[str, ...] = tuple(self._nodes)
        self._index: dict[str, int] = {nid: i for i, nid in enumerate(self._order)}

        # Dependencies are deduplicated but keep their declared order, so a
        # repeated edge in a hand-written plan is harmless rather than fatal.
        deps: dict[str, tuple[str, ...]] = {}
        dependents: dict[str, list[str]] = {nid: [] for nid in self._order}
        for nid in self._order:
            seen: dict[str, None] = {}
            for dep in self._nodes[nid].depends_on:
                if dep not in self._nodes:
                    raise GraphError(f"node {nid!r} depends on unknown node {dep!r}")
                seen.setdefault(dep, None)
            deps[nid] = tuple(seen)
            for dep in seen:
                dependents[dep].append(nid)
        self._deps = deps
        self._dependents: dict[str, tuple[str, ...]] = {
            nid: tuple(children) for nid, children in dependents.items()
        }

        cycle = self._find_cycle()
        if cycle is not None:
            raise GraphError("cycle detected: " + " -> ".join(cycle))

        self._level_ids: tuple[tuple[str, ...], ...] = self._compute_levels()

    # ------------------------------------------------------------------
    # Basic access
    # ------------------------------------------------------------------

    @property
    def plan(self) -> Plan:
        """The plan this graph was built from. Treat it as read only."""
        return self._plan

    @property
    def node_ids(self) -> tuple[str, ...]:
        """All node ids in plan declaration order."""
        return self._order

    def node(self, node_id: str) -> NodeSpec:
        """Look up one node, or raise `GraphError` if it is not in the plan."""
        try:
            return self._nodes[node_id]
        except KeyError:
            raise GraphError(f"no such node: {node_id!r}") from None

    def dependencies(self, node_id: str) -> tuple[str, ...]:
        """Direct dependencies, deduplicated, in declared order."""
        self._require(node_id)
        return self._deps[node_id]

    def dependents(self, node_id: str) -> tuple[str, ...]:
        """Nodes that wait directly on this one."""
        self._require(node_id)
        return self._dependents[node_id]

    @property
    def roots(self) -> tuple[str, ...]:
        """Nodes with no dependencies. These start the run."""
        return tuple(nid for nid in self._order if not self._deps[nid])

    @property
    def leaves(self) -> tuple[str, ...]:
        """Nodes nothing waits on. When these finish, the run is done."""
        return tuple(nid for nid in self._order if not self._dependents[nid])

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def levels(self) -> list[list[NodeSpec]]:
        """Topological levels, outermost first.

        Every node in a level has all of its dependencies satisfied by earlier
        levels, so a level is a set of nodes that may run concurrently and the
        boundary between two levels is a synchronization barrier. This is the
        whole of the sequential-versus-parallel decision: the plan's shape
        decides it, not the executor.

        Nodes keep their plan order inside a level so the layout is stable
        across runs.
        """
        return [[self._nodes[nid] for nid in level] for level in self._level_ids]

    def ready(self, completed: set[str]) -> list[NodeSpec]:
        """Nodes that can start now, given what has already finished.

        A node is ready when every dependency is in `completed` and it is not
        itself completed. Unlike `levels`, this tolerates a ragged front: if one
        branch of the graph runs ahead, its successors are offered as soon as
        their own dependencies land rather than waiting for the slowest node in
        the level.

        Unknown ids in `completed` raise `GraphError`. Silently ignoring them
        would hide the real bug, which is usually a stale id left over from a
        previous plan version after a re-plan.
        """
        unknown = sorted(set(completed) - self._nodes.keys())
        if unknown:
            raise GraphError(f"completed contains unknown node ids: {', '.join(unknown)}")
        return [
            self._nodes[nid]
            for nid in self._order
            if nid not in completed and all(dep in completed for dep in self._deps[nid])
        ]

    # ------------------------------------------------------------------
    # Reachability and staleness
    # ------------------------------------------------------------------

    def descendants(self, node_id: str) -> set[str]:
        """Everything transitively downstream. Excludes the node itself."""
        return self._reach(node_id, self._dependents)

    def ancestors(self, node_id: str) -> set[str]:
        """Everything transitively upstream. Excludes the node itself."""
        return self._reach(node_id, self._deps)

    def mark_stale(self, changed_node_id: str) -> set[str]:
        """The set that must re-run because this node's output changed.

        Downstream nodes consumed a specific version of an upstream artifact
        (see `LineageEdge`), so once that artifact's hash moves, their results
        describe an input that no longer exists. The changed node is included
        because the change is what triggered the re-run in the first place.
        """
        stale = self.descendants(changed_node_id)
        stale.add(changed_node_id)
        return stale

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def subgraph(self, node_ids: set[str]) -> Plan:
        """Extract the given nodes as a standalone plan.

        Only edges internal to the set survive; a dependency on a node outside
        the set is dropped, which turns that node into a root of the sub-plan.
        That is what the re-planner wants when it repairs a stale region: the
        nodes outside the set already ran and their artifacts are available, so
        inside the sub-plan they are inputs rather than work.

        The returned nodes are copies. Rewriting a repaired sub-plan therefore
        cannot corrupt the plan it was carved out of.
        """
        unknown = sorted(node_ids - self._nodes.keys())
        if unknown:
            raise GraphError(f"cannot extract unknown node ids: {', '.join(unknown)}")

        nodes = [
            self._clone(self._nodes[nid], [d for d in self._deps[nid] if d in node_ids])
            for nid in self._order
            if nid in node_ids
        ]
        return Plan(
            nodes=nodes,
            version=self._plan.version,
            rationale=self._plan.rationale,
            supersedes=self._plan.supersedes,
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def critical_path(self) -> list[str]:
        """The longest dependency chain, from root to leaf.

        Length is measured in nodes, not seconds, because a `NodeSpec` carries
        no duration estimate. It still bounds the run: no amount of concurrency
        makes a run shorter than this chain, so it is the first thing to look at
        when the end-to-end latency metric is worse than expected.

        Ties break toward the earlier-declared node, so the result is stable.
        """
        length: dict[str, int] = {}
        previous: dict[str, str | None] = {}
        for level in self._level_ids:
            for nid in level:
                best_dep: str | None = None
                best_len = 0
                for dep in self._deps[nid]:
                    if length[dep] > best_len:
                        best_len = length[dep]
                        best_dep = dep
                length[nid] = best_len + 1
                previous[nid] = best_dep

        end: str | None = None
        for nid in self._order:
            if end is None or length[nid] > length[end]:
                end = nid
        if end is None:
            return []

        path: list[str] = []
        cursor: str | None = end
        while cursor is not None:
            path.append(cursor)
            cursor = previous[cursor]
        path.reverse()
        return path

    def to_mermaid(self) -> str:
        """Mermaid `graph TD` source for the DAG.

        Emitted rather than drawn so the same string can go into the HTML run
        report, into docs and into a code review comment. Labels carry the node
        id and its stage kind, which is enough to read the plan without opening
        it. Ids are rewritten to a mermaid-safe form because a node id is free
        text and characters like `.` or `-` break the parser.
        """
        safe = self._mermaid_ids()
        lines = [f"%% keel plan v{self._plan.version}, {len(self._order)} nodes", "graph TD"]
        for nid in self._order:
            node = self._nodes[nid]
            label = f"{_escape_label(nid)}<br/>{_escape_label(node.kind.value)}"
            lines.append(f'    {safe[nid]}["{label}"]')
        for nid in self._order:
            for dep in self._deps[nid]:
                lines.append(f"    {safe[dep]} --> {safe[nid]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise GraphError(f"no such node: {node_id!r}")

    def _reach(self, node_id: str, edges: dict[str, tuple[str, ...]]) -> set[str]:
        """Breadth-first closure over one edge direction, minus the start node."""
        self._require(node_id)
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for nxt in edges[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        seen.discard(node_id)
        return seen

    def _find_cycle(self) -> list[str] | None:
        """Return one cycle as a path that starts and ends on the same id.

        Iterative rather than recursive so a pathological generated plan cannot
        blow the interpreter stack, and it returns the actual path instead of a
        bare boolean because "there is a cycle somewhere" is not a fixable
        report. The walk follows dependencies and the result is reversed, so the
        path reads in execution order.
        """
        color = dict.fromkeys(self._order, _WHITE)
        for start in self._order:
            if color[start] != _WHITE:
                continue
            color[start] = _GRAY
            path = [start]
            stack = [(start, iter(self._deps[start]))]
            while stack:
                node, pending = stack[-1]
                descended = False
                for nxt in pending:
                    if color[nxt] == _GRAY:
                        cycle = path[path.index(nxt) :] + [nxt]
                        cycle.reverse()
                        return cycle
                    if color[nxt] == _WHITE:
                        color[nxt] = _GRAY
                        path.append(nxt)
                        stack.append((nxt, iter(self._deps[nxt])))
                        descended = True
                        break
                if not descended:
                    color[node] = _BLACK
                    stack.pop()
                    path.pop()
        return None

    def _compute_levels(self) -> tuple[tuple[str, ...], ...]:
        """Kahn's algorithm, batched one level at a time.

        Batching is the point: the frontier at each step is exactly the set of
        nodes whose dependencies all landed in earlier levels, which is the set
        that may run in parallel.
        """
        remaining = {nid: len(self._deps[nid]) for nid in self._order}
        frontier = [nid for nid in self._order if remaining[nid] == 0]
        levels: list[tuple[str, ...]] = []
        while frontier:
            levels.append(tuple(frontier))
            nxt: list[str] = []
            for nid in frontier:
                for child in self._dependents[nid]:
                    remaining[child] -= 1
                    if remaining[child] == 0:
                        nxt.append(child)
            nxt.sort(key=self._index.__getitem__)
            frontier = nxt
        return tuple(levels)

    def _mermaid_ids(self) -> dict[str, str]:
        """Map node ids to unique mermaid-safe identifiers."""
        safe: dict[str, str] = {}
        taken: set[str] = set()
        for i, nid in enumerate(self._order):
            candidate = _MERMAID_UNSAFE.sub("_", nid)
            if not candidate or not candidate[0].isalpha():
                candidate = f"n_{candidate}"
            if candidate in taken:
                candidate = f"{candidate}_{i}"
            taken.add(candidate)
            safe[nid] = candidate
        return safe

    @staticmethod
    def _clone(node: NodeSpec, depends_on: Iterable[str]) -> NodeSpec:
        """Copy a node, rewriting its dependencies and unsharing its lists."""
        return replace(
            node,
            depends_on=list(depends_on),
            retry=replace(node.retry),
            entry_rules=list(node.entry_rules),
            exit_rules=list(node.exit_rules),
            produces=list(node.produces),
        )


def _escape_label(text: str) -> str:
    """Neutralize the characters that end a mermaid label early.

    Applied per label fragment, not to the finished label, so the `<br/>` the
    caller puts between fragments survives while a `<` inside a node id does
    not become markup.
    """
    for raw, entity in (
        ("&", "#amp;"),
        ('"', "#quot;"),
        ("<", "#lt;"),
        (">", "#gt;"),
        ("[", "#91;"),
        ("]", "#93;"),
    ):
        text = text.replace(raw, entity)
    return text
