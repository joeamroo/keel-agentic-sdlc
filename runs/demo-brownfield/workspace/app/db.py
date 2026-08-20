"""SQLite persistence for links and clicks.

Every statement is parameterized; no SQL is ever built by string formatting.
The schema is frozen: ``links`` and ``clicks`` only.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

LOGGER = logging.getLogger("app.db")

CONNECT_TIMEOUT_SECONDS = 5.0

SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id INTEGER NOT NULL,
        clicked_at TEXT NOT NULL,
        FOREIGN KEY (link_id) REFERENCES links (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clicks_link_id ON clicks (link_id)",
)


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection for the duration of the context.

    Yields a connection in autocommit mode with ``sqlite3.Row`` rows and
    foreign keys enabled, and always closes it.
    Raises :class:`sqlite3.Error` when the database cannot be opened.
    """
    conn = sqlite3.connect(
        db_path, timeout=CONNECT_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the database file and the frozen schema if they do not exist.

    Returns ``None``.
    Raises :class:`OSError` when the parent directory cannot be created and
    :class:`sqlite3.Error` when the schema cannot be applied.
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect(db_path) as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            LOGGER.warning("could not enable WAL journal mode; continuing")
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def insert_link(
    conn: sqlite3.Connection,
    code: str,
    url: str,
    created_at: str,
    expires_at: Optional[str],
) -> int:
    """Insert one link row.

    Returns the new row id.
    Raises :class:`sqlite3.IntegrityError` when the code already exists and
    :class:`sqlite3.Error` for any other database failure.
    """
    cursor = conn.execute(
        "INSERT INTO links (code, url, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (code, url, created_at, expires_at),
    )
    return int(cursor.lastrowid or 0)


def fetch_link_by_code(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    """Look up one link by its short code.

    Returns the row, or ``None`` when no link has that code.
    Raises :class:`sqlite3.Error` on a database failure.
    """
    cursor = conn.execute(
        "SELECT id, code, url, created_at, expires_at FROM links WHERE code = ?",
        (code,),
    )
    row = cursor.fetchone()
    return row


def record_click(conn: sqlite3.Connection, link_id: int, clicked_at: str) -> None:
    """Record one served redirect.

    Returns ``None``.
    Raises :class:`sqlite3.Error` on a database failure.
    """
    conn.execute(
        "INSERT INTO clicks (link_id, clicked_at) VALUES (?, ?)",
        (link_id, clicked_at),
    )


def fetch_click_stats(
    conn: sqlite3.Connection, link_id: int
) -> Tuple[int, Optional[str]]:
    """Aggregate the clicks recorded for one link.

    Returns ``(total_clicks, last_clicked_at)`` where the second element is
    ``None`` when the link was never visited.
    Raises :class:`sqlite3.Error` on a database failure.
    """
    cursor = conn.execute(
        "SELECT COUNT(*) AS total, MAX(clicked_at) AS last_clicked_at "
        "FROM clicks WHERE link_id = ?",
        (link_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return 0, None
    total = int(row["total"] or 0)
    last = row["last_clicked_at"]
    return total, (str(last) if last is not None else None)
