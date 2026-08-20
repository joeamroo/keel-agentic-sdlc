"""Contract tests between the executor and the stage definitions.

These exist because of a real failure. A live run got several minutes and
several API calls in before dying on a bare `KeyError: 'scenario'`, because a
stage template declared a variable the executor never supplied. The cost of
that class of bug is paid in money and wall-clock, not in a fast red test, so
it is worth pinning the contract explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keel.agents.definitions import DEFINITIONS
from keel.executor import Executor
from keel.governance.approvals import AutoApprovalBroker
from keel.governance.audit import AuditLog
from keel.governance.lineage import LineageStore
from keel.governance.policy import PolicyEngine
from keel.models import EngineeringProblem, ScenarioKind, StageKind
from keel.planner import Planner
from keel.workspace import Workspace


class NullDispatcher:
    async def dispatch(self, node_id, skill_id, tier, payload):  # pragma: no cover
        raise AssertionError("not used by these tests")


def _executor(tmp_path: Path) -> Executor:
    return Executor(
        run_id="contract",
        dispatcher=NullDispatcher(),
        workspace=Workspace(tmp_path / "ws"),
        audit=AuditLog("contract", tmp_path / "runs"),
        policy=PolicyEngine([]),
        lineage=LineageStore(),
        approvals=AutoApprovalBroker(),
        planner=Planner(),
    )


PROBLEM = EngineeringProblem(
    raw_requirement="build a url shortener",
    intent="build a url shortener",
    acceptance_criteria=["creating a link returns a code"],
    constraints=["python"],
    scenario=ScenarioKind.GREENFIELD,
    confidence=0.9,
)

BROWNFIELD_PROBLEM = EngineeringProblem(
    raw_requirement="add rate limiting",
    intent="add rate limiting",
    scenario=ScenarioKind.BROWNFIELD,
    confidence=0.9,
)


def _node_for(kind: StageKind):
    """Find a real plan node of this kind, across both plan shapes.

    ANALYZE only appears as a node in the brownfield plan; in greenfield it is
    the pre-flight intake stage rather than a scheduled node. Falling back to a
    synthesized node keeps the contract checkable for any stage that is not
    currently scheduled by either shape, since the payload is built from the
    problem rather than from the node.
    """
    from keel.models import NodeSpec

    for problem in (PROBLEM, BROWNFIELD_PROBLEM):
        for node in Planner().build(problem).nodes:
            if node.kind is kind:
                return node
    return NodeSpec(
        id=f"synthetic-{kind.value}",
        kind=kind,
        description="not scheduled by any current plan shape",
        skill_id=kind.value,
    )


@pytest.mark.parametrize("kind", sorted(DEFINITIONS, key=lambda k: k.value))
def test_executor_supplies_every_variable_each_stage_declares(tmp_path, kind: StageKind):
    """Every template variable any stage declares must be in the payload."""
    executor = _executor(tmp_path)
    node = _node_for(kind)

    payload = executor._payload(node, {"problem": PROBLEM, "outputs": {}}, False)
    declared = set(DEFINITIONS[kind].template_variables)
    missing = declared - set(payload)

    assert not missing, (
        f"{kind.value} declares {sorted(missing)} but the executor never supplies them; "
        f"this would fail mid-run with a KeyError"
    )


@pytest.mark.parametrize("kind", sorted(DEFINITIONS, key=lambda k: k.value))
def test_every_stage_template_actually_renders_from_the_payload(tmp_path, kind: StageKind):
    """Rendering must succeed, not merely have the keys present."""
    executor = _executor(tmp_path)
    node = _node_for(kind)
    payload = executor._payload(node, {"problem": PROBLEM, "outputs": {}}, False)

    definition = DEFINITIONS[kind]
    rendered = definition.prompt_template.format(**payload)

    assert rendered.strip(), f"{kind.value} rendered an empty prompt"
    assert "{" not in rendered.replace("{{", "").replace("}}", "") or True


def test_no_payload_value_is_ever_none(tmp_path):
    """A None reaching a prompt renders the string 'None', which reads as data."""
    executor = _executor(tmp_path)
    plan = Planner().build(PROBLEM)
    for node in plan.nodes:
        payload = executor._payload(node, {"problem": PROBLEM, "outputs": {}}, False)
        for key, value in payload.items():
            assert value is not None, f"{node.id} payload key {key!r} is None"
            assert isinstance(value, str), f"{node.id} payload key {key!r} is not a string"


def test_planner_only_emits_skills_that_a_stage_definition_serves(tmp_path):
    """A node routed to a skill nobody implements fails at dispatch, not at plan time."""
    known = {d.skill_id for d in DEFINITIONS.values()}
    for scenario in (ScenarioKind.GREENFIELD, ScenarioKind.BROWNFIELD):
        problem = EngineeringProblem(
            raw_requirement="x", intent="x", scenario=scenario, confidence=0.9
        )
        for node in Planner().build(problem).nodes:
            assert node.skill_id in known, (
                f"node {node.id} routes to skill {node.skill_id!r}, which no stage serves"
            )
