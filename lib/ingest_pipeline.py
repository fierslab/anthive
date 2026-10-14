"""One dataset from an S3 deposit to the served store (``ant-ingest`` and ``ant-ingest-watch``).

The deposit layout is the one ``mus s3 upload`` writes (mus
``internal/cli/s3_upload.go``)::

    <prefix><data_project>/[<subfolder>/]<basename>.h5ad
    <prefix><data_project>/[<subfolder>/]<basename>.h5ad.mus     collapsed sidecar, sha256=
    <prefix><data_project>/static/<file>                          --static FILE
    <prefix><data_project>/de/<folder>/<file>(.mus)               DE tables

An :class:`Item` is one h5ad with its sidecar. :class:`Pipeline` runs, for one
item: pull (ETag pinned), sha256 against the sidecar, catalogue ``.md``
(filled for a new dataset, version bump for an update), ``ant-convert`` in
the work dir, ``ant-ingest-check`` (errors and held reasons block), atomic
placement into the store (an update archives the previous version and swaps
under ``/admin/release`` + ``/admin/restore``), rescan, push of the catalog
copy (verified by HEAD), the post steps, a store git commit and a final
rescan. Anything before placement that fails blocks publication; anything
after it is a warning.

:func:`delete_consumed` is the only function that deletes on S3, and only
the exact keys an item consumed, each when its ETag is still the pulled one.
It never takes a prefix. The legacy h5ad checker and its umbrella wrapper,
which rewrite ``.md`` files, are never called (``tests/test_ingest_watch_*``).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import ingest_md_fill as md_fill
import ingest_versions

H5AD_SUFFIX = '.h5ad'
MUS_SUFFIX = '.h5ad.mus'
DE_DATA_EXTS = ('.tsv', '.csv', '.tsv.gz', '.csv.gz')
REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / 'bin'
POST_STEPS = ('score', 'gene_stats', 'module_variability', 'facet_test', 'facet_test_modules',
              'ingest_index', 'manifest')

MULTIPART_THRESHOLD = 256 * 1024 ** 2
RESUMABLE_THRESHOLD = 1 * 1024 ** 3
RESUMABLE_CHUNK = 256 * 1024 ** 2
RESUMABLE_WORKERS = 4
RESUMABLE_RETRIES = 15


class TransientError(RuntimeError):
    """A failure worth retrying later (S3 hiccup, object changed during the pull)."""


class Blocked(RuntimeError):
    """The item must not be published: ``state`` is failed, held or needs_decision."""

    def __init__(self, state: str, message: str, report_path: Optional[str] = None,
                 held_reasons: Optional[List[str]] = None):
        super().__init__(message)
        self.state = state
        self.report_path = report_path
        self.held_reasons = held_reasons or []


# ── the deposit: objects and items ───────────────────────────────────────────

@dataclass
class ObjInfo:
    """One S3 object as listed."""
    key: str
    etag: str
    size: int
    last_modified: float = 0.0

    @classmethod
    def from_listing(cls, obj: Dict[str, Any]) -> 'ObjInfo':
        modified = obj.get('LastModified')
        stamp = modified.timestamp() if hasattr(modified, 'timestamp') else float(modified or 0)
        return cls(obj['Key'], str(obj.get('ETag', '')).strip('"'), int(obj.get('Size', 0)), stamp)


@dataclass
class Item:
    """One deposited h5ad with its sidecar and the static / DE files of its project."""
    prefix: str
    data_project: str
    subfolder: Optional[str]
    basename: str
    h5ad: ObjInfo
    mus: Optional[ObjInfo]
    static: List[ObjInfo] = field(default_factory=list)
    de: List[ObjInfo] = field(default_factory=list)
    other_basenames: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.h5ad.key

    @property
    def rel_dir(self) -> str:
        return f'{self.data_project}/{self.subfolder}' if self.subfolder else self.data_project

    @property
    def display(self) -> str:
        return f'{self.rel_dir}/{self.basename}'

    def content_signature(self) -> str:
        """ETag + size of the h5ad and its sidecar: a change means a new upload to process."""
        parts = [(self.h5ad.key, self.h5ad.etag, self.h5ad.size)]
        if self.mus:
            parts.append((self.mus.key, self.mus.etag, self.mus.size))
        return json.dumps(parts)

    def settle_signature(self) -> str:
        """Everything the item may consume; must stay unchanged across two polls."""
        objs = [self.h5ad] + ([self.mus] if self.mus else []) + self.static + self.de
        return json.dumps(sorted((obj.key, obj.etag, obj.size) for obj in objs))

    def newest_modified(self) -> float:
        objs = [self.h5ad] + ([self.mus] if self.mus else []) + self.static + self.de
        return max(obj.last_modified for obj in objs)

    def seed_names(self) -> Tuple[str, str]:
        """Names of the catalogue seeds a deposit may carry as ``--static`` files."""
        return f'{self.basename}.md', 'index.md'

    def static_to_consume(self) -> List[ObjInfo]:
        """Static files this item uses: all but the ``.md`` seeds of other items in the project."""
        others = {f'{name}.md' for name in self.other_basenames}
        return [obj for obj in self.static if obj.key.rsplit('/', 1)[-1] not in others]


def plan_items(objects: Sequence[Dict[str, Any]], prefix: str) -> Tuple[List[Item], List[str]]:
    """Group a listing of ``prefix`` into items. Returns ``(items, malformed messages)``."""
    infos = {obj['Key']: ObjInfo.from_listing(obj) for obj in objects}
    items: List[Item] = []
    malformed: List[str] = []
    static_by_project: Dict[str, List[ObjInfo]] = {}
    de_by_project: Dict[str, List[ObjInfo]] = {}
    for key in sorted(infos):
        rest = key[len(prefix):]
        parts = rest.split('/')
        if len(parts) >= 3 and parts[1] == 'static':
            static_by_project.setdefault(parts[0], []).append(infos[key])
        elif len(parts) >= 4 and parts[1] == 'de':
            de_by_project.setdefault(parts[0], []).append(infos[key])
    for key in sorted(infos):
        if not key.endswith(H5AD_SUFFIX):
            continue
        parts = key[len(prefix):].split('/')
        if len(parts) == 2:
            data_project, subfolder, filename = parts[0], None, parts[1]
        elif len(parts) == 3 and parts[1] not in ('static', 'de'):
            data_project, subfolder, filename = parts
        else:
            malformed.append(f'{key}: expected {prefix}<data_project>/[<subfolder>/]<file>.h5ad')
            continue
        if not data_project or data_project.startswith(('_', '.')):
            malformed.append(f'{key}: data project {data_project!r} is not allowed')
            continue
        items.append(Item(prefix, data_project, subfolder, filename[:-len(H5AD_SUFFIX)], infos[key],
                          infos.get(key + '.mus'), static_by_project.get(data_project, []),
                          de_by_project.get(data_project, [])))
    for key in sorted(infos):
        parts = key[len(prefix):].split('/')
        if key.endswith(MUS_SUFFIX) and key[:-len('.mus')] not in infos and 'de' not in parts[1:2]:
            malformed.append(f'{key}: sidecar without its h5ad (upload the h5ad, or remove the sidecar)')
    for item in items:
        item.other_basenames = [other.basename for other in items
                                if other.data_project == item.data_project and other is not item]
    return items, malformed


# ── S3 transfer helpers ──────────────────────────────────────────────────────

def _transfer_config() -> Any:
    try:
        from boto3.s3.transfer import TransferConfig  # noqa: PLC0415
    except ImportError:                                # tests with a stub client
        return None
    return TransferConfig(multipart_threshold=MULTIPART_THRESHOLD, multipart_chunksize=MULTIPART_THRESHOLD,
                          max_concurrency=4)


def download(s3: Any, bucket: str, key: str, dest: Path, size: Optional[int] = None) -> None:
    """Download ``key`` to ``dest``. Files of 1 GB or more use resumable range GETs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if size is None:
        try:
            size = int(s3.head_object(Bucket=bucket, Key=key)['ContentLength'])
        except Exception:  # noqa: BLE001
            size = None
    if size is not None and size >= RESUMABLE_THRESHOLD and hasattr(s3, 'get_object'):
        _download_resumable(s3, bucket, key, dest, size)
        return
    config = _transfer_config()
    if config is None:
        s3.download_file(bucket, key, str(dest))
    else:
        s3.download_file(bucket, key, str(dest), Config=config)


def _download_resumable(s3: Any, bucket: str, key: str, dest: Path, size: int) -> None:
    """Concurrent range GETs written at their offset, with a ``.progress`` sidecar to resume."""
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    n_chunks = (size + RESUMABLE_CHUNK - 1) // RESUMABLE_CHUNK
    progress_path = dest.with_suffix(dest.suffix + '.progress')
    done: set = set()
    if progress_path.exists() and dest.exists():
        try:
            state = json.loads(progress_path.read_text())
            if state.get('key') == key and state.get('size') == size:
                done = set(state.get('done', []))
        except (OSError, ValueError):
            done = set()
    if not dest.exists() or dest.stat().st_size != size:
        with open(dest, 'wb') as handle:
            handle.truncate(size)
        done = set()

    def fetch(index: int) -> int:
        start = index * RESUMABLE_CHUNK
        end = min(start + RESUMABLE_CHUNK, size) - 1
        last_error: Optional[BaseException] = None
        for attempt in range(RESUMABLE_RETRIES):
            try:
                body = s3.get_object(Bucket=bucket, Key=key, Range=f'bytes={start}-{end}')['Body'].read()
                if len(body) != end - start + 1:
                    raise IOError(f'chunk {index}: got {len(body)} bytes')
                descriptor = os.open(dest, os.O_WRONLY)
                try:
                    os.pwrite(descriptor, body, start)
                finally:
                    os.close(descriptor)
                return index
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(min(60, 2 ** attempt))
        raise TransientError(f'chunk {index} of {key} failed: {last_error}')

    missing = [index for index in range(n_chunks) if index not in done]
    with ThreadPoolExecutor(max_workers=RESUMABLE_WORKERS) as pool:
        futures = [pool.submit(fetch, index) for index in missing]
        try:
            for future in as_completed(futures):
                done.add(future.result())
                progress_path.write_text(json.dumps({'key': key, 'size': size, 'done': sorted(done)}))
        except BaseException:
            for future in futures:
                future.cancel()
            progress_path.write_text(json.dumps({'key': key, 'size': size, 'done': sorted(done)}))
            raise
    progress_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(2 ** 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sidecar_text(text: str) -> Dict[str, str]:
    """KEY=VALUE pairs of a sidecar (last occurrence wins, comments skipped)."""
    pairs: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        pairs[key.strip()] = value.strip()
    return pairs


def head_etag(s3: Any, bucket: str, key: str) -> Optional[str]:
    """Current ETag of ``key`` (None when absent)."""
    try:
        return str(s3.head_object(Bucket=bucket, Key=key).get('ETag', '')).strip('"')
    except Exception:  # noqa: BLE001
        return None


def delete_consumed(s3: Any, bucket: str, prefix: str, consumed: Dict[str, str],
                    record: Optional[Callable[[str, str, str], None]] = None) -> Tuple[List[str], List[str]]:
    """Delete the exact incoming keys an item consumed, each only if its ETag is unchanged.

    Args:
        prefix: The incoming prefix; a key outside it is refused.
        consumed: ``{key: etag at pull time}``.
        record: Called as ``record(key, etag, outcome)`` with outcome
            ``deleted`` or ``skipped:<reason>``.

    Returns:
        ``(deleted keys, skipped keys)``. Keys are deleted one at a time with
        ``DeleteObject``; there is no prefix or batch delete.
    """
    deleted: List[str] = []
    skipped: List[str] = []
    for key, etag in sorted(consumed.items()):
        if not prefix or not key.startswith(prefix) or key.endswith('/') or key == prefix:
            raise ValueError(f'refusing to delete {key!r}: not an object under {prefix!r}')
        current = head_etag(s3, bucket, key)
        if current is None:
            skipped.append(key)
            if record:
                record(key, etag, 'skipped:absent')
            continue
        if current != etag:
            skipped.append(key)
            if record:
                record(key, etag, f'skipped:etag_changed:{current}')
            continue
        s3.delete_object(Bucket=bucket, Key=key)
        deleted.append(key)
        if record:
            record(key, etag, 'deleted')
    return deleted, skipped


# ── subprocesses and the server ──────────────────────────────────────────────

class SubprocessRunner:
    """Runs the ``ant-*`` tools; output goes to one log file per item."""

    def __init__(self, log_path: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        self.log_path = log_path
        self.env = env

    def run(self, label: str, cmd: Sequence[str], cwd: Optional[Path] = None,
            timeout: Optional[float] = None) -> Tuple[int, str]:
        """Run ``cmd``; returns ``(exit code, last 30 lines of output)``. Exit 127: not found."""
        environment = {**os.environ, **(self.env or {})}
        started = time.time()
        try:
            completed = subprocess.run([str(part) for part in cmd], cwd=str(cwd) if cwd else None, env=environment,
                                       capture_output=True, text=True, timeout=timeout, check=False)
            code, output = completed.returncode, (completed.stdout or '') + (completed.stderr or '')
        except FileNotFoundError as exc:
            code, output = 127, f'{label}: not found ({exc})'
        except subprocess.TimeoutExpired as exc:
            code, output = 124, f'{label}: timed out after {timeout} s\n{exc.stdout or ""}{exc.stderr or ""}'
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, 'a') as handle:
                handle.write(f'\n=== {label} ({time.time() - started:.1f} s, exit {code})\n$ '
                             f'{" ".join(str(part) for part in cmd)}\n{output}\n')
        return code, '\n'.join(output.strip().splitlines()[-30:])


class ApiClient:
    """Local ant-serve admin calls (loopback local tool, or the internal secret header)."""

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None,
                 retry_delays: Sequence[float] = (5, 10, 20, 40, 60, 60, 60)):
        self.base_url = base_url.rstrip('/')
        self.headers = headers or {}
        self.retry_delays = list(retry_delays)

    def _post(self, path: str, timeout: float = 600) -> Dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json', **self.headers})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}')

    def post_with_retry(self, path: str) -> Dict[str, Any]:
        """POST with backoff while the server is down or restarting; raises the last error."""
        last_error: Optional[Exception] = None
        for delay in [0.0] + self.retry_delays:
            if delay:
                time.sleep(delay)
            try:
                return self._post(path)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
        raise TransientError(f'POST {path} failed: {last_error}')

    def rescan(self) -> Dict[str, Any]:
        return self.post_with_retry('/admin/rescan')

    def release(self, db_id: str) -> Dict[str, Any]:
        return self.post_with_retry(f'/admin/release/{db_id}')

    def restore(self, db_id: str) -> Dict[str, Any]:
        return self.post_with_retry(f'/admin/restore/{db_id}')


def served_ids(rescan_result: Dict[str, Any]) -> List[str]:
    """Dataset ids listed by a ``/admin/rescan`` answer."""
    databases = rescan_result.get('databases') or []
    if isinstance(databases, dict):
        return list(databases)
    return [str(entry.get('id') if isinstance(entry, dict) else entry) for entry in databases]


@contextmanager
def writer_lock(duckdb_path: Path, timeout: float = 600) -> Iterator[None]:
    """The flock on ``<duckdb>.lock`` that AnnotationWriter, ant-score and friends take."""
    lock_path = duckdb_path.with_name(duckdb_path.name + '.lock')
    handle = open(lock_path, 'a+')
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TransientError(f'{lock_path} is held by another writer')
                time.sleep(5)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def place_file(source: Path, target: Path) -> None:
    """Put ``source`` at ``target`` atomically (rename, or copy to a partial name then rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
        return
    except OSError:
        pass
    partial = target.with_name(f'.{target.name}.ingest-partial')
    shutil.copy2(source, partial)
    os.replace(partial, target)


# ── the pipeline ─────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    """Paths and switches of one pipeline run."""
    store_root: Path
    work_root: Path
    reports_root: Path
    bucket: str
    catalog_prefix: str = ''
    post_steps: Sequence[str] = POST_STEPS
    post_step_timeout: Optional[float] = 21600
    store_git_commit: bool = True
    store_git_push: bool = False
    git_identity: str = 'anthive ingest-watch <noreply@anthive.local>'
    route: str = 's3-watch'
    push_catalog: bool = True
    state_dir: Optional[Path] = None
    s3_endpoint: Optional[str] = None


@dataclass
class Outcome:
    """Result of :meth:`Pipeline.process`."""
    state: str = 'failed'                    # live | held | failed | needs_decision
    detail: str = ''
    kind: str = ''                           # new | update_minor | update_patch | unchanged
    db_id: Optional[str] = None
    version: Optional[str] = None
    previous_version: Optional[str] = None
    sha256: Optional[str] = None
    previous_sha256: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    consumed: Dict[str, str] = field(default_factory=dict)
    report_path: Optional[str] = None
    held_reasons: List[str] = field(default_factory=list)
    served: bool = False
    catalog_verified: bool = False
    catalog_keys: List[str] = field(default_factory=list)
    archived_local: List[str] = field(default_factory=list)
    archived_s3: List[str] = field(default_factory=list)
    git_commit: Optional[str] = None
    timings: Dict[str, float] = field(default_factory=dict)
    store_h5ad: Optional[str] = None

    @property
    def deletable(self) -> bool:
        """True when the consumed incoming keys may be deleted (live, served, catalog copy verified)."""
        return self.state == 'live' and self.served and self.catalog_verified and bool(self.consumed)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _slug(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12] + '_' + key.rsplit('/', 1)[-1].replace('.', '_')[:40]


class Pipeline:
    """Runs one :class:`Item` from the deposit to the served store."""

    def __init__(self, config: PipelineConfig, s3: Any, runner: Any, api: Any,
                 log: Callable[[str], None] = print):
        self.config = config
        self.s3 = s3
        self.runner = runner
        self.api = api
        self.log = log

    # helpers --------------------------------------------------------------

    def _tool(self, domain: str, name: str) -> Path:
        return BIN / domain / name

    def _timed(self, outcome: Outcome, label: str, started: float) -> None:
        outcome.timings[label] = round(time.time() - started, 1)

    def _pull(self, obj: ObjInfo, dest: Path) -> None:
        """Download one object, pinned to the ETag of the listing."""
        before = head_etag(self.s3, self.config.bucket, obj.key)
        if before != obj.etag:
            raise TransientError(f'{obj.key} changed since it was listed (ETag {before} != {obj.etag})')
        try:
            download(self.s3, self.config.bucket, obj.key, dest, obj.size)
        except TransientError:
            raise
        except Exception as exc:  # noqa: BLE001 - S3 / network
            raise TransientError(f'pull of {obj.key} failed: {exc}') from exc
        after = head_etag(self.s3, self.config.bucket, obj.key)
        if after != obj.etag:
            raise TransientError(f'{obj.key} changed during the pull')
        if dest.stat().st_size != obj.size:
            raise TransientError(f'{obj.key}: pulled {dest.stat().st_size} bytes, listed {obj.size}')

    def _db_id(self, store_dir_index: Path, md_meta: Dict[str, Any]) -> Optional[str]:
        from metadata import parse_md_file  # noqa: PLC0415

        index_meta = parse_md_file(store_dir_index)[0] if store_dir_index.exists() else {}
        group_id = md_meta.get('group_id') or index_meta.get('group_id')
        return f'{group_id}/{md_meta["id"]}' if group_id and md_meta.get('id') else None

    # main -----------------------------------------------------------------

    def process(self, item: Item) -> Outcome:
        """Run every step for ``item``. Raises :class:`TransientError` for a retry later."""
        outcome = Outcome()
        try:
            self._process(item, outcome)
        except Blocked as blocked:
            outcome.state = blocked.state
            outcome.detail = str(blocked)
            outcome.report_path = blocked.report_path or outcome.report_path
            outcome.held_reasons = blocked.held_reasons
        return outcome

    def _process(self, item: Item, outcome: Outcome) -> None:
        from metadata import parse_md_file, write_md_file  # noqa: PLC0415

        cfg = self.config
        today = md_fill.today_iso()
        if item.mus is None:
            raise Blocked('failed', f'no sidecar {item.key}.mus')
        work = cfg.work_root / _slug(item.key)
        if work.exists():
            shutil.rmtree(work)
        group_dir = work / item.data_project          # the staging group folder ant-ingest-check reads
        group_dir.mkdir(parents=True)
        reports = cfg.reports_root / _slug(item.key)
        reports.mkdir(parents=True, exist_ok=True)
        staged_h5ad = group_dir / f'{item.basename}{H5AD_SUFFIX}'
        staged_mus = group_dir / f'{item.basename}{MUS_SUFFIX}'
        staged_md = group_dir / f'{item.basename}.md'

        # 1. pull, pinned to the listed ETags
        started = time.time()
        self.log(f'pull {item.mus.key}, {item.h5ad.key} ({item.h5ad.size / 1e6:.1f} MB)')
        self._pull(item.mus, staged_mus)
        self._pull(item.h5ad, staged_h5ad)
        outcome.consumed[item.h5ad.key] = item.h5ad.etag
        outcome.consumed[item.mus.key] = item.mus.etag
        static_dir = work / '_static'
        seeds: Dict[str, Path] = {}
        md_seed_name, index_seed_name = item.seed_names()
        for obj in item.static_to_consume():
            name = obj.key.rsplit('/', 1)[-1]
            dest = static_dir / name
            self._pull(obj, dest)
            outcome.consumed[obj.key] = obj.etag
            if name in (md_seed_name, index_seed_name):
                seeds[name] = dest
        self._timed(outcome, 'pull', started)

        # 2. sha256 against the sidecar
        started = time.time()
        sidecar_text = staged_mus.read_text(encoding='utf-8', errors='replace')
        sidecar = parse_sidecar_text(sidecar_text)
        declared = sidecar.get('sha256', '')
        actual = sha256_file(staged_h5ad)
        outcome.sha256 = actual
        self._timed(outcome, 'sha256', started)
        if not declared:
            raise Blocked('failed', 'the sidecar has no sha256= line; integrity cannot be verified')
        if declared != actual:
            raise Blocked('failed', f'sha256 mismatch: pulled {actual}, sidecar declares {declared}')
        if sidecar.get('data_project') and sidecar['data_project'] != item.data_project:
            raise Blocked('failed', f'sidecar data_project={sidecar["data_project"]} but the deposit is under '
                                    f'{item.data_project}/')
        import mus  # noqa: PLC0415

        annotation = mus.parse_mus_file(staged_mus)
        curation_notes: List[Tuple[str, str]] = []
        if not annotation.get('access'):
            with open(staged_mus, 'a', encoding='utf-8') as handle:
                handle.write(('' if sidecar_text.endswith('\n') else '\n') + 'access=internal\n')
            annotation['access'] = 'internal'
            curation_notes.append(('access_default', 'The deposit did not state access; the ingestion route set '
                                                     'access=internal (login only), the default for new datasets.'))

        # 3. new dataset or update
        store_dir = cfg.store_root / item.rel_dir
        group_store_dir = cfg.store_root / item.data_project
        store_h5ad = store_dir / f'{item.basename}{H5AD_SUFFIX}'
        store_md = store_dir / f'{item.basename}.md'
        store_mus = store_dir / f'{item.basename}{MUS_SUFFIX}'
        store_index = group_store_dir / 'index.md'
        outcome.store_h5ad = str(store_h5ad)
        if store_h5ad.exists() and not store_md.exists():
            raise Blocked('needs_decision', f'{store_h5ad} exists without a .md; decide by hand what it is')
        if store_md.exists() and not store_h5ad.exists():
            raise Blocked('needs_decision', f'{store_md} exists without its h5ad (S3-only or retired dataset?); '
                                            f'decide by hand')
        is_update = store_h5ad.exists()
        ingest = md_fill.ingest_block(cfg.route, item.h5ad.key, item.h5ad.etag, md_fill.depositor_of(sidecar), today)

        started = time.time()
        old_sha = sha256_file(store_h5ad) if is_update else ''
        # layer statistics stream the whole matrix: only when the data is new
        new_profile = md_fill.profile_h5ad(staged_h5ad, with_layer_stats=not is_update or old_sha != actual)
        if not is_update:
            seed_meta, seed_body = (parse_md_file(seeds[md_seed_name]) if md_seed_name in seeds else ({}, ''))
            index_meta: Dict[str, Any]
            if store_index.exists():
                index_meta = parse_md_file(store_index)[0]
            else:
                if index_seed_name in seeds:
                    index_meta, index_body = parse_md_file(seeds[index_seed_name])
                else:
                    from md_check import collect_db_ids  # noqa: PLC0415

                    taken_groups = {db_id.split('/')[0] for db_id in collect_db_ids(cfg.store_root)}
                    index_meta, index_body = md_fill.build_index_md(sidecar, item.data_project, taken_groups,
                                                                    item.basename)
                index_meta.setdefault('group', item.data_project)
                write_md_file(group_dir / 'index.md', index_meta, index_body)
            from md_check import collect_db_ids  # noqa: PLC0415

            meta, filled = md_fill.fill_catalog_fields(seed_meta, new_profile, actual, annotation, item.basename,
                                                       index_meta.get('group_id'), collect_db_ids(cfg.store_root),
                                                       today, ingest)
            for topic, message in curation_notes:
                meta.setdefault('caveats', []).append(md_fill.curation_caveat(message, topic, today))
                meta['changelog'][-1]['changes'].append(f'Caveat: {message}')
            body = seed_body or f'# {meta.get("title", item.basename)}\n\n{meta.get("description", "")}\n'
            write_md_file(staged_md, meta, body)
            outcome.kind = 'new'
            outcome.version = str(meta.get('version'))
            outcome.db_id = self._db_id(group_dir / 'index.md' if not store_index.exists() else store_index, meta)
            self.log(f'new dataset {item.display} ({outcome.db_id}), filled {", ".join(filled)}')
        else:
            old_text = store_md.read_text(encoding='utf-8')
            old_meta = parse_md_file(store_md)[0]
            outcome.previous_sha256 = old_sha
            outcome.previous_version = str(old_meta.get('version') or '')
            merged_mus, sidecar_changes = md_fill.merge_sidecar_incoming_wins(
                store_mus.read_text(encoding='utf-8') if store_mus.exists() else '', staged_mus.read_text())
            staged_mus.write_text(merged_mus)
            data_changed = old_sha != actual
            old_profile = md_fill.profile_h5ad(store_h5ad, with_layer_stats=False) if data_changed else None
            plan = md_fill.plan_update(old_meta, old_profile, new_profile, old_sha, actual, sidecar_changes)
            outcome.db_id = self._db_id(store_index, old_meta)
            if plan.part is None:
                outcome.kind = 'unchanged'
                outcome.version = outcome.previous_version
                self._timed(outcome, 'catalogue', started)
                self._publish_unchanged(item, outcome, store_h5ad, actual)
                return
            caveat = md_fill.curation_caveat(
                f'Version {outcome.previous_version or "?"} was replaced by a deposit on {today} '
                f'({"new data" if data_changed else "metadata only"}): ' + ' '.join(plan.changes[:4]),
                'dataset_update', today)
            try:
                new_text = md_fill.update_md_text(old_text, plan, actual, new_profile, today, caveat=caveat,
                                                  ingest=ingest)
            except Exception as exc:  # noqa: BLE001 - md_edit refuses an edit it cannot verify
                raise Blocked('needs_decision', f'the store .md cannot be edited safely ({exc}); update it by hand')
            staged_md.write_text(new_text)
            outcome.kind = 'update_minor' if data_changed else 'update_patch'
            outcome.version = str(parse_md_file(staged_md)[0].get('version'))
            de_sidecar = store_dir / f'{item.basename}.de.parquet'
            if de_sidecar.exists() and data_changed:
                shutil.copy2(de_sidecar, group_dir / de_sidecar.name)
            self.log(f'update {item.display} ({outcome.db_id}) {outcome.previous_version} -> {outcome.version} '
                     f'({outcome.kind})')
        self._timed(outcome, 'catalogue', started)

        # 4. convert in the work dir (not for a metadata-only update)
        staged_duckdb = group_dir / f'{item.basename}.duckdb'
        if outcome.kind != 'update_patch':
            started = time.time()
            code, tail = self.runner.run('ant-convert', [self._tool('ingest', 'ant-convert'), staged_h5ad], cwd=work)
            self._timed(outcome, 'convert', started)
            if code != 0 or not staged_duckdb.exists():
                raise Blocked('failed', f'ant-convert failed (exit {code}): {tail[-1500:]}')

        # 5. ant-ingest-check (read-only); errors and held reasons block
        started = time.time()
        report_json = reports / f'{time.strftime("%Y%m%dT%H%M%S")}.json'
        report_md = report_json.with_suffix('.md')
        cmd = [self._tool('ingest', 'ant-ingest-check'), group_dir, '--dataset', item.basename,
               '--store', cfg.store_root, '--json', report_json, '--md-summary', report_md, '--sha256', actual,
               '--mode', 'existing' if is_update else 'new']
        if outcome.kind == 'update_patch':
            cmd += ['--duckdb', store_dir / f'{item.basename}.duckdb']
        code, tail = self.runner.run('ant-ingest-check', cmd, cwd=work)
        self._timed(outcome, 'ingest_check', started)
        outcome.report_path = str(report_json)
        if code == 2 or not report_json.exists():
            raise Blocked('failed', f'ant-ingest-check could not run (exit {code}): {tail[-1500:]}', str(report_json))
        report = json.loads(report_json.read_text())
        errors = [entry for entry in report.get('checks', report.get('results', []))
                  if isinstance(entry, dict) and entry.get('status') == 'fail']
        if report.get('result') == 'fail':
            names = ', '.join(entry.get('id', '?') for entry in errors) or 'see report'
            raise Blocked('failed', f'ant-ingest-check found errors: {names}', str(report_json))
        if report.get('publish') == 'held':
            reasons = list(report.get('held_reasons') or [])
            raise Blocked('held', f'held for an admin: {", ".join(reasons)}', str(report_json), reasons)
        warnings = [entry.get('id', '?') for entry in report.get('checks', report.get('results', []))
                    if isinstance(entry, dict) and entry.get('status') == 'warn']
        if warnings:
            outcome.warnings.append(f'ingest-check warnings: {", ".join(warnings)}')
        if not outcome.db_id:
            outcome.db_id = (report.get('dataset') or {}).get('db_id')
        self.log(f'ant-ingest-check passed ({len(warnings)} warnings); publishing {outcome.db_id}')

        # 6. publish: place into the store (update: archive + swap under release/restore)
        started = time.time()
        if is_update:
            self._swap_update(item, outcome, group_dir, store_dir, data_changed=outcome.kind == 'update_minor')
        else:
            self._place_new(item, group_dir, store_dir, group_store_dir)
        self._promote_static(item, outcome, static_dir, group_store_dir, seeds)
        self._timed(outcome, 'publish', started)
        outcome.state = 'live'
        outcome.detail = f'{outcome.kind} {outcome.version}'
        self.log(f'placed in {store_dir} ({outcome.kind} {outcome.version})')

        # 7. rescan and check it is served
        self._rescan_and_verify(outcome)

        # 8. catalog copy on S3 (verified by HEAD)
        if cfg.push_catalog:
            started = time.time()
            self._push_catalog(item, outcome, store_dir, include_h5ad=outcome.kind != 'update_patch')
            self._timed(outcome, 'push', started)
            self.log(f'catalog push: {len(outcome.catalog_keys)} object(s), verified {outcome.catalog_verified}')

        # 9. post steps, DE, derived push, git, final rescan: warnings only
        self._post(item, outcome, store_dir, work)

    # publication ----------------------------------------------------------

    def _staged_files(self, group_dir: Path, basename: str) -> List[Path]:
        """Files of the staged dataset in placement order (.md and .duckdb last)."""
        files = sorted(group_dir.glob(f'{basename}.layer.*.parquet'))
        for suffix in ('.de.parquet', H5AD_SUFFIX, MUS_SUFFIX, '.md', '.duckdb'):
            path = group_dir / f'{basename}{suffix}'
            if path.exists():
                files.append(path)
        return files

    def _place_new(self, item: Item, group_dir: Path, store_dir: Path, group_store_dir: Path) -> None:
        store_dir.mkdir(parents=True, exist_ok=True)
        staged_index = group_dir / 'index.md'
        if staged_index.exists() and not (group_store_dir / 'index.md').exists():
            place_file(staged_index, group_store_dir / 'index.md')
        for path in self._staged_files(group_dir, item.basename):
            place_file(path, store_dir / path.name)

    def _swap_update(self, item: Item, outcome: Outcome, group_dir: Path, store_dir: Path,
                     data_changed: bool) -> None:
        cfg = self.config
        db_id = outcome.db_id
        store_duckdb = store_dir / f'{item.basename}.duckdb'
        released = False
        if data_changed and db_id:
            try:
                self.api.release(db_id)
                released = True
            except TransientError as exc:
                outcome.warnings.append(f'release of {db_id} failed ({exc}); swapping anyway')
        try:
            with writer_lock(store_duckdb):
                archived = ingest_versions.archive_local(store_dir, item.basename, outcome.previous_version or '0.0.0',
                                                         data_changed=data_changed)
                outcome.archived_local = [str(path) for path in archived]
                if cfg.push_catalog:
                    try:
                        outcome.archived_s3 = ingest_versions.archive_s3(
                            self.s3, cfg.bucket, cfg.catalog_prefix, item.rel_dir, item.basename,
                            outcome.previous_version or '0.0.0', archived, include_h5ad=data_changed)
                    except Exception as exc:  # noqa: BLE001
                        outcome.warnings.append(f'S3 copy of version {outcome.previous_version} failed: {exc}')
                staged = self._staged_files(group_dir, item.basename)
                new_names = {path.name for path in staged}
                for path in staged:
                    place_file(path, store_dir / path.name)
                if data_changed:
                    for stale in store_dir.glob(f'{item.basename}.layer.*.parquet'):
                        if stale.name not in new_names:
                            stale.unlink()
        finally:
            if released:
                try:
                    self.api.restore(db_id)
                except TransientError as exc:
                    outcome.warnings.append(f'restore of {db_id} failed: {exc}')
        self._record_version(outcome, item)

    def _record_version(self, outcome: Outcome, item: Item) -> None:
        if self.config.state_dir is None:
            return
        log_path = self.config.state_dir / 'versions.jsonl'
        with open(log_path, 'a') as handle:
            handle.write(json.dumps({'ts': _now_iso(), 'db_id': outcome.db_id, 'dataset': item.display,
                                     'archived_version': outcome.previous_version,
                                     'archived_sha256': outcome.previous_sha256, 'current_version': outcome.version,
                                     'current_sha256': outcome.sha256, 'local': outcome.archived_local,
                                     's3': outcome.archived_s3}) + '\n')

    def _promote_static(self, item: Item, outcome: Outcome, static_dir: Path, group_store_dir: Path,
                        seeds: Dict[str, Path]) -> None:
        if not static_dir.is_dir():
            return
        seed_paths = {path.resolve() for path in seeds.values()}
        for path in sorted(static_dir.iterdir()):
            if path.resolve() in seed_paths:
                continue
            target = group_store_dir / 'static' / path.name
            if target.exists():
                if sha256_file(target) != sha256_file(path):
                    outcome.warnings.append(f'static file {target} differs from the deposit; kept the store copy')
                continue
            place_file(path, target)

    def _publish_unchanged(self, item: Item, outcome: Outcome, store_h5ad: Path, sha: str) -> None:
        """Identical re-upload: nothing to publish; verify that it is served and on S3."""
        outcome.state = 'live'
        outcome.detail = f'unchanged (already live as {outcome.version})'
        self._rescan_and_verify(outcome)
        if self.config.push_catalog:
            outcome.catalog_verified = self._verify_catalog(item, store_h5ad, sha, outcome)

    def _rescan_and_verify(self, outcome: Outcome) -> None:
        try:
            result = self.api.rescan()
            outcome.served = bool(outcome.db_id) and outcome.db_id in served_ids(result)
            if not outcome.served:
                outcome.warnings.append(f'{outcome.db_id} is not listed after the rescan')
        except TransientError as exc:
            outcome.warnings.append(f'rescan failed ({exc}); the dataset is placed but not yet served')

    # S3 catalog -------------------------------------------------------------

    def _catalog_key(self, item: Item, name: str) -> str:
        return f'{self.config.catalog_prefix}{item.rel_dir}/{name}'

    def _upload(self, path: Path, key: str, sha: Optional[str] = None) -> None:
        metadata = {'original-mtime': str(int(path.stat().st_mtime))}
        if sha:
            metadata['sha256'] = sha
        config = _transfer_config()
        kwargs: Dict[str, Any] = {'ExtraArgs': {'Metadata': metadata}}
        if config is not None:
            kwargs['Config'] = config
        self.s3.upload_file(str(path), self.config.bucket, key, **kwargs)

    def _verify_catalog(self, item: Item, store_h5ad: Path, sha: str, outcome: Outcome) -> bool:
        """HEAD the catalog h5ad: same size and the sha256 we recorded."""
        key = self._catalog_key(item, store_h5ad.name)
        try:
            head = self.s3.head_object(Bucket=self.config.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f'catalog copy {key} not found ({exc})')
            return False
        size_ok = int(head.get('ContentLength', -1)) == store_h5ad.stat().st_size
        remote_sha = (head.get('Metadata') or {}).get('sha256')
        if not size_ok or remote_sha != sha:
            outcome.warnings.append(f'catalog copy {key} does not match (size ok: {size_ok}, '
                                    f'sha256 metadata: {remote_sha or "absent"})')
            return False
        return True

    def _push_catalog(self, item: Item, outcome: Outcome, store_dir: Path, include_h5ad: bool) -> None:
        from sidecar_merge import restamp_s3_block, write_sidecar_atomic  # noqa: PLC0415

        cfg = self.config
        store_h5ad = store_dir / f'{item.basename}{H5AD_SUFFIX}'
        store_mus = store_dir / f'{item.basename}{MUS_SUFFIX}'
        try:
            if include_h5ad:
                key = self._catalog_key(item, store_h5ad.name)
                self._upload(store_h5ad, key, outcome.sha256)
                outcome.catalog_keys.append(key)
                head = self.s3.head_object(Bucket=cfg.bucket, Key=key)
                endpoint = cfg.s3_endpoint
                url = f'https://{endpoint}/{cfg.bucket}/{key}' if endpoint else f's3://{cfg.bucket}/{key}'
                write_sidecar_atomic(store_mus, restamp_s3_block(
                    store_mus.read_text(encoding='utf-8'), bucket=cfg.bucket, key=key, url=url,
                    etag=str(head.get('ETag', '')).strip('"'), uploaded_at=_now_iso()))
            mus_key = self._catalog_key(item, store_mus.name)
            self._upload(store_mus, mus_key)
            outcome.catalog_keys.append(mus_key)
            for path in [store_dir / f'{item.basename}.duckdb', *sorted(store_dir.glob(f'{item.basename}.layer.*.parquet')),
                         store_dir / f'{item.basename}.de.parquet']:
                if path.exists():
                    key = self._catalog_key(item, path.name)
                    self._upload(path, key)
                    outcome.catalog_keys.append(key)
        except Exception as exc:  # noqa: BLE001 - publication already happened
            outcome.warnings.append(f'push to the catalog failed: {exc}')
            return
        outcome.catalog_verified = self._verify_catalog(item, store_h5ad, outcome.sha256 or '', outcome)

    # after publication -----------------------------------------------------

    def _post(self, item: Item, outcome: Outcome, store_dir: Path, work: Path) -> None:
        cfg = self.config
        store = cfg.store_root
        db_id = outcome.db_id
        if outcome.kind == 'update_patch':
            steps: List[str] = ['manifest']
        else:
            steps = [step for step in cfg.post_steps if step in POST_STEPS]
        state_dir = cfg.state_dir or work
        commands = {
            'score': [self._tool('annot', 'ant-score-fleet'), '--h5adstore-root', store, '--only', db_id, 'run',
                      '--workers', '1', '--state', state_dir / 'score_fleet.state.jsonl',
                      '--log', state_dir / 'score_fleet.log'],
            'gene_stats': [self._tool('annot', 'ant-gene-stats-precompute'), '--h5adstore-root', store,
                           '--only', db_id, '--no-prune', '--workers', '2'],
            'module_variability': [self._tool('annot', 'ant-module-variability'), '--h5adstore-root', store,
                                   '--only', db_id, '--no-prune', '--workers', '2'],
            'facet_test': [self._tool('annot', 'ant-facet-test'), 'run', '--h5adstore-root', store,
                           '--db-id', db_id],
            'facet_test_modules': [self._tool('annot', 'ant-facet-test-modules'), 'run', '--h5adstore-root', store,
                                   '--db-id', db_id],
            'ingest_index': [self._tool('ingest', 'ant-ingest-index'), 'build', '--store', store, '--only', db_id],
        }
        if db_id:
            for step in steps:
                started = time.time()
                self.log(f'post step {step}')
                if step == 'manifest':
                    self._manifest(outcome)
                else:
                    code, tail = self.runner.run(f'post:{step}', commands[step], cwd=work,
                                                 timeout=cfg.post_step_timeout)
                    if code != 0:
                        outcome.warnings.append(f'post step {step} failed (exit {code}): {tail.splitlines()[-1] if tail else ""}')
                self._timed(outcome, f'post:{step}', started)
        else:
            outcome.warnings.append('no db id: post steps skipped')
        self._ingest_de(item, outcome, store_dir, work)
        if cfg.push_catalog:
            for suffix in ('.scores.parquet', '.de.parquet'):
                path = store_dir / f'{item.basename}{suffix}'
                if path.exists():
                    try:
                        self._upload(path, self._catalog_key(item, path.name))
                    except Exception as exc:  # noqa: BLE001
                        outcome.warnings.append(f'push of {path.name} failed: {exc}')
        if cfg.store_git_commit:
            self._git_commit(item, outcome, store_dir)
        try:
            self.api.rescan()
        except TransientError as exc:
            outcome.warnings.append(f'final rescan failed: {exc}')

    def _manifest(self, outcome: Outcome) -> None:
        if not self.config.push_catalog:
            return
        try:
            from catalog_manifest import write_manifest  # noqa: PLC0415

            write_manifest(self.config.store_root, self.s3, self.config.bucket,
                           key=f'{self.config.catalog_prefix}_catalog/manifest.json')
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f'catalog manifest refresh failed: {exc}')

    def _ingest_de(self, item: Item, outcome: Outcome, store_dir: Path, work: Path) -> None:
        """DE tables of the project whose sidecar names this dataset (``de_dataset_id`` / ``de_dataset_h5ad``)."""
        data_files = [obj for obj in item.de if not obj.key.endswith('.mus')]
        by_key = {obj.key: obj for obj in item.de}
        if not data_files:
            return
        file_id = (outcome.db_id or '/').split('/')[-1]
        de_dir = work / '_de'
        for obj in data_files:
            mus_obj = by_key.get(obj.key + '.mus')
            name = obj.key.rsplit('/', 1)[-1]
            if mus_obj is None or not name.endswith(DE_DATA_EXTS):
                continue
            parent = obj.key.rsplit('/', 2)[-2]
            staged_mus = de_dir / parent / (name + '.mus')
            try:
                self._pull(mus_obj, staged_mus)
            except TransientError as exc:
                outcome.warnings.append(f'DE sidecar {mus_obj.key}: {exc}')
                continue
            meta = parse_sidecar_text(staged_mus.read_text(encoding='utf-8', errors='replace'))
            if meta.get('de_dataset_id') not in (None, '', file_id) or (
                    not meta.get('de_dataset_id') and meta.get('de_dataset_h5ad') != f'{item.basename}{H5AD_SUFFIX}'):
                continue
            source_dir = self.config.store_root / item.data_project / 'de_sources' / parent
            staged_data = de_dir / parent / name
            try:
                self._pull(obj, staged_data)
            except TransientError as exc:
                outcome.warnings.append(f'DE file {obj.key}: {exc}')
                continue
            if meta.get('sha256') and meta['sha256'] != sha256_file(staged_data):
                outcome.warnings.append(f'DE file {obj.key}: sha256 mismatch, not ingested')
                continue
            place_file(staged_data, source_dir / name)
            place_file(staged_mus, source_dir / (name + '.mus'))
            code, tail = self.runner.run('ant-de-ingest', de_ingest_command(
                store_dir / f'{item.basename}.duckdb', source_dir / name, meta, name), cwd=work)
            if code != 0:
                outcome.warnings.append(f'ant-de-ingest {name} failed (exit {code})')
                continue
            outcome.consumed[obj.key] = obj.etag
            outcome.consumed[mus_obj.key] = mus_obj.etag

    def _git_commit(self, item: Item, outcome: Outcome, store_dir: Path) -> None:
        store = self.config.store_root
        if not (store / '.git').exists():
            outcome.warnings.append(f'{store} is not a git repository: no commit')
            return
        files = [store_dir / f'{item.basename}.md', store_dir / f'{item.basename}{MUS_SUFFIX}']
        index_md = store / item.data_project / 'index.md'
        if index_md.exists():
            files.append(index_md)
        rel = [str(path.relative_to(store)) for path in files if path.exists()]
        name, _, email = self.config.git_identity.partition('<')
        committer = ['-c', f'user.name={name.strip()}', '-c', f'user.email={email.rstrip(">").strip()}']
        sidecar_path = store_dir / f'{item.basename}{MUS_SUFFIX}'
        depositor = md_fill.depositor_of(parse_sidecar_text(sidecar_path.read_text())) if sidecar_path.exists() else None
        message = (f'ingest-watch: {item.display} {outcome.version} ({outcome.kind})\n\n'
                   f'Deposit {item.h5ad.key} (ETag {item.h5ad.etag}), sha256 {outcome.sha256}.\n')
        code, tail = self.runner.run('git add', ['git', '-C', store, 'add', '--', *rel])
        if code != 0:
            outcome.warnings.append(f'git add failed: {tail}')
            return
        cmd = ['git', '-C', store, *committer, 'commit', '--only', '-m', message,
               '--author', depositor or self.config.git_identity, '--', *rel]
        code, tail = self.runner.run('git commit', cmd)
        if code != 0:
            outcome.warnings.append(f'git commit failed: {tail}')
            return
        code, head = self.runner.run('git rev-parse', ['git', '-C', store, 'rev-parse', 'HEAD'])
        outcome.git_commit = head.strip().splitlines()[-1] if code == 0 and head.strip() else None
        if self.config.store_git_push:
            code, tail = self.runner.run('git push', ['git', '-C', store, 'push'])
            if code != 0:
                outcome.warnings.append(f'git push failed: {tail}')


def de_ingest_command(target_duckdb: Path, data_file: Path, meta: Dict[str, str], basename: str) -> List[Any]:
    """The ``ant-de-ingest`` call for one DE table and its sidecar keys (``de_*``, ``de_map_*``)."""
    study_id = meta.get('de_study_id') or next(
        (basename[:-len(ext)] for ext in DE_DATA_EXTS if basename.endswith(ext)), basename.rsplit('.', 1)[0])
    method = meta.get('de_method')
    if not method and meta.get('de_format'):
        try:
            from de_presets import PRESETS  # noqa: PLC0415

            preset = PRESETS.get(meta['de_format'])
            method = preset.default_method if preset else None
        except ImportError:
            method = None
    cmd: List[Any] = [BIN / 'annot' / 'ant-de-ingest', target_duckdb, '--from', data_file,
                      '--preset', meta.get('de_format', ''), '--study-id', study_id, '--replace']
    for flag, key in (('--term', 'de_term'), ('--contrast', 'de_contrast'), ('--description', 'de_description'),
                      ('--covariates', 'de_covariates'), ('--reference-type', 'de_reference_type')):
        if meta.get(key):
            cmd += [flag, meta[key]]
    if method:
        cmd += ['--method', method]
    pairs = [f'{key[len("de_map_"):]}={value}' for key, value in meta.items()
             if key.startswith('de_map_') and key[len('de_map_'):] and value]
    if pairs:
        cmd += ['--map', ','.join(pairs)]
    return cmd
