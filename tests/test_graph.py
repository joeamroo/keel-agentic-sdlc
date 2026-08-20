"""Tests for `keel.graph`.

Plans are built inline rather than through a fixture factory module, so each
test reads as the shape it is actually about.
"""

from __future__ import annotations

import pytest

from keel.graph import GraphError, PlanGraph
from keel.models import ImpactLevel, ModelTier, NodeSpec, Plan, StageKind


def node(
    node_id: str,
    kind: StageKind = StageKind.IMPLEMENT,
    depends_on: list[str] | None = None,
    **kwargs: object,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        kind=kind,
        description=f"{kind.value} {node_id}",
        depends_on=list(depends_on or []),
        **kwargs,  # type: ignore[arg-type]
    )


def ids(nodes: list[NodeSpec]) -> list[str]:
    return [n.id for n in nodes]


def level_ids(graph: PlanGraph) -> list[list[str]]:
    return [ids(level) for level in graph.levels()]


def linear_plan() -> Plan:
    """analyze -> design -> implement -> test, one node per stage."""
    return Plan(
        nodes=[
            node("analyze", StageKind.ANALYZE),
            node("design", StageKind.DESIGN, ["analyze"]),
            node("implement", StageKind.IMPLEMENT, ["design"]),
            node("verify", StageKind.TEST, ["implement"]),
        ],
        version=3,
        rationale="straight line",
    )


def diamond_plan() -> Plan:
    """One fan out, two independent branches, one join.

    The join is the interesting part: it is the synchronization barrier that
    both branches have to reach before anything downstream may run.
    """
    return Plan(
        nodes=[
            node("design", StageKind.DESIGN),
            node("impl_api", StageKind.IMPLEMENT, ["design"]),
            node("impl_ui", StageKind.IMPLEMENT, ["design"]),
            node("integrate", StageKind.TEST, ["impl_api", "impl_ui"]),
        ]
    )


# ----------------------------------------------------------------------
# Construction and validation
# ----------------------------------------------------------------------


def test_linear_plan_builds_and_exposes_structure() -> None:
    graph = PlanGraph(linear_plan())

    assert len(graph) == 4
    assert "design" in graph
    assert "nope" not in graph
    assert graph.node_ids == ("analyze", "design", "implement", "verify")
    assert graph.roots == ("analyze",)
    assert graph.leaves == ("verify",)
    assert graph.node("design").kind is StageKind.DESIGN
    assert graph.dependencies("design") == ("analyze",)
    assert graph.dependents("design") == ("implement",)
    assert graph.plan.version == 3


def test_empty_plan_is_legal_and_answers_everything_emptily() -> None:
    graph = PlanGraph(Plan(nodes=[]))

    assert len(graph) == 0
    assert graph.levels() == []
    assert graph.ready(set()) == []
    assert graph.critical_path() == []
    assert graph.roots == ()
    assert graph.to_mermaid().splitlines()[-1] == "graph TD"


def test_duplicate_node_ids_are_rejected() -> None:
    plan = Plan(nodes=[node("build"), node("build", StageKind.TEST)])

    with pytest.raises(GraphError, match="duplicate node id: 'build'"):
        PlanGraph(plan)


def test_unknown_dependency_is_rejected_and_names_both_ends() -> None:
    plan = Plan(nodes=[node("design"), node("implement", depends_on=["desgin"])])

    with pytest.raises(GraphError) as excinfo:
        PlanGraph(plan)

    message = str(excinfo.value)
    assert "implement" in message
    assert "desgin" in message


def test_graph_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        PlanGraph(Plan(nodes=[node("a", depends_on=["ghost"])]))


def test_unknown_node_lookups_raise_rather_than_key_error() -> None:
    graph = PlanGraph(linear_plan())

    for call in (graph.node, graph.dependencies, graph.dependents, graph.descendants):
        with pytest.raises(GraphError, match="no such node"):
            call("ghost")


def test_repeated_dependency_is_tolerated_and_deduplicated() -> None:
    plan = Plan(nodes=[node("a"), node("b", depends_on=["a", "a"])])
    graph = PlanGraph(plan)

    assert graph.dependencies("b") == ("a",)
    assert graph.dependents("a") == ("b",)
    assert level_ids(graph) == [["a"], ["b"]]


# ----------------------------------------------------------------------
# Cycles
# ----------------------------------------------------------------------


def test_two_node_cycle_is_reported_with_its_path() -> None:
    plan = Plan(nodes=[node("a", depends_on=["b"]), node("b", depends_on=["a"])])

    with pytest.raises(GraphError) as excinfo:
        PlanGraph(plan)

    message = str(excinfo.value)
    assert message.startswith("cycle detected: ")
    path = message.removeprefix("cycle detected: ").split(" -> ")
    assert path[0] == path[-1]
    assert set(path) == {"a", "b"}


def test_three_node_cycle_reports_every_member_in_execution_order() -> None:
    plan = Plan(
        nodes=[
            node("a", depends_on=["c"]),
            node("b", depends_on=["a"]),
            node("c", depends_on=["b"]),
        ]
    )

    with pytest.raises(GraphError) as excinfo:
        PlanGraph(plan)

    path = str(excinfo.value).removeprefix("cycle detected: ").split(" -> ")
    assert len(path) == 4
    assert path[0] == path[-1]
    assert set(path) == {"a", "b", "c"}
    # Consecutive pairs must be real edges: the successor depends on the
    # predecessor. That is what makes the path copy-pasteable into a fix.
    by_id = {n.id: n for n in plan.nodes}
    # noqa-worthy on purpose: a pairwise window has one fewer pair than items,
    # so strict=True would be wrong here.
    for before, after in zip(path, path[1:]):  # noqa: B905
        assert before in by_id[after].depends_on


def test_self_dependency_is_a_cycle() -> None:
    plan = Plan(nodes=[node("solo", depends_on=["solo"])])

    with pytest.raises(GraphError, match="cycle detected: solo -> solo"):
        PlanGraph(plan)


def test_cycle_is_found_even_when_it_is_not_reachable_from_the_first_node() -> None:
    plan = Plan(
        nodes=[
            node("clean"),
            node("x", depends_on=["y"]),
            node("y", depends_on=["x"]),
        ]
    )

    with pytest.raises(GraphError, match="cycle detected"):
        PlanGraph(plan)


def test_deep_chain_does_not_blow_the_stack() -> None:
    # Cycle detection is iterative on purpose; a generated plan can be long.
    nodes = [node("n0")] + [node(f"n{i}", depends_on=[f"n{i - 1}"]) for i in range(1, 3000)]
    graph = PlanGraph(Plan(nodes=nodes))

    assert len(graph.levels()) == 3000
    assert len(graph.critical_path()) == 3000


# ----------------------------------------------------------------------
# Levels and readiness
# ----------------------------------------------------------------------


def test_linear_plan_has_one_node_per_level() -> None:
    graph = PlanGraph(linear_plan())

    assert level_ids(graph) == [["analyze"], ["design"], ["implement"], ["verify"]]


def test_diamond_puts_independent_branches_in_one_level() -> None:
    graph = PlanGraph(diamond_plan())

    assert level_ids(graph) == [["design"], ["impl_api", "impl_ui"], ["integrate"]]


def test_every_level_is_satisfied_by_the_levels_before_it() -> None:
    plan = Plan(
        nodes=[
            node("a", StageKind.ANALYZE),
            node("b", StageKind.DESIGN, ["a"]),
            node("c", StageKind.DESIGN, ["a"]),
            node("d", StageKind.IMPLEMENT, ["b"]),
            node("e", StageKind.IMPLEMENT, ["b", "c"]),
            node("f", StageKind.REVIEW, ["d", "e"]),
            node("standalone", StageKind.DOCUMENT),
        ]
    )
    graph = PlanGraph(plan)

    seen: set[str] = set()
    for level in graph.levels():
        for spec in level:
            assert set(spec.depends_on) <= seen, f"{spec.id} ran before its dependencies"
        seen.update(ids(level))

    assert seen == plan.node_ids
    # An unconnected node has nothing to wait for, so it belongs in level 0.
    assert "standalone" in level_ids(graph)[0]


def test_levels_return_the_plans_own_node_objects() -> None:
    plan = diamond_plan()
    graph = PlanGraph(plan)

    assert graph.levels()[0][0] is plan.by_id("design")


def test_levels_are_stable_and_the_returned_lists_are_disposable() -> None:
    graph = PlanGraph(diamond_plan())

    first = graph.levels()
    first[0].clear()

    assert level_ids(graph) == [["design"], ["impl_api", "impl_ui"], ["integrate"]]


def test_ready_walks_the_diamond_to_completion() -> None:
    graph = PlanGraph(diamond_plan())

    assert ids(graph.ready(set())) == ["design"]
    assert ids(graph.ready({"design"})) == ["impl_api", "impl_ui"]
    # The join waits for both branches, which is the barrier.
    assert ids(graph.ready({"design", "impl_api"})) == ["impl_ui"]
    assert ids(graph.ready({"design", "impl_api", "impl_ui"})) == ["integrate"]
    assert graph.ready({"design", "impl_api", "impl_ui", "integrate"}) == []


def test_ready_excludes_nodes_that_are_already_completed() -> None:
    graph = PlanGraph(linear_plan())

    assert ids(graph.ready({"analyze", "design"})) == ["implement"]


def test_ready_rejects_completed_ids_that_are_not_in_the_plan() -> None:
    graph = PlanGraph(linear_plan())

    with pytest.raises(GraphError, match="stale_node"):
        graph.ready({"analyze", "stale_node"})


def test_ready_agrees_with_levels_when_progress_is_lockstep() -> None:
    graph = PlanGraph(diamond_plan())

    completed: set[str] = set()
    for level in graph.levels():
        assert ids(graph.ready(completed)) == ids(level)
        completed.update(ids(level))


# ----------------------------------------------------------------------
# Reachability and staleness
# ----------------------------------------------------------------------


def test_descendants_and_ancestors_are_transitive_and_exclude_the_node() -> None:
    graph = PlanGraph(linear_plan())

    assert graph.descendants("design") == {"implement", "verify"}
    assert graph.ancestors("implement") == {"analyze", "design"}
    assert graph.descendants("verify") == set()
    assert graph.ancestors("analyze") == set()


def test_descendants_only_follow_real_edges_in_a_diamond() -> None:
    graph = PlanGraph(diamond_plan())

    assert graph.descendants("design") == {"impl_api", "impl_ui", "integrate"}
    assert graph.descendants("impl_api") == {"integrate"}
    assert graph.ancestors("integrate") == {"design", "impl_api", "impl_ui"}
    # Sibling branches are independent, which is why they can run together.
    assert "impl_ui" not in graph.descendants("impl_api")
    assert "impl_ui" not in graph.ancestors("impl_api")


def test_mark_stale_includes_the_changed_node_and_everything_downstream() -> None:
    graph = PlanGraph(diamond_plan())

    assert graph.mark_stale("impl_api") == {"impl_api", "integrate"}
    assert graph.mark_stale("design") == set(graph.plan.node_ids)


def test_mark_stale_on_a_leaf_touches_only_that_node() -> None:
    graph = PlanGraph(diamond_plan())

    assert graph.mark_stale("integrate") == {"integrate"}


def test_stale_set_is_a_fresh_mutable_copy_each_call() -> None:
    graph = PlanGraph(diamond_plan())

    first = graph.mark_stale("impl_api")
    first.add("scribble")

    assert graph.mark_stale("impl_api") == {"impl_api", "integrate"}


# ----------------------------------------------------------------------
# Subgraph extraction
# ----------------------------------------------------------------------


def test_subgraph_keeps_internal_edges_and_drops_external_ones() -> None:
    graph = PlanGraph(diamond_plan())

    sub = graph.subgraph({"impl_api", "impl_ui", "integrate"})

    assert [n.id for n in sub.nodes] == ["impl_api", "impl_ui", "integrate"]
    # design is outside the set, so the branches become roots of the sub-plan.
    assert sub.by_id("impl_api").depends_on == []
    assert sub.by_id("integrate").depends_on == ["impl_api", "impl_ui"]
    assert PlanGraph(sub).roots == ("impl_api", "impl_ui")


def test_subgraph_preserves_node_payload_and_plan_metadata() -> None:
    plan = Plan(
        nodes=[
            node("a", StageKind.ANALYZE),
            node(
                "b",
                StageKind.RELEASE_CHECK,
                ["a"],
                skill_id="release.check",
                model_tier=ModelTier.FAST,
                impact=ImpactLevel.HIGH,
                exit_rules=["no criticals"],
                produces=["release_report.md"],
            ),
        ],
        version=7,
        rationale="why this shape",
        supersedes=6,
    )
    graph = PlanGraph(plan)

    sub = graph.subgraph({"b"})
    extracted = sub.by_id("b")

    assert extracted.kind is StageKind.RELEASE_CHECK
    assert extracted.skill_id == "release.check"
    assert extracted.model_tier is ModelTier.FAST
    assert extracted.needs_approval is True
    assert extracted.exit_rules == ["no criticals"]
    assert extracted.produces == ["release_report.md"]
    assert extracted.retry.max_attempts == plan.by_id("b").retry.max_attempts
    assert (sub.version, sub.rationale, sub.supersedes) == (7, "why this shape", 6)


def test_subgraph_nodes_are_copies_so_repairs_cannot_leak_upward() -> None:
    plan = diamond_plan()
    graph = PlanGraph(plan)

    sub = graph.subgraph({"impl_api", "integrate"})
    sub.by_id("integrate").depends_on.append("something_new")
    sub.by_id("impl_api").produces.append("patch.diff")

    assert plan.by_id("integrate").depends_on == ["impl_api", "impl_ui"]
    assert plan.by_id("impl_api").produces == []


def test_subgraph_of_a_stale_set_is_a_runnable_plan() -> None:
    graph = PlanGraph(diamond_plan())

    sub = graph.subgraph(graph.mark_stale("impl_ui"))
    sub_graph = PlanGraph(sub)

    assert [ids(level) for level in sub_graph.levels()] == [["impl_ui"], ["integrate"]]


def test_subgraph_of_everything_round_trips() -> None:
    graph = PlanGraph(diamond_plan())

    sub = graph.subgraph(set(graph.node_ids))

    assert level_ids(PlanGraph(sub)) == level_ids(graph)


def test_subgraph_of_nothing_is_an_empty_plan() -> None:
    graph = PlanGraph(diamond_plan())

    assert graph.subgraph(set()).nodes == []


def test_subgraph_rejects_ids_outside_the_plan() -> None:
    graph = PlanGraph(diamond_plan())

    with pytest.raises(GraphError, match="ghost"):
        graph.subgraph({"design", "ghost"})


# ----------------------------------------------------------------------
# Critical path
# ----------------------------------------------------------------------


def test_critical_path_of_a_linear_plan_is_the_whole_plan() -> None:
    graph = PlanGraph(linear_plan())

    assert graph.critical_path() == ["analyze", "design", "implement", "verify"]


def test_critical_path_picks_the_longest_chain_not_the_widest_level() -> None:
    plan = Plan(
        nodes=[
            node("start", StageKind.ANALYZE),
            node("short", StageKind.DOCUMENT, ["start"]),
            node("long_a", StageKind.IMPLEMENT, ["start"]),
            node("long_b", StageKind.IMPLEMENT, ["long_a"]),
            node("finish", StageKind.REVIEW, ["short", "long_b"]),
        ]
    )
    graph = PlanGraph(plan)

    assert graph.critical_path() == ["start", "long_a", "long_b", "finish"]


def test_critical_path_of_a_diamond_breaks_ties_toward_declaration_order() -> None:
    graph = PlanGraph(diamond_plan())

    assert graph.critical_path() == ["design", "impl_api", "integrate"]


def test_critical_path_length_matches_the_level_count() -> None:
    graph = PlanGraph(diamond_plan())

    assert len(graph.critical_path()) == len(graph.levels())


def test_critical_path_across_disconnected_components() -> None:
    plan = Plan(
        nodes=[
            node("lonely", StageKind.DOCUMENT),
            node("a", StageKind.ANALYZE),
            node("b", StageKind.DESIGN, ["a"]),
        ]
    )
    graph = PlanGraph(plan)

    assert graph.critical_path() == ["a", "b"]


# ----------------------------------------------------------------------
# Mermaid
# ----------------------------------------------------------------------


def test_mermaid_declares_every_node_with_its_id_and_stage_kind() -> None:
    graph = PlanGraph(diamond_plan())

    source = graph.to_mermaid()
    lines = source.splitlines()

    assert lines[0].startswith("%% keel plan v1, 4 nodes")
    assert lines[1] == "graph TD"
    assert '    design["design<br/>design"]' in lines
    assert '    impl_api["impl_api<br/>implement"]' in lines
    assert '    integrate["integrate<br/>test"]' in lines


def test_mermaid_draws_one_edge_per_dependency_in_execution_direction() -> None:
    graph = PlanGraph(diamond_plan())

    edges = [line.strip() for line in graph.to_mermaid().splitlines() if "-->" in line]

    assert edges == [
        "design --> impl_api",
        "design --> impl_ui",
        "impl_api --> integrate",
        "impl_ui --> integrate",
    ]


def test_mermaid_has_no_edges_when_nothing_depends_on_anything() -> None:
    graph = PlanGraph(Plan(nodes=[node("a"), node("b")]))

    assert "-->" not in graph.to_mermaid()
    assert len(graph.to_mermaid().splitlines()) == 4


def test_mermaid_sanitizes_ids_that_would_break_the_parser() -> None:
    plan = Plan(
        nodes=[
            node("stage.one", StageKind.ANALYZE),
            node("stage one", StageKind.DESIGN, ["stage.one"]),
        ]
    )
    graph = PlanGraph(plan)
    lines = graph.to_mermaid().splitlines()

    assert '    stage_one["stage.one<br/>analyze"]' in lines
    # Two different ids that sanitize the same way must not collapse into one.
    assert '    stage_one_1["stage one<br/>design"]' in lines
    assert "    stage_one --> stage_one_1" in lines


def test_mermaid_escapes_label_characters_that_end_a_label_early() -> None:
    graph = PlanGraph(Plan(nodes=[node('weird"[id]<x>', StageKind.REVIEW)]))

    line = graph.to_mermaid().splitlines()[-1]
    mermaid_id, _, rest = line.strip().partition('["')
    label = rest.removesuffix('"]')

    assert mermaid_id.isidentifier()
    # A raw quote or bracket would terminate the label and corrupt the diagram.
    assert '"' not in label
    assert "[" not in label and "]" not in label
    assert label == "weird#quot;#91;id#93;#lt;x#gt;<br/>review"


def test_mermaid_output_is_deterministic() -> None:
    plan = diamond_plan()

    assert PlanGraph(plan).to_mermaid() == PlanGraph(diamond_plan()).to_mermaid()
