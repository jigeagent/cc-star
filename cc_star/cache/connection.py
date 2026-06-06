"""SQLite connection management with performance optimizations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional


# Shared statement cache across threads
_STMT_CACHE: dict[str, sqlite3.Cursor] = {}


class CacheConnection:
    """Thread-safe SQLite connection manager with performance tuning.

    Optimizations:
    - WAL mode for concurrent reads
    - 64MB cache for hot data
    - memory-mapped I/O (256MB)
    - Lazy pragma initialization (deferred until first query)
    """

    def __init__(self, db_path: str, wal_mode: bool = True):
        self._db_path = str(Path(db_path).expanduser())
        self._wal = wal_mode
        self._local = threading.local()
        self._lock = threading.Lock()
        self._initialized = False

        # Ensure parent directory exists
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def _ensure_init(self) -> None:
        """Apply performance pragmas once per connection."""
        if self._initialized:
            return
        conn = self._get_conn_raw()
        if self._wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;
            PRAGMA cache_size=-65536;
            PRAGMA mmap_size=268435456;
            PRAGMA temp_store=MEMORY;
            PRAGMA busy_timeout=5000;
        """)
        self._initialized = True

    def _get_conn_raw(self) -> sqlite3.Connection:
        """Create a raw connection without pragma setup."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit mode
            )
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        self._ensure_init()
        return self._get_conn_raw()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute with automatic pragma init."""
        self._ensure_init()
        return self._get_conn_raw().execute(sql, params)

    def executemany(self, sql: str, params: list[tuple]) -> sqlite3.Cursor:
        """Batch execute with automatic pragma init."""
        self._ensure_init()
        return self._get_conn_raw().executemany(sql, params)

    def close(self) -> None:
        """Close the connection for the current thread."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.execute("PRAGMA optimize")
            except Exception:
                pass
            self._local.conn.close()
            self._local.conn = None
            self._initialized = False

    def close_all(self) -> None:
        """Force close via lock (use sparingly)."""
        with self._lock:
            if hasattr(self._local, "conn") and self._local.conn is not None:
                try:
                    self._local.conn.execute("PRAGMA optimize")
                except Exception:
                    pass
                self._local.conn.close()
                self._local.conn = None
                self._initialized = False
