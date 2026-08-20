"""Agent Cards: the capability contract each stage agent publishes.

One card per `StageKind`, each declaring exactly one skill whose id is the
StageKind value. That one-to-one rule is the point of this module. The planner
never carries a hardcoded table of what the mesh can do; it reads the served
cards and discovers the skill ids, so adding a stage agent to the mesh is a
deployment concern rather than a code change in the planner. `keel.planner`
already emits `skill_id=StageKind.X.value`, and the equality is asserted in the
test suite so the two halves cannot drift apart silently.

Cards are protobuf messages, not pydantic models. `a2a.types.AgentCard` is
`a2a_pb2.AgentCard`, so nested values are constructed as protobuf messages and
maps are plain dicts of them.

On the security declaration: the card advertises an HTTP bearer scheme named
`keel_bearer`. This is not decoration. The SDK's `AuthInterceptor` reads
`security_requirements` and `security_schemes` off the *resolved* card to decide
whether to attach an Authorization header, so a client only sends the token
because the card asked for it. Discovery itself stays unauthenticated, which it
must: a client cannot know to authenticate until it has read the card.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from a2a.types import a2a_pb2 as pb
from a2a.utils import TransportProtocol

from keel.models import StageKind

# The well-known discovery path defined by A2A. Clients resolve this relative to
# the agent's base URL.
AGENT_CARD_PATH = "/.well-known/agent-card.json"

# Name of the security scheme on every keel card. The client's credential
# service is keyed by this name.
BEARER_SCHEME_NAME = "keel_bearer"

# Environment variable holding the token the orchestrator presents.
AUTH_TOKEN_ENV = "KEEL_A2A_AUTH_TOKEN"

AGENT_VERSION = "1.0.0"

# Every stage kind, in SDLC order. Exported so the mesh launcher and the tests
# iterate the same sequence the planner does.
ALL_STAGES: tuple[StageKind, ...] = tuple(StageKind)

# Base for the conventional local port assignment, one port per stage.
DEFAULT_PORT_BASE = 8710


class _StageProfile:
    """Human-facing copy for one stage card. Kept out of the enum on purpose.

    `keel.models` is a frozen contract and must not grow presentation strings,
    so the prose that appears on the wire lives here instead.
    """

    __slots__ = ("title", "summary", "skill_summary", "tags")

    def __init__(self, title: str, summary: str, skill_summary: str, tags: tuple[str, ...]):
        self.title = title
        self.summary = summary
        self.skill_summary = skill_summary
        self.tags = tags


_PROFILES: dict[StageKind, _StageProfile] = {
    StageKind.ANALYZE: _StageProfile(
        title="Analyze",
        summary="Turns a raw requirement into something a planner can act on.",
        skill_summary=(
            "Normalizes a requirement into intent, acceptance criteria and constraints, "
            "and names the ambiguities that block planning instead of guessing past them."
        ),
        tags=("requirements", "ambiguity"),
    ),
    StageKind.DESIGN: _StageProfile(
        title="Design",
        summary="Chooses a shape for the change before any code is written.",
        skill_summary=(
            "Produces a component design, the interfaces it implies and the trade-offs "
            "that were rejected, so review has something to argue with."
        ),
        tags=("architecture", "interfaces"),
    ),
    StageKind.IMPLEMENT: _StageProfile(
        title="Implement",
        summary="Writes the code for one planned unit of work.",
        skill_summary=(
            "Emits source files for a single node of the plan, scoped to that node's "
            "description so a failure rolls back one unit rather than the whole run."
        ),
        tags=("codegen", "files"),
    ),
    StageKind.TEST: _StageProfile(
        title="Test",
        summary="Writes and evaluates tests against the acceptance criteria.",
        skill_summary=(
            "Produces tests traceable to acceptance criteria and reports failures as "
            "structured artifacts, which is what lets the exit gate be mechanical."
        ),
        tags=("tests", "verification"),
    ),
    StageKind.DOCUMENT: _StageProfile(
        title="Document",
        summary="Keeps the docs honest in the same pass as the change.",
        skill_summary=(
            "Writes the documentation a change makes necessary, so no run finishes "
            "with docs that contradict the code it just shipped."
        ),
        tags=("docs",),
    ),
    StageKind.REVIEW: _StageProfile(
        title="Review",
        summary="Reads the diff the way a senior engineer would.",
        skill_summary=(
            "Reviews produced artifacts for correctness, security surface and "
            "convention drift, returning severities the governance plane can gate on."
        ),
        tags=("review", "security"),
    ),
    StageKind.RELEASE_CHECK: _StageProfile(
        title="Release Check",
        summary="The last gate before anything is called done.",
        skill_summary=(
            "Confirms acceptance criteria are covered, policy violations are cleared "
            "and rollback is available, then allows or blocks the release."
        ),
        tags=("gate", "release"),
    ),
}


def agent_name(kind: StageKind) -> str:
    """Stable agent name for a stage. Used in logs and on the card."""
    return f"keel-{kind.value.replace('_', '-')}"


def default_port(kind: StageKind) -> int:
    """Conventional local port for a stage agent.

    Deterministic so a scenario run can boot the mesh and the orchestrator can
    find it without a service registry. Tests use ephemeral ports instead.
    """
    return DEFAULT_PORT_BASE + ALL_STAGES.index(kind)


def local_base_url(kind: StageKind, host: str = "127.0.0.1") -> str:
    """Where this stage agent lives when the mesh runs on one machine."""
    return f"http://{host}:{default_port(kind)}"


def bearer_security_scheme() -> pb.SecurityScheme:
    """The HTTP bearer scheme advertised by every keel stage agent."""
    return pb.SecurityScheme(
        http_auth_security_scheme=pb.HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="opaque",
            description=(
                "Bearer token issued to the keel orchestrator. Presented on every "
                "JSON-RPC call. Card discovery is deliberately exempt."
            ),
        )
    )


def bearer_security_requirement() -> pb.SecurityRequirement:
    """Requires the bearer scheme with no additional scopes.

    `SecurityRequirement.schemes` is a map of scheme name to a `StringList` of
    scopes. Bearer tokens here are opaque and unscoped, so the list is empty,
    which is the protobuf spelling of "this scheme, no scopes".
    """
    return pb.SecurityRequirement(schemes={BEARER_SCHEME_NAME: pb.StringList(list=[])})


def skill_for(kind: StageKind) -> AgentSkill:
    """The single skill a stage agent offers.

    The skill id is the StageKind value verbatim. `NodeSpec.skill_id` holds the
    same string, so dispatching a node is a lookup rather than a translation.
    """
    profile = _PROFILES[kind]
    return AgentSkill(
        id=kind.value,
        name=profile.title,
        description=profile.skill_summary,
        tags=["sdlc", *profile.tags],
    )


def card_for(kind: StageKind, base_url: str) -> AgentCard:
    """Build the Agent Card a stage agent serves at `AGENT_CARD_PATH`.

    `base_url` is the agent's own externally reachable root. It is written into
    `supported_interfaces` because that, not the discovery URL, is what the
    client dials for JSON-RPC.
    """
    profile = _PROFILES[kind]
    rpc_url = base_url.rstrip("/") + "/"
    return AgentCard(
        name=agent_name(kind),
        description=profile.summary,
        version=AGENT_VERSION,
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill_for(kind)],
        supported_interfaces=[
            AgentInterface(url=rpc_url, protocol_binding=TransportProtocol.JSONRPC)
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        security_schemes={BEARER_SCHEME_NAME: bearer_security_scheme()},
        security_requirements=[bearer_security_requirement()],
    )


def all_cards(host: str = "127.0.0.1") -> dict[StageKind, AgentCard]:
    """Cards for the whole mesh at its conventional local addresses."""
    return {kind: card_for(kind, local_base_url(kind, host)) for kind in ALL_STAGES}


def skill_ids(card: AgentCard) -> list[str]:
    """Read the capabilities off a card.

    This is how the planner learns what the mesh can do. It takes a card that
    arrived over the wire and returns skill ids, with no local table involved.
    """
    return [skill.id for skill in card.skills]


def stage_for_skill(skill_id: str) -> StageKind:
    """Inverse of the skill id rule. Raises for an unknown skill."""
    try:
        return StageKind(skill_id)
    except ValueError as exc:
        raise KeyError(f"no stage kind for skill {skill_id!r}") from exc
