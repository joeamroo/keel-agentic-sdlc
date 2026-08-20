"""Tests for the stage agent definitions.

These are contract tests, not prompt quality tests. They pin the things that
break other modules silently: a stage with no definition, a schema the model
provider will reject as non strict, a template variable the dispatcher does not
supply, and a skill id that does not route.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import pytest

from keel.agents.definitions import (
    DEFINITIONS,
    MissingTemplateVariable,
    StageDefinition,
    definition_for,
    render,
)
from keel.models import EngineeringProblem, ModelTier, StageKind

ALL_STAGES = list(StageKind)

# Plausible dispatcher payloads. ANALYZE's keys are fixed by keel/dispatch.py,
# which calls prompt_template.format(**payload) with exactly these three.
SAMPLE_PAYLOADS: dict[StageKind, dict[str, str]] = {
    StageKind.ANALYZE: {
        "requirement": "Build a URL shortener with click analytics and link expiry.",
        "existing_code": "",
        "prior_answers": "Q: Should codes be guessable? A: No.",
    },
    StageKind.DESIGN: {
        "intent": "Shorten a URL and redirect visitors to it while counting clicks.",
        "acceptance_criteria": "- POST /links returns a short code\n- GET /{code} redirects",
        "constraints": "FastAPI, SQLite, single node deployment",
        "scenario": "greenfield",
    },
    StageKind.IMPLEMENT: {
        "intent": "Shorten a URL and redirect visitors to it while counting clicks.",
        "design": '{"endpoints": [{"method": "POST", "path": "/links"}]}',
        "acceptance_criteria": "- POST /links returns a short code",
        "constraints": "FastAPI, SQLite",
    },
    StageKind.TEST: {
        "design": '{"redirect_status_code": {"code": 307}}',
        "implementation": "app/main.py:\nfrom fastapi import FastAPI\napp = FastAPI()\n",
        "acceptance_criteria": "- GET /{code} redirects with 307",
    },
    StageKind.DOCUMENT: {
        "intent": "Shorten a URL and redirect visitors to it.",
        "design": '{"endpoints": []}',
        "implementation": "app/main.py:\napp = FastAPI()\n",
    },
    StageKind.REVIEW: {
        "artifacts": "app/main.py\ntests/test_links.py\nREADME.md",
        "acceptance_criteria": "- Private hosts are rejected",
        "constraints": "FastAPI, SQLite",
    },
    StageKind.RELEASE_CHECK: {
        "acceptance_criteria": "- Private hosts are rejected",
        "artifacts": "app/main.py\ntests/test_links.py\nREADME.md",
        "review_findings": '[{"severity": "low", "summary": "missing docstring"}]',
    },
}

EXPECTED_TIERS: dict[StageKind, ModelTier] = {
    StageKind.ANALYZE: ModelTier.DEEP,
    StageKind.DESIGN: ModelTier.DEEP,
    StageKind.IMPLEMENT: ModelTier.DEEP,
    StageKind.TEST: ModelTier.DEEP,
    StageKind.DOCUMENT: ModelTier.FAST,
    StageKind.REVIEW: ModelTier.DEEP,
    StageKind.RELEASE_CHECK: ModelTier.FAST,
}


# --------------------------------------------------------------------------
# Coverage of the enum
# --------------------------------------------------------------------------


def test_every_stage_kind_has_a_definition() -> None:
    assert set(DEFINITIONS) == set(StageKind)
    assert len(DEFINITIONS) == len(StageKind)


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_definition_kind_matches_its_key(kind: StageKind) -> None:
    defn = DEFINITIONS[kind]
    assert isinstance(defn, StageDefinition)
    assert defn.kind is kind


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_definition_text_fields_are_populated(kind: StageKind) -> None:
    defn = DEFINITIONS[kind]
    assert defn.title.strip()
    assert len(defn.description.strip()) > 80, "Agent Card descriptions must say when to use"
    assert len(defn.system_prompt.strip()) > 200
    assert defn.prompt_template.strip()
    assert defn.produces, "a stage that produces nothing cannot be gated"
    assert all(name.strip() for name in defn.produces)


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_model_tier_is_as_assigned(kind: StageKind) -> None:
    assert DEFINITIONS[kind].model_tier is EXPECTED_TIERS[kind]


# --------------------------------------------------------------------------
# Skill ids
# --------------------------------------------------------------------------


def test_skill_ids_are_unique() -> None:
    skill_ids = [d.skill_id for d in DEFINITIONS.values()]
    assert len(set(skill_ids)) == len(skill_ids)


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_skill_id_matches_stage_kind_value(kind: StageKind) -> None:
    assert DEFINITIONS[kind].skill_id == kind.value


def test_definition_for_accepts_enum_and_raw_value() -> None:
    assert definition_for(StageKind.REVIEW) is DEFINITIONS[StageKind.REVIEW]
    assert definition_for("review") is DEFINITIONS[StageKind.REVIEW]


def test_definition_for_rejects_unknown_stage() -> None:
    with pytest.raises(KeyError) as exc:
        definition_for("deploy")
    assert "deploy" in str(exc.value)
    assert "analyze" in str(exc.value), "the error should list the valid stages"


# --------------------------------------------------------------------------
# Schema strictness
# --------------------------------------------------------------------------


def _object_schemas(schema: dict[str, Any], path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    """Every object schema reachable from `schema`, with a path for messages."""
    found: list[tuple[str, dict[str, Any]]] = []
    if schema.get("type") == "object":
        found.append((path, schema))
        for name, sub in schema.get("properties", {}).items():
            found.extend(_object_schemas(sub, f"{path}.{name}"))
    elif schema.get("type") == "array":
        items = schema.get("items")
        assert isinstance(items, dict), f"{path}: array schema needs an items schema"
        found.extend(_object_schemas(items, f"{path}[]"))
    return found


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_json_schema_is_a_strict_object_schema(kind: StageKind) -> None:
    schema = DEFINITIONS[kind].json_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"], "an empty required list makes every field optional"
    assert isinstance(schema["properties"], dict) and schema["properties"]


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_every_required_key_exists_in_properties(kind: StageKind) -> None:
    schema = DEFINITIONS[kind].json_schema
    for path, obj in _object_schemas(schema):
        properties = obj.get("properties", {})
        for key in obj.get("required", []):
            assert key in properties, f"{kind.value} {path}: required '{key}' has no property"


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_nested_objects_are_strict_too(kind: StageKind) -> None:
    """A nested object without additionalProperties false is an unchecked hole."""
    for path, obj in _object_schemas(DEFINITIONS[kind].json_schema):
        assert obj.get("additionalProperties") is False, f"{kind.value} {path} is not strict"
        assert obj.get("required"), f"{kind.value} {path} has no required keys"


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_schema_types_are_json_types(kind: StageKind) -> None:
    valid = {"object", "array", "string", "number", "integer", "boolean", "null"}

    def walk(node: dict[str, Any], path: str) -> None:
        assert node.get("type") in valid, f"{kind.value} {path}: bad type {node.get('type')!r}"
        if node["type"] == "object":
            for name, sub in node.get("properties", {}).items():
                walk(sub, f"{path}.{name}")
        elif node["type"] == "array":
            walk(node["items"], f"{path}[]")

    walk(DEFINITIONS[kind].json_schema, "$")


# --------------------------------------------------------------------------
# ANALYZE mirrors the frozen EngineeringProblem contract
# --------------------------------------------------------------------------


def test_analyze_schema_covers_every_engineering_problem_field() -> None:
    properties = DEFINITIONS[StageKind.ANALYZE].json_schema["properties"]
    for field in dataclass_fields(EngineeringProblem):
        assert field.name in properties, f"EngineeringProblem.{field.name} is unreachable"


def test_analyze_schema_requires_the_fields_the_model_must_produce() -> None:
    schema = DEFINITIONS[StageKind.ANALYZE].json_schema
    required = set(schema["required"])
    assert required == {
        "intent",
        "acceptance_criteria",
        "constraints",
        "ambiguities",
        "scenario",
        "confidence",
        "notes",
    }
    # raw_requirement is supplied by the orchestrator, so it stays optional.
    assert "raw_requirement" not in required


def test_analyze_scenario_enum_matches_scenario_kind() -> None:
    from keel.models import ScenarioKind

    scenario = DEFINITIONS[StageKind.ANALYZE].json_schema["properties"]["scenario"]
    assert set(scenario["enum"]) == {s.value for s in ScenarioKind}


def test_analyze_confidence_is_bounded_zero_to_one() -> None:
    confidence = DEFINITIONS[StageKind.ANALYZE].json_schema["properties"]["confidence"]
    assert confidence["type"] == "number"
    assert confidence["minimum"] == 0.0
    assert confidence["maximum"] == 1.0


def test_analyze_ambiguity_item_mirrors_the_ambiguity_dataclass() -> None:
    item = DEFINITIONS[StageKind.ANALYZE].json_schema["properties"]["ambiguities"]["items"]
    assert set(item["required"]) == {"question", "why_it_matters", "blocking", "options"}
    assert item["properties"]["blocking"]["type"] == "boolean"
    assert item["properties"]["options"]["type"] == "array"


def test_review_severity_enum_matches_the_severity_enum() -> None:
    from keel.models import Severity

    finding = DEFINITIONS[StageKind.REVIEW].json_schema["properties"]["findings"]["items"]
    assert set(finding["properties"]["severity"]["enum"]) == {s.value for s in Severity}


def test_review_verdict_has_the_three_documented_values() -> None:
    verdict = DEFINITIONS[StageKind.REVIEW].json_schema["properties"]["verdict"]
    assert set(verdict["enum"]) == {"approve", "approve_with_findings", "reject"}


def test_release_check_shape() -> None:
    schema = DEFINITIONS[StageKind.RELEASE_CHECK].json_schema
    assert schema["properties"]["ready"]["type"] == "boolean"
    item = schema["properties"]["checklist"]["items"]
    assert set(item["required"]) == {"item", "passed"}


@pytest.mark.parametrize(
    "kind", [StageKind.IMPLEMENT, StageKind.TEST, StageKind.DOCUMENT], ids=lambda k: k.value
)
def test_file_producing_stages_return_path_and_content(kind: StageKind) -> None:
    item = DEFINITIONS[kind].json_schema["properties"]["files"]["items"]
    assert set(item["required"]) == {"path", "content"}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_analyze_template_uses_exactly_the_dispatcher_keys() -> None:
    """keel/dispatch.py calls format() with these three and nothing else."""
    assert DEFINITIONS[StageKind.ANALYZE].template_variables == (
        "requirement",
        "existing_code",
        "prior_answers",
    )


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_sample_payload_matches_declared_template_variables(kind: StageKind) -> None:
    assert set(SAMPLE_PAYLOADS[kind]) == set(DEFINITIONS[kind].template_variables)


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_render_returns_system_and_filled_prompt(kind: StageKind) -> None:
    payload = SAMPLE_PAYLOADS[kind]
    system, prompt = render(kind, **payload)

    assert system == DEFINITIONS[kind].system_prompt
    for name in DEFINITIONS[kind].template_variables:
        placeholder = "{" + name + "}"
        assert placeholder not in prompt, f"{placeholder} was left unfilled"
    for value in payload.values():
        if value:
            assert value in prompt


def test_render_accepts_a_raw_stage_value() -> None:
    system, prompt = render("analyze", **SAMPLE_PAYLOADS[StageKind.ANALYZE])
    assert system == DEFINITIONS[StageKind.ANALYZE].system_prompt
    assert "URL shortener" in prompt


def test_render_ignores_extra_keys() -> None:
    payload = dict(SAMPLE_PAYLOADS[StageKind.DOCUMENT], unused="ignored")
    _, prompt = render(StageKind.DOCUMENT, **payload)
    assert "ignored" not in prompt


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_render_raises_on_a_missing_variable(kind: StageKind) -> None:
    payload = dict(SAMPLE_PAYLOADS[kind])
    dropped = DEFINITIONS[kind].template_variables[0]
    payload.pop(dropped)

    with pytest.raises(MissingTemplateVariable) as exc:
        render(kind, **payload)

    message = str(exc.value)
    assert dropped in message
    assert kind.value in message


def test_missing_template_variable_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        render(StageKind.ANALYZE, requirement="only one of three")


# --------------------------------------------------------------------------
# House rules the prompts themselves have to follow
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_STAGES, ids=lambda k: k.value)
def test_prompts_ask_for_schema_conforming_json(kind: StageKind) -> None:
    assert "json" in DEFINITIONS[kind].system_prompt.lower()


def test_no_em_dashes_anywhere_in_the_module() -> None:
    """House rule, and an em dash in a prompt survives into generated docs."""
    source = Path(__file__).resolve().parents[1] / "keel" / "agents" / "definitions.py"
    text = source.read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text


def test_implement_prompt_bans_the_dangerous_builtins() -> None:
    system = DEFINITIONS[StageKind.IMPLEMENT].system_prompt
    for banned in ("eval", "exec", "os.system", "shell=True"):
        assert banned in system, f"{banned} is not forbidden in the implement prompt"


def test_implement_prompt_names_the_metadata_address() -> None:
    assert "169.254.169.254" in DEFINITIONS[StageKind.IMPLEMENT].system_prompt
    assert "169.254.169.254" in DEFINITIONS[StageKind.TEST].system_prompt


def test_design_prompt_forces_the_redirect_and_short_code_decisions() -> None:
    system = DEFINITIONS[StageKind.DESIGN].system_prompt
    assert "301" in system and "302" in system and "307" in system
    assert "base62" in system.lower()
    schema_properties = DEFINITIONS[StageKind.DESIGN].json_schema["properties"]
    assert "redirect_status_code" in schema_properties
    assert "short_code_strategy" in schema_properties


def test_review_prompt_forbids_self_filtering() -> None:
    system = DEFINITIONS[StageKind.REVIEW].system_prompt.lower()
    assert "do not filter" in system
    assert "confidence" in system
