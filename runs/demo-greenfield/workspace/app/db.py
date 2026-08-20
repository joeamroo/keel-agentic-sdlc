"""SQLite access helpers.

One connection per request; WAL mode so a click insert does not block
concurrent redirect reads. Every statement in the service is parameterized.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("links.db")

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
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
    """
    CREATE INDEX IF NOT EXISTS idx_clicks_link_time
        ON clicks(link_id, clicked_at DESC, id DESC)
    """,
)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    ``db_path`` is the database file path. Returns a connection with
    ``sqlite3.Row`` rows, foreign keys enforced, WAL journalling and a 5 second
    busy timeout. Raises :class:`sqlite3.Error` when the database cannot be
    opened.
    """
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:  # pragma: no cover - filesystem dependent
        logger.warning("WAL journal mode unavailable; continuing with default")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a configured SQLite connection.

    ``db_path`` is the database file path. Yields the connection and always
    closes it. Raises :class:`sqlite3.Error` when the database cannot be
    opened.
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the schema if it does not exist.

    ``db_path`` is the database file path; its parent directory is created when
    missing. Returns nothing. Raises :class:`sqlite3.Error` or :class:`OSError`
    when the database or its directory cannot be created.
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_connection(db_path) as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()
