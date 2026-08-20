"""Live Claude adapter.

One class, one job: turn an `AdapterRequest` into a real Anthropic API call
and hand back an `AdapterResponse`. Everything the orchestrator considers
policy - retries across attempts, tier fallback, rollback, safe stop - lives
in the governance plane, not here. The only retry in this file is the
mechanical one for rate limits and 5xx, because "the server said try again"
is a transport concern, not a governance decision, and duplicating it upstream
would multiply the attempt budget without anybody choosing that.

API rules encoded here (all of them are 400s if you get them wrong):

  * `claude-opus-5` rejects `temperature`, `top_p` and `top_k`. They are never
    sent, for either tier, so no future edit can leak one in on the DEEP path.
  * `claude-opus-5` rejects `thinking={"type": "enabled", "budget_tokens": N}`.
    Adaptive thinking is the only supported mode, and it is on by default.
  * `max_tokens` on opus-5 caps thinking *plus* visible text, so the default
    is generous and the call streams above the timeout-safe threshold.
  * `claude-haiku-4-5` is an older generation: no `output_config.effort`, and
    a different thinking shape. The FAST path therefore sends neither.
  * Response `content` is a list whose first block may be a thinking block,
    so text is collected by filtering on `block.type`, never by index.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any, Callable

import anthropic

from keel.adapters.base import extract_json, load_env
from keel.models import (
    MODEL_FOR_TIER,
    AdapterRequest,
    AdapterResponse,
    ModelTier,
    RunMode,
)

__all__ = [
    "ClaudeAdapter",
    "AdapterError",
    "MissingAPIKey",
    "ModelRefusal",
    "ResponseTruncated",
]

# Above this, a non-streaming request risks an HTTP read timeout before the
# model is done thinking. The SDK streams and reassembles instead.
STREAM_ABOVE_MAX_TOKENS = 16_000

# Parameters the current Opus generation removed. Kept as data so the test
# suite can assert on the same list the adapter promises to never send.
FORBIDDEN_PARAMS = ("temperature", "top_p", "top_k")


class AdapterError(RuntimeError):
    """Base class for every failure this adapter raises deliberately."""


class MissingAPIKey(AdapterError):
    """No credential for live mode. Actionable message, not a KeyError."""


class ModelRefusal(AdapterError):
    """`stop_reason == "refusal"`: the model declined, HTTP 200 regardless."""


class ResponseTruncated(AdapterError):
    """`stop_reason == "max_tokens"`: output was cut mid-thought.

    Raised rather than returned. A half-written artifact that passes silently
    downstream is worse than a failed node: the node failure is visible to the
    retry and fallback machinery, the truncated file is not.
    """


class ClaudeAdapter:
    """`AgentAdapter` backed by the Anthropic API.

    Args:
        api_key: falls back to `ANTHROPIC_API_KEY` (including via `.env`).
        on_call: invoked as `on_call(request, response)` after every
            successful call so the audit log can record a replay cassette.
            May be sync or async.
        client: injection point for tests. Supplying one skips both the key
            requirement and the real SDK client construction.
        deep_max_tokens / fast_max_tokens: per-tier output budgets.
        max_retries / retry_base_delay: bound on the internal transport retry.
    """

    mode: RunMode = RunMode.LIVE

    # On the deep tier, max_tokens bounds thinking AND response text together,
    # and adaptive thinking at high effort will happily spend most of a small
    # budget before writing anything. A live run at 24k produced 2.2k
    # characters of code and then truncated, which is the failure this number
    # exists to prevent. 64k is the documented starting point for code
    # generation at this effort; the model stops when it is done, so the
    # headroom costs nothing on shorter stages.
    DEEP_MAX_TOKENS = 64_000
    FAST_MAX_TOKENS = 8_192

    def __init__(
        self,
        api_key: str | None = None,
        on_call: Callable[[AdapterRequest, AdapterResponse], Any] | None = None,
        *,
        client: Any | None = None,
        deep_max_tokens: int | None = None,
        fast_max_tokens: int | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.on_call = on_call
        self._deep_max_tokens = deep_max_tokens or self.DEEP_MAX_TOKENS
        self._fast_max_tokens = fast_max_tokens or self.FAST_MAX_TOKENS
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = max(0.0, retry_base_delay)

        if client is not None:
            self._api_key = api_key
            self._client = client
            return

        load_env()
        resolved = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        if not resolved.strip():
            raise MissingAPIKey(
                "Live mode needs an Anthropic API key. Either set "
                "ANTHROPIC_API_KEY in the environment, add it to the .env "
                "file at the project root (see .env.example), or run in "
                "replay mode with KEEL_AGENT_MODE=replay, which needs no key "
                "and costs nothing."
            )
        self._api_key = resolved
        self._client = anthropic.AsyncAnthropic(api_key=resolved)

    # -- public API --------------------------------------------------------

    async def invoke(self, request: AdapterRequest) -> AdapterResponse:
        """Run one model call and normalize the result."""
        params = self.build_params(request)

        started = time.perf_counter()
        message = await self._call_with_retry(params)
        latency = time.perf_counter() - started

        response = self._to_response(request, params, message, latency)

        if self.on_call is not None:
            result = self.on_call(request, response)
            if inspect.isawaitable(result):
                await result

        return response

    def max_tokens_for(self, tier: ModelTier) -> int:
        return self._deep_max_tokens if tier is ModelTier.DEEP else self._fast_max_tokens

    def build_params(self, request: AdapterRequest) -> dict[str, Any]:
        """Build the request body. Exposed so tests can assert on it directly."""
        tier = request.tier
        params: dict[str, Any] = {
            "model": MODEL_FOR_TIER[tier],
            "max_tokens": self.max_tokens_for(tier),
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            params["system"] = request.system

        output_config: dict[str, Any] = {}

        if tier is ModelTier.DEEP:
            # Adaptive is the only accepted shape on opus-5; budget_tokens 400s.
            params["thinking"] = {"type": "adaptive"}
            output_config["effort"] = "high"
        # FAST is an older-generation model: no thinking block, no effort.

        if request.json_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": _strict_schema(request.json_schema),
            }

        if output_config:
            params["output_config"] = output_config

        return params

    # -- internals ---------------------------------------------------------

    async def _call_with_retry(self, params: dict[str, Any]) -> Any:
        """Bounded retry for rate limits and 5xx only.

        Anything else - a 400 from a bad schema, an auth failure, a refusal -
        propagates untouched so the orchestrator's own retry, fallback and
        rollback machinery is the single place that decides what happens next.
        """
        attempt = 0
        while True:
            try:
                return await self._call(params)
            except Exception as exc:  # narrowed immediately below
                if not _is_retryable(exc) or attempt >= self._max_retries:
                    raise
                delay = self._retry_base_delay * (2**attempt)
                attempt += 1
                if delay > 0:
                    await asyncio.sleep(delay)

    async def _call(self, params: dict[str, Any]) -> Any:
        if params["max_tokens"] > STREAM_ABOVE_MAX_TOKENS:
            async with self._client.messages.stream(**params) as stream:
                return await stream.get_final_message()
        return await self._client.messages.create(**params)

    def _to_response(
        self,
        request: AdapterRequest,
        params: dict[str, Any],
        message: Any,
        latency: float,
    ) -> AdapterResponse:
        stop_reason = getattr(message, "stop_reason", None)

        # Checked before touching content: on a refusal the content list is
        # empty or partial, and reading it first would produce a plausible
        # looking empty artifact instead of a loud failure.
        if stop_reason == "refusal":
            raise ModelRefusal(
                f"{params['model']} refused node {request.node_id!r} "
                f"(skill {request.skill_id!r}){_refusal_detail(message)}"
            )

        text = collect_text(message)

        if stop_reason == "max_tokens":
            raise ResponseTruncated(
                f"{params['model']} hit max_tokens ({params['max_tokens']}) on "
                f"node {request.node_id!r}; produced {len(text)} characters of "
                "text. Raise the tier budget or split the node."
            )

        usage = getattr(message, "usage", None)
        parsed = extract_json(text) if request.json_schema is not None else None

        return AdapterResponse(
            text=text,
            parsed=parsed,
            model=getattr(message, "model", None) or params["model"],
            input_tokens=_int_attr(usage, "input_tokens"),
            output_tokens=_int_attr(usage, "output_tokens"),
            latency_seconds=latency,
            from_replay=False,
        )


def collect_text(message: Any) -> str:
    """Join every `text` block, in order.

    Never `content[0].text`: with adaptive thinking on opus-5 the first block
    is routinely a thinking block, which has no `.text` at all.
    """
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        value = getattr(block, "text", "")
        if value:
            parts.append(value)
    return "\n".join(parts).strip()


# JSON Schema keywords the structured-output engine rejects outright. A
# schema carrying any of them returns a 400 naming the offending keyword, so
# they are stripped here rather than in every caller's schema.
#
# The constraint is still worth expressing at the authoring site, because it
# documents intent and because the value is validated on the way back in
# (`EngineeringProblem` clamps confidence into 0..1 regardless). Dropping the
# keyword loses enforcement at generation time, not enforcement overall.
UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Coerce a schema into the shape structured output demands.

    `additionalProperties: false` and an explicit `required` list are not
    optional. Filling them in here means callers can write the interesting
    half of the schema and still get a valid request. Unsupported validation
    keywords are dropped for the same reason: the caller should not have to
    memorise the engine's subset of JSON Schema.
    """
    result = {k: v for k, v in schema.items() if k not in UNSUPPORTED_SCHEMA_KEYWORDS}
    if result.get("type", "object") == "object":
        result.setdefault("type", "object")
        result["additionalProperties"] = False
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["properties"] = {
                name: _strict_schema(sub) if isinstance(sub, dict) else sub
                for name, sub in properties.items()
            }
            result.setdefault("required", list(properties))
    elif result.get("type") == "array":
        items = result.get("items")
        if isinstance(items, dict):
            result["items"] = _strict_schema(items)
    return result


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return int(getattr(exc, "status_code", 0) or 0) >= 500
    return False


def _refusal_detail(message: Any) -> str:
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    return f": category={category}" if category else ""


def _int_attr(obj: Any, name: str) -> int:
    try:
        return int(getattr(obj, name, 0) or 0)
    except (TypeError, ValueError):
        return 0
