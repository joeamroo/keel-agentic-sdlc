"""SQLite access layer.

Every statement is parameterised; no SQL is ever built with string formatting.
Schema is created if missing and is otherwise untouched.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional, Tuple

from .timeutil import is_expired, to_rfc3339

LOGGER = logging.getLogger("shortener.db")

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        target_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        clicked_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code)",
)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection in autocommit mode.

    Args:
        db_path: Filesystem path of the database file.

    Returns:
        A connection with ``sqlite3.Row`` rows and manual transaction control.

    Raises:
        sqlite3.Error: If the database cannot be opened.
    """
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    """Create the database file, directory and schema when they are missing.

    Args:
        db_path: Filesystem path of the database file.

    Returns:
        None.

    Raises:
        sqlite3.Error: If the schema cannot be created.
        OSError: If the containing directory cannot be created.
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
    LOGGER.info("Database ready.")


def insert_link(
    conn: sqlite3.Connection,
    code: str,
    target_url: str,
    created_at: datetime,
    expires_at: Optional[datetime],
) -> bool:
    """Insert one link row.

    Args:
        conn: Open connection.
        code: Short code, unique across the table.
        target_url: Already validated destination URL.
        created_at: Timezone-aware creation instant.
        expires_at: Timezone-aware expiry instant, or ``None`` for never.

    Returns:
        ``True`` when the row was written, ``False`` when the code was already taken.

    Raises:
        sqlite3.Error: On any database failure other than the uniqueness conflict.
        ValueError: If a naive datetime is supplied.
    """
    try:
        conn.execute(
            "INSERT INTO links (code, target_url, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                code,
                target_url,
                to_rfc3339(created_at),
                to_rfc3339(expires_at) if expires_at is not None else None,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def get_link(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    """Fetch one link row by code.

    Args:
        conn: Open connection.
        code: Short code to look up.

    Returns:
        The row with ``target_url``, ``created_at`` and ``expires_at``, or ``None``.

    Raises:
        sqlite3.Error: On any database failure.
    """
    cursor = conn.execute(
        "SELECT code, target_url, created_at, expires_at FROM links WHERE code = ?",
        (code,),
    )
    return cursor.fetchone()


def get_click_stats(conn: sqlite3.Connection, code: str) -> Tuple[int, Optional[str]]:
    """Summarise recorded clicks for one code.

    Args:
        conn: Open connection.
        code: Short code to summarise.

    Returns:
        A tuple of (click count, last click timestamp or ``None``).

    Raises:
        sqlite3.Error: On any database failure.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, MAX(clicked_at) AS last_clicked_at FROM clicks WHERE code = ?",
        (code,),
    ).fetchone()
    if row is None:
        return 0, None
    last = row["last_clicked_at"]
    return int(row["total"] or 0), str(last) if last is not None else None


def resolve_redirect(conn: sqlite3.Connection, code: str, now: datetime) -> Optional[str]:
    """Look up a live target URL and record the click in the same transaction.

    Args:
        conn: Open connection.
        code: Short code requested by the visitor.
        now: Timezone-aware instant of the request.

    Returns:
        The stored target URL when the link exists and has not expired, otherwise
        ``None`` (no click is recorded in that case).

    Raises:
        sqlite3.Error: On any database failure.
        ValueError: If ``now`` is naive.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT target_url, expires_at FROM links WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None or is_expired(row["expires_at"], now):
            conn.rollback()
            return None
        conn.execute(
            "INSERT INTO clicks (code, clicked_at) VALUES (?, ?)",
            (code, to_rfc3339(now)),
        )
        conn.commit()
        return str(row["target_url"])
    except Exception:
        conn.rollback()
        raise
