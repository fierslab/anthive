"""Watcher side of artefact and collection deposits (doc/DESIGN_artefacts.md sections 5 and 6, phase A2).

``ant-ingest-watch`` calls :meth:`ArtefactPublisher.poll` once per poll with the listing of the incoming
prefix. Items are driven by the deposit store (``deposits.sqlite``, ``deposit_kind`` ``artefact`` or
``collection``, state ``uploaded``), like the DE-only deposits: an item is linked only when every object
the deposit completed is listed with exactly its key and ETag and carries the deposit's mark; anything
else under ``<prefix><data_project>/artefacts/`` is never processed (``needs_decision``).

Per artefact deposit (design 5.2): settle (two identical listings, ``settle_seconds``), scope (the
collection is live, the dataset is served and not inactive, each ``made_from`` version exists; its h5ad
sha256 is filled from the dataset's changelog), hash (a streaming GET pinned to the listed ETag, sha256 and,
for single-part objects, MD5 against the ETag), QC (section 6.1: names, sizes, extension and magic bytes,
title, description, provenance, secrets and donor identifiers held, public scopes held, ZIP central
directory), then under the scope's writer lock: the version (``max(every version, purged ones too) + 1``),
a resume marker, the server-side copies to ``<catalog prefix><Group>/static/artefacts/.../v<N>/files/<name>``
(``CopySourceIfMatch``, ``x-amz-meta-sha256``, HEAD-checked; a key that already exists with other bytes stops
the item), previews and inline copies, the manifest (written last on S3: its existence makes the version
real), the head record; then the rescan of the artefact index, the served check, the git commit of the head
record and manifest, and the guarded delete of the incoming objects.

Collection deposits place the rendered ``index.md`` in a new ``<Group>/`` folder. Purges (design 5.3) are
requests of the API in ``artefact_purges``: only keys listed in the version's manifest are deleted, each
HEAD-compared with the manifest sha256, one at a time, logged; then the tombstone.

Input: the listing, ``deposits.sqlite``, the store tree, S3. Output: the store tree, S3 objects, rows of the
watcher's ``ingest_watch.sqlite`` (key ``artefact:<deposit_id>``) mirrored into the deposit record.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import artefact_deposit as rules
import artefacts
import dataset_deposit
from ingest_pipeline import ObjInfo, TransientError, delete_consumed, ensure_inside, writer_lock

FINAL = ('live', 'held', 'failed', 'needs_decision', 'rejected')
MAX_ATTEMPTS = 8
MARKER_DIR = 'artefact_resume'
REPORT_DIR = 'artefact_reports'
CHUNK = 8 * 1024 * 1024
HEAD_BYTES = 64 * 1024
PREVIEW_PIXELS = 512
ZIP_MAX_RATIO = 20
ZIP_MAX_TOTAL = 1024 ** 4


@dataclass
class Outcome:
    state: str = 'failed'
    detail: str = ''
    version: Optional[int] = None
    warnings: List[str] = field(default_factory=list)
    held_reasons: List[str] = field(default_factory=list)
    report: Optional[Dict[str, Any]] = None
    consumed: Dict[str, str] = field(default_factory=dict)
    served: bool = False
    git_commit: Optional[str] = None


class Stop(Exception):
    """The item stops in ``state`` (failed, held or needs_decision) with ``message``."""

    def __init__(self, state: str, message: str, report: Optional[Dict[str, Any]] = None,
                 held: Optional[List[str]] = None):
        super().__init__(message)
        self.state = state
        self.report = report
        self.held = held or []


def _today() -> str:
    return _dt.date.today().isoformat()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _meta_of(head: Dict[str, Any]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (head.get('Metadata') or {}).items()}


def _code(exc: BaseException) -> str:
    response = getattr(exc, 'response', None) or {}
    return str((response.get('Error') or {}).get('Code') or type(exc).__name__)


def _absent(exc: BaseException) -> bool:
    return _code(exc) in ('404', 'NoSuchKey', 'NotFound')


#: endings a browser runs or renders as active content: stored as octet-stream, attachment (review C8)
ACTIVE_ENDINGS = ('.html', '.htm', '.xhtml', '.svg', '.js', '.mjs', '.xml', '.qmd', '.rmd', '.ipynb')


def stored_type(name: str) -> Tuple[str, str]:
    """(Content-Type, Content-Disposition) the object is stored with. Active types never keep their own type."""
    filename = PurePosixPath(name).name
    disposition = f'attachment; filename="{filename}"'
    if rules.file_extension(name) in ACTIVE_ENDINGS:
        return 'application/octet-stream', disposition
    return rules.content_type_for(name), disposition


SCAN_CHUNK = 1024 * 1024
SCAN_OVERLAP = 16 * 1024
DONOR_ROW_CAP = 2_000_000
#: endings the donor identifier scan reads (review C7: also parquet, xlsx, json, text)
DONOR_SCAN_ENDINGS = ('.tsv', '.csv', '.tsv.gz', '.csv.gz', '.txt', '.json', '.parquet', '.xlsx', '.yaml', '.yml',
                      '.md')
TOKEN_RE = __import__('re').compile(r'[A-Za-z0-9][A-Za-z0-9:_.\-]{2,}')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(SCAN_CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def scan_secrets(path: Path) -> List[str]:
    """The secret scan over a file in chunks with an overlap, never the whole file in memory (review S4)."""
    found: set = set()
    tail = b''
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(SCAN_CHUNK), b''):
            window = tail + chunk
            found.update(rules.secret_findings(window))
            tail = window[-SCAN_OVERLAP:]
            newline = tail.find(b'\n')
            if newline >= 0:              # the next window starts at a line start (the patterns anchor on ^)
                tail = tail[newline + 1:]
    return sorted(found)


def _token_hits(values: set, tokens) -> set:
    return {token for token in tokens if token in values}


def donor_hits(path: Path, name: str, values: set, row_cap: int = DONOR_ROW_CAP) -> Tuple[int, bool]:
    """Distinct crosswalk values (aliases and canonical uids, lower case) found in a table or text file, and
    whether the row cap cut the scan. Streamed; tables cell by cell, text files token by token, parquet through
    DuckDB, xlsx from its shared strings and sheets (never extracted to disk)."""
    import csv  # noqa: PLC0415
    import gzip  # noqa: PLC0415
    lower = name.lower()
    hits: set = set()
    rows = 0
    try:
        if lower.endswith(('.tsv', '.csv', '.tsv.gz', '.csv.gz')):
            handle = gzip.open(path, 'rt', encoding='utf-8', errors='replace', newline='') if lower.endswith('.gz') \
                else open(path, 'r', encoding='utf-8', errors='replace', newline='')
            with handle:
                for row in csv.reader(handle, delimiter='\t' if '.tsv' in lower else ','):
                    rows += 1
                    if rows > row_cap:
                        return len(hits), True
                    hits.update(cell.strip().lower() for cell in row if cell.strip().lower() in values)
        elif lower.endswith('.parquet'):
            import duckdb  # noqa: PLC0415
            connection = duckdb.connect()
            try:
                cursor = connection.execute('SELECT * FROM read_parquet(?) LIMIT ?', [str(path), row_cap + 1])
                while True:
                    batch = cursor.fetchmany(10000)
                    if not batch:
                        break
                    for row in batch:
                        rows += 1
                        if rows > row_cap:
                            return len(hits), True
                        hits.update(str(cell).strip().lower() for cell in row
                                    if cell is not None and str(cell).strip().lower() in values)
            finally:
                connection.close()
        elif lower.endswith('.xlsx'):
            import xml.etree.ElementTree as ElementTree  # noqa: PLC0415
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if not (member == 'xl/sharedStrings.xml' or member.startswith('xl/worksheets/')):
                        continue
                    with archive.open(member) as handle:
                        for _event, element in ElementTree.iterparse(handle):
                            if element.tag.endswith('}t') or element.tag.endswith('}v'):
                                rows += 1
                                if rows > row_cap:
                                    return len(hits), True
                                text = (element.text or '').strip().lower()
                                if text in values:
                                    hits.add(text)
                            element.clear()
        else:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    rows += 1
                    if rows > row_cap:
                        return len(hits), True
                    hits.update(_token_hits(values, (token.lower() for token in TOKEN_RE.findall(line))))
    except (OSError, EOFError, zipfile.BadZipFile, ValueError) as exc:
        raise Stop('failed', f'{name} could not be read for the donor identifier scan ({exc})')
    except Exception as exc:  # noqa: BLE001 - e.g. a parquet DuckDB cannot read: never a silent pass
        raise Stop('failed', f'{name} could not be read for the donor identifier scan ({type(exc).__name__})')
    return len(hits), False


class RangedObject(io.RawIOBase):
    """A seekable read-only view of one S3 object through ranged GETs pinned to its ETag (zip central directory
    of an archive that is not kept on the watcher's disk)."""

    def __init__(self, s3: Any, bucket: str, key: str, etag: str, size: int):
        super().__init__()
        self.s3, self.bucket, self.key, self.etag, self.size = s3, bucket, key, etag, size
        self.position = 0
        self.ranges = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.position, 2: self.size}[whence]
        self.position = max(0, min(self.size, base + offset))
        return self.position

    def read(self, size: int = -1) -> bytes:
        if self.position >= self.size:
            return b''
        end = self.size if size is None or size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b''
        self.ranges += 1
        if self.ranges > 1000:
            raise OSError('too many ranged reads for one archive')
        response = self.s3.get_object(Bucket=self.bucket, Key=self.key, IfMatch=f'"{self.etag}"',
                                      Range=f'bytes={self.position}-{end - 1}')
        data = response['Body'].read()
        self.position += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class DeApi:
    """release / restore of a dataset in ant-serve around a DE import (the publisher's API client)."""

    def __init__(self, api: Any) -> None:
        self.api = api

    def release(self, db_id: str) -> None:
        if self.api is not None:
            self.api.post_with_retry(f'/admin/release/{db_id}')

    def restore(self, db_id: str) -> None:
        if self.api is not None:
            self.api.post_with_retry(f'/admin/restore/{db_id}')


#: the columns of ``__de`` a study fingerprint covers (what the served DE rows say)
FINGERPRINT_COLUMNS = ('term', 'contrast', 'gene_id', 'log2fc', 'zeff', 'effsize', 'efftype', 'pvalue', 'padj',
                       'estimate', 'SE', 'ci_low', 'ci_high')


def study_fingerprints(duckdb_path: Path, studies: Sequence[str]) -> Dict[str, str]:
    """``{study: "<rows>:<sum of row hashes>"}`` of the studies present in a dataset's duckdb (read-only).

    The fingerprint identifies which import wrote the rows a study serves now (review N2): every de_table
    version records the fingerprint of what it imported, and a purge compares. Order independent; computed
    by the same DuckDB that compares it (a DuckDB upgrade that changes ``hash`` makes every comparison differ,
    which stops a purge for an admin, never removes the wrong rows)."""
    import duckdb  # noqa: PLC0415
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
        if '__de' not in tables:
            return {}
        columns = ', '.join(FINGERPRINT_COLUMNS)
        out = {}
        for study in studies:
            count, total = connection.execute(
                f'SELECT count(*), coalesce(sum(hash({columns})::HUGEINT), 0) FROM __de WHERE study_id = ?',
                [study]).fetchone()
            if count:
                out[study] = f'{int(count)}:{int(total)}'
        return out
    finally:
        connection.close()


@dataclass
class FileCheck:
    """What the hash step learnt about one file."""
    name: str
    size: int
    sha256: str
    etag: str
    head: bytes
    tail: bytes
    local: Optional[Path]          # the bytes in the work dir (inline copies and content checks)


class ArtefactPublisher:
    """One poll of the artefact route (see the module docstring)."""

    def __init__(self, *, store_root: Path, state_dir: Path, work_root: Path, s3: Any, bucket: str,
                 incoming_prefix: str, catalog_prefix: str, artefact_settings: Dict[str, Any], db: Any,
                 api: Any = None, settle_seconds: float = 120, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, git_commit: bool = True,
                 git_identity: str = 'anthive ingest-watch <noreply@anthive.local>',
                 delete_incoming: bool = True, notify_settings: Optional[Dict[str, Any]] = None,
                 stop_requested: Callable[[], bool] = lambda: False, server_version: str = '',
                 site_id: str = '', run: Optional[Callable[..., Any]] = None, de_runner: Any = None,
                 dataset_catalog_prefix: Optional[str] = None, update_policy: str = 'own'):
        self.store_root = Path(store_root)
        self.state_dir = Path(state_dir)
        self.work_root = Path(work_root)
        self.s3 = s3
        self.bucket = bucket
        self.prefix = incoming_prefix
        self.catalog_prefix = catalog_prefix
        self.settings = dict(artefact_settings)
        self.db = db
        self.api = api
        self.settle_seconds = float(settle_seconds)
        self.log = log
        self.clock = clock
        self.git_commit = git_commit
        self.git_identity = git_identity
        self.delete_incoming = delete_incoming
        self.notify_settings = notify_settings or {}
        self.stop_requested = stop_requested
        self.server_version = server_version
        self.site_id = site_id
        self.run = run or (lambda cmd: subprocess.run([str(part) for part in cmd], capture_output=True, text=True))
        #: runs ant-de-ingest for de_table artefacts (``run(label, cmd, cwd)`` -> ``(code, output)``)
        if de_runner is None:
            from ingest_pipeline import SubprocessRunner  # noqa: PLC0415
            de_runner = SubprocessRunner(log_path=self.state_dir / 'logs' / 'artefact_de.log')
        self.de_runner = de_runner
        #: where a dataset's ``.de.parquet`` lives in the bucket (the dataset route's catalog prefix)
        self.dataset_catalog_prefix = catalog_prefix if dataset_catalog_prefix is None else dataset_catalog_prefix
        self._marks: Dict[Tuple[str, str], str] = {}
        #: ``datasets_deposit_update_policy``: who may change a dataset through a de_table (review N1)
        self.update_policy = update_policy
        self._store: Any = None

    # ── the poll ────────────────────────────────────────────────────────────
    def poll(self, objects: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        listing = {obj['Key']: ObjInfo.from_listing(obj) for obj in objects}
        try:
            store = dataset_deposit.DepositStore.open_if_exists(self.state_dir)
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise TransientError(f'the deposit store cannot be opened ({exc}); artefacts not polled') from exc
        if store is None:
            self.refuse_unlinked(None, listing)
            return counts
        self._store = store
        try:
            store.conn.executescript(rules.PURGE_SCHEMA)
            for state, number in self.process_purges(store).items():
                counts[f'purge_{state}'] = counts.get(f'purge_{state}', 0) + number
            records = store._rows("SELECT * FROM deposits WHERE deposit_kind IN ('artefact', 'collection') AND "
                                  "state = 'uploaded' ORDER BY completed")
            for record in records:
                if self.stop_requested():
                    break
                state = self.handle(store, record, listing)
                if state:
                    counts[state] = counts.get(state, 0) + 1
            self.refuse_unlinked(store, listing)
        finally:
            self._store = None
            store.close()
        return counts

    def refuse_unlinked(self, store: Any, listing: Dict[str, ObjInfo]) -> None:
        """Objects under ``<prefix><data_project>/artefacts/`` that no completed deposit accounts for (a
        replayed URL, a leftover, a hand upload): recorded once as needs_decision, never processed."""
        accounted: Dict[str, str] = {}
        if store is not None:
            for row in store._rows("SELECT f.key, f.etag FROM files f JOIN deposits d ON d.id = f.deposit_id WHERE "
                                   "d.deposit_kind IN ('artefact', 'collection') AND d.state IN ('uploaded', "
                                   "'completing', 'uploading', 'withdrawing') AND f.etag IS NOT NULL"):
                accounted[row['key']] = row['etag']
            open_keys = store.held_keys()
        else:
            open_keys = {}
        for key, obj in listing.items():
            parsed = rules.deposit_of_incoming_key(self.prefix, key)
            if parsed is None or key in open_keys or accounted.get(key) == obj.etag:
                continue
            row_key = f'artefact-unlinked:{key}'
            row = self.db.get(row_key)
            if row and row.get('processed_signature') == obj.etag:
                continue
            message = (f'{key} is under the artefact folder of incoming but no completed deposit accounts for it '
                       f'(a replayed URL or a leftover); never processed. Delete it after a look.')
            self.db.transition(row_key, 'needs_decision', message, error=message, processed_signature=obj.etag,
                               data_project=parsed[0], basename=parsed[2], kind='artefact')
            self.log(f'artefacts: {message}')

    # ── one deposit ─────────────────────────────────────────────────────────
    def handle(self, store: Any, record: Dict[str, Any], listing: Dict[str, ObjInfo]) -> Optional[str]:
        key = record['watch_key']
        row = self.db.get(key)
        files = store.files(record['id'])
        if row and row['state'] == 'live':
            if not row.get('deleted'):
                self._delete_incoming(key, json.loads(row.get('consumed') or '{}'))
            return None
        if row and row['state'] in FINAL:
            return None
        objs = [listing.get(entry['key']) for entry in files]
        signature = json.dumps(sorted([entry['key'], entry['etag'], entry['size']] for entry in files)
                               + [['deposit', record['id'], 0]])
        base = {'data_project': record['data_project'], 'basename': record['basename'], 'last_seen': self.clock(),
                'kind': record['deposit_kind'], 'db_id': record.get('artefact_ref')}
        if not files or any(obj is None or obj.etag != entry['etag'] or obj.size != entry['size']
                            for obj, entry in zip(objs, files)):
            message = 'files of this deposit are missing or changed in incoming; withdraw it and deposit again'
            self._finish(store, record, key, Outcome(state='needs_decision', detail=message),
                         signature, first=row is None, base=base)
            return 'needs_decision'
        if row is None:
            self.db.transition(key, 'seen', 'first listed', content_signature=signature, settle_signature=signature,
                               first_seen=self.clock(), polls_unchanged=0, attempts=0, next_attempt=0, **base)
            return None
        if row['settle_signature'] != signature:
            self.db.upsert(key, settle_signature=signature, content_signature=signature, polls_unchanged=0,
                           state='seen', approval=None, **base)
            return None
        newest = max(obj.last_modified for obj in objs)
        if self.clock() - newest < self.settle_seconds:
            return None
        if row['state'] == 'seen':
            self.db.transition(key, 'stable', 'upload complete (two identical listings)', error=None)
        if float(row.get('next_attempt') or 0) > self.clock():
            return None
        try:
            for obj in objs:
                mark = self._deposit_mark(obj)
                if mark != record['id']:
                    message = f'{obj.key} does not carry the mark of {record["id"]}; refused'
                    self._finish(store, record, key, Outcome(state='needs_decision', detail=message), signature)
                    return 'needs_decision'
        except TransientError as exc:
            self.log(f'artefacts: {record["id"]} waits: {exc}')
            return None
        approval = json.loads(row['approval']) if row.get('approval') else None
        if approval and approval.get('signature') != signature:
            approval = None
        self.db.transition(key, 'pulled', 'processing started' + (' (approved)' if approval else ''))
        try:
            if record['deposit_kind'] == 'collection':
                outcome = self.publish_collection(record, files, objs, approval)
            else:
                outcome = self.publish_artefact(record, files, objs, approval)
        except TransientError as exc:
            attempts = int(row.get('attempts') or 0) + 1
            if attempts >= MAX_ATTEMPTS:
                detail = self.abandon_after_error(record, f'gave up after {attempts} attempts: {exc}')
                self._finish(store, record, key, Outcome(state='failed', detail=detail), signature)
                return 'failed'
            delay = min(3600, 60 * 2 ** attempts)
            self.db.transition(key, 'stable', f'retry in {delay} s: {exc}', attempts=attempts,
                               next_attempt=self.clock() + delay, error=str(exc))
            return 'retry'
        except Exception as exc:  # noqa: BLE001 - a bug: recorded, never crash the poll
            outcome = Outcome(state='failed', detail=self.abandon_after_error(record, f'{type(exc).__name__}: {exc}'))
        self._finish(store, record, key, outcome, signature, approval=approval)
        if outcome.state == 'live':
            self._delete_incoming(key, outcome.consumed)
        return outcome.state

    def _finish(self, store: Any, record: Dict[str, Any], key: str, outcome: Outcome, signature: str,
                first: bool = False, base: Optional[Dict[str, Any]] = None, approval: Any = None) -> None:
        report_path = None
        if outcome.report is not None:
            folder = self.state_dir / REPORT_DIR
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'{record["id"]}.json'
            partial = path.with_suffix('.json.partial')
            partial.write_text(json.dumps(outcome.report, indent=1, default=str))
            os.replace(partial, path)
            report_path = str(path)
        fields = dict(base or {})
        fields.update(processed_signature=signature, content_signature=signature,
                      error=None if outcome.state == 'live' else outcome.detail,
                      warnings=json.dumps(outcome.warnings), held_reasons=json.dumps(outcome.held_reasons),
                      version=f'v{outcome.version}' if outcome.version and outcome.state == 'live' else None,
                      report_path=report_path, consumed=json.dumps(outcome.consumed), served=int(outcome.served),
                      catalog_verified=int(outcome.state == 'live'), git_commit=outcome.git_commit, attempts=0,
                      next_attempt=0, approval=json.dumps(approval) if approval else None)
        if first:
            fields.setdefault('first_seen', self.clock())
            fields.setdefault('settle_signature', signature)
        self.db.transition(key, outcome.state, outcome.detail or outcome.state, **fields)
        row = self.db.get(key) or {}
        roots = {'store': self.store_root, 'state': self.state_dir, 'work': self.work_root, 'bucket': self.bucket}
        try:
            copy = dataset_deposit.write_report_copy(self.state_dir, record['id'], report_path, roots)
            store.record_watch(record['id'], row, roots, report_file=str(copy) if copy else None)
            store.event(record['id'], 'watcher', outcome.state,
                        str(dataset_deposit.sanitize_text(outcome.detail or row.get('version') or '', roots)))
            marker = f'{outcome.state}:{signature}'
            if record.get('notified_state') != marker and outcome.state in dataset_deposit.NOTIFY_DEPOSITOR_ON:
                import ingest_watch  # noqa: PLC0415
                summary = dataset_deposit.report_summary(outcome.report) if outcome.report else None
                if ingest_watch.notify_depositor({'notify': self.notify_settings}, record, row, summary, self.log):
                    store.update(record['id'], notified_state=marker)
        except Exception as exc:  # noqa: BLE001 - the record is a mirror; the watch row is the truth
            self.log(f'artefacts: could not mirror the outcome of {record["id"]}: {exc}')
        self.log(f'artefacts: {record["id"]} ({record.get("artefact_ref")}): {outcome.state}'
                 + (f' v{outcome.version}' if outcome.version and outcome.state == 'live' else '')
                 + (f': {outcome.detail}' if outcome.detail and outcome.state != 'live' else ''))

    def _deposit_mark(self, obj: ObjInfo) -> str:
        cache = (obj.key, obj.etag)
        if cache not in self._marks:
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=obj.key)
            except Exception as exc:  # noqa: BLE001
                raise TransientError(f'HEAD {obj.key} failed ({_code(exc)})') from exc
            if str(head.get('ETag', '')).strip('"') != obj.etag:
                raise TransientError(f'{obj.key} changed since it was listed')
            self._marks[cache] = _meta_of(head).get('deposit', '')
        return self._marks[cache]

    def _delete_incoming(self, key: str, consumed: Dict[str, str]) -> None:
        if not self.delete_incoming or not consumed:
            return
        results: Dict[str, str] = {}

        def record(s3_key: str, etag: str, result: str) -> None:
            results[s3_key] = result
            self.db.record_deletion(key, s3_key, etag, result)
        delete_consumed(self.s3, self.bucket, self.prefix, consumed, record)
        gone = all(results.get(s3_key) in ('deleted', 'skipped:absent') for s3_key in consumed)
        self.db.upsert(key, deleted=int(gone))

    # ── the store's view ────────────────────────────────────────────────────
    def _catalog(self) -> Tuple[Dict[str, artefacts.Collection], Dict[str, Path], Dict[str, str]]:
        """(collections by group id, served dataset .md by db_id, dataset folder by db_id) from the store."""
        from h5adstore import catalog_dataset_mds  # noqa: PLC0415
        mds: Dict[str, Path] = {}
        folders: Dict[str, str] = {}
        group_ids: Dict[str, set] = {}
        for _item, resolved, rel in catalog_dataset_mds(self.store_root):
            db_id = dataset_deposit.dataset_db_id(self.store_root, rel.parts[0], resolved.stem) \
                if len(rel.parts) == 2 else self._nested_db_id(resolved, rel)
            if not db_id:
                continue
            mds[db_id] = resolved
            folders[db_id] = rel.parts[0]
            group_ids.setdefault(rel.parts[0], set()).add(db_id.split('/')[0])
        collections, _problems = artefacts.scan_collections(self.store_root, group_ids)
        return collections, mds, folders

    @staticmethod
    def _nested_db_id(md: Path, rel: PurePosixPath) -> Optional[str]:
        meta = rules.read_md_meta(md)
        index = rules.read_md_meta(md.parent / 'index.md')
        group_id, file_id = index.get('group_id'), meta.get('id')
        return f'{group_id}/{file_id}' if group_id and file_id else None

    def _scope_lock_path(self, path: artefacts.ArtefactPath, scope_md: Optional[Path]) -> Path:
        """The artefact lock of a scope: ``<stem>.artefacts.lock`` next to the dataset, or
        ``static/artefacts/_group.lock``. The duckdb's own writer lock is taken only around a DE import or study
        removal (review C9: placement and purge never block annotation writers)."""
        if scope_md is not None:
            return scope_md.with_suffix('.artefacts')
        base = self.store_root / path.group / artefacts.ARTEFACTS_PARTS[0] / artefacts.ARTEFACTS_PARTS[1]
        base.mkdir(parents=True, exist_ok=True)
        return base / artefacts.GROUP_SCOPE

    def _artefacts_root(self, group: str) -> Path:
        root = self.store_root / group / artefacts.ARTEFACTS_PARTS[0] / artefacts.ARTEFACTS_PARTS[1]
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _write_local(self, rel: PurePosixPath, data: bytes, group: str) -> Path:
        """Write a store file under ``<Group>/static/artefacts/`` atomically (temp file renamed over it)."""
        target = self.store_root / str(rel)
        ensure_inside(target, self._artefacts_root(group))
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_inside(target, self._artefacts_root(group))       # again, now that the folders exist
        partial = target.with_name(f'.{target.name}.partial')
        partial.write_bytes(data)
        os.replace(partial, target)
        return target

    # ── markers ─────────────────────────────────────────────────────────────
    def _marker_path(self, deposit_id: str) -> Path:
        if not dataset_deposit.DEPOSIT_ID_RE.fullmatch(deposit_id):
            raise ValueError(deposit_id)
        return self.state_dir / MARKER_DIR / f'{deposit_id}.json'

    def load_marker(self, deposit_id: str) -> Optional[Dict[str, Any]]:
        path = self._marker_path(deposit_id)
        try:
            return json.loads(path.read_text()) if path.exists() else None
        except ValueError:
            return None

    def save_marker(self, deposit_id: str, marker: Dict[str, Any]) -> None:
        path = self._marker_path(deposit_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix('.json.partial')
        partial.write_text(json.dumps(marker, indent=1))
        os.replace(partial, path)

    def drop_marker(self, deposit_id: str) -> None:
        self._marker_path(deposit_id).unlink(missing_ok=True)

    # ── hashing and QC ──────────────────────────────────────────────────────
    def _hash(self, obj: ObjInfo, entry: Dict[str, Any], keep: Optional[Path]) -> FileCheck:
        """Stream the object pinned to its listed ETag; sha256 (and MD5 for single-part ETags) on the stream."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=obj.key, IfMatch=f'"{obj.etag}"')
        except Exception as exc:  # noqa: BLE001
            if _code(exc) in ('PreconditionFailed', '412'):
                raise Stop('needs_decision', f'{entry["name"]} changed in incoming while it was read')
            raise TransientError(f'GET {entry["name"]} failed ({_code(exc)})') from exc
        sha, md5 = hashlib.sha256(), hashlib.md5()
        head = b''
        tail = b''
        size = 0
        handle = None
        if keep is not None:
            keep.parent.mkdir(parents=True, exist_ok=True)
            handle = keep.open('wb')
        try:
            body = response['Body']
            while True:
                chunk = body.read(CHUNK)
                if not chunk:
                    break
                sha.update(chunk)
                md5.update(chunk)
                size += len(chunk)
                if len(head) < HEAD_BYTES:
                    head += chunk[:HEAD_BYTES - len(head)]
                tail = (tail + chunk)[-8:]
                if handle is not None:
                    handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'reading {entry["name"]} failed ({exc})') from exc
        finally:
            if handle is not None:
                handle.close()
        if '-' not in obj.etag and md5.hexdigest() != obj.etag:
            raise Stop('needs_decision', f'{entry["name"]}: the bytes read are not the listed object (MD5)')
        return FileCheck(entry['name'], size, sha.hexdigest(), obj.etag, head, tail, keep)

    def _keep_bytes(self, kind: str, name: str, size: int) -> bool:
        """Read into the work dir: inline files and files whose content the checks read."""
        ending = rules.file_extension(name)
        limit = float(self.settings.get('artefacts_content_check_max_gb', 2)) * 1024 ** 3
        if kind == 'de_table':
            return size <= limit              # read by the DE checks and the import (larger: the check fails)
        inline = (kind == 'figure' and size <= limit) \
            or size <= float(self.settings.get('artefacts_inline_max_mb', 2)) * 1024 ** 2
        content = ending in rules.TEXT_EXTENSIONS or ending in rules.TABLE_EXTENSIONS or ending == '.zip' \
            or ending in DONOR_SCAN_ENDINGS or kind in ('notebook', 'figure')
        return inline or (content and size <= float(self.settings.get('artefacts_content_check_max_gb', 2)) * 1024 ** 3)

    def _qc(self, record: Dict[str, Any], block: Dict[str, Any], checks: List[FileCheck], declared_rows: List[Dict[str, Any]],
            public: bool, approval: Optional[Dict[str, Any]],
            extra: Optional[List[Dict[str, Any]]] = None,
            extra_held: Sequence[Tuple[str, str]] = (),
            objs_by_name: Optional[Dict[str, ObjInfo]] = None) -> Dict[str, Any]:
        """Section 6.1 generic checks plus ``extra`` (the DE checks of section 6.2). Returns the report; raises
        Stop for errors and held reasons."""
        kind = block['kind']
        objs_by_name = objs_by_name or {}
        results: List[Dict[str, Any]] = []

        def add(check_id: str, status: str, summary: str, fix: str = '') -> None:
            results.append({'id': check_id, 'status': status, 'summary': summary, 'fix': fix})
        names = [entry['name'] for entry in declared_rows]
        name_problems = [problem for problem in (artefacts.file_name_problem(name) for name in names) if problem]
        name_problems += [f'{clash}: differs from another file only in case' for clash in artefacts.casefold_clashes(names)]
        add('files.names', 'fail' if name_problems else 'pass', '; '.join(name_problems) or 'names are safe')
        max_files = int(self.settings.get('artefacts_max_files', 50))
        max_file = float(self.settings.get('artefacts_max_file_gb', 100)) * 1024 ** 3
        max_total = float(self.settings.get('artefacts_max_deposit_gb', 200)) * 1024 ** 3
        size_problems = []
        if len(checks) > max_files:
            size_problems.append(f'{len(checks)} files (at most {max_files})')
        size_problems += [f'{check.name}: {check.size} bytes is over the file limit' for check in checks
                          if check.size > max_file]
        if sum(check.size for check in checks) > max_total:
            size_problems.append('the deposit is over its size limit')
        add('files.limits', 'fail' if size_problems else 'pass', '; '.join(size_problems) or 'within the limits')
        mismatched = [f'{check.name}: declared {row["size"]} bytes / {row["sha256"][:12]}, read {check.size} bytes / '
                      f'{check.sha256[:12]}' for check, row in zip(checks, declared_rows)
                      if check.size != int(row['size']) or check.sha256 != row['sha256']]
        add('files.sha256', 'fail' if mismatched else 'pass', '; '.join(mismatched) or 'sizes and sha256 match',
            'compute the sha256 of the files you upload' if mismatched else '')
        type_problems = []
        for check in checks:
            problem = rules.extension_problem(kind, check.name) or rules.magic_problem(check.name, check.head,
                                                                                       check.tail, check.size)
            if problem:
                type_problems.append(problem)
            elif rules.file_extension(check.name) == '.zip' or rules.file_extension(check.name) in ('.xlsx', '.docx'):
                problem = self._zip_problem(check, objs_by_name.get(check.name))
                if problem:
                    type_problems.append(problem)
        add('files.type', 'fail' if type_problems else 'pass', '; '.join(type_problems) or 'types agree with endings')
        text_problems = []
        if not rules.TITLE_MIN_CHARS <= len(block.get('title') or '') <= artefacts.TITLE_MAX_CHARS:
            text_problems.append('title length')
        if len((block.get('description') or '').strip()) < rules.DESCRIPTION_MIN_CHARS:
            text_problems.append('description shorter than 50 characters')
        add('meta.text', 'fail' if text_problems else 'pass', '; '.join(text_problems) or 'title and description')
        missing = rules.provenance_missing(block.get('declared'))
        if block.get('alias_of') == 'de_only':
            missing = [item for item in missing if not item.startswith('declared.software')]
        if missing:
            strict = kind in rules.PROVENANCE_STRICT_KINDS and block.get('alias_of') != 'de_only'
            add('meta.provenance', 'fail' if strict else 'warn',
                'provenance incomplete: ' + ', '.join(missing), 'vark artefacts add --code auto --software auto')
        else:
            add('meta.provenance', 'pass', 'author, date, software and code declared')
        held: List[str] = []
        secrets_found = []
        donors_found = []
        crosswalk_values = self._crosswalk_values()
        not_scanned = []
        for check in checks:
            ending = rules.file_extension(check.name)
            local = check.local if check.local is not None and check.local.exists() else None
            scannable = ending in rules.TEXT_EXTENSIONS or ending in DONOR_SCAN_ENDINGS or ending == '.mus'
            if local is None:
                if scannable:
                    not_scanned.append(check.name)   # review C7: held, never a silent pass
                continue
            if ending in rules.TEXT_EXTENSIONS or ending == '.mus':
                found = scan_secrets(local)
                if found:
                    secrets_found.append(f'{check.name}: {", ".join(found)}')
            if ending in DONOR_SCAN_ENDINGS and crosswalk_values:
                hits, truncated = donor_hits(local, check.name, crosswalk_values)
                if hits:
                    donors_found.append(f'{check.name}: {hits} value(s)')
                if truncated:
                    add('content.donors_cap', 'warn', f'{check.name}: only the first {DONOR_ROW_CAP} rows were '
                                                      f'scanned for donor identifiers')
        if not_scanned:
            held.append('not_scanned')
            add('content.not_scanned', 'held', 'larger than the content check limit, not scanned: '
                + ', '.join(not_scanned), 'an admin looks at the files, or raise artefacts_content_check_max_gb')
        if secrets_found:
            held.append('possible_secret')
            add('content.secrets', 'held', '; '.join(secrets_found), 'remove the secret and deposit a new version')
        else:
            add('content.secrets', 'pass', 'no key, token or password found')
        if donors_found:
            held.append('donor_identifiers')
            add('content.donors', 'held', '; '.join(donors_found), 'replace internal donor ids by public ones')
        else:
            add('content.donors', 'pass', 'no donor crosswalk values' if crosswalk_values else
                'no donor crosswalk on this site')
        if public and self.settings.get('artefacts_hold_public', True):
            held.append('public_access')
            add('access.public', 'held', 'the artefact will be public: an admin publishes it')
        results.extend(extra or [])
        if any(entry['status'] == 'held' for entry in extra or []):
            held.append('de_not_checked')
        for reason, summary in extra_held:
            held.append(reason)
            add(reason.replace('_', '.', 1), 'held', summary)
        approved = set((approval or {}).get('reasons') or [])
        remaining = [reason for reason in held if reason not in approved]
        failed = [entry for entry in results if entry['status'] == 'fail']
        report = {'schema': 'anthive.artefact_qc/1', 'deposit_id': record['id'], 'ref': record.get('artefact_ref'),
                  'result': 'fail' if failed else ('held' if remaining else 'pass'),
                  'publish': not failed and not remaining, 'held_reasons': remaining,
                  'counts': {status: sum(entry['status'] == status for entry in results)
                             for status in ('pass', 'warn', 'fail', 'held')},
                  'checks': results}
        if failed:
            raise Stop('failed', 'checks failed: ' + '; '.join(entry['summary'] for entry in failed)[:2000], report)
        if remaining:
            raise Stop('held', 'held for an admin: ' + ', '.join(remaining), report, held=remaining)
        return report

    def _zip_problem(self, check: FileCheck, obj: Optional[ObjInfo] = None) -> Optional[str]:
        """Central directory only, never extracted: names without ``..`` or absolute paths, sane totals. An
        archive not kept in the work dir is read with ranged GETs pinned to its ETag (review C7)."""
        source: Any = check.local
        if source is None:
            if obj is None:
                return f'{check.name}: the archive could not be read for its central directory'
            source = RangedObject(self.s3, self.bucket, obj.key, obj.etag, check.size)
        try:
            with zipfile.ZipFile(source) as archive:
                total = 0
                for info in archive.infolist():
                    name = info.filename
                    if name.startswith(('/', '\\')) or '..' in name.replace('\\', '/').split('/') or ':' in name[:3]:
                        return f'{check.name}: entry {name[:80]!r} escapes the archive'
                    total += info.file_size
        except (zipfile.BadZipFile, OSError) as exc:
            return f'{check.name}: not a readable ZIP ({exc})'
        if total > ZIP_MAX_TOTAL or total > ZIP_MAX_RATIO * max(1, check.size) and total > 100 * 1024 ** 2:
            return f'{check.name}: uncompresses to {total} bytes (more than {ZIP_MAX_RATIO} times its size)'
        return None

    def _crosswalk_values(self) -> set:
        path = self.store_root / 'donor_crosswalk.tsv'
        if not path.is_file():
            return set()
        from obs_derive_forms import load_crosswalk  # noqa: PLC0415
        try:
            crosswalk = load_crosswalk(path)
        except Exception:  # noqa: BLE001 - an unreadable crosswalk: no scan (recorded as such)
            return set()
        values = set()
        for table in crosswalk.by_cohort.values():
            for alias, uid in table.items():
                for value in (alias, uid):
                    if value and len(str(value)) >= 4:
                        values.add(str(value).strip().lower())
        return values

    # ── publish an artefact ─────────────────────────────────────────────────
    def publish_artefact(self, record: Dict[str, Any], files: List[Dict[str, Any]], objs: List[ObjInfo],
                         approval: Optional[Dict[str, Any]]) -> Outcome:
        try:
            return self._publish_artefact(record, files, objs, approval)
        except Stop as stop:
            return Outcome(state=stop.state, detail=str(stop), report=stop.report, held_reasons=stop.held)

    def _publish_artefact(self, record: Dict[str, Any], files: List[Dict[str, Any]], objs: List[ObjInfo],
                          approval: Optional[Dict[str, Any]]) -> Outcome:
        block = json.loads(record.get('artefact') or '{}')
        group, group_id, scope = record['data_project'], block.get('group_id'), block.get('scope')
        kind, artefact_id = block.get('kind'), block.get('artefact_id')
        try:
            path = artefacts.ArtefactPath(group, scope, kind, artefact_id)
        except (TypeError, ValueError) as exc:
            raise Stop('failed', f'the deposit names no valid artefact ({exc})')
        if kind in rules.DEFERRED_KINDS:
            raise Stop('failed', f'{kind} artefacts are not accepted yet')
        ref = artefacts.format_ref(group_id, scope, artefact_id)
        outcome = Outcome()
        # scope ---------------------------------------------------------------
        collections, mds, folders = self._catalog()
        collection = collections.get(group_id)
        if collection is None or collection.folder != group:
            raise Stop('failed', f'the collection {group} ({group_id}) is not live on this site')
        scope_db_id = None if scope == artefacts.GROUP_SCOPE else f'{group_id}/{scope}'
        scope_md = None
        if scope_db_id is not None:
            scope_md = mds.get(scope_db_id)
            if scope_md is None or folders.get(scope_db_id) != group:
                raise Stop('failed', f'the dataset {scope_db_id} is not served (removed or inactive)')
        made_from = []
        for entry in block.get('made_from') or []:
            md = mds.get(entry.get('db_id'))
            meta = rules.read_md_meta(md) if md else {}
            sha = rules.h5ad_sha256_at(meta, str(entry.get('dataset_version'))) if md else None
            if md is None or str(entry.get('dataset_version')) not in rules.changelog_versions(meta):
                raise Stop('failed', f'made_from {entry.get("db_id")} {entry.get("dataset_version")}: no such '
                                     f'dataset version (any more)')
            if str(meta.get('version')) != str(entry.get('dataset_version')):
                outcome.warnings.append(f'made from {entry["db_id"]} {entry["dataset_version"]}; the dataset is now '
                                        f'{meta.get("version")}')
            made_from.append({'db_id': entry['db_id'], 'dataset_version': str(entry['dataset_version']),
                              'h5ad_sha256': sha})
        if scope_db_id is not None and scope_db_id not in {entry['db_id'] for entry in made_from}:
            raise Stop('failed', f'made_from does not include the scope dataset {scope_db_id}')
        if kind == 'de_table':
            self._require_dataset_update(record, scope_db_id)
        policy = artefacts.AccessPolicy(collections, self._mus_access(mds), self._folder_db_ids(folders))
        public = block.get('access') != 'internal' and (
            policy.dataset_public(scope_db_id) if scope_db_id else policy.collection_public(group_id))
        # review S1: a public artefact must not name internal datasets it was made from (held for an admin)
        hidden_sources = [entry['db_id'] for entry in made_from if not policy.dataset_public(entry['db_id'])] \
            if public else []
        extra_held = [('made_from_internal', f'the artefact will be public but was made from internal '
                                             f'dataset(s) {", ".join(hidden_sources)}: an admin decides (or deposit '
                                             f'it with access internal)')] if hidden_sources else []
        # the datasets the checks read, as they were before the checks (compared again under the lock)
        watched = [scope_db_id] if scope_db_id else []
        watched += [entry['db_id'] for entry in made_from]
        state_before = self._dataset_state(mds, watched)
        # hash and QC (skipped when a marker shows they passed before the placement started) --------------
        marker = self.load_marker(record['id'])
        work = self.work_root / f'artefact_{record["id"]}'
        checks: List[FileCheck] = []
        report = None
        if marker is None:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            for obj, entry in zip(objs, files):
                keep = work / entry['name'] if self._keep_bytes(kind, entry['name'], int(entry['size'])) else None
                if keep is not None:
                    ensure_inside(keep, work)
                checks.append(self._hash(obj, entry, keep))
            de_checks: List[Dict[str, Any]] = []
            de_tables: List[Dict[str, Any]] = []
            if kind == 'de_table':
                de_checks, de_tables = self._de_checks(scope_db_id, scope_md, files, work)
            report = self._qc(record, block, checks, files, public, approval, extra=de_checks,
                              extra_held=extra_held,
                              objs_by_name={entry['name']: obj for entry, obj in zip(files, objs)})
        # version, placement, manifest, head record: under the scope's writer lock ---------------------------
        lock = self._scope_lock_path(path, scope_md)
        with writer_lock(lock, timeout=600):
            head = self._read_head(path)
            published = [v for v in (head or {}).get('versions') or [] if v['state'] in ('live', 'purging', 'purged')]
            if head is not None and published:
                live = [v['version'] for v in head['versions'] if v['state'] == 'live']
                latest = max(live) if live else max(v['version'] for v in published)
                if marker is None:
                    if block.get('new_version_of') is None:
                        raise Stop('failed', f'artefact_exists: {ref} exists (latest {latest})')
                    if not artefacts.owned_by(head.get('owner'), record['user']) and not record['is_admin']:
                        raise Stop('failed', f'not_owner: new versions of {ref} are deposited by its owner or an '
                                             f'admin')
                    if block.get('new_version_of') != latest:
                        raise Stop('failed', f'version_conflict: the latest version of {ref} is {latest}, the '
                                             f'deposit was for {block.get("new_version_of")}')
                if head.get('kind') != kind:
                    raise Stop('failed', f'{ref} is a {head.get("kind")}; the kind cannot change')
            elif block.get('new_version_of') is not None and marker is None:
                raise Stop('failed', f'version_conflict: {ref} does not exist')
            if head is not None and head.get('kind') != kind:
                raise Stop('failed', f'{ref} is a {head.get("kind")}; the kind cannot change')
            if marker is None and self._dataset_state(mds, watched) != state_before:
                # a dataset changed while its checks ran (genes, versions): checked again at the next attempt
                raise TransientError('a dataset this artefact was checked against changed during the checks; '
                                     'they run again')
            if marker is None:
                existing_numbers = [v['version'] for v in (head or {}).get('versions') or []]
                if len(existing_numbers) >= int(self.settings.get('artefacts_max_versions', 100)):
                    raise Stop('failed', f'{ref} has the maximum number of versions')
                number = max(existing_numbers, default=0) + 1
                marker = {'deposit_id': record['id'], 'ref': ref, 'version': number, 'steps': [],
                          'started': _now_iso(),
                          'files': {check.name: {'sha256': check.sha256, 'size': check.size, 'etag': check.etag}
                                    for check in checks},
                          'made_from': made_from, 'warnings': outcome.warnings, 'report': report,
                          'de_tables': de_tables if kind == 'de_table' else None,
                          'approval': ({'by': approval.get('by'), 'reasons': approval.get('reasons') or [],
                                        'date': approval.get('date')} if approval else None)}
                self.save_marker(record['id'], marker)
                # the number is reserved in the head record before any copy (review S3): a failed placement
                # marks it failed and a later deposit takes the next number, never this one again
                head = self._reserve_version(path, head, record, number, block.get('access') or 'inherit')
            number = int(marker['version'])
            outcome.version = number
            outcome.warnings = list(marker.get('warnings') or outcome.warnings)
            report = marker.get('report') or report
            try:
                head = self._place_version(record, block, path, ref, group, group_id, number, kind, scope_db_id,
                                           scope_md, objs, files, work, marker, outcome, head)
            except Stop as stop:
                if stop.state == 'held':
                    raise
                reason = self._abandon_version(path, number, marker, record, str(stop), scope_md=scope_md,
                                               scope_db_id=scope_db_id)
                raise Stop(stop.state, reason, stop.report, held=stop.held) from stop
        return self._finish_publication(record, path, ref, group_id, scope, artefact_id, number, kind, scope_md,
                                        objs, work, report, outcome)

    def _place_version(self, record, block, path, ref, group, group_id, number, kind, scope_db_id, scope_md, objs,
                       files, work, marker, outcome, head):
        """Copies, DE import, local copies, manifest and head record of one version (under the scope lock)."""
        # the files: server-side copies, never overwriting a key of the version
        for obj, entry in zip(objs, files):
            info = marker['files'][entry['name']]
            self._place(path, number, entry['name'], obj, info, record, ref)
        if kind == 'de_table' and 'imported' not in marker['steps']:
            self._de_import(record, path, ref, number, scope_db_id, scope_md, objs, files, work, marker, outcome)
            marker['steps'].append('imported')
            self.save_marker(record['id'], marker)
        if 'local' not in marker['steps']:
            self._local_copies(path, number, kind, files, work, marker, outcome)
            marker['steps'].append('local')
            self.save_marker(record['id'], marker)
        if marker.get('manifest'):            # resumed: exactly the manifest decided before the interruption
            raw = marker['manifest'].encode('utf-8')
            manifest = json.loads(raw.decode('utf-8'))
        else:
            manifest = self._manifest(record, block, path, group_id, number, marker, files)
            raw = (json.dumps(manifest, indent=2, sort_keys=False) + '\n').encode('utf-8')
            marker['manifest'] = raw.decode('utf-8')
            self.save_marker(record['id'], marker)
        manifest_sha = hashlib.sha256(raw).hexdigest()
        # written last on S3: its existence makes the version real
        self._put_once(path.manifest_rel(number), raw, manifest_sha, record, ref, marker)
        self._write_local_once(path.manifest_rel(number), raw, group)
        if 'head' not in marker['steps']:
            self._update_head(path, self._read_head(path), record, manifest, number, manifest_sha)
            marker['steps'].append('head')
            self.save_marker(record['id'], marker)
        return head

    def _finish_publication(self, record, path, ref, group_id, scope, artefact_id, number, kind, scope_md, objs,
                            work, report, outcome):
        """After the scope lock: the DE sidecar push, rescans, served check, git commit, cleanup."""
        commit_paths = [path.head_rel, path.manifest_rel(number)]
        if kind == 'de_table':
            self._push_de_sidecar(scope_md, outcome.warnings)
            commit_paths.append(PurePosixPath(scope_md.relative_to(self.store_root)))
            self._full_rescan(outcome.warnings)
        outcome.served = self._rescan_and_check(f'/artefacts/{group_id}/{scope}/{artefact_id}/v{number}',
                                                outcome.warnings)
        outcome.git_commit = self._commit(commit_paths,
                                          f'artefact {ref}@v{number} ({kind})\n\nDeposit {record["id"]} by '
                                          f'{record["user"]}.\n', record, outcome.warnings)
        outcome.consumed = {obj.key: obj.etag for obj in objs}
        outcome.state = 'live'
        outcome.detail = f'published {ref}@v{number}'
        outcome.report = report
        self.drop_marker(record['id'])
        shutil.rmtree(work, ignore_errors=True)
        return outcome

    def _mus_access(self, mds: Dict[str, Path]) -> Dict[str, Dict[str, Any]]:
        """The resolved ``access`` of each dataset (sidecar and .env cascade), as the server sees it."""
        out: Dict[str, Dict[str, Any]] = {}
        try:
            import mus  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return out
        for db_id, md in mds.items():
            try:
                annotation = mus.resolve_annotation(md.with_suffix('.h5ad'))
            except Exception:  # noqa: BLE001 - unresolvable: internal (fail closed)
                annotation = {}
            out[db_id] = {**rules.read_md_meta(md), 'access': (annotation or {}).get('access')}
        return out

    @staticmethod
    def _folder_db_ids(folders: Dict[str, str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for db_id, folder in folders.items():
            out.setdefault(folder, []).append(db_id)
        return out

    def _read_head(self, path: artefacts.ArtefactPath) -> Optional[Dict[str, Any]]:
        head_path = self.store_root / str(path.head_rel)
        if not head_path.exists():
            return None
        try:
            head = artefacts.parse_head_record(head_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise Stop('needs_decision', f'the head record of {path.rel} cannot be read ({exc})')
        problems = artefacts.validate_head_record(head, path)
        if problems:
            raise Stop('needs_decision', f'the head record of {path.rel} is inconsistent: ' + '; '.join(problems))
        return head

    def _place(self, path: artefacts.ArtefactPath, number: int, name: str, obj: ObjInfo, info: Dict[str, Any],
               record: Dict[str, Any], ref: str) -> None:
        """Copy one incoming object to its version key (HEAD first: an existing key is reused only when it is
        exactly this file; any other bytes stop the item, a version is never overwritten)."""
        key = artefacts.s3_key(self.catalog_prefix, path.file_rel(number, name))
        existing = self._head_or_none(key)
        if existing is not None:
            existing_meta = _meta_of(existing)
            # reused only when it is exactly this file AND this deposit placed it (review S3)
            if existing_meta.get('sha256') == info['sha256'] and existing_meta.get('deposit') == record['id'] \
                    and int(existing.get('ContentLength', -1)) == info['size']:
                return
            raise Stop('needs_decision', f'{key} already exists with other content; a version is never overwritten')
        metadata = {'sha256': info['sha256'], 'deposit': record['id'], 'artefact': f'{ref}@v{number}'}
        source = {'Bucket': self.bucket, 'Key': obj.key}
        condition = f'"{obj.etag}"'
        content_type, disposition = stored_type(name)
        try:
            if obj.size <= dataset_deposit.SINGLE_PUT_HARD_MAX:
                self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource=source, Metadata=metadata,
                                    MetadataDirective='REPLACE', CopySourceIfMatch=condition, ContentType=content_type,
                                    ContentDisposition=disposition)
            else:
                self.s3.copy(source, self.bucket, key, ExtraArgs={'Metadata': metadata, 'MetadataDirective': 'REPLACE',
                                                                  'CopySourceIfMatch': condition,
                                                                  'ContentType': content_type,
                                                                  'ContentDisposition': disposition})
        except Exception as exc:  # noqa: BLE001
            if _code(exc) in ('PreconditionFailed', '412'):
                raise Stop('needs_decision', f'{name} changed in incoming during the copy')
            raise TransientError(f'copying {name} failed ({_code(exc)})') from exc
        placed = self._head_or_none(key)
        if placed is None or int(placed.get('ContentLength', -1)) != info['size'] \
                or _meta_of(placed).get('sha256') != info['sha256']:
            raise TransientError(f'the copy of {name} could not be verified')
        if '-' not in obj.etag and str(placed.get('ETag', '')).strip('"') != obj.etag:
            raise Stop('needs_decision', f'the copy of {name} is not the checked object (ETag differs)')
        if '-' in obj.etag:
            # a multipart source: the copy has its own composite ETag, and the store may ignore
            # CopySourceIfMatch. The source still carrying the checked ETag after the copy means the bytes copied
            # were the checked ones (review C6).
            source_now = self._head_or_none(obj.key)
            if source_now is None or str(source_now.get('ETag', '')).strip('"') != obj.etag:
                raise Stop('needs_decision', f'{name} changed in incoming during the copy')

    def _head_or_none(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _absent(exc):
                return None
            raise TransientError(f'HEAD {key} failed ({_code(exc)})') from exc

    def _put_once(self, rel: PurePosixPath, raw: bytes, sha: str, record: Dict[str, Any], ref: str,
                  marker: Dict[str, Any]) -> None:
        """Write a version object (manifest, preview) unless it exists with exactly these bytes."""
        key = artefacts.s3_key(self.catalog_prefix, rel)
        existing = self._head_or_none(key)
        if existing is not None:
            if _meta_of(existing).get('sha256') == sha:
                return
            raise Stop('needs_decision', f'{key} already exists with other content; a version is never overwritten')
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType='application/json',
                               Metadata={'sha256': sha, 'deposit': record['id'], 'artefact': ref})
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'writing {rel.name} failed ({_code(exc)})') from exc
        placed = self._head_or_none(key)
        if placed is None or _meta_of(placed).get('sha256') != sha:
            raise TransientError(f'{rel.name} could not be verified on S3')

    def _copy_local_once(self, rel: PurePosixPath, source: Path, sha256: str, group: str) -> None:
        """A local inline copy, streamed (a temp file renamed over the target); an existing other file stops."""
        target = self.store_root / str(rel)
        ensure_inside(target, self._artefacts_root(group))
        if target.exists():
            if sha256_file(target) == sha256:
                return
            raise Stop('needs_decision', f'{rel} exists locally with other content')
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_inside(target, self._artefacts_root(group))
        partial = target.with_name(f'.{target.name}.partial')
        shutil.copyfile(source, partial)
        os.replace(partial, target)

    def _write_local_once(self, rel: PurePosixPath, raw: bytes, group: str) -> None:
        target = self.store_root / str(rel)
        if target.exists():
            if target.read_bytes() == raw:
                return
            raise Stop('needs_decision', f'{rel} exists locally with other content')
        self._write_local(rel, raw, group)

    def _local_copies(self, path: artefacts.ArtefactPath, number: int, kind: str, files: List[Dict[str, Any]],
                      work: Path, marker: Dict[str, Any], outcome: Outcome) -> None:
        """Inline copies (figures, small files) and previews, locally and (previews) on S3. Warnings only."""
        for entry in files:
            name = entry['name']
            source = work / name
            if not source.is_file():
                continue
            if sha256_file(source) != marker['files'][name]['sha256']:
                outcome.warnings.append(f'{name}: the work copy changed; no inline copy')
                continue
            inline = kind == 'figure' or entry['size'] <= float(self.settings.get('artefacts_inline_max_mb', 2)) * 1024 ** 2
            try:
                if inline:
                    self._copy_local_once(path.file_rel(number, name), source, marker['files'][name]['sha256'],
                                          path.group)
                preview = self._make_preview(name, source)
                if preview is not None:
                    suffix, data = preview
                    rel = path.preview_rel(number, f'{name}{suffix}')
                    self._write_local_once(rel, data, path.group)
                    key = artefacts.s3_key(self.catalog_prefix, rel)
                    if self._head_or_none(key) is None:
                        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                                           ContentType=artefacts.PREVIEW_TYPES[suffix],
                                           Metadata={'sha256': hashlib.sha256(data).hexdigest()})
            except Stop:
                raise
            except Exception as exc:  # noqa: BLE001 - previews are a convenience
                outcome.warnings.append(f'{name}: no preview or inline copy ({type(exc).__name__}: {exc})')

    @staticmethod
    def _make_preview(name: str, source: Path) -> Optional[Tuple[str, bytes]]:
        ending = rules.file_extension(name)
        if ending in ('.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'):
            try:
                from PIL import Image  # noqa: PLC0415
            except ImportError:
                return None
            Image.MAX_IMAGE_PIXELS = 100_000_000          # a decompression bomb raises instead of filling memory
            with Image.open(source) as image:
                image.thumbnail((PREVIEW_PIXELS, PREVIEW_PIXELS))
                buffer = io.BytesIO()
                image.convert('RGB').save(buffer, format='WEBP', quality=80)
                return '.webp', buffer.getvalue()
        if ending in ('.tsv', '.csv'):
            with source.open('rb') as handle:
                lines = [handle.readline() for _ in range(21)]
            return '.txt', b''.join(lines)[:65536]
        return None

    def _manifest(self, record: Dict[str, Any], block: Dict[str, Any], path: artefacts.ArtefactPath, group_id: str,
                  number: int, marker: Dict[str, Any], files: List[Dict[str, Any]]) -> Dict[str, Any]:
        roles = {entry['name']: entry.get('role') for entry in block.get('files') or []}
        manifest = {
            'schema': artefacts.SCHEMA, 'ref': artefacts.format_ref(group_id, path.scope, path.artefact_id, number),
            'artefact_id': path.artefact_id, 'version': number, 'kind': path.kind, 'group': path.group,
            'group_id': group_id, 'scope': path.scope,
            'db_id': None if path.scope == artefacts.GROUP_SCOPE else f'{group_id}/{path.scope}',
            'title': block['title'], 'description': block.get('description'), 'change_note': block.get('change_note'),
            'supersedes': block.get('new_version_of'), 'access': block.get('access') or 'inherit',
            'files': [{'name': entry['name'], 'size': marker['files'][entry['name']]['size'],
                       'sha256': marker['files'][entry['name']]['sha256'],
                       'content_type': rules.content_type_for(entry['name']),
                       'role': roles.get(entry['name']) or 'supporting'} for entry in files],
            'made_from': marker.get('made_from') or [],
            'declared': block.get('declared') or {},
            'recorded': {'uploaded_by': record['user'], 'uploaded_by_name': record.get('name') or '',
                         'deposit_id': record['id'], 'published': marker.get('started') or _now_iso(),
                         'site_id': self.site_id, 'server_version': self.server_version,
                         'checks': {entry['id']: entry['status'] for entry in (marker.get('report') or {}).get('checks') or []},
                         **({'approval': marker['approval']} if marker.get('approval') else {})},
            'de': marker.get('de') if path.kind == 'de_table' else None,
        }
        problems = artefacts.validate_manifest(manifest, path, group_id, number)
        if problems:
            raise Stop('failed', 'the manifest would not be valid: ' + '; '.join(problems))
        return manifest

    def _update_head(self, path: artefacts.ArtefactPath, head: Optional[Dict[str, Any]], record: Dict[str, Any],
                     manifest: Dict[str, Any], number: int, manifest_sha: str) -> None:
        today = _today()
        if head is None:
            head = {'artefact_id': path.artefact_id, 'kind': path.kind, 'scope': path.scope, 'owner': record['user'],
                    'created': today, 'title': manifest['title'], 'latest': None, 'versions': []}
        if not any(entry['state'] in ('live', 'purging', 'purged') for entry in head['versions']):
            head = {**head, 'owner': record['user'], 'created': today}   # the first published version: its owner
        versions = [entry for entry in head['versions'] if entry['version'] != number]
        versions.append({'version': number, 'state': 'live', 'manifest_sha256': manifest_sha, 'published': today,
                         'access': manifest.get('access') or 'inherit'})
        versions.sort(key=lambda entry: entry['version'])
        live = [entry['version'] for entry in versions if entry['state'] == 'live']
        head = {**head, 'title': manifest['title'], 'latest': max(live) if live else None, 'versions': versions}
        self._write_head(path, head)

    def _write_head(self, path: artefacts.ArtefactPath, head: Dict[str, Any]) -> None:
        problems = artefacts.validate_head_record(head, path)
        if problems:
            raise Stop('needs_decision', 'the new head record would be inconsistent: ' + '; '.join(problems))
        text = artefacts.render_head_record(head).encode('utf-8')
        self._write_local(path.head_rel, text, path.group)
        key = artefacts.s3_key(self.catalog_prefix, path.head_rel)
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=text, ContentType='text/markdown',
                               Metadata={'sha256': hashlib.sha256(text).hexdigest()})
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'pushing the head record failed ({_code(exc)})') from exc

    def _rescan_and_check(self, path: str, warnings: List[str]) -> bool:
        if self.api is None:
            warnings.append('no API configured: not rescanned')
            return False
        try:
            self.api.post_with_retry('/admin/rescan?what=artefacts')
            answer = self.api._request(path, method='GET', timeout=60)
            return bool(answer)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'rescan or served check failed ({exc}); the next rescan serves it')
            return False

    def _commit(self, rels: Sequence[PurePosixPath], message: str, record: Optional[Dict[str, Any]],
                warnings: List[str]) -> Optional[str]:
        """Commit only these store paths (written, or deleted, by the route); never others' staged changes."""
        if not self.git_commit:
            return None
        if not (self.store_root / '.git').exists():
            warnings.append('the store is not a git repository: not committed')
            return None
        rel = [str(item) for item in rels]
        staged = self.run(['git', '-C', self.store_root, 'diff', '--cached', '--name-only', '--', *rel])
        if staged.returncode != 0 or staged.stdout.strip():
            warnings.append('not committed: staged changes of someone else on these paths')
            return None
        name, _, email = self.git_identity.partition('<')
        author = []
        if record and record.get('user'):
            mail = record.get('email') or f'{record["user"]}@anthive'
            author = ['--author', f'{record.get("name") or record["user"]} <{mail}>']
        add = self.run(['git', '-C', self.store_root, 'add', '-A', '--', *rel])
        if add.returncode == 0:
            result = self.run(['git', '-C', self.store_root, '-c', f'user.name={name.strip()}', '-c',
                               f'user.email={email.rstrip(">").strip()}', 'commit', '--only', '-m', message,
                               *author, '--', *rel])
        else:
            result = add
        if result.returncode != 0:
            warnings.append(f'git commit failed: {(result.stdout or "") + (result.stderr or "")}'.strip()[:300])
            self.run(['git', '-C', self.store_root, 'reset', '-q', '--', *rel])
            return None
        head = self.run(['git', '-C', self.store_root, 'rev-parse', 'HEAD'])
        return head.stdout.strip() if head.returncode == 0 else None

    # ── publish a collection ────────────────────────────────────────────────
    def publish_collection(self, record: Dict[str, Any], files: List[Dict[str, Any]], objs: List[ObjInfo],
                           approval: Optional[Dict[str, Any]]) -> Outcome:
        try:
            return self._publish_collection(record, files, objs, approval)
        except Stop as stop:
            return Outcome(state=stop.state, detail=str(stop), report=stop.report, held_reasons=stop.held)

    def _publish_collection(self, record: Dict[str, Any], files: List[Dict[str, Any]], objs: List[ObjInfo],
                            approval: Optional[Dict[str, Any]]) -> Outcome:
        data_project = record['data_project']
        if len(files) != 1 or files[0]['name'] != 'index.md':
            raise Stop('failed', 'a collection deposit holds exactly its index.md')
        check = self._hash(objs[0], files[0], None)
        if check.sha256 != files[0]['sha256']:
            raise Stop('needs_decision', 'the index.md in incoming is not the one the server wrote')
        response = self.s3.get_object(Bucket=self.bucket, Key=objs[0].key, IfMatch=f'"{objs[0].etag}"')
        raw = response['Body'].read(1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != files[0]['sha256']:
            raise Stop('needs_decision', 'the index.md read back is not the one the server wrote')
        text = raw.decode('utf-8')
        meta = artefacts.parse_front_matter(text)
        problems = artefacts.analysis_collection_problems(data_project, meta)
        if problems or meta.get('collection_kind') != 'analysis':
            raise Stop('failed', 'the index.md is not a valid analysis collection: ' + '; '.join(problems))
        if not artefacts.DATA_PROJECT_RE.fullmatch(data_project):
            raise Stop('failed', 'the collection name does not follow the naming rule')
        clash = [entry.name for entry in self.store_root.iterdir() if entry.name.lower() == data_project.lower()]
        if clash:
            raise Stop('needs_decision', f'{clash[0]} already exists in the store')
        collections, mds, _folders = self._catalog()
        taken = set(collections) | {db_id.split('/')[0] for db_id in mds}
        if str(meta['group_id']).upper() in {value.upper() for value in taken}:
            raise Stop('failed', f'the group id {meta["group_id"]} is taken meanwhile; create the collection again')
        held = []
        if meta.get('access') == 'public' and self.settings.get('artefacts_hold_public', True):
            held.append('public_access')
        remaining = [reason for reason in held if reason not in set((approval or {}).get('reasons') or [])]
        report = {'schema': 'anthive.artefact_qc/1', 'deposit_id': record['id'], 'result': 'held' if remaining else 'pass',
                  'publish': not remaining, 'held_reasons': remaining, 'counts': {},
                  'checks': [{'id': 'collection.index_md', 'status': 'pass', 'summary': 'valid analysis index.md',
                              'fix': ''}]
                  + ([{'id': 'access.public', 'status': 'held', 'summary': 'public collection: an admin decides',
                       'fix': ''}] if remaining else [])}
        if remaining:
            raise Stop('held', 'held for an admin: ' + ', '.join(remaining), report, held=remaining)
        folder = self.store_root / data_project
        ensure_inside(folder, self.store_root)
        folder.mkdir()
        target = folder / 'index.md'
        partial = folder / '.index.md.partial'
        partial.write_bytes(raw)
        os.replace(partial, target)
        outcome = Outcome(state='live', detail=f'collection {data_project} ({meta["group_id"]}) created',
                          report=report)
        outcome.served = self._rescan_and_check(f'/groups/{meta["group_id"]}', outcome.warnings)
        outcome.git_commit = self._commit([PurePosixPath(data_project, 'index.md')],
                                          f'collection {data_project} ({meta["group_id"]})\n\nDeposit {record["id"]} '
                                          f'by {record["user"]}.\n', record, outcome.warnings)
        outcome.consumed = {objs[0].key: objs[0].etag}
        return outcome

    # ── purge ───────────────────────────────────────────────────────────────
    def process_purges(self, store: Any) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in store._rows("SELECT * FROM artefact_purges WHERE state IN ('requested', 'purging') ORDER BY id"):
            if self.stop_requested():
                break
            try:
                state, detail = self.purge(row)
            except TransientError as exc:
                self.log(f'artefacts: purge of {row["ref"]}@v{row["version"]} waits: {exc}')
                continue
            except Exception as exc:  # noqa: BLE001
                state, detail = 'needs_decision', f'{type(exc).__name__}: {exc}'
            with store._lock:
                store.conn.execute('UPDATE artefact_purges SET state = ?, detail = ?, updated = ? WHERE id = ?',
                                   (state, detail[:2000], self.clock(), row['id']))
            self.db.event(f'purge:{row["ref"]}@v{row["version"]}', state, detail)
            self.log(f'artefacts: purge of {row["ref"]}@v{row["version"]}: {state} {detail}')
            counts[state] = counts.get(state, 0) + 1
        return counts

    def _guarded_delete(self, key: str, version_prefix: str, expected_sha: Optional[str], log_key: str) -> str:
        """Delete one catalog key of the version being purged, only when its sha256 metadata equals
        ``expected_sha`` (the manifest's; for a preview without a local copy: any sha256 the pipeline wrote)."""
        if not key.startswith(version_prefix) or '..' in key.split('/'):
            raise ValueError(f'refusing to delete {key!r}: outside {version_prefix!r}')
        head = self._head_or_none(key)
        if head is None:
            self.db.record_deletion(log_key, key, '', 'skipped:absent')
            return 'absent'
        stored = _meta_of(head).get('sha256')
        if not stored or (expected_sha is not None and stored != expected_sha):
            self.db.record_deletion(log_key, key, str(head.get('ETag', '')).strip('"'), 'skipped:sha256_differs')
            return 'differs'
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if not _absent(exc):
                raise TransientError(f'deleting {key} failed ({_code(exc)})') from exc
        self.db.record_deletion(log_key, key, str(head.get('ETag', '')).strip('"'), 'deleted')
        return 'deleted'

    def purge(self, row: Dict[str, Any]) -> Tuple[str, str]:
        """Design 5.3: mark purging, delete exactly the listed keys (manifest last), local copies, tombstone."""
        path = artefacts.ArtefactPath(row['group_folder'], row['scope'], row['kind'], row['artefact_id'])
        number = int(row['version'])
        scope_md = None
        if path.scope != artefacts.GROUP_SCOPE:
            _collections, mds, folders = self._catalog()
            scope_md = next((md for db_id, md in mds.items() if db_id.split('/')[1] == path.scope
                             and folders.get(db_id) == path.group), None)
        log_key = f'purge:{row["ref"]}@v{number}'
        with writer_lock(self._scope_lock_path(path, scope_md), timeout=600):
            head = self._read_head(path)
            entry = next((item for item in (head or {}).get('versions') or [] if item['version'] == number), None)
            if head is None or entry is None:
                return 'failed', 'no such version in the head record'
            if entry['state'] == 'purged':
                return 'done', 'already purged'
            if entry['state'] == 'failed':
                deleted = self.cleanup_failed_version(path, number, log_key)
                entry.update({'state': 'purged', 'purged': _today(), 'purged_by': row['requested_by'],
                              'purge_reason': row.get('reason') or ''})
                if row.get('by_admin'):
                    entry['purged_by_admin'] = True
                self._write_head(path, head)
                return 'done', f'failed version cleaned up ({len(deleted)} object(s) deleted)'
            if entry['state'] == 'reserved':
                return 'needs_decision', 'the version is still reserved by a publication in progress'
            manifest_path = self.store_root / str(path.manifest_rel(number))
            if not manifest_path.is_file():
                return 'needs_decision', 'the manifest of the version is missing locally; nothing deleted'
            raw = manifest_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['manifest_sha256']:
                return 'needs_decision', 'the local manifest differs from the head record; nothing deleted'
            manifest = json.loads(raw.decode('utf-8'))
            deferred: List[str] = []
            if path.kind == 'de_table' and scope_md is None:
                # the dataset is inactive or not on this host: the files go, the study removal waits for it
                deferred = list(((manifest.get('de') or {}).get('imported') or {}).get('study_ids') or [])
            elif path.kind == 'de_table':
                removal = self._de_remove_for_purge(row, path, number, head, manifest, scope_md)
                if removal is not None:
                    return 'needs_decision', removal
            if entry['state'] != 'purging':
                entry['state'] = 'purging'
                self._write_head(path, {**head, 'versions': head['versions'],
                                        'latest': max([v['version'] for v in head['versions']
                                                       if v['state'] == 'live'], default=None)})
            version_prefix = artefacts.s3_key(self.catalog_prefix, path.version_rel(number)) + '/'
            kept = []
            for item in manifest.get('files') or []:
                for rel, sha in ((path.file_rel(number, item['name']), item['sha256']),):
                    if self._guarded_delete(artefacts.s3_key(self.catalog_prefix, rel), version_prefix, sha,
                                            log_key) == 'differs':
                        kept.append(str(rel))
                for suffix in artefacts.PREVIEW_TYPES:
                    rel = path.preview_rel(number, f'{item["name"]}{suffix}')
                    local = self.store_root / str(rel)
                    sha = sha256_file(local) if local.is_file() else None
                    if self._head_or_none(artefacts.s3_key(self.catalog_prefix, rel)) is not None:
                        if self._guarded_delete(artefacts.s3_key(self.catalog_prefix, rel), version_prefix, sha,
                                                log_key) == 'differs':
                            kept.append(str(rel))
            if kept:
                return 'needs_decision', 'kept (sha256 differs from the manifest): ' + ', '.join(kept)
            self._guarded_delete(artefacts.s3_key(self.catalog_prefix, path.manifest_rel(number)), version_prefix,
                                 entry['manifest_sha256'], log_key)
            version_dir = self.store_root / str(path.version_rel(number))
            if version_dir.exists():
                ensure_inside(version_dir, self._artefacts_root(path.group))
                shutil.rmtree(version_dir)
            entry.update({'state': 'purged', 'purged': _today(), 'purged_by': row['requested_by'],
                          'purge_reason': row.get('reason') or '',
                          'access': entry.get('access') or manifest.get('access') or 'inherit'})
            if deferred:
                entry['de_removal_deferred'] = deferred
            if row.get('by_admin'):
                entry['purged_by_admin'] = True
            live = [v['version'] for v in head['versions'] if v['state'] == 'live']
            self._write_head(path, {**head, 'latest': max(live) if live else None})
        warnings: List[str] = []
        commit_paths = [path.head_rel, path.manifest_rel(number)]
        if path.kind == 'de_table' and scope_md is not None:
            self._push_de_sidecar(scope_md, warnings)
            commit_paths.append(PurePosixPath(scope_md.relative_to(self.store_root)))
            self._full_rescan(warnings)
        self._rescan_and_check(f'/artefacts/{row["ref"]}/versions', warnings)
        self._commit(commit_paths, f'purge {row["ref"]}@v{number}\n\nRequested by '
                     f'{row["requested_by"]}: {row.get("reason") or ""}\n', None, warnings)
        if deferred:
            warnings.append(f'the dataset is not served here: removal of DE study {", ".join(deferred)} is deferred '
                            f'until it is active again (recorded as de_removal_deferred in the head record)')
        return 'done', 'purged' + (f' ({"; ".join(warnings)})' if warnings else '')

    # ── de_table artefacts (phase A3) ────────────────────────────────────────
    def _dataset_genes(self, scope_md: Path) -> Optional[set]:
        """Gene names of the scope dataset (its local ``.duckdb``, read-only); None when unreadable."""
        duckdb_path = scope_md.with_suffix('.duckdb')
        if not duckdb_path.is_file():
            return None
        try:
            import duckdb  # noqa: PLC0415
            connection = duckdb.connect(str(duckdb_path), read_only=True)
            try:
                return {str(row[0]) for row in connection.execute('SELECT gene_name FROM __genes').fetchall()}
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a busy or odd duckdb: the gene check becomes a warning
            return None

    def _de_checks(self, scope_db_id: str, scope_md: Path, files: List[Dict[str, Any]], work: Path
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Section 6.2 checks of every table (``lib/artefact_de.py``) and the facts for the manifest."""
        import artefact_de  # noqa: PLC0415
        from ingest_pipeline import de_study_id, parse_sidecar_text  # noqa: PLC0415

        checks: List[Dict[str, Any]] = []
        tables: List[Dict[str, Any]] = []
        if not scope_md.with_suffix('.duckdb').is_file():
            checks.append({'id': 'de.dataset_local', 'status': 'fail', 'summary': f'the duckdb of {scope_db_id} is '
                           f'not on the host that publishes; DE tables cannot be imported here', 'fix': 'an admin pulls '
                           f'the dataset (ant sync pull) and retries'})
            return checks, tables
        genes = self._dataset_genes(scope_md)
        limit = float(self.settings.get('artefacts_content_check_max_gb', 2)) * 1024 ** 3
        names = {entry['name'] for entry in files}
        studies: Dict[str, str] = {}
        for entry in files:
            name = entry['name']
            if name.lower().endswith('.mus') or name + '.mus' not in names:
                continue
            if int(entry['size']) > limit:
                checks.append({'id': 'de.read', 'status': 'fail', 'summary': f'{name} is larger than the DE check '
                               f'limit (artefacts_content_check_max_gb)', 'fix': ''})
                continue
            meta = parse_sidecar_text((work / (name + '.mus')).read_text(encoding='utf-8', errors='replace'))
            study = de_study_id(meta, PurePosixPath(name).name)
            if study in studies:
                checks.append({'id': 'de.study_id', 'status': 'fail', 'summary': f'{name} and {studies[study]} have '
                               f'the same study id {study}', 'fix': 'set de_study_id in one of the sidecars'})
            studies[study] = name
            result = artefact_de.check_table(work / name, name, name + '.mus', meta, file_id=scope_db_id.split('/')[1],
                                             dataset_basename=scope_md.stem, db_id=scope_db_id, genes=genes,
                                             study_id=study)
            checks.extend(result.checks)
            tables.append(result.manifest_entry())
        return checks, tables

    @staticmethod
    def _dataset_state(mds: Dict[str, Path], db_ids: Sequence[str]) -> Dict[str, Tuple[str, str]]:
        """``{db_id: (version, sha256)}`` of datasets, to notice a change between the checks and the placement
        (review: the gene and made_from checks are rechecked under the lock). A rebuild changes the sha256, a
        new version the version; annotation writes (obs columns) change neither and need no recheck."""
        out: Dict[str, Tuple[str, str]] = {}
        for db_id in sorted(set(db_ids)):
            md = mds.get(db_id)
            meta = rules.read_md_meta(md) if md is not None and md.is_file() else {}
            out[db_id] = (str(meta.get('version')), str(meta.get('sha256')))
        return out

    def _require_dataset_update(self, record: Dict[str, Any], db_id: str) -> None:
        """Review N1, again at publication: the depositor may still update the dataset (an owner change or a
        policy change since the create is honoured)."""
        if record.get('is_admin') or self.update_policy == 'any':
            return
        owner = self._store.owner_of_dataset(db_id) if self._store is not None else None
        if owner != record.get('user'):
            raise Stop('failed', f'not_owner: DE tables are added to {db_id} by its owner or an admin '
                                 f'(datasets_deposit_update_policy: own)')

    def _own_studies(self, path: artefacts.ArtefactPath, head: Optional[Dict[str, Any]], exclude: int) -> set:
        """Study ids that live versions of this artefact (other than ``exclude``) imported."""
        owned: set = set()
        for version in (head or {}).get('versions') or []:
            if version['state'] != 'live' or version['version'] == exclude:
                continue
            manifest_path = self.store_root / str(path.manifest_rel(version['version']))
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            owned |= set(((manifest.get('de') or {}).get('imported') or {}).get('study_ids') or [])
        return owned

    def _ensure_work_copy(self, obj: ObjInfo, entry: Dict[str, Any], work: Path, sha256: str) -> Path:
        """The work-dir copy of a file, read again (pinned to its ETag) when a restart lost it."""
        target = work / entry['name']
        ensure_inside(target, work)
        if target.is_file() and sha256_file(target) == sha256:
            return target
        check = self._hash(obj, entry, target)
        if check.sha256 != sha256:
            raise Stop('needs_decision', f'{entry["name"]} in incoming is not the checked file any more')
        return target

    def _de_import(self, record: Dict[str, Any], path: artefacts.ArtefactPath, ref: str, number: int,
                   scope_db_id: str, scope_md: Path, objs: List[ObjInfo], files: List[Dict[str, Any]], work: Path,
                   marker: Dict[str, Any], outcome: Outcome) -> None:
        """Import the tables into the dataset's DE tables, all or nothing (``import_de_all_or_nothing``),
        under the dataset's writer lock (held by the caller). Study ids of this artefact's earlier versions are
        replaced; a study id another artefact or an earlier route imported is refused unless an admin
        deposited it (then the previous study is archived). On failure the version's copies are removed and
        nothing else changed. Idempotent across a kill: a changelog that already names this version means the
        import happened."""
        import md_edit  # noqa: F401,PLC0415  (import_de_all_or_nothing uses it)
        from ingest_md_fill import today_iso  # noqa: PLC0415
        from ingest_pipeline import (DeImportFailed, friendly_de_error, import_de_all_or_nothing,  # noqa: PLC0415
                                     parse_sidecar_text)

        token = f'artefact {ref}@v{number} (deposit {record["id"]})'
        if token in scope_md.read_text(encoding='utf-8'):
            marker.setdefault('de', self._de_block(marker, None))
            return                                     # imported before a kill; the changelog says so
        studies = [table['study_id'] for table in marker.get('de_tables') or []]
        duckdb_path = scope_md.with_suffix('.duckdb')
        try:
            existing = self._list_studies(duckdb_path)
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'cannot read the DE studies of {scope_db_id}: {exc}') from exc
        own = self._own_studies(path, self._read_head(path), exclude=number)
        clashes = sorted(set(studies) & existing)
        foreign = [study for study in clashes if study not in own]
        if foreign and not record.get('is_admin'):
            self._remove_version_copies(path, number, marker)
            raise Stop('failed', f'{scope_db_id} already has the DE study {", ".join(foreign)} (imported by another '
                                 f'route or artefact); give the table another de_study_id, or ask an admin to replace '
                                 f'it')
        tables = []
        for entry, obj in zip(files, objs):
            if entry['name'].lower().endswith('.mus'):
                continue
            data = self._ensure_work_copy(obj, entry, work, marker['files'][entry['name']]['sha256'])
            sidecar_entry = next(item for item in files if item['name'] == entry['name'] + '.mus')
            sidecar_obj = objs[files.index(sidecar_entry)]
            sidecar = self._ensure_work_copy(sidecar_obj, sidecar_entry, work,
                                             marker['files'][sidecar_entry['name']]['sha256'])
            meta = parse_sidecar_text(sidecar.read_text(encoding='utf-8', errors='replace'))
            study = next(table['study_id'] for table in marker['de_tables'] if table['file'] == entry['name'])
            tables.append((data, meta, study))
        added = [study for study in studies if study not in clashes]
        # what a rollback needs if the version is abandoned after the import (review N3); saved before it
        marker['de_plan'] = {'added': added, 'replaced': clashes, 'started': time.time()}
        self.save_marker(record['id'], marker)

        def change(archived: List[str]) -> str:
            return (f'DE from {token} by {record["user"]}: '
                    + (f'added {", ".join(added)}' if added else '')
                    + ('; ' if added and clashes else '')
                    + (f'replaced {", ".join(clashes)} (previous archived at {", ".join(archived) or "-"})'
                       if clashes else '') + '.')

        class _Api:                                    # release / restore through the publisher's API client
            def __init__(self, api: Any) -> None:
                self.api = api

            def release(self, db_id: str) -> None:
                if self.api is not None:
                    self.api.post_with_retry(f'/admin/release/{db_id}')

            def restore(self, db_id: str) -> None:
                if self.api is not None:
                    self.api.post_with_retry(f'/admin/restore/{db_id}')
        import_work = self.work_root / f'artefact_{record["id"]}_import'
        shutil.rmtree(import_work, ignore_errors=True)
        try:
            with writer_lock(duckdb_path, timeout=600):
                version, archived, warnings = import_de_all_or_nothing(
                    store_root=self.store_root, md=scope_md, db_id=scope_db_id, work=import_work, tables=tables,
                    replace=clashes, runner=self.de_runner, api=_Api(self.api), today=today_iso(), change=change,
                    archive=self._archive_study)
        except DeImportFailed as exc:
            self._remove_version_copies(path, number, marker)
            raise Stop('failed', f'the DE table {exc.table} could not be imported: {friendly_de_error(exc.output)} '
                                 f'Nothing was changed; correct the table or its .mus and deposit it again.')
        finally:
            shutil.rmtree(import_work, ignore_errors=True)
        outcome.warnings.extend(warnings)
        rows: Dict[str, int] = {}
        fingerprints: Dict[str, str] = {}
        try:
            rows = self._study_rows(duckdb_path, studies)
            fingerprints = study_fingerprints(duckdb_path, studies)
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f'row counts of the imported studies not read ({exc})')
        marker['de'] = self._de_block(marker, {'study_ids': studies, 'rows': rows, 'imported_at': _now_iso(),
                                               'dataset_version_after': version, 'replaced': clashes,
                                               'archived': archived, 'fingerprints': fingerprints})

    @staticmethod
    def _de_block(marker: Dict[str, Any], imported: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        block = {'tables': marker.get('de_tables') or []}
        if imported is None:
            imported = {'study_ids': [table['study_id'] for table in block['tables']], 'rows': {},
                        'imported_at': None, 'dataset_version_after': None, 'replaced': [], 'archived': [],
                        'note': 'recovered after an interruption; see the dataset changelog'}
        block['imported'] = imported
        return block

    @staticmethod
    def _list_studies(duckdb_path: Path) -> set:
        import duckdb  # noqa: PLC0415
        connection = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
            if '__de_study' not in tables:
                return set()
            return {str(row[0]) for row in connection.execute('SELECT study_id FROM __de_study').fetchall()}
        finally:
            connection.close()

    @staticmethod
    def _study_rows(duckdb_path: Path, studies: Sequence[str]) -> Dict[str, int]:
        import duckdb  # noqa: PLC0415
        connection = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            return {study: int(connection.execute('SELECT count(*) FROM __de WHERE study_id = ?',
                                                  [study]).fetchone()[0]) for study in studies}
        finally:
            connection.close()

    @staticmethod
    def _archive_study(served_duckdb: Path, served_sidecar: Path, study: str, target: Path) -> bool:
        """The DE-only route's archive of a replaced study (only that study, else the whole sidecar)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from de_storage import write_sidecar  # noqa: PLC0415
            if write_sidecar(target, served_duckdb, only_study=study) > 0:
                return True
        except Exception:  # noqa: BLE001
            target.unlink(missing_ok=True)
        if served_sidecar.exists():
            shutil.copy2(served_sidecar, target)
            return True
        return False

    def _remove_version_copies(self, path: artefacts.ArtefactPath, number: int, marker: Dict[str, Any]) -> None:
        """A version that will not be published: delete its copies (only keys of this version carrying the
        sha256 this deposit placed) and its marker, so a later deposit gets the same number cleanly."""
        version_prefix = artefacts.s3_key(self.catalog_prefix, path.version_rel(number)) + '/'
        for name, info in (marker.get('files') or {}).items():
            key = artefacts.s3_key(self.catalog_prefix, path.file_rel(number, name))
            try:
                self._guarded_delete(key, version_prefix, info['sha256'], f'abandon:{marker.get("ref")}@v{number}')
            except TransientError as exc:
                self.log(f'artefacts: copy {key} of an abandoned version kept ({exc})')
        self.drop_marker(marker['deposit_id'])

    def _push_de_sidecar(self, scope_md: Path, warnings: List[str]) -> None:
        """Push the dataset's ``.de.parquet`` to the catalog (or delete its catalog copy when it is gone)."""
        sidecar = scope_md.with_name(f'{scope_md.stem}.de.parquet')
        rel = scope_md.parent.relative_to(self.store_root)
        key = f'{self.dataset_catalog_prefix}{rel}/{sidecar.name}'
        try:
            if sidecar.exists():
                data = sidecar.read_bytes()
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   Metadata={'sha256': hashlib.sha256(data).hexdigest()})
            elif self._head_or_none(key) is not None:
                self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'push of {sidecar.name} failed ({exc}); ant-sync push carries it later')

    def _full_rescan(self, warnings: List[str]) -> None:
        if self.api is None:
            return
        try:
            self.api.post_with_retry('/admin/rescan')
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'rescan failed ({exc})')

    def _scope_writers(self, path: artefacts.ArtefactPath) -> List[Tuple[str, int, str, Dict[str, Any]]]:
        """``(artefact_id, version, state, manifest)`` of every live or purging de_table version in the scope
        (every artefact of the dataset), from the local head records and manifests."""
        root = self.store_root / path.group / artefacts.ARTEFACTS_PARTS[0] / artefacts.ARTEFACTS_PARTS[1] / \
            path.scope / 'de_table'
        out = []
        if not root.is_dir():
            return out
        for head_path in sorted(root.glob('*/artefact.md')):
            try:
                other = artefacts.ArtefactPath(path.group, path.scope, 'de_table', head_path.parent.name)
                head = artefacts.parse_head_record(head_path.read_text(encoding='utf-8'))
            except Exception:  # noqa: BLE001 - an unreadable record names no writer
                continue
            for version in head.get('versions') or []:
                if version.get('state') not in ('live', 'purging'):
                    continue
                try:
                    manifest = json.loads((self.store_root / str(other.manifest_rel(version['version'])))
                                          .read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                out.append((other.artefact_id, int(version['version']), version['state'], manifest))
        return out

    def _version_file(self, path: artefacts.ArtefactPath, version: int, name: str, sha256: str, work: Path) -> Path:
        """A file of a published version: its local copy when it matches, else read from the catalog."""
        local = self.store_root / str(path.file_rel(version, name))
        if local.is_file() and sha256_file(local) == sha256:
            return local
        target = work / f'v{version}' / name
        ensure_inside(target, work)
        target.parent.mkdir(parents=True, exist_ok=True)
        key = artefacts.s3_key(self.catalog_prefix, path.file_rel(version, name))
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)['Body']
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'reading {key} failed ({_code(exc)})') from exc
        digest = hashlib.sha256()
        with open(target, 'wb') as handle:
            for chunk in iter(lambda: body.read(SCAN_CHUNK), b''):
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != sha256:
            raise Stop('needs_decision', f'{key} does not match its manifest any more')
        return target

    def _de_remove_for_purge(self, row: Dict[str, Any], path: artefacts.ArtefactPath, number: int,
                             head: Dict[str, Any], manifest: Dict[str, Any], scope_md: Optional[Path]) -> Optional[str]:
        """Before a de_table version's files go, settle its studies in the dataset (review N2). Per study:

        * the dataset serves rows this version did not write (a fingerprint differs): left alone when another
          de_table version in the scope wrote them, else an admin decides (a stop);
        * this version wrote them and another live version of the same artefact lists the study: that version's
          table is imported again (the purged rows are archived);
        * this version wrote them and nothing else lists the study: removed.

        All of that in one all-or-nothing import, a patch version with a changelog line and a caveat. Returns a
        reason to stop, or None."""
        from ingest_md_fill import curation_caveat, today_iso  # noqa: PLC0415
        from ingest_pipeline import DeImportFailed, import_de_all_or_nothing, parse_sidecar_text  # noqa: PLC0415

        imported = (manifest.get('de') or {}).get('imported') or {}
        studies = list(imported.get('study_ids') or [])
        if not studies or scope_md is None:
            return None
        mine = imported.get('fingerprints') or {}
        duckdb_path = scope_md.with_suffix('.duckdb')
        try:
            current = study_fingerprints(duckdb_path, studies)
        except Exception as exc:  # noqa: BLE001
            raise TransientError(f'cannot read the DE studies ({exc})') from exc
        writers = [item for item in self._scope_writers(path)
                   if not (item[0] == path.artefact_id and item[1] == number)]
        remove: List[str] = []
        reimport: List[Tuple[int, Dict[str, Any], str]] = []
        undecided: List[str] = []
        for study in studies:
            if study not in current:
                continue                                   # gone already
            if mine.get(study) != current[study]:
                theirs = any(((other.get('de') or {}).get('imported') or {}).get('fingerprints', {}).get(study)
                             == current[study] for _artefact, _version, _state, other in writers)
                if not theirs:
                    undecided.append(study)
                continue
            siblings = sorted(((version, other) for artefact_id, version, state, other in writers
                               if artefact_id == path.artefact_id and state == 'live'
                               and study in (((other.get('de') or {}).get('imported') or {}).get('study_ids') or [])),
                              key=lambda pair: -pair[0])
            if siblings:
                version, other = siblings[0]
                if ((other.get('de') or {}).get('imported') or {}).get('fingerprints', {}).get(study) != current[study]:
                    reimport.append((version, other, study))
            else:
                remove.append(study)
        if undecided:
            return (f'the dataset serves rows of DE study {", ".join(undecided)} that no de_table version recorded '
                    f'(imported by hand or by another route since, or this version predates fingerprints); nothing '
                    f'deleted: an admin removes or keeps the study and purges again')
        if not remove and not reimport:
            return None
        ref = f'{row["ref"]}@v{number}'
        reason = row.get('reason') or ''
        base = ref.split('@')[0]
        parts = []
        if remove:
            parts.append(f'DE study {", ".join(remove)} removed')
        if reimport:
            parts.append('DE study ' + ', '.join(f'{study} restored from {base}@v{version}'
                                                  for version, _other, study in reimport))
        message = '; '.join(parts) + f': purged artefact {ref}' + (f' ({reason})' if reason else '')
        caveat = curation_caveat(message, 'de_study_removed', today_iso(), detected_by='ant-ingest-watch')
        work = self.work_root / f'purge_{hashlib.sha1(ref.encode()).hexdigest()[:12]}'
        shutil.rmtree(work, ignore_errors=True)
        try:
            tables = []
            for version, other, study in reimport:
                table = next(item for item in (other.get('de') or {}).get('tables') or [] if item['study_id'] == study)
                files = {item['name']: item['sha256'] for item in other.get('files') or []}
                data = self._version_file(path, version, table['file'], files[table['file']], work)
                sidecar = self._version_file(path, version, table['sidecar'], files[table['sidecar']], work)
                tables.append((data, parse_sidecar_text(sidecar.read_text(encoding='utf-8', errors='replace')), study))
            with writer_lock(duckdb_path, timeout=600):
                import_de_all_or_nothing(store_root=self.store_root, md=scope_md, db_id=manifest.get('db_id') or '',
                                         work=work / 'import', tables=tables,
                                         replace=[study for _version, _other, study in reimport], remove=remove,
                                         runner=self.de_runner, api=DeApi(self.api), today=today_iso(),
                                         change=lambda _archived: message + '.', caveat=caveat,
                                         archive=self._archive_study)
        except DeImportFailed as exc:
            return f'the DE study could not be settled ({exc.output}); nothing deleted'
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return None

    # ── version reservation (review S3) ──────────────────────────────────────
    def _reserve_version(self, path: artefacts.ArtefactPath, head: Optional[Dict[str, Any]], record: Dict[str, Any],
                         number: int, access: str) -> Dict[str, Any]:
        """Add ``{version: N, state: reserved, deposit_id, access}`` to the head record (local and S3)."""
        today = _today()
        if head is None:
            head = {'artefact_id': path.artefact_id, 'kind': path.kind, 'scope': path.scope, 'owner': record['user'],
                    'created': today, 'title': json.loads(record.get('artefact') or '{}').get('title') or '',
                    'latest': None, 'versions': []}
        if any(entry['version'] == number for entry in head['versions']):
            return head
        versions = list(head['versions']) + [{'version': number, 'state': 'reserved', 'published': today,
                                              'deposit_id': record['id'], 'access': access}]
        head = {**head, 'versions': versions}
        self._write_head(path, head)
        return head

    def _abandon_version(self, path: artefacts.ArtefactPath, number: int, marker: Dict[str, Any],
                         record: Dict[str, Any], reason: str, scope_md: Optional[Path] = None,
                         scope_db_id: Optional[str] = None) -> str:
        """A version that will not be published: roll back its DE import when it happened (review N3), delete
        its copies (keys of this version that this deposit placed, by sha256 and deposit mark), mark the head
        record entry failed, drop the marker. Returns ``reason`` with what the rollback did."""
        if path.kind == 'de_table' and scope_md is not None and 'rolled_back' not in marker.get('steps', []):
            token = f'artefact {marker.get("ref")}@v{number} (deposit {record["id"]})'
            imported = 'imported' in marker.get('steps', []) or (
                scope_md.is_file() and token in scope_md.read_text(encoding='utf-8'))
            if imported:
                note = self._rollback_de_import(path, number, marker, record, reason, scope_md, scope_db_id, token)
                reason = f'{reason}; {note}'
                marker.setdefault('steps', []).append('rolled_back')
                self.save_marker(record['id'], marker)
        self._remove_version_copies(path, number, marker)
        head = self._read_head_quiet(path)
        if head is None:
            return reason
        changed = False
        for entry in head['versions']:
            if entry['version'] == number and entry['state'] == 'reserved':
                entry.update({'state': 'failed', 'failed': _today(),
                              'failure': dataset_deposit.clean_note(reason, 300)})
                changed = True
        if changed:
            try:
                self._write_head(path, head)
            except (Stop, TransientError) as exc:
                self.log(f'artefacts: the failed version v{number} of {path.rel} could not be recorded ({exc})')
        return reason

    def _rollback_de_import(self, path: artefacts.ArtefactPath, number: int, marker: Dict[str, Any],
                            record: Dict[str, Any], reason: str, scope_md: Path, scope_db_id: Optional[str],
                            token: str) -> str:
        """Undo the DE import of an abandoned version, all or nothing: the studies it added are removed, the
        ones it replaced are restored from their archives; a patch version with a changelog line and a caveat
        says so. Returns a note for the failure text."""
        from ingest_md_fill import curation_caveat, today_iso  # noqa: PLC0415
        from ingest_pipeline import DeImportFailed, import_de_all_or_nothing  # noqa: PLC0415
        plan = marker.get('de_plan') or {}
        imported = ((marker.get('de') or {}).get('imported') or {})
        added = list(plan.get('added') or [])
        replaced = list(plan.get('replaced') or [])
        if not plan:            # imported before the plan was recorded: every study of the version was added
            added = [table['study_id'] for table in marker.get('de_tables') or []]
        restore = []
        missing = []
        archive_root = scope_md.parent / '_versions' / scope_md.stem / 'de'
        archived = {Path(item).parent.name: self.store_root / item for item in imported.get('archived') or []}
        for study in replaced:
            source = archived.get(study)
            if source is None or not source.is_file():
                candidates = sorted((archive_root / study).glob('*.de.parquet')) if (archive_root / study).is_dir() \
                    else []
                candidates = [item for item in candidates
                              if item.stat().st_mtime >= float(plan.get('started') or 0) - 5]
                source = candidates[-1] if candidates else None
            if source is None:
                missing.append(study)
            else:
                restore.append((source, study))
        existing = self._list_studies(scope_md.with_suffix('.duckdb'))
        remove = [study for study in added if study in existing]
        if not remove and not restore:
            return 'the DE import left nothing to roll back' + (
                f' (no archive of {", ".join(missing)}: an admin restores it)' if missing else '')
        message = (f'DE from {token} rolled back: the version was not published ({reason[:200]})'
                   + (f'; removed {", ".join(remove)}' if remove else '')
                   + (f'; restored {", ".join(study for _source, study in restore)}' if restore else ''))
        caveat = curation_caveat(message, 'de_import_rolled_back', today_iso(), detected_by='ant-ingest-watch')
        work = self.work_root / f'artefact_{record["id"]}_rollback'
        shutil.rmtree(work, ignore_errors=True)
        try:
            with writer_lock(scope_md.with_suffix('.duckdb'), timeout=600):
                import_de_all_or_nothing(store_root=self.store_root, md=scope_md, db_id=scope_db_id or '', work=work,
                                         tables=[], replace=[], remove=remove, restore=restore,
                                         runner=self.de_runner, api=DeApi(self.api), today=today_iso(),
                                         change=lambda _archived: message + '.', caveat=caveat)
        except DeImportFailed as exc:
            self.log(f'artefacts: the DE import of {marker.get("ref")}@v{number} could not be rolled back: '
                     f'{exc.output}')
            return f'the DE import could NOT be rolled back ({exc.output[:200]}): an admin removes it'
        finally:
            shutil.rmtree(work, ignore_errors=True)
        warnings: List[str] = []
        self._push_de_sidecar(scope_md, warnings)
        self._full_rescan(warnings)
        self._commit([PurePosixPath(scope_md.relative_to(self.store_root))], message + '\n', record, warnings)
        return 'the DE import was rolled back' + (f' (no archive of {", ".join(missing)}: an admin restores it)'
                                                  if missing else '')

    def abandon_after_error(self, record: Dict[str, Any], reason: str) -> str:
        """Review S3 residue: a deposit that ends in failed through a give-up after transient errors or an
        unexpected exception abandons its reserved version like a Stop does (rollback, copies, failed entry)."""
        marker = self.load_marker(record['id'])
        if marker is None or record.get('deposit_kind') != 'artefact':
            return reason
        try:
            block = json.loads(record.get('artefact') or '{}')
            path = artefacts.ArtefactPath(record['data_project'], block.get('scope'), block.get('kind'),
                                          block.get('artefact_id'))
            scope_md, scope_db_id = None, None
            if path.scope != artefacts.GROUP_SCOPE:
                _collections, mds, folders = self._catalog()
                scope_db_id = f'{block.get("group_id")}/{path.scope}'
                scope_md = mds.get(scope_db_id) if folders.get(scope_db_id) == path.group else None
            with writer_lock(self._scope_lock_path(path, scope_md), timeout=600):
                return self._abandon_version(path, int(marker['version']), marker, record, reason,
                                             scope_md=scope_md, scope_db_id=scope_db_id)
        except Exception as exc:  # noqa: BLE001 - the marker stays; an admin purges the reserved version
            self.log(f'artefacts: {record["id"]}: the version could not be abandoned ({type(exc).__name__}: {exc})')
            return f'{reason}; the reserved version could not be abandoned ({type(exc).__name__}): an admin purges it'

    def _read_head_quiet(self, path: artefacts.ArtefactPath) -> Optional[Dict[str, Any]]:
        try:
            return self._read_head(path)
        except Stop:
            return None

    def cleanup_failed_version(self, path: artefacts.ArtefactPath, number: int, log_key: str) -> List[str]:
        """Admin cleanup of a failed version: delete every object under ``.../v<N>/`` that carries the failed
        deposit's mark (a listing of that one version prefix, filtered by the deposit id the head record names),
        then the version becomes a tombstone. Returns the keys deleted."""
        head = self._read_head(path)
        entry = next((item for item in (head or {}).get('versions') or [] if item['version'] == number), None)
        if entry is None or entry['state'] != 'failed' or not entry.get('deposit_id'):
            raise Stop('failed', f'v{number} is not a failed version')
        prefix = artefacts.s3_key(self.catalog_prefix, path.version_rel(number)) + '/'
        deleted = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get('Contents', []) or []:
                head_object = self._head_or_none(obj['Key'])
                if head_object is None or _meta_of(head_object).get('deposit') != entry['deposit_id']:
                    continue
                if self._guarded_delete(obj['Key'], prefix, _meta_of(head_object).get('sha256'), log_key) == 'deleted':
                    deleted.append(obj['Key'])
        return deleted

