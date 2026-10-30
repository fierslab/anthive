"""journal — comments, reports and usage events attached to datasets (doc/DESIGN_dataset_journal.md).

A per-site SQLite store (``<state_dir>/journal.sqlite`` by default, ``journal.db_path``) with three
things in it:

* **entries**: notes people write (comment, question, finding, link, used-in, data-error,
  annotation-concern, data-request) and events the server records (used-in-analysis as declared by a
  client, artefact-made, deposited, version-published). Every entry names a target (dataset,
  collection, gene set, facet or artefact) and, for a dataset, the dataset version it was written
  against. Notes can carry typed links (ELN, DOI, ...), pointers into the data (a column, a gene, a cell
  type) and replies. Data-error, annotation-concern and data-request notes have a status
  (open, acknowledged, fixed, declined). Nothing is ever deleted: an edit keeps the old text in
  ``history``, an admin hides an entry instead of removing it.
* **downloads**: who downloaded which file, kept ``download_retention_days`` and shown to ordinary
  users only as counts (admins and the dataset's depositor see names).
* **seen**: the dataset versions and artefact versions already turned into events, so the catalog can
  be compared with the journal cheaply after every scan.

Input: calls from ant-serve, which supplies the caller (:class:`Actor`) and decides what the caller may
see. Output: plain dicts ready to be returned as JSON. No network, no HTTP; one connection per store
guarded by a lock (ant-serve's thread pool shares it).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

TARGET_TYPES = ('dataset', 'collection', 'geneset', 'facet', 'artefact')
NOTE_CATEGORIES = ('comment', 'question', 'finding', 'link', 'used-in',
                   'data-error', 'annotation-concern', 'data-request')
EVENT_CATEGORIES = ('downloaded', 'used-in-analysis', 'artefact-made', 'deposited', 'version-published')
#: notes with a status
ACTIONABLE = ('data-error', 'annotation-concern', 'data-request')
#: closing one of these as fixed needs a changelog reference and the new dataset version
NEEDS_CHANGELOG = ('data-error', 'annotation-concern')
STATUSES = ('open', 'acknowledged', 'fixed', 'declined')
OPEN_STATUSES = ('open', 'acknowledged')
#: categories a reply may have (replies never carry a status)
REPLY_CATEGORIES = ('comment', 'question', 'finding', 'link')
LINK_TYPES = ('eln', 'doi', 'artefact', 'protocol', 'manuscript', 'other')
REF_KEYS = ('column', 'value', 'gene', 'genes', 'cell_type', 'evidence')
DOWNLOAD_KINDS = ('h5ad', 'static', 'artefact')

DEFAULT_MAX_BODY_CHARS = 10000
MAX_LINKS = 20
MAX_URL_CHARS = 2000
MAX_LABEL_CHARS = 200
MAX_REF_CHARS = 2000
MAX_REF_LIST = 100
SYSTEM_AUTHOR = 'anthive'

_TARGET_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/@+ -]{0,199}$')
_URL_RE = re.compile(r'^https?://[^\s<>"]+$', re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, dataset_version TEXT,
    author TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL, category TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '[]', refs TEXT NOT NULL DEFAULT '{}',
    parent_id INTEGER, status TEXT, status_by TEXT, status_at REAL, status_note TEXT,
    fixed_in_version TEXT, changelog_ref TEXT,
    about_type TEXT, about_id TEXT,
    edited REAL, hidden INTEGER NOT NULL DEFAULT 0, hidden_by TEXT, hidden_at REAL, hidden_reason TEXT
);
CREATE INDEX IF NOT EXISTS entries_target ON entries(target_type, target_id, created);
CREATE INDEX IF NOT EXISTS entries_open ON entries(category, status);
CREATE INDEX IF NOT EXISTS entries_parent ON entries(parent_id);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, before TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS history_entry ON history(entry_id);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    dataset_version TEXT, user TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, what TEXT NOT NULL, name TEXT
);
CREATE INDEX IF NOT EXISTS downloads_target ON downloads(target_type, target_id, ts);
CREATE INDEX IF NOT EXISTS downloads_ts ON downloads(ts);
CREATE TABLE IF NOT EXISTS seen (kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_JSON_FIELDS = ('links', 'refs')
_HISTORY_FIELDS = ('body', 'links', 'refs', 'category', 'status', 'status_note', 'fixed_in_version',
                   'changelog_ref', 'hidden', 'hidden_reason')


class JournalError(Exception):
    """A refusal with an HTTP status and a JSON body ``{'status': <word>, 'message': ...}``."""

    def __init__(self, status: int, word: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.body: Dict[str, Any] = {'status': word, 'message': message, **extra}


@dataclass(frozen=True)
class Actor:
    """Who asks. ``user`` is the login name ('' for an anonymous caller); ``may_triage`` is a member of the
    triage groups (admins and curators by default); ``owns`` is True when the caller is the depositor or
    assigned owner of the target dataset (filled by ant-serve per request)."""
    user: str
    is_admin: bool = False
    may_triage: bool = False
    owns: bool = False

    @property
    def signed_in(self) -> bool:
        return bool(self.user)


def iso(stamp: Optional[float]) -> Optional[str]:
    """A unix time as ISO 8601 UTC with seconds, or None."""
    if stamp is None:
        return None
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text: Any) -> Optional[float]:
    """An ISO 8601 date or time (as written by the artefact store) as a unix time, or None."""
    if not text:
        return None
    value = str(text).strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ── validation ─────────────────────────────────────────────────────────────────

def check_target(target_type: Any, target_id: Any) -> Tuple[str, str]:
    """A valid (target_type, target_id) pair; JournalError 400 otherwise."""
    kind = str(target_type or '').strip().lower()
    if kind not in TARGET_TYPES:
        raise JournalError(400, 'bad_target', f"target_type is one of {', '.join(TARGET_TYPES)}")
    ident = str(target_id or '').strip()
    if not _TARGET_ID_RE.fullmatch(ident) or '..' in ident:
        raise JournalError(400, 'bad_target', 'target_id is missing or has characters a target id never has')
    return kind, ident


def clean_body(body: Any, limit: int = DEFAULT_MAX_BODY_CHARS, required: bool = True) -> str:
    """The note text, stripped, control characters other than newline and tab removed, at most ``limit``
    characters (longer text is refused, not cut)."""
    text = '' if body is None else str(body)
    text = ''.join(ch for ch in text if ch in '\n\t' or ord(ch) >= 32).strip()
    if required and not text:
        raise JournalError(400, 'empty_body', 'the note has no text')
    if len(text) > limit:
        raise JournalError(400, 'body_too_long', f'the note is {len(text)} characters; the limit is {limit}')
    return text


def clean_links(links: Any) -> List[Dict[str, str]]:
    """Typed links: ``[{url, label, type}]``, http(s) only, at most :data:`MAX_LINKS`."""
    if links in (None, ''):
        return []
    if not isinstance(links, list):
        raise JournalError(400, 'bad_links', 'links is a list of {url, label, type}')
    if len(links) > MAX_LINKS:
        raise JournalError(400, 'bad_links', f'at most {MAX_LINKS} links per note')
    out: List[Dict[str, str]] = []
    for item in links:
        if isinstance(item, str):
            item = {'url': item}
        if not isinstance(item, dict):
            raise JournalError(400, 'bad_links', 'each link is {url, label, type}')
        url = str(item.get('url') or '').strip()
        if len(url) > MAX_URL_CHARS or not _URL_RE.fullmatch(url):
            raise JournalError(400, 'bad_links', f'not an http(s) link: {url[:100]!r}')
        link_type = str(item.get('type') or 'other').strip().lower()
        if link_type not in LINK_TYPES:
            raise JournalError(400, 'bad_links', f"link type is one of {', '.join(LINK_TYPES)}")
        label = clean_body(item.get('label'), MAX_LABEL_CHARS, required=False).replace('\n', ' ')
        out.append({'url': url, 'label': label, 'type': link_type})
    return out


def clean_refs(refs: Any) -> Dict[str, Any]:
    """Pointers into the data: ``column``, ``value``, ``gene``, ``cell_type``, ``evidence`` (strings) and
    ``genes`` (a list of strings)."""
    if refs in (None, ''):
        return {}
    if not isinstance(refs, dict):
        raise JournalError(400, 'bad_refs', f"refs is an object with the keys {', '.join(REF_KEYS)}")
    unknown = sorted(set(refs) - set(REF_KEYS))
    if unknown:
        raise JournalError(400, 'bad_refs', f"unknown refs key(s) {', '.join(unknown)} "
                                            f"(known: {', '.join(REF_KEYS)})")
    out: Dict[str, Any] = {}
    for key, value in refs.items():
        if value in (None, '', []):
            continue
        if key == 'genes':
            if not isinstance(value, list) or len(value) > MAX_REF_LIST:
                raise JournalError(400, 'bad_refs', f'refs.genes is a list of at most {MAX_REF_LIST} gene names')
            out[key] = [clean_body(gene, MAX_LABEL_CHARS) for gene in value]
        else:
            if isinstance(value, (dict, list)):
                raise JournalError(400, 'bad_refs', f'refs.{key} is a string')
            out[key] = clean_body(value, MAX_REF_CHARS)
    return out


def check_category(category: Any, allowed: Sequence[str] = NOTE_CATEGORIES) -> str:
    value = str(category or '').strip().lower()
    if value not in allowed:
        raise JournalError(400, 'bad_category', f"category is one of {', '.join(allowed)}")
    return value


# ── the store ──────────────────────────────────────────────────────────────────

class JournalStore:
    """The journal's SQLite file. All methods are thread-safe (one connection, one lock)."""

    def __init__(self, path: Path, max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_body_chars = int(max_body_chars)
        self._clock = clock
        self._lock = Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA busy_timeout=5000')
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # low level

    def _rows(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, tuple(args)).fetchall()]

    def _execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(args))
            return int(cursor.lastrowid or 0)

    def _row(self, entry_id: int) -> Optional[Dict[str, Any]]:
        rows = self._rows('SELECT * FROM entries WHERE id = ?', (int(entry_id),))
        return rows[0] if rows else None

    def _history(self, entry_id: int, actor: str, action: str, before: Mapping[str, Any]) -> None:
        kept = {key: before.get(key) for key in _HISTORY_FIELDS if key in before}
        self._execute('INSERT INTO history (entry_id, ts, actor, action, before) VALUES (?, ?, ?, ?, ?)',
                      (int(entry_id), self._clock(), actor, action, json.dumps(kept)))

    def meta_get(self, key: str) -> Optional[str]:
        rows = self._rows('SELECT value FROM meta WHERE key = ?', (key,))
        return rows[0]['value'] if rows else None

    def meta_set(self, key: str, value: str) -> None:
        self._execute('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                      (key, value))

    # entries

    def insert(self, *, target_type: str, target_id: str, author: str, kind: str, category: str,
               body: str = '', links: Optional[List[Dict[str, str]]] = None, refs: Optional[Dict[str, Any]] = None,
               dataset_version: Optional[str] = None, parent_id: Optional[int] = None,
               status: Optional[str] = None, about_type: Optional[str] = None, about_id: Optional[str] = None,
               created: Optional[float] = None) -> int:
        """Add one entry exactly as given (validation is the caller's); returns its id."""
        return self._execute(
            'INSERT INTO entries (target_type, target_id, dataset_version, author, created, kind, category, body, '
            'links, refs, parent_id, status, about_type, about_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (target_type, target_id, dataset_version, author, created if created is not None else self._clock(),
             kind, category, body, json.dumps(links or []), json.dumps(refs or {}), parent_id, status,
             about_type, about_id))

    def get(self, entry_id: int) -> Optional[Dict[str, Any]]:
        return self._row(entry_id)

    def entries(self, *, target_type: Optional[str] = None, target_id: Optional[str] = None,
                category: Optional[Sequence[str]] = None, status: Optional[Sequence[str]] = None,
                kind: Optional[str] = None, include_hidden: bool = False, parent_id: Any = 'any',
                before_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Entries newest first. ``parent_id`` None: top-level entries only; an id: that entry's replies."""
        where, args = [], []
        if target_type:
            where.append('target_type = ?')
            args.append(target_type)
        if target_id:
            where.append('target_id = ?')
            args.append(target_id)
        if category:
            where.append(f"category IN ({','.join('?' * len(category))})")
            args.extend(category)
        if status:
            where.append(f"status IN ({','.join('?' * len(status))})")
            args.extend(status)
        if kind:
            where.append('kind = ?')
            args.append(kind)
        if not include_hidden:
            where.append('hidden = 0')
        if parent_id is None:
            where.append('parent_id IS NULL')
        elif parent_id != 'any':
            where.append('parent_id = ?')
            args.append(int(parent_id))
        if before_id:
            where.append('id < ?')
            args.append(int(before_id))
        sql = 'SELECT * FROM entries' + (' WHERE ' + ' AND '.join(where) if where else '')
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(max(1, min(int(limit), 1000)))
        return self._rows(sql, args)

    def replies_of(self, entry_ids: Sequence[int], include_hidden: bool = False) -> Dict[int, List[Dict[str, Any]]]:
        if not entry_ids:
            return {}
        sql = (f"SELECT * FROM entries WHERE parent_id IN ({','.join('?' * len(entry_ids))})"
               + ('' if include_hidden else ' AND hidden = 0') + ' ORDER BY id')
        out: Dict[int, List[Dict[str, Any]]] = {}
        for row in self._rows(sql, list(entry_ids)):
            out.setdefault(int(row['parent_id']), []).append(row)
        return out

    def update(self, entry_id: int, actor: str, action: str, **fields: Any) -> None:
        before = self._row(entry_id) or {}
        self._history(entry_id, actor, action, before)
        for key in _JSON_FIELDS:
            if key in fields:
                fields[key] = json.dumps(fields[key])
        sets = ', '.join(f'{key} = ?' for key in fields)
        self._execute(f'UPDATE entries SET {sets} WHERE id = ?', [*fields.values(), int(entry_id)])

    def history(self, entry_id: int) -> List[Dict[str, Any]]:
        return self._rows('SELECT * FROM history WHERE entry_id = ? ORDER BY id', (int(entry_id),))

    # downloads

    def record_download(self, target_type: str, target_id: str, what: str, user: str = '',
                        dataset_version: Optional[str] = None, name: Optional[str] = None) -> None:
        self._execute('INSERT INTO downloads (target_type, target_id, dataset_version, user, ts, what, name) '
                      'VALUES (?, ?, ?, ?, ?, ?, ?)',
                      (target_type, target_id, dataset_version, user or '', self._clock(), what, name))

    def download_summary(self, target_type: str, target_id: str) -> Dict[str, Any]:
        rows = self._rows("SELECT COUNT(*) AS n, COUNT(DISTINCT NULLIF(user, '')) AS people, "
                          "SUM(CASE WHEN user = '' THEN 1 ELSE 0 END) AS anonymous, MAX(ts) AS last "
                          'FROM downloads WHERE target_type = ? AND target_id = ?', (target_type, target_id))
        row = rows[0]
        return {'n_downloads': int(row['n'] or 0), 'n_people': int(row['people'] or 0),
                'n_anonymous': int(row['anonymous'] or 0), 'last_download': iso(row['last'])}

    def download_rows(self, target_type: str, target_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self._rows('SELECT * FROM downloads WHERE target_type = ? AND target_id = ? ORDER BY id DESC LIMIT ?',
                          (target_type, target_id, max(1, min(int(limit), 5000))))

    def purge_downloads(self, retention_days: float) -> int:
        """Delete download rows older than ``retention_days`` (0 keeps everything); returns how many."""
        if not retention_days or retention_days <= 0:
            return 0
        cutoff = self._clock() - float(retention_days) * 86400.0
        with self._lock:
            cursor = self._conn.execute('DELETE FROM downloads WHERE ts < ?', (cutoff,))
            return int(cursor.rowcount or 0)

    # seen

    def seen(self, kind: str) -> Dict[str, Optional[str]]:
        return {row['key']: row['value'] for row in self._rows('SELECT key, value FROM seen WHERE kind = ?', (kind,))}

    def mark_seen(self, kind: str, key: str, value: Optional[str]) -> None:
        self._execute('INSERT INTO seen (kind, key, value) VALUES (?, ?, ?) '
                      'ON CONFLICT(kind, key) DO UPDATE SET value = excluded.value', (kind, key, value))


class WriteLimiter:
    """At most ``per_minute`` writes per user in any 60 seconds (0: no limit). In memory, per process."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time):
        self.per_minute = int(per_minute)
        self._clock = clock
        self._hits: Dict[str, List[float]] = {}
        self._lock = Lock()

    def check(self, user: str) -> None:
        if self.per_minute <= 0:
            return
        now = self._clock()
        with self._lock:
            recent = [stamp for stamp in self._hits.get(user, []) if now - stamp < 60.0]
            if len(recent) >= self.per_minute:
                self._hits[user] = recent
                raise JournalError(429, 'too_many_writes',
                                   f'at most {self.per_minute} journal writes per minute; try again shortly')
            recent.append(now)
            self._hits[user] = recent


# ── the service: rules on top of the store ────────────────────────────────────

Visible = Callable[[str, str], bool]


class JournalService:
    """The journal's rules. ``visible(target_type, target_id)`` answers whether the current caller may see a
    target; ant-serve builds it per request from the dataset access rules and the artefact policy."""

    def __init__(self, store: JournalStore, retention_days: float = 730.0, writes_per_minute: int = 30):
        self.store = store
        self.retention_days = float(retention_days)
        self.limiter = WriteLimiter(writes_per_minute, clock=store._clock)

    # presentation

    @staticmethod
    def _public(row: Mapping[str, Any], actor: Actor) -> Dict[str, Any]:
        out = {
            'id': int(row['id']), 'target_type': row['target_type'], 'target_id': row['target_id'],
            'dataset_version': row['dataset_version'], 'author': row['author'], 'created': iso(row['created']),
            'kind': row['kind'], 'category': row['category'], 'body': row['body'],
            'links': json.loads(row['links'] or '[]'), 'refs': json.loads(row['refs'] or '{}'),
            'parent_id': row['parent_id'], 'edited': iso(row['edited']),
        }
        if row['category'] in ACTIONABLE and row['parent_id'] is None:
            out.update(status=row['status'], status_by=row['status_by'], status_at=iso(row['status_at']),
                       status_note=row['status_note'], fixed_in_version=row['fixed_in_version'],
                       changelog_ref=row['changelog_ref'])
        if row['about_type']:
            out['about'] = {'type': row['about_type'], 'id': row['about_id']}
        if row['hidden']:
            out.update(hidden=True, hidden_by=row['hidden_by'], hidden_at=iso(row['hidden_at']),
                       hidden_reason=row['hidden_reason'])
        out['may_edit'] = bool(actor.user) and row['author'] == actor.user and row['kind'] == 'note'
        return out

    def _shown(self, row: Mapping[str, Any], actor: Actor, visible: Visible) -> bool:
        if row['hidden'] and not actor.is_admin:
            return False
        if not visible(row['target_type'], row['target_id']):
            return False
        if row['about_type'] and not visible(row['about_type'], row['about_id']):
            return False
        return True

    def _require_entry(self, entry_id: int, actor: Actor, visible: Visible,
                       author_sees_hidden: bool = False) -> Dict[str, Any]:
        row = self.store.get(entry_id)
        if row is not None and author_sees_hidden and row['hidden'] and actor.user and row['author'] == actor.user:
            row_for_check = {**row, 'hidden': 0}
        else:
            row_for_check = row
        if row is None or not self._shown(row_for_check, actor, visible):
            raise JournalError(404, 'not_found', f'no journal entry {entry_id}')
        return row

    # reading

    def list(self, actor: Actor, visible: Visible, target_type: str, target_id: str, *,
             category: Optional[Sequence[str]] = None, status: Optional[Sequence[str]] = None,
             kind: Optional[str] = None, before_id: Optional[int] = None, limit: int = 100) -> Dict[str, Any]:
        """Top-level entries of one target, newest first, each with its replies; plus the download counts."""
        target_type, target_id = check_target(target_type, target_id)
        if not visible(target_type, target_id):
            raise JournalError(404, 'not_found', f'no {target_type} {target_id}')
        for value in category or ():
            check_category(value, NOTE_CATEGORIES + EVENT_CATEGORIES)
        for value in status or ():
            if value not in STATUSES:
                raise JournalError(400, 'bad_status', f"status is one of {', '.join(STATUSES)}")
        if kind is not None and kind not in ('note', 'event'):
            raise JournalError(400, 'bad_kind', 'kind is note or event')
        limit = max(1, min(int(limit), 500))
        rows = self.store.entries(target_type=target_type, target_id=target_id, category=category, status=status,
                                  kind=kind, include_hidden=actor.is_admin, parent_id=None, before_id=before_id,
                                  limit=limit + 1)
        more = len(rows) > limit
        rows = [row for row in rows[:limit] if self._shown(row, actor, visible)]
        replies = self.store.replies_of([int(row['id']) for row in rows], include_hidden=actor.is_admin)
        entries = []
        for row in rows:
            item = self._public(row, actor)
            item['replies'] = [self._public(reply, actor) for reply in replies.get(int(row['id']), [])
                               if self._shown(reply, actor, visible)]
            entries.append(item)
        out = {'target_type': target_type, 'target_id': target_id, 'entries': entries,
               'downloads': self.store.download_summary(target_type, target_id),
               'next_before_id': int(rows[-1]['id']) if more and rows else None,
               'may_set_status': self.may_set_status(actor)}
        return out

    @staticmethod
    def may_set_status(actor: Actor) -> bool:
        """Admins, triage groups and the target dataset's owner set report statuses."""
        return bool(actor.signed_in and (actor.is_admin or actor.may_triage or actor.owns))

    def get(self, actor: Actor, visible: Visible, entry_id: int) -> Dict[str, Any]:
        row = self._require_entry(entry_id, actor, visible)
        item = self._public(row, actor)
        item['replies'] = [self._public(reply, actor)
                           for reply in self.store.replies_of([int(row['id'])], actor.is_admin).get(int(row['id']), [])
                           if self._shown(reply, actor, visible)]
        return item

    def history(self, actor: Actor, visible: Visible, entry_id: int) -> Dict[str, Any]:
        row = self._require_entry(entry_id, actor, visible, author_sees_hidden=True)
        if not (actor.is_admin or row['author'] == actor.user):
            raise JournalError(403, 'forbidden', 'only the author and admins read the edit history')
        return {'id': int(entry_id), 'history': [
            {'ts': iso(item['ts']), 'actor': item['actor'], 'action': item['action'],
             'before': json.loads(item['before'] or '{}')} for item in self.store.history(entry_id)]}

    def open_issues(self, actor: Actor, visible: Visible, *, category: Optional[Sequence[str]] = None,
                    target_type: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        """Open and acknowledged data errors, annotation concerns and data requests across the site."""
        wanted = [check_category(value, ACTIONABLE) for value in (category or ACTIONABLE)]
        if target_type is not None and target_type not in TARGET_TYPES:
            raise JournalError(400, 'bad_target', f"target_type is one of {', '.join(TARGET_TYPES)}")
        limit = max(1, min(int(limit), 1000))
        issues: List[Dict[str, Any]] = []
        before_id: Optional[int] = None
        while len(issues) < limit:
            # page through the store so reports the caller may not see never push visible ones out
            rows = self.store.entries(category=wanted, status=list(OPEN_STATUSES), parent_id=None,
                                      target_type=target_type, include_hidden=False, before_id=before_id,
                                      limit=1000)
            issues.extend(self._public(row, actor) for row in rows if self._shown(row, actor, visible))
            if len(rows) < 1000:
                break
            before_id = int(rows[-1]['id'])
        issues = issues[:limit]
        counts: Dict[str, int] = {}
        for item in issues:
            counts[item['category']] = counts.get(item['category'], 0) + 1
        return {'issues': issues, 'counts': counts}

    # writing

    def _require_signed_in(self, actor: Actor) -> None:
        if not actor.signed_in:
            raise JournalError(401, 'sign_in_required', 'sign in to write in the journal')
        self.limiter.check(actor.user)

    def add_note(self, actor: Actor, visible: Visible, target_type: str, target_id: str, category: str,
                 body: Any, links: Any = None, refs: Any = None, dataset_version: Optional[str] = None,
                 parent_id: Optional[int] = None) -> Dict[str, Any]:
        """Write a note (or, with ``parent_id``, a reply). Actionable notes start as open."""
        self._require_signed_in(actor)
        target_type, target_id = check_target(target_type, target_id)
        if parent_id is not None:
            parent = self._require_entry(int(parent_id), actor, visible)
            if parent['parent_id'] is not None:
                parent = self._require_entry(int(parent['parent_id']), actor, visible)
            target_type, target_id = parent['target_type'], parent['target_id']
            category = check_category(category or 'comment', REPLY_CATEGORIES)
            parent_id = int(parent['id'])
        else:
            category = check_category(category)
        if not visible(target_type, target_id):
            raise JournalError(404, 'not_found', f'no {target_type} {target_id}')
        text = clean_body(body, self.store.max_body_chars, required=category != 'link')
        clean = clean_links(links)
        if category == 'link' and not clean:
            raise JournalError(400, 'bad_links', 'a link note needs at least one link')
        entry_id = self.store.insert(
            target_type=target_type, target_id=target_id, author=actor.user, kind='note', category=category,
            body=text, links=clean, refs=clean_refs(refs),
            dataset_version=dataset_version if target_type == 'dataset' else None, parent_id=parent_id,
            status='open' if category in ACTIONABLE and parent_id is None else None)
        return self.get(actor, visible, entry_id)

    def declare_use(self, actor: Actor, visible: Visible, target_type: str, target_id: str, body: Any = None,
                    links: Any = None, dataset_version: Optional[str] = None, tool: Optional[str] = None) -> Dict[str, Any]:
        """A client (vark, pyvark, a notebook) records that the caller used this dataset in an analysis."""
        self._require_signed_in(actor)
        target_type, target_id = check_target(target_type, target_id)
        if not visible(target_type, target_id):
            raise JournalError(404, 'not_found', f'no {target_type} {target_id}')
        refs = {}
        if tool:
            refs['evidence'] = clean_body(f'declared with {tool}', MAX_LABEL_CHARS)
        entry_id = self.store.insert(
            target_type=target_type, target_id=target_id, author=actor.user, kind='event',
            category='used-in-analysis', body=clean_body(body, self.store.max_body_chars, required=False),
            links=clean_links(links), refs=refs,
            dataset_version=dataset_version if target_type == 'dataset' else None)
        return self.get(actor, visible, entry_id)

    def edit(self, actor: Actor, visible: Visible, entry_id: int, body: Any = None, links: Any = None,
             refs: Any = None) -> Dict[str, Any]:
        """The author changes the text, links or refs of their own note; the old values go to the history."""
        self._require_signed_in(actor)
        row = self._require_entry(entry_id, actor, visible, author_sees_hidden=True)
        if row['author'] != actor.user or row['kind'] != 'note':
            raise JournalError(403, 'forbidden', 'only the author edits a note (admins hide notes instead)')
        if row['hidden']:
            raise JournalError(409, 'hidden', 'a hidden note cannot be edited')
        fields: Dict[str, Any] = {}
        if body is not None:
            fields['body'] = clean_body(body, self.store.max_body_chars, required=row['category'] != 'link')
        if links is not None:
            fields['links'] = clean_links(links)
            if row['category'] == 'link' and not fields['links']:
                raise JournalError(400, 'bad_links', 'a link note needs at least one link')
        if refs is not None:
            fields['refs'] = clean_refs(refs)
        if not fields:
            raise JournalError(400, 'nothing_to_change', 'give body, links or refs')
        fields['edited'] = self.store._clock()
        self.store.update(entry_id, actor.user, 'edit', **fields)
        return self.get(actor, visible, entry_id)

    def set_status(self, actor: Actor, visible: Visible, entry_id: int, status: str, note: Any = None,
                   changelog_ref: Any = None, fixed_in_version: Any = None) -> Dict[str, Any]:
        """Admins, triagers and the dataset's owner move an actionable note through open, acknowledged,
        fixed and declined. Fixing a data error or annotation concern needs the changelog line and the
        dataset version that holds the fix."""
        self._require_signed_in(actor)
        row = self._require_entry(entry_id, actor, visible)
        if row['category'] not in ACTIONABLE or row['parent_id'] is not None:
            raise JournalError(400, 'no_status', f"only {', '.join(ACTIONABLE)} notes have a status")
        if not self.may_set_status(actor):
            raise JournalError(403, 'forbidden', "status changes are for admins, curators and the dataset's owner")
        status = str(status or '').strip().lower()
        if status not in STATUSES:
            raise JournalError(400, 'bad_status', f"status is one of {', '.join(STATUSES)}")
        fields: Dict[str, Any] = {'status': status, 'status_by': actor.user, 'status_at': self.store._clock(),
                                  'status_note': clean_body(note, 2000, required=False) or None}
        if status == 'fixed' and row['category'] in NEEDS_CHANGELOG:
            reference = clean_body(changelog_ref, 2000, required=False)
            version = clean_body(fixed_in_version, 100, required=False)
            if not reference or not version:
                raise JournalError(400, 'changelog_required',
                                   'closing a data error or annotation concern as fixed needs changelog_ref (the '
                                   'changelog line) and fixed_in_version (the dataset version with the fix)')
            if row['dataset_version'] and version == row['dataset_version']:
                raise JournalError(400, 'same_version',
                                   f'the problem was reported on version {version}; a fix is a new version')
            fields.update(changelog_ref=reference, fixed_in_version=version)
        elif status != 'fixed':
            fields.update(changelog_ref=None, fixed_in_version=None)
        self.store.update(entry_id, actor.user, f'status:{status}', **fields)
        return self.get(actor, visible, entry_id)

    def hide(self, actor: Actor, visible: Visible, entry_id: int, hidden: bool = True, reason: Any = None) -> Dict[str, Any]:
        """An admin hides (or shows again) an entry. Nothing is ever deleted."""
        if not actor.is_admin:
            raise JournalError(403, 'forbidden', 'only admins hide journal entries')
        self._require_entry(entry_id, actor, visible)
        if hidden:
            fields = {'hidden': 1, 'hidden_by': actor.user, 'hidden_at': self.store._clock(),
                      'hidden_reason': clean_body(reason, 2000, required=False) or None}
        else:
            fields = {'hidden': 0, 'hidden_by': None, 'hidden_at': None, 'hidden_reason': None}
        self.store.update(entry_id, actor.user, 'hide' if hidden else 'unhide', **fields)
        return self.get(actor, visible, entry_id)

    # downloads

    def record_download(self, target_type: str, target_id: str, what: str, user: str = '',
                        dataset_version: Optional[str] = None, name: Optional[str] = None) -> None:
        if what not in DOWNLOAD_KINDS:
            raise ValueError(f'download kind is one of {DOWNLOAD_KINDS}')
        self.store.record_download(target_type, target_id, what, user, dataset_version, name)

    def downloads(self, actor: Actor, visible: Visible, target_type: str, target_id: str,
                  limit: int = 200) -> Dict[str, Any]:
        """Counts for everyone who can see the target; the rows with names for admins and the owner."""
        target_type, target_id = check_target(target_type, target_id)
        if not visible(target_type, target_id):
            raise JournalError(404, 'not_found', f'no {target_type} {target_id}')
        out: Dict[str, Any] = {'target_type': target_type, 'target_id': target_id,
                               **self.store.download_summary(target_type, target_id),
                               'retention_days': self.retention_days, 'names_visible': False}
        if actor.is_admin or actor.owns:
            out['names_visible'] = True
            out['rows'] = [{'user': row['user'] or None, 'ts': iso(row['ts']), 'what': row['what'],
                            'name': row['name'], 'dataset_version': row['dataset_version']}
                           for row in self.store.download_rows(target_type, target_id, limit)]
        return out

    def purge_old_downloads(self) -> int:
        removed = self.store.purge_downloads(self.retention_days)
        self.store.meta_set('downloads_purged_at', str(self.store._clock()))
        return removed

    def purge_due(self, every_seconds: float = 86400.0) -> bool:
        last = self.store.meta_get('downloads_purged_at')
        try:
            return last is None or self.store._clock() - float(last) >= every_seconds
        except ValueError:
            return True

    # catalog → events

    def sync_dataset_versions(self, current: Mapping[str, Optional[str]],
                              depositor: Callable[[str, str], Optional[str]] = lambda db_id, version: None) -> int:
        """Turn dataset versions the journal has not seen into events. The first call records the catalog as
        it is without events (no invented history); after that, a new dataset or a new version becomes a
        ``deposited`` event when a deposit produced it (author: the depositor), else ``version-published``.
        Returns the number of events written."""
        seen = self.store.seen('dataset_version')
        baseline = self.store.meta_get('datasets_baseline') is None
        written = 0
        for db_id, version in sorted(current.items()):
            version = None if version in (None, '') else str(version)
            if version is None or seen.get(db_id) == version:
                continue
            previous = seen.get(db_id)
            if not baseline:
                user = None
                try:
                    user = depositor(db_id, version)
                except Exception:  # noqa: BLE001 - a lookup failure only loses the author
                    user = None
                if previous is None:
                    text = f'Version {version} published (first version on this site).'
                else:
                    text = f'Version {version} published (was {previous}).'
                self.store.insert(target_type='dataset', target_id=db_id, author=user or SYSTEM_AUTHOR,
                                  kind='event', category='deposited' if user else 'version-published',
                                  body=text, dataset_version=version)
                written += 1
            self.store.mark_seen('dataset_version', db_id, version)
        if baseline:
            self.store.meta_set('datasets_baseline', str(self.store._clock()))
        return written

    def sync_artefact_versions(self, versions: Iterable[Mapping[str, Any]]) -> int:
        """One ``artefact-made`` event per (artefact version, dataset it was made from), dated when the
        version was published. ``versions`` items: ``ref``, ``version``, ``published`` (ISO), ``owner``,
        ``title`` and ``made_from`` (a list of ``{db_id, dataset_version}``). Returns events written."""
        seen = self.store.seen('artefact_version')
        written = 0
        for item in versions:
            key = f"{item['ref']}@v{int(item['version'])}"
            if key in seen:
                continue
            for source in item.get('made_from') or []:
                db_id = str((source or {}).get('db_id') or '').strip()
                if not db_id:
                    continue
                title = str(item.get('title') or '').strip()
                text = f"Artefact {key} made from version {source.get('dataset_version') or '?'}" + (
                    f': {title[:300]}' if title else '.')
                self.store.insert(target_type='dataset', target_id=db_id, author=str(item.get('owner') or SYSTEM_AUTHOR),
                                  kind='event', category='artefact-made', body=text,
                                  dataset_version=str(source.get('dataset_version') or '') or None,
                                  links=[], about_type='artefact', about_id=str(item['ref']),
                                  created=parse_iso(item.get('published')))
                written += 1
            self.store.mark_seen('artefact_version', key, iso(self.store._clock()))
        return written
