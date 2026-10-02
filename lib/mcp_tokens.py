"""Personal MCP links: several named, rotatable tokens per user.

A token is never stored. It is derived from a server secret:

    token = "amcp_" + public_id + "_" + b64url(HMAC-SHA256(secret, f"{public_id}:{generation}"))[:43]

The SQLite table keeps one row per link (id, user, label, public_id,
generation, ...), so a link can be re-displayed at any time, a database leak
alone yields no working link, and rotating a link (generation + 1) kills its
previous token immediately without touching the user's other links.

Input: a state directory (see ``config.get_state_dir``) and the server secret.
Output: ``McpLinkStore`` with create/rotate/revoke/verify/list operations.
Databases from the single-link era (one row per user, ``user`` primary key)
are migrated in place on open; their tokens keep working.
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
LABEL_MAX_CHARS = 40
DEFAULT_MAX_LINKS = 5


class LinkLimitError(ValueError):
    """The user already has the maximum number of links."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def looks_like_token(value: str) -> bool:
    return bool(_TOKEN_RE.match(value or ''))


def clean_label(label: Optional[str]) -> str:
    text = ' '.join(str(label or '').split())
    return text[:LABEL_MAX_CHARS]


@dataclass
class LinkRecord:
    id: int
    user: str
    label: str
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


_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS mcp_links (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user        TEXT NOT NULL,
        label       TEXT NOT NULL DEFAULT '',
        public_id   TEXT NOT NULL UNIQUE,
        generation  INTEGER NOT NULL DEFAULT 1,
        groups      TEXT NOT NULL DEFAULT '',
        created     TEXT NOT NULL,
        rotated_at  TEXT,
        expires_at  TEXT,
        revoked     INTEGER NOT NULL DEFAULT 0,
        last_used   TEXT,
        last_ip     TEXT
    )"""


class McpLinkStore:
    """SQLite-backed store of personal MCP links (thread-safe, WAL)."""

    def __init__(self, db_path: Path, secret: str, ttl_days: int = 90,
                 max_links_per_user: int = DEFAULT_MAX_LINKS):
        if not secret:
            raise ValueError("McpLinkStore needs a non-empty server secret")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = secret.encode()
        self.ttl_days = int(ttl_days)
        self.max_links = max(1, int(max_links_per_user or DEFAULT_MAX_LINKS))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                     isolation_level=None, timeout=10)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._migrate()

    def _migrate(self) -> None:
        """Create the table, or convert a single-link table (user primary key)
        into the multi-link layout in one transaction. Rows keep their
        public_id and generation, so existing tokens stay valid."""
        with self._lock:
            columns = [row[1] for row in self._conn.execute("PRAGMA table_info(mcp_links)")]
            if not columns:
                self._conn.execute(_CREATE_TABLE)
            elif 'id' not in columns:
                self._conn.execute('BEGIN IMMEDIATE')
                try:
                    self._conn.execute("ALTER TABLE mcp_links RENAME TO mcp_links_single")
                    self._conn.execute(_CREATE_TABLE)
                    self._conn.execute(
                        "INSERT INTO mcp_links (user, label, public_id, generation, groups, "
                        "created, rotated_at, expires_at, revoked, last_used, last_ip) "
                        "SELECT user, 'default', public_id, generation, groups, created, "
                        "rotated_at, expires_at, revoked, last_used, last_ip "
                        "FROM mcp_links_single ORDER BY user")
                    self._conn.execute("DROP TABLE mcp_links_single")
                    self._conn.execute('COMMIT')
                except Exception:
                    self._conn.execute('ROLLBACK')
                    raise
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS mcp_links_user ON mcp_links (user)")

    # ── token derivation ────────────────────────────────────────────────
    def _mac(self, public_id: str, generation: int) -> str:
        digest = hmac.new(self._secret, f"{public_id}:{generation}".encode(),
                          hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip('=')[:_MAC_CHARS]

    def token_for(self, record: LinkRecord) -> str:
        return f"{TOKEN_PREFIX}{record.public_id}_{self._mac(record.public_id, record.generation)}"

    # ── row helpers ────────────────────────────────────────────────────
    _COLUMNS = ('id, user, label, public_id, generation, groups, created, rotated_at, '
                'expires_at, revoked, last_used, last_ip')

    def _row_to_record(self, row) -> LinkRecord:
        (link_id, user, label, public_id, generation, groups, created, rotated_at,
         expires_at, revoked, last_used, last_ip) = row
        return LinkRecord(
            id=int(link_id), user=user, label=label or '', public_id=public_id,
            generation=int(generation),
            groups=[g for g in (groups or '').split(',') if g],
            created=_parse(created), rotated_at=_parse(rotated_at),
            expires_at=_parse(expires_at), revoked=bool(revoked),
            last_used=_parse(last_used), last_ip=last_ip,
        )

    def _fetch(self, where: str, params: list) -> List[LinkRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM mcp_links WHERE {where} ORDER BY id",
                params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_link(self, user: str, link_id: int) -> Optional[LinkRecord]:
        found = self._fetch("id = ? AND user = ?", [int(link_id), user])
        return found[0] if found else None

    def links_for(self, user: str) -> List[LinkRecord]:
        """The user's links that are not revoked (active or expired)."""
        return self._fetch("user = ? AND revoked = 0", [user])

    def get(self, user: str) -> Optional[LinkRecord]:
        """The user's first non-revoked link; else their most recent revoked
        one (so callers can still show it as inactive); else None."""
        live = self.links_for(user)
        if live:
            return live[0]
        revoked = self._fetch("user = ? AND revoked = 1", [user])
        return revoked[-1] if revoked else None

    def _new_expiry(self, now: datetime) -> Optional[datetime]:
        return now + timedelta(days=self.ttl_days) if self.ttl_days > 0 else None

    @staticmethod
    def _groups_text(groups) -> str:
        return ','.join(sorted({g.strip() for g in (groups or []) if g and g.strip()}))

    # ── mutations ──────────────────────────────────────────────────────
    def create(self, user: str, label: Optional[str] = None,
               groups: Optional[List[str]] = None) -> LinkRecord:
        """Add a new link for the user (LinkLimitError at the cap)."""
        if not user:
            raise ValueError("user is required")
        now = _utcnow()
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM mcp_links WHERE user = ? AND revoked = 0",
                [user]).fetchone()[0]
            if count >= self.max_links:
                raise LinkLimitError(
                    f"you already have {count} MCP links (the maximum is {self.max_links}); "
                    f"revoke one first")
            name = clean_label(label) or f"link {count + 1}"
            cursor = self._conn.execute(
                "INSERT INTO mcp_links (user, label, public_id, generation, groups, created, "
                "rotated_at, expires_at, revoked) VALUES (?, ?, ?, 1, ?, ?, NULL, ?, 0)",
                [user, name, secrets.token_urlsafe(_PUBLIC_ID_BYTES), self._groups_text(groups),
                 _iso(now), _iso(self._new_expiry(now))])
            link_id = cursor.lastrowid
        return self.get_link(user, link_id)

    def rotate_link(self, user: str, link_id: int,
                    groups: Optional[List[str]] = None) -> Optional[LinkRecord]:
        """New token for one link (its old token dies now); None if not the user's."""
        now = _utcnow()
        params: list = [_iso(now), _iso(self._new_expiry(now))]
        groups_sql = ''
        if groups is not None:
            groups_sql = ', groups = ?'
            params.append(self._groups_text(groups))
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE mcp_links SET generation = generation + 1, rotated_at = ?, "
                f"expires_at = ?, revoked = 0, last_used = NULL, last_ip = NULL{groups_sql} "
                "WHERE id = ? AND user = ?", params + [int(link_id), user])
        return self.get_link(user, link_id) if cursor.rowcount else None

    def revoke_link(self, link_id: int, user: Optional[str] = None) -> bool:
        """Revoke one link (scoped to ``user`` unless an admin passes None)."""
        sql = "UPDATE mcp_links SET revoked = 1, generation = generation + 1 WHERE id = ? AND revoked = 0"
        params: list = [int(link_id)]
        if user is not None:
            sql += " AND user = ?"
            params.append(user)
        with self._lock:
            cursor = self._conn.execute(sql, params)
        return cursor.rowcount > 0

    def rotate(self, user: str, groups: Optional[List[str]] = None) -> LinkRecord:
        """Single-link API: rotate the user's first link, or issue one.
        A user whose links are all revoked gets their latest link back."""
        if not user:
            raise ValueError("user is required")
        current = self.get(user)
        if current is None:
            return self.create(user, 'default', groups)
        return self.rotate_link(user, current.id, groups)

    def revoke(self, user: str) -> bool:
        """Revoke every link of the user."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE mcp_links SET revoked = 1, generation = generation + 1 "
                "WHERE user = ? AND revoked = 0", [user])
        return cursor.rowcount > 0

    def update_groups(self, user: str, groups: List[str]) -> None:
        with self._lock:
            self._conn.execute("UPDATE mcp_links SET groups = ? WHERE user = ?",
                               [self._groups_text(groups), user])

    # ── verification ───────────────────────────────────────────────────
    def verify(self, token: str, client_ip: Optional[str] = None) -> Optional[LinkRecord]:
        """Return the link's record for a valid, active token, else None."""
        match = _TOKEN_RE.match(token or '')
        if not match:
            return None
        public_id, presented_mac = match.groups()
        found = self._fetch("public_id = ?", [public_id])
        if not found:
            return None
        record = found[0]
        expected = self._mac(record.public_id, record.generation)
        if not hmac.compare_digest(expected, presented_mac) or not record.is_active:
            return None
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE mcp_links SET last_used = ?, last_ip = ? WHERE id = ?",
                [_iso(now), client_ip, record.id])
        record.last_used, record.last_ip = now, client_ip
        return record

    def list_all(self) -> List[LinkRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM mcp_links ORDER BY user, id").fetchall()
        return [self._row_to_record(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── presentation helpers (shared by ant-serve endpoints) ──────────────────

def link_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/mcp/u/{token}"


def client_snippets(url: str, server_name: str = 'anthive') -> Dict[str, str]:
    """Copy-ready setup for the common clients, keyed by client.

    doc/MCP_CLIENTS.md has the full per-client guide; keep the two in step.
    ``json_config`` (the Claude Code ``.mcp.json`` shape) predates the others
    and is kept for API clients that read it.
    """
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
        'claude_code_user': f"claude mcp add --transport http --scope user {server_name} {url}",
        'json_config': json_config,
        # Cursor, Kiro, LM Studio, JetBrains AI Assistant, Junie, Pi
        'json_url': '{ "mcpServers": { "%s": { "url": "%s" } } }' % (server_name, url),
        'vscode_json': '{ "servers": { "%s": { "type": "http", "url": "%s" } } }' % (server_name, url),
        'codex': f"codex mcp add {server_name} --url {url}",
        'gemini_cli': f"gemini mcp add --scope user --transport http {server_name} {url}",
        'mcp_remote': ('{ "mcpServers": { "%s": { "command": "npx", '
                       '"args": ["-y", "mcp-remote", "%s", "--transport", "http-only"] } } }'
                       % (server_name, url)),
    }


SNIPPETS_TEMPLATE = client_snippets('{url}')


def link_entry(record: LinkRecord, store: McpLinkStore, base_url: str) -> Dict[str, Any]:
    """One link as shown to its owner."""
    url = link_url(base_url, store.token_for(record)) if record.is_active else None
    return {
        'id': record.id, 'label': record.label, 'url': url, 'active': record.is_active,
        'created': _iso(record.created), 'rotated_at': _iso(record.rotated_at),
        'expires_at': _iso(record.expires_at), 'last_used': _iso(record.last_used),
    }


def describe(record: Optional[LinkRecord], store: McpLinkStore, base_url: str,
             user: str, enabled: bool,
             links: Optional[List[LinkRecord]] = None) -> Dict[str, Any]:
    """The GET /me/mcp response body.

    ``links`` lists every non-revoked link of the user; the top-level
    has_link/url/created/... fields describe ``record`` (the first link) and
    are kept for clients written against the single-link API.
    """
    empty = {key: '' for key in SNIPPETS_TEMPLATE}
    body: Dict[str, Any] = {
        'enabled': bool(enabled), 'user': user, 'has_link': False, 'url': None,
        'created': None, 'rotated_at': None, 'expires_at': None, 'last_used': None,
        'ttl_days': store.ttl_days if store else 0, 'snippets': empty,
        'links': [], 'max_links': store.max_links if store else 0,
        'snippets_template': SNIPPETS_TEMPLATE,
    }
    if not enabled:
        return body
    body['links'] = [link_entry(r, store, base_url) for r in (links or [])]
    if record is None:
        return body
    body.update(created=_iso(record.created), rotated_at=_iso(record.rotated_at),
                expires_at=_iso(record.expires_at), last_used=_iso(record.last_used))
    if record.is_active:
        url = link_url(base_url, store.token_for(record))
        body.update(has_link=True, url=url, snippets=client_snippets(url))
    return body


def admin_row(record: LinkRecord) -> Dict[str, Any]:
    return {
        'id': record.id, 'user': record.user, 'label': record.label,
        'has_link': record.is_active,
        'created': _iso(record.created), 'rotated_at': _iso(record.rotated_at),
        'expires_at': _iso(record.expires_at), 'last_used': _iso(record.last_used),
        'last_ip': record.last_ip, 'revoked': record.revoked,
    }
