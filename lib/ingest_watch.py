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

from ingest_pipeline import Item, Outcome, Pipeline, TransientError, delete_consumed, plan_items

STATES = ('seen', 'stable', 'pulled', 'checked', 'live', 'held', 'failed', 'needs_decision')
FINAL_STATES = ('live', 'held', 'failed', 'needs_decision')
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
    updated REAL
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

def observe(db: StateDB, items: Sequence[Item], now: float, settle_seconds: float) -> List[Item]:
    """Record one poll; return the items that are ready to process (oldest first).

    An item becomes ready when its h5ad and sidecar exist and either its
    settle signature (ETag + size of every object it may consume) was the
    same on the previous poll, or its newest object is older than
    ``settle_seconds``. A final row comes back only when the h5ad or the
    sidecar changed (``content_signature``).
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
            row = db.get(item.key)
        elif row['state'] in FINAL_STATES:
            if row['processed_signature'] == content:
                continue
            db.transition(item.key, 'seen', 'new upload (ETag or size changed)', content_signature=content,
                          settle_signature=settle, polls_unchanged=0, attempts=0, next_attempt=0, error=None,
                          warnings=None, consumed=None, served=0, catalog_verified=0, deleted=0, db_id=None,
                          kind=None, version=None, report_path=None, held_reasons=None, git_commit=None,
                          timings=None, **base)
            row = db.get(item.key)
        elif row['settle_signature'] != settle:
            db.upsert(item.key, content_signature=content, settle_signature=settle, polls_unchanged=0,
                      state='seen' if row['state'] == 'stable' else row['state'], **base)
            row = db.get(item.key)
        else:
            db.upsert(item.key, polls_unchanged=int(row['polls_unchanged'] or 0) + 1, **base)
            row = db.get(item.key)
        if item.mus is None:
            db.upsert(item.key, error=f'waiting for {item.key}.mus')
            continue
        old_enough = now - item.newest_modified() >= settle_seconds
        if int(row['polls_unchanged'] or 0) >= 1 or old_enough:
            if row['state'] != 'stable':
                db.transition(item.key, 'stable', 'upload complete' + (' (settle time)' if old_enough else ''),
                              error=None)
            if float(row['next_attempt'] or 0) <= now:
                ready.append(item)
    return sorted(ready, key=lambda entry: entry.newest_modified())


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
    """One poll of the prefix: list, settle, process ready items one at a time, delete what was consumed."""

    def __init__(self, settings: Dict[str, Any], db: StateDB, s3: Any, bucket: str, pipeline: Pipeline,
                 state_dir: Path, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 max_items: Optional[int] = None):
        self.settings = settings
        self.db = db
        self.s3 = s3
        self.bucket = bucket
        self.pipeline = pipeline
        self.state_dir = Path(state_dir)
        self.log = log
        self.clock = clock
        self.max_items = max_items

    def list_prefix(self) -> List[Dict[str, Any]]:
        """Every object under the prefix (raises TransientError on an S3 error)."""
        objects: List[Dict[str, Any]] = []
        try:
            paginator = self.s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.settings['prefix']):
                objects.extend(page.get('Contents', []) or [])
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'listing {self.settings["prefix"]} failed: {exc}') from exc
        return objects

    def poll(self) -> Dict[str, int]:
        """List, settle and process. Returns counts per outcome of this poll."""
        counts: Dict[str, int] = {}
        objects = self.list_prefix()
        items, malformed = plan_items(objects, self.settings['prefix'])
        for message in malformed:
            self.log(f'malformed: {message}')
        ready = observe(self.db, items, self.clock(), float(self.settings['settle_seconds']))
        self.retry_unverified(items)
        for index, item in enumerate(ready):
            if self.max_items is not None and index >= self.max_items:
                break
            state = self.process(item)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def process(self, item: Item) -> str:
        """Run the pipeline for one ready item and record the outcome."""
        key = item.key
        row = self.db.get(key) or {}
        self.db.transition(key, 'pulled', 'processing started')
        self.log(f'processing {item.display}')
        try:
            outcome = self.pipeline.process(item)
        except TransientError as exc:
            attempts = int(row.get('attempts') or 0) + 1
            if attempts >= MAX_ITEM_ATTEMPTS:
                self.db.transition(key, 'failed', f'gave up after {attempts} attempts: {exc}', attempts=attempts,
                                   error=str(exc), processed_signature=item.content_signature())
                self._notify(key)
                return 'failed'
            delay = min(3600, 60 * 2 ** attempts)
            self.db.transition(key, 'stable', f'retry in {delay} s: {exc}', attempts=attempts,
                               next_attempt=self.clock() + delay, error=str(exc))
            return 'retry'
        except Exception as exc:  # noqa: BLE001 - a bug or an unexpected tool state: record, never crash the loop
            self.db.transition(key, 'failed', f'{type(exc).__name__}: {exc}', error=f'{type(exc).__name__}: {exc}',
                               processed_signature=item.content_signature())
            self._notify(key)
            return 'failed'
        self.record(item, outcome)
        if outcome.state == 'live':
            self.delete_if_verified(item, outcome)
        self._notify(key)
        return outcome.state

    def record(self, item: Item, outcome: Outcome) -> None:
        if outcome.state in ('live', 'held'):
            self.db.event(item.key, 'checked', f'ant-ingest-check report {outcome.report_path}')
        live = outcome.state == 'live'
        detail = outcome.detail if live or not outcome.version else f'{outcome.detail} (would have been {outcome.version})'
        self.db.transition(
            item.key, outcome.state, detail, processed_signature=item.content_signature(),
            db_id=outcome.db_id, kind=outcome.kind, version=outcome.version if live else None,
            report_path=outcome.report_path,
            error=None if outcome.state == 'live' else outcome.detail,
            warnings=json.dumps(outcome.warnings), held_reasons=json.dumps(outcome.held_reasons),
            consumed=json.dumps(outcome.consumed), served=int(outcome.served),
            catalog_verified=int(outcome.catalog_verified), git_commit=outcome.git_commit,
            timings=json.dumps(outcome.timings), attempts=0, next_attempt=0,
            log_path=str(getattr(self.pipeline.runner, 'log_path', '') or ''))
        if outcome.state == 'live' and outcome.kind in ('new', 'update_minor', 'update_patch'):
            self.db.record_version(outcome.db_id or '', item.display, 'current', outcome.version or '',
                                   outcome.sha256 or '', outcome.store_h5ad or '')
            if outcome.previous_version and outcome.kind != 'new':
                location = '; '.join(outcome.archived_local[:1] + outcome.archived_s3[:1])
                self.db.record_version(outcome.db_id or '', item.display, 'archived', outcome.previous_version,
                                       outcome.previous_sha256 or '', location)

    def delete_if_verified(self, item: Item, outcome: Outcome) -> None:
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

        def record(s3_key: str, etag: str, result: str) -> None:
            self.db.record_deletion(item.key, s3_key, etag, result)
            self.log(f'incoming {result}: {s3_key} ({etag})')

        deleted, skipped = delete_consumed(self.s3, self.bucket, self.settings['prefix'], outcome.consumed, record)
        self.db.upsert(item.key, deleted=int(bool(deleted) and not skipped))
        self.db.event(item.key, 'live', f'incoming: {len(deleted)} deleted, {len(skipped)} kept')

    def retry_unverified(self, items: Sequence[Item]) -> None:
        """Live rows whose rescan or catalog check failed: try the rescan again, then delete when verified."""
        if not self.settings.get('delete_incoming_after_ingest'):
            return
        listed = {item.key: item for item in items}
        for row in self.db.items():
            if row['state'] != 'live' or row['deleted'] or row['key'] not in listed:
                continue
            if row['processed_signature'] != listed[row['key']].content_signature():
                continue
            if row['served'] and row['catalog_verified']:
                continue
            if not row['catalog_verified']:
                continue                       # needs a person (or the next upload): the catalog copy is wrong
            try:
                result = self.pipeline.api.rescan()
            except TransientError:
                continue
            from ingest_pipeline import served_ids  # noqa: PLC0415

            if row['db_id'] in served_ids(result):
                self.db.upsert(row['key'], served=1)
                outcome = Outcome(state='live', served=True, catalog_verified=True, db_id=row['db_id'],
                                  consumed=json.loads(row['consumed'] or '{}'))
                self.delete_if_verified(listed[row['key']], outcome)

    def _notify(self, key: str) -> None:
        row = self.db.get(key)
        if row:
            notify(self.settings, row, self.log)
