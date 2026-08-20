"""Frozen contracts for keel.

Every other module in this package builds against the types declared here.
Nothing in this file may import from the rest of `keel`, which keeps the
dependency graph acyclic and lets the modules be developed independently.

Design note worth defending in review: the governance plane models task state
itself rather than reaching for the protobuf enum from the A2A SDK. The values
mirror A2A v1.0 exactly and `to_a2a` / `from_a2a` convert at the boundary, so
governance logic stays serializable and free of any wire-format dependency
while remaining protocol-faithful.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Task state: mirrors the A2A v1.0 TaskState machine
# --------------------------------------------------------------------------


class TaskState(str, Enum):
    """The A2A v1.0 task lifecycle, mirrored for internal use."""

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input_required"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth_required"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_interrupted(self) -> bool:
        """Not finished, not running: waiting on a human or on credentials."""
        return self in _INTERRUPTED

    def to_a2a(self) -> int:
        """Convert to the A2A protobuf enum value."""
        from a2a.types import TaskState as PbTaskState

        return getattr(PbTaskState, f"TASK_STATE_{self.name}")

    @classmethod
    def from_a2a(cls, value: int) -> TaskState:
        from a2a.types import TaskState as PbTaskState

        return cls[PbTaskState.Name(value).removeprefix("TASK_STATE_")]


_TERMINAL = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELED,
    TaskState.REJECTED,
}
_INTERRUPTED = {TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED}


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class StageKind(str, Enum):
    """SDLC stages. Each maps to one A2A stage agent and one Agent Card skill."""

    ANALYZE = "analyze"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    DOCUMENT = "document"
    REVIEW = "review"
    RELEASE_CHECK = "release_check"


class ModelTier(str, Enum):
    """Cost-aware routing. Nodes declare the thinking they actually need."""

    DEEP = "deep"  # claude-opus-5: planning, code generation, review
    FAST = "fast"  # claude-haiku-4-5: classification, mechanical gate checks


MODEL_FOR_TIER: dict[ModelTier, str] = {
    ModelTier.DEEP: "claude-opus-5",
    ModelTier.FAST: "claude-haiku-4-5",
}

# USD per million tokens, for the cost metric. Input, output.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def price_for(model: str) -> tuple[float, float]:
    """Rates for a model id, tolerating a dated suffix.

    The API echoes back the resolved id, which for some models carries a date
    (`claude-haiku-4-5-20251001`) that an alias-keyed table does not contain.
    An exact lookup therefore priced those calls at zero and silently
    understated the cost metric, which was caught by a live run reporting a
    Haiku stage as free. Returning zero for a genuinely unknown model is still
    the right default, but a known model wearing its date is not unknown.
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for known, rates in MODEL_PRICING.items():
        if model.startswith(known):
            return rates
    return (0.0, 0.0)


class ScenarioKind(str, Enum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"
    AMBIGUOUS = "ambiguous"


class RunMode(str, Enum):
    LIVE = "live"
    REPLAY = "replay"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def blocks(self) -> bool:
        return self in (Severity.HIGH, Severity.CRITICAL)


class ImpactLevel(str, Enum):
    """Drives whether a node needs human sign-off before it runs."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"  # public API change, destructive op, security surface


# --------------------------------------------------------------------------
# Requirement understanding (§4.1)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Ambiguity:
    """One thing the requirement failed to specify."""

    question: str
    why_it_matters: str
    blocking: bool = True
    options: list[str] = field(default_factory=list)
    answer: str | None = None

    @property
    def resolved(self) -> bool:
        return self.answer is not None


@dataclass(slots=True)
class EngineeringProblem:
    """A natural-language requirement normalized into something plannable."""

    raw_requirement: str
    intent: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    scenario: ScenarioKind = ScenarioKind.GREENFIELD
    confidence: float = 0.0  # 0.0-1.0
    notes: str = ""

    @property
    def blocking_ambiguities(self) -> list[Ambiguity]:
        return [a for a in self.ambiguities if a.blocking and not a.resolved]

    def is_plannable(self, threshold: float = 0.6) -> bool:
        """False means: park in INPUT_REQUIRED, do not write code."""
        return not self.blocking_ambiguities and self.confidence >= threshold


# --------------------------------------------------------------------------
# Plan graph (§4.4: explicit dependency graph with gates)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RetryPolicy:
    """Bounded retries, then fallback, then rollback, then safe stop."""

    max_attempts: int = 2
    backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    fallback_tier: ModelTier | None = None
    fallback_hint: str | None = None

    def delay_for(self, attempt: int) -> float:
        """Backoff before retry number `attempt`, which is 1-based.

        `delay_for(1)` is the wait before the first retry, so the first attempt
        itself is never delayed.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based; the first retry is attempt=1")
        return self.backoff_seconds * (self.backoff_multiplier ** (attempt - 1))


@dataclass(slots=True)
class NodeSpec:
    """One unit of work in the plan graph."""

    id: str
    kind: StageKind
    description: str
    depends_on: list[str] = field(default_factory=list)
    skill_id: str = ""  # Agent Card skill this node dispatches to
    model_tier: ModelTier = ModelTier.DEEP
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    impact: ImpactLevel = ImpactLevel.LOW
    entry_rules: list[str] = field(default_factory=list)
    exit_rules: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)  # expected artifact names
    rollback: bool = True  # snapshot workspace before running

    @property
    def needs_approval(self) -> bool:
        return self.impact is ImpactLevel.HIGH


@dataclass(slots=True)
class Plan:
    """A versioned DAG. Regenerating this is what makes re-planning possible."""

    nodes: list[NodeSpec]
    version: int = 1
    rationale: str = ""
    supersedes: int | None = None

    def by_id(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no such node: {node_id}")

    @property
    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


# --------------------------------------------------------------------------
# Artifacts and lineage (§4.3, §4.5)
# --------------------------------------------------------------------------


def content_hash(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(slots=True)
class Artifact:
    """Something a stage produced. Hashed so staleness is detectable."""

    name: str
    content: str
    produced_by: str  # node id
    path: str | None = None  # relative path in the workspace, if a file
    media_type: str = "text/plain"
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    @property
    def sha(self) -> str:
        return content_hash(self.content)


@dataclass(slots=True)
class LineageEdge:
    """Records that a node consumed a specific version of an artifact.

    When the artifact's hash later changes, everything downstream of this edge
    is stale and the planner is asked to repair that subgraph.
    """

    artifact_name: str
    artifact_sha: str
    produced_by: str
    consumed_by: str
    at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Policy and gates (§4.4: entry/exit gates, policy guardrails)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PolicyViolation:
    rule_id: str
    severity: Severity
    message: str
    location: str | None = None

    @property
    def blocks(self) -> bool:
        return self.severity.blocks


@dataclass(slots=True)
class GateDecision:
    """Auditable yes/no with a reason. Never a bare boolean."""

    gate: str  # "entry" or "exit"
    node_id: str
    allowed: bool
    reason: str
    violations: list[PolicyViolation] = field(default_factory=list)

    @classmethod
    def allow(cls, gate: str, node_id: str, reason: str = "ok") -> GateDecision:
        return cls(gate=gate, node_id=node_id, allowed=True, reason=reason)

    @classmethod
    def deny(
        cls, gate: str, node_id: str, reason: str, violations: list[PolicyViolation] | None = None
    ) -> GateDecision:
        return cls(
            gate=gate,
            node_id=node_id,
            allowed=False,
            reason=reason,
            violations=violations or [],
        )


# --------------------------------------------------------------------------
# Human-in-the-loop (§4.4, §4.7: controlled autonomy)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ApprovalRequest:
    node_id: str
    reason: str
    impact: ImpactLevel
    details: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass(slots=True)
class ApprovalDecision:
    request_id: str
    approved: bool
    decided_by: str = "human"
    note: str = ""
    at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Execution results and audit (§4.4: observability, metrics)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class NodeResult:
    node_id: str
    state: TaskState
    attempts: int = 1
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    rolled_back: bool = False
    used_fallback: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    gate_decisions: list[GateDecision] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


class AuditEventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    INTAKE = "intake"
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    GATE_DECISION = "gate_decision"
    POLICY_VIOLATION = "policy_violation"
    RETRY = "retry"
    FALLBACK = "fallback"
    ROLLBACK = "rollback"
    REPAIR_STARTED = "repair_started"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    SAFE_STOP = "safe_stop"
    MODEL_CALL = "model_call"
    ARTIFACT_WRITTEN = "artifact_written"


@dataclass(slots=True)
class AuditEvent:
    """One append-only line of the audit log.

    The model_call events double as replay cassettes, so observability and
    reproducibility are satisfied by a single artifact.
    """

    run_id: str
    event_type: AuditEventType
    payload: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    at: float = field(default_factory=time.time)
    seq: int = 0


@dataclass(slots=True)
class RunMetrics:
    """Exactly the reliability metrics §4.4 names, plus cost."""

    run_id: str
    total_nodes: int = 0
    succeeded: int = 0
    failed: int = 0
    retries: int = 0
    rollbacks: int = 0
    fallbacks: int = 0
    approvals_requested: int = 0
    plan_revisions: int = 0
    e2e_latency_seconds: float = 0.0
    mttr_seconds: float | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.total_nodes if self.total_nodes else 0.0


# --------------------------------------------------------------------------
# Adapter contract (live Claude vs replay)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class AdapterRequest:
    node_id: str
    skill_id: str
    tier: ModelTier
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None

    @property
    def cassette_key(self) -> str:
        """Stable identity for replay lookup. Must not include timestamps.

        The schema is part of the key because it changes the shape of what the
        model returns. Without it, a structured request and an unstructured one
        with identical prompts collide, and replay serves the wrong recording.
        """
        schema = (
            json.dumps(self.json_schema, sort_keys=True) if self.json_schema else "none"
        )
        return content_hash(
            "\n".join([self.skill_id, self.tier.value, self.system, self.prompt, schema])
        )


@dataclass(slots=True)
class AdapterResponse:
    text: str
    parsed: dict[str, Any] | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    from_replay: bool = False

    @property
    def cost_usd(self) -> float:
        rate_in, rate_out = price_for(self.model)
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1_000_000


@runtime_checkable
class AgentAdapter(Protocol):
    """What actually does the thinking. Live Claude, or a recorded transcript."""

    mode: RunMode

    async def invoke(self, request: AdapterRequest) -> AdapterResponse: ...


@runtime_checkable
class PolicyRule(Protocol):
    """A governance rule evaluated at a gate."""

    rule_id: str
    severity: Severity

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]: ...


# --------------------------------------------------------------------------
# Run context
# --------------------------------------------------------------------------


def new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:4]}"
