"""obs_clicks — tiny per-(db_id, column) click counter for the dataset page.

Backs anthive-web's "frequently used" obs-column chip row (pika #259, GO
from Mark 2026-06-27): every time a user clicks an obs column to plot it,
ant-serve increments a counter, and the dataset page later fetches the
top-N most-clicked columns to surface as suggestions.

Design notes:
  - One SQLite file (`tmp/obs_clicks.db` by default; configurable).
  - One table, `clicks(db_id, column, n, last_seen)`. PK on (db_id, column).
  - Increments via INSERT OR REPLACE / ON CONFLICT — fire-and-forget,
    fast. The endpoint replies 204 without waiting on disk fsync.
  - Read path is a single ORDER BY n DESC LIMIT n — cheap.
  - WAL mode + check_same_thread=False so the ant-serve async pool can
    share one connection.

Intentionally NOT in chat_history.db: clicks aren't keyed on user (any
visitor's click counts), they're per-deployment community stats — the
two stores are different shape, different lifecycle, easier to wipe one
without the other.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clicks (
    db_id      TEXT    NOT NULL,
    column     TEXT    NOT NULL,
    n          INTEGER NOT NULL DEFAULT 0,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (db_id, column)
);
CREATE INDEX IF NOT EXISTS idx_clicks_db_n
    ON clicks (db_id, n DESC);
"""


class ObsClickStore:
    """SQLite-backed obs-column click counter. Threadsafe-ish for low write
    volume; if traffic ever exceeds ~10 writes/s we'd want an in-memory
    aggregator that flushes every N seconds. Not needed at our scale.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,            # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def click(self, db_id: str, column: str) -> None:
        """Increment the counter for (db_id, column). Fire-and-forget."""
        now = int(time.time())
        # SQLite UPSERT — atomic, single-statement.
        self._conn.execute(
            "INSERT INTO clicks(db_id, column, n, last_seen) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(db_id, column) DO UPDATE SET "
            "  n = clicks.n + 1, last_seen = excluded.last_seen",
            (db_id, column, now),
        )

    def top(self, db_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Return the top-N most-clicked columns for ``db_id``.

        Shape: ``[{'column': str, 'count': int}, ...]``, ordered by count
        desc. Empty list when nothing tracked yet.
        """
        cur = self._conn.execute(
            "SELECT column, n FROM clicks "
            "WHERE db_id = ? "
            "ORDER BY n DESC, last_seen DESC "
            "LIMIT ?",
            (db_id, int(limit)),
        )
        return [{'column': row[0], 'count': int(row[1])} for row in cur.fetchall()]

    def stats(self) -> Dict[str, Any]:
        n_rows = self._conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        total = self._conn.execute("SELECT COALESCE(SUM(n), 0) FROM clicks").fetchone()[0]
        return {
            'rows': int(n_rows),
            'total_clicks': int(total),
            'db_path': str(self.path),
            'db_size_bytes': self.path.stat().st_size if self.path.exists() else 0,
        }

    def close(self) -> None:
        self._conn.close()
