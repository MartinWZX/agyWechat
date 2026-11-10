"""Shared SQLite connection helpers guaranteeing zero file-descriptor leaks.

`sqlite3.Connection.__exit__` only commits/rolls back; it does NOT close the
connection. Every `with conn:` block in the codebase previously leaked one FD per
call. `ClosingConnection` commits/rolls back and then always closes.
"""
import sqlite3
from typing import Any, Union
from pathlib import Path


class ClosingConnection(sqlite3.Connection):
    """sqlite3 connection whose context manager commits/rolls back and then closes."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Union[str, Path], timeout: float = 30.0) -> sqlite3.Connection:
    """Open a WAL-mode connection with Row factory that closes itself on context exit."""
    conn = sqlite3.connect(str(db_path), timeout=timeout, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn
