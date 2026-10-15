"""Dataset deposits through the API (presigned uploads into the S3 watch route's incoming prefix).

Design: ``doc/DESIGN_dataset_ingestion.md`` section 22. A depositor signs in
to Anthive (no S3 keys), declares the files of one dataset, and gets
presigned URLs for exact keys under ``<prefix><data_project>/`` (the layout
``mus s3 upload`` writes, so the S3 watcher in ``lib/ingest_watch.py`` picks
the deposit up unchanged). The server's S3 credentials never leave the
server: it creates multipart uploads itself, presigns ``UploadPart`` (and a
single ``PutObject`` for small files) with the exact ``Content-Length`` and
the ``x-amz-meta-sha256`` / ``x-amz-meta-deposit`` headers signed in, and
completes the uploads after checking every part.

Three parts:

* :class:`DepositStore`: SQLite at ``<ingest_state_dir>/deposits.sqlite``
  (``deposits``, ``files``, ``events``). Written by ant-serve (create,
  complete, withdraw, expiry) and by the watcher (its outcome per deposit,
  the redacted report, the notification sent).
* :class:`DepositService`: the rules behind the REST routes (no FastAPI in
  here; it raises :class:`DepositError` with an HTTP status and a body).
* Redaction: :func:`sanitize_text` and :func:`redacted_report` turn watcher
  messages and ``ant-ingest-check`` reports into what a depositor may see
  (curator view, no admin detail, no server paths).

Identity is the proxy's ``Remote-User`` only; a ``depositor=`` line in the
sidecar is never taken as identity on this route.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ingest_pipeline import (H5AD_SUFFIX, MUS_SUFFIX, delete_consumed, parse_sidecar_text, valid_segment)

DB_FILE = 'deposits.sqlite'
REPORTS_DIR = 'deposit_reports'
WATCH_DB_FILE = 'ingest_watch.sqlite'

FILE_KINDS = ('h5ad', 'mus', 'seed_md', 'index_md', 'static', 'de')
TEXT_KINDS = ('mus', 'seed_md', 'index_md')
TEXT_MAX_BYTES = 1024 * 1024
MiB = 1024 * 1024
MIN_PART_BYTES = 64 * MiB
MAX_PARTS = 10000
PART_COUNT_TARGET = 9500                 # leaves room below the S3 maximum of 10,000 parts
SINGLE_PUT_HARD_MAX = 5 * 1024 ** 3      # S3 PutObject limit
MAX_PARTS_PER_CALL = 1000

#: A new group (data_project) is named like a paper: capital letter first, a year last (mus' rule).
NEW_GROUP_RE = re.compile(r'^[A-Z][A-Za-z0-9_]*\d{4}$')
BASENAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DEPOSIT_ID_RE = re.compile(r'^dep_[0-9a-f]{20}$')
DB_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,32}/[A-Za-z0-9._-]{1,64}$')

#: States of the deposit record itself. After ``uploaded`` the watcher's state is shown.
RECORD_STATES = ('uploading', 'uploaded', 'withdrawn', 'expired')
#: Watcher states a depositor sees (``pulled`` / ``checked`` are shown as ``checking``).
WATCH_FINAL = ('live', 'held', 'failed', 'needs_decision', 'rejected')
WATCH_BUSY = ('pulled', 'checked')
NOTIFY_DEPOSITOR_ON = ('live', 'held', 'failed')

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposits (
    id TEXT PRIMARY KEY, user TEXT NOT NULL, name TEXT, email TEXT, is_admin INTEGER DEFAULT 0,
    data_project TEXT NOT NULL, basename TEXT NOT NULL, update_of TEXT, state TEXT NOT NULL,
    created REAL, updated REAL, expires REAL, completed REAL, total_bytes INTEGER DEFAULT 0,
    watch_key TEXT, h5ad_etag TEXT, mus_key TEXT, mus_etag TEXT, continues TEXT,
    watch_state TEXT, db_id TEXT, version TEXT, kind TEXT, held_reasons TEXT, error TEXT, warnings TEXT,
    report_file TEXT, notified_state TEXT, approval TEXT
);
CREATE INDEX IF NOT EXISTS deposits_user ON deposits(user);
CREATE INDEX IF NOT EXISTS deposits_watch_key ON deposits(watch_key);
CREATE TABLE IF NOT EXISTS files (
    deposit_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, size INTEGER,
    sha256 TEXT, method TEXT, upload_id TEXT, part_size INTEGER, part_count INTEGER,
    state TEXT NOT NULL, etag TEXT, completed REAL,
    PRIMARY KEY (deposit_id, name)
);
CREATE INDEX IF NOT EXISTS files_key ON files(key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, deposit_id TEXT, ts REAL, actor TEXT, action TEXT, detail TEXT
);
"""


class DepositError(Exception):
    """A refusal with an HTTP status and a JSON body ``{'status': <word>, 'message': ...}``."""

    def __init__(self, status: int, word: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.body: Dict[str, Any] = {'status': word, 'message': message, **extra}
        self.headers: Dict[str, str] = {}


@dataclass
class Identity:
    """Who calls: the proxy's Remote-User, with name and e-mail when the proxy sends them."""
    user: str
    name: str = ''
    email: str = ''
    is_admin: bool = False


@dataclass
class DepositSettings:
    """The ``datasets_deposit_*`` keys (``config.get_dataset_deposit_settings``)."""
    enabled: bool = False
    upload_groups: Sequence[str] = ('admins', 'curators')
    max_gb: float = 200.0
    url_ttl_hours: float = 24.0
    max_open_days: float = 7.0
    max_open_per_user: int = 5
    max_pending_gb_per_user: float = 400.0
    presign_per_minute: int = 30
    parts_per_batch: int = 100
    single_put_max_mb: int = 64
    max_files: int = 100
    update_policy: str = 'own'          # own | any
    prefix: str = '__incoming/'
    bucket: str = ''
    s3: Dict[str, Any] = field(default_factory=dict)

    @property
    def credentials_present(self) -> bool:
        return bool(self.bucket and self.s3.get('access_key_id') and self.s3.get('secret_access_key'))

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.credentials_present)


# ── redaction ────────────────────────────────────────────────────────────────

_ABS_PATH = re.compile(r'(?<![\w.:/~>-])/(?:[^\s\'"`,;()<>\[\]{}]+/)+([^\s\'"`,;()<>\[\]{}/]+)')


def sanitize_text(text: Any, roots: Optional[Dict[str, Path]] = None) -> Any:
    """Remove server paths from a message: known roots become ``<label>``, other absolute paths ``<path>/<name>``."""
    if not isinstance(text, str):
        return text
    result = text
    for label, root in sorted((roots or {}).items(), key=lambda pair: -len(str(pair[1]))):
        if root:
            result = result.replace(str(root).rstrip('/'), f'<{label}>')
    return _ABS_PATH.sub(lambda match: f'<path>/{match.group(1)}', result)


def _sanitize_tree(node: Any, roots: Optional[Dict[str, Path]]) -> Any:
    if isinstance(node, str):
        return sanitize_text(node, roots)
    if isinstance(node, list):
        return [_sanitize_tree(entry, roots) for entry in node]
    if isinstance(node, dict):
        return {key: _sanitize_tree(value, roots) for key, value in node.items()}
    return node


def redacted_report(report: Dict[str, Any], roots: Optional[Dict[str, Path]] = None) -> Dict[str, Any]:
    """The curator view of an ``ant-ingest-check`` report without server paths.

    ``admin_detail`` (other datasets' ids, cohort names, demographic match
    tables) is dropped, as in ``ant ingest-check --audience curator``; every
    string loses its server paths.
    """
    view = {key: value for key, value in report.items() if key not in ('admin_detail', 'log', 'paths')}
    view['audience'] = 'curator'
    return _sanitize_tree(view, roots)


def report_summary(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Verdict, counts and the failed or warned checks with their fix (for the status answer)."""
    if not report:
        return None
    problems = [{'id': entry.get('id'), 'status': entry.get('status'), 'summary': entry.get('summary'),
                 'fix': entry.get('fix')}
                for entry in report.get('checks', []) if isinstance(entry, dict)
                and entry.get('status') in ('fail', 'warn')]
    return {'result': report.get('result'), 'publish': report.get('publish'),
            'held_reasons': report.get('held_reasons') or [], 'counts': report.get('counts') or {},
            'problems': problems}


def report_copy_path(state_dir: Path, deposit_id: str) -> Path:
    """Where the watcher writes the redacted report of a deposit (and the API reads it)."""
    if not DEPOSIT_ID_RE.match(deposit_id):
        raise ValueError(f'not a deposit id: {deposit_id!r}')
    return Path(state_dir) / REPORTS_DIR / f'{deposit_id}.json'


def write_report_copy(state_dir: Path, deposit_id: str, report_path: Optional[str],
                      roots: Optional[Dict[str, Path]] = None) -> Optional[Path]:
    """Write the redacted copy of ``report_path`` for the API; None when there is no report."""
    if not report_path or not Path(report_path).is_file():
        return None
    try:
        report = json.loads(Path(report_path).read_text())
    except (OSError, ValueError):
        return None
    target = report_copy_path(state_dir, deposit_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix('.json.partial')
    partial.write_text(json.dumps(redacted_report(report, roots), indent=1, default=str))
    partial.replace(target)
    return target


# ── store ────────────────────────────────────────────────────────────────────

class DepositStore:
    """SQLite record of deposits (WAL; ant-serve and the watcher both write, briefly)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @classmethod
    def open_if_exists(cls, state_dir: Path) -> Optional['DepositStore']:
        path = Path(state_dir) / DB_FILE
        return cls(path) if path.exists() else None

    def close(self) -> None:
        self.conn.close()

    def _rows(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.conn.execute(sql, args)]

    def get(self, deposit_id: str) -> Optional[Dict[str, Any]]:
        rows = self._rows('SELECT * FROM deposits WHERE id = ?', (deposit_id,))
        return rows[0] if rows else None

    def files(self, deposit_id: str) -> List[Dict[str, Any]]:
        return self._rows('SELECT * FROM files WHERE deposit_id = ? ORDER BY rowid', (deposit_id,))

    def deposits(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        if user is None:
            return self._rows('SELECT * FROM deposits ORDER BY created DESC')
        return self._rows('SELECT * FROM deposits WHERE user = ? ORDER BY created DESC', (user,))

    def insert(self, record: Dict[str, Any], files: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            columns = list(record)
            self.conn.execute(f'INSERT INTO deposits ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))})',
                              [record[name] for name in columns])
            for entry in files:
                names = list(entry)
                self.conn.execute(f'INSERT INTO files ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
                                  [entry[name] for name in names])
            self.conn.commit()

    def update(self, deposit_id: str, **fields: Any) -> None:
        fields['updated'] = time.time()
        with self._lock:
            assignments = ', '.join(f'{name} = ?' for name in fields)
            self.conn.execute(f'UPDATE deposits SET {assignments} WHERE id = ?', list(fields.values()) + [deposit_id])
            self.conn.commit()

    def update_file(self, deposit_id: str, name: str, **fields: Any) -> None:
        with self._lock:
            assignments = ', '.join(f'{column} = ?' for column in fields)
            self.conn.execute(f'UPDATE files SET {assignments} WHERE deposit_id = ? AND name = ?',
                              list(fields.values()) + [deposit_id, name])
            self.conn.commit()

    def event(self, deposit_id: str, actor: str, action: str, detail: str = '') -> None:
        with self._lock:
            self.conn.execute('INSERT INTO events (deposit_id, ts, actor, action, detail) VALUES (?, ?, ?, ?, ?)',
                              (deposit_id, time.time(), actor, action, detail[:4000]))
            self.conn.commit()

    def events(self, deposit_id: str) -> List[Dict[str, Any]]:
        return self._rows('SELECT * FROM events WHERE deposit_id = ? ORDER BY id', (deposit_id,))

    # queries the rules need ------------------------------------------------

    def open_deposits(self, now: float, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Deposits still uploading and not past their expiry."""
        sql = "SELECT * FROM deposits WHERE state = 'uploading' AND expires > ?"
        args: List[Any] = [now]
        if user is not None:
            sql += ' AND user = ?'
            args.append(user)
        return self._rows(sql, args)

    def open_projects(self, now: float) -> Set[str]:
        """data_projects with an open deposit: the watcher leaves their items alone until it completes."""
        return {row['data_project'] for row in self.open_deposits(now)}

    def open_keys(self, now: float) -> Dict[str, str]:
        """``{s3 key: deposit id}`` of every file of an open deposit."""
        return {row['key']: row['deposit_id'] for row in self._rows(
            "SELECT f.key, f.deposit_id FROM files f JOIN deposits d ON d.id = f.deposit_id "
            "WHERE d.state = 'uploading' AND d.expires > ?", (now,))}

    def find_for_item(self, h5ad_key: str, h5ad_etag: str, mus_key: Optional[str],
                      mus_etag: Optional[str]) -> Optional[Dict[str, Any]]:
        """The completed deposit whose h5ad and sidecar are exactly the listed objects (newest first)."""
        rows = self._rows("SELECT * FROM deposits WHERE state = 'uploaded' AND watch_key = ? AND h5ad_etag = ? "
                          "AND mus_key = ? AND mus_etag = ? ORDER BY completed DESC, rowid DESC LIMIT 1",
                          (h5ad_key, h5ad_etag, mus_key or '', mus_etag or ''))
        return rows[0] if rows else None

    def owner_of_dataset(self, db_id: str) -> Optional[str]:
        """User of the newest deposit that published ``db_id`` (None: never published through a deposit)."""
        rows = self._rows("SELECT user FROM deposits WHERE UPPER(db_id) = UPPER(?) AND watch_state = 'live' "
                          "ORDER BY completed DESC LIMIT 1", (db_id,))
        return rows[0]['user'] if rows else None

    def record_watch(self, deposit_id: str, row: Dict[str, Any], roots: Optional[Dict[str, Path]] = None,
                     report_file: Optional[str] = None) -> None:
        """Copy the watcher's outcome of the deposit's item into the record (redacted)."""
        warnings = json.loads(row.get('warnings') or '[]') if isinstance(row.get('warnings'), str) else (
            row.get('warnings') or [])
        held = json.loads(row.get('held_reasons') or '[]') if isinstance(row.get('held_reasons'), str) else (
            row.get('held_reasons') or [])
        fields: Dict[str, Any] = {
            'watch_state': row.get('state'), 'db_id': row.get('db_id'), 'version': row.get('version'),
            'kind': row.get('kind'), 'held_reasons': json.dumps(held),
            'error': sanitize_text(row.get('error'), roots),
            'warnings': json.dumps([sanitize_text(text, roots) for text in warnings])}
        if report_file:
            fields['report_file'] = report_file
        self.update(deposit_id, **fields)


# ── helpers ──────────────────────────────────────────────────────────────────

def new_deposit_id() -> str:
    return 'dep_' + secrets.token_hex(10)


def choose_part_size(size: int) -> int:
    """Part size for a multipart upload: at least 64 MiB, whole MiB, at most ~9,500 parts."""
    part = max(MIN_PART_BYTES, math.ceil(size / PART_COUNT_TARGET))
    return int(math.ceil(part / MiB) * MiB)


def part_sizes(size: int, part_size: int) -> List[int]:
    count = max(1, math.ceil(size / part_size))
    return [part_size] * (count - 1) + [size - part_size * (count - 1)]


def _iso(stamp: Optional[float]) -> Optional[str]:
    if not stamp:
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp))


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, 'response', None) or {}
    return str((response.get('Error') or {}).get('Code') or type(exc).__name__)


def _head(s3: Any, bucket: str, key: str) -> Optional[Dict[str, Any]]:
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 - absent or unreadable: treated as absent
        return None
    return {'etag': str(response.get('ETag', '')).strip('"'), 'size': int(response.get('ContentLength') or 0),
            'metadata': {str(k).lower(): str(v) for k, v in (response.get('Metadata') or {}).items()}}


def dataset_db_id(store_root: Path, data_project: str, basename: str) -> Optional[str]:
    """``group_id/id`` of ``<store>/<data_project>/<basename>.md`` (None when the dataset is not in the store)."""
    from metadata import parse_md_file  # noqa: PLC0415

    md = Path(store_root) / data_project / f'{basename}.md'
    if not md.exists():
        return None
    meta = parse_md_file(md)[0]
    index = Path(store_root) / data_project / 'index.md'
    group_id = meta.get('group_id') or (parse_md_file(index)[0].get('group_id') if index.exists() else None)
    return f'{group_id}/{meta["id"]}' if group_id and meta.get('id') else f'{data_project}/{basename}'


def public_state(record: Dict[str, Any], watch_row: Optional[Dict[str, Any]]) -> str:
    """The state a depositor sees: the record's own state until the upload is complete, then the watcher's."""
    if record['state'] != 'uploaded':
        return record['state']
    state = watch_row['state'] if watch_row else record.get('watch_state')
    if not state:
        return 'uploaded'
    return 'checking' if state in WATCH_BUSY else state


def _signature_deposit(signature: Optional[str]) -> Optional[str]:
    """The deposit id inside an item's content signature (``Item.content_signature``), if any."""
    try:
        parts = json.loads(signature or '[]')
    except ValueError:
        return None
    for part in parts:
        if isinstance(part, list) and len(part) == 3 and part[0] == 'deposit':
            return str(part[1])
    return None


def watch_row_matches(record: Dict[str, Any], row: Optional[Dict[str, Any]]) -> bool:
    """The watcher's row is about this deposit: the exact objects (ETags of h5ad and sidecar), and,
    when the watcher linked the row to a deposit, this one (identical bytes may be deposited twice)."""
    if not row or row.get('h5ad_etag') != record.get('h5ad_etag') or \
            (row.get('mus_etag') or '') != (record.get('mus_etag') or ''):
        return False
    linked = _signature_deposit(row.get('content_signature'))
    return linked is None or linked == record.get('id')


def read_watch_row(state_dir: Path, key: Optional[str]) -> Optional[Dict[str, Any]]:
    """The watcher's row for an h5ad key, read without creating the watcher's database."""
    path = Path(state_dir) / WATCH_DB_FILE
    if not key or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute('SELECT * FROM items WHERE key = ?', (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


# ── the rules ────────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding one-minute window per user."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time):
        self.per_minute = max(1, int(per_minute))
        self.clock = clock
        self.calls: Dict[str, List[float]] = {}
        self.lock = threading.Lock()

    def check(self, user: str) -> None:
        now = self.clock()
        with self.lock:
            recent = [stamp for stamp in self.calls.get(user, []) if now - stamp < 60]
            if len(recent) >= self.per_minute:
                error = DepositError(429, 'rate_limited', f'at most {self.per_minute} presign calls per minute; '
                                                          f'wait a moment')
                error.headers['Retry-After'] = str(max(1, int(60 - (now - recent[0])) + 1))
                self.calls[user] = recent
                raise error
            recent.append(now)
            self.calls[user] = recent


class DepositService:
    """The deposit rules. One instance per server process."""

    def __init__(self, settings: DepositSettings, store: DepositStore, s3: Any, store_root: Path,
                 state_dir: Path, clock: Callable[[], float] = time.time,
                 roots: Optional[Dict[str, Path]] = None):
        self.settings = settings
        self.store = store
        self.s3 = s3
        self.store_root = Path(store_root)
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.limiter = RateLimiter(settings.presign_per_minute, clock)
        self.roots = roots or {'store': self.store_root, 'state': self.state_dir}

    # small helpers ---------------------------------------------------------

    @property
    def bucket(self) -> str:
        return self.settings.bucket

    def _ttl(self) -> int:
        return int(self.settings.url_ttl_hours * 3600)

    def _require_active(self) -> None:
        if not self.settings.active:
            raise DepositError(503, 'disabled', 'Dataset deposits are switched off on this site '
                                                '(datasets_deposit_enabled, or no S3 credentials for presigning).')

    def _key(self, data_project: str, kind: str, name: str) -> str:
        base = f'{self.settings.prefix}{data_project}/'
        if kind in ('h5ad', 'mus'):
            return base + name
        if kind == 'de':
            return base + 'de/' + name
        return base + 'static/' + name

    def _get_own(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        """The deposit when the caller owns it (admins: any); 404 otherwise, also for other users' ids."""
        record = self.store.get(deposit_id) if DEPOSIT_ID_RE.match(deposit_id or '') else None
        if record is None or (record['user'] != identity.user and not identity.is_admin):
            raise DepositError(404, 'not_found', f'no deposit {deposit_id!r} of yours')
        return record

    def _metadata(self, deposit_id: str, sha256: str) -> Dict[str, str]:
        return {'sha256': sha256, 'deposit': deposit_id}

    # vocabulary ------------------------------------------------------------

    def vocab(self) -> Dict[str, Any]:
        """What a deposit must contain, from the server's own vocabulary and rules."""
        import mus  # noqa: PLC0415
        from disease_rule import DISEASE_BASES  # noqa: PLC0415
        from replicates import REPLICATE_UNITS  # noqa: PLC0415

        vocabulary = mus.load_vocabulary()
        settings = self.settings
        return {
            'schema': 'anthive.deposit_vocab/1',
            'uploads_enabled': settings.active,
            'limits': {'max_gb': settings.max_gb, 'max_files': settings.max_files,
                       'text_file_max_kb': TEXT_MAX_BYTES // 1024,
                       'single_put_max_mb': settings.single_put_max_mb, 'min_part_mb': MIN_PART_BYTES // MiB,
                       'url_ttl_hours': settings.url_ttl_hours, 'max_open_days': settings.max_open_days,
                       'max_open_per_user': settings.max_open_per_user,
                       'max_pending_gb_per_user': settings.max_pending_gb_per_user,
                       'presign_calls_per_minute': settings.presign_per_minute,
                       'parts_per_batch': settings.parts_per_batch},
            'naming': {
                'segment': '^[A-Za-z0-9][A-Za-z0-9._-]*$ (ASCII letters, digits, dot, dash, underscore; '
                           'not starting with a dot or underscore)',
                'basename': '^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$ (the h5ad name without .h5ad, at most 50 characters)',
                'data_project_new': NEW_GROUP_RE.pattern + ' (a new group: capital letter first, a year last, '
                                                           'e.g. Smith2024)',
                'data_project_existing': 'an existing group folder of the store, spelled exactly as it is',
            },
            'file_kinds': {
                'h5ad': {'name': '<basename>.h5ad', 'key': '<prefix><data_project>/<basename>.h5ad',
                         'required': 'yes, except when a corrected sidecar is deposited for an h5ad of yours '
                                     'that is still waiting in incoming'},
                'mus': {'name': '<basename>.h5ad.mus', 'key': '<prefix><data_project>/<basename>.h5ad.mus',
                        'required': 'yes; the collapsed sidecar with sha256=<sha256 of the h5ad>'},
                'seed_md': {'name': '<basename>.md', 'key': '<prefix><data_project>/static/<basename>.md',
                            'required': 'strongly recommended (without it the check meta.required_keys fails)'},
                'index_md': {'name': 'index.md or <basename>.index.md', 'key': '<prefix><data_project>/static/<name>',
                             'required': 'for a new group (or the sidecar names doi, year, paper_title, authors)'},
                'static': {'name': '<file> (not index.md, not *.index.md, not <basename>.md)',
                           'key': '<prefix><data_project>/static/<file>', 'required': 'no'},
                'de': {'name': '<folder>/<file> (a DE table and its <file>.mus naming the dataset)',
                       'key': '<prefix><data_project>/de/<folder>/<file>', 'required': 'no'},
            },
            'prefix': settings.prefix,
            'mus': {'source': 'lib/_mus_vocab.yaml (the server snapshot of the mus vocabulary)',
                    'schema_version': vocabulary.get('schema_version'),
                    'required': list(vocabulary.get('required') or []),
                    'soft': list(vocabulary.get('soft') or []),
                    'vocabulary': {name: list(values) for name, values in (vocabulary.get('vocabulary') or {}).items()},
                    'matching': 'case-insensitive; _ space and - are equivalent'},
            'sidecar_keys': {
                'sha256': 'required: sha256 of the h5ad (hex)',
                'data_project': 'optional; must equal the deposit data_project',
                'access': 'internal (default, login only) or public (always held for an admin)',
                'data_use': 'open or controlled; missing or controlled: held for an admin',
                'license': 'an SPDX id (CC-BY-4.0, CC0-1.0, ...) or in-house; missing: held',
                'depositor': 'ignored for identity on this route: the Anthive login of the depositor is recorded',
                'depositor_email': 'ignored for identity on this route (the login e-mail is used)',
                'update_of': '<db_id> of the dataset this deposit replaces; required for an update (give it in the '
                             'deposit request too; the request value wins)',
                'previous_sha256': 'optional: sha256 of the published h5ad this deposit replaces',
                'override_<key>': 'true lets an update replace the curated value of <key> (access, data_use and '
                                  'license are never replaced automatically)',
                'doi, pmid, year, journal, paper_title, authors': 'used for index.md of a new group when no '
                                                                  'index.md is deposited',
                'title, description': 'used when the seed .md does not set them',
            },
            'seed_md': {
                'format': 'YAML front matter between --- lines, then a markdown body',
                'required': ['title', f'description (at least 200 characters, the key or the body)',
                             'replicate_unit', 'api (the facet mapping)'],
                'replicate_unit': list(REPLICATE_UNITS),
                'disease_basis': list(DISEASE_BASES),
                'disease_basis_rule': 'required when ant_disease is published',
                'filled_by_route': ['id (when missing)', 'sha256', 'n_obs', 'n_vars', 'version (1.0.0)',
                                    'changelog', 'date', 'default_layer', 'layers_include', 'obsm_include',
                                    'obsnum_include', 'obscat_include', 'layer_stats', 'ingest'],
                'leave_out': ['version', 'changelog', 'sha256', 'n_obs', 'n_vars', 'layer_stats', 'ingest'],
                'template': SEED_MD_TEMPLATE,
            },
            'index_md': {'required': ['group (equals data_project)', 'group_id (3 characters, unique)',
                                      'doi or paper_title', 'year', 'authors', 'default_dataset'],
                         'template': INDEX_MD_TEMPLATE},
            'held_reasons': {
                'public_access': 'access=public: an admin publishes it',
                'licence_unknown': 'no license= in the sidecar',
                'data_use_unknown': 'no data_use= in the sidecar',
                'controlled_access': 'data_use=controlled, or a donor cohort flagged controlled',
                'donor_overlap': 'many donors match an existing cohort: an admin decides',
            },
            'update_policy': settings.update_policy,
            'states': {'uploading': 'URLs handed out, not completed', 'uploaded': 'complete, waiting for the watcher',
                       'seen': 'listed by the watcher', 'stable': 'queued', 'checking': 'being checked and built',
                       'live': 'published', 'held': 'clean, waits for an admin',
                       'failed': 'errors in the checks (see the report)',
                       'needs_decision': 'the store conflicts; an admin decides',
                       'rejected': 'an admin rejected it', 'withdrawn': 'withdrawn by the depositor or an admin',
                       'expired': 'not completed in time; uploads aborted', 'superseded': 'a newer upload replaced it'},
        }

    # create ----------------------------------------------------------------

    def _validate_files(self, data_project: str, basename: str, files: Any) -> List[Dict[str, Any]]:
        if not isinstance(files, list) or not files:
            raise DepositError(422, 'invalid', 'files must be a non-empty list')
        if len(files) > self.settings.max_files:
            raise DepositError(422, 'invalid', f'at most {self.settings.max_files} files per deposit')
        seen_names: Set[str] = set()
        counts: Dict[str, int] = {}
        clean: List[Dict[str, Any]] = []
        for entry in files:
            if not isinstance(entry, dict):
                raise DepositError(422, 'invalid', 'each file is an object {name, size, sha256, kind}')
            name, kind = entry.get('name'), entry.get('kind')
            size, sha = entry.get('size'), str(entry.get('sha256') or '').lower()
            if kind not in FILE_KINDS:
                raise DepositError(422, 'invalid', f'kind must be one of {", ".join(FILE_KINDS)} (file {name!r})')
            if not isinstance(name, str) or not name:
                raise DepositError(422, 'invalid_name', 'every file needs a name')
            parts = name.split('/')
            if kind == 'de':
                if len(parts) != 2 or not all(valid_segment(part) for part in parts):
                    raise DepositError(422, 'invalid_name', f'{name!r}: a DE file is <folder>/<file>, each part '
                                                            f'letters, digits, ._- and not starting with . or _')
            elif len(parts) != 1 or not valid_segment(name):
                raise DepositError(422, 'invalid_name', f'{name!r}: letters, digits, ._- only, no folders, not '
                                                        f'starting with . or _')
            if len(name) > 200:
                raise DepositError(422, 'invalid_name', f'{name!r}: longer than 200 characters')
            if name in seen_names:
                raise DepositError(422, 'invalid', f'{name!r} is listed twice')
            seen_names.add(name)
            if kind == 'h5ad' and name != f'{basename}{H5AD_SUFFIX}':
                raise DepositError(422, 'invalid_name', f'the h5ad must be named {basename}{H5AD_SUFFIX}')
            if kind == 'mus' and name != f'{basename}{MUS_SUFFIX}':
                raise DepositError(422, 'invalid_name', f'the sidecar must be named {basename}{MUS_SUFFIX}')
            if kind == 'seed_md' and name != f'{basename}.md':
                raise DepositError(422, 'invalid_name', f'the seed .md must be named {basename}.md')
            if kind == 'index_md' and name not in ('index.md', f'{basename}.index.md'):
                raise DepositError(422, 'invalid_name', f'the index must be named index.md or {basename}.index.md')
            if kind == 'static' and (name in ('index.md', f'{basename}.md') or name.endswith('.index.md')):
                raise DepositError(422, 'invalid_name', f'{name!r} is a seed name: use kind seed_md or index_md')
            if kind != 'de' and (name.endswith(H5AD_SUFFIX) or name.endswith(MUS_SUFFIX)) and kind not in ('h5ad', 'mus'):
                raise DepositError(422, 'invalid_name', f'{name!r}: an h5ad or sidecar must have kind h5ad or mus')
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise DepositError(422, 'invalid', f'{name!r}: size must be a non-negative integer (bytes)')
            if kind == 'h5ad' and size == 0:
                raise DepositError(422, 'invalid', f'{name!r} is empty')
            if kind in TEXT_KINDS and size > TEXT_MAX_BYTES:
                raise DepositError(413, 'too_large', f'{name!r}: text files ({", ".join(TEXT_KINDS)}) are at most '
                                                     f'{TEXT_MAX_BYTES // 1024} KB')
            if not SHA256_RE.match(sha):
                raise DepositError(422, 'invalid', f'{name!r}: sha256 must be 64 hex characters')
            counts[kind] = counts.get(kind, 0) + 1
            clean.append({'name': name, 'kind': kind, 'size': size, 'sha256': sha,
                          'key': self._key(data_project, kind, name)})
        for kind in ('h5ad', 'mus', 'seed_md', 'index_md'):
            if counts.get(kind, 0) > 1:
                raise DepositError(422, 'invalid', f'at most one file of kind {kind}')
        if not counts.get('mus'):
            raise DepositError(422, 'invalid', f'the sidecar {basename}{MUS_SUFFIX} (kind mus) is required')
        return clean

    def _check_group(self, data_project: str) -> bool:
        """True when the group exists in the store; raises for a name that cannot be a group."""
        if not isinstance(data_project, str) or not valid_segment(data_project) or len(data_project) > 64:
            raise DepositError(422, 'invalid_data_project', 'data_project: letters, digits, ._- only, at most 64 '
                                                            'characters, not starting with . or _')
        if (self.store_root / data_project).is_dir():
            return True
        if self.store_root.is_dir():
            clash = [entry.name for entry in self.store_root.iterdir()
                     if entry.is_dir() and entry.name.lower() == data_project.lower()]
            if clash:
                raise DepositError(422, 'invalid_data_project', f'the store has the group {clash[0]!r}: spell it '
                                                                f'exactly like that')
        if not NEW_GROUP_RE.match(data_project):
            raise DepositError(422, 'invalid_data_project', f'{data_project!r} is not an existing group, and a new '
                                                            f'group is named like Smith2024 ({NEW_GROUP_RE.pattern})')
        return False

    def expire_stale(self) -> List[str]:
        """Abort and clean up deposits whose upload window passed; returns their ids."""
        now = self.clock()
        expired: List[str] = []
        for record in self.store._rows("SELECT * FROM deposits WHERE state = 'uploading' AND expires <= ?", (now,)):
            try:
                self._abort_and_delete(record, actor='expiry')
            except Exception as exc:  # noqa: BLE001 - S3 trouble: try again on the next call
                self.store.event(record['id'], 'expiry', 'cleanup_failed', _error_code(exc))
                continue
            self.store.update(record['id'], state='expired')
            self.store.event(record['id'], 'expiry', 'expired', 'upload window passed; uploads aborted')
            expired.append(record['id'])
        return expired

    def _pending_bytes(self, user: str) -> int:
        total = 0
        for record in self.store.deposits(user):
            if record['state'] == 'uploading' or (record['state'] == 'uploaded'
                                                  and record.get('watch_state') not in ('live', 'rejected')):
                total += int(record['total_bytes'] or 0)
        return total

    def _check_keys(self, identity: Identity, deposit_files: Sequence[Dict[str, Any]], now: float) -> None:
        open_keys = self.store.open_keys(now)
        for entry in deposit_files:
            key = entry['key']
            if key in open_keys:
                holder = self.store.get(open_keys[key]) or {}
                which = open_keys[key] if holder.get('user') == identity.user else 'of another user'
                raise DepositError(409, 'key_in_open_deposit', f'{entry["name"]} is part of the open deposit '
                                                               f'{which}: complete or withdraw it first')
            head = _head(self.s3, self.bucket, key)
            if head is None or identity.is_admin:
                continue
            owner_deposit = self.store.get(head['metadata'].get('deposit', '')) if head['metadata'].get('deposit') else None
            if owner_deposit is None:
                raise DepositError(409, 'key_taken', f'{entry["name"]} already waits in incoming (not from a deposit '
                                                     f'of yours); ask an admin')
            if owner_deposit['user'] != identity.user:
                raise DepositError(409, 'key_taken', f'{entry["name"]} already waits in incoming from another '
                                                     f"user's deposit")

    def create(self, identity: Identity, body: Dict[str, Any]) -> Dict[str, Any]:
        """Record a deposit and hand out the upload instructions (``POST /datasets/deposit``)."""
        self._require_active()
        if not isinstance(body, dict):
            raise DepositError(422, 'invalid', 'the body is a JSON object')
        self.expire_stale()
        now = self.clock()
        data_project = body.get('data_project')
        basename = body.get('basename')
        group_exists = self._check_group(data_project)
        if not isinstance(basename, str) or not BASENAME_RE.match(basename) or basename.endswith(H5AD_SUFFIX):
            raise DepositError(422, 'invalid_name', 'basename: the h5ad name without .h5ad; letters, digits, ._-, '
                                                    'at most 50 characters')
        deposit_files = self._validate_files(data_project, basename, body.get('files'))
        update_of = body.get('update_of')
        if update_of is not None and (not isinstance(update_of, str) or not DB_ID_RE.match(update_of)):
            raise DepositError(422, 'invalid', 'update_of is a dataset id like ABC/xyz')
        total = sum(entry['size'] for entry in deposit_files)
        if total > self.settings.max_gb * 1024 ** 3:
            raise DepositError(413, 'too_large', f'{total / 1024 ** 3:.1f} GB is over the {self.settings.max_gb:g} GB '
                                                 f'limit of one deposit')

        # an update of a dataset that is in the store
        existing_db_id = dataset_db_id(self.store_root, data_project, basename) if group_exists else None
        if (self.store_root / data_project / f'{basename}{H5AD_SUFFIX}').exists() and existing_db_id is None:
            existing_db_id = f'{data_project}/{basename}'
        if existing_db_id:
            if not update_of:
                raise DepositError(409, 'update_of_required', f'{data_project}/{basename} is the published dataset '
                                                              f'{existing_db_id}; a new version needs '
                                                              f'"update_of": "{existing_db_id}"', db_id=existing_db_id)
            if update_of.upper() != existing_db_id.upper():
                raise DepositError(409, 'update_of_mismatch', f'{data_project}/{basename} is {existing_db_id}, not '
                                                              f'{update_of}', db_id=existing_db_id)
            if self.settings.update_policy != 'any' and not identity.is_admin \
                    and self.store.owner_of_dataset(existing_db_id) != identity.user:
                raise DepositError(403, 'not_your_dataset', f'{existing_db_id} was not published by a deposit of '
                                                            f'yours; ask an admin to deposit the new version')
        elif update_of:
            raise DepositError(422, 'update_of_unknown', f'update_of={update_of}, but {data_project}/{basename} is '
                                                         f'not in the store')

        # the h5ad: deposited now, or a corrected sidecar for an h5ad of yours still in incoming
        h5ad_key = self._key(data_project, 'h5ad', f'{basename}{H5AD_SUFFIX}')
        continues = None
        h5ad_etag = None
        if not any(entry['kind'] == 'h5ad' for entry in deposit_files):
            head = _head(self.s3, self.bucket, h5ad_key)
            previous = self.store.get(head['metadata'].get('deposit', '')) if head and head['metadata'].get('deposit') else None
            if head is None or previous is None or (previous['user'] != identity.user and not identity.is_admin):
                raise DepositError(422, 'h5ad_required', f'no h5ad of yours waits at {data_project}/{basename}'
                                                         f'{H5AD_SUFFIX}: deposit the h5ad with its sidecar')
            continues, h5ad_etag = previous['id'], head['etag']

        # limits
        if len(self.store.open_deposits(now, identity.user)) >= self.settings.max_open_per_user:
            raise DepositError(429, 'too_many_open_deposits', f'you have {self.settings.max_open_per_user} open '
                                                              f'deposits; complete or withdraw one first')
        pending = self._pending_bytes(identity.user)
        if pending + total > self.settings.max_pending_gb_per_user * 1024 ** 3:
            raise DepositError(413, 'quota_exceeded', f'{(pending + total) / 1024 ** 3:.1f} GB would be pending for '
                                                      f'you (limit {self.settings.max_pending_gb_per_user:g} GB); wait '
                                                      f'until earlier deposits are published or withdraw them')
        self._check_keys(identity, deposit_files, now)
        self.limiter.check(identity.user)

        deposit_id = new_deposit_id()
        expires = now + self._ttl()
        rows: List[Dict[str, Any]] = []
        instructions: List[Dict[str, Any]] = []
        created_uploads: List[Tuple[str, str]] = []
        try:
            for entry in deposit_files:
                row = {'deposit_id': deposit_id, 'name': entry['name'], 'kind': entry['kind'], 'key': entry['key'],
                       'size': entry['size'], 'sha256': entry['sha256'], 'state': 'pending', 'etag': None,
                       'completed': None, 'upload_id': None, 'part_size': None, 'part_count': None}
                if entry['size'] <= min(self.settings.single_put_max_mb * MiB, SINGLE_PUT_HARD_MAX):
                    row['method'] = 'put'
                else:
                    row['method'] = 'multipart'
                    response = self.s3.create_multipart_upload(
                        Bucket=self.bucket, Key=entry['key'], Metadata=self._metadata(deposit_id, entry['sha256']))
                    row['upload_id'] = response['UploadId']
                    created_uploads.append((entry['key'], row['upload_id']))
                    row['part_size'] = choose_part_size(entry['size'])
                    row['part_count'] = len(part_sizes(entry['size'], row['part_size']))
                rows.append(row)
                instructions.append(self._instructions(deposit_id, row, 1, self.settings.parts_per_batch))
        except DepositError:
            raise
        except Exception as exc:  # noqa: BLE001 - S3 refused: abort what was started
            for key, upload_id in created_uploads:
                try:
                    self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
                except Exception:  # noqa: BLE001
                    pass
            raise DepositError(502, 's3_error', f'the object store refused to start the upload ({_error_code(exc)}); '
                                                f'try again later')
        mus_key = self._key(data_project, 'mus', f'{basename}{MUS_SUFFIX}')
        record = {'id': deposit_id, 'user': identity.user, 'name': identity.name, 'email': identity.email,
                  'is_admin': int(identity.is_admin), 'data_project': data_project, 'basename': basename,
                  'update_of': update_of, 'state': 'uploading', 'created': now, 'updated': now, 'expires': expires,
                  'total_bytes': total, 'watch_key': h5ad_key, 'h5ad_etag': h5ad_etag, 'mus_key': mus_key,
                  'continues': continues}
        self.store.insert(record, rows)
        self.store.event(deposit_id, identity.user, 'created',
                         f'{len(rows)} file(s), {total} bytes' + (f', update of {update_of}' if update_of else '')
                         + (f', continues {continues}' if continues else ''))
        warnings = []
        if not any(entry['kind'] == 'seed_md' for entry in deposit_files) and not existing_db_id:
            warnings.append(f'no seed {basename}.md: the route writes a minimal .md, which usually fails '
                            f'meta.required_keys (no description)')
        if not group_exists and not any(entry['kind'] == 'index_md' for entry in deposit_files):
            warnings.append('a new group without index.md: the route builds one from the sidecar keys doi, year, '
                            'paper_title and authors, or the check meta.index_md fails')
        return {'deposit_id': deposit_id, 'state': 'uploading', 'data_project': data_project, 'basename': basename,
                'update_of': update_of, 'continues': continues, 'expires': _iso(expires),
                'url_ttl_seconds': self._ttl(), 'total_bytes': total, 'files': instructions, 'warnings': warnings,
                'next': {'urls': f'/datasets/deposit/{deposit_id}/urls',
                         'complete': f'/datasets/deposit/{deposit_id}/complete',
                         'status': f'/datasets/deposit/{deposit_id}'}}

    def _instructions(self, deposit_id: str, row: Dict[str, Any], first: int, last: int) -> Dict[str, Any]:
        """Presigned URL(s) for one file: a single PUT, or UploadPart URLs for parts ``first..last``."""
        ttl = self._ttl()
        base = {'name': row['name'], 'kind': row['kind'], 'key': row['key'], 'size': row['size'],
                'sha256': row['sha256'], 'method': row['method']}
        if row['method'] == 'put':
            url = self.s3.generate_presigned_url(
                'put_object', ExpiresIn=ttl,
                Params={'Bucket': self.bucket, 'Key': row['key'], 'ContentLength': row['size'],
                        'Metadata': self._metadata(deposit_id, row['sha256'])})
            return {**base, 'url': url, 'headers': {'x-amz-meta-sha256': row['sha256'],
                                                    'x-amz-meta-deposit': deposit_id,
                                                    'Content-Length': str(row['size'])}}
        sizes = part_sizes(row['size'], row['part_size'])
        last = min(last, len(sizes))
        parts = []
        for number in range(first, last + 1):
            url = self.s3.generate_presigned_url(
                'upload_part', ExpiresIn=ttl,
                Params={'Bucket': self.bucket, 'Key': row['key'], 'UploadId': row['upload_id'],
                        'PartNumber': number, 'ContentLength': sizes[number - 1]})
            parts.append({'part_number': number, 'size': sizes[number - 1], 'url': url})
        return {**base, 'upload_id': row['upload_id'], 'part_size': row['part_size'], 'part_count': len(sizes),
                'parts': parts, 'parts_more': last < len(sizes)}

    def _uploaded_parts(self, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        marker = 0
        while True:
            response = self.s3.list_parts(Bucket=self.bucket, Key=row['key'], UploadId=row['upload_id'],
                                          PartNumberMarker=marker, MaxParts=1000)
            for part in response.get('Parts', []) or []:
                parts.append({'part_number': int(part['PartNumber']), 'size': int(part['Size']),
                              'etag': str(part['ETag']).strip('"')})
            if not response.get('IsTruncated'):
                break
            marker = int(response.get('NextPartNumberMarker') or 0)
            if not marker:
                break
        return sorted(parts, key=lambda part: part['part_number'])

    def _require_uploading(self, record: Dict[str, Any]) -> None:
        if record['state'] == 'uploading' and record['expires'] <= self.clock():
            self.expire_stale()
            record = self.store.get(record['id']) or record
        if record['state'] == 'expired':
            raise DepositError(410, 'expired', 'the upload window of this deposit passed; start a new deposit')
        if record['state'] != 'uploading':
            raise DepositError(409, 'not_uploading', f'the deposit is {record["state"]}')

    def refresh_urls(self, identity: Identity, deposit_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Fresh URLs for one file, and the parts S3 already has (resume) (``POST .../urls``)."""
        self._require_active()
        record = self._get_own(identity, deposit_id)
        self._require_uploading(record)
        name = (body or {}).get('name')
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        if name not in rows:
            raise DepositError(404, 'not_found', f'{name!r} is not a file of this deposit')
        row = rows[name]
        if row['state'] == 'complete':
            raise DepositError(409, 'already_complete', f'{name} is already complete')
        self.limiter.check(identity.user)
        now = self.clock()
        expires = min(now + self._ttl(), float(record['created']) + self.settings.max_open_days * 86400)
        if expires <= now:
            raise DepositError(410, 'expired', 'this deposit has been open too long; start a new deposit')
        self.store.update(deposit_id, expires=max(float(record['expires']), expires))
        if row['method'] == 'put':
            return {**self._instructions(deposit_id, row, 1, 1), 'expires': _iso(max(float(record['expires']), expires))}
        first = int(body.get('first_part') or 1)
        last = int(body.get('last_part') or first + self.settings.parts_per_batch - 1)
        if first < 1 or last < first or last - first + 1 > MAX_PARTS_PER_CALL or first > int(row['part_count']):
            raise DepositError(422, 'invalid', f'parts {first}..{last}: 1 <= first <= last <= {row["part_count"]}, '
                                               f'at most {MAX_PARTS_PER_CALL} per call')
        try:
            done = self._uploaded_parts(row)
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'listing the uploaded parts failed ({_error_code(exc)})')
        return {**self._instructions(deposit_id, row, first, last), 'parts_done': done,
                'expires': _iso(max(float(record['expires']), expires))}

    # complete --------------------------------------------------------------

    def _complete_file(self, deposit_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Complete one file; returns ``{'name', 'ok': bool, ...}`` without raising for missing parts."""
        if row['state'] == 'complete':
            return {'name': row['name'], 'ok': True}
        if row['method'] == 'multipart':
            parts = self._uploaded_parts(row)
            sizes = part_sizes(row['size'], row['part_size'])
            have = {part['part_number']: part for part in parts}
            missing = [number for number in range(1, len(sizes) + 1) if number not in have]
            wrong = [number for number, part in have.items()
                     if number > len(sizes) or part['size'] != sizes[number - 1]]
            if missing or wrong:
                return {'name': row['name'], 'ok': False, 'missing_parts': missing[:200],
                        'wrong_size_parts': sorted(wrong)[:200],
                        'uploaded_bytes': sum(part['size'] for part in parts)}
            self.s3.complete_multipart_upload(
                Bucket=self.bucket, Key=row['key'], UploadId=row['upload_id'],
                MultipartUpload={'Parts': [{'PartNumber': number, 'ETag': f'"{have[number]["etag"]}"'}
                                           for number in range(1, len(sizes) + 1)]})
        head = _head(self.s3, self.bucket, row['key'])
        if head is None:
            return {'name': row['name'], 'ok': False, 'missing': True}
        problems = []
        if head['size'] != row['size']:
            problems.append(f'size {head["size"]} != declared {row["size"]}')
        if head['metadata'].get('deposit') != deposit_id:
            problems.append('the object was not written through this deposit')
        if head['metadata'].get('sha256') != row['sha256']:
            problems.append('x-amz-meta-sha256 differs from the declared sha256')
        if problems:
            if head['metadata'].get('deposit') == deposit_id:      # ours but wrong: remove it, the URL may be reused
                delete_consumed(self.s3, self.bucket, self.settings.prefix, {row['key']: head['etag']})
            return {'name': row['name'], 'ok': False, 'problems': problems}
        self.store.update_file(deposit_id, row['name'], state='complete', etag=head['etag'], completed=self.clock())
        return {'name': row['name'], 'ok': True}

    def _check_sidecar(self, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """(errors, warnings) of the deposited sidecar against the deposit (read from S3, at most 1 MB)."""
        import mus  # noqa: PLC0415

        mus_row = next(row for row in rows.values() if row['kind'] == 'mus')
        response = self.s3.get_object(Bucket=self.bucket, Key=mus_row['key'])
        text = response['Body'].read(TEXT_MAX_BYTES + 1).decode('utf-8', errors='replace')
        sidecar = parse_sidecar_text(text)
        errors: List[str] = []
        warnings: List[str] = []
        h5ad_row = next((row for row in rows.values() if row['kind'] == 'h5ad'), None)
        expected = h5ad_row['sha256'] if h5ad_row else None
        declared = sidecar.get('sha256', '').lower()
        if not declared:
            errors.append('the sidecar has no sha256= line')
        elif expected and declared != expected:
            errors.append(f'the sidecar says sha256={declared[:12]}..., the deposited h5ad is {expected[:12]}...')
        if sidecar.get('data_project') and sidecar['data_project'] != record['data_project']:
            errors.append(f'the sidecar says data_project={sidecar["data_project"]}, the deposit is '
                          f'{record["data_project"]}')
        for key in ('depositor', 'depositor_email'):
            if sidecar.get(key):
                warnings.append(f'{key}= in the sidecar is ignored for identity: the deposit is recorded as '
                                f'{record["user"]}')
        if record.get('update_of') and sidecar.get('update_of') and \
                sidecar['update_of'].upper() != record['update_of'].upper():
            warnings.append(f'the sidecar says update_of={sidecar["update_of"]}; the deposit request '
                            f'({record["update_of"]}) is used')
        if not record.get('update_of'):
            for key, reason in (('license', 'licence_unknown'), ('data_use', 'data_use_unknown')):
                if not sidecar.get(key):
                    warnings.append(f'no {key}= in the sidecar: the deposit will be held ({reason})')
            if (sidecar.get('access') or '').strip().lower() == 'public':
                warnings.append('access=public: the deposit will be held for an admin (public_access)')
            annotation = {key: value for key, value in sidecar.items()}
            try:
                found = mus.validate_annotation(annotation)
            except Exception:  # noqa: BLE001 - the vocabulary check is advisory here
                found = {}
            if found.get('missing_required'):
                warnings.append('required mus fields missing (the check mus.vocab will fail): '
                                + ', '.join(found['missing_required']))
            for message in found.get('invalid_controlled') or []:
                warnings.append(f'outside the vocabulary: {message}')
        return errors, warnings

    def complete(self, identity: Identity, deposit_id: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Complete every file, verify sizes and sha256 metadata, record the ETags (``POST .../complete``)."""
        self._require_active()
        record = self._get_own(identity, deposit_id)
        self._require_uploading(record)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        results = []
        try:
            for row in rows.values():
                results.append(self._complete_file(deposit_id, row))
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'completing the upload failed ({_error_code(exc)}); try again')
        incomplete = [entry for entry in results if not entry['ok']]
        if incomplete:
            raise DepositError(409, 'incomplete', 'not every file is uploaded completely', files=incomplete)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        try:
            errors, warnings = self._check_sidecar(record, rows)
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'reading the sidecar back failed ({_error_code(exc)})')
        if errors:
            mus_row = next(row for row in rows.values() if row['kind'] == 'mus')
            self.store.update_file(deposit_id, mus_row['name'], state='pending', etag=None, completed=None)
            head = _head(self.s3, self.bucket, mus_row['key'])
            if head and head['metadata'].get('deposit') == deposit_id:
                delete_consumed(self.s3, self.bucket, self.settings.prefix, {mus_row['key']: head['etag']})
            raise DepositError(422, 'sidecar_mismatch', '; '.join(errors) + '. Upload a corrected sidecar (fresh '
                                                                            'URL from .../urls) and complete again.')
        h5ad_row = next((row for row in rows.values() if row['kind'] == 'h5ad'), None)
        mus_row = next(row for row in rows.values() if row['kind'] == 'mus')
        h5ad_etag = h5ad_row['etag'] if h5ad_row else record['h5ad_etag']
        if not h5ad_row:                          # a sidecar for an h5ad still in incoming: it must not have changed
            head = _head(self.s3, self.bucket, record['watch_key'])
            if head is None or head['etag'] != record['h5ad_etag']:
                raise DepositError(409, 'h5ad_changed', 'the h5ad this sidecar belongs to changed or is gone; '
                                                        'withdraw this deposit and deposit the h5ad again')
        now = self.clock()
        self.store.update(deposit_id, state='uploaded', completed=now, h5ad_etag=h5ad_etag,
                          mus_etag=mus_row['etag'])
        self.store.event(deposit_id, identity.user, 'completed', f'{len(rows)} file(s)')
        return {'deposit_id': deposit_id, 'state': 'uploaded',
                'files': [{'name': row['name'], 'key': row['key'], 'size': row['size'], 'sha256': row['sha256'],
                           'etag': row['etag']} for row in rows.values()],
                'warnings': warnings,
                'next': 'The watcher checks the deposit within a few minutes after it settles; follow it with '
                        f'GET /datasets/deposit/{deposit_id}.'}

    # status ----------------------------------------------------------------

    def _report(self, record: Dict[str, Any], watch_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The redacted report: the watcher's copy, or (when the watcher's newer report has no copy yet) derived."""
        copy = report_copy_path(self.state_dir, record['id'])
        source = Path(watch_row['report_path']) if watch_row and watch_row.get('report_path') else None
        source_ok = source is not None and source.is_file()
        if copy.is_file() and (not source_ok or copy.stat().st_mtime >= source.stat().st_mtime):
            try:
                return json.loads(copy.read_text())
            except (OSError, ValueError):
                pass
        if source_ok:
            try:
                return redacted_report(json.loads(source.read_text()), self.roots)
            except (OSError, ValueError):
                return None
        return None

    def _view(self, record: Dict[str, Any], detail: bool, identity: Identity) -> Dict[str, Any]:
        row = read_watch_row(self.state_dir, record.get('watch_key')) if record['state'] == 'uploaded' else None
        matches = watch_row_matches(record, row)
        state = public_state(record, row if matches else None)
        if record['state'] == 'uploaded' and row and not matches and not record.get('watch_state'):
            state = 'superseded'
        source = row if matches else {}
        held = json.loads(source['held_reasons'] or '[]') if source.get('held_reasons') else json.loads(
            record.get('held_reasons') or '[]')
        view: Dict[str, Any] = {
            'deposit_id': record['id'], 'data_project': record['data_project'], 'basename': record['basename'],
            'state': state, 'db_id': source.get('db_id') or record.get('db_id'),
            'version': source.get('version') or record.get('version'),
            'kind': source.get('kind') or record.get('kind'), 'held_reasons': held,
            'update_of': record.get('update_of'), 'continues': record.get('continues'),
            'total_bytes': record['total_bytes'], 'created': _iso(record['created']),
            'updated': _iso(max(float(record['updated'] or 0), float(source.get('updated') or 0))),
            'completed': _iso(record['completed']),
            'expires': _iso(record['expires']) if record['state'] == 'uploading' else None}
        if identity.is_admin:
            view['user'] = record['user']
        if not detail:
            return view
        error = source.get('error') if source else record.get('error')
        warnings = json.loads(source['warnings'] or '[]') if source.get('warnings') else json.loads(
            record.get('warnings') or '[]')
        report = self._report(record, row if matches else None)
        view.update({
            'error': sanitize_text(error, self.roots),
            'warnings': [sanitize_text(text, self.roots) for text in warnings],
            'files': [{'name': entry['name'], 'kind': entry['kind'], 'key': entry['key'], 'size': entry['size'],
                       'sha256': entry['sha256'], 'method': entry['method'], 'state': entry['state']}
                      for entry in self.store.files(record['id'])],
            'watch': ({'state': row['state'], 'served': bool(row.get('served')),
                       'catalog_verified': bool(row.get('catalog_verified')),
                       'incoming_deleted': bool(row.get('deleted'))} if matches else None),
            'report': report_summary(report),
            'report_available': report is not None,
            'events': [{'time': _iso(entry['ts']), 'action': entry['action'], 'actor': entry['actor'],
                        'detail': sanitize_text(entry['detail'], self.roots)}
                       for entry in self.store.events(record['id'])],
            'approval': json.loads(record['approval']) if record.get('approval') else None,
        })
        return view

    def list(self, identity: Identity, all_users: bool = False) -> Dict[str, Any]:
        if self.settings.active:
            self.expire_stale()
        if all_users and not identity.is_admin:
            raise DepositError(403, 'forbidden', 'only admins list every deposit')
        records = self.store.deposits(None if all_users else identity.user)
        return {'deposits': [self._view(record, False, identity) for record in records], 'all': bool(all_users)}

    def status(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        record = self._get_own(identity, deposit_id)
        if record['state'] == 'uploading' and record['expires'] <= self.clock() and self.settings.active:
            self.expire_stale()
            record = self.store.get(deposit_id) or record
        return self._view(record, True, identity)

    def report(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        record = self._get_own(identity, deposit_id)
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        report = self._report(record, row if watch_row_matches(record, row) else None)
        if report is None:
            raise DepositError(404, 'no_report', 'no check report for this deposit yet')
        return report

    # withdraw / approve / reject ------------------------------------------

    def _abort_and_delete(self, record: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Abort this deposit's multipart uploads and delete its own objects (metadata and ETag checked)."""
        aborted, deleted, kept = [], [], []
        for row in self.store.files(record['id']):
            if row['method'] == 'multipart' and row['state'] != 'complete' and row['upload_id']:
                try:
                    self.s3.abort_multipart_upload(Bucket=self.bucket, Key=row['key'], UploadId=row['upload_id'])
                except Exception as exc:  # noqa: BLE001
                    if _error_code(exc) not in ('NoSuchUpload', '404'):
                        raise
                aborted.append(row['name'])
                self.store.update_file(record['id'], row['name'], state='aborted')
                continue
            head = _head(self.s3, self.bucket, row['key'])
            if head is None:
                continue
            if head['metadata'].get('deposit') != record['id']:
                kept.append({'key': row['key'], 'reason': 'written by another deposit or upload'})
                continue
            if row['etag'] and head['etag'] != row['etag']:
                kept.append({'key': row['key'], 'reason': 'changed since it was completed'})
                continue
            outcomes: Dict[str, str] = {}
            delete_consumed(self.s3, self.bucket, self.settings.prefix, {row['key']: head['etag']},
                            record=lambda key, etag, result: outcomes.__setitem__(key, result))
            if outcomes.get(row['key']) == 'deleted':
                deleted.append(row['key'])
                self.store.update_file(record['id'], row['name'], state='deleted')
            elif outcomes.get(row['key']) != 'skipped:absent':
                kept.append({'key': row['key'], 'reason': outcomes.get(row['key'], 'unknown')})
        self.store.event(record['id'], actor, 'cleanup', json.dumps({'aborted': aborted, 'deleted': deleted,
                                                                     'kept': kept}))
        return {'aborted': aborted, 'deleted': deleted, 'kept': kept}

    def withdraw(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        """Abort and delete this deposit's own incoming objects; mark it withdrawn."""
        self._require_active()
        record = self._get_own(identity, deposit_id)
        if record['state'] in ('withdrawn', 'expired'):
            raise DepositError(409, 'not_withdrawable', f'the deposit is already {record["state"]}')
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        state = public_state(record, row if watch_row_matches(record, row) else None)
        if state == 'checking':
            raise DepositError(409, 'busy', 'the watcher is processing this deposit; withdraw it once it is done')
        if state == 'live':
            raise DepositError(409, 'live', 'the deposit is published; ask an admin to retire the dataset')
        try:
            result = self._abort_and_delete(record, identity.user)
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'withdrawing failed ({_error_code(exc)}); try again')
        self.store.update(deposit_id, state='withdrawn')
        self.store.event(deposit_id, identity.user, 'withdrawn', '')
        return {'deposit_id': deposit_id, 'state': 'withdrawn', **result}

    @staticmethod
    def _require_admin(identity: Identity) -> None:
        if not identity.is_admin:
            raise DepositError(403, 'forbidden', 'only admins approve or reject deposits')

    def _held_row(self, record: Dict[str, Any]) -> Dict[str, Any]:
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        if record['state'] != 'uploaded' or not watch_row_matches(record, row):
            raise DepositError(409, 'not_reviewable', 'the watcher has no current record of this deposit')
        return row

    def approve(self, identity: Identity, deposit_id: str, note: str = '') -> Dict[str, Any]:
        """Admin: publish a held deposit past its held reasons (the watcher's approve)."""
        import ingest_watch  # noqa: PLC0415

        self._require_admin(identity)
        record = self._get_own(identity, deposit_id)
        row = self._held_row(record)
        if row['state'] != 'held':
            raise DepositError(409, 'not_held', f'the deposit is {public_state(record, row)}, not held')
        watch_db = ingest_watch.StateDB(self.state_dir / WATCH_DB_FILE)
        try:
            approval = ingest_watch.approve(watch_db, row['key'], identity.user, note or '', channel='api')
        except ValueError as exc:
            raise DepositError(409, 'not_held', str(exc))
        finally:
            watch_db.close()
        self.store.update(deposit_id, approval=json.dumps({'by': identity.user, 'reasons': approval['reasons'],
                                                           'note': note or '', 'date': approval['date']}))
        self.store.event(deposit_id, identity.user, 'approved', ', '.join(approval['reasons']))
        return {'deposit_id': deposit_id, 'state': 'stable', 'approval': {'by': identity.user,
                                                                          'reasons': approval['reasons'],
                                                                          'note': note or ''},
                'next': 'the next poll of the watcher publishes it'}

    def reject(self, identity: Identity, deposit_id: str, note: str = '') -> Dict[str, Any]:
        """Admin: reject a held, failed or needs_decision deposit (the watcher's reject)."""
        import ingest_watch  # noqa: PLC0415

        self._require_admin(identity)
        record = self._get_own(identity, deposit_id)
        row = self._held_row(record)
        watch_db = ingest_watch.StateDB(self.state_dir / WATCH_DB_FILE)
        try:
            ingest_watch.reject(watch_db, row['key'], identity.user, note or '', channel='api')
        except ValueError as exc:
            raise DepositError(409, 'not_rejectable', str(exc))
        finally:
            watch_db.close()
        self.store.update(deposit_id, watch_state='rejected', error=f'rejected by {identity.user}'
                          + (f': {note}' if note else ''))
        self.store.event(deposit_id, identity.user, 'rejected', note or '')
        return {'deposit_id': deposit_id, 'state': 'rejected', 'note': note or ''}


SEED_MD_TEMPLATE = """---
title: Microglia from the prefrontal cortex of Alzheimer's disease donors
description: >-
  Single-nucleus RNA-seq of microglia sorted from ... (at least 200 characters,
  taken from the paper: tissue, donors, technology, what was compared).
replicate_unit: donor            # donor | animal | sample | none
replicate_column: donor_id       # the obs column that holds the replicate
disease_basis: author_dx         # when ant_disease is published
api:
  obs_rename:
    donor_id: ant_donor
    sex: ant_sex
  obs_value_rename:               # keyed by the author column; values to the facet vocabulary
    sex: {M: m, F: f}
  obs_derive:
    ant_donor_uid: {source: donor_id, template: '{group_id}:{value}'}
caveats: []
---

Free text about the dataset (optional when description is set).
"""

INDEX_MD_TEMPLATE = """---
group: Smith2024                 # equals data_project
group_id: SM4                    # 3 characters, unique in the store
paper_title: Microglia states in Alzheimer's disease
doi: 10.1000/example
year: 2024
authors:
  - Smith, Anna
  - Jones, Bob
default_dataset: microglia       # basename of the dataset shown first
---

# Smith2024
"""
