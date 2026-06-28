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

-- Per-user, per-session ack of the privacy / data-handling notice (per
-- anthive-web pika 2026-06-28 #260, Mark's decision: ack lives server-
-- side keyed on Remote-User so it follows across devices, not browser
-- localStorage). One row per user; once acknowledged_at is set, the
-- chat panel unblocks across all of that user's sessions.
CREATE TABLE IF NOT EXISTS ack (
    user             TEXT PRIMARY KEY,
    acknowledged_at  INTEGER NOT NULL
);

-- Per-message feedback flag. anthive-web's chat surfaces a small
-- "flag this message" button per assistant message; a click POSTs a
-- row here. category is a small enum, comment is an optional one-liner.
-- Triaged by the dev team (admin GET endpoint), never shown back to the
-- user. Per pika #260: per-message granularity (turn_seq + round_idx),
-- attributed via Remote-User (not anonymous), NO public visibility.
CREATE TABLE IF NOT EXISTS feedback (
    user        TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    turn_seq    INTEGER NOT NULL,
    round_idx   INTEGER NOT NULL,
    category    TEXT    NOT NULL,
    comment     TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_session
    ON feedback (user, session_id, turn_seq);

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
        # ── one-shot migration: add turn_seq / round_idx columns to the
        # legacy messages table if they're missing (per anthive-web
        # 2026-06-28 #260, per-message feedback needs them exposed on
        # /chat/history). NULL for user/system rows; populated for
        # assistant rows. SQLite ADD COLUMN is O(1).
        for col in ('turn_seq', 'round_idx'):
            try:
                self._conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # already there

    # ── reads ────────────────────────────────────────────────────────────────

    def list_messages(
        self, user: str, session_id: str, *, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return the ordered transcript for (user, session_id).

        Format matches the web client's expected shape:
        ``[{"role": "...", "content": "...", "turn_seq": int|None,
           "round_idx": int|None}, ...]``. For role='action' the
        ``content`` is the JSON string of the original action frame.

        ``turn_seq`` + ``round_idx`` identify which assistant message
        a given row belongs to — populated for assistant rows;
        ``None`` for user/system. The web client uses them as STABLE
        per-message ids for the feedback-flag flow (pika 2026-06-28
        #260).

        Empty list when no rows.
        """
        sql = (
            "SELECT role, content, turn_seq, round_idx FROM messages "
            "WHERE user=? AND session_id=? "
            "ORDER BY seq ASC"
        )
        params: List[Any] = [user, session_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                'role': role,
                'content': content,
                'turn_seq': int(turn_seq) if turn_seq is not None else None,
                'round_idx': int(round_idx) if round_idx is not None else None,
            }
            for role, content, turn_seq, round_idx in rows
        ]

    # ── writes ───────────────────────────────────────────────────────────────

    def append(
        self,
        user: str,
        session_id: str,
        role: str,
        content: str,
        *,
        turn_seq: Optional[int] = None,
        round_idx: Optional[int] = None,
    ) -> int:
        """Append a single message; return its ``seq``.

        Atomic per-row insert. The ``seq`` is one more than the current
        max ``seq`` for this (user, session_id) — looked up inline so
        concurrent writes for the same session would race; in practice
        a session is single-client so this is fine.

        ``turn_seq`` + ``round_idx`` are optional per-message identifiers
        for assistant rows; pass them when the row is the result of an
        agent-loop round so the web client can target per-message
        feedback. Left null for user / system / action rows.
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
            "(user, session_id, seq, role, content, created_at, "
            " turn_seq, round_idx) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user, session_id, next_seq, role, content,
             int(time.time()),
             None if turn_seq is None else int(turn_seq),
             None if round_idx is None else int(round_idx)),
        )
        return int(next_seq)

    def append_action(
        self,
        user: str,
        session_id: str,
        action_frame: Dict[str, Any],
        *,
        turn_seq: Optional[int] = None,
        round_idx: Optional[int] = None,
    ) -> int:
        """Convenience: serialise an action frame as JSON and append.

        ``turn_seq`` + ``round_idx`` propagate the per-message identity so
        the editor can associate an action with the assistant message it
        accompanied (per pika 2026-06-28 #260 — needed for per-message
        feedback targeting).
        """
        return self.append(
            user, session_id, 'action',
            json.dumps(action_frame, separators=(',', ':')),
            turn_seq=turn_seq, round_idx=round_idx,
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

    # ── ack (privacy-notice acknowledgement, per-user) ───────────────────────

    def is_acknowledged(self, user: str) -> bool:
        """True iff this user has accepted the privacy / data-handling
        notice. Acks survive across sessions + devices (the row is keyed
        on Authelia Remote-User, not session_id).
        """
        row = self._conn.execute(
            "SELECT 1 FROM ack WHERE user = ?", (user,),
        ).fetchone()
        return row is not None

    def acknowledge(self, user: str) -> None:
        """Set the user's acknowledged_at to now. Idempotent."""
        self._conn.execute(
            "INSERT OR REPLACE INTO ack(user, acknowledged_at) "
            "VALUES (?, ?)",
            (user, int(time.time())),
        )

    # ── feedback (per-message flag, triaged by the dev team) ────────────────

    VALID_FEEDBACK_CATEGORIES = (
        'wrong answer',
        'bad plot',
        'misinterpreted me',
        'could be better',
    )

    def append_feedback(
        self,
        user: str,
        session_id: str,
        turn_seq: int,
        round_idx: int,
        category: str,
        comment: Optional[str] = None,
    ) -> None:
        """Record a user-submitted feedback flag against one assistant message.

        ``category`` must be one of ``VALID_FEEDBACK_CATEGORIES``.
        ``comment`` is an optional one-line free text (truncated at 1024
        chars by the API endpoint).
        """
        if category not in self.VALID_FEEDBACK_CATEGORIES:
            raise ValueError(
                f"invalid feedback category {category!r} — must be one of "
                f"{self.VALID_FEEDBACK_CATEGORIES}"
            )
        self._conn.execute(
            "INSERT INTO feedback "
            "(user, session_id, turn_seq, round_idx, category, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, session_id, int(turn_seq), int(round_idx),
             category, comment, int(time.time())),
        )

    def recent_feedback(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent feedback rows (newest first) for admin
        triage. Joins back the assistant message content so reviewers
        don't have to look it up separately. Limit clamped to 1000.
        """
        limit = max(1, min(1000, int(limit)))
        rows = self._conn.execute(
            "SELECT f.created_at, f.user, f.session_id, f.turn_seq, "
            "       f.round_idx, f.category, f.comment, m.content "
            "FROM feedback f "
            "LEFT JOIN messages m "
            "  ON m.user = f.user "
            " AND m.session_id = f.session_id "
            " AND m.turn_seq = f.turn_seq "
            " AND m.round_idx = f.round_idx "
            "ORDER BY f.created_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                'created_at': int(ts),
                'user': user,
                'session_id': session_id,
                'turn_seq': int(turn_seq),
                'round_idx': int(round_idx),
                'category': category,
                'comment': comment,
                'message_content': content,
            }
            for ts, user, session_id, turn_seq, round_idx, category, comment, content
            in rows
        ]

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
