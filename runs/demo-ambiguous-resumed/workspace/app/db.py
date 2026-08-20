"""SQLite persistence for issued short links.

All statements are parameterized; no SQL is ever built by string formatting.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Dict, Iterator, Optional, Tuple

from .timeutil import to_iso_z, utc_now

LOGGER = logging.getLogger("links.db")

_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        destination TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        creator_ip TEXT NOT NULL,
        click_count INTEGER NOT NULL DEFAULT 0,
        last_clicked_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_links_expires_at ON links (expires_at)",
)


class Database:
    """Thin, thread safe wrapper around the links SQLite database."""

    def __init__(self, path: str) -> None:
        """Open (or prepare to open) the database at ``path``.

        Args:
            path: Filesystem path, or ``:memory:`` for an ephemeral database that
                lives in a single shared, lock protected connection.

        Returns:
            None.

        Raises:
            sqlite3.Error: If an in-memory connection cannot be created.
            OSError: If the parent directory of a file database cannot be created.
        """
        self._path = path.strip() or ":memory:"
        self._memory = self._path == ":memory:"
        self._lock = threading.RLock()
        self._shared: Optional[sqlite3.Connection] = None
        if self._memory:
            self._shared = self._open()
        else:
            parent = os.path.dirname(os.path.abspath(self._path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    @property
    def path(self) -> str:
        """Return the configured database path.

        Returns:
            The path string, ``:memory:`` for ephemeral databases.

        Raises:
            Nothing.
        """
        return self._path

    def _open(self) -> sqlite3.Connection:
        """Create a configured SQLite connection.

        Returns:
            A connection in autocommit mode with WAL, NORMAL sync and a busy timeout.

        Raises:
            sqlite3.Error: If the connection cannot be established.
        """
        connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            if not self._memory:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            LOGGER.warning("could not apply all SQLite pragmas")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, shared for in-memory mode and per call for file mode.

        Returns:
            A context manager yielding a :class:`sqlite3.Connection`.

        Raises:
            sqlite3.Error: If a new connection cannot be opened.
            RuntimeError: If the shared in-memory connection is missing.
        """
        if self._memory:
            if self._shared is None:
                raise RuntimeError("in-memory database is not initialised")
            with self._lock:
                yield self._shared
            return
        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema and indexes when they do not yet exist.

        Returns:
            None.

        Raises:
            sqlite3.Error: If the schema cannot be created.
        """
        with self.connection() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)

    def insert_link(
        self,
        code: str,
        destination: str,
        created_at: str,
        expires_at: str,
        creator_ip: str,
    ) -> None:
        """Insert one link row.

        Args:
            code: The generated base62 short code.
            destination: The normalised absolute destination URL.
            created_at: ISO-8601 UTC creation timestamp.
            expires_at: ISO-8601 UTC expiry timestamp.
            creator_ip: Client IP recorded for abuse triage; never returned.

        Returns:
            None.

        Raises:
            sqlite3.IntegrityError: If the code collides with an existing row.
            sqlite3.Error: On any other datastore failure.
        """
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO links (code, destination, created_at, expires_at, creator_ip, "
                "click_count, last_clicked_at) VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (code, destination, created_at, expires_at, creator_ip),
            )

    def get_link(self, code: str) -> Optional[Dict[str, Any]]:
        """Look up a link by its code.

        Args:
            code: The short code from the request path.

        Returns:
            A dictionary with ``code``, ``destination`` and ``expires_at``, or
            ``None`` when no such row exists.

        Raises:
            sqlite3.Error: On a datastore failure.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                "SELECT code, destination, expires_at FROM links WHERE code = ?",
                (code,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "code": row["code"],
            "destination": row["destination"],
            "expires_at": row["expires_at"],
        }

    def record_click(self, code: str, clicked_at: str) -> bool:
        """Increment best effort click analytics for a code.

        Args:
            code: The short code that was resolved.
            clicked_at: ISO-8601 UTC timestamp of the redirect.

        Returns:
            ``True`` when a row was updated.

        Raises:
            sqlite3.Error: On a datastore failure.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE links SET click_count = click_count + 1, last_clicked_at = ? WHERE code = ?",
                (clicked_at, code),
            )
            return cursor.rowcount > 0

    def delete_expired(self, cutoff: str) -> int:
        """Delete rows whose expiry is at or before ``cutoff``.

        Args:
            cutoff: ISO-8601 UTC timestamp; rows expiring at or before it are removed.

        Returns:
            The number of deleted rows.

        Raises:
            sqlite3.Error: On a datastore failure.
        """
        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM links WHERE expires_at <= ?", (cutoff,))
            return int(cursor.rowcount or 0)


class ExpiryPurger:
    """Rate limited housekeeping that removes long expired rows."""

    def __init__(self, database: Database, interval_seconds: int = 300, grace_days: int = 30) -> None:
        """Create a purger.

        Args:
            database: The database to clean.
            interval_seconds: Minimum seconds between two purges.
            grace_days: Rows keep serving 410 for this long after expiry before deletion.

        Returns:
            None.

        Raises:
            Nothing.
        """
        self._database = database
        self._interval = max(1, interval_seconds)
        self._grace_days = max(0, grace_days)
        self._lock = threading.Lock()
        self._last_run: Optional[float] = None

    def maybe_purge(self) -> int:
        """Purge long expired rows if the interval has elapsed.

        Returns:
            The number of deleted rows (0 when the purge was skipped or failed).

        Raises:
            Nothing; datastore failures are logged and swallowed.
        """
        now = time.monotonic()
        with self._lock:
            if self._last_run is not None and now - self._last_run < self._interval:
                return 0
            self._last_run = now
        cutoff = to_iso_z(utc_now() - timedelta(days=self._grace_days))
        try:
            deleted = self._database.delete_expired(cutoff)
        except sqlite3.Error:
            LOGGER.warning("expired link purge failed")
            return 0
        if deleted:
            LOGGER.info("purged %d expired links", deleted)
        return deleted
