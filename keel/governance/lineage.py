"""Artifact provenance: who produced what, and who read which version of it.

An SDLC orchestrator that only tracks node dependencies can answer "what runs
next". It cannot answer the question that actually comes up in review, which is
"why does this file look like this". That answer lives in data the plan graph
never holds: the specific revision of the design document the implement node
was looking at when it wrote the code.

`LineageStore` records two facts and derives the rest from them. A node
produced an artifact with a given content hash. A node consumed an artifact at
a given content hash. Once both are recorded, three useful things fall out:

* Staleness. If the design document is regenerated and its hash changes, every
  node that consumed the old hash was reasoning about a document that no longer
  exists. `stale_consumers` names them, and that set is exactly the subgraph
  the planner is asked to repair. This is the trigger for dynamic re-planning,
  and it is hash-based rather than timestamp-based on purpose: a node that
  re-runs and produces byte-identical output invalidates nothing, so retries
  and reruns do not cascade a pointless rebuild through the graph.
* Provenance. `explain` walks the consumption edges backwards from an artifact
  through the nodes that produced its inputs, so a reviewer gets the chain that
  led to a file instead of a bare "produced by implement".
* A picture. `to_mermaid` renders artifacts and nodes as one bipartite graph,
  with stale consumptions drawn as dotted edges.

Everything is held in memory and in insertion order. The store is a derived
view of a run; the audit log is the durable record, and lineage edges are
emitted there as events by the orchestrator.
"""

from __future__ import annotations

import re

from keel.models import Artifact, LineageEdge

__all__ = ["LineageStore"]

_MERMAID_UNSAFE = re.compile(r"[^0-9A-Za-z_]")

# Enough of a sha to be unambiguous in a diagram without pushing the label
# wider than the box. The full hash stays available on the artifact.
_SHORT_SHA = 8


def _escape_label(text: str) -> str:
    """Make free text safe inside a mermaid label."""
    return text.replace('"', "#quot;").replace("\n", " ")


class LineageStore:
    """Producer and consumer records for every artifact in a run.

    Artifact names are the identity, not `artifact_id`. A regenerated design
    document is a new `Artifact` object with a new id and the same name, and
    "the design document changed" is the fact staleness depends on.
    """

    __slots__ = ("_history", "_edges")

    def __init__(self) -> None:
        # name -> revisions in the order they were produced. The list is the
        # answer to "it used to look different", which is half of any real
        # provenance question.
        self._history: dict[str, list[Artifact]] = {}
        self._edges: list[LineageEdge] = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_production(self, artifact: Artifact) -> None:
        """Record that a node produced this artifact.

        Re-recording content that is already the latest revision is a no-op. A
        node that retries and produces byte-identical output has not revised
        anything, and counting it as a revision would make the history lie
        about how many times the file really changed.
        """
        revisions = self._history.setdefault(artifact.name, [])
        if revisions and revisions[-1].sha == artifact.sha:
            return
        revisions.append(artifact)

    def record_consumption(self, node_id: str, artifact: Artifact) -> LineageEdge:
        """Record that `node_id` read this exact revision, and return the edge.

        An artifact that was never registered as produced is registered here.
        Consumption is proof it existed, and a lineage graph with a hole in it
        is worse than one that infers the obvious.
        """
        if not self._knows_sha(artifact.name, artifact.sha):
            self.record_production(artifact)
        edge = LineageEdge(
            artifact_name=artifact.name,
            artifact_sha=artifact.sha,
            produced_by=artifact.produced_by,
            consumed_by=node_id,
        )
        self._edges.append(edge)
        return edge

    def _knows_sha(self, name: str, sha: str) -> bool:
        return any(rev.sha == sha for rev in self._history.get(name, ()))

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def edges(self) -> list[LineageEdge]:
        """Every consumption edge, in the order it was recorded (a copy)."""
        return list(self._edges)

    @property
    def artifacts(self) -> dict[str, Artifact]:
        """Latest known revision of every artifact, by name."""
        return {name: revisions[-1] for name, revisions in self._history.items() if revisions}

    def history(self, artifact_name: str) -> list[Artifact]:
        """Every recorded revision of one artifact, oldest first."""
        return list(self._history.get(artifact_name, ()))

    def latest(self, artifact_name: str) -> Artifact | None:
        revisions = self._history.get(artifact_name)
        return revisions[-1] if revisions else None

    def producer(self, artifact_name: str) -> str | None:
        """Node id that produced the latest revision, if the artifact is known."""
        latest = self.latest(artifact_name)
        return latest.produced_by if latest else None

    def consumers(self, artifact_name: str) -> list[str]:
        """Nodes that read this artifact, first consumption first, deduplicated."""
        seen: list[str] = []
        for edge in self._edges:
            if edge.artifact_name == artifact_name and edge.consumed_by not in seen:
                seen.append(edge.consumed_by)
        return seen

    def inputs_of(self, node_id: str) -> list[LineageEdge]:
        """Every artifact revision a node consumed, in consumption order."""
        return [edge for edge in self._edges if edge.consumed_by == node_id]

    def outputs_of(self, node_id: str) -> list[Artifact]:
        """Every artifact revision a node produced, in production order."""
        return [
            revision
            for revisions in self._history.values()
            for revision in revisions
            if revision.produced_by == node_id
        ]

    # ------------------------------------------------------------------
    # Staleness
    # ------------------------------------------------------------------

    def stale_consumers(self, artifact_name: str, new_sha: str) -> set[str]:
        """Nodes that consumed a different revision of this artifact.

        Called with the hash of a freshly produced artifact. Every node in the
        returned set worked from content that no longer exists, so its output
        can no longer be trusted and the planner should be asked to re-plan
        that subgraph. An empty set means the regeneration changed nothing that
        anyone had already read, and the run continues untouched.
        """
        return {
            edge.consumed_by
            for edge in self._edges
            if edge.artifact_name == artifact_name and edge.artifact_sha != new_sha
        }

    def stale_edges(self, artifact_name: str, new_sha: str) -> list[LineageEdge]:
        """The edges behind `stale_consumers`, for reporting which read what."""
        return [
            edge
            for edge in self._edges
            if edge.artifact_name == artifact_name and edge.artifact_sha != new_sha
        ]

    # ------------------------------------------------------------------
    # Explanation
    # ------------------------------------------------------------------

    def explain(self, artifact_name: str) -> str:
        """Human-readable provenance chain for one artifact.

        Answers "why does this file look like this" by naming the node that
        produced it, listing the revisions it went through, then walking
        backwards through what that node consumed, and what produced those, to
        the roots of the run.

        Written for a person reading a run report or a pull request comment,
        so it is prose-shaped rather than a serialized graph. `to_mermaid` is
        the machine-shaped view of the same data.
        """
        revisions = self._history.get(artifact_name)
        if not revisions:
            return f'no lineage recorded for "{artifact_name}"'

        latest = revisions[-1]
        lines = [
            f'why "{artifact_name}" looks like this',
            f"  current: sha {latest.sha}, produced by node '{latest.produced_by}'",
        ]
        if len(revisions) > 1:
            lines.append(f"  history: {len(revisions)} revisions recorded")
            for index, revision in enumerate(revisions, start=1):
                marker = " (current)" if index == len(revisions) else ""
                lines.append(
                    f"    {index}. sha {revision.sha} by '{revision.produced_by}'{marker}"
                )
        else:
            lines.append("  history: 1 revision recorded")

        lines.append("  inputs:")
        body = self._explain_node(latest.produced_by, depth=2, path=(latest.produced_by,))
        lines.extend(body)
        return "\n".join(lines)

    def _explain_node(self, node_id: str, depth: int, path: tuple[str, ...]) -> list[str]:
        """Recursive half of `explain`, one indented block per node."""
        indent = "  " * depth
        edges = self.inputs_of(node_id)
        if not edges:
            return [f"{indent}node '{node_id}' consumed nothing that was recorded"]

        lines: list[str] = []
        for edge in edges:
            current = self.latest(edge.artifact_name)
            drift = ""
            if current is not None and current.sha != edge.artifact_sha:
                drift = f" [stale, now {current.sha}]"
            lines.append(
                f"{indent}node '{node_id}' consumed \"{edge.artifact_name}\" @ "
                f"{edge.artifact_sha} (produced by '{edge.produced_by}'){drift}"
            )
            if edge.produced_by in path:
                # Lineage should be acyclic, but it is assembled from whatever
                # the nodes reported, so a loop is an input error rather than
                # an impossibility. Stop and say so instead of recursing.
                lines.append(
                    f"{indent}  ... cycle back to node '{edge.produced_by}', stopping"
                )
                continue
            lines.extend(
                self._explain_node(edge.produced_by, depth + 1, path + (edge.produced_by,))
            )
        return lines

    # ------------------------------------------------------------------
    # Diagram
    # ------------------------------------------------------------------

    def to_mermaid(self) -> str:
        """Mermaid `graph LR` source for the artifact and node lineage graph.

        Bipartite by design: nodes are boxes, artifacts are rounded, and an
        edge always crosses between the two kinds, so "which stage touched this
        file" is readable at a glance. Node and artifact identifiers are
        namespaced apart because a node called `spec` and a file called `spec`
        are different things that would otherwise collide.

        A consumption of a revision that is no longer current is drawn dotted
        and labelled, which puts the re-planning trigger on the picture.
        """
        node_ids: list[str] = []
        for revisions in self._history.values():
            for revision in revisions:
                if revision.produced_by and revision.produced_by not in node_ids:
                    node_ids.append(revision.produced_by)
        for edge in self._edges:
            if edge.consumed_by not in node_ids:
                node_ids.append(edge.consumed_by)

        lines = [
            f"%% keel lineage, {len(self._history)} artifacts, {len(self._edges)} edges",
            "graph LR",
        ]
        for node_id in node_ids:
            lines.append(f'    {self._node_key(node_id)}["{_escape_label(node_id)}"]')
        for name, revisions in self._history.items():
            if not revisions:
                continue
            label = f"{_escape_label(name)}<br/>{revisions[-1].sha[:_SHORT_SHA]}"
            lines.append(f'    {self._artifact_key(name)}(["{label}"])')

        drawn: set[tuple[str, str, str]] = set()
        for name, revisions in self._history.items():
            for revision in revisions:
                if not revision.produced_by:
                    continue
                link = (self._node_key(revision.produced_by), "produces", self._artifact_key(name))
                if link in drawn:
                    continue
                drawn.add(link)
                lines.append(f"    {link[0]} --> {link[2]}")
        for edge in self._edges:
            current = self.latest(edge.artifact_name)
            stale = current is not None and current.sha != edge.artifact_sha
            source = self._artifact_key(edge.artifact_name)
            target = self._node_key(edge.consumed_by)
            link = (source, "stale" if stale else "consumes", target)
            if link in drawn:
                continue
            drawn.add(link)
            if stale:
                lines.append(f"    {source} -. stale {edge.artifact_sha[:_SHORT_SHA]} .-> {target}")
            else:
                lines.append(f"    {source} --> {target}")
        return "\n".join(lines)

    @staticmethod
    def _node_key(node_id: str) -> str:
        return f"n_{_MERMAID_UNSAFE.sub('_', node_id)}"

    @staticmethod
    def _artifact_key(name: str) -> str:
        return f"a_{_MERMAID_UNSAFE.sub('_', name)}"

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"LineageStore(artifacts={len(self._history)}, edges={len(self._edges)})"
