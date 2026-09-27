"""Personal MCP links: one derived, rotatable token per user.

A token is never stored. It is derived from a server secret:

    token = "amcp_" + public_id + "_" + b64url(HMAC-SHA256(secret, f"{public_id}:{generation}"))[:43]

The SQLite table keeps (user, public_id, generation, ...), so the link can be
re-displayed at any time, a database leak alone yields no working link, and
rotating (generation + 1) kills the previous link immediately.

Input: a state directory (see ``config.get_state_dir``) and the server secret.
Output: ``McpLinkStore`` with issue/rotate/revoke/verify/list operations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

TOKEN_PREFIX = 'amcp_'
_PUBLIC_ID_BYTES = 12            # 16 b64url chars
_MAC_CHARS = 43                  # full 256-bit HMAC in b64url, unpadded
_TOKEN_RE = re.compile(r'^amcp_([A-Za-z0-9_-]{16})_([A-Za-z0-9_-]{43})$')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def looks_like_token(value: str) -> bool:
    return bool(_TOKEN_RE.match(value or ''))


@dataclass
class LinkRecord:
    user: str
    public_id: str
    generation: int
    groups: List[str]
    created: datetime
    rotated_at: Optional[datetime]
    expires_at: Optional[datetime]
    revoked: bool
    last_used: Optional[datetime]
    last_ip: Optional[str]

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and _utcnow() >= self.expires_at

    @property
    def is_active(self) -> bool:
        return not self.revoked and not self.is_expired


class McpLinkStore:
    """SQLite-backed store of personal MCP links (thread-safe, WAL)."""

    def __init__(self, db_path: Path, secret: str, ttl_days: int = 90):
        if not secret:
            raise ValueError("McpLinkStore needs a non-empty server secret")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = secret.encode()
        self.ttl_days = int(ttl_days)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                     isolation_level=None, timeout=10)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS mcp_links (
                user        TEXT PRIMARY KEY,
                public_id   TEXT NOT NULL UNIQUE,
                generation  INTEGER NOT NULL DEFAULT 1,
                groups      TEXT NOT NULL DEFAULT '',
                created     TEXT NOT NULL,
                rotated_at  TEXT,
                expires_at  TEXT,
                revoked     INTEGER NOT NULL DEFAULT 0,
                last_used   TEXT,
                last_ip     TEXT
            )""")

    # ── token derivation ────────────────────────────────────────────────
    def _mac(self, public_id: str, generation: int) -> str:
        digest = hmac.new(self._secret, f"{public_id}:{generation}".encode(),
                          hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip('=')[:_MAC_CHARS]

    def token_for(self, record: LinkRecord) -> str:
        return f"{TOKEN_PREFIX}{record.public_id}_{self._mac(record.public_id, record.generation)}"

    # ── row helpers ────────────────────────────────────────────────────
    def _row_to_record(self, row) -> LinkRecord:
        (user, public_id, generation, groups, created, rotated_at,
         expires_at, revoked, last_used, last_ip) = row
        return LinkRecord(
            user=user, public_id=public_id, generation=int(generation),
            groups=[g for g in (groups or '').split(',') if g],
            created=_parse(created), rotated_at=_parse(rotated_at),
            expires_at=_parse(expires_at), revoked=bool(revoked),
            last_used=_parse(last_used), last_ip=last_ip,
        )

    _COLUMNS = ('user, public_id, generation, groups, created, rotated_at, '
                'expires_at, revoked, last_used, last_ip')

    def get(self, user: str) -> Optional[LinkRecord]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM mcp_links WHERE user = ?", [user]).fetchone()
        return self._row_to_record(row) if row else None

    def _new_expiry(self, now: datetime) -> Optional[datetime]:
        return now + timedelta(days=self.ttl_days) if self.ttl_days > 0 else None

    @staticmethod
    def _groups_text(groups) -> str:
        return ','.join(sorted({g.strip() for g in (groups or []) if g and g.strip()}))

    # ── mutations ──────────────────────────────────────────────────────
    def rotate(self, user: str, groups: Optional[List[str]] = None) -> LinkRecord:
        """Issue the first link or replace the current one (old dies now)."""
        if not user:
            raise ValueError("user is required")
        now = _utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT generation FROM mcp_links WHERE user = ?", [user]).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO mcp_links (user, public_id, generation, groups, created, "
                    "rotated_at, expires_at, revoked) VALUES (?, ?, 1, ?, ?, NULL, ?, 0)",
                    [user, secrets.token_urlsafe(_PUBLIC_ID_BYTES), self._groups_text(groups),
                     _iso(now), _iso(self._new_expiry(now))])
            else:
                params = [_iso(now), _iso(self._new_expiry(now))]
                groups_sql = ''
                if groups is not None:
                    groups_sql = ', groups = ?'
                    params.append(self._groups_text(groups))
                self._conn.execute(
                    "UPDATE mcp_links SET generation = generation + 1, rotated_at = ?, "
                    f"expires_at = ?, revoked = 0, last_used = NULL, last_ip = NULL{groups_sql} "
                    "WHERE user = ?", params + [user])
        return self.get(user)

    def revoke(self, user: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE mcp_links SET revoked = 1, generation = generation + 1 WHERE user = ?",
                [user])
        return cursor.rowcount > 0

    def update_groups(self, user: str, groups: List[str]) -> None:
        with self._lock:
            self._conn.execute("UPDATE mcp_links SET groups = ? WHERE user = ?",
                               [self._groups_text(groups), user])

    # ── verification ───────────────────────────────────────────────────
    def verify(self, token: str, client_ip: Optional[str] = None) -> Optional[LinkRecord]:
        """Return the owner's record for a valid, active token, else None."""
        match = _TOKEN_RE.match(token or '')
        if not match:
            return None
        public_id, presented_mac = match.groups()
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM mcp_links WHERE public_id = ?",
                [public_id]).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        expected = self._mac(record.public_id, record.generation)
        if not hmac.compare_digest(expected, presented_mac) or not record.is_active:
            return None
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE mcp_links SET last_used = ?, last_ip = ? WHERE user = ?",
                [_iso(now), client_ip, record.user])
        record.last_used, record.last_ip = now, client_ip
        return record

    def list_all(self) -> List[LinkRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM mcp_links ORDER BY user").fetchall()
        return [self._row_to_record(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── presentation helpers (shared by ant-serve endpoints) ──────────────────

def link_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/mcp/u/{token}"


def client_snippets(url: str, server_name: str = 'anthive') -> Dict[str, str]:
    json_config = (
        '{\n'
        '  "mcpServers": {\n'
        f'    "{server_name}": {{\n'
        '      "type": "http",\n'
        f'      "url": "{url}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    return {
        'claude_code': f"claude mcp add --transport http {server_name} {url}",
        'json_config': json_config,
    }


def describe(record: Optional[LinkRecord], store: McpLinkStore, base_url: str,
             user: str, enabled: bool) -> Dict[str, Any]:
    """The GET /me/mcp response body."""
    empty = {'claude_code': '', 'json_config': ''}
    body: Dict[str, Any] = {
        'enabled': bool(enabled), 'user': user, 'has_link': False, 'url': None,
        'created': None, 'rotated_at': None, 'expires_at': None, 'last_used': None,
        'ttl_days': store.ttl_days if store else 0, 'snippets': empty,
    }
    if not enabled or record is None or not record.is_active:
        if record is not None and enabled:
            body.update(created=_iso(record.created), rotated_at=_iso(record.rotated_at),
                        expires_at=_iso(record.expires_at), last_used=_iso(record.last_used))
        return body
    url = link_url(base_url, store.token_for(record))
    body.update(
        has_link=True, url=url, created=_iso(record.created),
        rotated_at=_iso(record.rotated_at), expires_at=_iso(record.expires_at),
        last_used=_iso(record.last_used), snippets=client_snippets(url),
    )
    return body


def admin_row(record: LinkRecord) -> Dict[str, Any]:
    return {
        'user': record.user, 'has_link': record.is_active,
        'created': _iso(record.created), 'rotated_at': _iso(record.rotated_at),
        'expires_at': _iso(record.expires_at), 'last_used': _iso(record.last_used),
        'last_ip': record.last_ip, 'revoked': record.revoked,
    }
