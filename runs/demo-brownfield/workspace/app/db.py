"""SQLite access layer.

The schema is frozen: ``SCHEMA_STATEMENTS`` defines exactly two tables and one
index, and nothing in this service writes client addresses or API keys to disk.
Every statement is parameterised; no SQL is ever built by string formatting.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        target_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        click_count INTEGER NOT NULL DEFAULT 0,
        last_clicked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
        clicked_at TEXT NOT NULL,
        referrer TEXT,
        user_agent TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clicks_link_id ON clicks(link_id)",
)

MAX_TEXT_FIELD_LENGTH = 512


def ensure_parent_directory(db_path: str) -> None:
    """Create the directory holding the SQLite file when it does not exist.

    Does nothing for in-memory databases or paths without a directory part.
    Returns None. Raises OSError when the directory cannot be created.
    """
    if db_path == ":memory:" or db_path.startswith("file:"):
        return
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection configured for WAL and foreign keys.

    Yields the connection and always closes it. Raises sqlite3.Error when the
    database cannot be opened or configured.
    """
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside an immediate SQLite transaction.

    Yields the connection, commits on success and rolls back on any exception.
    Raises whatever the wrapped block raises, including sqlite3.IntegrityError.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(db_path: str) -> None:
    """Create the schema if it is missing.

    Returns None. Raises sqlite3.Error when the schema cannot be applied and
    OSError when the parent directory cannot be created.
    """
    ensure_parent_directory(db_path)
    with connect(db_path) as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def insert_link(
    conn: sqlite3.Connection,
    code: str,
    target_url: str,
    created_at: str,
    expires_at: Optional[str],
) -> int:
    """Insert a new link row.

    Returns the new row id. Raises sqlite3.IntegrityError when ``code``
    collides with an existing row (the UNIQUE constraint is the only place code
    uniqueness is enforced).
    """
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO links (code, target_url, created_at, expires_at, click_count, "
            "last_clicked_at) VALUES (?, ?, ?, ?, 0, NULL)",
            (code, target_url, created_at, expires_at),
        )
        row_id = cursor.lastrowid
    return int(row_id or 0)


def fetch_link(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    """Look up a single link by its short code.

    Returns the row, or None when no link with that code exists. Raises
    sqlite3.Error on database failure.
    """
    cursor = conn.execute(
        "SELECT id, code, target_url, created_at, expires_at, click_count, last_clicked_at "
        "FROM links WHERE code = ?",
        (code,),
    )
    return cursor.fetchone()


def record_click(
    conn: sqlite3.Connection,
    link_id: int,
    clicked_at: str,
    referrer: Optional[str],
    user_agent: Optional[str],
) -> None:
    """Record a redirect: bump the counter and append to the click log.

    Deliberately stores no client address and no API key. Returns None. Raises
    sqlite3.Error on database failure.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE links SET click_count = click_count + 1, last_clicked_at = ? WHERE id = ?",
            (clicked_at, link_id),
        )
        conn.execute(
            "INSERT INTO clicks (link_id, clicked_at, referrer, user_agent) VALUES (?, ?, ?, ?)",
            (
                link_id,
                clicked_at,
                referrer[:MAX_TEXT_FIELD_LENGTH] if referrer is not None else None,
                user_agent[:MAX_TEXT_FIELD_LENGTH] if user_agent is not None else None,
            ),
        )
