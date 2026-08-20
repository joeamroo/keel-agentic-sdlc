"""SQLite access helpers and schema definition.

The schema is intentionally minimal: short links and click records.  No API
key, quota, counter or client IP address is ever persisted.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Tuple

SCHEMA_STATEMENTS: Tuple[str, ...] = (
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
        link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
        clicked_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clicks_link_id_clicked_at ON clicks (link_id, clicked_at)",
)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with the service's standard pragmas.

    Returns a connection whose rows are :class:`sqlite3.Row` and which enforces
    foreign keys.  Raises :class:`sqlite3.Error` when the database cannot be
    opened.
    """
    connection = sqlite3.connect(db_path, timeout=10.0, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str) -> None:
    """Create the database file and schema when they do not already exist.

    Returns ``None``.  Raises :class:`sqlite3.Error` when the schema cannot be
    created and :class:`OSError` when the parent directory cannot be created.
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = connect(db_path)
    try:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


@contextmanager
def get_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection, committing on success and rolling back on error.

    Yields an open connection; the connection is always closed on exit.  Raises
    whatever the caller's block raises, plus :class:`sqlite3.Error` if the
    connection cannot be opened or committed.
    """
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
