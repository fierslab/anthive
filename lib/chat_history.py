"""SQLite-backed chat history store, keyed on (user, session_id).

Per anthive-web's locked contract (pika #217 §9):

  - History is server-side, indexed by **(Authelia Remote-User, session_id)**.
  - Each turn writes three roles: ``user`` (the prompt), ``assistant`` (the
    streamed response), and zero or more ``action`` entries (each one is the
    JSON of a client-action frame that was emitted during the turn).
  - Mark's decision (pika #225): the DB file MUST live somewhere that
    survives a ``tmp/`` clean + restarts. Default path is
    ``data/chat_history.db`` (configurable via ``chat.history_path``).

This module is pure SQLite — no FastAPI, no httpx, easy to unit test.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


VALID_ROLES = ('user', 'assistant', 'action', 'system')


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    user        TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,   -- monotonic per (user, session_id)
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,      -- raw text for user/assistant/system;
                                    -- JSON for role='action'
    created_at  INTEGER NOT NULL,   -- unix ts
    PRIMARY KEY (user, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_user_session
    ON messages (user, session_id, seq);
"""


class ChatHistory:
    """Thread-safe (well, connection-safe) chat history store.

    Pass ``check_same_thread=False`` so ant-serve's async request handlers
    (which may dispatch to different threads via ``asyncio.to_thread``) can
    share the connection. Writes serialise on SQLite's own lock — fine for
    this low-volume use case.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,           # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    # ── reads ────────────────────────────────────────────────────────────────

    def list_messages(
        self, user: str, session_id: str, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return the ordered transcript for (user, session_id).

        Format matches the web client's expected shape:
        ``[{"role": "...", "content": "..."}, ...]``. For role='action'
        the ``content`` is the JSON string of the original action frame
        (e.g. ``{"type":"action","name":"navigate","args":{"url":"..."}}``).
        Empty list when no rows.
        """
        sql = (
            "SELECT role, content FROM messages "
            "WHERE user=? AND session_id=? "
            "ORDER BY seq ASC"
        )
        params: List[Any] = [user, session_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [{'role': role, 'content': content} for role, content in rows]

    # ── writes ───────────────────────────────────────────────────────────────

    def append(
        self,
        user: str,
        session_id: str,
        role: str,
        content: str,
    ) -> int:
        """Append a single message; return its ``seq``.

        Atomic per-row insert. The ``seq`` is one more than the current
        max ``seq`` for this (user, session_id) — looked up inline so
        concurrent writes for the same session would race; in practice
        a session is single-client so this is fine.
        """
        if role not in VALID_ROLES:
            raise ValueError(
                f"invalid role {role!r} — must be one of {VALID_ROLES}"
            )
        next_seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages "
            "WHERE user=? AND session_id=?",
            (user, session_id),
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO messages "
            "(user, session_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user, session_id, next_seq, role, content, int(time.time())),
        )
        return int(next_seq)

    def append_action(
        self,
        user: str,
        session_id: str,
        action_frame: Dict[str, Any],
    ) -> int:
        """Convenience: serialise an action frame as JSON and append."""
        return self.append(
            user, session_id, 'action',
            json.dumps(action_frame, separators=(',', ':')),
        )

    # ── admin / inspection ───────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        n_rows = self._conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
        n_sessions = self._conn.execute(
            "SELECT COUNT(DISTINCT (user || '|' || session_id)) FROM messages"
        ).fetchone()[0]
        n_users = self._conn.execute(
            "SELECT COUNT(DISTINCT user) FROM messages"
        ).fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            'rows': n_rows,
            'sessions': n_sessions,
            'users': n_users,
            'db_path': str(self.path),
            'db_size_bytes': size,
        }

    def clear_session(self, user: str, session_id: str) -> int:
        """Wipe one (user, session_id) transcript. Returns rows deleted."""
        cur = self._conn.execute(
            "DELETE FROM messages WHERE user=? AND session_id=?",
            (user, session_id),
        )
        return int(cur.rowcount or 0)

    def close(self) -> None:
        self._conn.close()
