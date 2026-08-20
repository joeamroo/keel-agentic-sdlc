"""Shared fixtures for the link shortener suite.

Every test gets: a brand new SQLite file (LINKS_DB_PATH), a freshly imported
application module (so module level configuration is re-read and the in-process
rate limiter state starts empty), and a stubbed name resolver so that no test
ever touches the network.
"""
from __future__ import annotations

import importlib
import ipaddress
import os
import secrets
import socket
import sqlite3
import string
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
PUBLIC_IP = "93.184.216.34"
BASE62 = string.ascii_letters + string.digits

# Exactly the names from the design's configuration list. No aliases.
BASE_ENV: Dict[str, str] = {
    "LINKS_BASE_URL": "http://testserver",
    "LINKS_DEFAULT_TTL_DAYS": "30",
    "LINKS_MAX_URL_LENGTH": "2048",
    "LINKS_CODE_LENGTH": "7",
    "LINKS_CODE_MAX_ATTEMPTS": "5",
    "LINKS_RATE_LIMIT_MAX": "10",
    "LINKS_RATE_LIMIT_WINDOW_SECONDS": "60",
    # Most tests create many links; the design sanctions disabling the limiter
    # through this exact flag. The rate limit tests switch it back on.
    "LINKS_RATE_LIMIT_ENABLED": "false",
    "LINKS_TRUST_FORWARDED_FOR": "false",
    "LINKS_STATS_DEFAULT_LIMIT": "50",
    "LINKS_STATS_MAX_LIMIT": "500",
    "LINKS_DNS_RESOLUTION_ENABLED": "true",
    "LINKS_LOG_LEVEL": "WARNING",
}

APP_MODULE_CANDIDATES = (
    "app.main",
    "app.app",
    "main",
    "app",
    "src.main",
    "src.app.main",
    "api.main",
    "server.main",
    "service.main",
    "shortener.main",
    "url_shortener.main",
    "urlshortener.main",
    "links.main",
    "application",
    "server",
)

_APP_MODULE_NAME: Optional[str] = None


# --------------------------------------------------------------------------
# application loading
# --------------------------------------------------------------------------
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
        except Exception:
            return None
        if built is not None and hasattr(built, "router"):
            return built
    return None


def _import_app_module():
    global _APP_MODULE_NAME
    errors: List[str] = []
    names = [_APP_MODULE_NAME] if _APP_MODULE_NAME else list(APP_MODULE_CANDIDATES)
    for name in names:
        _purge_root(name.split(".")[0])
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - depends on layout
            errors.append("{0}: {1}: {2}".format(name, type(exc).__name__, exc))
            continue
        if _get_app_object(module) is None:
            errors.append("{0}: no FastAPI `app` and no usable `create_app()`".format(name))
            continue
        _APP_MODULE_NAME = name
        return module
    raise RuntimeError(
        "Could not import the ASGI application. Tried:\n  " + "\n  ".join(errors)
    )


# --------------------------------------------------------------------------
# stub resolver: no test may hit real DNS
# --------------------------------------------------------------------------
class FakeResolver:
    def __init__(self) -> None:
        self.map: Dict[str, List[str]] = {}
        self.failures = set()
        self.lookups: List[str] = []

    def set(self, host: str, addresses) -> None:
        self.map[host.strip("[]").lower()] = list(addresses)

    def fail(self, host: str) -> None:
        self.failures.add(host.strip("[]").lower())

    def _addresses_for(self, host: str) -> List[str]:
        h = (host or "").strip("[]").lower()
        self.lookups.append(h)
        if h in self.failures:
            raise socket.gaierror(-2, "Name or service not known")
        if h in self.map:
            return list(self.map[h])
        try:
            ipaddress.ip_address(h)
            return [h]
        except ValueError:
            return [PUBLIC_IP]

    def getaddrinfo(self, host, port=0, family=0, type=0, proto=0, flags=0):
        out = []
        try:
            portnum = int(port)
        except (TypeError, ValueError):
            portnum = 80
        for addr in self._addresses_for(host):
            ip = ipaddress.ip_address(addr)
            if ip.version == 4:
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
        addrs = [a for a in self._addresses_for(host) if ipaddress.ip_address(a).version == 4]
        return (host, [], addrs)


@pytest.fixture
def fake_dns(monkeypatch):
    resolver = FakeResolver()
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", resolver.gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", resolver.gethostbyname_ex)
    return resolver


# --------------------------------------------------------------------------
# the app under test
# --------------------------------------------------------------------------
@dataclass
class AppUnderTest:
    client: TestClient
    db_path: str
    module: Any

    # ---- HTTP helpers ----
    def create(self, url=None, expires_at=None, headers=None, payload=None):
        if payload is None:
            payload = {}
            if url is not None:
                payload["url"] = url
            if expires_at is not None:
                payload["expires_at"] = expires_at
        return self.client.post("/api/links", json=payload, headers=headers or {})

    def visit(self, code, headers=None):
        try:
            return self.client.get(
                "/" + str(code), headers=headers or {}, follow_redirects=False
            )
        except TypeError:  # pragma: no cover - very old TestClient
            return self.client.get(
                "/" + str(code), headers=headers or {}, allow_redirects=False
            )

    def stats(self, code, **params):
        return self.client.get(
            "/api/links/{0}/stats".format(code), params=params or None
        )

    # ---- database helpers ----
    def _rows(self, sql, params=()):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            raise AssertionError(
                "sqlite error {0!r} against {1}: does the service honour "
                "LINKS_DB_PATH and create its schema at startup?".format(
                    str(exc), self.db_path
                )
            )
        finally:
            con.close()

    def link_rows(self):
        return self._rows("SELECT * FROM links ORDER BY id")

    def click_rows(self):
        return self._rows("SELECT * FROM clicks ORDER BY id")

    def columns(self, table):
        return [r["name"] for r in self._rows("PRAGMA table_info({0})".format(table))]

    def set_expiry(self, code, when: datetime) -> None:
        stamp = when.astimezone(timezone.utc).strftime(TS_FORMAT)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(
                "UPDATE links SET expires_at = ? WHERE code = ?", (stamp, code)
            )
            con.commit()
            assert cur.rowcount == 1, "no links row with code {0!r} to expire".format(code)
        finally:
            con.close()

    def disk_bytes(self) -> bytes:
        blob = b""
        base = Path(self.db_path)
        for path in sorted(base.parent.glob("*")):
            if path.is_file():
                blob += path.read_bytes()
        return blob


@pytest.fixture
def app_factory(tmp_path, monkeypatch, fake_dns):
    stack = ExitStack()
    counter = {"n": 0}

    def factory(**overrides) -> AppUnderTest:
        counter["n"] += 1
        db_path = tmp_path / "links-{0}.db".format(counter["n"])
        env = dict(BASE_ENV)
        env["LINKS_DB_PATH"] = str(db_path)
        env.update({k: str(v) for k, v in overrides.items()})
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
        return AppUnderTest(client=client, db_path=str(db_path), module=module)

    try:
        yield factory
    finally:
        stack.close()


@pytest.fixture
def app(app_factory) -> AppUnderTest:
    return app_factory()


# --------------------------------------------------------------------------
# forcing short code collisions
# --------------------------------------------------------------------------
def _random_code(length: int = 7) -> str:
    return "".join(secrets.choice(BASE62) for _ in range(length))


class CodeStub:
    """Stands in for the service's short code generator."""

    mode = "named"

    def __init__(self, codes, length: int = 7):
        self.queue = list(codes)
        self.length = length
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.queue:
            return self.queue.pop(0)
        return _random_code(self.length)


class CharStub:
    """Fallback: feed the characters of the desired codes to secrets.choice."""

    mode = "secrets.choice"

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


_HINTS = ("gen", "make", "new", "random", "build", "mint", "alloc")
_BAD = ("max", "attempt", "length", "len", "pattern", "alphabet", "chars", "error", "status")


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
            if any(bad in low for bad in _BAD):
                continue
            if not any(hint in low for hint in _HINTS):
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
    """Make the next short codes produced by the service be exactly `codes`."""

    def _force(app_under_test: AppUnderTest, codes):
        length = int(os.environ.get("LINKS_CODE_LENGTH", "7"))
        targets = _find_code_generators(app_under_test.module)
        if targets:
            stub = CodeStub(codes, length)
            for mod, attr in targets:
                monkeypatch.setattr(mod, attr, stub)
            return stub
        stub = CharStub(codes, length, secrets.choice)
        monkeypatch.setattr(secrets, "choice", stub)
        return stub

    return _force


# --------------------------------------------------------------------------
# assorted helpers used by the tests
# --------------------------------------------------------------------------
def parse_ts(value: str) -> datetime:
    assert isinstance(value, str), "timestamp must be a string, got {0!r}".format(value)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def assert_error_envelope(response) -> Dict[str, Any]:
    assert "application/json" in response.headers.get("content-type", ""), (
        "error responses must be JSON, got "
        + repr(response.headers.get("content-type"))
    )
    body = response.json()
    assert isinstance(body, dict) and "error" in body, (
        "error body must be {'error': {...}}, got " + repr(body)
    )
    err = body["error"]
    assert isinstance(err, dict), "error must be an object, got " + repr(err)
    assert isinstance(err.get("code"), str) and err["code"], (
        "error.code must be a non-empty string, got " + repr(err)
    )
    assert isinstance(err.get("message"), str) and err["message"], (
        "error.message must be a non-empty string, got " + repr(err)
    )
    return err
