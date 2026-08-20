"""The orchestrator's side of the A2A connection.

`StageClient` wraps the SDK client so the rest of keel deals in
`keel.models` types. A2A protobuf messages stop here: callers get
`TaskState` and `Artifact`, never a `pb.StreamResponse`. That containment is
what lets the governance plane stay serializable and free of a wire-format
dependency, which is the design note at the top of `keel.models`.

On `StageOutcome`: it is imported from `keel.dispatch` rather than redefined
here. `keel.dispatch` already owns that type and the executor already consumes
it, so a second structurally similar dataclass would be two types the codebase
has to keep in sync by hand. It is re-exported from this module because a
stage client returning a stage outcome is the natural reading.

Bearer auth is genuinely wired, not decorative. The chain is worth stating
because it is easy to assume and wrong to guess:

  `AuthInterceptor.before` reads `security_requirements` and `security_schemes`
  off the resolved card, resolves a credential through a `CredentialService`,
  and writes `context.service_parameters["Authorization"]`. The JSON-RPC
  transport's `get_http_args` copies `service_parameters` into httpx headers.
  `BaseClient` passes the interceptor-mutated context to the transport call.

So the header is only attached because the served card asked for it, which is
the behaviour the A2A spec intends. If `KEEL_A2A_AUTH_TOKEN` is unset the
interceptor is not installed at all and calls go out unauthenticated.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from a2a.client import (
    A2ACardResolver,
    AuthInterceptor,
    CredentialService,
    create_client,
)
from a2a.types import AgentCard, Part
from a2a.types import a2a_pb2 as pb

from keel.a2a.cards import AGENT_CARD_PATH, AUTH_TOKEN_ENV
from keel.dispatch import StageOutcome
from keel.models import Artifact, TaskState

__all__ = ["StageClient", "StageOutcome", "StaticTokenCredentials", "discover"]


class StaticTokenCredentials(CredentialService):
    """Hands the same token to any scheme the card names.

    The SDK ships `InMemoryContextCredentialStore`, but it keys credentials by
    a `sessionId` that has to be threaded through a `ClientCallContext` on every
    call. keel presents one process-wide token to a trusted internal mesh, so
    that indirection would buy nothing and add a way to get it silently wrong.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_credentials(
        self, security_scheme_name: str, context: Any = None
    ) -> str | None:
        return self._token


async def discover(base_url: str, *, timeout: float = 10.0) -> AgentCard:
    """Fetch and parse an agent's card.

    Uses the SDK's resolver rather than `json_format.ParseDict`, and that is
    not a stylistic preference. The served card is a hybrid document: it
    carries the protobuf-JSON encoding *and*, alongside it, the flattened v0.3
    spec aliases (`type`, `scheme` and `bearerFormat` promoted to the top of
    each security scheme, plus `url`, `security` and `preferredTransport` at
    the root). A strict protobuf parse rejects those siblings as unknown
    fields. The resolver is the code path `create_client` itself uses, so
    discovery here and connection there agree by construction.

    Kept as a free function so the planner can read what the mesh offers
    without standing up a transport.
    """
    async with httpx.AsyncClient(timeout=timeout) as http:
        resolver = A2ACardResolver(http, base_url, agent_card_path=AGENT_CARD_PATH)
        return await resolver.get_agent_card()


def _text_of(parts: Any) -> str:
    """Join the text parts of a protobuf message or artifact."""
    return "\n".join(part.text for part in parts if part.text)


class StageClient:
    """Talks to one stage agent over A2A JSON-RPC.

    One client per agent, because the SDK client is bound to a resolved card
    and therefore to a single agent's transport and security scheme.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        node_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Read the env at construction so a caller can override per client and
        # tests can monkeypatch without reaching into module state.
        self.token = token if token is not None else os.environ.get(AUTH_TOKEN_ENV, "")
        self.node_id = node_id
        self.last_task_id: str = ""
        self._client: Any | None = None
        self._card: AgentCard | None = None

    @staticmethod
    async def discover(base_url: str, *, timeout: float = 10.0) -> AgentCard:
        """Read a card without committing to a connection."""
        return await discover(base_url, timeout=timeout)

    async def card(self) -> AgentCard:
        """The card this client is bound to, resolved once and cached."""
        if self._card is None:
            await self._ensure_client()
        assert self._card is not None
        return self._card

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        interceptors = (
            [AuthInterceptor(StaticTokenCredentials(self.token))] if self.token else None
        )
        # `create_client` is a coroutine function in a2a-sdk 1.1.2. Handling
        # both shapes keeps this working if that ever changes back.
        created = create_client(self.base_url, interceptors=interceptors)
        if asyncio.iscoroutine(created):
            created = await created
        self._client = created
        self._card = created._card if hasattr(created, "_card") else None
        if self._card is None:
            self._card = await discover(self.base_url)
        return self._client

    def supports(self, skill_id: str) -> bool:
        """Whether the resolved card advertises this skill.

        Cheap guard against dispatching a node at an agent that cannot serve
        it, which otherwise surfaces as a confusing mid-stream failure.
        """
        if self._card is None:
            return False
        return any(skill.id == skill_id for skill in self._card.skills)

    async def invoke(
        self,
        skill_id: str,
        text: str,
        *,
        node_id: str = "",
        on_task_id: Callable[[str], None] | None = None,
    ) -> StageOutcome:
        """Run one stage task to a terminal or interrupted state.

        Consumes the whole event stream rather than returning early, so the
        outcome carries every artifact the stage emitted. `on_task_id` fires as
        soon as the id is known, which is what a caller needs to cancel a task
        that is still streaming.
        """
        client = await self._ensure_client()
        if self._card is not None and not self.supports(skill_id):
            raise KeyError(
                f"agent at {self.base_url} does not offer skill {skill_id!r}"
            )

        request = pb.SendMessageRequest(
            message=pb.Message(
                message_id=str(uuid.uuid4()),
                role=pb.Role.ROLE_USER,
                parts=[Part(text=text)],
            )
        )
        # Carried as request metadata so it lands on `RequestContext.metadata`,
        # which reads the request's Struct rather than the message's.
        request.metadata.update({"skill_id": skill_id})

        owner = node_id or self.node_id or skill_id
        state = TaskState.SUBMITTED
        artifacts: list[Artifact] = []
        message = ""
        task_id = ""

        async for event in client.send_message(request):
            which = event.WhichOneof("payload")

            if which == "task":
                task_id = event.task.id or task_id
                state = TaskState.from_a2a(event.task.status.state)
            elif which == "status_update":
                task_id = event.status_update.task_id or task_id
                state = TaskState.from_a2a(event.status_update.status.state)
                part_text = _text_of(event.status_update.status.message.parts)
                if part_text:
                    message = part_text
            elif which == "artifact_update":
                incoming = event.artifact_update.artifact
                artifacts.append(
                    Artifact(
                        name=incoming.name or "artifact",
                        content=_text_of(incoming.parts),
                        produced_by=owner,
                    )
                )
            elif which == "message":
                part_text = _text_of(event.message.parts)
                if part_text:
                    message = part_text

            if task_id and task_id != self.last_task_id:
                self.last_task_id = task_id
                if on_task_id is not None:
                    on_task_id(task_id)

        return StageOutcome(state=state, artifacts=artifacts, message=message)

    async def cancel(self, task_id: str) -> TaskState:
        """Request cancellation and report the state the agent settled on."""
        client = await self._ensure_client()
        cancelled = await client.cancel_task(pb.CancelTaskRequest(id=task_id))
        return TaskState.from_a2a(cancelled.status.state)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
