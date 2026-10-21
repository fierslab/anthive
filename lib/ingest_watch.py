"""State, settle logic and the poll loop of ``ant-ingest-watch`` (the S3 watch route).

One row per deposited h5ad key in ``<ingest_state_dir>/ingest_watch.sqlite``:

    seen -> stable -> pulled -> checked -> live | held | failed | needs_decision

* ``seen``: listed, waiting until the upload is complete.
* ``stable``: the h5ad and its ``.h5ad.mus`` exist and every object of the
  item kept its ETag and size over two polls (or the newest is older than
  ``settle_seconds``). Ready to process.
* ``pulled`` / ``checked``: progress markers while the pipeline runs.
* ``live``: published. ``held``: clean but waits for an admin (held reasons
  of ``ant-ingest-check``). ``failed``: errors or a tool failure.
  ``needs_decision``: the store holds something the route cannot reconcile.

A final row is processed again only when the ETag or size of its h5ad or
sidecar changes. S3 errors back off exponentially (per poll and per item).
After an item went live, was served after a rescan and its catalog copy on S3
was verified, its consumed incoming keys are deleted one by one (when
``delete_incoming_after_ingest`` is on and their ETag is unchanged); every
delete is logged in the ``deletions`` table.

A file lock (``ingest_watch.lock``) keeps two runs from overlapping. The
watcher holds no lock the server needs: it reaches ant-serve over HTTP only,
and takes the per-dataset writer flock only for the seconds of a swap.
"""
from __future__ import annotations

import fcntl
import json
import os
import smtplib
import sqlite3
import time
import urllib.request
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ingest_pipeline import (Item, Outcome, Pipeline, TransientError, delete_consumed, if_match_selftest,
                             plan_items)

STATES = ('seen', 'stable', 'pulled', 'checked', 'live', 'held', 'failed', 'needs_decision', 'rejected')
FINAL_STATES = ('live', 'held', 'failed', 'needs_decision', 'rejected')
#: states an admin may send back to the pipeline once the cause is fixed (``retry``)
RETRY_STATES = ('needs_decision', 'failed')
MAX_ITEM_ATTEMPTS = 8
STATUS_FILE = 'ingest_watch_status.json'
LOCK_FILE = 'ingest_watch.lock'
DB_FILE = 'ingest_watch.sqlite'

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY, data_project TEXT, subfolder TEXT, basename TEXT, state TEXT NOT NULL,
    content_signature TEXT, settle_signature TEXT, processed_signature TEXT,
    h5ad_etag TEXT, h5ad_size INTEGER, mus_etag TEXT,
    first_seen REAL, last_seen REAL, polls_unchanged INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
    db_id TEXT, kind TEXT, version TEXT, report_path TEXT, log_path TEXT, error TEXT,
    warnings TEXT, held_reasons TEXT, consumed TEXT, served INTEGER DEFAULT 0,
    catalog_verified INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, git_commit TEXT, timings TEXT,
    updated REAL, sha256 TEXT, approval TEXT, shared_keys TEXT, post_failed TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, ts REAL, state TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT, s3_key TEXT, etag TEXT, outcome TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, db_id TEXT, dataset TEXT, action TEXT, version TEXT,
    sha256 TEXT, location TEXT, ts REAL
);
"""


class LockBusy(RuntimeError):
    """Another ant-ingest-watch run holds the lock."""


class WatchLock:
    """Non-blocking exclusive flock on ``<state_dir>/ingest_watch.lock``."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / LOCK_FILE
        self.handle = None

    def __enter__(self) -> 'WatchLock':
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, 'a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise LockBusy(f'{self.path} is held by another ant-ingest-watch run')
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f'{os.getpid()}\n')
        self.handle.flush()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class StateDB:
    """SQLite store of the watch state (WAL, one writer: the watcher)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        #: called with ``(key, state, detail, fields)`` after every state change (ant-ingest-watch's console)
        self.on_transition: Optional[Callable[[str, str, str, Dict[str, Any]], None]] = None
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(items)')}
        for column in ('sha256', 'approval', 'shared_keys', 'post_failed'):
            if column not in columns:                  # state files written before 2026-10-14
                self.conn.execute(f'ALTER TABLE items ADD COLUMN {column} TEXT')
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute('SELECT * FROM items WHERE key = ?', (key,)).fetchone()
        return dict(row) if row else None

    def items(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.conn.execute('SELECT * FROM items ORDER BY updated DESC')]

    def upsert(self, key: str, **fields: Any) -> None:
        fields['updated'] = time.time()
        existing = self.get(key)
        if existing is None:
            columns = ['key'] + list(fields)
            self.conn.execute(f'INSERT INTO items ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))})',
                              [key] + list(fields.values()))
        else:
            assignments = ', '.join(f'{name} = ?' for name in fields)
            self.conn.execute(f'UPDATE items SET {assignments} WHERE key = ?', list(fields.values()) + [key])
        self.conn.commit()

    def transition(self, key: str, state: str, detail: str = '', **fields: Any) -> None:
        if state not in STATES:
            raise ValueError(f'unknown state {state!r}')
        self.upsert(key, state=state, **fields)
        self.event(key, state, detail)
        if self.on_transition is not None:
            try:
                self.on_transition(key, state, detail, fields)
            except Exception:  # noqa: BLE001 - the console must never break the state machine
                pass

    def event(self, key: str, state: str, detail: str = '') -> None:
        self.conn.execute('INSERT INTO events (key, ts, state, detail) VALUES (?, ?, ?, ?)',
                          (key, time.time(), state, detail[:4000]))
        self.conn.commit()

    def events(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        if key is None:
            rows = self.conn.execute('SELECT * FROM events ORDER BY id')
        else:
            rows = self.conn.execute('SELECT * FROM events WHERE key = ? ORDER BY id', (key,))
        return [dict(row) for row in rows]

    def record_deletion(self, item_key: str, s3_key: str, etag: str, outcome: str) -> None:
        self.conn.execute('INSERT INTO deletions (item_key, s3_key, etag, outcome, ts) VALUES (?, ?, ?, ?, ?)',
                          (item_key, s3_key, etag, outcome, time.time()))
        self.conn.commit()

    def deletions(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.conn.execute('SELECT * FROM deletions ORDER BY id')]

    def record_version(self, db_id: str, dataset: str, action: str, version: str, sha256: str, location: str) -> None:
        self.conn.execute('INSERT INTO versions (db_id, dataset, action, version, sha256, location, ts) '
                          'VALUES (?, ?, ?, ?, ?, ?, ?)', (db_id, dataset, action, version, sha256, location, time.time()))
        self.conn.commit()

    def versions(self, db_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if db_id is None:
            rows = self.conn.execute('SELECT * FROM versions ORDER BY id')
        else:
            rows = self.conn.execute('SELECT * FROM versions WHERE db_id = ? ORDER BY id', (db_id,))
        return [dict(row) for row in rows]


# ── settle logic ─────────────────────────────────────────────────────────────

def leftover_of_live(row: Dict[str, Any], item: Item) -> bool:
    """The listed objects are what is left of a live deposit after some of its keys were deleted."""
    return (row['state'] == 'live' and item.h5ad.etag == row['h5ad_etag'] and item.h5ad.size == row['h5ad_size']
            and (item.mus is None or item.mus.etag == row['mus_etag'])
            and not item.deposit)              # a new API deposit of the same bytes is processed (as unchanged)


def observe(db: StateDB, items: Sequence[Item], now: float, settle_seconds: float) -> List[Item]:
    """Record one poll; return the items that are ready to process (oldest first).

    An item becomes ready when its h5ad and sidecar exist, its settle
    signature (ETag + size of every object it may consume, the project's
    static, seed and DE keys included) was identical on the previous listing,
    and its newest object is at least ``settle_seconds`` old. The first listing
    of an item never makes it ready. A final row comes back only when the h5ad
    or the sidecar changed (``content_signature``), or after ``approve``.
    """
    ready: List[Item] = []
    for item in items:
        row = db.get(item.key)
        content, settle = item.content_signature(), item.settle_signature()
        base = {'data_project': item.data_project, 'subfolder': item.subfolder, 'basename': item.basename,
                'h5ad_etag': item.h5ad.etag, 'h5ad_size': item.h5ad.size,
                'mus_etag': item.mus.etag if item.mus else None, 'last_seen': now}
        if row is None:
            db.transition(item.key, 'seen', 'first listed', first_seen=now, content_signature=content,
                          settle_signature=settle, polls_unchanged=0, attempts=0, next_attempt=0, **base)
            if item.mus is None:
                db.upsert(item.key, error=f'waiting for {item.key}.mus')
            continue
        if row['state'] in FINAL_STATES:
            if row['processed_signature'] == content or leftover_of_live(row, item):
                continue
            db.transition(item.key, 'seen', 'new upload (ETag or size changed)', content_signature=content,
                          settle_signature=settle, polls_unchanged=0, attempts=0, next_attempt=0, error=None,
                          warnings=None, consumed=None, served=0, catalog_verified=0, deleted=0, db_id=None,
                          kind=None, version=None, report_path=None, held_reasons=None, git_commit=None,
                          timings=None, approval=None, shared_keys=None, sha256=None, **base)
            continue
        if row['settle_signature'] != settle or row['content_signature'] != content:
            if row['approval']:
                db.event(item.key, row['state'], 'deposit changed (data, sidecar, seeds or static files): '
                                                 'the approval no longer applies')
            db.upsert(item.key, content_signature=content, settle_signature=settle, polls_unchanged=0,
                      state='seen' if row['state'] == 'stable' else row['state'], approval=None, **base)
            continue
        db.upsert(item.key, polls_unchanged=int(row['polls_unchanged'] or 0) + 1, **base)
        if item.mus is None:
            db.upsert(item.key, error=f'waiting for {item.key}.mus')
            continue
        if now - item.newest_modified() < settle_seconds:
            continue
        if row['state'] != 'stable':
            db.transition(item.key, 'stable', 'upload complete (two identical listings)', error=None)
        if float(row['next_attempt'] or 0) <= now:
            ready.append(item)
    return sorted(ready, key=lambda entry: entry.newest_modified())


def _os_user() -> str:
    import getpass  # noqa: PLC0415

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return f'uid {os.getuid()}'


def _who(by: str, channel: str) -> str:
    """How the decision is recorded: the OS user on the host (cli), or the API login (api)."""
    return f'API login {by}' if channel == 'api' else f'OS user {_os_user()}'


def approve(db: StateDB, key_or_db_id: str, by: str, note: str = '', channel: str = 'cli') -> Dict[str, Any]:
    """Mark a held item for publication by the next poll, past the held reasons it has now.

    ``channel`` is ``cli`` (``ant-ingest-watch approve`` on the host; the OS
    user is recorded) or ``api`` (``POST /datasets/deposit/{id}/approve`` by an
    admin; ``by`` is the proxy's Remote-User).

    Raises:
        ValueError: no such item, the item is not held, or ``by`` is empty.
    """
    row = _find(db, key_or_db_id)
    if not by.strip():
        raise ValueError('approve needs --by <name>')
    if row['state'] in RETRY_STATES:
        raise ValueError(f'{row["key"]} is {row["state"]}, not held: nothing to approve. Fix the cause in its '
                         f'message, then retry it (ant ingest-watch retry), or reject it')
    if row['state'] != 'held':
        raise ValueError(f'{row["key"]} is {row["state"]}, not held')
    approval = {'by': by.strip(), 'os_user': _os_user() if channel == 'cli' else None, 'channel': channel,
                'reasons': json.loads(row['held_reasons'] or '[]'),
                'note': note.strip(), 'date': time.strftime('%Y-%m-%d'), 'signature': row['settle_signature']}
    db.transition(row['key'], 'stable', f'approved by {by} ({_who(by, channel)}) for '
                                        f'{", ".join(approval["reasons"])}',
                  approval=json.dumps(approval), polls_unchanged=1, next_attempt=0)
    return approval


def retry(db: StateDB, key_or_db_id: str, by: str, note: str = '', channel: str = 'cli') -> Dict[str, Any]:
    """Send a needs_decision or failed item back to the pipeline (the next poll runs it again).

    For a cause that is fixed outside the deposit (a store ``.md`` edited by hand, a missing reference, a
    transient error that used up its attempts). An unchanged cause stops the item at the same state again.

    Raises:
        ValueError: no such item, the item is in another state, or ``by`` is empty.
    """
    row = _find(db, key_or_db_id)
    if not by.strip():
        raise ValueError('retry needs --by <name>')
    if row['state'] not in RETRY_STATES:
        raise ValueError(f'{row["key"]} is {row["state"]}; only needs_decision or failed items are retried')
    text = f'retry requested by {by} ({_who(by, channel)}) after {row["state"]}' + (f': {note}' if note else '')
    db.transition(row['key'], 'stable', text, error=None, attempts=0, polls_unchanged=1, next_attempt=0,
                  approval=None, processed_signature=None)
    return {'key': row['key'], 'previous_state': row['state']}


def deposit_of_row(row: Dict[str, Any]) -> Optional[str]:
    """The API deposit an item row is linked to (its content signature), or None."""
    try:
        parts = json.loads(row.get('content_signature') or '[]')
    except ValueError:
        return None
    for part in parts:
        if isinstance(part, list) and len(part) == 3 and part[0] == 'deposit':
            return str(part[1])
    return None


def reject(db: StateDB, key_or_db_id: str, by: str, note: str = '', channel: str = 'cli') -> None:
    """Mark a held, failed or needs_decision item as rejected (final; nothing is published).

    An API deposit's incoming objects are deleted by the deposit service (at once through the API, at the
    next poll after a reject on the host)."""
    row = _find(db, key_or_db_id)
    if row['state'] not in ('held', 'failed', 'needs_decision'):
        raise ValueError(f'{row["key"]} is {row["state"]}; only held, failed or needs_decision items are rejected')
    text = f'rejected by {by} ({_who(by, channel)})' + (f': {note}' if note else '')
    db.transition(row['key'], 'rejected', text, error=text)


def _find(db: StateDB, key_or_db_id: str) -> Dict[str, Any]:
    row = db.get(key_or_db_id)
    if row is None:
        matches = [entry for entry in db.items() if (entry['db_id'] or '').upper() == key_or_db_id.upper()
                   or entry['key'].endswith('/' + key_or_db_id)]
        if len(matches) != 1:
            raise ValueError(f'{key_or_db_id}: {len(matches)} matching items (give the full key)')
        row = matches[0]
    return row


def repo_dir_problems(paths: Dict[str, Path], repo_root: Path) -> List[str]:
    """Runtime directories that lie inside the repository checkout (runtime writes belong outside it)."""
    root = Path(repo_root).resolve()
    problems = []
    for label, path in paths.items():
        resolved = Path(path).resolve()
        if resolved == root or root in resolved.parents:
            problems.append(f'{label} {resolved} is inside the repository {root}')
    return problems


# ── notifications ────────────────────────────────────────────────────────────

def notify(settings: Dict[str, Any], row: Dict[str, Any], log: Callable[[str], None] = print) -> None:
    """Send the configured webhook / e-mail for a final state; never raises."""
    notify_settings = settings.get('notify') or {}
    if row.get('state') not in (notify_settings.get('on') or []):
        return
    payload = {'event': 'anthive.ingest_watch', 'state': row.get('state'), 'key': row.get('key'),
               'db_id': row.get('db_id'), 'version': row.get('version'), 'kind': row.get('kind'),
               'error': row.get('error'), 'held_reasons': json.loads(row.get('held_reasons') or '[]'),
               'warnings': json.loads(row.get('warnings') or '[]'), 'report': row.get('report_path')}
    if notify_settings.get('webhook_url'):
        try:
            request = urllib.request.Request(notify_settings['webhook_url'], data=json.dumps(payload).encode(),
                                             headers={'Content-Type': 'application/json'}, method='POST')
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:  # noqa: BLE001
            log(f'notify: webhook failed: {exc}')
    if notify_settings.get('email_to') and notify_settings.get('smtp_host'):
        try:
            message = EmailMessage()
            message['Subject'] = f'[anthive ingest] {payload["state"]}: {payload["key"]}'
            message['From'] = notify_settings.get('email_from') or 'anthive-ingest@localhost'
            message['To'] = notify_settings['email_to']
            message.set_content(json.dumps(payload, indent=2))
            with smtplib.SMTP(notify_settings['smtp_host'], int(notify_settings.get('smtp_port') or 25),
                              timeout=20) as smtp:
                smtp.send_message(message)
        except Exception as exc:  # noqa: BLE001
            log(f'notify: e-mail failed: {exc}')


def notify_depositor(settings: Dict[str, Any], deposit: Dict[str, Any], row: Dict[str, Any],
                     summary: Optional[Dict[str, Any]], log: Callable[[str], None] = print) -> bool:
    """E-mail the depositor of an API deposit about live, held or failed (redacted; never raises).

    Uses the ``notify`` SMTP settings; needs ``smtp_host`` and a valid e-mail
    of the deposit (the proxy's Remote-Email at deposit time). Returns True
    when a message was handed to the SMTP server.
    """
    import dataset_deposit  # noqa: PLC0415
    from ingest_md_fill import _EMAIL_RE  # noqa: PLC0415

    notify_settings = settings.get('notify') or {}
    email = str(deposit.get('email') or '').strip()
    if row.get('state') not in dataset_deposit.NOTIFY_DEPOSITOR_ON or not notify_settings.get('smtp_host') \
            or not _EMAIL_RE.fullmatch(email):
        return False
    held = json.loads(row.get('held_reasons') or '[]')
    lines = [f'Your Anthive deposit {deposit.get("id")} ({deposit.get("data_project")}/{deposit.get("basename")}) '
             f'is now: {row.get("state")}.', '']
    if row.get('state') == 'live':
        lines.append(f'Published as {row.get("db_id")} version {row.get("version")}.')
    if held:
        lines.append('Held for an admin: ' + ', '.join(held) + '.')
    if row.get('state') != 'live' and row.get('error'):
        lines.append('Note: ' + str(dataset_deposit.sanitize_text(row.get('error')))[:1000])
    for problem in (summary or {}).get('problems', [])[:20]:
        lines.append(f'- {problem.get("status")} {problem.get("id")}: {problem.get("summary")}'
                     + (f' Fix: {problem.get("fix")}' if problem.get('fix') else ''))
    lines += ['', f'Details: GET /api/datasets/deposit/{deposit.get("id")} (or `vark datasets status`).']
    try:
        message = EmailMessage()
        message['Subject'] = f'[anthive] deposit {deposit.get("basename")}: {row.get("state")}'
        message['From'] = notify_settings.get('email_from') or 'anthive-ingest@localhost'
        message['To'] = email
        message.set_content('\n'.join(lines))
        with smtplib.SMTP(notify_settings['smtp_host'], int(notify_settings.get('smtp_port') or 25), timeout=20) as smtp:
            smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f'notify: depositor e-mail failed: {exc}')
        return False


# ── status ───────────────────────────────────────────────────────────────────

def status_rows(db: StateDB) -> List[Dict[str, Any]]:
    """Items for the status table and the JSON status file."""
    rows = []
    for row in db.items():
        rows.append({
            'key': row['key'], 'state': row['state'], 'db_id': row['db_id'], 'kind': row['kind'],
            'version': row['version'], 'error': row['error'],
            'warnings': json.loads(row['warnings'] or '[]'), 'held_reasons': json.loads(row['held_reasons'] or '[]'),
            'report': row['report_path'], 'log': row['log_path'], 'served': bool(row['served']),
            'catalog_verified': bool(row['catalog_verified']), 'incoming_deleted': bool(row['deleted']),
            'git_commit': row['git_commit'], 'timings': json.loads(row['timings'] or '{}'),
            'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['updated'] or 0)),
        })
    return rows


def write_status(state_dir: Path, db: StateDB, extra: Optional[Dict[str, Any]] = None) -> Path:
    """Write ``ingest_watch_status.json`` atomically."""
    path = Path(state_dir) / STATUS_FILE
    data = {'updated': time.strftime('%Y-%m-%dT%H:%M:%S'), 'pid': os.getpid(), 'items': status_rows(db),
            **(extra or {})}
    partial = path.with_suffix('.json.partial')
    partial.write_text(json.dumps(data, indent=1, default=str))
    os.replace(partial, path)
    return path


def render_status_table(rows: Sequence[Dict[str, Any]]) -> str:
    """Plain-text table: state, dataset, version, key, note."""
    if not rows:
        return '(no items recorded)'
    lines = [f'{"STATE":<15} {"DB_ID":<9} {"VERSION":<8} {"UPDATED":<19}  KEY / NOTE']
    for row in rows:
        note = row['error'] or ', '.join(row['held_reasons']) or (
            '; '.join(row['warnings'])[:120] if row['warnings'] else '')
        lines.append(f'{row["state"]:<15} {row["db_id"] or "-":<9} {row["version"] or "-":<8} {row["updated"]:<19}  '
                     f'{row["key"]}' + (f'\n{"":<55}{note}' if note else ''))
    return '\n'.join(lines)


# ── one poll ─────────────────────────────────────────────────────────────────

class Watcher:
    """One poll of the prefix: recover swaps, list, settle, process ready items one at a time, delete what was consumed."""

    def __init__(self, settings: Dict[str, Any], db: StateDB, s3: Any, bucket: str, pipeline: Pipeline,
                 state_dir: Path, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 max_items: Optional[int] = None, stop_requested: Callable[[], bool] = lambda: False,
                 deposit_settings: Any = None, artefact_publisher: Any = None,
                 list_s3: Any = None, remote: Any = None):
        self.settings = settings
        #: ``remote_deposits.RemoteSites``: the deposits of other sites (central watcher, design section 24)
        self.remote = remote
        self.db = db
        self.s3 = s3
        #: client for listing the prefix (fewer, shorter retries than the transfer client ``s3``); None: ``s3``
        self._list_s3 = list_s3
        self.bucket = bucket
        self.pipeline = pipeline
        self.state_dir = Path(state_dir)
        self.log = log
        self.clock = clock
        self.max_items = max_items
        self.stop_requested = stop_requested
        self.if_match: Optional[bool] = None
        #: dataset items found in the last listing (the console's polling line)
        self.last_listed = 0
        #: ``dataset_deposit.DepositSettings`` for expiry and the staging sweep (None: no deposit housekeeping)
        self.deposit_settings = deposit_settings
        self._deposit_marks: Dict[Tuple[str, str], str] = {}
        #: ``artefact_pipeline.ArtefactPublisher``: artefact and collection deposits, purges (REST 2.47)
        self.artefact_publisher = artefact_publisher

    def check_if_match(self) -> bool:
        """Whether the backend enforces ``IfMatch`` on DeleteObject (self-test, cached a week per bucket and prefix)."""
        if self.if_match is not None:
            return self.if_match
        cache = self.state_dir / 'ifmatch_selftest.json'
        prefix = self.settings['prefix']
        try:
            cached = json.loads(cache.read_text())
            if cached.get('bucket') == self.bucket and cached.get('prefix') == prefix \
                    and self.clock() - float(cached.get('ts', 0)) < 7 * 86400:
                self.if_match = cached.get('result') == 'enforced'
                return self.if_match
        except (OSError, ValueError):
            pass
        result = if_match_selftest(self.s3, self.bucket, prefix)
        self.if_match = result == 'enforced'
        self.log(f'IfMatch self-test on {prefix}_selftest/: {result} '
                 f'({"used on top of the HEAD check" if self.if_match else "not relied on; HEAD check only"})')
        self.db.event('-', 'selftest', f'IfMatch delete: {result}')
        try:
            cache.write_text(json.dumps({'bucket': self.bucket, 'prefix': prefix, 'result': result, 'ts': self.clock()}))
        except OSError:
            pass
        return self.if_match

    def list_prefix(self) -> List[Dict[str, Any]]:
        """Every object under the prefix (raises TransientError on an S3 error)."""
        objects: List[Dict[str, Any]] = []
        try:
            paginator = (self._list_s3 if self._list_s3 is not None else self.s3).get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.settings['prefix']):
                objects.extend(page.get('Contents', []) or [])
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'listing {self.settings["prefix"]} failed: {exc}') from exc
        return objects

    def poll(self) -> Dict[str, int]:
        """Recover, list, settle and process. Returns counts per outcome of this poll."""
        counts: Dict[str, int] = {}
        for message in self.pipeline.recover():
            self.db.event('-', 'recovery', message)
        objects = self.list_prefix()
        if self.remote is not None:
            self.import_remote_records(objects)
        items, malformed = plan_items(objects, self.settings['prefix'])
        for message in malformed:
            self.log(f'malformed: {message}')
        items = self.link_deposits(items)
        self.last_listed = len(items)
        ready = observe(self.db, items, self.clock(), float(self.settings['settle_seconds']))
        self.retry_unverified(items)
        for index, item in enumerate(ready):
            if self.stop_requested():
                self.log('stop requested: leaving the remaining items for the next run')
                break
            if self.max_items is not None and index >= self.max_items:
                break
            state = self.process(item, items)
            counts[state] = counts.get(state, 0) + 1
        if not self.stop_requested():
            try:
                for state, number in self.process_de_deposits(objects).items():
                    counts[state] = counts.get(state, 0) + number
            except Exception as exc:  # noqa: BLE001 - never stop the poll for the DE route
                self.log(f'deposits: DE deposits not processed: {exc}')
        if self.artefact_publisher is not None and not self.stop_requested():
            try:
                for state, number in self.artefact_publisher.poll(objects).items():
                    counts[f'artefact_{state}'] = counts.get(f'artefact_{state}', 0) + number
            except Exception as exc:  # noqa: BLE001 - never stop the poll for the artefact route
                self.log(f'artefacts: not processed this poll: {exc}')
        if self.remote is not None:
            self.export_remote_results()
        self.prune_work_dirs()
        return counts

    # deposits of other sites (central watcher, lib/remote_deposits.py) -------

    def _open_deposit_store(self) -> Any:
        import dataset_deposit  # noqa: PLC0415

        return dataset_deposit.DepositStore(self.state_dir / dataset_deposit.DB_FILE)

    def import_remote_records(self, objects: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Verify and import the signed records of the known sites before the deposits are linked."""
        import remote_deposits  # noqa: PLC0415

        listed = {str(obj['Key']): (str(obj.get('ETag', '')).strip('"'), int(obj.get('Size') or 0),
                                    remote_deposits.epoch(obj.get('LastModified'))) for obj in objects}
        store = self._open_deposit_store()
        try:
            return self.remote.import_records(store, listed)
        except Exception as exc:  # noqa: BLE001 - a broken site never stops the route
            self.log(f'remote deposits: import failed ({type(exc).__name__}: {str(exc)[:200]})')
            return {}
        finally:
            store.close()

    def export_remote_results(self) -> int:
        """Write the signed results of imported deposits whose outcome changed."""
        store = self._open_deposit_store()
        try:
            return self.remote.export_results(store)
        except Exception as exc:  # noqa: BLE001
            self.log(f'remote deposits: results not written ({type(exc).__name__}: {str(exc)[:200]})')
            return 0
        finally:
            store.close()

    # API deposits (lib/dataset_deposit.py) --------------------------------

    def deposit_roots(self) -> Dict[str, Path]:
        """Server paths replaced in what a depositor sees."""
        return {'store': self.pipeline.config.store_root, 'state': self.state_dir,
                'work': self.pipeline.config.work_root, 'bucket': self.bucket}

    def link_deposits(self, items: List[Item]) -> List[Item]:
        """Attach API deposit records to their items, hold back items of deposits still completing, and refuse
        deposit-marked objects that no completed deposit accounts for.

        * An item belongs to a deposit when its h5ad and sidecar keys and ETags are exactly the ones the
          deposit completed. It uses only the static and DE files of its own deposit (and of the deposits a
          corrected sidecar continues); an unlinked item never uses a deposit's files.
        * Items whose h5ad or sidecar key is locked by a deposit that is uploading or completing wait.
        * An unlinked item whose h5ad or sidecar carries ``x-amz-meta-deposit`` is refused
          (``needs_decision``): only the server writes that mark, so it is a replay or a leftover. With
          ``ingest_watch.accept_unlinked: false`` every unlinked item is refused (deposits through the API only).
        * Fails closed: a deposit store that exists but cannot be read stops the poll (TransientError).

        Each poll also expires stale deposits, sweeps the staging prefix and writes pending owner caveats.
        """
        import dataset_deposit  # noqa: PLC0415

        accept_unlinked = bool(self.settings.get('accept_unlinked', True))
        try:
            store = dataset_deposit.DepositStore.open_if_exists(self.state_dir)
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'the deposit store exists but cannot be opened ({exc}); not polling') from exc
        if store is None:
            if not accept_unlinked:
                for item in items:
                    item.refused = self._unlinked_refusal(item, accept_unlinked)
            return items
        try:
            if self.deposit_settings is not None:
                try:
                    service = dataset_deposit.DepositService(self.deposit_settings, store, self.s3,
                                                             self.pipeline.config.store_root, self.state_dir,
                                                             clock=self.clock)
                    for deposit_id in service.expire_stale():
                        self.log(f'deposits: {deposit_id} expired (uploads aborted, its files removed)')
                    for deposit_id in service.pending_rejected():
                        result = service.clean_rejected(deposit_id, 'watcher')
                        self.log(f'deposits: {deposit_id} rejected: {len(result["deleted"])} incoming object(s) '
                                 f'deleted' + (f', {len(result["kept"])} kept (not its own any more)'
                                               if result['kept'] else ''))
                except Exception as exc:  # noqa: BLE001 - housekeeping; the next poll retries
                    self.log(f'deposits: expiry or staging sweep failed: {exc}')
            try:
                dataset_deposit.apply_owner_caveats(store, self.pipeline.config.store_root, self.log,
                                                    git_identity=str(self.settings.get('git_identity') or
                                                                     'anthive ingest-watch <noreply@anthive.local>'),
                                                    commit=bool(self.settings.get('store_git_commit', True)))
            except Exception as exc:  # noqa: BLE001
                self.log(f'deposits: owner caveats not written: {exc}')
            try:
                held = store.held_keys()
                objects = store.deposit_objects()
                kept: List[Item] = []
                for item in items:
                    if item.h5ad.key in held or (item.mus is not None and item.mus.key in held):
                        continue
                    if item.mus is None:
                        # a deposit's h5ad whose sidecar is gone (e.g. a withdrawn correction) is never ignored
                        try:
                            entry = objects.get(item.h5ad.key)
                            owner = entry[0] if entry and entry[1] == item.h5ad.etag else self._deposit_mark(item.h5ad)
                        except TransientError as exc:
                            self.log(f'deposits: {item.key} waits: {exc}')
                            continue
                        if owner:
                            self._orphan_h5ad(item, owner)
                            continue
                    record = store.find_for_item(item.h5ad.key, item.h5ad.etag, item.mus.key,
                                                 item.mus.etag) if item.mus is not None else None
                    if record:
                        item.deposit = {key: record.get(key) for key in
                                        ('id', 'user', 'name', 'email', 'update_of', 'data_project', 'basename')}
                        chain = set(store.chain(record['id']))
                        if record.get('site_id'):
                            # another site's deposit: the pipeline checks every pulled file against the signed
                            # sha256 (review C5); the h5ad of a correction is the continued deposit's
                            signed = {row['key']: row['sha256'] for deposit_id in reversed(store.chain(record['id']))
                                      for row in store.files(deposit_id) if row.get('sha256')}   # newest wins
                            if record.get('h5ad_sha256'):
                                signed[item.h5ad.key] = record['h5ad_sha256']
                            item.deposit['signed_sha256'] = signed

                        def own(obj, chain=chain):
                            entry = objects.get(obj.key)
                            return entry is not None and entry[0] in chain and entry[1] == obj.etag
                        item.static = [obj for obj in item.static if own(obj)]
                        item.de = [obj for obj in item.de if own(obj)]
                    else:
                        try:
                            def foreign(obj):
                                entry = objects.get(obj.key)
                                return (obj.key in held or (entry is not None and entry[1] == obj.etag)
                                        or bool(self._deposit_mark(obj)))
                            item.static = [obj for obj in item.static if not foreign(obj)]
                            item.de = [obj for obj in item.de if not foreign(obj)]
                            item.refused = self._unlinked_refusal(item, accept_unlinked)
                        except TransientError as exc:          # fail closed for this item: look again next poll
                            self.log(f'deposits: {item.key} waits: {exc}')
                            continue
                    kept.append(item)
            except sqlite3.Error as exc:
                raise TransientError(f'the deposit store cannot be read ({exc}); not polling') from exc
            if len(kept) < len(items):
                self.log(f'deposits: {len(items) - len(kept)} item(s) wait for a deposit that is still completing')
            return kept
        finally:
            store.close()

    def _orphan_h5ad(self, item: Item, deposit_id: str) -> None:
        """Record an API deposit's h5ad without its sidecar as needs_decision (once per upload)."""
        row = self.db.get(item.key)
        signature = item.content_signature()
        if row and row['state'] == 'needs_decision' and row.get('processed_signature') == signature:
            return
        if row and row['state'] == 'live' and row.get('h5ad_etag') == item.h5ad.etag:
            return                     # what is left of a published deposit (its deletes are retried)
        message = (f'{item.h5ad.key} of the API deposit {deposit_id} has no sidecar in incoming (withdrawn or '
                   f'deleted); deposit the sidecar again, or withdraw the deposit')
        if row is None:
            self.db.transition(item.key, 'needs_decision', message, data_project=item.data_project,
                               subfolder=item.subfolder, basename=item.basename, h5ad_etag=item.h5ad.etag,
                               h5ad_size=item.h5ad.size, content_signature=signature, settle_signature=signature,
                               processed_signature=signature, error=message, first_seen=self.clock(),
                               last_seen=self.clock())
        else:
            self.db.transition(item.key, 'needs_decision', message, processed_signature=signature,
                               content_signature=signature, error=message)
        self.log(f'deposits: {message}')
        self._notify(item.key)

    def process_de_deposits(self, objects: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Completed API deposits of DE tables for a published dataset (no h5ad): import them."""
        import dataset_deposit  # noqa: PLC0415
        from ingest_pipeline import ObjInfo  # noqa: PLC0415

        counts: Dict[str, int] = {}
        store = dataset_deposit.DepositStore.open_if_exists(self.state_dir)
        if store is None:
            return counts
        try:
            listing = {obj['Key']: ObjInfo.from_listing(obj) for obj in objects}
            # an import a previous run left 'importing' died with it (the watcher runs alone): try it again
            for stale in store._rows("SELECT id FROM deposits WHERE deposit_kind = 'de_only' AND "
                                     "watch_state = 'importing'"):
                store.update(stale['id'], watch_state=None)
                store.event(stale['id'], 'watcher', 'retry', 'an interrupted import is tried again')
            records = store._rows("SELECT * FROM deposits WHERE deposit_kind = 'de_only' AND state = 'uploaded' "
                                  "AND COALESCE(watch_state, '') = '' AND COALESCE(disputed, '') = ''")
            for record in records:
                if self.stop_requested():
                    break
                if not store.claim_import(record['id']):     # withdrawn meanwhile, or another claim
                    continue
                files = store.files(record['id'])
                objs = [listing.get(row['key']) for row in files]
                roots = self.deposit_roots()
                if any(obj is None or obj.etag != row['etag'] for obj, row in zip(objs, files)):
                    outcome_row = {'state': 'needs_decision', 'db_id': record.get('update_of'),
                                   'error': 'files of this DE deposit are missing or changed in incoming'}
                    store.record_watch(record['id'], outcome_row, roots)
                    counts['needs_decision'] = counts.get('needs_decision', 0) + 1
                    continue
                self.log(f'DE deposit {record["id"]} for {record.get("update_of")}')
                if record.get('site_id'):                     # review C5: pulled files against the signed sha256
                    record = {**record, 'signed_sha256': {row['key']: row['sha256'] for row in files}}
                try:
                    outcome = self.pipeline.ingest_de_deposit(record, objs)
                except TransientError as exc:
                    store.update(record['id'], watch_state=None)
                    store.event(record['id'], 'watcher', 'retry', str(dataset_deposit.sanitize_text(str(exc), roots)))
                    counts['retry'] = counts.get('retry', 0) + 1
                    continue
                row = {'state': outcome.state, 'db_id': outcome.db_id, 'version': outcome.version, 'kind': 'de',
                       'held_reasons': '[]', 'error': None if outcome.state == 'live' else outcome.detail,
                       'warnings': json.dumps(outcome.warnings)}
                store.record_watch(record['id'], row, roots)
                store.event(record['id'], 'watcher', outcome.state, str(dataset_deposit.sanitize_text(
                    outcome.detail, roots)))
                if outcome.state == 'live' and outcome.consumed and self.settings.get('delete_incoming_after_ingest'):
                    delete_consumed(self.s3, self.bucket, self.settings['prefix'], outcome.consumed,
                                    lambda key, etag, result: self.db.record_deletion(f'deposit:{record["id"]}', key,
                                                                                       etag, result),
                                    if_match=self.check_if_match())
                if outcome.state in dataset_deposit.NOTIFY_DEPOSITOR_ON:
                    notify_depositor(self.settings, record, {**row, 'held_reasons': '[]'}, None, self.log)
                counts[outcome.state] = counts.get(outcome.state, 0) + 1
        finally:
            store.close()
        return counts

    def _deposit_mark(self, obj: Any) -> str:
        """``x-amz-meta-deposit`` of a listed object ('' when unmarked), cached per key and ETag.

        A HEAD that fails is a TransientError: the item waits for the next poll (fail closed)."""
        cache_key = (obj.key, obj.etag)
        if cache_key not in self._deposit_marks:
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=obj.key)
            except Exception as exc:  # noqa: BLE001
                raise TransientError(f'HEAD {obj.key} failed ({exc}); its deposit mark is unknown') from exc
            metadata = {str(k).lower(): str(v) for k, v in (head.get('Metadata') or {}).items()}
            self._deposit_marks[cache_key] = metadata.get('deposit', '')
        return self._deposit_marks[cache_key]

    def _unlinked_refusal(self, item: Item, accept_unlinked: bool) -> Optional[str]:
        """Why an item that belongs to no completed deposit is refused, or None."""
        if not accept_unlinked:
            return ('uploads outside the deposit API are not accepted on this site '
                    '(ingest_watch.accept_unlinked: false); deposit through the API')
        for obj in (item.h5ad, item.mus):
            if obj is None:
                continue
            mark = self._deposit_mark(obj)
            if mark:
                return (f'{obj.key} carries the mark of API deposit {mark} but is not an upload that deposit '
                        f'completed (a replayed or left-over object); refused. Deposit again through the API.')
        return None

    def sync_deposit(self, item: Item) -> None:
        """Copy the outcome of an API deposit's item into its record, write its redacted report, mail the depositor."""
        if not item.deposit:
            return
        import dataset_deposit  # noqa: PLC0415

        row = self.db.get(item.key)
        if not row:
            return
        try:
            store = dataset_deposit.DepositStore.open_if_exists(self.state_dir)
            if store is None:
                return
            try:
                roots = self.deposit_roots()
                deposit_id = item.deposit['id']
                copy = dataset_deposit.write_report_copy(self.state_dir, deposit_id, row.get('report_path'), roots)
                store.record_watch(deposit_id, row, roots, report_file=str(copy) if copy else None)
                store.event(deposit_id, 'watcher', row['state'],
                            str(dataset_deposit.sanitize_text(row.get('error') or row.get('version') or '', roots)))
                record = store.get(deposit_id) or {}
                marker = f'{row["state"]}:{row.get("processed_signature") or ""}'
                if record.get('notified_state') != marker and row['state'] in dataset_deposit.NOTIFY_DEPOSITOR_ON \
                        and not record.get('site_id'):     # another site's depositor: that site sends the e-mail
                    summary = None
                    if copy:
                        summary = dataset_deposit.report_summary(json.loads(copy.read_text()))
                    if notify_depositor(self.settings, {**record, **item.deposit}, row, summary, self.log):
                        store.update(deposit_id, notified_state=marker)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - the deposit record is a mirror; never stop the watch route
            self.log(f'deposits: could not record the outcome of {item.key}: {exc}')

    def process(self, item: Item, listed: Sequence[Item] = ()) -> str:
        """Run the pipeline for one ready item and record the outcome."""
        key = item.key
        if getattr(item, 'refused', None):
            self.db.transition(key, 'needs_decision', item.refused, error=item.refused,
                               processed_signature=item.content_signature())
            self.log(f'refused {item.display}: {item.refused}')
            self._notify(key)
            return 'needs_decision'
        row = self.db.get(key) or {}
        approval = json.loads(row['approval']) if row.get('approval') else None
        if approval and approval.get('signature') != item.settle_signature():
            approval = None
        self.db.transition(key, 'pulled', 'processing started' + (' (approved)' if approval else ''))
        self.log(f'processing {item.display}')
        try:
            outcome = self.pipeline.process(item, approval=approval)
        except TransientError as exc:
            attempts = int(row.get('attempts') or 0) + (0 if self.stop_requested() else 1)
            if attempts >= MAX_ITEM_ATTEMPTS:
                self.db.transition(key, 'failed', f'gave up after {attempts} attempts: {exc}', attempts=attempts,
                                   error=str(exc), processed_signature=item.content_signature())
                self._notify(key)
                self.sync_deposit(item)
                return 'failed'
            delay = 0 if self.stop_requested() else min(3600, 60 * 2 ** attempts)
            self.db.transition(key, 'stable', f'retry in {delay} s: {exc}', attempts=attempts,
                               next_attempt=self.clock() + delay, error=str(exc))
            return 'retry'
        except Exception as exc:  # noqa: BLE001 - a bug or an unexpected tool state: record, never crash the loop
            self.db.transition(key, 'failed', f'{type(exc).__name__}: {exc}', error=f'{type(exc).__name__}: {exc}',
                               processed_signature=item.content_signature())
            self._notify(key)
            self.sync_deposit(item)
            return 'failed'
        self.record(item, outcome, approval)
        if outcome.state == 'live':
            self.delete_if_verified(item, outcome, listed)
        self._notify(key)
        self.sync_deposit(item)
        return outcome.state

    def record(self, item: Item, outcome: Outcome, approval: Optional[Dict[str, Any]] = None) -> None:
        if outcome.state in ('live', 'held'):
            self.db.event(item.key, 'checked', f'ant-ingest-check report {outcome.report_path}')
        live = outcome.state == 'live'
        detail = outcome.detail if live or not outcome.version else f'{outcome.detail} (would have been {outcome.version})'
        self.db.transition(
            item.key, outcome.state, detail, processed_signature=item.content_signature(),
            db_id=outcome.db_id, kind=outcome.kind, version=outcome.version if live else None,
            report_path=outcome.report_path,
            error=None if live else outcome.detail,
            warnings=json.dumps(outcome.warnings), held_reasons=json.dumps(outcome.held_reasons),
            consumed=json.dumps(outcome.consumed), served=int(outcome.served),
            catalog_verified=int(outcome.catalog_verified), git_commit=outcome.git_commit,
            timings=json.dumps(outcome.timings), attempts=0, next_attempt=0, sha256=outcome.sha256,
            shared_keys=json.dumps(outcome.shared_keys), approval=json.dumps(approval) if approval else None,
            post_failed=json.dumps(outcome.post_failed),
            log_path=str(getattr(self.pipeline.runner, 'log_path', '') or ''))
        pending = outcome.finished_pending
        if live and pending is not None and pending.version != outcome.version and pending.kind != 'new':
            self.db.record_version(pending.db_id or '', item.display, 'current', pending.version or '',
                                   pending.sha256 or '', pending.store_h5ad or '')
            self.db.record_version(pending.db_id or '', item.display, 'archived', pending.previous_version or '',
                                   pending.previous_sha256 or '', '; '.join(pending.archived_local[:1]))
        if live and outcome.kind in ('new', 'update_minor', 'update_patch'):
            self.db.record_version(outcome.db_id or '', item.display, 'current', outcome.version or '',
                                   outcome.sha256 or '', outcome.store_h5ad or '')
            if outcome.previous_version and outcome.kind != 'new':
                location = '; '.join(outcome.archived_local[:1] + outcome.archived_s3[:1])
                self.db.record_version(outcome.db_id or '', item.display, 'archived', outcome.previous_version,
                                       outcome.previous_sha256 or '', location)

    def _pending_siblings(self, item: Item, listed: Sequence[Item]) -> List[str]:
        """Other items of the same project still in incoming and not live with their keys deleted."""
        pending = []
        for other in listed:
            if other is item or other.data_project != item.data_project:
                continue
            row = self.db.get(other.key)
            if row is None or row['state'] != 'live' or not row['deleted']:
                pending.append(other.key)
        return pending

    def delete_if_verified(self, item: Item, outcome: Outcome, listed: Sequence[Item] = ()) -> None:
        """Delete the consumed incoming keys of a live, served, verified item (when enabled)."""
        if not self.settings.get('delete_incoming_after_ingest'):
            return
        if not outcome.deletable:
            self.db.event(item.key, 'live', 'incoming objects kept: not verified '
                                            f'(served={outcome.served}, catalog={outcome.catalog_verified})')
            return
        if outcome.store_h5ad and not Path(outcome.store_h5ad).exists():
            self.db.event(item.key, 'live', 'incoming objects kept: the store copy is missing')
            return
        if self.pipeline.has_pending(item):
            self.db.event(item.key, 'live', 'incoming objects kept: push or post steps still pending')
            return
        if outcome.post_failed:
            if not self.settings.get('delete_despite_post_warnings', True):
                self.db.event(item.key, 'live', 'incoming objects kept: post steps failed ('
                              + ', '.join(outcome.post_failed) + '); delete_despite_post_warnings is off')
                return
            self.db.event(item.key, 'live', 'post steps failed (' + ', '.join(outcome.post_failed)
                          + '): accepted for the delete (ingest_watch.delete_despite_post_warnings)')
        pending = self._pending_siblings(item, listed)
        keep = {key: 'shared_with_pending_sibling' for key in outcome.shared_keys} if pending else {}

        results: Dict[str, str] = {}

        def record(s3_key: str, etag: str, result: str) -> None:
            results[s3_key] = result
            self.db.record_deletion(item.key, s3_key, etag, result)
            self.log(f'incoming {result}: {s3_key} ({etag})')

        deleted, skipped = delete_consumed(self.s3, self.bucket, self.settings['prefix'], outcome.consumed, record,
                                           keep=keep, if_match=self.check_if_match())
        own_keys = [key for key in outcome.consumed if key not in keep]
        gone = all(results.get(key) in ('deleted', 'skipped:absent') for key in own_keys)
        self.db.upsert(item.key, deleted=int(bool(own_keys) and gone))
        self.db.event(item.key, 'live', f'incoming: {len(deleted)} deleted, {len(skipped)} kept'
                                        + (f' (shared static kept for {len(pending)} pending sibling(s))'
                                           if keep else ''))

    def retry_unverified(self, items: Sequence[Item]) -> None:
        """Live rows whose rescan, served check or catalog push failed: retry, then delete when verified.

        At most one rescan per poll for all rows together.
        """
        if not self.settings.get('delete_incoming_after_ingest'):
            return
        listed = {item.key: item for item in items}
        candidates = [row for row in self.db.items()
                      if row['state'] == 'live' and not row['deleted'] and row['key'] in listed
                      and (row['processed_signature'] == listed[row['key']].content_signature()
                           or leftover_of_live(row, listed[row['key']]))
                      and not (json.loads(row['post_failed'] or '[]')
                               and not self.settings.get('delete_despite_post_warnings', True))]
        if not candidates:
            return
        rescan_result: Optional[Dict[str, Any]] = None
        if any(not row['served'] for row in candidates):
            try:
                rescan_result = self.pipeline.api.rescan()
            except TransientError:
                rescan_result = None
        for row in candidates:
            item = listed[row['key']]
            served, verified = bool(row['served']), bool(row['catalog_verified'])
            warnings: List[str] = []
            if not verified and self.pipeline.config.push_catalog:
                pushed, warnings = self.pipeline.repush(item, row['kind'] or 'new', row['sha256'] or '')
                if pushed is None:
                    self.db.transition(row['key'], 'needs_decision', '; '.join(warnings), error='; '.join(warnings))
                    continue
                verified = pushed
            if not served and rescan_result is not None:
                served = self.pipeline.verify_served(row['db_id'], row['sha256'], row['version'], rescan_result,
                                                     warnings)
            self.db.upsert(row['key'], served=int(served), catalog_verified=int(verified))
            if warnings:
                self.db.event(row['key'], 'live', 'retry: ' + '; '.join(warnings)[:1000])
            if served and verified:                 # also retries deletes that failed with an S3 error
                outcome = Outcome(state='live', served=True, catalog_verified=True, db_id=row['db_id'],
                                  consumed=json.loads(row['consumed'] or '{}'),
                                  shared_keys=json.loads(row['shared_keys'] or '[]'),
                                  post_failed=json.loads(row['post_failed'] or '[]'))
                self.delete_if_verified(item, outcome, items)

    def prune_work_dirs(self) -> None:
        """Remove work dirs of finished items older than ``work_ttl_days`` (held and failed ones are kept that long)."""
        ttl_days = float(self.settings.get('work_ttl_days', 14))
        work_root = self.pipeline.config.work_root
        if not work_root.is_dir():
            return
        from ingest_pipeline import slug  # noqa: PLC0415

        active = {slug(row['key']) for row in self.db.items() if row['state'] in ('seen', 'stable', 'pulled', 'checked')}
        cutoff = self.clock() - ttl_days * 86400
        for entry in work_root.iterdir():
            if entry.is_dir() and entry.name not in active and entry.stat().st_mtime < cutoff:
                import shutil  # noqa: PLC0415

                shutil.rmtree(entry, ignore_errors=True)
                self.db.event('-', 'cleanup', f'removed work dir {entry.name} (older than {ttl_days:g} days)')

    def _notify(self, key: str) -> None:
        row = self.db.get(key)
        if row:
            notify(self.settings, row, self.log)
