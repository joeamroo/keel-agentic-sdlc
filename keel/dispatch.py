"""How a plan node gets turned into actual work.

The executor must not know whether a stage ran in this process or across an A2A
connection. It hands a node to a `StageDispatcher` and gets a `StageOutcome`
back. That boundary is what lets the orchestration logic be unit tested without
binding seven ports, and it is why swapping the transport later is a
configuration change rather than a rewrite.

`LocalDispatcher` calls the model adapter directly. `A2ADispatcher` (in
`keel.a2a.transport`) speaks the real protocol. Both satisfy the same protocol
declared here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from keel.models import (
    AdapterRequest,
    AdapterResponse,
    AgentAdapter,
    Artifact,
    ModelTier,
    TaskState,
)


@dataclass(slots=True)
class StageOutcome:
    """What a stage produced, in terms the governance plane understands.

    `state` is the A2A task state, so a stage that needs a human is
    indistinguishable at this layer from one that ran over the wire and parked
    in INPUT_REQUIRED.
    """

    state: TaskState
    artifacts: list[Artifact] = field(default_factory=list)
    message: str = ""
    parsed: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    from_replay: bool = False

    @property
    def ok(self) -> bool:
        return self.state is TaskState.COMPLETED


@runtime_checkable
class StageDispatcher(Protocol):
    """Runs one stage. In-process or over A2A, the executor cannot tell."""

    async def dispatch(
        self,
        node_id: str,
        skill_id: str,
        tier: ModelTier,
        payload: dict[str, Any],
    ) -> StageOutcome: ...


def _artifacts_from_parsed(
    parsed: dict[str, Any], node_id: str, fallback_name: str
) -> list[Artifact]:
    """Turn a stage's structured output into artifacts.

    Stages that emit source code return a `files` list; everything else is
    treated as one JSON artifact so that policy rules, hashing, and lineage
    work uniformly regardless of stage.
    """
    files = parsed.get("files")
    if isinstance(files, list) and files:
        out: list[Artifact] = []
        for f in files:
            if not isinstance(f, dict) or "path" not in f:
                continue
            out.append(
                Artifact(
                    name=str(f["path"]),
                    content=str(f.get("content", "")),
                    produced_by=node_id,
                    path=str(f["path"]),
                    media_type="text/x-python"
                    if str(f["path"]).endswith(".py")
                    else "text/plain",
                )
            )
        if out:
            return out

    return [
        Artifact(
            name=fallback_name,
            content=json.dumps(parsed, indent=2, sort_keys=True),
            produced_by=node_id,
            media_type="application/json",
        )
    ]


class LocalDispatcher:
    """Runs a stage by calling the model adapter directly, no network.

    Used by the unit test suite and by anyone who wants the orchestrator
    without the A2A mesh. The scenario runs use the A2A dispatcher instead.
    """

    def __init__(
        self,
        adapter: AgentAdapter,
        definitions: dict[Any, Any] | None = None,
        on_call: Callable[[AdapterRequest, AdapterResponse], Any] | None = None,
    ):
        """`on_call` fires after every model call, whichever adapter is in use.

        Recording lives here rather than inside an adapter on purpose. Hanging
        it off one adapter's callback means swapping the adapter silently stops
        the audit trail, and "every model call is recorded" is not a property
        that should depend on which implementation happens to be plugged in.
        """
        self.adapter = adapter
        self._definitions = definitions
        self._on_call = on_call

    def _definition(self, skill_id: str):
        if self._definitions is None:
            from keel.agents.definitions import DEFINITIONS

            self._definitions = DEFINITIONS
        for defn in self._definitions.values():
            if defn.skill_id == skill_id:
                return defn
        raise KeyError(f"no stage definition for skill {skill_id!r}")

    async def dispatch(
        self,
        node_id: str,
        skill_id: str,
        tier: ModelTier,
        payload: dict[str, Any],
    ) -> StageOutcome:
        defn = self._definition(skill_id)
        prompt = defn.prompt_template.format(**payload)

        request = AdapterRequest(
            node_id=node_id,
            skill_id=skill_id,
            tier=tier,
            system=defn.system_prompt,
            prompt=prompt,
            json_schema=defn.json_schema,
        )
        response = await self.adapter.invoke(request)
        if self._on_call is not None:
            self._on_call(request, response)

        parsed = response.parsed
        if parsed is None:
            return StageOutcome(
                state=TaskState.FAILED,
                message="stage returned no parseable structured output",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                model=response.model,
                from_replay=response.from_replay,
            )

        return StageOutcome(
            state=TaskState.COMPLETED,
            artifacts=_artifacts_from_parsed(parsed, node_id, f"{skill_id}.json"),
            parsed=parsed,
            message="ok",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            model=response.model,
            from_replay=response.from_replay,
        )
