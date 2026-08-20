"""Shared fixtures for the link-shortener HTTP suite.

Design contract notes (see coverage_notes):
  * every environment variable name/default below is taken verbatim from the
    design's configuration list;
  * each test gets its own SQLite file, its own freshly imported application
    module and its own in-process rate-limit state;
  * DNS is stubbed at import time so no test ever touches the network, and
    outbound socket connects are trapped so an SSRF regression shows up as a
    test failure rather than as a hung test.
"""

from __future__ import annotations

import base64
import datetime as _dt
import importlib
import ipaddress
import os
import re
import socket
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(os.environ.get("LINKS_PROJECT_ROOT", os.getcwd())).resolve()
TESTS_DIR = Path(__file__).resolve().parent

for _p in (str(ROOT), str(ROOT / "src")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


# --------------------------------------------------------------------------
# Deterministic, offline DNS
# --------------------------------------------------------------------------

PUBLIC_A = "93.184.216.34"

HOST_MAP = {
    "example.com": [PUBLIC_A],
    "www.example.com": [PUBLIC_A],
    "example.org": ["93.184.216.35"],
    "other.example.com": ["93.184.216.36"],
    "xn--e1awd7f.example.com": ["93.184.216.37"],
    # deny-listed answers
    "metadata.google.internal": ["169.254.169.254"],
    "internal.corp.example": ["10.0.0.5"],
    "loopback.example.com": ["127.0.0.1"],
    "localhost": ["127.0.0.1"],
}

_REAL_GETADDRINFO = socket.getaddrinfo


def _resolve(host):
    if host is None:
        return []
    h = str(host).strip().strip("[]").lower().rstrip(".")
    try:
        ipaddress.ip_address(h)
        return [h]
    except ValueError:
        pass
    return list(HOST_MAP.get(h, []))


def _fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    ips = _resolve(host)
    if not ips:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    out = []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        p = port if isinstance(port, int) else 0
        if addr.version == 4 and family in (0, socket.AF_INET, socket.AF_UNSPEC):
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p)))
        elif addr.version == 6 and family in (0, socket.AF_INET6, socket.AF_UNSPEC):
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, p, 0, 0)))
    if not out:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return out


def _fake_gethostbyname(host):
    ips = [i for i in _resolve(host) if ipaddress.ip_address(i).version == 4]
    if not ips:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return ips[0]


def _fake_gethostbyname_ex(host):
    return (str(host), [], [_fake_gethostbyname(host)])


socket.getaddrinfo = _fake_getaddrinfo
socket.gethostbyname = _fake_gethostbyname
socket.gethostbyname_ex = _fake_gethostbyname_ex


# --------------------------------------------------------------------------
# Application loading (fresh module graph per test so env is re-read)
# --------------------------------------------------------------------------

_APP_MODULE_NAME = None


def _is_project_module(mod):
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    try:
        s = str(Path(f).resolve())
    except Exception:
        return False
    if "site-packages" in s or "dist-packages" in s:
        return False
    if s.startswith(str(TESTS_DIR) + os.sep):
        return False
    return s.startswith(str(ROOT) + os.sep)


def _purge_project_modules():
    for name, mod in list(sys.modules.items()):
        if isinstance(mod, types.ModuleType) and _is_project_module(mod):
            sys.modules.pop(name, None)


def _candidate_module_names():
    names = []
    explicit = os.environ.get("LINKS_APP_MODULE")
    if explicit:
        names.append(explicit)
    names += [
        "app.main",
        "main",
        "app",
        "src.main",
        "src.app",
        "src.app.main",
        "links.main",
        "links",
        "links_service.main",
        "link_shortener.main",
        "shortener.main",
        "shortener",
        "url_shortener.main",
        "service.main",
        "server.main",
        "server",
        "api.main",
        "api",
        "app.app",
        "app.api",
    ]
    try:
        for p in sorted(ROOT.glob("*.py")):
            if p.stem in ("conftest", "setup") or p.stem.startswith("test_"):
                continue
            names.append(p.stem)
        for d in sorted(x for x in ROOT.iterdir() if x.is_dir()):
            if d.name in ("tests", "test", ".git", "__pycache__", ".venv", "venv"):
                continue
            if not (d / "__init__.py").exists():
                continue
            names += [d.name, f"{d.name}.main", f"{d.name}.app", f"{d.name}.api", f"{d.name}.server"]
    except Exception:
        pass
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _app_from_module(mod):
    candidate = getattr(mod, "app", None)
    if candidate is not None and hasattr(candidate, "router"):
        return candidate
    for factory_name in ("create_app", "get_app", "make_app", "build_app"):
        factory = getattr(mod, factory_name, None)
        if callable(factory):
            try:
                built = factory()
            except Exception:
                continue
            if built is not None and hasattr(built, "router"):
                return built
    return None


def build_app():
    """Import the project fresh (so it re-reads os.environ) and return its ASGI app."""
    global _APP_MODULE_NAME
    _purge_project_modules()
    errors = []
    names = [_APP_MODULE_NAME] if _APP_MODULE_NAME else _candidate_module_names()
    for name in names:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - discovery noise
            errors.append(f"{name}: {exc!r}")
            continue
        app = _app_from_module(mod)
        if app is not None:
            _APP_MODULE_NAME = name
            return app
        errors.append(f"{name}: imported but exposes no FastAPI 'app'/'create_app'")
    raise RuntimeError(
        "Could not locate the FastAPI application. Tried: "
        + "; ".join(errors[:20])
        + " (set LINKS_APP_MODULE to override)"
    )


# --------------------------------------------------------------------------
# Configuration (names/defaults bound to the design's configuration list)
# --------------------------------------------------------------------------


def default_env(db_path):
    return {
        "LINKS_DB_PATH": str(db_path),
        "LINKS_BIND_HOST": "127.0.0.1",
        "LINKS_BIND_PORT": "8080",
        "LINKS_PUBLIC_BASE_URL": "https://short.example.com",
        "LINKS_DEFAULT_EXPIRY_DAYS": "30",
        "LINKS_MAX_EXPIRY_DAYS": "365",
        "LINKS_MAX_URL_LENGTH": "2048",
        "LINKS_CODE_LENGTH": "7",
        "LINKS_CODE_MAX_ATTEMPTS": "5",
        "LINKS_RATE_LIMIT_MAX": "20",
        "LINKS_RATE_LIMIT_WINDOW_SECONDS": "60",
        "LINKS_TRUST_PROXY_HEADER": "false",
        "LINKS_DNS_TIMEOUT_MS": "2000",
        "LINKS_DNS_CACHE_TTL_SECONDS": "0",
        "LINKS_ALLOW_PRIVATE_DESTINATIONS": "false",
        "LINKS_LOG_LEVEL": "error",
    }


# --------------------------------------------------------------------------
# Datastore inspection
# --------------------------------------------------------------------------

DEST_COLS = ("destination", "destination_url", "url", "target", "long_url")
EXPIRY_COLS = ("expires_at", "expiry", "expires", "expire_at")
CLICK_COLS = ("click_count", "clicks", "hit_count", "hits")


class LinkDB:
    def __init__(self, path):
        self.path = str(path)

    # -- internals ---------------------------------------------------------
    def _connect(self):
        if not os.path.exists(self.path):
            return None
        return sqlite3.connect(self.path, timeout=5)

    def _meta(self, con):
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(r[0]).startswith("sqlite_")
        ]
        for t in tables:
            cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
            low = [c.lower() for c in cols]
            if "code" in low and any(d in low for d in DEST_COLS):
                return t, cols
        return None, []

    # -- API ---------------------------------------------------------------
    def rows(self):
        con = self._connect()
        if con is None:
            return []
        try:
            table, cols = self._meta(con)
            if table is None:
                return []
            out = []
            for r in con.execute('SELECT * FROM "%s"' % table):
                out.append({c.lower(): v for c, v in zip(cols, r)})
            return out
        finally:
            con.close()

    def count(self):
        return len(self.rows())

    def row_for(self, code):
        for r in self.rows():
            if r.get("code") == code:
                return r
        return None

    def destination_of(self, row):
        for c in DEST_COLS:
            if c in row and row[c] is not None:
                return row[c]
        return None

    def click_count_of(self, code):
        row = self.row_for(code) or {}
        for c in CLICK_COLS:
            if c in row:
                return row[c]
        return None

    def set_expiry(self, code, iso_value):
        con = self._connect()
        assert con is not None, f"no database file at {self.path}"
        try:
            table, cols = self._meta(con)
            assert table is not None, "no links table found in the database"
            low = {c.lower(): c for c in cols}
            col = next((low[c] for c in EXPIRY_COLS if c in low), None)
            assert col is not None, f"no expiry column among {cols}"
            cur = con.execute(
                'UPDATE "%s" SET "%s" = ? WHERE code = ?' % (table, col), (iso_value, code)
            )
            con.commit()
            assert cur.rowcount == 1, f"expected to age exactly one row, aged {cur.rowcount}"
        finally:
            con.close()


# --------------------------------------------------------------------------
# Helpers exposed to tests
# --------------------------------------------------------------------------

CODE_FN_NAMES = {
    "generatecode",
    "gencode",
    "newcode",
    "makecode",
    "randomcode",
    "shortcode",
    "generateshortcode",
    "makeshortcode",
    "newshortcode",
    "randomshortcode",
    "createcode",
    "generateuniquecode",
    "codegenerator",
    "generatecandidate",
}


def find_code_generators():
    """Every module-level callable in the project that looks like the code generator."""
    found = []
    for mod in list(sys.modules.values()):
        if not isinstance(mod, types.ModuleType) or not _is_project_module(mod):
            continue
        for attr in list(vars(mod)):
            norm = attr.replace("_", "").lower()
            if norm in CODE_FN_NAMES and callable(getattr(mod, attr, None)):
                found.append((mod, attr))
    return found


def code_of(body):
    if not isinstance(body, dict):
        return None
    for key in ("code", "short_code", "shortCode", "slug", "key"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    for key in ("short_url", "shortUrl", "short_link", "url"):
        v = body.get(key)
        if isinstance(v, str) and "/" in v:
            return v.rstrip("/").rsplit("/", 1)[-1]
    return None


def short_url_of(body):
    for key in ("short_url", "shortUrl", "short_link"):
        v = body.get(key)
        if isinstance(v, str):
            return v
    return None


def error_code(resp):
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        for key in ("error_code", "errorCode", "code", "error"):
            v = body.get(key)
            if isinstance(v, str) and v:
                return v
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict):
            for key in ("error_code", "errorCode", "code", "error"):
                v = detail.get(key)
                if isinstance(v, str) and v:
                    return v
    return None


def parse_iso(value):
    assert isinstance(value, str) and value, f"expected an ISO-8601 string, got {value!r}"
    text = value.strip()
    assert text.endswith("Z") or "+" in text, f"expected a UTC timestamp, got {value!r}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(text)


def wait_until(predicate, pump, tries=30):
    """Poll a predicate, pumping the event loop with a cheap request. Never sleeps."""
    value = predicate()
    for _ in range(tries):
        if value:
            return value
        pump()
        value = predicate()
    return value


class ScriptedRandom:
    """Deterministic replacement for the crypto RNG.

    The first two generated codes are identical (forcing exactly one collision),
    every later code differs.
    """

    def __init__(self, code_len):
        self.code_len = max(1, int(code_len))
        self.chars = 0
        self.tokens = 0
        self.calls = 0

    def choice(self, seq):
        self.calls += 1
        block = self.chars // self.code_len
        self.chars += 1
        if block < 2 or len(seq) < 2:
            return seq[0]
        return seq[1 + ((block - 2) % (len(seq) - 1))]

    def randbelow(self, n):
        self.calls += 1
        block = self.chars // self.code_len
        self.chars += 1
        if block < 2 or n < 2:
            return 0
        return 1 + ((block - 2) % (n - 1))

    def _token(self, n):
        self.calls += 1
        block = self.tokens
        self.tokens += 1
        fill = 0 if block < 2 else (block % 251) + 1
        return bytes([fill]) * max(1, int(n or 1))

    def token_bytes(self, n=32):
        return self._token(n)

    def token_hex(self, n=32):
        return self._token(n).hex()

    def token_urlsafe(self, n=32):
        return base64.urlsafe_b64encode(self._token(n)).decode().rstrip("=")


HELPERS = types.SimpleNamespace(
    code_of=code_of,
    short_url_of=short_url_of,
    error_code=error_code,
    parse_iso=parse_iso,
    wait_until=wait_until,
    find_code_generators=find_code_generators,
    ScriptedRandom=ScriptedRandom,
    LinkDB=LinkDB,
    CODE_RE=re.compile(r"^[0-9A-Za-z]+$"),
)


@pytest.fixture
def helpers():
    return HELPERS


# --------------------------------------------------------------------------
# Request-body field discovery (design says "url", README says "destination_url")
# --------------------------------------------------------------------------

_REQ_FIELDS = ["destination_url", "url"]


@pytest.fixture(scope="session", autouse=True)
def _detect_request_fields(tmp_path_factory):
    global _REQ_FIELDS
    probe_dir = tmp_path_factory.mktemp("probe")
    saved = dict(os.environ)
    try:
        os.environ.update(default_env(probe_dir / "probe.db"))
        app = build_app()
        with TestClient(app) as client:
            for field in ("destination_url", "url"):
                try:
                    resp = client.post("/links", json={field: "https://example.com/probe"})
                except Exception:
                    continue
                if resp.status_code in (200, 201):
                    _REQ_FIELDS = [field]
                    break
            else:
                _REQ_FIELDS = ["destination_url", "url"]
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return _REQ_FIELDS


# --------------------------------------------------------------------------
# Per-test fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_outbound_connections(monkeypatch):
    """Any attempt to open a socket to the destination fails loudly."""
    attempts = []

    def _blocked(*args, **kwargs):
        attempts.append(args)
        raise AssertionError(
            "the service opened an outbound socket during validation/redirect; "
            "destinations must only be resolved, never contacted"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked, raising=False)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)
    yield attempts


@pytest.fixture
def env(tmp_path):
    return default_env(tmp_path / "links.db")


@pytest.fixture
def make_client(env, monkeypatch, _no_outbound_connections):
    opened = []

    def _make(**overrides):
        cfg = dict(env)
        cfg.update({k: str(v) for k, v in overrides.items()})
        for key, value in cfg.items():
            monkeypatch.setenv(key, str(value))
        app = build_app()
        client = TestClient(app)
        client.__enter__()
        client.db_path = cfg["LINKS_DB_PATH"]
        opened.append(client)
        return client

    yield _make
    for client in reversed(opened):
        try:
            client.__exit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def db(env):
    return LinkDB(env["LINKS_DB_PATH"])


@pytest.fixture
def link_db():
    return LinkDB


@pytest.fixture
def create_link():
    def _create(client, destination, headers=None, **fields):
        payload = {name: destination for name in _REQ_FIELDS}
        payload.update(fields)
        return client.post("/links", json=payload, headers=headers or {})

    return _create
