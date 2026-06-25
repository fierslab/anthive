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

-- One row per LLM call (a single user message can produce N rows when the
-- agent makes N tool rounds). `seq` is the seq of the user message that
-- kicked off the turn, so you can group rows by turn.
CREATE TABLE IF NOT EXISTS usage (
    user                TEXT    NOT NULL,
    session_id          TEXT    NOT NULL,
    turn_seq            INTEGER NOT NULL,    -- seq of the user message
    round_idx           INTEGER NOT NULL,    -- 0..N-1 within the turn
    model               TEXT    NOT NULL,
    prompt_tokens       INTEGER NOT NULL,
    completion_tokens   INTEGER NOT NULL,
    cached_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL    NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    PRIMARY KEY (user, session_id, turn_seq, round_idx)
);
CREATE INDEX IF NOT EXISTS idx_usage_user_session
    ON usage (user, session_id);
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

    # ── usage tracking (cost + tokens per LLM call) ──────────────────────────

    def append_usage(
        self,
        user: str,
        session_id: str,
        turn_seq: int,
        round_idx: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record one LLM call's usage. Idempotent on (user, session_id, turn_seq, round_idx)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO usage "
            "(user, session_id, turn_seq, round_idx, model, "
            " prompt_tokens, completion_tokens, cached_tokens, "
            " cache_write_tokens, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user, session_id, int(turn_seq), int(round_idx), model,
             int(prompt_tokens), int(completion_tokens),
             int(cached_tokens), int(cache_write_tokens),
             float(cost_usd), int(time.time())),
        )

    def session_usage(self, user: str, session_id: str) -> Dict[str, Any]:
        """Aggregate usage for one (user, session_id).

        Returns totals (turns, llm_calls, tokens, cost) and a per-model
        breakdown. ``cached_fraction`` is the share of prompt tokens that
        landed in Anthropic's prompt cache — a clean signal for whether
        the system-prompt cache is working.
        """
        agg = self._conn.execute(
            "SELECT COUNT(DISTINCT turn_seq), COUNT(*), "
            "       COALESCE(SUM(prompt_tokens), 0), "
            "       COALESCE(SUM(completion_tokens), 0), "
            "       COALESCE(SUM(cached_tokens), 0), "
            "       COALESCE(SUM(cache_write_tokens), 0), "
            "       COALESCE(SUM(cost_usd), 0.0) "
            "FROM usage WHERE user=? AND session_id=?",
            (user, session_id),
        ).fetchone()
        turns, calls, prompt, completion, cached, cache_write, cost = agg

        by_model_rows = self._conn.execute(
            "SELECT model, COUNT(*), "
            "       SUM(prompt_tokens), SUM(completion_tokens), "
            "       SUM(cached_tokens), SUM(cost_usd) "
            "FROM usage WHERE user=? AND session_id=? "
            "GROUP BY model ORDER BY model",
            (user, session_id),
        ).fetchall()

        total_prompt = int(prompt or 0)
        cached_fraction = (
            float(cached or 0) / total_prompt if total_prompt > 0 else 0.0
        )

        return {
            'turns':              int(turns or 0),
            'llm_calls':          int(calls or 0),
            'prompt_tokens':      total_prompt,
            'completion_tokens':  int(completion or 0),
            'cached_tokens':      int(cached or 0),
            'cache_write_tokens': int(cache_write or 0),
            'total_tokens':       total_prompt + int(completion or 0),
            'cached_fraction':    round(cached_fraction, 4),
            'cost_usd':           round(float(cost or 0.0), 6),
            'by_model': [
                {
                    'model':             model,
                    'llm_calls':         int(n_calls),
                    'prompt_tokens':     int(p_toks),
                    'completion_tokens': int(c_toks),
                    'cached_tokens':     int(cach_toks),
                    'cost_usd':          round(float(m_cost), 6),
                }
                for model, n_calls, p_toks, c_toks, cach_toks, m_cost in by_model_rows
            ],
        }

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
