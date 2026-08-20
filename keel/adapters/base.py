"""Shared helpers for the adapter layer.

Both adapters need two small things and neither is worth a dependency:
reading `.env` so a reviewer can drop an API key in a file instead of
exporting it, and pulling JSON out of prose because models wrap structured
answers in fenced code blocks even when a schema was requested.

Nothing here imports from the rest of `keel` beyond `models`, so the adapter
package stays loadable before the governance plane exists.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, MutableMapping

__all__ = ["project_root", "load_env", "extract_json"]

# keel/adapters/base.py -> keel/adapters -> keel -> <project root>
_THIS_FILE = Path(__file__).resolve()

_FENCE_RE = re.compile(
    r"```(?:json|jsonc|json5)?\s*(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def project_root() -> Path:
    """Directory that holds `.env`, `runs/` and the `keel` package itself."""
    return _THIS_FILE.parents[2]


def load_env(
    path: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Load a `.env` file into the process environment.

    Deliberately does not overwrite variables that are already set: a real
    exported `ANTHROPIC_API_KEY` (or a CI secret) must always win over a file
    that happens to be sitting in the checkout. That ordering is also what
    makes it safe to call this on every adapter construction.

    Hand-rolled rather than pulling in python-dotenv, because the whole
    grammar we need is `KEY=VALUE`, `#` comments and optional quotes, and a
    public interview repo is better off with one less dependency to vet.

    Returns everything parsed out of the file, including keys that were left
    alone because the environment already had them.
    """
    target = Path(path) if path is not None else project_root() / ".env"
    env = os.environ if environ is None else environ

    parsed: dict[str, str] = {}
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        # A missing .env is the normal case in replay mode, not an error.
        return parsed

    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        if entry.startswith("export "):
            entry = entry[len("export ") :].lstrip()

        key, _, value = entry.partition("=")
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Trailing comment, only for unquoted values: `KEY=v  # note`.
            value = value.split(" #", 1)[0].rstrip()

        parsed[key] = value
        if key not in env:
            env[key] = value

    return parsed


def extract_json(text: str | None) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from a model response.

    Structured output makes the whole response a JSON document, but the same
    prompt run without a schema (or through a model that decided to be
    chatty) comes back as ```json ... ``` inside prose. The orchestrator
    should not care which happened, so try, in order: the whole string, any
    fenced block, then the first balanced `{...}` span.

    Returns None rather than raising. A parse failure is information the
    caller's exit gate should act on, not an exception for a helper to throw.
    """
    if not text:
        return None

    candidates: list[str] = [text.strip()]
    candidates.extend(match.group("body").strip() for match in _FENCE_RE.finditer(text))

    span = _first_object_span(text)
    if span is not None:
        candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value

    return None


def _first_object_span(text: str) -> str | None:
    """Return the first balanced `{...}` substring, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None
