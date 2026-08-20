"""Shared fixtures for the URL shortener suite.

Every test gets a freshly constructed application (so the in-process rate
limiter starts with empty buckets) backed by its own SQLite file underneath
``tmp_path``, plus a stubbed name resolver so that no test ever performs DNS or
network I/O.  Nothing is shared between tests and nothing sleeps.
"""
from __future__ import annotations

import ipaddress
import itertools
import os
import socket
import sqlite3
import sys
import tempfile
import time as real_time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# app.main builds an application at import time and that construction touches
# LINKS_DB_PATH; point it at a throwaway directory before importing.
_IMPORT_DIR = tempfile.mkdtemp(prefix="links-import-")
os.environ["LINKS_DB_PATH"] = os.path.join(_IMPORT_DIR, "import.db")

import app.codes as codes_module  # noqa: E402
import app.main as main_module  # noqa: E402
import app.ratelimit as ratelimit_module  # noqa: E402
from app.main import create_app  # noqa: E402

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
DEFAULT_HOST = "203.0.113.10"
OTHER_HOST = "198.51.100.23"
PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"

LINK_COLUMNS = ["id", "code", "url", "created_at", "expires_at"]
CLICK_COLUMNS = ["id", "link_id", "clicked_at", "referrer", "user_agent"]


# ---------------------------------------------------------------------------
# environment hygiene
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove every service variable so ambient configuration cannot leak in."""
    for name in list(os.environ):
        if name.startswith("LINKS_") or name == "SHORTENER_API_KEYS":
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_outbound_connections(monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("the service must never open an outbound connection")

    monkeypatch.setattr(socket, "create_connection", refuse, raising=False)


# ---------------------------------------------------------------------------
# deterministic resolver
# ---------------------------------------------------------------------------
class FakeResolver:
    """Stand-in for :func:`socket.getaddrinfo` with no network access."""

    def __init__(self) -> None:
        self.map: Dict[str, List[str]] = {}
        self.failures = set()
        self.lookups: List[str] = []

    def set(self, host: str, addresses: Iterable[str]) -> None:
        self.map[host.strip("[]").lower()] = list(addresses)

    def fail(self, host: str) -> None:
        self.failures.add(host.strip("[]").lower())

    def _addresses_for(self, host: str) -> List[str]:
        name = (host or "").strip("[]").lower()
        self.lookups.append(name)
        if name in self.failures:
            raise socket.gaierror(-2, "Name or service not known")
        if name in self.map:
            return list(self.map[name])
        try:
            ipaddress.ip_address(name)
            return [name]
        except ValueError:
            return [PUBLIC_IPV4]

    def getaddrinfo(self, host, port=0, family=0, type=0, proto=0, flags=0):
        try:
            portnum = int(port)
        except (TypeError, ValueError):
            portnum = 0
        out = []
        for address in self._addresses_for(host):
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, portnum)))
            else:
                out.append(
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, portnum, 0, 0))
                )
        return out

    def gethostbyname(self, host):
        for address in self._addresses_for(host):
            if ipaddress.ip_address(address).version == 4:
                return address
        raise socket.gaierror(-2, "Name or service not known")


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    resolver = FakeResolver()
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", resolver.gethostbyname)
    return resolver


# ---------------------------------------------------------------------------
# limiter clock (advance the window without sleeping)
# ---------------------------------------------------------------------------
class FakeClock:
    """Frozen, advanceable replacement for the limiter's time source."""

    def __init__(self, real) -> None:
        self._real = real
        self.value = 100000.0

    def monotonic(self) -> float:
        return self.value

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    def __getattr__(self, name):  # pragma: no cover - delegation only
        return getattr(self._real, name)


@pytest.fixture
def limiter_clock(monkeypatch):
    clock = FakeClock(real_time)
    monkeypatch.setattr(ratelimit_module, "time", clock, raising=False)
    return clock


# ---------------------------------------------------------------------------
# scripted short codes (collision forcing)
# ---------------------------------------------------------------------------
_FALLBACK_COUNTER = itertools.count(1)


def _fallback_code(length: int = 7) -> str:
    text = "Zz{0:05d}".format(next(_FALLBACK_COUNTER))
    if len(text) >= length:
        return text[:length]
    return text + "0" * (length - len(text))


class ScriptedCodes:
    """Deterministic short code generator used to force UNIQUE collisions."""

    def __init__(self, codes: Iterable[str]) -> None:
        self.queue = list(codes)
        self.calls = 0
        self.produced: List[str] = []

    def __call__(self, length: int = 7, *args, **kwargs) -> str:
        self.calls += 1
        if self.queue:
            value = self.queue.pop(0)
        else:
            value = _fallback_code(int(length or 7))
        self.produced.append(value)
        return value


@pytest.fixture
def scripted_codes(monkeypatch):
    def install(codes: Iterable[str]) -> ScriptedCodes:
        stub = ScriptedCodes(codes)
        monkeypatch.setattr(main_module, "generate_code", stub, raising=False)
        monkeypatch.setattr(codes_module, "generate_code", stub, raising=False)
        return stub

    return install


# ---------------------------------------------------------------------------
# the application under test
# ---------------------------------------------------------------------------
class Harness:
    """HTTP + database view of one freshly built application."""

    def __init__(self, application, db_path) -> None:
        self.app = application
        self.db_path = str(db_path)
        self._clients: Dict[str, TestClient] = {}

    # -- HTTP -------------------------------------------------------------
    def client(self, host: str = DEFAULT_HOST) -> TestClient:
        if host not in self._clients:
            try:
                client = TestClient(
                    self.app,
                    base_url="http://testserver",
                    raise_server_exceptions=False,
                    follow_redirects=False,
                    client=(host, 45000),
                )
            except TypeError:  # pragma: no cover - very old TestClient
                client = TestClient(
                    self.app,
                    base_url="http://testserver",
                    raise_server_exceptions=False,
                )
            self._clients[host] = client
        return self._clients[host]

    @staticmethod
    def _headers(api_key: Optional[str], headers: Optional[Dict[str, str]]):
        merged = dict(headers or {})
        if api_key is not None:
            merged["X-API-Key"] = api_key
        return merged

    def create(
        self,
        url: Optional[str] = None,
        *,
        expires_at: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        host: str = DEFAULT_HOST,
        headers: Optional[Dict[str, str]] = None,
    ):
        if payload is None:
            payload = {}
            if url is not None:
                payload["url"] = url
            if expires_at is not None:
                payload["expires_at"] = expires_at
        return self.client(host).post(
            "/api/links", json=payload, headers=self._headers(api_key, headers)
        )

    def create_ok(self, url: str, **kwargs) -> str:
        response = self.create(url, **kwargs)
        assert response.status_code == 201, "create({0!r}) -> {1}: {2}".format(
            url, response.status_code, response.text[:200]
        )
        return response.json()["code"]

    def visit(
        self,
        code,
        *,
        api_key: Optional[str] = None,
        host: str = DEFAULT_HOST,
        headers: Optional[Dict[str, str]] = None,
    ):
        return self.client(host).get(
            "/" + str(code), headers=self._headers(api_key, headers)
        )

    def stats(
        self,
        code,
        *,
        api_key: Optional[str] = None,
        host: str = DEFAULT_HOST,
        params: Optional[Dict[str, Any]] = None,
    ):
        return self.client(host).get(
            "/api/links/{0}/stats".format(code),
            params=params,
            headers=self._headers(api_key, None),
        )

    # -- database ---------------------------------------------------------
    def _rows(self, sql: str, params=()) -> List[Dict[str, Any]]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            raise AssertionError(
                "sqlite error {0!r} against {1}: does the service honour "
                "LINKS_DB_PATH and create its schema?".format(str(exc), self.db_path)
            )
        finally:
            con.close()

    def link_rows(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM links ORDER BY id")

    def click_rows(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM clicks ORDER BY id")

    def link_count(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM links")[0]["n"])

    def click_count(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM clicks")[0]["n"])

    def tables(self) -> List[str]:
        return sorted(
            row["name"]
            for row in self._rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        )

    def columns(self, table: str) -> List[str]:
        return [row["name"] for row in self._rows("PRAGMA table_info({0})".format(table))]

    def set_expiry(self, code: str, when: datetime) -> None:
        stamp = when.astimezone(timezone.utc).strftime(TS_FORMAT)
        con = sqlite3.connect(self.db_path)
        try:
            cursor = con.execute(
                "UPDATE links SET expires_at = ? WHERE code = ?", (stamp, code)
            )
            con.commit()
            assert cursor.rowcount == 1, "no links row with code {0!r}".format(code)
        finally:
            con.close()

    def disk_bytes(self) -> bytes:
        blob = b""
        for path in sorted(Path(self.db_path).parent.glob("*")):
            if path.is_file():
                blob += path.read_bytes()
        return blob

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass


@pytest.fixture
def app_factory(tmp_path, monkeypatch, fake_dns):
    counter = itertools.count(1)
    built: List[Harness] = []

    def factory(**env) -> Harness:
        index = next(counter)
        db_path = tmp_path / "app{0}".format(index) / "links.db"
        monkeypatch.setenv("LINKS_DB_PATH", str(db_path))
        for name, value in env.items():
            monkeypatch.setenv(name, str(value))
        harness = Harness(create_app(), db_path)
        built.append(harness)
        return harness

    yield factory

    for harness in built:
        harness.close()


@pytest.fixture
def app(app_factory) -> Harness:
    return app_factory()


# ---------------------------------------------------------------------------
# assertion helpers
# ---------------------------------------------------------------------------
def assert_error(response) -> Dict[str, Any]:
    """Assert the stable error envelope and return the inner error object."""
    assert "application/json" in response.headers.get("content-type", ""), (
        "error responses must be JSON, got "
        + repr(response.headers.get("content-type"))
    )
    body = response.json()
    assert isinstance(body, dict) and "error" in body, (
        "error body must be {'error': {...}}, got " + repr(body)[:200]
    )
    error = body["error"]
    assert isinstance(error, dict), repr(error)[:200]
    assert isinstance(error.get("code"), str) and error["code"], repr(error)[:200]
    assert isinstance(error.get("message"), str) and error["message"], repr(error)[:200]
    return error


def assert_retry_after(response, window: int = 60) -> int:
    value = response.headers.get("retry-after")
    assert value is not None, "a 429 must carry Retry-After"
    assert value.isdigit(), "Retry-After must be whole seconds, got " + repr(value)
    seconds = int(value)
    assert 1 <= seconds <= window, "Retry-After out of range: " + repr(value)
    assert "no-store" in response.headers.get("cache-control", "").lower(), (
        "a 429 must not be cached; Cache-Control was "
        + repr(response.headers.get("cache-control"))
    )
    return seconds


def parse_ts(value: str) -> datetime:
    assert isinstance(value, str), "timestamp must be a string, got " + repr(value)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
