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

import hashlib
import json
import math
import os
import socket
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ingest_pipeline import (DE_DATA_EXTS, H5AD_SUFFIX, MUS_SUFFIX, delete_consumed, parse_sidecar_text,
                             valid_segment)

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
#: Every validator below is applied with ``fullmatch``: ``re.match(r'...$')`` also accepts a trailing newline.
NEW_GROUP_RE = re.compile(r'^[A-Z][A-Za-z0-9_]*[0-9]{4}$')
BASENAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$')
#: Basenames the watch route reads as group seeds (``index.md``, ``<x>.index.md``): never a dataset name.
RESERVED_BASENAME_RE = re.compile(r'^(index|.*\.index)$', re.IGNORECASE | re.DOTALL)
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DEPOSIT_ID_RE = re.compile(r'^dep_[0-9a-f]{20}$')
#: A dataset id is ``<group_id>/<dataset id>``, both short alphanumeric codes (generated as 3 characters, e.g.
#: ``AL1/CBL``); no dots, so ``..`` and other path forms never pass.
DB_ID_RE = re.compile(r'^[A-Za-z0-9]{1,16}/[A-Za-z0-9]{1,16}$')
USER_RE = re.compile(r'^[A-Za-z0-9._@-]{1,80}$')

#: States of the deposit record itself. After ``uploaded`` the watcher's state is shown. ``completing``,
#: ``withdrawing`` and ``expiring`` are short locks (compare-and-set) so complete, withdraw and expiry
#: never run on one deposit at the same time.
RECORD_STATES = ('uploading', 'completing', 'uploaded', 'withdrawing', 'withdrawn', 'expiring', 'expired')
OPEN_STATES = ('uploading', 'completing')
#: States after which a deposit's staging folder is never needed again.
TERMINAL_STATES = ('uploaded', 'withdrawn', 'expired')
#: A ``completing`` deposit older than this is treated as interrupted and cleaned up like an expired one.
STALE_COMPLETING_SECONDS = 12 * 3600
#: Watcher states a depositor sees (``pulled`` / ``checked`` are shown as ``checking``).
WATCH_FINAL = ('live', 'held', 'failed', 'needs_decision', 'rejected')
WATCH_BUSY = ('pulled', 'checked')
NOTIFY_DEPOSITOR_ON = ('live', 'held', 'failed', 'expired')

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposits (
    id TEXT PRIMARY KEY, user TEXT NOT NULL, name TEXT, email TEXT, is_admin INTEGER DEFAULT 0,
    data_project TEXT NOT NULL, basename TEXT NOT NULL, update_of TEXT, state TEXT NOT NULL,
    created REAL, updated REAL, expires REAL, completed REAL, total_bytes INTEGER DEFAULT 0,
    watch_key TEXT, h5ad_etag TEXT, mus_key TEXT, mus_etag TEXT, continues TEXT,
    watch_state TEXT, db_id TEXT, version TEXT, kind TEXT, held_reasons TEXT, error TEXT, warnings TEXT,
    report_file TEXT, notified_state TEXT, approval TEXT, h5ad_sha256 TEXT, refreshes INTEGER DEFAULT 0,
    deposit_kind TEXT DEFAULT 'dataset', progress TEXT, complete_error TEXT, worker TEXT
);
CREATE INDEX IF NOT EXISTS deposits_user ON deposits(user);
CREATE INDEX IF NOT EXISTS deposits_watch_key ON deposits(watch_key);
CREATE TABLE IF NOT EXISTS files (
    deposit_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, size INTEGER,
    sha256 TEXT, method TEXT, upload_id TEXT, part_size INTEGER, part_count INTEGER,
    state TEXT NOT NULL, etag TEXT, completed REAL, staging_key TEXT, staging_etag TEXT,
    PRIMARY KEY (deposit_id, name)
);
-- target keys of open deposits: one deposit at a time per key (the PRIMARY KEY is the lock)
CREATE TABLE IF NOT EXISTS open_keys (key TEXT PRIMARY KEY, deposit_id TEXT NOT NULL);
-- owners of datasets published before deposits existed (set by an admin)
CREATE TABLE IF NOT EXISTS owners (
    db_id TEXT PRIMARY KEY, user TEXT NOT NULL, assigned_by TEXT, ts REAL, note TEXT,
    caveat_applied INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_key ON files(key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, deposit_id TEXT, ts REAL, actor TEXT, action TEXT, detail TEXT
);
"""


class StagingChanged(RuntimeError):
    """A staging object changed between its verification and its copy (a re-PUT during complete)."""


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
    #: a central watcher's needs_decision deposit keeps its staged uploads this long (0: 2 x max_open_days)
    decision_days: float = 0.0
    max_open_per_user: int = 5
    max_pending_gb_per_user: float = 400.0
    presign_per_minute: int = 30
    parts_per_batch: int = 100
    single_put_max_mb: int = 64
    max_files: int = 100
    max_per_day: int = 20               # deposits a user may start in 24 hours
    max_refreshes: int = 500            # URL refresh calls per deposit
    async_complete_gb: float = 5.0      # deposits larger than this complete in the background (202)
    complete_lease_minutes: float = 10.0  # a completing deposit not touched for this long may be resumed
    update_policy: str = 'own'          # own | any
    prefix: str = '__incoming/'
    #: Where presigned uploads land: outside the watched prefix, one folder per deposit. Complete copies the
    #: verified files into ``prefix``; a URL used after that can only write here, never a watched key.
    staging_prefix: str = '__deposits/'
    site_id: str = ''
    #: where the watcher pushes published files (``ingest_watch.catalog_prefix``): metadata updates reuse them
    catalog_prefix: str = ''
    bucket: str = ''
    s3: Dict[str, Any] = field(default_factory=dict)
    #: the bucket of the published catalog, when deposits use their own ingestion bucket (``bucket``); metadata
    #: updates copy the published h5ad from it (empty: the same bucket)
    catalog_bucket: str = ''
    #: one central watcher for several sites (design section 24): records travel as signed record.json next to
    #: the uploads, results come back as signed result.json, and this API does the expiry and the e-mails
    central_watcher: bool = False
    site_key_id: str = 'k1'
    site_secret: str = ''
    watcher_public_keys: Sequence[str] = ()
    notify: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        prefix, staging = self.prefix or '', self.staging_prefix or ''
        if not staging.endswith('/') or not prefix.endswith('/') or staging.startswith(prefix) \
                or prefix.startswith(staging):
            raise ValueError(f'datasets_deposit_staging_prefix {staging!r} must be a folder outside the watched '
                             f'prefix {prefix!r} (and the other way round)')

    @property
    def credentials_present(self) -> bool:
        return bool(self.bucket and self.s3.get('access_key_id') and self.s3.get('secret_access_key'))

    @property
    def catalog(self) -> str:
        """The bucket the published files live in (``catalog_bucket``, else the deposit bucket)."""
        return self.catalog_bucket or self.bucket

    @property
    def staging_root(self) -> str:
        """``__deposits/``: the staging prefix without this site's folder (where every site's records live)."""
        folder = f'{self.site_id}/'
        prefix = self.staging_prefix or '__deposits/'
        return prefix[:-len(folder)] if self.site_id and prefix.endswith(folder) else prefix

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.credentials_present)


# ── redaction ────────────────────────────────────────────────────────────────

_ABS_PATH = re.compile(r'(?<![\w.:/~>-])/(?:[^\s\'"`,;()<>\[\]{}]+/)+([^\s\'"`,;()<>\[\]{}/]+)')


_S3_URL = re.compile(r's3://\S+')


def sanitize_text(text: Any, roots: Optional[Dict[str, Any]] = None) -> Any:
    """Remove server paths from a message: known roots become ``<label>``, other absolute paths ``<path>/<name>``,
    ``s3://`` addresses ``<s3>``; the root ``bucket`` (a bucket name) is replaced where it stands as a word."""
    if not isinstance(text, str):
        return text
    result = _S3_URL.sub('<s3>', text)
    roots = dict(roots or {})
    bucket = str(roots.pop('bucket', '') or '')
    for label, root in sorted(roots.items(), key=lambda pair: -len(str(pair[1]))):
        if root:
            result = result.replace(str(root).rstrip('/'), f'<{label}>')
    result = _ABS_PATH.sub(lambda match: f'<path>/{match.group(1)}', result)
    if bucket:
        result = re.sub(rf'(?<![\w.-]){re.escape(bucket)}(?![\w.-])', '<bucket>', result)
    return result


def _sanitize_tree(node: Any, roots: Optional[Dict[str, Path]]) -> Any:
    if isinstance(node, str):
        return sanitize_text(node, roots)
    if isinstance(node, list):
        return [_sanitize_tree(entry, roots) for entry in node]
    if isinstance(node, dict):
        return {key: _sanitize_tree(value, roots) for key, value in node.items()}
    return node


#: Instructions in check texts that only an admin can follow, and what a depositor reads instead.
_ADMIN_ONLY = [
    (re.compile(r'no overlap index; an admin runs `?ant ingest-index build`?'),
     'not checked on this site yet (its reference index is not built); nothing for you to do'),
    (re.compile(r'\(no gene universe for organism [^;)]*; run ant ingest-index\)'),
     '(not judged on this site yet: its gene reference is not built; nothing for you to do)'),
    (re.compile(r'\(?ant-score-fleet run --only <db_id>\)?'), 'by the site after publication'),
]
_ADMIN_COMMAND = re.compile(r'`?\bant[ -](ingest-index|score-fleet|ingest-check|sync|convert|md-check)\b[^.;)]*`?')


#: the "(API login NAME)" / "(OS user NAME)" the watcher records after a decision's author
ADMIN_WHO_RE = re.compile(r' \((?:API login|OS user) [^)]*\)')


def depositor_text(text: Any) -> Any:
    """A check text without instructions only an admin can follow (commands, index builds)."""
    if not isinstance(text, str):
        return text
    for pattern, replacement in _ADMIN_ONLY:
        text = pattern.sub(replacement, text)
    return _ADMIN_COMMAND.sub('an admin task', text)


def redacted_report(report: Dict[str, Any], roots: Optional[Dict[str, Path]] = None) -> Dict[str, Any]:
    """The curator view of an ``ant-ingest-check`` report without server paths.

    ``admin_detail`` (other datasets' ids, cohort names, demographic match
    tables) is dropped, as in ``ant ingest-check --audience curator``; every
    string loses its server paths.
    """
    view = {key: value for key, value in report.items() if key not in ('admin_detail', 'log', 'paths')}
    view['audience'] = 'curator'
    checks = []
    for entry in view.get('checks') or []:
        if isinstance(entry, dict):
            entry = {**entry, 'summary': depositor_text(entry.get('summary')), 'fix': depositor_text(entry.get('fix'))}
        checks.append(entry)
    if 'checks' in view:
        view['checks'] = checks
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
    if not DEPOSIT_ID_RE.fullmatch(deposit_id):
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
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        for table, column, kind in (('deposits', 'h5ad_sha256', 'TEXT'), ('deposits', 'refreshes', 'INTEGER DEFAULT 0'),
                                    ('files', 'staging_key', 'TEXT'), ('files', 'staging_etag', 'TEXT'),
                                    ('deposits', 'deposit_kind', "TEXT DEFAULT 'dataset'"),
                                    ('deposits', 'progress', 'TEXT'), ('deposits', 'complete_error', 'TEXT'),
                                    ('deposits', 'worker', 'TEXT'),
                                    # artefact and collection deposits (lib/artefact_deposit.py, REST 2.47)
                                    ('deposits', 'artefact_ref', 'TEXT'), ('deposits', 'artefact', 'TEXT'),
                                    ('deposits', 'group_id', 'TEXT'),
                                    # one central watcher for several sites (lib/deposit_records.py, 2.47)
                                    ('deposits', 'site_id', 'TEXT'), ('deposits', 'record_version', 'INTEGER'),
                                    ('deposits', 'record_digest', 'TEXT'), ('deposits', 'result_version', 'INTEGER'),
                                    ('deposits', 'result_digest', 'TEXT'),
                                    # watcher time of the import (orders remote deposits, never a site's clock)
                                    # and why two deposits claiming one object are both held (review S1)
                                    ('deposits', 'imported', 'REAL'), ('deposits', 'disputed', 'TEXT')):
            if column not in {row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')}:
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')

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

    def cas(self, deposit_id: str, from_states: Sequence[str], to_state: str, not_importing: bool = False,
            **fields: Any) -> bool:
        """Move the deposit to ``to_state`` only when it is in one of ``from_states`` (the per-deposit lock).

        ``not_importing``: also only while the watcher is not importing it (DE-only deposits)."""
        fields.setdefault('updated', time.time())
        assignments = ', '.join(f'{name} = ?' for name in fields)
        extra = " AND COALESCE(watch_state, '') != 'importing'" if not_importing else ''
        with self._lock:
            cursor = self.conn.execute(
                f'UPDATE deposits SET state = ?, {assignments} WHERE id = ? AND state IN '
                f'({", ".join("?" * len(from_states))}){extra}',
                [to_state] + list(fields.values()) + [deposit_id] + list(from_states))
            return cursor.rowcount == 1

    def release_keys(self, deposit_id: str) -> None:
        with self._lock:
            self.conn.execute('DELETE FROM open_keys WHERE deposit_id = ?', (deposit_id,))

    def reserve(self, record: Dict[str, Any], files: Sequence[Dict[str, Any]], now: float,
                settings: 'DepositSettings',
                extra_check: Optional[Callable[[sqlite3.Connection], None]] = None) -> None:
        """Check the per-user limits and insert the deposit, its files and its key locks in one transaction.

        ``BEGIN IMMEDIATE`` serialises concurrent creates (also across processes), and the primary key of
        ``open_keys`` makes a key part of at most one open deposit. ``extra_check(conn)`` runs inside the same
        transaction after the limits (artefact deposits: one pending deposit per artefact or collection) and
        refuses by raising :class:`DepositError`.
        """
        user = record['user']
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                open_count = self.conn.execute(
                    f"SELECT COUNT(*) FROM deposits WHERE user = ? AND state IN ('uploading', 'completing') "
                    f"AND expires > ?", (user, now)).fetchone()[0]
                if open_count >= settings.max_open_per_user:
                    raise DepositError(429, 'too_many_open_deposits', f'you have {settings.max_open_per_user} open '
                                                                      f'deposits; complete or withdraw one first')
                today = self.conn.execute('SELECT COUNT(*) FROM deposits WHERE user = ? AND created > ?',
                                          (user, now - 86400)).fetchone()[0]
                if today >= settings.max_per_day:
                    raise DepositError(429, 'too_many_deposits_today', f'at most {settings.max_per_day} deposits per '
                                                                       f'user in 24 hours; try again later')
                pending = self.conn.execute(
                    "SELECT COALESCE(SUM(total_bytes), 0) FROM deposits WHERE user = ? AND (state IN "
                    "('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') NOT IN "
                    "('live', 'rejected')))", (user,)).fetchone()[0]
                if pending + record['total_bytes'] > settings.max_pending_gb_per_user * 1024 ** 3:
                    raise DepositError(413, 'quota_exceeded',
                                       f'{(pending + record["total_bytes"]) / 1024 ** 3:.1f} GB would be pending for '
                                       f'you (limit {settings.max_pending_gb_per_user:g} GB); wait until earlier '
                                       f'deposits are published or withdraw them')
                if extra_check is not None:
                    extra_check(self.conn)
                # locks of deposits that expired but were not cleaned up yet do not count
                self.conn.execute("DELETE FROM open_keys WHERE deposit_id IN (SELECT id FROM deposits WHERE "
                                  "state = 'uploading' AND expires <= ?)", (now,))
                columns = list(record)
                self.conn.execute(f'INSERT INTO deposits ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))})',
                                  [record[name] for name in columns])
                for entry in files:
                    names = list(entry)
                    self.conn.execute(f'INSERT INTO files ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
                                      [entry[name] for name in names])
                    try:
                        self.conn.execute('INSERT INTO open_keys (key, deposit_id) VALUES (?, ?)',
                                          (entry['key'], record['id']))
                    except sqlite3.IntegrityError:
                        holder = self.conn.execute('SELECT d.user, d.id FROM open_keys k JOIN deposits d ON '
                                                   'd.id = k.deposit_id WHERE k.key = ?', (entry['key'],)).fetchone()
                        which = holder[1] if holder and holder[0] == user else 'of another user'
                        hint = (f'; name yours {record["basename"]}.index.md (kind index_md) instead'
                                if entry['name'] == 'index.md' else '; complete or withdraw it first')
                        raise DepositError(409, 'key_in_open_deposit',
                                           f'{entry["name"]} is part of the open deposit {which}{hint}')
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def import_remote(self, deposit_id: str, values: Dict[str, Any], files: Sequence[Dict[str, Any]]) -> None:
        """The watcher's copy of another site's verified record (``deposit_records.import_record``).

        Only the columns a site owns are written; the watcher's own columns (watch_state, db_id, report_file,
        ...) of an earlier import stay. The files are replaced as a whole.
        """
        columns = list(values)
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                updates = ', '.join(f'{name} = excluded.{name}' for name in columns if name != 'id')
                self.conn.execute(f'INSERT INTO deposits ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))}) '
                                  f'ON CONFLICT(id) DO UPDATE SET {updates}', [values[name] for name in columns])
                self.conn.execute('DELETE FROM files WHERE deposit_id = ?', (deposit_id,))
                for entry in files:
                    entry = {**entry, 'deposit_id': deposit_id}
                    entry.setdefault('state', 'pending')
                    names = list(entry)
                    self.conn.execute(f'INSERT INTO files ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
                                      [entry[name] for name in names])
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def claims(self, keys: Mapping[str, str], exclude: str) -> List[Dict[str, Any]]:
        """Other deposits (not ``exclude``) that claim one of ``keys`` (incoming key -> ETag): the key is locked
        by a deposit still uploading or completing, or a deposit completing or completed has a file with that
        key, whatever its ETag and watch state (the caller skips final ones). Keyed by the key alone: another
        site cannot take a key by putting other bytes there."""
        if not keys:
            return []
        found: Dict[str, Dict[str, Any]] = {}
        marks = ', '.join('?' * len(keys))
        for row in self._rows(f"SELECT d.id, d.site_id, d.state, d.watch_state, d.disputed, k.key FROM open_keys k JOIN deposits d "
                              f"ON d.id = k.deposit_id WHERE d.state IN ('uploading', 'completing') AND k.key IN ({marks}) "
                              f"AND d.id != ?", list(keys) + [exclude]):
            found.setdefault(row['id'], {**row, 'open': True})
        for row in self._rows(f"SELECT d.id, d.site_id, d.state, d.watch_state, d.disputed, f.key, f.etag FROM files f JOIN deposits d "
                              f"ON d.id = f.deposit_id WHERE d.state IN ('completing', 'uploaded') AND f.state = 'complete' "
                              f"AND f.key IN ({marks}) AND d.id != ?", list(keys) + [exclude]):
            found.setdefault(row['id'], {**row, 'open': False})
        return list(found.values())

    def dispute(self, deposit_id: str, reason: str) -> None:
        """Hold a deposit another site's record claims the same objects of: never linked again (an admin decides)."""
        self.update(deposit_id, disputed=reason[:2000], watch_state='needs_decision', error=reason[:2000])
        self.event(deposit_id, 'watcher', 'disputed', reason)

    def remote_rows(self, local_site: Optional[str] = None) -> List[Dict[str, Any]]:
        """Deposits imported from other sites (``site_id`` set and not ``local_site``)."""
        return [row for row in self._rows("SELECT * FROM deposits WHERE COALESCE(site_id, '') != ''")
                if row.get('site_id') != local_site]

    def claim_import(self, deposit_id: str) -> bool:
        """The watcher takes a completed DE-only deposit (watch state ``importing``); withdraw then waits."""
        with self._lock:
            cursor = self.conn.execute("UPDATE deposits SET watch_state = 'importing', updated = ? WHERE id = ? AND "
                                       "state = 'uploaded' AND COALESCE(watch_state, '') = ''",
                                       (time.time(), deposit_id))
            return cursor.rowcount == 1

    def finish_complete(self, deposit_id: str, file_etags: Dict[str, str], now: float, **fields: Any) -> bool:
        """In one transaction: ``completing -> uploaded`` with ``fields``, and the files complete with their
        incoming ETags. False (nothing changed) when the deposit is no longer completing."""
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                assignments = ', '.join(f'{name} = ?' for name in fields)
                cursor = self.conn.execute(
                    f"UPDATE deposits SET state = 'uploaded', updated = ?{', ' if fields else ''}{assignments} "
                    f"WHERE id = ? AND state = 'completing'", [now] + list(fields.values()) + [deposit_id])
                if cursor.rowcount != 1:
                    self.conn.execute('ROLLBACK')
                    return False
                for name, etag in file_etags.items():
                    self.conn.execute("UPDATE files SET state = 'complete', etag = ?, completed = ? WHERE "
                                      "deposit_id = ? AND name = ?", (etag, now, deposit_id, name))
                self.conn.execute('DELETE FROM open_keys WHERE deposit_id = ?', (deposit_id,))
                self.conn.execute('COMMIT')
                return True
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def redeclare(self, deposit_id: str, name: str, size: int, sha256: str) -> bool:
        """Declare the sidecar again, only while the deposit is uploading (one transaction)."""
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                deposit = self.conn.execute("SELECT state FROM deposits WHERE id = ?", (deposit_id,)).fetchone()
                row = self.conn.execute("SELECT size, state FROM files WHERE deposit_id = ? AND name = ? AND "
                                        "kind = 'mus'", (deposit_id, name)).fetchone()
                if deposit is None or deposit[0] != 'uploading' or row is None or row[1] == 'complete':
                    self.conn.execute('ROLLBACK')
                    return False
                self.conn.execute("UPDATE files SET size = ?, sha256 = ?, method = 'put', state = 'pending', "
                                  "staging_etag = NULL, upload_id = NULL, part_size = NULL, part_count = NULL "
                                  "WHERE deposit_id = ? AND name = ?", (size, sha256, deposit_id, name))
                self.conn.execute("UPDATE deposits SET total_bytes = total_bytes - ? + ?, updated = ? WHERE id = ?",
                                  (int(row[0] or 0), size, time.time(), deposit_id))
                self.conn.execute('COMMIT')
                return True
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def held_keys(self) -> Dict[str, str]:
        """Target keys of deposits that are uploading or completing: the watcher leaves those items alone."""
        return {row['key']: row['deposit_id'] for row in self._rows(
            "SELECT k.key, k.deposit_id FROM open_keys k JOIN deposits d ON d.id = k.deposit_id "
            "WHERE d.state IN ('uploading', 'completing')")}

    def deposit_objects(self) -> Dict[str, Tuple[str, str]]:
        """``{incoming key: (deposit id, ETag)}`` of the completed files of every deposit still waiting."""
        return {row['key']: (row['deposit_id'], row['etag']) for row in self._rows(
            "SELECT f.key, f.deposit_id, f.etag FROM files f JOIN deposits d ON d.id = f.deposit_id "
            "WHERE d.state IN ('completing', 'uploaded') AND f.state = 'complete' AND f.etag IS NOT NULL")}

    def chain(self, deposit_id: str) -> List[str]:
        """The deposit and the deposits it continues (a corrected sidecar continues the h5ad's deposit)."""
        found: List[str] = []
        current: Optional[str] = deposit_id
        while current and current not in found and len(found) < 50:
            found.append(current)
            record = self.get(current)
            current = record.get('continues') if record else None
        return found

    def set_owner(self, db_id: str, user: str, assigned_by: str, note: str = '') -> None:
        with self._lock:
            self.conn.execute('INSERT OR REPLACE INTO owners (db_id, user, assigned_by, ts, note, caveat_applied) '
                              'VALUES (?, ?, ?, ?, ?, 0)', (db_id.upper(), user, assigned_by, time.time(), note))

    def owners(self, pending_caveat: bool = False) -> List[Dict[str, Any]]:
        sql = 'SELECT * FROM owners' + (' WHERE caveat_applied = 0' if pending_caveat else '') + ' ORDER BY ts'
        return self._rows(sql)

    def mark_owner_caveat(self, db_id: str) -> None:
        with self._lock:
            self.conn.execute('UPDATE owners SET caveat_applied = 1 WHERE db_id = ?', (db_id.upper(),))

    def update(self, deposit_id: str, **fields: Any) -> None:
        fields.setdefault('updated', time.time())
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

    def find_for_item(self, h5ad_key: str, h5ad_etag: str, mus_key: Optional[str],
                      mus_etag: Optional[str]) -> Optional[Dict[str, Any]]:
        """The completed deposit whose h5ad and sidecar are exactly the listed objects (newest first, by this
        host's clock: the watcher's import time of a remote record, else the local completion; never a site's
        own time). A disputed deposit is never linked."""
        rows = self._rows("SELECT * FROM deposits WHERE state = 'uploaded' AND watch_key = ? AND h5ad_etag = ? "
                          "AND mus_key = ? AND mus_etag = ? AND COALESCE(disputed, '') = '' "
                          "ORDER BY COALESCE(imported, completed) DESC, rowid DESC LIMIT 1",
                          (h5ad_key, h5ad_etag, mus_key or '', mus_etag or ''))
        return rows[0] if rows else None

    def owner_of_dataset(self, db_id: str) -> Optional[str]:
        """Owner set by an admin, else the user of the newest deposit that published ``db_id``."""
        assigned = self._rows('SELECT user FROM owners WHERE db_id = ?', (db_id.upper(),))
        if assigned:
            return assigned[0]['user']
        rows = self._rows("SELECT user FROM deposits WHERE UPPER(db_id) = UPPER(?) AND watch_state = 'live' "
                          "ORDER BY COALESCE(imported, completed) DESC, rowid DESC LIMIT 1", (db_id,))
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
    """``group_id/id`` of ``<store>/<data_project>/<basename>.md`` (None when the dataset is not in the store).

    A ``.md`` or ``index.md`` that does not parse still counts as an existing dataset (``<group>/<basename>``).
    """
    from metadata import parse_md_file  # noqa: PLC0415

    md = Path(store_root) / data_project / f'{basename}.md'
    if not md.exists():
        return None
    try:
        meta = parse_md_file(md)[0] or {}
        index = Path(store_root) / data_project / 'index.md'
        group_id = meta.get('group_id') or (parse_md_file(index)[0].get('group_id') if index.exists() else None)
    except Exception:  # noqa: BLE001 - a broken .md is still a dataset that exists
        return f'{data_project}/{basename}'
    return f'{group_id}/{meta["id"]}' if group_id and meta.get('id') else f'{data_project}/{basename}'


def find_dataset_md(store_root: Path, db_id: str) -> Optional[Path]:
    """The ``.md`` of a published dataset by its db id (case-insensitive), or None."""
    import ingest_versions  # noqa: PLC0415

    for entry in ingest_versions.find_datasets(store_root):
        if entry['db_id'].upper() == db_id.upper():
            return Path(entry['dataset_dir']) / f'{entry["basename"]}.md'
    return None


def de_presets_for_vocab() -> Dict[str, Dict[str, Any]]:
    """Every DE format with the sidecar keys it requires (from ``lib/de_presets.py``)."""
    from de_presets import PRESETS  # noqa: PLC0415
    from ingest_pipeline import DE_FLAG_KEYS  # noqa: PLC0415

    presets = {}
    for name, preset in PRESETS.items():
        required = []
        for flag in preset.required_cli_flags:
            key = DE_FLAG_KEYS.get(flag)
            if key and not (key == 'de_method' and preset.default_method):
                required.append(key)
        presets[name] = {'description': preset.description, 'shape': preset.shape,
                         'required_keys': ['de_format'] + required,
                         'default_method': preset.default_method,
                         'default_reference_type': preset.default_reference_type,
                         'required_columns': list(preset.required_in_file),
                         'column_map': dict(preset.column_map)}
    return presets


def facet_registry_for_vocab() -> List[Dict[str, Any]]:
    """Every registered (and deprecated) ``ant_*`` facet with what a client needs to propose a mapping."""
    import obscol_tags  # noqa: PLC0415

    facets = []
    for tag, entry in sorted(obscol_tags.facet_registry().items()):
        definition = obscol_tags.facet_definition(tag)
        facets.append({'column': entry.get('column') or f'ant_{tag}', 'tag': tag, 'status': entry.get('status'),
                       'kind': entry.get('kind'), 'controlled_values': entry.get('controlled_values'),
                       'range': entry.get('range'), 'integer': entry.get('integer'), 'ordered': entry.get('ordered'),
                       'facet_scopes': entry.get('facet_scopes') or [], 'replaced_by': entry.get('replaced_by') or [],
                       'computed_from': entry.get('computed_from'),
                       'hint_keywords': definition.get('hint_keywords') or [], 'label': definition.get('label'),
                       'summary': definition.get('summary')})
    return facets


def clean_note(note: Any, limit: int = 300) -> str:
    """One line of plain text: control characters and newlines become spaces, YAML document markers go."""
    text = ''.join(char if char.isprintable() else ' ' for char in str(note or ''))
    text = re.sub(r'-{3,}', '-', text)
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def apply_owner_caveats(store: 'DepositStore', store_root: Path, log: Callable[[str], None] = print,
                        git_identity: str = 'anthive ingest-watch <noreply@anthive.local>',
                        run: Optional[Callable[..., Any]] = None, commit: bool = True) -> int:
    """Record each owner assignment not yet applied in the dataset's ``.md``: a caveat, a patch version bump
    with a changelog entry (every curation decision), written through verified ``md_edit`` operations to a
    temp file renamed over the ``.md`` under the dataset's writer lock, then a git commit of that file alone
    (skipped when the store is no git repository or the path has staged changes of someone else)."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    import md_edit  # noqa: PLC0415
    from ingest_md_fill import curation_caveat, today_iso  # noqa: PLC0415
    from ingest_pipeline import writer_lock  # noqa: PLC0415

    run = run or (lambda cmd: subprocess.run([str(part) for part in cmd], capture_output=True, text=True))
    applied = 0
    for row in store.owners(pending_caveat=True):
        md = find_dataset_md(store_root, row['db_id'])
        if md is None:
            log(f'owner of {row["db_id"]}: no such dataset in {store_root}; caveat not written')
            continue
        user, by, note = clean_note(row['user'], 80), clean_note(row['assigned_by'], 80), clean_note(row.get('note'))
        today = today_iso()
        message = (f'Deposits of new versions of this dataset through the API are accepted from {user} '
                   f'(owner set by {by} on {time.strftime("%Y-%m-%d", time.localtime(row["ts"]))})'
                   + (f': {note}' if note else '.'))
        caveat = curation_caveat(message, 'dataset_owner', today, detected_by='ant-ingest-watch')
        try:
            with writer_lock(md.with_suffix('.duckdb'), timeout=60):
                text = md.read_text(encoding='utf-8')
                new_text = md_edit.apply_edits(text, [md_edit.append_top('caveats', caveat),
                                                      md_edit.bump_version([f'Caveat: {message}'], today, 'patch')])
                partial = md.with_name(f'.{md.name}.owner-partial')
                partial.write_text(new_text, encoding='utf-8')
                os.replace(partial, md)
        except Exception as exc:  # noqa: BLE001 - an unusual .md or a busy writer: a person or the next poll
            log(f'owner of {row["db_id"]}: caveat not written ({exc})')
            continue
        store.mark_owner_caveat(row['db_id'])
        applied += 1
        if commit:
                _commit_md(store_root, md, f'deposit owner of {row["db_id"]}: {user}\n\n{message}\n', git_identity,
                       run, log)
    return applied


def _commit_md(store_root: Path, md: Path, message: str, git_identity: str, run: Callable[..., Any],
               log: Callable[[str], None]) -> None:
    """Commit one ``.md`` the route wrote, the way the watcher commits (only that path, never others' staging)."""
    store_root = Path(store_root)
    if not (store_root / '.git').exists():
        log(f'{store_root} is not a git repository: {md.name} not committed')
        return
    rel = str(md.relative_to(store_root))
    staged = run(['git', '-C', store_root, 'diff', '--cached', '--name-only', '--', rel])
    if staged.returncode != 0 or staged.stdout.strip():
        log(f'{rel}: staged changes of someone else; not committed, commit by hand')
        return
    name, _, email = git_identity.partition('<')
    result = run(['git', '-C', store_root, '-c', f'user.name={name.strip()}', '-c',
                  f'user.email={email.rstrip(">").strip()}', 'commit', '--only', '-m', message, '--', rel])
    if result.returncode != 0:
        log(f'{rel}: git commit failed: {(result.stdout or "") + (result.stderr or "")}'.strip()[:300])
        run(['git', '-C', store_root, 'reset', '-q', '--', rel])


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
                 roots: Optional[Dict[str, Path]] = None,
                 background: Optional[Callable[[Callable[[], None]], None]] = None):
        self.settings = settings
        self.store = store
        self.s3 = s3
        self.store_root = Path(store_root)
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.limiter = RateLimiter(settings.presign_per_minute, clock)
        self.lookup_limiter = RateLimiter(max(60, settings.presign_per_minute * 2), clock)
        #: identifies this process in ``deposits.worker`` (a restart resumes completes of other workers)
        self.worker_id = f'{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}'
        #: how background work runs (a daemon thread; tests pass a synchronous runner)
        self.background: Callable[[Callable[[], None]], None] = background or (
            lambda work: threading.Thread(target=work, name='deposit-complete', daemon=True).start())
        self.roots = dict(roots or {'store': self.store_root, 'state': self.state_dir})
        self.roots.setdefault('bucket', settings.bucket)

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
        record = self.store.get(deposit_id) if DEPOSIT_ID_RE.fullmatch(deposit_id or '') else None
        if record is None or (record['user'] != identity.user and not identity.is_admin):
            raise DepositError(404, 'not_found', f'no deposit {deposit_id!r} of yours')
        return record

    def _metadata(self, deposit_id: str, sha256: str) -> Dict[str, str]:
        return {'sha256': sha256, 'deposit': deposit_id}

    # vocabulary ------------------------------------------------------------

    def vocab(self, identity: Optional[Identity] = None) -> Dict[str, Any]:
        """What a deposit must contain, from the server's own vocabulary and rules (``caller``: what the
        caller may do)."""
        import mus  # noqa: PLC0415
        from disease_rule import DISEASE_BASES  # noqa: PLC0415
        from replicates import REPLICATE_UNITS  # noqa: PLC0415
        from ingest_md_fill import DEPOSITOR_EDITABLE_KEYS  # noqa: PLC0415

        vocabulary = mus.load_vocabulary()
        settings = self.settings
        caller = None
        if identity is not None:
            caller = {'user': identity.user, 'is_admin': identity.is_admin,
                      'may_replace_de_study': identity.is_admin, 'may_list_all_deposits': identity.is_admin,
                      'may_update_any_dataset': identity.is_admin or settings.update_policy == 'any'}
        return {
            'schema': 'anthive.deposit_vocab/1',
            'caller': caller,
            'uploads_enabled': settings.active,
            'limits': {'max_gb': settings.max_gb, 'max_files': settings.max_files,
                       'text_file_max_kb': TEXT_MAX_BYTES // 1024,
                       'single_put_max_mb': settings.single_put_max_mb, 'min_part_mb': MIN_PART_BYTES // MiB,
                       'url_ttl_hours': settings.url_ttl_hours, 'max_open_days': settings.max_open_days,
                       'max_open_per_user': settings.max_open_per_user,
                       'max_pending_gb_per_user': settings.max_pending_gb_per_user,
                       'presign_calls_per_minute': settings.presign_per_minute,
                       'parts_per_batch': settings.parts_per_batch, 'max_per_day': settings.max_per_day,
                       'max_refreshes': settings.max_refreshes,
                       'async_complete_gb': settings.async_complete_gb},
            'upload': ('URLs write to a staging key of the deposit (upload_key), outside the watched prefix; '
                       'complete checks the files and copies them to their key under the prefix'),
            'naming': {
                'segment': '^[A-Za-z0-9][A-Za-z0-9._-]*$ (ASCII letters, digits, dot, dash, underscore; '
                           'not starting with a dot or underscore)',
                'basename': '^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$ (the h5ad name without .h5ad, at most 50 characters; '
                            'not index and not ending in .index)',
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
                    'field_info': mus.field_info(),
                    'vocabulary': {name: list(values) for name, values in (vocabulary.get('vocabulary') or {}).items()},
                    'matching': 'case-insensitive; _ space and - are equivalent'},
            'sidecar_keys': {
                'sha256': 'required: sha256 of the h5ad (hex)',
                'data_project': 'optional; must equal the deposit data_project',
                'access': 'internal (default, login only) or public (always held for an admin)',
                'data_use': ('open or controlled; missing or controlled: a warning, never held (data use agreements apply '
                             'to the whole server: everyone with access to it sees every dataset)'),
                'license': 'an SPDX id (CC-BY-4.0, CC0-1.0, ...) or in-house; missing: held',
                'depositor': 'ignored for identity on this route: the Anthive login of the depositor is recorded',
                'depositor_email': 'ignored for identity on this route (the login e-mail is used)',
                'update_of': '<db_id> of the dataset this deposit replaces; required for an update (give it in the '
                             'deposit request too; the request value wins)',
                'previous_sha256': 'optional: sha256 of the published h5ad this deposit replaces',
                'override_<key>': 'true lets an update replace the curated value of <key> (access and license '
                                  'are never replaced automatically; a data_use change is applied without it, '
                                  'with a changelog line and a caveat)',
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
                'required_spec': {'keys': ['title', 'replicate_unit', 'api'],
                                  'one_of': [],
                                  'description': {'key': 'description', 'or_markdown_body': True,
                                                  'min_chars': 200},
                                  'conditional': [{'key': 'disease_basis', 'when': 'api publishes ant_disease'},
                                                  {'key': 'replicate_column', 'when': 'replicate_unit is not none'}],
                                  'leave_out': ['version', 'changelog', 'sha256', 'n_obs', 'n_vars', 'layer_stats',
                                                'ingest']},
                'template': SEED_MD_TEMPLATE,
                'designation': {
                    'primary': 'nothing declared, or is_secondary: false',
                    'reanalysis': 'is_secondary: true and reanalysis_of: <db_id> (or a list)',
                    'donor_group': 'donor_group: <slug> for datasets of one donor collection',
                    'qc_companion': ('qc: true and qc_of: <db_id of the final dataset>: all cells before filtering '
                                     'with per-cell QC statistics. Always secondary (is_secondary: false is '
                                     'refused), never counted or facet-tested, gets a caveat; qc_of may stay empty '
                                     'when the QC dataset is deposited first and is filled by a metadata-only update; '
                                     'the overlap checks do not compare a QC companion with its final dataset'),
                    'editable_on_update': list(DEPOSITOR_EDITABLE_KEYS),
                },
            },
            'index_md': {'required': ['group (equals data_project)', 'group_id (3 characters, unique)',
                                      'doi or paper_title', 'year', 'authors', 'default_dataset'],
                         'required_spec': {'keys': ['group', 'group_id', 'year', 'default_dataset'],
                                           'one_of': [['doi', 'paper_title'], ['authors', 'first_author']],
                                           'equals': {'group': 'data_project'},
                                           'group_id': {'length': 3, 'unique_in_store': True}},
                         'template': INDEX_MD_TEMPLATE},
            'facets': facet_registry_for_vocab(),
            'de_only': ('a deposit with update_of=<db_id> and only kind de files adds DE tables to that published '
                        'dataset. Each table has its <file>.mus naming the dataset by de_dataset_id=<file id> (the '
                        'part after the slash of the db id, e.g. SMI for ST6/SMI; the full db id ST6/SMI is accepted '
                        'too; case-insensitive) or, without de_dataset_id, de_dataset_h5ad=<basename>.h5ad. '
                        'de_format is one of the DE presets; formats that hold one comparison per file (deseq2, '
                        'edger, scanpy, seurat, glmmseq, glmmtmb) also need de_term and de_contrast'),
            'de_presets': de_presets_for_vocab(),
            'metadata_update': ('a deposit with update_of=<db_id> carrying only the sidecar and/or the seed .md (no '
                                'h5ad) changes the metadata of a published dataset: the published h5ad (and '
                                'sidecar, when none is deposited) is reused, the result is a patch version; '
                                'access and license changes still wait for an admin'),
            'de_sidecar_keys': {'de_format': 'required: deseq2 | edger | scanpy | long | seurat | glmmseq | glmmtmb',
                                'de_dataset_id': 'the dataset: file id (SMI) or db id (ST6/SMI)',
                                'de_dataset_h5ad': '<basename>.h5ad, when de_dataset_id is not given',
                                'de_term': 'the model term, required by one-comparison formats',
                                'de_contrast': 'the comparison, required by one-comparison formats',
                                'de_method': 'optional; the format has a default',
                                'de_study_id': 'optional; default the file name without extension',
                                'de_description': 'optional', 'de_covariates': 'optional, comma separated',
                                'de_reference_type': 'optional', 'de_map_<column>': 'optional column renames',
                                'sha256': 'optional sha256 of the table'},
            'held_reasons': {
                'public_access': 'access=public: an admin publishes it',
                'licence_unknown': 'no license= in the sidecar',
                'donor_overlap': 'many donors match an existing cohort: an admin decides',
            },
            'held_reasons_retired': {   # before 2.47.0; older held records may still carry them
                'data_use_unknown': 'no data_use= in the sidecar (no longer held since 2.47.0)',
                'controlled_access': 'data_use=controlled, or a donor cohort flagged controlled (no longer held '
                                     'since 2.47.0)',
            },
            'update_policy': settings.update_policy,
            'states': {'uploading': 'URLs handed out, not completed', 'completing': 'being completed (seconds)',
                       'withdrawing': 'being withdrawn (seconds)', 'uploaded': 'complete, waiting for the watcher',
                       'seen': 'listed by the watcher', 'stable': 'queued', 'checking': 'being checked and built',
                       'live': 'published', 'held': 'clean, waits for an admin',
                       'failed': 'errors in the checks (see the report)',
                       'needs_decision': 'the store conflicts; an admin decides',
                       'rejected': 'an admin rejected it', 'withdrawn': 'withdrawn by the depositor or an admin',
                       'expired': 'not completed in time; uploads aborted', 'superseded': 'a newer upload replaced it'},
        }

    # create ----------------------------------------------------------------

    def _validate_files(self, data_project: str, basename: str, files: Any,
                        de_only: bool = False, mus_optional: bool = False) -> List[Dict[str, Any]]:
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
            if not SHA256_RE.fullmatch(sha):
                raise DepositError(422, 'invalid', f'{name!r}: sha256 must be 64 hex characters')
            counts[kind] = counts.get(kind, 0) + 1
            clean.append({'name': name, 'kind': kind, 'size': size, 'sha256': sha,
                          'key': self._key(data_project, kind, name)})
        for kind in ('h5ad', 'mus', 'seed_md', 'index_md'):
            if counts.get(kind, 0) > 1:
                raise DepositError(422, 'invalid', f'at most one file of kind {kind}')
        if de_only:
            names = {entry['name'] for entry in clean}
            tables = [entry['name'] for entry in clean if entry['name'].endswith(DE_DATA_EXTS)]
            if not tables:
                raise DepositError(422, 'invalid', f'a DE deposit needs at least one table '
                                                   f'({", ".join(DE_DATA_EXTS)}) with its .mus')
            missing = [name for name in tables if name + '.mus' not in names]
            if missing:
                raise DepositError(422, 'invalid', f'every DE table needs its sidecar: {missing[0]}.mus is missing')
            stray = [entry['name'] for entry in clean if not entry['name'].endswith(DE_DATA_EXTS)
                     and not (entry['name'].endswith('.mus') and entry['name'][:-4] in names)]
            if stray:
                raise DepositError(422, 'invalid', f'{stray[0]}: a DE deposit holds only tables and their .mus')
            return clean
        if not counts.get('mus') and not mus_optional:
            raise DepositError(422, 'invalid', f'the sidecar {basename}{MUS_SUFFIX} (kind mus) is required')
        if not counts.get('mus') and not counts.get('seed_md'):
            raise DepositError(422, 'invalid', 'a metadata update carries the sidecar, the seed .md or both')
        return clean

    def _group_dirs(self) -> List[Path]:
        """Group folders of the store: folders with an ``index.md`` (``scripts/`` and the like are not groups)."""
        if not self.store_root.is_dir():
            return []
        return [entry for entry in self.store_root.iterdir() if entry.is_dir() and (entry / 'index.md').exists()]

    def _check_group(self, data_project: str, identity: Identity) -> bool:
        """True when the group exists in the store; raises for a name that cannot be a group."""
        if not isinstance(data_project, str) or not valid_segment(data_project) or len(data_project) > 64:
            raise DepositError(422, 'invalid_data_project', 'data_project: letters, digits, ._- only, at most 64 '
                                                            'characters, not starting with . or _')
        folder = self.store_root / data_project
        if folder.is_dir() and (folder / 'index.md').exists():
            return True
        if folder.exists():
            raise DepositError(422, 'invalid_data_project', f'{data_project!r} is not available as a group name')
        clash = [entry.name for entry in (self.store_root.iterdir() if self.store_root.is_dir() else [])
                 if entry.name.lower() == data_project.lower()]
        if clash:
            shown = f' ({clash[0]!r})' if identity.is_admin else ''
            raise DepositError(422, 'invalid_data_project', f'a group with this name in other letter case exists'
                                                            f'{shown}: spell it exactly as the catalogue does')
        if not NEW_GROUP_RE.fullmatch(data_project):
            raise DepositError(422, 'invalid_data_project', f'{data_project!r} is not an existing group, and a new '
                                                            f'group is named like Smith2024 ({NEW_GROUP_RE.pattern})')
        return False

    # cleanup: expiry, staging, force expiry ---------------------------------

    def _staging_folder(self, deposit_id: str) -> str:
        if not DEPOSIT_ID_RE.fullmatch(deposit_id):
            raise ValueError(f'not a deposit id: {deposit_id!r}')
        return f'{self.settings.staging_prefix}{deposit_id}/'

    def _delete_staging(self, deposit_id: str, row: Dict[str, Any]) -> None:
        """Delete a file's staging object, never anything outside the deposit's staging folder (a reused
        file's source is the published copy and is never deleted)."""
        key = str(row.get('staging_key') or '')
        if row.get('method') == 'reuse' or not key.startswith(self._staging_folder(deposit_id)):
            return
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 - absent is fine; the staging sweep retries
            pass

    def _staging_key(self, deposit_id: str, key: str) -> str:
        """The upload key of a file: its target key below ``<staging_prefix><deposit_id>/``, outside the watch."""
        return self._staging_folder(deposit_id) + key[len(self.settings.prefix):]

    def _list(self, prefix: str) -> List[Dict[str, Any]]:
        objects: List[Dict[str, Any]] = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix):
            objects.extend(page.get('Contents', []) or [])
        return objects

    def _clean_staging(self, deposit_id: str, keep_records: bool = True) -> List[str]:
        """Delete every object below this deposit's staging folder (never outside it).

        ``keep_records``: the signed record.json and result.json of a central watcher (section 24) stay.
        """
        import deposit_records  # noqa: PLC0415

        folder = self._staging_folder(deposit_id)
        removed = []
        for obj in self._list(folder):
            key = obj['Key']
            if not key.startswith(folder) or '..' in key.split('/'):
                continue
            if keep_records and key[len(folder):] in (deposit_records.RECORD_NAME, deposit_records.RESULT_NAME):
                continue
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            removed.append(key)
        return removed

    def sweep_staging(self) -> List[str]:
        """Remove staging objects of finished deposits (late replays of old URLs, leftovers of a crash).

        Only folders of deposits this install knows in a terminal state (``uploaded``, ``withdrawn``,
        ``expired``) are emptied at once. A deposit id this install does not know (another site or a test
        watcher on the same bucket and prefix) is left alone until all its objects are older than
        ``max_open_days``: no deposit of any site can still be open then.
        """
        removed: List[str] = []
        prefix = self.settings.staging_prefix
        newest: Dict[str, float] = {}
        for obj in self._list(prefix):
            deposit_id = obj['Key'][len(prefix):].split('/', 1)[0]
            if not DEPOSIT_ID_RE.fullmatch(deposit_id):
                continue                                   # not a deposit folder: never touched
            modified = obj.get('LastModified')
            stamp = modified.timestamp() if hasattr(modified, 'timestamp') else float(modified or 0)
            newest[deposit_id] = max(newest.get(deposit_id, 0.0), stamp)
        cutoff = self.clock() - self.settings.max_open_days * 86400
        for deposit_id, stamp in newest.items():
            record = self.store.get(deposit_id)
            if record is not None:
                if self.settings.central_watcher and record['state'] == 'uploaded' \
                        and record.get('watch_state') not in ('live', 'rejected', 'failed'):
                    continue              # the central watcher copies from staging: kept until it is final
                                          # (a needs_decision deposit ends after decision_days: expire_decided)
                if record['state'] in TERMINAL_STATES:
                    # a central watcher's record and result stay until the deposit is over and old
                    over = record['state'] in ('withdrawn', 'expired') or record.get('watch_state') in ('live',
                                                                                                        'rejected')
                    removed += self._clean_staging(deposit_id, keep_records=not (over and stamp < cutoff))
            elif stamp < cutoff:
                removed += self._clean_staging(deposit_id, keep_records=False)
        return removed

    def _expire_one(self, record: Dict[str, Any], actor: str, from_states: Sequence[str]) -> bool:
        if not self.store.cas(record['id'], from_states, 'expiring'):
            return False
        try:
            self._abort_and_delete(record, actor=actor)
        except Exception as exc:  # noqa: BLE001 - S3 trouble: back to its state, try again on the next call
            self.store.cas(record['id'], ('expiring',), record['state'])
            self.store.event(record['id'], actor, 'cleanup_failed', _error_code(exc))
            return False
        self.store.cas(record['id'], ('expiring',), 'expired')
        self.store.release_keys(record['id'])
        self.store.event(record['id'], actor, 'expired', 'upload window passed; uploads aborted, files removed')
        if record.get('record_version'):            # only a deposit the central watcher has seen
            self.publish_record(record['id'])
        return True

    def expire_decided(self) -> List[str]:
        """A site with a central watcher: a deposit that ended in ``needs_decision`` (refused, or stopped by the
        watcher) keeps its staged uploads ``decision_days`` after its completion (default twice
        ``max_open_days``); then they are removed, the deposit expires (the signed record tells the watcher, which
        removes its incoming copies and signs a final result) and the depositor gets an e-mail."""
        if not self.settings.central_watcher:
            return []
        days = self.settings.decision_days or 2 * self.settings.max_open_days
        cutoff = self.clock() - days * 86400
        ended: List[str] = []
        for record in self.store._rows("SELECT * FROM deposits WHERE state = 'uploaded' AND watch_state = "
                                       "'needs_decision' AND COALESCE(completed, created) < ?", (cutoff,)):
            if not self._expire_one(record, 'decision_expiry', ('uploaded',)):
                continue
            message = (f'this deposit waited {days:g} days in needs_decision; its uploads were removed and the '
                       f'deposit has ended. Deposit again once the cause is fixed.')
            self.store.update(record['id'], error=message)
            self.store.event(record['id'], 'decision_expiry', 'expired', message)
            try:
                import ingest_watch  # noqa: PLC0415
                fresh = self.store.get(record['id']) or record
                ingest_watch.notify_depositor({'notify': self.settings.notify}, fresh,
                                              {'state': 'expired', 'error': message, 'held_reasons': '[]'}, None,
                                              lambda line: self.store.event(record['id'], 'site', 'notify', line))
            except Exception:  # noqa: BLE001 - the e-mail is best effort
                pass
            ended.append(record['id'])
        return ended

    def expire_stale(self) -> List[str]:
        """Expire open deposits whose window passed (and interrupted completes), then sweep the staging prefix."""
        now = self.clock()
        expired: List[str] = []
        try:
            expired += self.expire_decided()
        except Exception:  # noqa: BLE001 - runs again on the next call
            pass
        for record in self.store._rows("SELECT * FROM deposits WHERE (state = 'uploading' AND expires <= ?) OR "
                                       "(state = 'completing' AND updated <= ?)", (now, now - STALE_COMPLETING_SECONDS)):
            if self._expire_one(record, 'expiry', (record['state'],)):
                expired.append(record['id'])
        try:
            self.sweep_staging()
        except Exception:  # noqa: BLE001 - the sweep runs again on the next call
            pass
        return expired

    def force_expire(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        """Admin: end an open deposit now (uploads aborted, its files removed)."""
        self._require_admin(identity, 'expire deposits')
        record = self._get_own(identity, deposit_id)
        if record['state'] != 'uploading' or not self._expire_one(record, identity.user, ('uploading',)):
            raise DepositError(409, 'not_open', f'the deposit is {record["state"]}, not uploading')
        return {'deposit_id': deposit_id, 'state': 'expired'}

    def group_info(self, identity: Identity, data_project: str, basename: Optional[str] = None) -> Dict[str, Any]:
        """Lookup for clients: does the group exist (its group_id), may it be created, and with ``basename``:
        is that dataset published and may the caller deposit a new version (its db id only then)."""
        if not isinstance(data_project, str) or not valid_segment(data_project) or len(data_project) > 64:
            raise DepositError(422, 'invalid_data_project', 'data_project: letters, digits, ._- only')
        self.lookup_limiter.check(identity.user)
        folder = self.store_root / data_project
        exists = folder.is_dir() and (folder / 'index.md').exists()
        answer: Dict[str, Any] = {'data_project': data_project, 'exists': exists}
        if exists:
            from metadata import parse_md_file  # noqa: PLC0415
            try:
                answer['group_id'] = parse_md_file(folder / 'index.md')[0].get('group_id')
            except Exception:  # noqa: BLE001
                answer['group_id'] = None
        else:
            try:
                self._check_group(data_project, identity)
                answer['may_create'] = True
            except DepositError as exc:
                answer['may_create'] = False
                answer['reason'] = exc.body['message']
        if basename is not None:
            if not isinstance(basename, str) or not BASENAME_RE.fullmatch(basename) or RESERVED_BASENAME_RE.fullmatch(basename):
                raise DepositError(422, 'invalid_name', f'{basename!r} is not a usable dataset name')
            existing = dataset_db_id(self.store_root, data_project, basename) if exists else None
            answer['dataset_exists'] = existing is not None
            inactive = existing is not None and (folder / f'{basename}.inactive').exists()
            if inactive and not identity.is_admin:
                answer['may_update'] = False          # a retired dataset is never offered, nor its id shown
            elif existing:
                if inactive:
                    answer['inactive'] = True
                may = identity.is_admin or self.settings.update_policy == 'any' \
                    or self.store.owner_of_dataset(existing) == identity.user
                answer['may_update'] = bool(may)
                if may:
                    answer['db_id'] = existing
        return answer

    def editable_md(self, identity: Identity, db_id: str, md_path: Optional[Path]) -> Dict[str, Any]:
        """The depositor-editable keys of a published dataset's ``.md`` (``GET /datasets/deposit/md/{db_id}``).

        For a client that diffs its local ``.md`` against the store before an update. Allowed under the update
        rule (owner, admin, or ``update_policy: any``); others get 403 whether the dataset exists or not, and an
        unknown dataset is 404 only for those who may update any dataset. Only
        ``ingest_md_fill.DEPOSITOR_EDITABLE_KEYS`` are returned (``keys``: the ones the ``.md`` sets), plus
        ``version`` (also on ``/info``); no curated or managed key, no caveats.
        """
        from ingest_md_fill import DEPOSITOR_EDITABLE_KEYS  # noqa: PLC0415
        from metadata import parse_md_file  # noqa: PLC0415

        if not isinstance(db_id, str) or not DB_ID_RE.fullmatch(db_id):
            raise DepositError(422, 'invalid', 'db_id is a dataset id like ABC/xyz')
        self.lookup_limiter.check(identity.user)
        may = identity.is_admin or self.settings.update_policy == 'any' \
            or self.store.owner_of_dataset(db_id) == identity.user
        if not may:
            raise DepositError(403, 'forbidden', f'only the owner of {db_id} or an admin may read its editable keys')
        if md_path is None or not Path(md_path).is_file():
            raise DepositError(404, 'unknown_dataset', f'{db_id} is not a published dataset')
        meta = parse_md_file(Path(md_path))[0] or {}
        return {'db_id': db_id, 'version': meta.get('version'),
                'keys': {key: meta[key] for key in DEPOSITOR_EDITABLE_KEYS if key in meta},
                'editable_keys': list(DEPOSITOR_EDITABLE_KEYS)}

    def set_owner(self, identity: Identity, db_id: str, user: str, note: str = '') -> Dict[str, Any]:
        """Admin: who may deposit new versions of a dataset (for datasets published before deposits existed)."""
        self._require_admin(identity, 'assign dataset owners')
        if not isinstance(db_id, str) or not DB_ID_RE.fullmatch(db_id):
            raise DepositError(422, 'invalid', 'db_id is a dataset id like ABC/xyz')
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise DepositError(422, 'invalid', 'user is a login name (letters, digits, ._@-)')
        if find_dataset_md(self.store_root, db_id) is None:
            raise DepositError(404, 'not_found', f'no dataset {db_id} in the store')
        self.store.set_owner(db_id, user, identity.user, clean_note(note))
        return {'db_id': db_id.upper(), 'user': user, 'assigned_by': identity.user,
                'caveat': 'written into the dataset .md by the watcher on its next poll'}

    def _published_files(self, data_project: str, basename: str,
                         deposit_files: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        """The catalog copies a metadata-only update reuses: the h5ad, and the sidecar when none is deposited.

        Returns (entries, sha256 of the published h5ad from the store ``.md``). The catalog h5ad must carry that
        sha256 (its ``x-amz-meta-sha256``, stamped by the catalog push) when it carries one."""
        from metadata import parse_md_file  # noqa: PLC0415

        try:
            meta = parse_md_file(self.store_root / data_project / f'{basename}.md')[0] or {}
        except Exception:  # noqa: BLE001
            meta = {}
        sha = str(meta.get('sha256') or '').lower()
        if not SHA256_RE.fullmatch(sha):
            raise DepositError(409, 'h5ad_unavailable', 'the published dataset has no recorded sha256: a metadata '
                                                        'update needs the h5ad deposited again')
        wanted = [('h5ad', f'{basename}{H5AD_SUFFIX}')]
        if not any(entry['kind'] == 'mus' for entry in deposit_files):
            wanted.append(('mus', f'{basename}{MUS_SUFFIX}'))
        entries = []
        for kind, name in wanted:
            source = f'{self.settings.catalog_prefix}{data_project}/{name}'
            try:
                head = _head(self.s3, self.settings.catalog, source)
            except Exception as exc:  # noqa: BLE001 - the ingestion keys may not read the catalog bucket
                raise DepositError(409, 'h5ad_unavailable', f'the published {name} cannot be read with this site\'s '
                                                            f'deposit keys ({_error_code(exc)}); deposit the h5ad '
                                                            f'again') from exc
            if head is None:
                raise DepositError(409, 'h5ad_unavailable', f'the published {name} is not on the object store; '
                                                            f'deposit it again')
            if kind == 'h5ad' and not head['metadata'].get('sha256'):
                # without the recorded hash the copy cannot be matched to the catalogue before a (large) copy;
                # the watcher stamps it at its next verified push (ant-sync pushes do not): ask an admin
                raise DepositError(409, 'h5ad_unavailable', f'the published {name} on the object store carries no '
                                                            f'recorded sha256, so it cannot be reused; deposit the '
                                                            f'h5ad again, or ask an admin to republish it')
            if kind == 'h5ad' and head['metadata']['sha256'] != sha:
                raise DepositError(409, 'h5ad_unavailable', f'the published {name} on the object store is not the '
                                                            f'version in the catalogue; ask an admin')
            entries.append({'name': name, 'kind': kind, 'key': self._key(data_project, kind, name), 'source': source,
                            'etag': head['etag'], 'size': head['size'],
                            'sha256': sha if kind == 'h5ad' else head['metadata'].get('sha256')})
        return entries, sha

    def _check_keys(self, identity: Identity, deposit_files: Sequence[Dict[str, Any]]) -> None:
        """A non-admin may not replace a waiting object of another user's deposit or of a lab member's upload.

        A site with a central watcher never writes incoming (the watcher copies; section 24.9), so there is
        nothing of others it could replace; the watcher checks the incoming keys against its claims."""
        if identity.is_admin or self.settings.central_watcher:
            return
        for entry in deposit_files:
            head = _head(self.s3, self.bucket, entry['key'])
            if head is None:
                continue
            owner_deposit = self.store.get(head['metadata'].get('deposit', '')) if head['metadata'].get('deposit') else None
            if owner_deposit is not None and owner_deposit['user'] == identity.user:
                continue
            hint = ' Name yours <basename>.index.md (kind index_md) instead.' if entry['name'] == 'index.md' else ''
            raise DepositError(409, 'key_taken', f'{entry["name"]} already waits in incoming from another upload; '
                                                 f'ask an admin.{hint}')

    def _existing_dataset(self, identity: Identity, data_project: str, basename: str,
                          update_of: Optional[str]) -> Optional[str]:
        """The db id of the published dataset this deposit replaces (None for a new one), after the update rules.

        Callers who may not update the dataset get one message whether or not they named it, and never its id.
        """
        existing = dataset_db_id(self.store_root, data_project, basename)
        if existing is None and (self.store_root / data_project / f'{basename}{H5AD_SUFFIX}').exists():
            existing = f'{data_project}/{basename}'
        if existing is None:
            if update_of:
                raise DepositError(422, 'update_of_unknown', f'update_of={update_of}, but {data_project}/{basename} '
                                                             f'is not a published dataset')
            return None
        may_update = identity.is_admin or self.settings.update_policy == 'any' \
            or self.store.owner_of_dataset(existing) == identity.user
        if not may_update:
            raise DepositError(409, 'name_taken', f'{data_project}/{basename} cannot be deposited by you: choose '
                                                  f'another basename (if it is a dataset you published, ask an '
                                                  f'admin to record you as its owner)')
        if not update_of:
            raise DepositError(409, 'update_of_required', f'{data_project}/{basename} is the published dataset '
                                                          f'{existing}; a new version needs "update_of": '
                                                          f'"{existing}"', db_id=existing)
        if update_of.upper() != existing.upper():
            raise DepositError(409, 'update_of_mismatch', f'{data_project}/{basename} is {existing}, not '
                                                          f'{update_of}', db_id=existing)
        return existing

    def create(self, identity: Identity, body: Dict[str, Any]) -> Dict[str, Any]:
        """Record a deposit and hand out the upload instructions (``POST /datasets/deposit``)."""
        self._require_active()
        if not isinstance(body, dict):
            raise DepositError(422, 'invalid', 'the body is a JSON object')
        data_project = body.get('data_project')
        basename = body.get('basename')
        group_exists = self._check_group(data_project, identity)
        if not isinstance(basename, str) or not BASENAME_RE.fullmatch(basename) or basename.endswith(H5AD_SUFFIX):
            raise DepositError(422, 'invalid_name', 'basename: the h5ad name without .h5ad; letters, digits, ._-, '
                                                    'at most 50 characters')
        if RESERVED_BASENAME_RE.fullmatch(basename):
            raise DepositError(422, 'invalid_name', f'{basename!r} cannot be a dataset name: index.md and '
                                                    f'<x>.index.md are group descriptions')
        update_of = body.get('update_of')
        if update_of is not None and (not isinstance(update_of, str) or not DB_ID_RE.fullmatch(update_of)):
            raise DepositError(422, 'invalid', 'update_of is a dataset id like ABC/xyz')
        files_in = body.get('files')
        de_only = isinstance(files_in, list) and bool(files_in) and all(
            isinstance(entry, dict) and entry.get('kind') == 'de' for entry in files_in)
        if de_only and not update_of:
            raise DepositError(422, 'invalid', 'DE tables alone are added to a published dataset: give '
                                               '"update_of": "<db_id>" (or deposit them with the h5ad)')
        metadata_candidate = bool(update_of) and not de_only and isinstance(files_in, list) and not any(
            isinstance(entry, dict) and entry.get('kind') == 'h5ad' for entry in files_in)
        deposit_files = self._validate_files(data_project, basename, files_in, de_only=de_only,
                                             mus_optional=metadata_candidate)
        total = sum(entry['size'] for entry in deposit_files)
        if total > self.settings.max_gb * 1024 ** 3:
            raise DepositError(413, 'too_large', f'{total / 1024 ** 3:.1f} GB is over the {self.settings.max_gb:g} GB '
                                                 f'limit of one deposit')
        existing_db_id = self._existing_dataset(identity, data_project, basename, update_of) if group_exists else None
        if not group_exists and update_of:
            raise DepositError(422, 'update_of_unknown', f'update_of={update_of}, but {data_project} is not a group')
        if de_only and not existing_db_id:
            raise DepositError(422, 'update_of_unknown', f'{data_project}/{basename} is not a published dataset')
        self.limiter.check(identity.user)              # before any call to the object store
        self.expire_stale()
        now = self.clock()

        # the h5ad: deposited now, or a corrected sidecar for an h5ad of yours still in incoming
        h5ad_key = self._key(data_project, 'h5ad', f'{basename}{H5AD_SUFFIX}')
        continues = h5ad_etag = h5ad_sha = None
        reuse: List[Dict[str, Any]] = []
        h5ad_entry = next((entry for entry in deposit_files if entry['kind'] == 'h5ad'), None)
        if de_only:
            pass
        elif h5ad_entry is None:
            # a site with a central watcher cannot continue an h5ad waiting in incoming (it neither reads nor
            # writes incoming, section 24.9): without an h5ad it is a metadata-only update or refused
            head = None if self.settings.central_watcher else _head(self.s3, self.bucket, h5ad_key)
            previous = self.store.get(head['metadata'].get('deposit', '')) if head and head['metadata'].get('deposit') else None
            previous_row = next((row for row in self.store.files(previous['id']) if row['kind'] == 'h5ad'
                                 and row['etag'] == head['etag']), None) if previous else None
            waiting = head is not None and previous is not None and previous_row is not None \
                and (previous['user'] == identity.user or identity.is_admin)
            if waiting and self._rejected(previous):
                raise DepositError(409, 'rejected', f'the h5ad at {data_project}/{basename}{H5AD_SUFFIX} belongs to '
                                                    f'the deposit {previous["id"]}, which an admin rejected; a '
                                                    f'rejected deposit is final and is not continued. Deposit the '
                                                    f'h5ad again with its sidecar (or ask an admin to remove the '
                                                    f'leftover object)')
            if waiting:
                if not any(entry['kind'] == 'mus' for entry in deposit_files):
                    raise DepositError(422, 'invalid', f'a correction of the waiting h5ad carries the sidecar '
                                                       f'{basename}{MUS_SUFFIX}')
                continues, h5ad_etag, h5ad_sha = previous['id'], head['etag'], previous_row['sha256']
            elif existing_db_id and head is None:
                # a metadata-only update: the published h5ad (and sidecar, when none is deposited) is reused
                reuse, h5ad_sha = self._published_files(data_project, basename, deposit_files)
            else:
                raise DepositError(422, 'h5ad_required', f'no h5ad of yours waits at {data_project}/{basename}'
                                                         f'{H5AD_SUFFIX}: deposit the h5ad with its sidecar')
        else:
            h5ad_sha = h5ad_entry['sha256']
        self._check_keys(identity, deposit_files)

        # a correction keeps the kind of the deposit it continues (a metadata update stays one)
        deposit_kind = 'de_only' if de_only else ('metadata_update' if reuse else 'dataset')
        if continues:
            deposit_kind = (self.store.get(continues) or {}).get('deposit_kind') or deposit_kind
        deposit_id = new_deposit_id()
        expires = now + self._ttl()
        rows: List[Dict[str, Any]] = []
        for entry in deposit_files:
            multipart = entry['size'] > min(self.settings.single_put_max_mb * MiB, SINGLE_PUT_HARD_MAX)
            part_size = choose_part_size(entry['size']) if multipart else None
            rows.append({'deposit_id': deposit_id, 'name': entry['name'], 'kind': entry['kind'], 'key': entry['key'],
                         'staging_key': self._staging_key(deposit_id, entry['key']), 'size': entry['size'],
                         'sha256': entry['sha256'], 'state': 'pending', 'etag': None, 'completed': None,
                         'method': 'multipart' if multipart else 'put', 'upload_id': None, 'part_size': part_size,
                         'part_count': len(part_sizes(entry['size'], part_size)) if multipart else None})
        for entry in reuse:                              # published files copied in at complete
            rows.append({'deposit_id': deposit_id, 'name': entry['name'], 'kind': entry['kind'], 'key': entry['key'],
                         'staging_key': entry['source'], 'staging_etag': entry['etag'], 'size': entry['size'],
                         'sha256': entry['sha256'], 'state': 'staged', 'etag': None, 'completed': None,
                         'method': 'reuse', 'upload_id': None, 'part_size': None, 'part_count': None})
        mus_key = self._key(data_project, 'mus', f'{basename}{MUS_SUFFIX}')
        record = {'id': deposit_id, 'user': identity.user, 'name': identity.name, 'email': identity.email,
                  'is_admin': int(identity.is_admin), 'data_project': data_project, 'basename': basename,
                  'update_of': update_of, 'state': 'uploading', 'created': now, 'updated': now, 'expires': expires,
                  'total_bytes': total, 'watch_key': None if de_only else h5ad_key, 'h5ad_etag': h5ad_etag,
                  'mus_key': None if de_only else mus_key, 'continues': continues,
                  'h5ad_sha256': None if de_only else h5ad_sha, 'refreshes': 0,
                  'deposit_kind': deposit_kind}
        self.store.reserve(record, rows, now, self.settings)
        try:
            for row in rows:
                if row['method'] == 'multipart':
                    # x-amz-meta-sha256 is the depositor's attestation only: the pipeline recomputes the sha256
                    response = self.s3.create_multipart_upload(
                        Bucket=self.bucket, Key=row['staging_key'], Metadata=self._metadata(deposit_id, row['sha256']))
                    row['upload_id'] = response['UploadId']
                    self.store.update_file(deposit_id, row['name'], upload_id=row['upload_id'])
        except Exception as exc:  # noqa: BLE001 - S3 refused: abort what was started, release the deposit
            self._abort_and_delete(self.store.get(deposit_id), actor='create')
            self.store.cas(deposit_id, ('uploading',), 'expired')
            self.store.release_keys(deposit_id)
            raise DepositError(502, 's3_error', f'the object store refused to start the upload ({_error_code(exc)}); '
                                                f'try again later')
        instructions = [self._instructions(deposit_id, row, 1, self.settings.parts_per_batch, expires) for row in rows]
        self.store.event(deposit_id, identity.user, 'created',
                         f'{len(rows)} file(s), {total} bytes' + (f', update of {update_of}' if update_of else '')
                         + (f', continues {continues}' if continues else ''))
        warnings = []
        if de_only:
            pass
        elif not any(entry['kind'] == 'seed_md' for entry in deposit_files) and not existing_db_id:
            warnings.append(f'no seed {basename}.md: the route writes a minimal .md, which usually fails '
                            f'meta.required_keys (no description)')
        if not group_exists and not any(entry['kind'] == 'index_md' for entry in deposit_files):
            warnings.append('a new group without index.md: the route builds one from the sidecar keys doi, year, '
                            'paper_title and authors, or the check meta.index_md fails')
        return {'deposit_id': deposit_id, 'state': 'uploading', 'data_project': data_project, 'basename': basename,
                'deposit_kind': deposit_kind,
                'update_of': update_of, 'continues': continues, 'expires': _iso(expires),
                'url_ttl_seconds': self._ttl(), 'total_bytes': total, 'files': instructions, 'warnings': warnings,
                'next': {'urls': f'/datasets/deposit/{deposit_id}/urls',
                         'complete': f'/datasets/deposit/{deposit_id}/complete',
                         'status': f'/datasets/deposit/{deposit_id}'}}

    def _instructions(self, deposit_id: str, row: Dict[str, Any], first: int, last: int,
                      expires: float) -> Dict[str, Any]:
        """Presigned URL(s) for one file (a single PUT, or UploadPart URLs for parts ``first..last``).

        Every URL points at the deposit's staging key and lives no longer than the deposit's window.
        """
        ttl = max(1, min(self._ttl(), int(expires - self.clock())))
        target = row['staging_key']
        base = {'name': row['name'], 'kind': row['kind'], 'key': row['key'], 'upload_key': target,
                'size': row['size'], 'sha256': row['sha256'], 'method': row['method']}
        if row['method'] == 'reuse':
            return {**base, 'upload_key': None, 'note': 'nothing to upload: the published file is reused'}
        if row['method'] == 'put':
            url = self.s3.generate_presigned_url(
                'put_object', ExpiresIn=ttl,
                Params={'Bucket': self.bucket, 'Key': target, 'ContentLength': row['size'],
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
                Params={'Bucket': self.bucket, 'Key': target, 'UploadId': row['upload_id'],
                        'PartNumber': number, 'ContentLength': sizes[number - 1]})
            parts.append({'part_number': number, 'size': sizes[number - 1], 'url': url})
        return {**base, 'upload_id': row['upload_id'], 'part_size': row['part_size'], 'part_count': len(sizes),
                'parts': parts, 'parts_more': last < len(sizes)}

    def _uploaded_parts(self, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        marker = 0
        while True:
            response = self.s3.list_parts(Bucket=self.bucket, Key=row['staging_key'], UploadId=row['upload_id'],
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

    def _require_uploading(self, record: Dict[str, Any]) -> Dict[str, Any]:
        if record['state'] == 'uploading' and record['expires'] <= self.clock():
            self.expire_stale()
            record = self.store.get(record['id']) or record
        if record['state'] in ('expired', 'expiring'):
            raise DepositError(410, 'expired', 'the upload window of this deposit passed; start a new deposit')
        if record['state'] != 'uploading':
            raise DepositError(409, 'not_uploading', f'the deposit is {record["state"]}')
        return record

    def refresh_urls(self, identity: Identity, deposit_id: str, body: Any) -> Dict[str, Any]:
        """Fresh URLs for one file, and the parts S3 already has (resume) (``POST .../urls``)."""
        self._require_active()
        record = self._get_own(identity, deposit_id)
        if not isinstance(body, dict) or not isinstance(body.get('name'), str):
            raise DepositError(422, 'invalid', 'the body is {"name": <file name>, "first_part"?, "last_part"?}')
        try:
            first = int(body.get('first_part') or 1)
            last = int(body.get('last_part') or first + self.settings.parts_per_batch - 1)
        except (TypeError, ValueError):
            raise DepositError(422, 'invalid', 'first_part and last_part are whole numbers')
        record = self._require_uploading(record)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        name = body['name']
        if name not in rows:
            raise DepositError(404, 'not_found', f'{name!r} is not a file of this deposit')
        row = rows[name]
        if row['state'] == 'complete':
            raise DepositError(409, 'already_complete', f'{name} is already complete')
        if row['method'] == 'reuse':
            raise DepositError(409, 'nothing_to_upload', f'{name} is the published file, reused; to change the '
                                                         f'sidecar, withdraw and deposit again with a sidecar file')
        redeclare = 'size' in body or 'sha256' in body
        if redeclare:
            if row['kind'] != 'mus':
                raise DepositError(422, 'invalid', 'only the sidecar (kind mus) can be declared again; withdraw and '
                                                   'deposit again for other files')
            size, sha = body.get('size'), str(body.get('sha256') or '').lower()
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= TEXT_MAX_BYTES \
                    or not SHA256_RE.fullmatch(sha):
                raise DepositError(422, 'invalid', f'size (0..{TEXT_MAX_BYTES} bytes) and sha256 (64 hex) of the '
                                                   f'corrected sidecar')
        if int(record.get('refreshes') or 0) >= self.settings.max_refreshes:
            raise DepositError(429, 'too_many_refreshes', f'this deposit asked for fresh URLs '
                                                          f'{self.settings.max_refreshes} times; start a new deposit')
        if row['method'] == 'multipart' and (first < 1 or last < first or last - first + 1 > MAX_PARTS_PER_CALL
                                             or first > int(row['part_count'])):
            raise DepositError(422, 'invalid', f'parts {first}..{last}: 1 <= first <= last <= {row["part_count"]}, '
                                               f'at most {MAX_PARTS_PER_CALL} per call')
        self.limiter.check(identity.user)
        now = self.clock()
        expires = max(float(record['expires']),
                      min(now + self._ttl(), float(record['created']) + self.settings.max_open_days * 86400))
        if expires <= now:
            raise DepositError(410, 'expired', 'this deposit has been open too long; start a new deposit')
        self.store.update(deposit_id, expires=expires, refreshes=int(record.get('refreshes') or 0) + 1)
        if redeclare:
            if not self.store.redeclare(deposit_id, name, size, sha):
                raise DepositError(409, 'not_uploading', 'the deposit is no longer uploading (completing or ended)')
            # the old staging object (if any) goes; the new URL signs the new size and sha256
            self._delete_staging(deposit_id, row)
            self.store.event(deposit_id, identity.user, 'redeclared', f'{name}: {size} bytes')
            row = {**row, 'size': size, 'sha256': sha, 'method': 'put', 'upload_id': None}
        if row['method'] == 'put':
            return {**self._instructions(deposit_id, row, 1, 1, expires), 'expires': _iso(expires)}
        try:
            done = self._uploaded_parts(row)
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'listing the uploaded parts failed ({_error_code(exc)})')
        return {**self._instructions(deposit_id, row, first, last, expires), 'parts_done': done,
                'expires': _iso(expires)}

    # complete --------------------------------------------------------------

    def _finish_upload(self, deposit_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Complete one file in staging and verify it; ``{'name', 'ok', ...}`` (no raise for missing parts)."""
        if row['state'] == 'complete':
            return {'name': row['name'], 'ok': True}
        if row['method'] == 'reuse':                   # the published copy, recorded at create: still the same?
            head = _head(self.s3, self.settings.catalog, row['staging_key'])
            if head is None or head['etag'] != row['staging_etag']:
                return {'name': row['name'], 'ok': False,
                        'problems': ['the published copy changed since this deposit started; deposit again']}
            return {'name': row['name'], 'ok': True}
        if row['method'] == 'multipart' and row['state'] != 'staged':
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
                Bucket=self.bucket, Key=row['staging_key'], UploadId=row['upload_id'],
                MultipartUpload={'Parts': [{'PartNumber': number, 'ETag': f'"{have[number]["etag"]}"'}
                                           for number in range(1, len(sizes) + 1)]})
            self.store.update_file(deposit_id, row['name'], state='staged')
        head = _head(self.s3, self.bucket, row['staging_key'])
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
            self._delete_staging(deposit_id, row)                                # the deposit's own staging key
            self.store.update_file(deposit_id, row['name'], state='pending')
            return {'name': row['name'], 'ok': False, 'problems': problems}
        # the ETag checked here is the only version that may be read back and copied (N1: a re-PUT of a staging
        # key during complete must not slip through unchecked)
        self.store.update_file(deposit_id, row['name'], state='staged', staging_etag=head['etag'])
        return {'name': row['name'], 'ok': True}

    def _copy_in(self, deposit_id: str, row: Dict[str, Any]) -> str:
        """Copy a verified staging object to its watched key (multipart copy above 5 GB); returns the new ETag.

        The copy is conditional on the staging ETag recorded at verification (``CopySourceIfMatch``, also
        forwarded to UploadPartCopy by the managed copy). Because an object store may ignore the condition,
        a single-PUT file (the only kind a URL can overwrite; MD5 ETags) must also arrive with exactly that
        ETag. A mismatch raises :class:`StagingChanged`.
        """
        staged = row.get('staging_etag')
        if not staged:
            raise StagingChanged(f'{row["name"]} was not verified')
        source = {'Bucket': self.settings.catalog if row.get('method') == 'reuse' else self.bucket,
                  'Key': row['staging_key']}
        metadata = self._metadata(deposit_id, row['sha256'])
        condition = f'"{staged}"'
        try:
            if row['size'] <= SINGLE_PUT_HARD_MAX:
                self.s3.copy_object(Bucket=self.bucket, Key=row['key'], CopySource=source, Metadata=metadata,
                                    MetadataDirective='REPLACE', CopySourceIfMatch=condition)
            else:                                         # managed copy: UploadPartCopy in parts
                beat = {'last': time.time()}

                def heartbeat(_bytes: int) -> None:       # keeps the lease of a long copy (resume_interrupted)
                    if time.time() - beat['last'] > 30:
                        beat['last'] = time.time()
                        self.store.update(deposit_id, updated=self.clock())
                self.s3.copy(source, self.bucket, row['key'],
                             ExtraArgs={'Metadata': metadata, 'MetadataDirective': 'REPLACE',
                                        'CopySourceIfMatch': condition}, Callback=heartbeat)
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in ('PreconditionFailed', '412'):
                raise StagingChanged(f'{row["name"]} changed in staging during complete') from exc
            raise
        head = _head(self.s3, self.bucket, row['key'])
        if head is None or head['size'] != row['size'] or head['metadata'].get('deposit') != deposit_id:
            raise RuntimeError(f'the copy of {row["name"]} into incoming could not be verified')
        if row['method'] == 'put' and head['etag'] != staged:
            delete_consumed(self.s3, self.bucket, self.settings.prefix, {row['key']: head['etag']})
            raise StagingChanged(f'{row["name"]} changed in staging during complete')
        return head['etag']

    def _read_staged(self, row: Dict[str, Any]) -> str:
        """A verified staging text file, read with IfMatch on its verified ETag (and its MD5 checked, as a store may
        ignore IfMatch); raises StagingChanged when it is no longer the verified version."""
        try:
            response = self.s3.get_object(Bucket=self.settings.catalog if row.get('method') == 'reuse' else self.bucket,
                                          Key=row['staging_key'], IfMatch=f'"{row["staging_etag"]}"')
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in ('PreconditionFailed', '412'):
                raise StagingChanged(f'{row["name"]} changed in staging during complete') from exc
            raise
        raw = response['Body'].read(TEXT_MAX_BYTES + 1)
        if row['method'] == 'put' and hashlib.md5(raw).hexdigest() != row['staging_etag']:
            raise StagingChanged(f'{row["name"]} read back is not the verified one')
        return raw.decode('utf-8', errors='replace')

    def _check_de_sidecars(self, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]],
                           naming: bool = True) -> List[str]:
        """Errors of the DE tables of a deposit: every table's .mus must have what its format needs (term,
        contrast, ...) and, for a DE-only deposit (``naming``), name the dataset it is added to."""
        from ingest_pipeline import de_sidecar_names, de_sidecar_problems  # noqa: PLC0415

        errors = []
        db_id = str(record.get('update_of') or '')
        file_id = db_id.split('/')[-1] if db_id else ''
        for name, row in rows.items():
            if row['kind'] != 'de' or not name.endswith('.mus'):
                continue
            meta = parse_sidecar_text(self._read_staged(row))
            errors += [f'{name}: {problem}' for problem in de_sidecar_problems(meta)]
            if naming and not de_sidecar_names(meta, file_id, record['basename'], db_id):
                errors.append(f'{name} names neither de_dataset_id={file_id} (or {db_id}) nor '
                              f'de_dataset_h5ad={record["basename"]}{H5AD_SUFFIX}')
        return errors

    def _check_sidecar(self, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
        """(errors, warnings) of the deposited sidecar against the deposit (read from staging, at most 1 MB)."""
        import mus  # noqa: PLC0415

        mus_row = next(row for row in rows.values() if row['kind'] == 'mus')
        text = self._read_staged(mus_row)
        sidecar = parse_sidecar_text(text)
        errors: List[str] = []
        warnings: List[str] = []
        expected = record.get('h5ad_sha256')            # the deposited h5ad, or the waiting one a continuation names
        declared = sidecar.get('sha256', '').lower()
        if not declared:
            errors.append('the sidecar has no sha256= line')
        elif expected and declared != expected:
            errors.append(f'the sidecar says sha256={declared[:12]}..., the h5ad is {expected[:12]}...')
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
            if not sidecar.get('license'):
                warnings.append('no license= in the sidecar: the deposit will be held (licence_unknown)')
            if not sidecar.get('data_use'):
                warnings.append('no data_use= in the sidecar: the check access.controlled will warn (say open or '
                                'controlled; everyone with access to this server sees every dataset)')
            elif sidecar['data_use'].strip().lower() == 'controlled':
                warnings.append("data_use=controlled: everyone with access to this server can see this dataset; "
                                "make sure the server's users are covered by the data use agreement")
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
        """Verify every upload in staging, then copy the files into the watched prefix and record their ETags.

        The deposit is ``completing`` meanwhile (withdraw and expiry wait); on any refusal it goes back to
        ``uploading`` and nothing stays in the watched prefix. The checks always run in the request. The copies
        run in the request too, unless the deposit is larger than ``async_complete_gb``: then the answer is
        ``202 {state: completing}`` and a background worker copies (progress in the status, errors in
        ``complete_error``; a restart resumes it, see :meth:`resume_interrupted`).
        """
        self._require_active()
        record = self._require_uploading(self._get_own(identity, deposit_id))
        if not self.store.cas(deposit_id, ('uploading',), 'completing', updated=self.clock(), complete_error=None,
                              progress=None, worker=self.worker_id):
            raise DepositError(409, 'busy', 'the deposit is being completed, withdrawn or expired right now')
        try:
            rows, warnings = self._verify_for_complete(record, deposit_id)
        except DepositError as error:
            self._undo_complete(deposit_id, {}, error)
            raise
        if self.settings.central_watcher:
            return self._finish_staged(deposit_id, rows, warnings, identity.user)
        copy_bytes = sum(int(row['size'] or 0) for row in rows.values())     # uploads and reused files
        if copy_bytes > self.settings.async_complete_gb * 1024 ** 3:
            self.store.event(deposit_id, identity.user, 'completing', 'large deposit: copying in the background')
            self._progress(deposit_id, rows, 0, 0)
            self.background(lambda: self._copy_phase_logged(deposit_id, record, rows, warnings, identity.user))
            return {'_http_status': 202, 'deposit_id': deposit_id, 'state': 'completing',
                    'progress': self._progress_view(self.store.get(deposit_id)), 'warnings': warnings,
                    'next': f'GET /datasets/deposit/{deposit_id} shows the copy progress; then it is uploaded'}
        return self._copy_phase(deposit_id, record, rows, warnings, identity.user)

    def _verify_for_complete(self, record: Dict[str, Any], deposit_id: str) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """The checks of complete (parts, sizes, metadata, sidecars, the waiting h5ad, key ownership)."""
        changed = self._staging_changed_error()
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        try:
            results = [self._finish_upload(deposit_id, row) for row in rows.values()]
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'completing the upload failed ({_error_code(exc)}); try again')
        incomplete = [entry for entry in results if not entry['ok']]
        if incomplete:
            raise DepositError(409, 'incomplete', 'not every file is uploaded completely', files=incomplete)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        warnings: List[str] = []
        try:
            if record.get('deposit_kind') == 'de_only':
                errors = self._check_de_sidecars(record, rows)
                if errors:
                    raise DepositError(422, 'de_mismatch', '; '.join(errors) + '. Withdraw and deposit the tables '
                                                                               'again with corrected sidecars.')
            else:
                de_errors = self._check_de_sidecars(record, rows, naming=False)
                if de_errors:
                    raise DepositError(422, 'de_mismatch', '; '.join(de_errors) + '. Withdraw and deposit again '
                                                                                  'with corrected DE sidecars.')
                errors, warnings = self._check_sidecar(record, rows)
                mus_row = next(row for row in rows.values() if row['kind'] == 'mus')
                if errors and mus_row['method'] == 'reuse':
                    # the published sidecar is not touched and cannot be declared again in this deposit
                    raise DepositError(422, 'sidecar_mismatch', 'the published sidecar does not match the '
                                       'catalogue (' + '; '.join(errors) + '): deposit a sidecar file (withdraw '
                                       'and deposit again, with a sidecar)')
                if errors:
                    self._delete_staging(deposit_id, mus_row)
                    self.store.update_file(deposit_id, mus_row['name'], state='pending', staging_etag=None)
                    raise DepositError(422, 'sidecar_mismatch', '; '.join(errors) + '. Declare the corrected '
                                       'sidecar with POST .../urls {"name", "size", "sha256"} (a new URL), upload it '
                                       'and complete again.')
        except StagingChanged:
            raise changed
        except DepositError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'reading a sidecar back failed ({_error_code(exc)})')
        if record.get('continues'):            # a sidecar for an h5ad still in incoming: it must not have changed
            head = _head(self.s3, self.bucket, record['watch_key'])
            if head is None or head['etag'] != record['h5ad_etag']:
                raise DepositError(409, 'h5ad_changed', 'the h5ad this sidecar belongs to changed or is gone; '
                                                        'withdraw this deposit and deposit the h5ad again')
        self._check_keys(Identity(user=record['user'], is_admin=bool(record['is_admin'])), list(rows.values()))
        return rows, warnings

    @staticmethod
    def _staging_changed_error() -> DepositError:
        return DepositError(409, 'staging_changed', 'a file was uploaded again while the deposit was being '
                                                    'completed; nothing was published. Complete again once your '
                                                    'uploads are finished.')

    def _undo_complete(self, deposit_id: str, copied: Dict[str, str], error: DepositError) -> None:
        """Remove the copies of a refused complete and return the deposit to ``uploading``."""
        for key, etag in copied.items():           # nothing of a refused complete stays where the watcher looks
            delete_consumed(self.s3, self.bucket, self.settings.prefix, {key: etag})
        if error.body.get('status') == 'staging_changed':      # every single-PUT file is checked again next time
            for row in self.store.files(deposit_id):
                if row['method'] == 'put' and row['state'] == 'staged':
                    self.store.update_file(deposit_id, row['name'], state='pending', staging_etag=None)
        self.store.cas(deposit_id, ('completing',), 'uploading', complete_error=json.dumps(error.body))

    def _progress(self, deposit_id: str, rows: Dict[str, Dict[str, Any]], files_copied: int, bytes_copied: int) -> None:
        self.store.update(deposit_id, updated=self.clock(), progress=json.dumps({
            'files_total': len(rows), 'files_copied': files_copied,
            'bytes_total': sum(int(row['size']) for row in rows.values()), 'bytes_copied': bytes_copied}))

    @staticmethod
    def _progress_view(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not record or not record.get('progress'):
            return None
        try:
            return json.loads(record['progress'])
        except ValueError:
            return None

    def _copy_phase_logged(self, deposit_id: str, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]],
                           warnings: List[str], user: str) -> None:
        try:
            self._copy_phase(deposit_id, record, rows, warnings, user)
        except DepositError:
            pass                                       # recorded in complete_error by _undo_complete
        except Exception as exc:  # noqa: BLE001 - a background worker never raises
            error = DepositError(502, 's3_error', f'the background copy failed ({_error_code(exc)})')
            self._undo_complete(deposit_id, {}, error)

    def _copy_phase(self, deposit_id: str, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]],
                    warnings: List[str], user: str) -> Dict[str, Any]:
        """Copy the verified staging files into the watched prefix; finish or undo."""
        copied: Dict[str, str] = {}
        try:
            copied_bytes = 0
            for row in rows.values():
                try:
                    copied[row['key']] = self._copy_in(deposit_id, row)
                except StagingChanged:
                    raise self._staging_changed_error()
                except Exception as exc:  # noqa: BLE001
                    raise DepositError(502, 's3_error', f'moving the files into incoming failed '
                                                        f'({_error_code(exc)}); try again')
                copied_bytes += int(row['size'])
                self._progress(deposit_id, rows, len(copied), copied_bytes)   # also: alive, no stale recovery
        except DepositError as error:
            self._undo_complete(deposit_id, copied, error)
            raise
        now = self.clock()
        h5ad_row = next((row for row in rows.values() if row['kind'] == 'h5ad'), None)
        mus_row = next((row for row in rows.values() if row['kind'] == 'mus'), None)
        finished = self.store.finish_complete(
            deposit_id, {row['name']: copied[row['key']] for row in rows.values()}, now, completed=now,
            h5ad_etag=copied[h5ad_row['key']] if h5ad_row else record['h5ad_etag'],
            mus_etag=copied[mus_row['key']] if mus_row else None, progress=None)
        if not finished:
            # expired, withdrawn or taken over meanwhile: not ours to publish
            for key, etag in copied.items():
                delete_consumed(self.s3, self.bucket, self.settings.prefix, {key: etag})
            raise DepositError(409, 'not_completed', 'the deposit was ended while it was being completed; '
                                                     'start a new deposit')
        for row in rows.values():                      # only now: the deposit is uploaded
            self._delete_staging(deposit_id, row)
        self.store.event(deposit_id, user, 'completed', f'{len(rows)} file(s)')
        self.publish_record(deposit_id)
        return {'deposit_id': deposit_id, 'state': 'uploaded',
                'files': [{'name': row['name'], 'key': row['key'], 'size': row['size'], 'sha256': row['sha256'],
                           'etag': copied[row['key']]} for row in rows.values()],
                'warnings': warnings,
                'next': 'The watcher checks the deposit within a few minutes after it settles; follow it with '
                        f'GET /datasets/deposit/{deposit_id}.'}

    def _finish_staged(self, deposit_id: str, rows: Dict[str, Dict[str, Any]], warnings: List[str],
                       user: str) -> Dict[str, Any]:
        """A site with a central watcher (section 24.9): the verified uploads stay in the deposit's staging
        folder, the signed record names them with their staging ETags, and the watcher copies them into incoming
        after it has verified the record and each staging object. Nothing is written to incoming here."""
        now = self.clock()
        finished = self.store.finish_complete(deposit_id, {row['name']: None for row in rows.values()}, now,
                                              completed=now, h5ad_etag=None, mus_etag=None, progress=None)
        if not finished:
            raise DepositError(409, 'not_completed', 'the deposit was ended while it was being completed; '
                                                     'start a new deposit')
        self.store.event(deposit_id, user, 'completed', f'{len(rows)} file(s), staged for the central watcher')
        self.publish_record(deposit_id)
        return {'deposit_id': deposit_id, 'state': 'uploaded',
                'files': [{'name': row['name'], 'key': row['key'], 'size': row['size'], 'sha256': row['sha256'],
                           'etag': None} for row in rows.values()],
                'warnings': warnings,
                'next': 'The central watcher verifies the deposit and copies it into incoming within a few minutes; '
                        f'follow it with GET /datasets/deposit/{deposit_id}.'}

    def resume_interrupted(self) -> List[str]:
        """After a restart: complete again the deposits a previous process left ``completing``.

        Their staging files are still there (they are deleted only after every copy succeeded); the copies are
        verified and made again in the background. Returns the deposit ids resumed.
        """
        resumed = []
        lease = self.settings.complete_lease_minutes * 60
        for record in self.store._rows("SELECT * FROM deposits WHERE state = 'completing' AND "
                                       "COALESCE(worker, '') != ? AND updated <= ?",
                                       (self.worker_id, self.clock() - lease)):
            if not self.store.cas(record['id'], ('completing',), 'uploading', worker=None):
                continue
            self.store.event(record['id'], 'restart', 'resumed', 'the server restarted during complete')
            identity = Identity(user=record['user'], name=record.get('name') or '', email=record.get('email') or '',
                                is_admin=bool(record['is_admin']))
            fresh = self.store.get(record['id'])

            def run(fresh=fresh, identity=identity):
                try:
                    if not self.store.cas(fresh['id'], ('uploading',), 'completing', updated=self.clock(),
                                          worker=self.worker_id):
                        return
                    rows, warnings = self._verify_for_complete(fresh, fresh['id'])
                except DepositError as error:
                    self._undo_complete(fresh['id'], {}, error)
                    return
                self._copy_phase_logged(fresh['id'], fresh, rows, warnings, identity.user)
            self.background(run)
            resumed.append(record['id'])
        return resumed

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

    def _replaced(self, record: Dict[str, Any], row: Dict[str, Any]) -> bool:
        """Did a newer upload replace this deposit's objects? A newer deposit of the same key, or a watcher row
        written after this deposit completed. An older row (the previous version of an update, not polled yet)
        is not a replacement: the deposit then reads ``uploaded``."""
        newer = self.store._rows("SELECT id FROM deposits WHERE watch_key = ? AND id != ? AND completed > ? AND "
                                 "state IN ('uploaded', 'completing')",
                                 (record.get('watch_key'), record['id'], float(record.get('completed') or 0)))
        if newer:
            return True
        return float(row.get('updated') or 0) > float(record.get('completed') or 0)

    def _view(self, record: Dict[str, Any], detail: bool, identity: Identity) -> Dict[str, Any]:
        row = read_watch_row(self.state_dir, record.get('watch_key')) if record['state'] == 'uploaded' else None
        matches = watch_row_matches(record, row)
        state = public_state(record, row if matches else None)
        if record['state'] == 'uploaded' and row and not matches and not record.get('watch_state') \
                and self._replaced(record, row):
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
            'expires': _iso(record['expires']) if record['state'] == 'uploading' else None,
            'deposit_kind': record.get('deposit_kind') or 'dataset'}
        if record['state'] == 'completing':
            view['progress'] = self._progress_view(record)
        if record.get('complete_error') and record['state'] == 'uploading':
            try:
                view['complete_error'] = json.loads(record['complete_error'])
            except ValueError:
                pass
        if identity.is_admin:
            view['user'] = record['user']
        if not detail:
            return view
        error = source.get('error') if source else record.get('error')
        warnings = json.loads(source['warnings'] or '[]') if source.get('warnings') else json.loads(
            record.get('warnings') or '[]')
        report = self._report(record, row if matches else None)
        events = self.store.events(record['id'])
        hidden = set()
        if not identity.is_admin:            # a depositor sees "an admin", not who
            hidden = {entry['actor'] for entry in events} - {record['user'], 'watcher', 'expiry', 'create', None, ''}
            approval_by = json.loads(record['approval']).get('by') if record.get('approval') else None
            if approval_by and approval_by != record['user']:
                hidden.add(approval_by)

        def mask(text: Any) -> Any:
            text = sanitize_text(text, self.roots)
            if isinstance(text, str):
                for name in sorted(hidden, key=len, reverse=True):
                    text = re.sub(rf'(?<![\w.@-]){re.escape(name)}(?![\w.@-])', 'an admin', text)
                if not identity.is_admin:       # "by NAME (API login NAME)": the login and OS user are admin detail
                    text = ADMIN_WHO_RE.sub('', text)
            return text

        approval = json.loads(record['approval']) if record.get('approval') else None
        if approval and hidden:
            approval = {**approval, 'by': 'an admin'}
        view.update({
            'error': mask(error),
            'warnings': [mask(text) for text in warnings],
            'files': [{'name': entry['name'], 'kind': entry['kind'], 'key': entry['key'], 'size': entry['size'],
                       'sha256': entry['sha256'], 'method': entry['method'], 'state': entry['state']}
                      for entry in self.store.files(record['id'])],
            'watch': ({'state': row['state'], 'served': bool(row.get('served')),
                       'catalog_verified': bool(row.get('catalog_verified')),
                       'incoming_deleted': bool(row.get('deleted'))} if matches else None),
            'report': report_summary(report),
            'report_available': report is not None,
            'events': [{'time': _iso(entry['ts']), 'action': entry['action'],
                        'actor': 'an admin' if entry['actor'] in hidden else entry['actor'],
                        'detail': mask(entry['detail'])}
                       for entry in events],
            'approval': approval,
        })
        return view

    def list(self, identity: Identity, all_users: bool = False) -> Dict[str, Any]:
        if self.settings.active:
            self.expire_stale()
        if all_users and not identity.is_admin:
            raise DepositError(403, 'forbidden', 'only admins list every deposit')
        records = [self.refresh_result(record) for record in self.store.deposits(None if all_users else identity.user)]
        return {'deposits': [self._view(record, False, identity) for record in records], 'all': bool(all_users)}

    def status(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        record = self.refresh_result(self._get_own(identity, deposit_id))
        if record['state'] == 'uploading' and record['expires'] <= self.clock() and self.settings.active:
            self.expire_stale()
            record = self.store.get(deposit_id) or record
        return self._view(record, True, identity)

    def report(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        record = self.refresh_result(self._get_own(identity, deposit_id))
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        report = self._report(record, row if watch_row_matches(record, row) else None)
        if report is None:
            view = self._view(record, True, identity)
            if view['state'] in ('needs_decision', 'failed', 'rejected', 'withdrawn', 'expired') and view.get('error'):
                raise DepositError(404, 'no_report', f'no check report: the deposit stopped as {view["state"]} '
                                                     f'before the checks ran. Reason: {view["error"]}',
                                   state=view['state'], reason=view['error'])
            raise DepositError(404, 'no_report', 'no check report for this deposit yet', state=view['state'])
        return report

    # withdraw / approve / reject ------------------------------------------

    def _abort_and_delete(self, record: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Abort this deposit's multipart uploads, empty its staging folder and delete its own incoming copies
        (only objects whose ``x-amz-meta-deposit`` is this deposit and whose ETag is the completed one)."""
        aborted, deleted, kept = [], [], []
        for row in self.store.files(record['id']):
            if row['method'] == 'multipart' and row['state'] in ('pending',) and row['upload_id']:
                try:
                    self.s3.abort_multipart_upload(Bucket=self.bucket, Key=row['staging_key'],
                                                   UploadId=row['upload_id'])
                except Exception as exc:  # noqa: BLE001
                    if _error_code(exc) not in ('NoSuchUpload', '404'):
                        raise
                aborted.append(row['name'])
                self.store.update_file(record['id'], row['name'], state='aborted')
            if self.settings.central_watcher:
                continue                  # incoming is the central watcher's: it removes its copies (24.9)
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
        staging = self._clean_staging(record['id'])
        self.store.event(record['id'], actor, 'cleanup', json.dumps({'aborted': aborted, 'deleted': deleted,
                                                                     'kept': kept, 'staging_removed': len(staging)}))
        return {'aborted': aborted, 'deleted': deleted, 'kept': kept}

    def withdraw(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        """Abort and delete this deposit's own objects (staging and incoming); mark it withdrawn."""
        self._require_active()
        record = self._get_own(identity, deposit_id)
        if record['state'] in ('withdrawn', 'expired', 'expiring', 'withdrawing'):
            raise DepositError(409, 'not_withdrawable', f'the deposit is already {record["state"]}')
        if record['state'] == 'completing':
            raise DepositError(409, 'busy', 'the deposit is being completed right now; try again in a moment')
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        state = public_state(record, row if watch_row_matches(record, row) else None)
        if state in ('checking', 'importing'):
            raise DepositError(409, 'busy', 'the watcher is processing this deposit; withdraw it once it is done')
        if state == 'live':
            raise DepositError(409, 'live', 'the deposit is published; ask an admin to retire the dataset')
        if record.get('continues'):
            original = self.store.get(record['continues']) or {}
            if original.get('state') in ('uploading', 'completing', 'uploaded') \
                    and original.get('watch_state') not in ('live', 'rejected'):
                raise DepositError(409, 'continuation', f'this deposit corrects {original["id"]}, whose h5ad still '
                                                        f'waits: withdraw the original deposit {original["id"]} '
                                                        f'(its corrections are withdrawn with it)')
        followers = [self.store.get(row['id']) for row in self.store._rows(
            "SELECT id FROM deposits WHERE continues = ? AND state IN ('uploading', 'completing', 'uploaded')",
            (deposit_id,))]
        if any(other['state'] == 'completing' for other in followers):
            raise DepositError(409, 'busy', 'a correction of this deposit is being completed; try again in a moment')
        cascaded = []
        for other in followers:                      # corrections first: they replaced files of this deposit
            other_row = read_watch_row(self.state_dir, other.get('watch_key'))
            if public_state(other, other_row if watch_row_matches(other, other_row) else None) in ('checking',
                                                                                                    'live'):
                raise DepositError(409, 'busy', f'the correction {other["id"]} is being processed or live')
            if self.store.cas(other['id'], ('uploading', 'uploaded'), 'withdrawing', not_importing=True):
                try:
                    self._abort_and_delete(other, identity.user)
                except Exception as exc:  # noqa: BLE001 - never leave a correction withdrawing
                    self.store.cas(other['id'], ('withdrawing',), other['state'])
                    raise DepositError(502, 's3_error', f'withdrawing the correction {other["id"]} failed '
                                                        f'({_error_code(exc)}); nothing else was withdrawn, '
                                                        f'try again')
                self.store.cas(other['id'], ('withdrawing',), 'withdrawn')
                self.store.release_keys(other['id'])
                self.store.event(other['id'], identity.user, 'withdrawn', f'with the original deposit {deposit_id}')
                self.publish_record(other['id'])
                cascaded.append(other['id'])
        if not self.store.cas(deposit_id, ('uploading', 'uploaded'), 'withdrawing', not_importing=True):
            raise DepositError(409, 'busy', 'the deposit changed state meanwhile; look at it again')
        try:
            result = self._abort_and_delete(record, identity.user)
        except Exception as exc:  # noqa: BLE001
            self.store.cas(deposit_id, ('withdrawing',), record['state'])
            raise DepositError(502, 's3_error', f'withdrawing failed ({_error_code(exc)}); try again')
        self.store.cas(deposit_id, ('withdrawing',), 'withdrawn')
        self.store.release_keys(deposit_id)
        self.store.event(deposit_id, identity.user, 'withdrawn', '')
        self.publish_record(deposit_id)
        return {'deposit_id': deposit_id, 'state': 'withdrawn', 'withdrawn_corrections': cascaded, **result}

    @staticmethod
    def _require_admin(identity: Identity, what: str = 'approve or reject deposits') -> None:
        if not identity.is_admin:
            raise DepositError(403, 'forbidden', f'only admins {what}')

    def _require_local_watcher(self) -> None:
        if self.settings.central_watcher:
            raise DepositError(409, 'central_watcher', 'this site sends its deposits to a central watcher: an admin '
                                                       'approves or rejects them there (ant-ingest-watch approve / '
                                                       'reject <item> --by NAME)')

    # ── central watcher: signed records out, signed results in (design section 24) ───────────────

    def publish_record(self, deposit_id: str) -> None:
        """Write this deposit's signed record.json next to its uploads (a site with a central watcher only).

        The version counter grows with every write, so the watcher refuses an older copy (replay). Never raises:
        a failed write is an event, and the next state change writes again.
        """
        if not self.settings.central_watcher:
            return
        import deposit_records  # noqa: PLC0415

        try:
            record = deposit_records.export_record(self.store, deposit_id)
            current = self.store.get(deposit_id) or {}
            version = int(current.get('record_version') or 0) + 1
            raw = deposit_records.sign_record(record, self.settings.site_id, deposit_id, version,
                                              self.settings.site_key_id, self.settings.site_secret)
            key = deposit_records.object_key(self.settings.staging_root, self.settings.site_id, deposit_id,
                                             deposit_records.RECORD_NAME)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType='application/json')
            self.store.update(deposit_id, record_version=version)
        except Exception as exc:  # noqa: BLE001 - the error text never carries the secret
            self.store.event(deposit_id, 'site', 'record_not_written', str(exc)[:200])

    def refresh_result(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Read the central watcher's signed result.json of an uploaded deposit into the local record.

        Verified with the watcher's public key(s); an older or equal version is ignored. Writes the redacted
        report copy, the watch state, db id, version, held reasons and warnings, and e-mails the depositor on
        live, held and failed (this site sends the e-mails of its deposits). Returns the (updated) record.
        Never raises (review C2): anything in the ingestion bucket is attacker-reachable, so a result that cannot
        be read or checked leaves the record as it is and becomes an event.
        """
        if not self.settings.central_watcher or record.get('state') != 'uploaded':
            return record
        try:
            return self._refresh_result(record)
        except Exception as exc:  # noqa: BLE001
            try:
                self.store.event(record['id'], 'site', 'result_refused', f'{type(exc).__name__}'[:200])
            except Exception:  # noqa: BLE001
                pass
            return record

    def _refresh_result(self, record: Dict[str, Any]) -> Dict[str, Any]:
        import deposit_records  # noqa: PLC0415

        deposit_id = record['id']
        key = deposit_records.object_key(self.settings.staging_root, self.settings.site_id, deposit_id,
                                         deposit_records.RESULT_NAME)
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            raw = response['Body'].read(deposit_records.MAX_RESULT_BYTES + 1)
        except Exception:  # noqa: BLE001 - no result yet (or unreadable): the record stays as it is
            return record
        try:
            envelope = deposit_records.verify_result(raw, self.settings.site_id, deposit_id,
                                                     list(self.settings.watcher_public_keys))
        except deposit_records.RecordError as exc:
            self.store.event(deposit_id, 'site', 'result_refused', str(exc)[:200])
            return record
        if envelope['version'] <= int(record.get('result_version') or 0):
            return record
        result = envelope['result']
        report = result.get('report') if isinstance(result.get('report'), dict) else None
        copy = None
        if report is not None:
            copy = report_copy_path(self.state_dir, deposit_id)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(json.dumps(redacted_report(report, self.roots), indent=1))
        row = {'state': str(result.get('state') or ''), 'db_id': result.get('db_id'), 'version': result.get('version'),
               'kind': result.get('kind'), 'held_reasons': list(result.get('held_reasons') or []),
               'warnings': list(result.get('warnings') or []), 'error': result.get('error')}
        self.store.record_watch(deposit_id, row, self.roots, report_file=str(copy) if copy else None)
        extra = {'result_version': envelope['version']}
        if result.get('artefact_ref'):
            extra['artefact_ref'] = str(result['artefact_ref'])[:200]
        self.store.update(deposit_id, **extra)
        self.store.event(deposit_id, 'watcher', row['state'] or 'result', str(sanitize_text(
            row.get('error') or row.get('version') or '', self.roots))[:300])
        updated = self.store.get(deposit_id) or record
        marker = f'{row["state"]}:{envelope["version"]}'
        if row['state'] in NOTIFY_DEPOSITOR_ON and updated.get('notified_state') != marker:
            try:
                import ingest_watch  # noqa: PLC0415
                summary = report_summary(json.loads(copy.read_text())) if copy else None
                if ingest_watch.notify_depositor({'notify': dict(self.settings.notify or {})}, updated, row, summary,
                                                 lambda message: None):
                    self.store.update(deposit_id, notified_state=marker)
            except Exception:  # noqa: BLE001 - the e-mail is a courtesy
                pass
        return self.store.get(deposit_id) or updated

    def site_sync(self, limit: int = 200) -> Dict[str, int]:
        """The periodic work of a site with a central watcher: expiry, the staging sweep of its own prefix,
        and the results of its uploaded deposits (ant-serve runs it in a background thread)."""
        counts = {'expired': 0, 'results': 0}
        if not self.settings.central_watcher:
            return counts
        counts['expired'] = len(self.expire_stale())
        rows = self.store._rows("SELECT * FROM deposits WHERE state = 'uploaded' AND COALESCE(watch_state, '') NOT IN "
                                "('live', 'rejected') ORDER BY updated LIMIT ?", (int(limit),))
        for row in rows:
            before = row.get('result_version')
            if self.refresh_result(row).get('result_version') != before:
                counts['results'] += 1
        return counts

    def _held_row(self, record: Dict[str, Any]) -> Dict[str, Any]:
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        if record['state'] != 'uploaded' or not watch_row_matches(record, row):
            raise DepositError(409, 'not_reviewable', 'the watcher has no current record of this deposit')
        return row

    def _rejected(self, record: Dict[str, Any]) -> bool:
        """The deposit (or the watcher's current row of it) was rejected: final, never continued."""
        if record.get('watch_state') == 'rejected':
            return True
        row = read_watch_row(self.state_dir, record.get('watch_key'))
        return bool(row and row.get('state') == 'rejected' and watch_row_matches(record, row))

    def approve(self, identity: Identity, deposit_id: str, note: str = '') -> Dict[str, Any]:
        """Admin: publish a held deposit past its held reasons (the watcher's approve)."""
        import ingest_watch  # noqa: PLC0415

        self._require_admin(identity)
        self._require_local_watcher()
        record = self._get_own(identity, deposit_id)
        row = self._held_row(record)
        if row['state'] in ('needs_decision', 'failed'):
            raise DepositError(409, 'not_held', f'the deposit is {row["state"]}, not held, so there is nothing to '
                                                f'approve: fix the cause named in its status, then retry it '
                                                f'(POST /datasets/deposit/{deposit_id}/retry), or reject it')
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

    def retry(self, identity: Identity, deposit_id: str, note: str = '') -> Dict[str, Any]:
        """Admin: run a needs_decision or failed deposit again once its cause is fixed (the watcher's retry)."""
        import ingest_watch  # noqa: PLC0415

        self._require_admin(identity, 'retry deposits')
        record = self._get_own(identity, deposit_id)
        row = self._held_row(record)
        if row['state'] not in ingest_watch.RETRY_STATES:
            raise DepositError(409, 'not_retryable', f'the deposit is {public_state(record, row)}; only '
                                                     f'needs_decision or failed deposits are retried'
                                                     + (' (a held one is approved or rejected)'
                                                        if row['state'] == 'held' else ''))
        watch_db = ingest_watch.StateDB(self.state_dir / WATCH_DB_FILE)
        try:
            ingest_watch.retry(watch_db, row['key'], identity.user, note or '', channel='api')
        except ValueError as exc:
            raise DepositError(409, 'not_retryable', str(exc))
        finally:
            watch_db.close()
        self.store.update(deposit_id, watch_state='stable', error=None)
        self.store.event(deposit_id, identity.user, 'retry', note or '')
        return {'deposit_id': deposit_id, 'state': 'checking', 'previous_state': row['state'], 'note': note or '',
                'next': 'the next poll of the watcher runs the deposit again; if the cause is not fixed it '
                        'stops at the same state'}

    def reject(self, identity: Identity, deposit_id: str, note: str = '') -> Dict[str, Any]:
        """Admin: reject a held, failed or needs_decision deposit (final).

        The watcher's item is marked rejected, then the incoming objects of the deposit and of the deposits it
        continues are deleted (only objects that are still exactly theirs) and corrections still being uploaded
        are withdrawn, so a later deposit of the same name starts afresh and never continues this one.
        """
        import ingest_watch  # noqa: PLC0415

        self._require_admin(identity)
        self._require_local_watcher()
        record = self._get_own(identity, deposit_id)
        row = self._held_row(record)
        watch_db = ingest_watch.StateDB(self.state_dir / WATCH_DB_FILE)
        try:
            ingest_watch.reject(watch_db, row['key'], identity.user, note or '', channel='api')
        except ValueError as exc:
            raise DepositError(409, 'not_rejectable', str(exc))
        finally:
            watch_db.close()
        self.store.update(deposit_id, watch_state='rejected', error='rejected by an admin'
                          + (f': {note}' if note else ''))
        self.store.event(deposit_id, identity.user, 'rejected', note or '')
        cleanup = self.clean_rejected(deposit_id, identity.user)
        return {'deposit_id': deposit_id, 'state': 'rejected', 'note': note or '', **cleanup,
                'next': 'final: the deposit is not continued; a new deposit of the same name starts afresh'}

    def clean_rejected(self, deposit_id: str, actor: str) -> Dict[str, Any]:
        """Delete the incoming objects of a rejected deposit and of the deposits it continues; withdraw its
        open corrections. Idempotent; an object written by someone else, or changed, is kept and reported."""
        deleted: List[str] = []
        kept: List[Dict[str, Any]] = []
        withdrawn: List[str] = []
        if self.s3 is None:
            return {'deleted': deleted, 'kept': kept, 'withdrawn_corrections': withdrawn}
        for chained in self.store.chain(deposit_id):
            other = self.store.get(chained)
            if not other:
                continue
            result = self._abort_and_delete(other, actor)
            deleted += result['deleted']
            kept += result['kept']
            for entry in result['kept']:
                name = next((row['name'] for row in self.store.files(chained) if row['key'] == entry['key']), None)
                if name:
                    self.store.update_file(chained, name, state='kept')
            if chained != deposit_id and other.get('watch_state') != 'rejected':
                self.store.update(chained, watch_state='rejected', error=f'rejected with {deposit_id}')
                self.store.event(chained, actor, 'rejected', f'with the deposit {deposit_id}, which continues it')
            self.store.release_keys(chained)
        followers = self.store._rows("SELECT id FROM deposits WHERE continues = ? AND state IN "
                                     "('uploading', 'uploaded')", (deposit_id,))
        for follower in followers:
            other = self.store.get(follower['id'])
            if other and other['state'] == 'uploading' and \
                    self.store.cas(other['id'], ('uploading',), 'withdrawing', not_importing=True):
                try:
                    self._abort_and_delete(other, actor)
                finally:
                    self.store.cas(other['id'], ('withdrawing',), 'withdrawn')
                    self.store.release_keys(other['id'])
                self.store.event(other['id'], actor, 'withdrawn', f'the deposit it corrects, {deposit_id}, '
                                                                  f'was rejected')
                withdrawn.append(other['id'])
        self.store.event(deposit_id, actor, 'cleanup', json.dumps({'deleted': deleted, 'kept': kept,
                                                                   'withdrawn_corrections': withdrawn}))
        return {'deleted': deleted, 'kept': kept, 'withdrawn_corrections': withdrawn}

    def pending_rejected(self) -> List[str]:
        """Rejected deposits whose incoming files are not cleaned up yet (e.g. rejected on the host)."""
        return [row['id'] for row in self.store._rows(
            "SELECT DISTINCT d.id FROM deposits d JOIN files f ON f.deposit_id = d.id "
            "WHERE d.state = 'uploaded' AND d.watch_state = 'rejected' AND f.state = 'complete'")]


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
