"""Turning an English sentence into a problem worth planning.

Intake is deliberately not a node in the plan graph. You cannot schedule work
until you know what the work is, so understanding the requirement has to happen
before the graph that would otherwise contain it exists. It runs as a pre-flight
stage through the same dispatcher every other stage uses.

The decision this module exists to make is not "what should we build". It is
"do we know enough to build anything at all". Answering no is a success, not a
failure: an agent that guesses at an underspecified requirement produces
confident, plausible, wrong work, which is more expensive than stopping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keel.dispatch import StageDispatcher
from keel.models import (
    Ambiguity,
    EngineeringProblem,
    ModelTier,
    ScenarioKind,
    StageKind,
    TaskState,
)

# Below this, we refuse to plan and ask a human instead. Tuned so that a
# well-specified greenfield requirement clears it comfortably and a one-line
# "make it better" style request does not.
DEFAULT_CONFIDENCE_THRESHOLD = 0.6


@dataclass(slots=True)
class IntakeResult:
    problem: EngineeringProblem
    state: TaskState
    questions: list[Ambiguity]
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def parked(self) -> bool:
        """True when a human has to answer before any code may be written."""
        return self.state is TaskState.INPUT_REQUIRED


class Intake:
    def __init__(
        self,
        dispatcher: StageDispatcher,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ):
        self.dispatcher = dispatcher
        self.threshold = threshold

    async def analyze(
        self,
        requirement: str,
        existing_code: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> IntakeResult:
        """Normalize a requirement and decide whether it is safe to plan.

        `existing_code` makes this a brownfield analysis: the stage is asked to
        identify impacted modules and data flows rather than design from
        nothing. `answers` carries a human's responses to a previous park, which
        is how a parked run resumes rather than restarts.
        """
        outcome = await self.dispatcher.dispatch(
            node_id="intake",
            skill_id=StageKind.ANALYZE.value,
            tier=ModelTier.DEEP,
            payload={
                "requirement": requirement,
                "existing_code": existing_code or "(none, this is a new system)",
                "prior_answers": _format_answers(answers),
            },
        )

        if outcome.state is not TaskState.COMPLETED or outcome.parsed is None:
            problem = EngineeringProblem(raw_requirement=requirement, confidence=0.0)
            return IntakeResult(
                problem=problem,
                state=TaskState.FAILED,
                questions=[],
                cost_usd=outcome.cost_usd,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            )

        problem = _to_problem(requirement, outcome.parsed)
        _apply_answers(problem, answers)

        state = (
            TaskState.COMPLETED
            if problem.is_plannable(self.threshold)
            else TaskState.INPUT_REQUIRED
        )

        return IntakeResult(
            problem=problem,
            state=state,
            questions=problem.blocking_ambiguities,
            cost_usd=outcome.cost_usd,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )


def _format_answers(answers: dict[str, str] | None) -> str:
    if not answers:
        return "(none)"
    return "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answers.items())


def _to_problem(requirement: str, parsed: dict[str, Any]) -> EngineeringProblem:
    """Map the analyst's structured output onto the frozen contract.

    Defensive throughout: this is model output crossing a trust boundary, so
    every field is coerced rather than assumed.
    """
    ambiguities = []
    for raw in parsed.get("ambiguities") or []:
        if not isinstance(raw, dict):
            continue
        ambiguities.append(
            Ambiguity(
                question=str(raw.get("question", "")).strip(),
                why_it_matters=str(raw.get("why_it_matters", "")).strip(),
                blocking=bool(raw.get("blocking", True)),
                options=[str(o) for o in (raw.get("options") or [])],
            )
        )

    try:
        scenario = ScenarioKind(str(parsed.get("scenario", "greenfield")).lower())
    except ValueError:
        scenario = ScenarioKind.GREENFIELD

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return EngineeringProblem(
        raw_requirement=requirement,
        intent=str(parsed.get("intent", "")).strip(),
        acceptance_criteria=[str(c) for c in (parsed.get("acceptance_criteria") or [])],
        constraints=[str(c) for c in (parsed.get("constraints") or [])],
        ambiguities=[a for a in ambiguities if a.question],
        scenario=scenario,
        confidence=max(0.0, min(1.0, confidence)),
        notes=str(parsed.get("notes", "")).strip(),
    )


def _apply_answers(problem: EngineeringProblem, answers: dict[str, str] | None) -> None:
    """Resolve ambiguities a human has already answered.

    Matching is on a normalized prefix of the question text rather than an id,
    because the analyst regenerates questions on resume and ids would not be
    stable across runs.
    """
    if not answers:
        return
    normalized = {_key(q): a for q, a in answers.items()}
    for amb in problem.ambiguities:
        hit = normalized.get(_key(amb.question))
        if hit is not None:
            amb.answer = hit
    if not problem.blocking_ambiguities:
        problem.confidence = max(problem.confidence, DEFAULT_CONFIDENCE_THRESHOLD)


def _key(question: str) -> str:
    return "".join(ch for ch in question.lower() if ch.isalnum())[:60]
