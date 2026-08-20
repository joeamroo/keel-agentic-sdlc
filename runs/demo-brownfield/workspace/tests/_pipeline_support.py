"""Shared fixtures for the delivery-pipeline suite.

Every test gets a brand new SQLite file (LINKS_DB_PATH), a freshly imported
application object (so process-level configuration is re-read and the
in-process rate limiter starts with an empty bucket map) and a stubbed name
resolver.  No test touches the network, no test sleeps, no test shares a
database or limiter state with another test, and no test depends on ordering.

This module is deliberately not a conftest.py: the repository already has one
and two conftest modules in the same directory are not possible.  Fixtures are
imported by name into each test module instead.
"""
from __future__ import annotations

import importlib
import ipaddress
import secrets
import socket
import sqlite3
import string
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for _candidate in (ROOT, ROOT / "src"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

API_KEY_HEADER = "X-API-Key"
PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
BASE62 = string.ascii_letters + string.digits
CODE_LENGTH = 7

#: Exactly the environment variable names the service reads.  Rate limiting is
#: off by default so that only the rate limit tests pay for it.
BASE_ENV: Dict[str, str] = {
    "LINKS_BASE_URL": "http://testserver",
    "LINKS_CODE_LENGTH": str(CODE_LENGTH),
    "LINKS_CODE_MAX_ATTEMPTS": "5",
    "LINKS_MAX_URL_LENGTH": "2048",
    "LINKS_DEFAULT_TTL_DAYS": "30",
    "LINKS_RATE_LIMIT_ENABLED": "false",
    "LINKS_RATE_LIMIT_MAX": "10",
    "LINKS_RATE_LIMIT_WINDOW_SECONDS": "60",
    "LINKS_TRUST_FORWARDED_FOR": "false",
    "LINKS_STATS_DEFAULT_LIMIT": "50",
    "LINKS_STATS_MAX_LIMIT": "500",
    "LINKS_DNS_RESOLUTION_ENABLED": "true",
    "LINKS_LOG_LEVEL": "WARNING",
    "SHORTENER_API_KEYS": "",
}

APP_MODULE_CANDIDATES = (
    "app.main",
    "app",
    "main",
    "src.app.main",
    "src.main",
)

_APP_MODULE_NAME: Optional[str] = None
_UNSET = object()


# ---------------------------------------------------------------- app import
def _purge_root(root: str) -> None:
    for name in list(sys.modules):
        if name == root or name.startswith(root + "."):
            del sys.modules[name]


def _get_app_object(module: Any):
    obj = getattr(module, "app", None)
    if obj is not None and hasattr(obj, "router"):
        return obj
    factory = getattr(module, "create_app", None)
    if callable(factory):
        try:
            built = factory()
        except Exception:  # pragma: no cover - depends on layout
            return None
        if built is not None and hasattr(built, "router"):
            return built
    return None


def _import_app_module():
    global _APP_MODULE_NAME
    problems: List[str] = []
    names = [_APP_MODULE_NAME] if _APP_MODULE_NAME else list(APP_MODULE_CANDIDATES)
    for name in names:
        _purge_root(name.split(".")[0])
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - depends on layout
            problems.append("{0}: {1}: {2}".format(name, type(exc).__name__, exc))
            continue
        if _get_app_object(module) is None:
            problems.append("{0}: no ASGI `app` and no usable create_app()".format(name))
            continue
        _APP_MODULE_NAME = name
        return module
    raise RuntimeError(
        "Could not import the ASGI application. Tried:\n  " + "\n  ".join(problems)
    )


# --------------------------------------------------------------- DNS stubbing
class FakeDns:
    """Deterministic stand-in for the system resolver. No test hits the network."""

    def __init__(self) -> None:
        self.map: Dict[str, List[str]] = {}
        self.failures = set()
        self.lookups: List[str] = []

    def set(self, host: str, addresses) -> None:
        self.map[host.strip("[]").lower()] = list(addresses)

    def fail(self, host: str) -> None:
        self.failures.add(host.strip("[]").lower())

    def _addresses_for(self, host: str) -> List[str]:
        key = (host or "").strip("[]").lower()
        self.lookups.append(key)
        if key in self.failures:
            raise socket.gaierror(-2, "Name or service not known")
        if key in self.map:
            return list(self.map[key])
        try:
            ipaddress.ip_address(key)
            return [key]
        except ValueError:
            return [PUBLIC_IPV4]

    def getaddrinfo(self, host, port=0, family=0, type=0, proto=0, flags=0):
        try:
            portnum = int(port)
        except (TypeError, ValueError):
            portnum = 80
        out = []
        for addr in self._addresses_for(host):
            parsed = ipaddress.ip_address(addr)
            if parsed.version == 4:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, portnum)))
            else:
                out.append(
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, portnum, 0, 0))
                )
        return out

    def gethostbyname(self, host):
        for addr in self._addresses_for(host):
            if ipaddress.ip_address(addr).version == 4:
                return addr
        raise socket.gaierror(-2, "Name or service not known")

    def gethostbyname_ex(self, host):
        addrs = [
            a for a in self._addresses_for(host) if ipaddress.ip_address(a).version == 4
        ]
        return (host, [], addrs)


@pytest.fixture
def stub_dns(monkeypatch):
    resolver = FakeDns()
    # A decimal IPv4 literal is what the real resolver would decode; map it so a
    # service that leans on the resolver and one that parses the literal itself
    # are both required to block it.
    resolver.set("2130706433", ["127.0.0.1"])
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", resolver.gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", resolver.gethostbyname_ex)
    return resolver


# ------------------------------------------------------------------- harness
@dataclass
class Harness:
    client: TestClient
    db_path: str
    module: Any
    env: Dict[str, str]

    # ---- HTTP ----
    def create(
        self,
        url=None,
        *,
        expires_at=None,
        api_key=_UNSET,
        headers=None,
        payload=None,
    ):
        body = payload
        if body is None:
            body = {}
            if url is not None:
                body["url"] = url
            if expires_at is not None:
                body["expires_at"] = expires_at
        hdrs = dict(headers or {})
        if api_key is not _UNSET:
            hdrs[API_KEY_HEADER] = api_key
        return self.client.post("/api/links", json=body, headers=hdrs)

    def visit(self, code, headers=None, api_key=_UNSET):
        hdrs = dict(headers or {})
        if api_key is not _UNSET:
            hdrs[API_KEY_HEADER] = api_key
        try:
            return self.client.get(
                "/" + str(code), headers=hdrs, follow_redirects=False
            )
        except TypeError:  # pragma: no cover - very old TestClient
            return self.client.get("/" + str(code), headers=hdrs, allow_redirects=False)

    def stats(self, code, headers=None, api_key=_UNSET, **params):
        hdrs = dict(headers or {})
        if api_key is not _UNSET:
            hdrs[API_KEY_HEADER] = api_key
        return self.client.get(
            "/api/links/{0}/stats".format(code), params=params or None, headers=hdrs
        )

    def health(self, api_key=_UNSET):
        hdrs = {}
        if api_key is not _UNSET:
            hdrs[API_KEY_HEADER] = api_key
        return self.client.get("/health", headers=hdrs)

    # ---- database ----
    def _rows(self, sql, params=()):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            raise AssertionError(
                "sqlite error {0!r} against {1}: does the service honour "
                "LINKS_DB_PATH and create its schema?".format(str(exc), self.db_path)
            )
        finally:
            con.close()

    def link_rows(self):
        return self._rows("SELECT * FROM links ORDER BY id")

    def click_rows(self):
        return self._rows("SELECT * FROM clicks ORDER BY id")

    def link_count(self) -> int:
        return len(self.link_rows())

    def click_count(self) -> int:
        return len(self.click_rows())

    def columns(self, table: str):
        return [r["name"] for r in self._rows("PRAGMA table_info({0})".format(table))]

    def table_names(self):
        return [
            r["name"]
            for r in self._rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]

    def set_expiry(self, code: str, when: datetime) -> None:
        stamp = when.astimezone(timezone.utc).strftime(TS_FORMAT)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(
                "UPDATE links SET expires_at = ? WHERE code = ?", (stamp, code)
            )
            con.commit()
            assert cur.rowcount == 1, "no links row with code {0!r}".format(code)
        finally:
            con.close()

    def disk_bytes(self) -> bytes:
        blob = b""
        for path in sorted(Path(self.db_path).parent.glob("*")):
            if path.is_file():
                blob += path.read_bytes()
        return blob


def link_target(row: Dict[str, Any]) -> Any:
    """Stored destination of a links row (implementation column is ``url``)."""
    if "url" in row:
        return row["url"]
    return row.get("target_url")


@pytest.fixture
def make_app(tmp_path, monkeypatch, stub_dns):
    stack = ExitStack()
    counter = {"n": 0}

    def factory(**overrides) -> Harness:
        counter["n"] += 1
        db_path = tmp_path / "links-{0}.db".format(counter["n"])
        env = dict(BASE_ENV)
        env["LINKS_DB_PATH"] = str(db_path)
        env.update({k: str(v) for k, v in overrides.items()})
        for name in list(os.environ):
            if (name.startswith("LINKS_") or name.startswith("SHORTENER_")) and name not in env:
                monkeypatch.delenv(name, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        module = _import_app_module()
        asgi = _get_app_object(module)
        assert asgi is not None, "application module exposes no ASGI app"
        try:
            client = TestClient(
                asgi,
                base_url="http://testserver",
                raise_server_exceptions=False,
                follow_redirects=False,
            )
        except TypeError:  # pragma: no cover
            client = TestClient(asgi, base_url="http://testserver")
        stack.enter_context(client)
        return Harness(client=client, db_path=str(db_path), module=module, env=env)

    try:
        yield factory
    finally:
        stack.close()


@pytest.fixture
def service(make_app) -> Harness:
    return make_app()


# ------------------------------------------------- forcing code collisions
def _random_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(BASE62) for _ in range(length))


class CodeStub:
    """Stands in for the service's short code generator."""

    def __init__(self, codes, length: int = CODE_LENGTH):
        self.queue = list(codes)
        self.length = length
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.queue:
            return self.queue.pop(0)
        return _random_code(self.length)


class CharStub:
    """Fallback: feed the characters of the wanted codes to secrets.choice."""

    def __init__(self, codes, length: int, original):
        self.chars: List[str] = []
        for code in codes:
            self.chars.extend(list(code))
        self.length = max(int(length), 1)
        self.consumed = 0
        self._original = original

    def __call__(self, sequence):
        if self.chars:
            self.consumed += 1
            return self.chars.pop(0)
        return self._original(sequence)

    @property
    def calls(self) -> int:
        return self.consumed // self.length


_GEN_HINTS = ("gen", "make", "new", "mint", "alloc", "random")
_BAD_TOKENS = (
    "max",
    "attempt",
    "length",
    "len",
    "pattern",
    "alphabet",
    "chars",
    "error",
    "status",
)


def _find_code_generators(module):
    root = module.__name__.split(".")[0]
    found = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name != root and not name.startswith(root + "."):
            continue
        for attr in dir(mod):
            low = attr.lower()
            if "code" not in low:
                continue
            if any(bad in low for bad in _BAD_TOKENS):
                continue
            if not any(hint in low for hint in _GEN_HINTS):
                continue
            value = getattr(mod, attr, None)
            if not callable(value):
                continue
            owner = getattr(value, "__module__", "") or ""
            if owner != root and not owner.startswith(root + "."):
                continue
            found.append((mod, attr))
    return found


@pytest.fixture
def force_codes(monkeypatch):
    """Make the next short codes the service produces be exactly ``codes``."""

    def _force(harness: Harness, codes):
        length = int(harness.env.get("LINKS_CODE_LENGTH", str(CODE_LENGTH)))
        targets = _find_code_generators(harness.module)
        if targets:
            stub = CodeStub(codes, length)
            for mod, attr in targets:
                monkeypatch.setattr(mod, attr, stub)
            return stub
        stub = CharStub(codes, length, secrets.choice)
        monkeypatch.setattr(secrets, "choice", stub)
        return stub

    return _force


# ---------------------------------------------------------------- assertions
def assert_error_envelope(response) -> Dict[str, Any]:
    ctype = response.headers.get("content-type", "")
    assert "application/json" in ctype, (
        "error responses must be JSON, content-type was " + repr(ctype)
    )
    body = response.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), (
        "error body must be {'error': {'code': ..., 'message': ...}}, got " + repr(body)
    )
    error = body["error"]
    assert isinstance(error.get("code"), str) and error["code"], repr(error)
    assert isinstance(error.get("message"), str) and error["message"], repr(error)
    return error


def parse_ts(value: str) -> datetime:
    assert isinstance(value, str), "timestamp must be a string, got " + repr(value)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
