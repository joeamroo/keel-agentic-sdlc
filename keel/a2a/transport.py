"""One interface, two ways of reaching a stage.

`HttpTransport` speaks real A2A over the network and is what the scenario runs
use. `InProcessTransport` calls the same stage handlers directly in this
process and is what the unit suite uses, so a full test run never binds a port
or waits on a socket. Both satisfy `Transport`, so nothing above this layer
knows or cares which is installed.

Keeping the fake at the transport seam rather than inside the client is what
makes the substitution honest. Both paths run the same handlers through the
same `parse_stage_result`, so an in-process test failing means the real mesh
would have failed too. The only thing `InProcessTransport` skips is the wire,
which is exactly the part the HTTP test covers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from keel.a2a.cards import ALL_STAGES, local_base_url
from keel.a2a.client import StageClient
from keel.a2a.server import StageHandler, parse_stage_result
from keel.dispatch import StageOutcome
from keel.models import Artifact, ModelTier, StageKind, TaskState

__all__ = [
    "A2ADispatcher",
    "CancelNotAllowed",
    "HttpTransport",
    "InProcessTransport",
    "Transport",
]


class CancelNotAllowed(RuntimeError):
    """Raised when a cancel arrives for a task that has already finished.

    Mirrors the server, which refuses to cancel a task in a terminal state. The
    in-process path raises the same error so a caller's handling of the race
    between "finished" and "cancelled" is exercised by the fast tests.
    """


@runtime_checkable
class Transport(Protocol):
    """How the orchestrator reaches a stage agent."""

    async def invoke(
        self, kind: StageKind, skill_id: str, text: str, *, node_id: str = ""
    ) -> StageOutcome: ...

    async def cancel(self, kind: StageKind, task_id: str) -> TaskState: ...

    async def close(self) -> None: ...


class InProcessTransport:
    """Runs stage handlers directly, with no server and no socket.

    Task ids are minted locally and tracked so cancellation behaves the way the
    real agent does rather than always succeeding.
    """

    def __init__(self, handlers: Mapping[StageKind, StageHandler]) -> None:
        self._handlers = dict(handlers)
        self._task_states: dict[str, TaskState] = {}

    @property
    def stages(self) -> tuple[StageKind, ...]:
        return tuple(self._handlers)

    def _handler(self, kind: StageKind) -> StageHandler:
        try:
            return self._handlers[kind]
        except KeyError as exc:
            raise KeyError(f"no in-process handler for stage {kind.value!r}") from exc

    async def invoke(
        self, kind: StageKind, skill_id: str, text: str, *, node_id: str = ""
    ) -> StageOutcome:
        handler = self._handler(kind)
        task_id = f"local-{uuid.uuid4().hex[:12]}"
        self._task_states[task_id] = TaskState.WORKING
        owner = node_id or skill_id or kind.value

        try:
            result = parse_stage_result(await handler(text))
        except Exception as exc:  # noqa: BLE001 - mirrors the executor
            self._task_states[task_id] = TaskState.FAILED
            return StageOutcome(
                state=TaskState.FAILED, message=f"{type(exc).__name__}: {exc}"
            )

        self._task_states[task_id] = result.state
        artifacts = [
            Artifact(name=name, content=content, produced_by=owner)
            for name, content in result.artifacts
        ]
        return StageOutcome(
            state=result.state, artifacts=artifacts, message=result.message
        )

    async def cancel(self, kind: StageKind, task_id: str) -> TaskState:
        if task_id not in self._task_states:
            raise KeyError(f"unknown task {task_id!r}")
        if self._task_states[task_id].is_terminal:
            raise CancelNotAllowed(f"task {task_id!r} already finished")
        self._task_states[task_id] = TaskState.CANCELED
        return TaskState.CANCELED

    async def close(self) -> None:
        """Nothing to release. Present so the interface is uniform."""
        return None


class HttpTransport:
    """Real A2A over HTTP, one `StageClient` per stage agent.

    Clients are built lazily because constructing one resolves the remote card,
    and a run that touches three stages should not require the other four to be
    reachable.
    """

    def __init__(
        self,
        base_urls: Mapping[StageKind, str] | None = None,
        *,
        token: str | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self._base_urls = (
            dict(base_urls)
            if base_urls is not None
            else {kind: local_base_url(kind, host) for kind in ALL_STAGES}
        )
        self._token = token
        self._clients: dict[StageKind, StageClient] = {}

    @property
    def stages(self) -> tuple[StageKind, ...]:
        return tuple(self._base_urls)

    def client_for(self, kind: StageKind) -> StageClient:
        if kind not in self._clients:
            try:
                base_url = self._base_urls[kind]
            except KeyError as exc:
                raise KeyError(f"no address for stage {kind.value!r}") from exc
            self._clients[kind] = StageClient(base_url, token=self._token)
        return self._clients[kind]

    async def invoke(
        self, kind: StageKind, skill_id: str, text: str, *, node_id: str = ""
    ) -> StageOutcome:
        return await self.client_for(kind).invoke(skill_id, text, node_id=node_id)

    async def cancel(self, kind: StageKind, task_id: str) -> TaskState:
        return await self.client_for(kind).cancel(task_id)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


class A2ADispatcher:
    """Adapts a `Transport` to the `StageDispatcher` protocol in `keel.dispatch`.

    The executor dispatches by node and skill and knows nothing about stages or
    addresses. This translates that call into a transport invocation, which is
    the seam `keel.dispatch` promises when it says the A2A dispatcher lives
    here.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    async def dispatch(
        self,
        node_id: str,
        skill_id: str,
        tier: ModelTier,
        payload: dict[str, Any],
    ) -> StageOutcome:
        try:
            kind = StageKind(skill_id)
        except ValueError as exc:
            raise KeyError(f"no stage kind for skill {skill_id!r}") from exc

        # The payload crosses the wire as text because A2A parts are text. JSON
        # with sorted keys keeps the request byte-stable, which matters for
        # replay cassettes keyed on request content.
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
        return await self.transport.invoke(kind, skill_id, text, node_id=node_id)
