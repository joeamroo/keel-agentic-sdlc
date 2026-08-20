"""Tests for `keel.governance.lineage`.

The scenario under most of these is the one that makes lineage worth keeping:
the analyze stage produces a spec, design reads it and produces a document,
implement reads that and writes code, and then something upstream is
regenerated and the run has to work out what it just invalidated.
"""

from __future__ import annotations

from keel.governance.lineage import LineageStore
from keel.models import Artifact


def artifact(name: str, content: str, produced_by: str) -> Artifact:
    return Artifact(name=name, content=content, produced_by=produced_by)


def chain() -> tuple[LineageStore, Artifact, Artifact, Artifact]:
    """analyze -> spec.md -> design -> design.md -> implement -> api.py."""
    store = LineageStore()
    spec = artifact("spec.md", "one health endpoint", "analyze")
    design = artifact("design.md", "a single flask route", "design")
    code = artifact("api.py", "def health(): return 'ok'", "implement")

    store.record_production(spec)
    store.record_consumption("design", spec)
    store.record_production(design)
    store.record_consumption("implement", design)
    store.record_production(code)
    return store, spec, design, code


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_record_production_tracks_latest_and_history() -> None:
    store = LineageStore()
    first = artifact("design.md", "version one", "design")
    second = artifact("design.md", "version two", "design")

    store.record_production(first)
    store.record_production(second)

    assert store.artifacts["design.md"].content == "version two"
    assert [a.content for a in store.history("design.md")] == ["version one", "version two"]
    assert store.latest("design.md") is second
    assert store.producer("design.md") == "design"


def test_identical_content_is_not_a_new_revision() -> None:
    """A retry that reproduces the same bytes has not changed anything."""
    store = LineageStore()
    store.record_production(artifact("design.md", "same bytes", "design"))
    store.record_production(artifact("design.md", "same bytes", "design"))

    assert len(store.history("design.md")) == 1


def test_record_consumption_returns_an_edge_naming_the_revision() -> None:
    store, spec, _, _ = chain()
    edge = store.record_consumption("test", spec)

    assert edge.artifact_name == "spec.md"
    assert edge.artifact_sha == spec.sha
    assert edge.produced_by == "analyze"
    assert edge.consumed_by == "test"
    assert edge.at > 0


def test_record_consumption_registers_an_artifact_it_has_not_seen() -> None:
    """Consumption is proof the artifact existed, so the graph stays closed."""
    store = LineageStore()
    spec = artifact("spec.md", "one health endpoint", "analyze")
    store.record_consumption("design", spec)

    assert store.latest("spec.md") is spec
    assert store.producer("spec.md") == "analyze"


def test_consuming_an_older_revision_does_not_reorder_history() -> None:
    store = LineageStore()
    first = artifact("design.md", "version one", "design")
    second = artifact("design.md", "version two", "design")
    store.record_production(first)
    store.record_production(second)

    store.record_consumption("implement", first)

    assert [a.content for a in store.history("design.md")] == ["version one", "version two"]
    assert store.latest("design.md") is second


def test_accessors_return_copies() -> None:
    store, spec, _, _ = chain()
    store.edges.clear()
    store.history("spec.md").clear()

    assert len(store.edges) == 2
    assert store.history("spec.md") == [spec]


def test_consumers_and_input_output_views() -> None:
    store, spec, design, code = chain()
    store.record_consumption("test", code)

    assert store.consumers("spec.md") == ["design"]
    assert [e.artifact_name for e in store.inputs_of("design")] == ["spec.md"]
    assert [a.name for a in store.outputs_of("implement")] == ["api.py"]
    assert store.outputs_of("nobody") == []


def test_consumers_are_deduplicated_but_ordered() -> None:
    store, spec, _, _ = chain()
    store.record_consumption("test", spec)
    store.record_consumption("design", spec)

    assert store.consumers("spec.md") == ["design", "test"]


# --------------------------------------------------------------------------
# Staleness, the re-planning trigger
# --------------------------------------------------------------------------


def test_stale_consumers_detects_a_changed_hash() -> None:
    store, _, design, _ = chain()
    store.record_consumption("document", design)

    revised = artifact("design.md", "two flask routes after review", "design")
    store.record_production(revised)

    assert revised.sha != design.sha
    assert store.stale_consumers("design.md", revised.sha) == {"implement", "document"}


def test_stale_consumers_is_empty_when_the_hash_is_unchanged() -> None:
    """A node that re-ran and produced identical bytes invalidates nothing."""
    store, _, design, _ = chain()
    rerun = artifact("design.md", design.content, "design")

    assert rerun.sha == design.sha
    assert store.stale_consumers("design.md", rerun.sha) == set()


def test_stale_consumers_is_scoped_to_one_artifact() -> None:
    store, spec, design, _ = chain()
    revised = artifact("spec.md", "two health endpoints", "analyze")

    assert store.stale_consumers("spec.md", revised.sha) == {"design"}
    assert store.stale_consumers("design.md", design.sha) == set()


def test_stale_consumers_of_an_unknown_artifact_is_empty() -> None:
    store, _, _, _ = chain()
    assert store.stale_consumers("never-seen.md", "0000000000000000") == set()


def test_stale_edges_name_the_revision_that_went_stale() -> None:
    store, _, design, _ = chain()
    revised = artifact("design.md", "two flask routes after review", "design")

    edges = store.stale_edges("design.md", revised.sha)
    assert [e.consumed_by for e in edges] == ["implement"]
    assert edges[0].artifact_sha == design.sha


def test_an_artifact_nobody_read_yet_is_never_stale() -> None:
    store, _, _, code = chain()
    revised = artifact("api.py", "def health(): return 'still ok'", "implement")

    assert store.stale_consumers("api.py", revised.sha) == set()


# --------------------------------------------------------------------------
# Explanation
# --------------------------------------------------------------------------


def test_explain_walks_the_chain_back_to_the_root() -> None:
    store, spec, design, code = chain()
    text = store.explain("api.py")

    assert 'why "api.py" looks like this' in text
    assert f"current: sha {code.sha}, produced by node 'implement'" in text
    assert f"node 'implement' consumed \"design.md\" @ {design.sha}" in text
    assert f"node 'design' consumed \"spec.md\" @ {spec.sha}" in text
    assert "node 'analyze' consumed nothing that was recorded" in text

    # The chain reads top down: nearest input first, root last.
    assert text.index("design.md") < text.index("spec.md")


def test_explain_reports_revisions_and_marks_the_current_one() -> None:
    store = LineageStore()
    first = artifact("design.md", "version one", "design")
    second = artifact("design.md", "version two", "design")
    store.record_production(first)
    store.record_production(second)

    text = store.explain("design.md")
    assert "history: 2 revisions recorded" in text
    assert f"1. sha {first.sha} by 'design'" in text
    assert f"2. sha {second.sha} by 'design' (current)" in text


def test_explain_flags_an_input_that_has_since_moved_on() -> None:
    store, _, design, _ = chain()
    revised = artifact("design.md", "two flask routes after review", "design")
    store.record_production(revised)

    text = store.explain("api.py")
    assert f"[stale, now {revised.sha}]" in text


def test_explain_of_an_unknown_artifact_says_so() -> None:
    assert LineageStore().explain("ghost.md") == 'no lineage recorded for "ghost.md"'


def test_explain_stops_on_a_cycle() -> None:
    """Lineage is assembled from what nodes reported, so a loop is possible."""
    store = LineageStore()
    left = artifact("left.md", "left", "one")
    right = artifact("right.md", "right", "two")
    store.record_consumption("two", left)
    store.record_consumption("one", right)

    text = store.explain("right.md")
    assert "cycle back to node 'two', stopping" in text


def test_explain_covers_a_node_with_several_inputs() -> None:
    store = LineageStore()
    spec = artifact("spec.md", "the requirement", "analyze")
    notes = artifact("notes.md", "the constraints", "analyze")
    store.record_consumption("design", spec)
    store.record_consumption("design", notes)
    store.record_production(artifact("design.md", "the design", "design"))

    text = store.explain("design.md")
    assert '"spec.md"' in text
    assert '"notes.md"' in text


# --------------------------------------------------------------------------
# Diagram
# --------------------------------------------------------------------------


def test_to_mermaid_renders_nodes_artifacts_and_both_edge_directions() -> None:
    store, _, _, _ = chain()
    diagram = store.to_mermaid()

    assert diagram.splitlines()[1] == "graph LR"
    assert '    n_analyze["analyze"]' in diagram
    assert "a_spec_md([" in diagram
    assert "    n_analyze --> a_spec_md" in diagram  # production
    assert "    a_spec_md --> n_design" in diagram  # consumption
    assert "3 artifacts, 2 edges" in diagram


def test_to_mermaid_marks_a_stale_consumption() -> None:
    store, _, design, _ = chain()
    store.record_production(artifact("design.md", "two flask routes after review", "design"))

    diagram = store.to_mermaid()
    assert f"a_design_md -. stale {design.sha[:8]} .-> n_implement" in diagram


def test_to_mermaid_sanitizes_ids_but_keeps_labels_readable() -> None:
    store = LineageStore()
    store.record_consumption("implement.api", artifact("src/app.py", "code", "design-2"))

    diagram = store.to_mermaid()
    assert "a_src_app_py" in diagram
    assert "n_design_2" in diagram
    assert "n_implement_api" in diagram
    assert '"src/app.py<br/>' in diagram


def test_to_mermaid_draws_one_edge_per_relationship() -> None:
    store, spec, _, _ = chain()
    store.record_consumption("design", spec)
    store.record_consumption("design", spec)

    diagram = store.to_mermaid()
    assert diagram.count("a_spec_md --> n_design") == 1


def test_to_mermaid_of_an_empty_store_is_still_valid() -> None:
    diagram = LineageStore().to_mermaid()
    assert diagram.splitlines() == ["%% keel lineage, 0 artifacts, 0 edges", "graph LR"]


def test_repr_summarizes_the_store() -> None:
    store, _, _, _ = chain()
    assert repr(store) == "LineageStore(artifacts=3, edges=2)"
