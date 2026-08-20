"""Serving one SDLC stage as a real A2A agent.

The executor here contains no business logic and never will. It takes a
`handler` coroutine, calls it, and translates the dict it returns into A2A
lifecycle calls. That split is deliberate: the interesting part of a stage is
what it decides, and that should be unit testable without a task store, an
event queue or a socket. Everything protocol-shaped lives in this file;
everything judgement-shaped lives in the handler.

Two SDK facts are load bearing and are the reason this file looks the way it
does:

1. `a2a.types` are protobuf messages, not pydantic models.
2. When `context.current_task` is None the executor must enqueue a `pb.Task`
   before any status update, or the SDK raises `InvalidAgentResponseError`
   ("Agent should enqueue Task before TaskStatusUpdateEvent"). The task is the
   thing status updates refer to, so it has to exist first.

The handler contract is a dict with a `status` key:

    {"status": "completed", "artifacts": [{"name": ..., "content": ...}]}
    {"status": "input_required", "message": "which auth model?"}
    {"status": "failed", "error": "..."}
    {"status": "rejected", "reason": "..."}

Those four map onto the A2A states the governance plane cares about. A stage
that cannot proceed parks in INPUT_REQUIRED rather than failing, which is the
difference between asking a human and losing the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCard, Part
from a2a.types import TaskState as PbTaskState
from a2a.types import a2a_pb2 as pb

from keel.a2a.cards import AGENT_CARD_PATH, AUTH_TOKEN_ENV
from keel.models import TaskState

# A stage's actual logic: prompt text in, result dict out.
StageHandler = Callable[[str], Awaitable[Mapping[str, Any]]]

# The four outcomes a handler is allowed to report. Anything else is a bug in
# the handler and is surfaced as a failure rather than silently coerced.
_HANDLER_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.INPUT_REQUIRED,
        TaskState.FAILED,
        TaskState.REJECTED,
    }
)


@dataclass(slots=True)
class StageResult:
    """A handler's dict, validated and normalized.

    Parsed once here so the executor and the in-process transport agree on what
    a handler said. Without a single parser the two paths drift and the unit
    tests stop predicting the behaviour of the real mesh.
    """

    state: TaskState
    artifacts: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""


def _artifact_content(value: Any) -> str:
    """Coerce artifact content to text.

    Non-strings are JSON encoded with sorted keys because `Artifact.sha` hashes
    this content and lineage staleness depends on the hash being stable across
    runs. Dict ordering must not decide whether downstream work is invalidated.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def parse_stage_result(raw: Mapping[str, Any] | Any) -> StageResult:
    """Validate a handler's return value. Raises ValueError on anything odd."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"stage handler must return a mapping, got {type(raw).__name__}")

    status = str(raw.get("status", "")).strip().lower()
    try:
        state = TaskState(status)
    except ValueError as exc:
        raise ValueError(f"stage handler returned unknown status {status!r}") from exc
    if state not in _HANDLER_STATES:
        raise ValueError(f"stage handler may not report {status!r}")

    artifacts: list[tuple[str, str]] = []
    raw_artifacts = raw.get("artifacts") or []
    if not isinstance(raw_artifacts, (list, tuple)):
        raise ValueError("stage handler 'artifacts' must be a list")
    for index, item in enumerate(raw_artifacts):
        if not isinstance(item, Mapping):
            raise ValueError("each artifact must be a mapping with name and content")
        name = str(item.get("name") or f"artifact-{index}")
        artifacts.append((name, _artifact_content(item.get("content", ""))))

    # Accept whichever key the handler used. The three names read naturally in
    # their own state and there is no value in making handlers remember which.
    message = ""
    for key in ("message", "error", "reason"):
        value = raw.get(key)
        if value:
            message = str(value)
            break

    return StageResult(state=state, artifacts=artifacts, message=message)


class StageAgentExecutor(AgentExecutor):
    """Adapts a stage handler to the A2A executor interface.

    Holds no stage knowledge. Give it a different handler and it serves a
    different stage, which is what makes one server module enough for all seven.
    """

    def __init__(self, handler: StageHandler, *, skill_id: str = "") -> None:
        self.handler = handler
        self.skill_id = skill_id

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The Task must be on the queue before any status update refers to it.
        if context.current_task is None:
            await event_queue.enqueue_event(
                pb.Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=pb.TaskStatus(state=PbTaskState.TASK_STATE_SUBMITTED),
                )
            )

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        text = context.get_user_input() or ""

        try:
            raw = await self.handler(text)
            result = parse_stage_result(raw)
        except asyncio.CancelledError:
            # A cancel is a control-plane decision, not a stage failure. Let it
            # propagate so the SDK runs its own cancellation path.
            raise
        except Exception as exc:  # noqa: BLE001 - a stage crash is a task failure
            await updater.failed(
                updater.new_agent_message(
                    [Part(text=f"{type(exc).__name__}: {exc}")]
                )
            )
            return

        message = (
            updater.new_agent_message([Part(text=result.message)])
            if result.message
            else None
        )

        if result.state is TaskState.COMPLETED:
            for name, content in result.artifacts:
                await updater.add_artifact([Part(text=content)], name=name)
            await updater.complete(message)
        elif result.state is TaskState.INPUT_REQUIRED:
            await updater.requires_input(message)
        elif result.state is TaskState.REJECTED:
            await updater.reject(message)
        else:
            await updater.failed(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Honour a cancel request. This is the safe-stop path."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def _bearer_guard(app: FastAPI) -> None:
    """Reject JSON-RPC calls that do not carry the expected bearer token.

    Card discovery is exempt on purpose. A client learns that it must
    authenticate by reading the card, so requiring the token to fetch the card
    would make the mesh undiscoverable.
    """

    @app.middleware("http")
    async def _require_bearer(request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/.well-known/"):
            return await call_next(request)

        expected = os.environ.get(AUTH_TOKEN_ENV, "")
        presented = request.headers.get("authorization", "")
        if not expected or presented != f"Bearer {expected}":
            return JSONResponse(
                {"error": "missing or invalid bearer token"}, status_code=401
            )
        return await call_next(request)


def build_app(
    card: AgentCard,
    executor: AgentExecutor,
    *,
    require_auth: bool = False,
) -> FastAPI:
    """Wire one stage agent into a FastAPI app.

    `require_auth` enforces the bearer scheme the card advertises. It is opt-in
    because a card can honestly declare a scheme it does not police in a local
    dev mesh, and pretending otherwise would be the kind of security theatre
    this project is supposed to avoid.
    """
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    if require_auth:
        _bearer_guard(app)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )
    return app


def pick_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an unused port.

    Tests bind ephemeral ports so a developer running the suite does not
    collide with a mesh already running on the conventional ports.
    """
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@dataclass(slots=True)
class ServedAgent:
    """A running stage agent and the handle needed to stop it again."""

    card: AgentCard
    server: uvicorn.Server
    thread: threading.Thread
    base_url: str

    async def wait_until_ready(self, timeout: float = 10.0) -> dict[str, Any]:
        """Poll the card route until the server answers.

        Readiness is defined as "serves its card", because that is exactly the
        precondition a client needs before it can do anything else.
        """
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=5.0) as http:
            while time.monotonic() < deadline:
                try:
                    response = await http.get(f"{self.base_url}{AGENT_CARD_PATH}")
                    if response.status_code == 200:
                        return response.json()
                except Exception as exc:  # noqa: BLE001 - still booting
                    last_error = exc
                await asyncio.sleep(0.05)
        raise RuntimeError(
            f"agent at {self.base_url} never served its card: {last_error}"
        )

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=timeout)


def serve(
    card: AgentCard,
    executor: AgentExecutor,
    port: int,
    *,
    host: str = "127.0.0.1",
    require_auth: bool = False,
) -> ServedAgent:
    """Run a stage agent on a background thread and return a handle.

    A thread rather than a subprocess so a scenario run can boot the whole mesh
    in one process and still be killed cleanly, and so tests can await
    readiness without shelling out. The caller is responsible for building the
    card with a `base_url` matching `host` and `port`.
    """
    app = build_app(card, executor, require_auth=require_auth)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return ServedAgent(
        card=card,
        server=server,
        thread=thread,
        base_url=f"http://{host}:{port}",
    )
