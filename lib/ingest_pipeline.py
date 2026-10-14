"""One dataset from an S3 deposit to the served store (``ant-ingest`` and ``ant-ingest-watch``).

The deposit layout is the one ``mus s3 upload`` writes (mus
``internal/cli/s3_upload.go``)::

    <prefix><data_project>/[<subfolder>/]<basename>.h5ad
    <prefix><data_project>/[<subfolder>/]<basename>.h5ad.mus     collapsed sidecar, sha256=
    <prefix><data_project>/static/<file>                          --static FILE
    <prefix><data_project>/de/<folder>/<file>(.mus)               DE tables

Every key segment must match :data:`SEGMENT_RE` (no ``..``, no leading ``_``
or ``.``, ASCII); anything else is reported as malformed and never pulled,
placed or deleted, and every write into the store is checked to stay inside
its group folder (:func:`ensure_inside`).

An :class:`Item` is one h5ad with its sidecar. :class:`Pipeline` runs, for one
item: pull (ETag pinned, kept across retries), sha256 against the sidecar,
catalogue ``.md`` (filled for a new dataset, version bump for an update),
``ant-convert`` in the work dir, ``ant-ingest-check`` (errors and held reasons
block; ``access=public`` is always held for an admin), placement into the store
through a :class:`SwapJournal` (staged hidden siblings, hard links into
``_versions/``, renames with the ``.md`` last; an interrupted swap is rolled
forward or back by :func:`recover_swaps` before anything else runs), rescan
and a served check of the new sha256 and version, push of the catalog copy
(verified by HEAD), the post steps, a store git commit and a final rescan.
Anything before placement that fails blocks publication; anything after it is
a warning.

Only what was actually placed or applied is "consumed". :func:`delete_consumed`
is the only function that deletes on S3: the exact consumed keys, each with a
conditional ``DeleteObject`` (``IfMatch``) or, where the store does not support
it, a HEAD immediately before. It never takes a prefix. The legacy h5ad checker
and its umbrella wrapper, which rewrite ``.md`` files, are never called.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import ingest_md_fill as md_fill
import ingest_versions

H5AD_SUFFIX = '.h5ad'
MUS_SUFFIX = '.h5ad.mus'
DE_DATA_EXTS = ('.tsv', '.csv', '.tsv.gz', '.csv.gz')
REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / 'bin'
POST_STEPS = ('score', 'gene_stats', 'module_variability', 'facet_test', 'facet_test_modules',
              'ingest_index', 'manifest')
#: One key segment (data project, subfolder, file name, DE folder).
SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
#: Child exit codes that mean "interrupted", not "the dataset is wrong": retried later.
TRANSIENT_EXIT_CODES = {124, 127, 137, 143}
UPDATE_MARKER_KEYS = ('update_of', 'previous_sha256')

MULTIPART_THRESHOLD = 256 * 1024 ** 2
RESUMABLE_THRESHOLD = 1 * 1024 ** 3
RESUMABLE_CHUNK = 256 * 1024 ** 2
RESUMABLE_WORKERS = 4
RESUMABLE_RETRIES = 15


class TransientError(RuntimeError):
    """A failure worth retrying later (S3 hiccup, object changed, server down, stop requested)."""


class Blocked(RuntimeError):
    """The item must not be published: ``state`` is failed, held or needs_decision."""

    def __init__(self, state: str, message: str, report_path: Optional[str] = None,
                 held_reasons: Optional[List[str]] = None):
        super().__init__(message)
        self.state = state
        self.report_path = report_path
        self.held_reasons = held_reasons or []


# ── key validation ───────────────────────────────────────────────────────────

def valid_segment(segment: str) -> bool:
    """True for a safe key segment: ASCII letters, digits, ``._-``, not starting with ``.`` or ``_``."""
    return (bool(segment) and segment not in ('.', '..') and unicodedata.normalize('NFC', segment) == segment
            and bool(SEGMENT_RE.match(segment)))


def ensure_inside(target: Path, root: Path) -> Path:
    """``target`` resolved; raises ValueError unless it lies inside ``root``."""
    resolved_root = Path(root).resolve()
    resolved = Path(target).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f'refusing to write {resolved}: outside {resolved_root}')
    return resolved


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

    @property
    def name(self) -> str:
        return self.key.rsplit('/', 1)[-1]


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
        """Everything the item may consume (static, seeds, DE of the project); stable over two listings."""
        objs = [self.h5ad] + ([self.mus] if self.mus else []) + self.static + self.de
        return json.dumps(sorted((obj.key, obj.etag, obj.size) for obj in objs))

    def newest_modified(self) -> float:
        objs = [self.h5ad] + ([self.mus] if self.mus else []) + self.static + self.de
        return max(obj.last_modified for obj in objs)

    @property
    def md_seed_name(self) -> str:
        return f'{self.basename}.md'

    def own_static_names(self) -> Set[str]:
        """Static names that belong to this item only: its ``.md`` seed and its ``<basename>.index.md``."""
        return {self.md_seed_name, f'{self.basename}.index.md'}

    def static_for_item(self) -> List[ObjInfo]:
        """Static files this item may use: all but the seeds of the other items of the project."""
        others = set()
        for name in self.other_basenames:
            others |= {f'{name}.md', f'{name}.index.md'}
        return [obj for obj in self.static if obj.name not in others]

    def index_seed(self) -> Optional[ObjInfo]:
        """``static/<basename>.index.md``, else ``static/index.md`` when no other item has its own index seed."""
        by_name = {obj.name: obj for obj in self.static}
        if f'{self.basename}.index.md' in by_name:
            return by_name[f'{self.basename}.index.md']
        if any(f'{name}.index.md' in by_name for name in self.other_basenames):
            return None
        return by_name.get('index.md')


def plan_items(objects: Sequence[Dict[str, Any]], prefix: str) -> Tuple[List[Item], List[str]]:
    """Group a listing of ``prefix`` into items. Returns ``(items, malformed messages)``.

    A key with an unsafe segment is malformed: it is reported and never
    processed, placed or deleted.
    """
    infos: Dict[str, ObjInfo] = {}
    malformed: List[str] = []
    for obj in objects:
        key = obj['Key']
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].split('/')
        if parts[0] == SELFTEST_DIR:
            continue                                   # the IfMatch self-test's scratch object
        if not all(valid_segment(part) for part in parts):
            malformed.append(f'{key}: unsafe key segment (allowed: letters, digits, ._-, not starting with . or _)')
            continue
        infos[key] = ObjInfo.from_listing(obj)
    items: List[Item] = []
    static_by_project: Dict[str, List[ObjInfo]] = {}
    de_by_project: Dict[str, List[ObjInfo]] = {}
    for key in sorted(infos):
        parts = key[len(prefix):].split('/')
        if len(parts) == 3 and parts[1] == 'static':
            static_by_project.setdefault(parts[0], []).append(infos[key])
        elif len(parts) == 4 and parts[1] == 'de':
            de_by_project.setdefault(parts[0], []).append(infos[key])
        elif len(parts) >= 3 and parts[1] in ('static', 'de'):
            malformed.append(f'{key}: expected {prefix}<data_project>/static/<file> or .../de/<folder>/<file>')
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
        basename = filename[:-len(H5AD_SUFFIX)]
        if not valid_segment(basename):
            malformed.append(f'{key}: unsafe dataset name {basename!r}')
            continue
        items.append(Item(prefix, data_project, subfolder, basename, infos[key], infos.get(key + '.mus'),
                          static_by_project.get(data_project, []), de_by_project.get(data_project, [])))
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


def md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - compared with a single-part S3 ETag, not for security
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(2 ** 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


#: Part sizes tried when recomputing a multipart ETag: boto3's default (8 MB), common tool settings,
#: and ant-sync / ant-ingest's 256 MB (MULTIPART_CHUNKSIZE).
MULTIPART_PART_SIZES_MB = (5, 8, 16, 64, 256)
MAX_PARTS = 10000


def candidate_part_sizes(size: int) -> List[int]:
    """Part sizes an uploader may have used for an object of ``size`` bytes.

    The configured sizes (5 MiB: AWS Go SDK, rclone and mus; 8 MiB: boto3;
    16, 64, 256 MiB), each enlarged as boto3 does for more than 10,000 parts
    (s3transfer ``ChunksizeAdjuster``: doubled until the parts fit), plus
    ``ceil(size / 10000)`` rounded up to a whole MiB (the rule of uploaders that
    grow the part size to the minimum that fits).
    """
    mib = 1024 * 1024
    sizes = []
    for size_mb in MULTIPART_PART_SIZES_MB:
        part = size_mb * mib
        while -(-size // part) > MAX_PARTS:
            part *= 2
        sizes.append(part)
    sizes.append(-(-(-(-size // MAX_PARTS)) // mib) * mib)
    return sorted(set(sizes))


def multipart_etag(path: Path, part_size: int) -> str:
    """The S3 ETag of ``path`` uploaded in parts of ``part_size`` bytes: md5(concat(part md5s))-<parts>."""
    digests = []
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(part_size)
            if not chunk:
                break
            digests.append(hashlib.md5(chunk).digest())  # noqa: S324 - S3 ETag, not security
    if not digests:
        digests.append(hashlib.md5(b'').digest())  # noqa: S324
    return hashlib.md5(b''.join(digests)).hexdigest() + f'-{len(digests)}'  # noqa: S324


def etag_matches_file(etag: str, path: Path) -> bool:
    """True when ``etag`` is the single-part MD5 of ``path`` or its multipart ETag for a common part size."""
    etag = etag.strip('"')
    if '-' not in etag:
        return md5_file(path) == etag
    try:
        parts = int(etag.rsplit('-', 1)[1])
    except ValueError:
        return False
    size = path.stat().st_size
    for part_size in candidate_part_sizes(size):
        if -(-size // part_size) == parts and multipart_etag(path, part_size) == etag:
            return True
    return False


#: Fragments of uv, pip and network errors in a tool's output: the tool did not run, retry later.
TRANSIENT_OUTPUT_PATTERNS = ('failed to resolve', 'no solution found', 'failed to download', 'failed to fetch',
                             'connection refused', 'could not connect', 'connection reset', 'timed out',
                             'temporary failure in name resolution', 'network is unreachable',
                             'error sending request', 'name or service not known', 'failed to build')


def transient_output(output: str) -> bool:
    """True when a tool's output says it failed to start (dependency resolution, download, network)."""
    lowered = output.lower()
    return any(pattern in lowered for pattern in TRANSIENT_OUTPUT_PATTERNS)


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


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, 'response', None) or {}
    return str((response.get('Error') or {}).get('Code') or type(exc).__name__)


def delete_consumed(s3: Any, bucket: str, prefix: str, consumed: Dict[str, str],
                    record: Optional[Callable[[str, str, str], None]] = None,
                    keep: Optional[Dict[str, str]] = None, if_match: bool = False) -> Tuple[List[str], List[str]]:
    """Delete the exact incoming keys an item consumed, each only if its ETag is unchanged.

    Args:
        prefix: The incoming prefix; a key outside it is refused.
        consumed: ``{key: etag at pull time}``.
        record: Called as ``record(key, etag, outcome)`` with outcome
            ``deleted`` or ``skipped:<reason>``.
        keep: ``{key: reason}`` of consumed keys that must stay this time
            (shared static files while a sibling item is pending).
        if_match: Also send ``IfMatch`` with the delete. Only when
            :func:`if_match_selftest` showed that the backend enforces it;
            the HEAD comparison right before the delete is done either way.

    Returns:
        ``(deleted keys, skipped keys)``. One ``DeleteObject`` per key, no
        prefix or batch delete.
    """
    deleted: List[str] = []
    skipped: List[str] = []
    keep = keep or {}
    for key, etag in sorted(consumed.items()):
        parts = key[len(prefix):].split('/') if prefix and key.startswith(prefix) else []
        if not parts or not all(valid_segment(part) for part in parts):
            raise ValueError(f'refusing to delete {key!r}: not an object under {prefix!r}')
        if key in keep:
            skipped.append(key)
            if record:
                record(key, etag, f'skipped:{keep[key]}')
            continue
        try:
            outcome = _delete_if_unchanged(s3, bucket, key, etag, if_match)
        except Exception as exc:  # noqa: BLE001 - one failing key never stops the others or the poll
            outcome = f'skipped:error:{_error_code(exc)}'
        (deleted if outcome == 'deleted' else skipped).append(key)
        if record:
            record(key, etag, outcome)
    return deleted, skipped


def _delete_if_unchanged(s3: Any, bucket: str, key: str, etag: str, if_match: bool) -> str:
    """HEAD-compare the ETag, then delete (with ``IfMatch`` when the backend enforces it)."""
    current = head_etag(s3, bucket, key)
    if current is None:
        return 'skipped:absent'
    if current != etag:
        return f'skipped:etag_changed:{current}'
    if not if_match:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            return 'skipped:absent' if code in ('NoSuchKey', '404') else f'skipped:error:{code}'
        return 'deleted'
    try:
        s3.delete_object(Bucket=bucket, Key=key, IfMatch=f'"{etag}"')
        return 'deleted'
    except Exception as exc:  # noqa: BLE001
        code = _error_code(exc)
        if code in ('PreconditionFailed', '412'):
            current = head_etag(s3, bucket, key)
            return 'skipped:absent' if current is None else f'skipped:etag_changed:{current}'
        if code in ('NoSuchKey', '404'):
            return 'skipped:absent'
        return f'skipped:error:{code}'


SELFTEST_DIR = '_selftest'


def _delete_selftest_object(s3: Any, bucket: str, prefix: str, key: str, **kwargs: Any) -> None:
    """Delete the self-test scratch object; refuses any key outside ``<prefix>_selftest/``."""
    rest = key[len(f'{prefix}{SELFTEST_DIR}/'):] if prefix and key.startswith(f'{prefix}{SELFTEST_DIR}/') else ''
    if not rest or '/' in rest or not valid_segment(rest):
        raise ValueError(f'refusing to delete {key!r}: not a self-test object under {prefix}{SELFTEST_DIR}/')
    s3.delete_object(Bucket=bucket, Key=key, **kwargs)


def if_match_selftest(s3: Any, bucket: str, prefix: str) -> str:
    """Does the backend refuse a ``DeleteObject`` whose ``IfMatch`` does not match?

    Writes a scratch object ``<prefix>_selftest/<random>`` twice (two ETags),
    tries to delete it with the first ETag and looks whether it survived; then
    removes the scratch object. Returns ``enforced``, ``ignored`` (deleted
    anyway), ``unsupported:<error code>`` or ``error:<text>``. Only this one
    scratch key is ever written or deleted here.
    """
    import uuid  # noqa: PLC0415

    key = f'{prefix}{SELFTEST_DIR}/{uuid.uuid4().hex}'
    if not key.startswith(f'{prefix}{SELFTEST_DIR}/'):
        raise ValueError(key)
    result = 'error:not run'
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b'anthive if-match self-test 1')
        first = head_etag(s3, bucket, key)
        s3.put_object(Bucket=bucket, Key=key, Body=b'anthive if-match self-test 2')
        second = head_etag(s3, bucket, key)
        if not first or not second or first == second:
            return 'error:could not create two versions of the scratch object'
        try:
            _delete_selftest_object(s3, bucket, prefix, key, IfMatch=f'"{first}"')
            result = 'ignored' if head_etag(s3, bucket, key) is None else 'enforced'
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            result = 'enforced' if code in ('PreconditionFailed', '412') and head_etag(s3, bucket, key) else \
                f'unsupported:{code}'
    except Exception as exc:  # noqa: BLE001
        result = f'error:{type(exc).__name__}: {exc}'[:200]
    finally:
        try:
            if head_etag(s3, bucket, key) is not None:
                _delete_selftest_object(s3, bucket, prefix, key)       # the scratch object of this test only
        except Exception:  # noqa: BLE001
            pass
    return result


# ── subprocesses and the server ──────────────────────────────────────────────

class SubprocessRunner:
    """Runs the ``ant-*`` tools; output goes to one log file per day."""

    def __init__(self, log_path: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        self.log_path = log_path
        self.env = env

    def run(self, label: str, cmd: Sequence[str], cwd: Optional[Path] = None,
            timeout: Optional[float] = None) -> Tuple[int, str]:
        """Run ``cmd``; returns ``(exit code, last 30 lines of output)``. 127: not found, 124: timeout."""
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


def transient_exit(code: int) -> bool:
    """A child that was killed, timed out or not found: retry, do not blame the dataset."""
    return code < 0 or code in TRANSIENT_EXIT_CODES


class ApiClient:
    """Local ant-serve calls (loopback local tool, or the internal secret header)."""

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None,
                 retry_delays: Sequence[float] = (5, 10, 20, 40, 60, 60, 60)):
        self.base_url = base_url.rstrip('/')
        self.headers = headers or {}
        self.retry_delays = list(retry_delays)

    def _request(self, path: str, method: str = 'POST', timeout: float = 600) -> Dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, data=b'{}' if method == 'POST' else None,
                                         method=method, headers={'Content-Type': 'application/json', **self.headers})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}')

    def post_with_retry(self, path: str) -> Dict[str, Any]:
        """POST with backoff while the server is down or restarting; raises the last error."""
        last_error: Optional[Exception] = None
        for delay in [0.0] + self.retry_delays:
            if delay:
                time.sleep(delay)
            try:
                return self._request(path)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
        raise TransientError(f'POST {path} failed: {last_error}')

    def rescan(self) -> Dict[str, Any]:
        return self.post_with_retry('/admin/rescan')

    def release(self, db_id: str) -> Dict[str, Any]:
        return self.post_with_retry(f'/admin/release/{db_id}')

    def restore(self, db_id: str) -> Dict[str, Any]:
        return self.post_with_retry(f'/admin/restore/{db_id}')

    def info(self, db_id: str) -> Dict[str, Any]:
        """``GET /databases/{db_id}/info`` (version and sha256 of the served dataset)."""
        try:
            return self._request(f'/databases/{urllib.parse.quote(db_id)}/info', method='GET', timeout=60)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransientError(f'GET info of {db_id} failed: {exc}') from exc


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


def place_file(source: Path, target: Path, partial: Optional[Path] = None) -> None:
    """Put ``source`` at ``target`` atomically (rename, or copy to a partial name then rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
        return
    except OSError:
        pass
    partial = partial or target.with_name(f'.{target.name}.ingest-partial')
    shutil.copy2(source, partial)
    os.replace(partial, target)


# ── swap journal ─────────────────────────────────────────────────────────────

class SwapJournal:
    """Crash-safe placement of a dataset version into the store.

    1. ``stage``: copy each new file next to its target as a hidden sibling
       (``.<name>.ingest-new``); ``link``: hard-link a current file into the
       version folder. Nothing visible changes.
    2. ``commit``: mark the journal ``committing``, rename every hidden sibling
       over its target (the ``.md`` last), remove the files listed for removal,
       delete the journal.
    3. :func:`recover_swaps` (run before any item is processed): a journal in
       ``staged`` is rolled back (hidden files and links removed); one in
       ``committing`` is rolled forward.
    """

    def __init__(self, journal_dir: Path, name: str, root: Path, meta: Optional[Dict[str, Any]] = None):
        self.path = Path(journal_dir) / f'{name}.json'
        self.root = Path(root)
        self.data: Dict[str, Any] = {'phase': 'staged', 'root': str(root), 'entries': [], 'created': [],
                                     'remove': [], 'meta': meta or {}, 'started': _now_iso()}

    @classmethod
    def load(cls, path: Path) -> 'SwapJournal':
        data = json.loads(Path(path).read_text())
        journal = cls(Path(path).parent, Path(path).stem, Path(data['root']))
        journal.data = data
        return journal

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix('.json.partial')
        partial.write_text(json.dumps(self.data, indent=1))
        os.replace(partial, self.path)

    def stage(self, source: Path, target: Path) -> None:
        ensure_inside(target.parent, self.root)
        hidden = target.with_name(f'.{target.name}.ingest-new')
        partial = target.with_name(f'.{target.name}.ingest-partial')      # a cross-device copy in progress
        self.data['entries'].append({'hidden': str(hidden), 'final': str(target), 'partial': str(partial)})
        self._save()
        target.parent.mkdir(parents=True, exist_ok=True)
        place_file(source, hidden, partial)

    def link(self, source: Path, destination: Path) -> None:
        ensure_inside(destination.parent, self.root)
        self.data['created'].append(str(destination))
        self._save()
        ingest_versions.link_or_copy(source, destination)

    def remove_after(self, path: Path) -> None:
        ensure_inside(path, self.root)
        self.data['remove'].append(str(path))

    def commit(self) -> None:
        self.data['phase'] = 'committing'
        self._save()
        entries = sorted(self.data['entries'], key=lambda entry: entry['final'].endswith('.md'))
        # Test hook for the end-to-end check of crash recovery: a pause after each rename, so a
        # process can be killed in the middle of a commit. Unset in normal operation.
        pause = float(os.environ.get('ANTHIVE_INGEST_SWAP_PAUSE_SECONDS', '0') or 0)
        for entry in entries:
            hidden, final = Path(entry['hidden']), Path(entry['final'])
            if hidden.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(hidden, final)
                if pause:
                    time.sleep(pause)
        for path in self.data['remove']:
            Path(path).unlink(missing_ok=True)
        for entry in self.data['entries']:
            if entry.get('partial'):
                Path(entry['partial']).unlink(missing_ok=True)
        meta = self.data.get('meta') or {}
        if meta.get('post_pending'):          # the publication is finished only after push and post steps
            write_resume(self.path.parent, self.path.stem, {**meta, 'done_steps': [], 'pushed': False})
        self.path.unlink(missing_ok=True)

    def rollback(self) -> None:
        for entry in self.data['entries']:
            Path(entry['hidden']).unlink(missing_ok=True)
            if entry.get('partial'):
                Path(entry['partial']).unlink(missing_ok=True)
        for path in reversed(self.data['created']):
            created = Path(path)
            created.unlink(missing_ok=True)
            for parent in (created.parent, created.parent.parent, created.parent.parent.parent):
                try:
                    if parent.name and parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    break
        self.path.unlink(missing_ok=True)


def resume_path(journal_dir: Path, name: str) -> Path:
    return Path(journal_dir) / 'resume' / f'{name}.json'


def write_resume(journal_dir: Path, name: str, data: Dict[str, Any]) -> None:
    """Write (atomically) the marker of a publication whose push or post steps are not finished."""
    path = resume_path(journal_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.json.partial')
    partial.write_text(json.dumps(data, indent=1))
    os.replace(partial, path)


def recover_swaps(journal_dir: Path, log: Callable[[str], None] = print,
                  restore: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Finish or undo every interrupted swap. Returns one message per journal.

    ``restore(db_id)`` is called for a swap that had released its dataset in
    ant-serve, so a crash between release and restore does not leave it offline.
    """
    messages: List[str] = []
    folder = Path(journal_dir)
    if not folder.is_dir():
        return messages
    for path in sorted(folder.glob('*.json')):
        journal = SwapJournal.load(path)
        if journal.data.get('phase') == 'committing':
            journal.commit()
            message = f'swap {path.stem}: rolled forward'
            if (journal.data.get('meta') or {}).get('post_pending'):
                message += ', post steps pending'       # commit() wrote the resume marker
        else:
            journal.rollback()
            message = f'swap {path.stem}: rolled back'
        db_id = (journal.data.get('meta') or {}).get('released')
        if db_id and restore is not None:
            try:
                restore(db_id)
                message += f', {db_id} restored in ant-serve'
            except Exception as exc:  # noqa: BLE001 - the server may be down; it reopens on its next start
                message += f', restore of {db_id} failed ({exc})'
        log(message)
        messages.append(message)
    return messages


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
    min_free_gb: float = 50.0
    require_update_marker: bool = False

    @property
    def journal_dir(self) -> Path:
        return (self.state_dir or self.work_root) / 'swaps'


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
    shared_keys: List[str] = field(default_factory=list)
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
    written: Dict[str, str] = field(default_factory=dict)       # store file -> sha256 the route wrote
    resumed: bool = False
    post_failed: List[str] = field(default_factory=list)          # post steps that ended with an error
    finished_pending: Optional['Outcome'] = None                  # an earlier publication finished first

    @property
    def deletable(self) -> bool:
        """True when the consumed incoming keys may be deleted (live, served, catalog copy verified)."""
        return self.state == 'live' and self.served and self.catalog_verified and bool(self.consumed)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def slug(key: str) -> str:
    """Work-dir and journal name of an item key."""
    return hashlib.sha1(key.encode()).hexdigest()[:12] + '_' + key.rsplit('/', 1)[-1].replace('.', '_')[:40]


def _norm(value: Any) -> str:
    return str(value or '').strip().lower()


class Pipeline:
    """Runs one :class:`Item` from the deposit to the served store."""

    def __init__(self, config: PipelineConfig, s3: Any, runner: Any, api: Any,
                 log: Callable[[str], None] = print, stop_requested: Callable[[], bool] = lambda: False):
        self.config = config
        self.s3 = s3
        self.runner = runner
        self.api = api
        self.log = log
        self.stop_requested = stop_requested

    # helpers --------------------------------------------------------------

    def _tool(self, domain: str, name: str) -> Path:
        return BIN / domain / name

    def _timed(self, outcome: Outcome, label: str, started: float) -> None:
        outcome.timings[label] = round(time.time() - started, 1)

    def _checkpoint(self, step: str) -> None:
        if self.stop_requested():
            raise TransientError(f'stop requested before {step}')

    def recover(self) -> List[str]:
        """Roll interrupted swaps forward or back; call before processing any item."""
        return recover_swaps(self.config.journal_dir, self.log, restore=self.api.restore)

    def _pull(self, obj: ObjInfo, dest: Path, manifest: Optional[Dict[str, str]] = None) -> None:
        """Download one object, pinned to the ETag of the listing; reuse a complete earlier pull."""
        if manifest is not None and manifest.get(obj.key) == obj.etag and dest.exists() \
                and dest.stat().st_size == obj.size:
            return
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
        if manifest is not None:
            manifest[obj.key] = obj.etag

    def _check_free_space(self, size: int) -> None:
        needed = 2 * size + self.config.min_free_gb * 1e9
        free = shutil.disk_usage(self.config.work_root).free
        if free < needed:
            raise TransientError(f'only {free / 1e9:.1f} GB free under {self.config.work_root}, need '
                                 f'{needed / 1e9:.1f} GB (2x the h5ad + {self.config.min_free_gb:g} GB)')

    def _db_id(self, index_path: Path, md_meta: Dict[str, Any]) -> Optional[str]:
        from metadata import parse_md_file  # noqa: PLC0415

        index_meta = parse_md_file(index_path)[0] if index_path.exists() else {}
        group_id = md_meta.get('group_id') or index_meta.get('group_id')
        return f'{group_id}/{md_meta["id"]}' if group_id and md_meta.get('id') else None

    def _catalog_key(self, item: Item, name: str) -> str:
        return f'{self.config.catalog_prefix}{item.rel_dir}/{name}'

    # main -----------------------------------------------------------------

    def process(self, item: Item, approval: Optional[Dict[str, Any]] = None) -> Outcome:
        """Run every step for ``item``. Raises :class:`TransientError` for a retry later.

        ``approval`` (``{'by', 'reasons', 'date'}``, from ``ant-ingest-watch
        approve``) lets an item through the held reasons it names; errors
        still block.
        """
        outcome = Outcome()
        try:
            self._process(item, outcome, approval)
        except Blocked as blocked:
            outcome.state = blocked.state
            outcome.detail = str(blocked)
            outcome.report_path = blocked.report_path or outcome.report_path
            outcome.held_reasons = blocked.held_reasons
            outcome.consumed = {}
            outcome.shared_keys = []
        return outcome

    def _prepare_work(self, item: Item) -> Tuple[Path, Dict[str, str]]:
        """The item's work dir: keeps earlier pulls whose ETag is unchanged, rebuilds the staging."""
        work = self.config.work_root / slug(item.key)
        pulled = work / '_pull'
        manifest_path = work / 'pulled.json'
        manifest: Dict[str, str] = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except ValueError:
                manifest = {}
        current = {obj.key: obj.etag for obj in [item.h5ad, item.mus] + item.static + item.de if obj}
        manifest = {key: etag for key, etag in manifest.items() if current.get(key) == etag}
        if work.exists():
            for entry in work.iterdir():
                if entry.name in ('_pull', 'pulled.json'):
                    continue
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        if pulled.exists():
            wanted = {hashlib.sha1(key.encode()).hexdigest() for key in manifest}
            in_progress = {hashlib.sha1(obj.key.encode()).hexdigest() for obj in [item.h5ad] if obj}
            for entry in pulled.iterdir():
                stem = entry.name.split('.')[0]
                if stem not in wanted and stem not in in_progress:
                    entry.unlink()
        pulled.mkdir(parents=True, exist_ok=True)
        return work, manifest

    def _pull_cached(self, obj: ObjInfo, work: Path, manifest: Dict[str, str]) -> Path:
        dest = work / '_pull' / (hashlib.sha1(obj.key.encode()).hexdigest() + '.bin')
        self._pull(obj, dest, manifest)
        (work / 'pulled.json').write_text(json.dumps(manifest))
        return dest

    def _process(self, item: Item, outcome: Outcome, approval: Optional[Dict[str, Any]]) -> None:
        from metadata import parse_md_file, write_md_file  # noqa: PLC0415

        cfg = self.config
        today = md_fill.today_iso()
        if item.mus is None:
            raise Blocked('failed', f'no sidecar {item.key}.mus')
        store_group = cfg.store_root / item.data_project
        store_dir = cfg.store_root / item.rel_dir
        ensure_inside(store_dir, cfg.store_root)
        store_h5ad = store_dir / f'{item.basename}{H5AD_SUFFIX}'
        store_md = store_dir / f'{item.basename}.md'
        store_mus = store_dir / f'{item.basename}{MUS_SUFFIX}'
        store_index = store_group / 'index.md'
        outcome.store_h5ad = str(store_h5ad)

        # 0. store states the route cannot reconcile, before any download
        versions_dir = store_dir / ingest_versions.VERSIONS_DIR / item.basename
        if store_h5ad.exists() and not store_md.exists():
            raise Blocked('needs_decision', f'{store_h5ad} exists without a .md; decide by hand what it is')
        if store_md.exists() and not store_h5ad.exists():
            raise Blocked('needs_decision', f'{store_md} exists without its h5ad (S3-only or retired dataset?)')
        if versions_dir.exists() and not store_md.exists():
            raise Blocked('needs_decision', f'{versions_dir} holds archived versions but there is no current '
                                            f'{store_md.name}: an interrupted update? Restore by hand')
        hidden = sorted(path for path in store_dir.glob(f'.{item.basename}.*')
                        if '.ingest-' in path.name) if store_dir.exists() else []
        if hidden:
            raise Blocked('needs_decision', f'staged files without a swap journal in {store_dir}: '
                                            f'{", ".join(path.name for path in hidden[:3])}')
        is_update = store_h5ad.exists()
        if not is_update and cfg.push_catalog:
            catalog_h5ad = self._catalog_key(item, f'{item.basename}{H5AD_SUFFIX}')
            if head_etag(self.s3, cfg.bucket, catalog_h5ad) is not None:
                raise Blocked('needs_decision', f'{catalog_h5ad} exists on S3 but there is no local dataset: '
                                                f'pull it (ant sync pull) or retire it before depositing again')

        # 0b. an earlier publication of this item whose push or post steps did not finish
        marker = self._load_resume(item)
        if marker:
            from metadata import parse_md_file as _parse  # noqa: PLC0415

            store_meta = _parse(store_md)[0] if store_md.exists() else {}
            if str(marker.get('to')) != str(store_meta.get('version')) or marker.get('sha256') != store_meta.get('sha256'):
                outcome.warnings.append(f'stale resume marker for version {marker.get("to")} removed: the store '
                                        f'is at {store_meta.get("version")}; its post steps were not run')
                self._resume_path(item).unlink(missing_ok=True)
            else:
                outcome.finished_pending = self._finish_pending(item, marker, store_dir)
                outcome.warnings.extend(outcome.finished_pending.warnings)

        # 1. pull, pinned to the listed ETags, kept across retries
        self._checkpoint('the pull')
        started = time.time()
        work, manifest = self._prepare_work(item)
        group_dir = work / item.data_project              # the staging group folder ant-ingest-check reads
        group_dir.mkdir(parents=True, exist_ok=True)
        reports = cfg.reports_root / slug(item.key)
        reports.mkdir(parents=True, exist_ok=True)
        staged_h5ad = group_dir / f'{item.basename}{H5AD_SUFFIX}'
        staged_mus = group_dir / f'{item.basename}{MUS_SUFFIX}'
        staged_md = group_dir / f'{item.basename}.md'
        if manifest.get(item.h5ad.key) != item.h5ad.etag:
            self._check_free_space(item.h5ad.size)
        self.log(f'pull {item.mus.key}, {item.h5ad.key} ({item.h5ad.size / 1e6:.1f} MB)')
        shutil.copy2(self._pull_cached(item.mus, work, manifest), staged_mus)
        ingest_versions.link_or_copy(self._pull_cached(item.h5ad, work, manifest), staged_h5ad)
        static_files: Dict[str, Tuple[ObjInfo, Path]] = {}
        for obj in item.static_for_item():
            static_files[obj.name] = (obj, self._pull_cached(obj, work, manifest))
        self._timed(outcome, 'pull', started)

        # 2. sha256 and update markers against the sidecar
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
        depositor = md_fill.depositor_of(sidecar)
        ingest = md_fill.ingest_block(cfg.route, item.h5ad.key, item.h5ad.etag, depositor, today)
        notes = [note for note in [md_fill.depositor_note(sidecar)] if note]
        approval_caveat = None
        if approval:
            approval_caveat = md_fill.curation_caveat(
                f'Published after review by {approval.get("by")} on {approval.get("date", today)} '
                f'(held reasons approved: {", ".join(approval.get("reasons") or []) or "none"})'
                + (f': {approval["note"]}' if approval.get('note') else '.'), 'admin_approval', today)
            notes.append(f'Caveat: {approval_caveat["message"]}')

        # 3. static files: placed only when new or identical; a differing file needs a person
        md_seed = static_files.get(item.md_seed_name)
        index_obj = item.index_seed()
        index_seed = static_files.get(index_obj.name) if index_obj else None
        seed_names = {item.md_seed_name} | ({index_obj.name} if index_obj else set())
        plain_static = {name: pair for name, pair in static_files.items()
                        if name not in seed_names and name not in item.own_static_names()
                        and not name.endswith('.index.md') and name != 'index.md'}
        for name, (obj, path) in plain_static.items():
            target = store_group / 'static' / name
            if target.exists() and sha256_file(target) != sha256_file(path):
                raise Blocked('needs_decision', f'static file {name} differs from {target}; replace one of them '
                                                f'by hand (the deposit stays in incoming)')

        # 4. catalogue .md: new dataset or update
        started = time.time()
        old_sha = sha256_file(store_h5ad) if is_update else ''
        outcome.previous_sha256 = old_sha or None
        new_profile = md_fill.profile_h5ad(staged_h5ad, with_layer_stats=not is_update or old_sha != actual)
        index_placed = False                         # a new group's index.md is staged for placement
        index_consumed = False                       # the deposited index seed was used or equals the store
        seed_md_applied = False

        def index_seed_matches_store() -> bool:
            if not index_seed or not store_index.exists():
                return False
            if parse_md_file(index_seed[1])[0] == parse_md_file(store_index)[0]:
                return True
            outcome.warnings.append(f'{index_obj.name} in the deposit differs from the store index.md: '
                                    f'not applied, left in incoming')
            return False
        if not is_update:
            if any(sidecar.get(key) for key in UPDATE_MARKER_KEYS):
                raise Blocked('needs_decision', f'the sidecar declares an update ({", ".join(k for k in UPDATE_MARKER_KEYS if sidecar.get(k))}) '
                                                f'but {store_md} does not exist')
            from sidecar_merge import S3_KEYS  # noqa: PLC0415

            kept = [line for line in sidecar_text.splitlines()
                    if line.partition('=')[0].strip() not in S3_KEYS]     # stamps of the incoming copy
            staged_mus.write_text('\n'.join(kept) + '\n')
            sidecar_text = staged_mus.read_text()
            curation_notes: List[Tuple[str, str]] = []
            if not annotation.get('access'):
                with open(staged_mus, 'a', encoding='utf-8') as handle:
                    handle.write(('' if sidecar_text.endswith('\n') else '\n') + 'access=internal\n')
                annotation['access'] = 'internal'
                curation_notes.append(('access_default', 'The deposit did not state access; the ingestion route '
                                                         'set access=internal (login only), the default for new '
                                                         'datasets.'))
            seed_meta, seed_body = parse_md_file(md_seed[1]) if md_seed else ({}, '')
            if store_index.exists():
                index_meta = parse_md_file(store_index)[0]
                index_consumed = index_seed_matches_store()
            else:
                index_consumed = index_seed is not None
                if index_seed:
                    index_meta, index_body = parse_md_file(index_seed[1])
                else:
                    from md_check import collect_db_ids  # noqa: PLC0415

                    taken_groups = {db_id.split('/')[0] for db_id in collect_db_ids(cfg.store_root)}
                    index_meta, index_body = md_fill.build_index_md(sidecar, item.data_project, taken_groups,
                                                                    item.basename)
                index_meta.setdefault('group', item.data_project)
                write_md_file(group_dir / 'index.md', index_meta, index_body)
                index_placed = True
            from md_check import collect_db_ids  # noqa: PLC0415

            meta, filled = md_fill.fill_catalog_fields(seed_meta, new_profile, actual, annotation, item.basename,
                                                       index_meta.get('group_id'), collect_db_ids(cfg.store_root),
                                                       today, ingest)
            for topic, message in curation_notes:
                meta.setdefault('caveats', []).append(md_fill.curation_caveat(message, topic, today))
                meta['changelog'][-1]['changes'].append(f'Caveat: {message}')
            if approval_caveat:
                meta.setdefault('caveats', []).append(approval_caveat)
            meta['changelog'][-1]['changes'].extend(note for note in notes)
            body = seed_body or f'# {meta.get("title", item.basename)}\n\n{meta.get("description", "")}\n'
            write_md_file(staged_md, meta, body)
            seed_md_applied = md_seed is not None
            outcome.kind = 'new'
            outcome.version = str(meta.get('version'))
            outcome.db_id = self._db_id(store_index if store_index.exists() else group_dir / 'index.md', meta)
            self.log(f'new dataset {item.display} ({outcome.db_id}), filled {", ".join(filled)}')
        else:
            old_text = store_md.read_text(encoding='utf-8')
            old_meta = parse_md_file(store_md)[0]
            index_consumed = index_seed_matches_store()
            outcome.previous_version = str(old_meta.get('version') or '0.0.0')
            outcome.db_id = self._db_id(store_index, old_meta)
            update_of = sidecar.get('update_of', '').strip()
            if update_of and update_of.upper() != (outcome.db_id or '').upper():
                raise Blocked('needs_decision', f'the sidecar says update_of={update_of} but {store_md} is '
                                                f'{outcome.db_id}: a name collision? Rename one of them')
            previous = sidecar.get('previous_sha256', '').strip()
            if previous and previous != old_sha:
                raise Blocked('needs_decision', f'the sidecar says previous_sha256={previous[:12]} but the store '
                                                f'h5ad is {old_sha[:12]}: not an update of this dataset?')
            if cfg.require_update_marker and not (update_of or previous):
                raise Blocked('needs_decision', f'{outcome.db_id} exists; an update needs update_of= or '
                                                f'previous_sha256= in the sidecar (ingest_watch.require_update_marker)')
            merge = md_fill.merge_sidecar_store_wins(
                store_mus.read_text(encoding='utf-8') if store_mus.exists() else '', staged_mus.read_text())
            if merge.guarded:
                changed = ', '.join(f'{key}: {old or "-"} -> {new}' for key, old, new in merge.guarded)
                raise Blocked('needs_decision', f'the update changes {changed}; access, data_use and license '
                                                f'are changed by hand')
            staged_mus.write_text(merge.text)
            for key, store_value, deposit_value in merge.conflicts:
                notes.append(f'Sidecar {key}: kept the curated {store_value!r}; the deposit said {deposit_value!r} '
                             f'(set override_{key}=true to replace it).')
            extra_changes: List[str] = []
            extra_caveats: List[Dict[str, Any]] = []
            if md_seed:
                seed_meta = parse_md_file(md_seed[1])[0]
                new_caveats, seed_conflicts = md_fill.seed_md_delta(seed_meta, old_meta)
                if seed_conflicts:                  # nothing of it is applied, as the warning says
                    outcome.warnings.append(f'{item.md_seed_name} in the deposit differs from the store in '
                                            f'{", ".join(seed_conflicts)}: not applied (its caveats neither), '
                                            f'left in incoming')
                else:
                    extra_caveats.extend(new_caveats)
                    extra_changes.extend(f'Caveat from the deposited {item.md_seed_name}: {caveat["message"]}'
                                         for caveat in new_caveats)
                    seed_md_applied = True
            data_changed = old_sha != actual
            old_profile = md_fill.profile_h5ad(store_h5ad, with_layer_stats=False) if data_changed else None
            plan = md_fill.plan_update(old_meta, old_profile, new_profile, old_sha, actual, merge.changes,
                                       extra_changes=extra_changes, notes=notes)
            if plan.part is None:
                if approval_caveat:
                    outcome.warnings.append('approval recorded, but the upload changes nothing')
                outcome.kind = 'unchanged'
                outcome.version = outcome.previous_version
                self._timed(outcome, 'catalogue', started)
                pending = outcome.finished_pending
                if pending is not None and pending.version == outcome.version:
                    for name in ('kind', 'previous_version', 'previous_sha256', 'archived_local', 'written'):
                        setattr(outcome, name, getattr(pending, name))
                    outcome.post_failed = list(pending.post_failed)
                    outcome.resumed = True
                self._publish_unchanged(item, outcome, store_dir, actual, plain_static, store_group,
                                        md_seed if seed_md_applied else None,
                                        index_seed if index_consumed else None)
                if outcome.resumed:
                    outcome.detail = f'{outcome.kind} {outcome.version} (finished after an interruption)'
                    outcome.catalog_verified = outcome.catalog_verified or pending.catalog_verified
                shutil.rmtree(work, ignore_errors=True)
                return
            caveat = md_fill.curation_caveat(
                f'Version {outcome.previous_version} was replaced by a deposit on {today} '
                f'({"new data" if data_changed else "metadata only"}): ' + ' '.join(plan.changes[:4]),
                'dataset_update', today)
            if approval_caveat:
                extra_caveats.append(approval_caveat)
            try:
                new_text = md_fill.update_md_text(old_text, plan, actual, new_profile, today, caveat=caveat,
                                                  ingest=ingest, extra_caveats=extra_caveats)
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

        # 5. convert in the work dir (not for a metadata-only update)
        staged_duckdb = group_dir / f'{item.basename}.duckdb'
        if outcome.kind != 'update_patch':
            self._checkpoint('the convert')
            started = time.time()
            code, tail = self.runner.run('ant-convert', [self._tool('ingest', 'ant-convert'), staged_h5ad], cwd=work)
            self._timed(outcome, 'convert', started)
            if transient_exit(code) or self.stop_requested() or (code != 0 and transient_output(tail)):
                raise TransientError(f'ant-convert did not run through (exit {code}): {tail[-300:]}')
            if code != 0 or not staged_duckdb.exists():
                raise Blocked('failed', f'ant-convert failed (exit {code}): {tail[-1500:]}')

        # 6. ant-ingest-check (read-only); errors and held reasons block
        self._checkpoint('ant-ingest-check')
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
        if transient_exit(code) or (not report_json.exists() and (self.stop_requested() or transient_output(tail))):
            raise TransientError(f'ant-ingest-check did not run through (exit {code}): {tail[-300:]}')
        if code == 2 or not report_json.exists():
            raise Blocked('failed', f'ant-ingest-check could not run (exit {code}): {tail[-1500:]}', str(report_json))
        report = json.loads(report_json.read_text())
        checks = [entry for entry in report.get('checks', []) if isinstance(entry, dict)]
        if report.get('result') == 'fail':
            names = ', '.join(entry.get('id', '?') for entry in checks if entry.get('status') == 'fail')
            raise Blocked('failed', f'ant-ingest-check found errors: {names or "see report"}', str(report_json))
        held = list(report.get('held_reasons') or []) if report.get('publish') == 'held' else []
        effective_access = _norm(parse_sidecar_text(staged_mus.read_text()).get('access'))
        if effective_access == 'public' and 'public_access' not in held:
            held.append('public_access')            # the watch route always holds public data for an admin
        approved = set((approval or {}).get('reasons') or [])
        unapproved = [reason for reason in held if reason not in approved]
        if unapproved:
            raise Blocked('held', f'held for an admin: {", ".join(unapproved)}', str(report_json), unapproved)
        warnings = [entry.get('id', '?') for entry in checks if entry.get('status') == 'warn']
        if warnings:
            outcome.warnings.append(f'ingest-check warnings: {", ".join(warnings)}')
        if not outcome.db_id:
            outcome.db_id = (report.get('dataset') or {}).get('db_id')
        self.log(f'ant-ingest-check passed ({len(warnings)} warnings); publishing {outcome.db_id}')

        # 7. publish through the swap journal (update: archive S3 first, release, swap, restore)
        self._checkpoint('the swap')
        started = time.time()
        if is_update:
            placed_static = self._swap_update(item, outcome, group_dir, store_dir, plain_static, store_group,
                                              data_changed=outcome.kind == 'update_minor')
        else:
            placed_static = self._swap_new(item, outcome, group_dir, store_dir, store_group, plain_static,
                                           place_index=index_placed)
        self._timed(outcome, 'publish', started)
        outcome.state = 'live'
        outcome.detail = f'{outcome.kind} {outcome.version}'
        self.log(f'placed in {store_dir} ({outcome.kind} {outcome.version})')

        # what was consumed: only what was placed or applied
        outcome.consumed[item.h5ad.key] = item.h5ad.etag
        outcome.consumed[item.mus.key] = item.mus.etag
        for name in placed_static:
            obj = plain_static[name][0]
            outcome.consumed[obj.key] = obj.etag
            outcome.shared_keys.append(obj.key)
        if md_seed and seed_md_applied:
            outcome.consumed[md_seed[0].key] = md_seed[0].etag
        if index_seed and index_consumed:
            outcome.consumed[index_seed[0].key] = index_seed[0].etag
            if index_seed[0].name == 'index.md':
                outcome.shared_keys.append(index_seed[0].key)

        # 8. rescan and check that the new version is served
        self._rescan_and_verify(outcome)

        # 9. catalog copy on S3 (verified by HEAD)
        if cfg.push_catalog:
            started = time.time()
            self._push_catalog(item, outcome, store_dir, include_h5ad=outcome.kind != 'update_patch')
            self._timed(outcome, 'push', started)
            self._update_resume(item, pushed=True)
            self.log(f'catalog push: {len(outcome.catalog_keys)} object(s), verified {outcome.catalog_verified}')

        # 10. post steps, DE, derived push, git, final rescan: warnings only
        self._post(item, outcome, store_dir, work)
        shutil.rmtree(work, ignore_errors=True)

    # publication ----------------------------------------------------------

    def _staged_files(self, group_dir: Path, basename: str) -> List[Path]:
        """Files of the staged dataset (the journal renames the ``.md`` last)."""
        files = sorted(group_dir.glob(f'{basename}.layer.*.parquet'))
        for suffix in ('.de.parquet', H5AD_SUFFIX, MUS_SUFFIX, '.duckdb', '.md'):
            path = group_dir / f'{basename}{suffix}'
            if path.exists():
                files.append(path)
        return files

    def _journal_meta(self, item: Item, outcome: Outcome, version_from: Optional[str]) -> Dict[str, Any]:
        """What recovery needs to finish an interrupted publication (post steps are still pending then)."""
        return {'item': item.key, 'kind': outcome.kind, 'from': version_from, 'to': outcome.version,
                'sha256': outcome.sha256, 'previous_sha256': outcome.previous_sha256, 'db_id': outcome.db_id,
                'post_pending': True, 'written': {}, 'released': None, 'archived_local': []}

    def _stage_static(self, journal: 'SwapJournal', plain_static: Dict[str, Tuple[ObjInfo, Path]],
                      store_group: Path, outcome: Outcome) -> List[str]:
        """Stage new static files in the journal; identical ones count as placed. Returns their names."""
        placed: List[str] = []
        for name, (_obj, path) in sorted(plain_static.items()):
            target = store_group / 'static' / name
            ensure_inside(target, store_group)
            if target.exists():
                if sha256_file(target) == sha256_file(path):
                    placed.append(name)
                else:
                    outcome.warnings.append(f'static file {name} differs from the store copy: left in incoming')
                continue
            journal.stage(path, target)
            placed.append(name)
        return placed

    def _record_written(self, journal: 'SwapJournal', outcome: Outcome) -> None:
        """sha256 of the catalogue files the route is about to write (for the store git commit)."""
        for entry in journal.data['entries']:
            final = entry['final']
            if final.endswith(('.md', MUS_SUFFIX)) and '/static/' not in final:
                outcome.written[final] = sha256_file(Path(entry['hidden']))
        journal.data['meta']['written'] = dict(outcome.written)
        journal._save()

    def _swap_new(self, item: Item, outcome: Outcome, group_dir: Path, store_dir: Path, store_group: Path,
                  plain_static: Dict[str, Tuple[ObjInfo, Path]], place_index: bool) -> List[str]:
        cfg = self.config
        store_dir.mkdir(parents=True, exist_ok=True)
        journal = SwapJournal(cfg.journal_dir, slug(item.key), store_group, self._journal_meta(item, outcome, None))
        staged_index = group_dir / 'index.md'
        try:
            if place_index and staged_index.exists() and not (store_group / 'index.md').exists():
                journal.stage(staged_index, store_group / 'index.md')
            for path in self._staged_files(group_dir, item.basename):
                journal.stage(path, store_dir / path.name)
            placed = self._stage_static(journal, plain_static, store_group, outcome)
            self._record_written(journal, outcome)
        except OSError as exc:
            journal.rollback()
            outcome.written.clear()
            raise TransientError(f'staging into the store failed ({exc}); rolled back') from exc
        if self.stop_requested():
            journal.rollback()
            outcome.written.clear()
            raise TransientError('stop requested before the commit; rolled back')
        self._commit(journal)
        return placed

    def _commit(self, journal: 'SwapJournal') -> None:
        try:
            journal.commit()
        except OSError as exc:      # the journal stays in "committing": the next run rolls it forward
            raise TransientError(f'commit of the swap failed ({exc}); it is finished on the next run') from exc

    def _swap_update(self, item: Item, outcome: Outcome, group_dir: Path, store_dir: Path,
                     plain_static: Dict[str, Tuple[ObjInfo, Path]], store_group: Path,
                     data_changed: bool) -> List[str]:
        """S3 copy of the old version, stage everything, then release, commit, restore (in that order)."""
        cfg = self.config
        db_id = outcome.db_id
        version = outcome.previous_version or '0.0.0'
        try:
            links, remove_after = ingest_versions.archive_plan(store_dir, item.basename, version, data_changed)
        except FileExistsError as exc:
            raise Blocked('needs_decision', str(exc))
        if cfg.push_catalog:                        # S3 copy of the old version first: nothing changed yet
            try:
                current = [store_dir / f'{item.basename}{suffix}' for suffix in ('.md', MUS_SUFFIX, H5AD_SUFFIX)]
                outcome.archived_s3 = ingest_versions.archive_s3(
                    self.s3, cfg.bucket, cfg.catalog_prefix, item.rel_dir, item.basename, version,
                    [path for path in current if path.exists()], include_h5ad=data_changed)
            except Exception as exc:  # noqa: BLE001
                raise TransientError(f'S3 copy of version {version} failed ({exc}); nothing was changed') from exc
        store_duckdb = store_dir / f'{item.basename}.duckdb'
        released = False
        with writer_lock(store_duckdb):
            meta = self._journal_meta(item, outcome, version)
            meta['archived_local'] = [str(destination) for _, destination in links]
            journal = SwapJournal(cfg.journal_dir, slug(item.key), cfg.store_root / item.data_project, meta)
            try:
                for source, destination in links:
                    journal.link(source, destination)
                staged = self._staged_files(group_dir, item.basename)
                new_names = {path.name for path in staged}
                for path in staged:
                    journal.stage(path, store_dir / path.name)
                placed = self._stage_static(journal, plain_static, store_group, outcome)
                for path in remove_after:
                    journal.remove_after(path)
                if data_changed:
                    for stale in store_dir.glob(f'{item.basename}.layer.*.parquet'):
                        if stale.name not in new_names:
                            journal.remove_after(stale)
                self._record_written(journal, outcome)
            except OSError as exc:
                journal.rollback()
                outcome.written.clear()
                raise TransientError(f'staging the update failed ({exc}); rolled back, the published '
                                     f'version is untouched') from exc
            if self.stop_requested():
                journal.rollback()
                outcome.written.clear()
                raise TransientError('stop requested before the swap; rolled back')
            try:
                if data_changed and db_id:          # released only for the seconds of the renames
                    try:
                        self.api.release(db_id)
                    except TransientError:
                        journal.rollback()
                        outcome.written.clear()
                        raise
                    released = True
                    journal.data['meta']['released'] = db_id
                    journal._save()
                self._commit(journal)
                outcome.archived_local = list(meta['archived_local'])
            finally:
                if released:
                    try:
                        self.api.restore(db_id)     # the rescan right after retires any old pool instance
                    except TransientError as exc:
                        outcome.warnings.append(f'restore of {db_id} failed: {exc}')
        self._record_version(outcome, item)
        return placed

    def _record_version(self, outcome: Outcome, item: Item) -> None:
        if self.config.state_dir is None:
            return
        with open(self.config.state_dir / 'versions.jsonl', 'a') as handle:
            handle.write(json.dumps({'ts': _now_iso(), 'db_id': outcome.db_id, 'dataset': item.display,
                                     'archived_version': outcome.previous_version,
                                     'archived_sha256': outcome.previous_sha256, 'current_version': outcome.version,
                                     'current_sha256': outcome.sha256, 'local': outcome.archived_local,
                                     's3': outcome.archived_s3}) + '\n')

    def _promote_static(self, item: Item, plain_static: Dict[str, Tuple[ObjInfo, Path]], store_group: Path,
                        outcome: Outcome) -> List[str]:
        """New static files of an unchanged deposit, placed through their own small journal."""
        if not plain_static:
            return []
        journal = SwapJournal(self.config.journal_dir, slug(item.key) + '_static', store_group,
                              {'item': item.key, 'kind': 'static'})
        try:
            placed = self._stage_static(journal, plain_static, store_group, outcome)
        except OSError as exc:
            journal.rollback()
            raise TransientError(f'placing static files failed ({exc}); rolled back') from exc
        self._commit(journal)
        return placed

    def _publish_unchanged(self, item: Item, outcome: Outcome, store_dir: Path, sha: str,
                           plain_static: Dict[str, Tuple[ObjInfo, Path]], store_group: Path,
                           md_seed: Optional[Tuple[ObjInfo, Path]],
                           index_seed: Optional[Tuple[ObjInfo, Path]] = None) -> None:
        """Identical re-upload: nothing new to publish; place new static files, verify serving and S3."""
        outcome.state = 'live'
        outcome.detail = f'unchanged (already live as {outcome.version})'
        placed = self._promote_static(item, plain_static, store_group, outcome)
        outcome.consumed[item.h5ad.key] = item.h5ad.etag
        outcome.consumed[item.mus.key] = item.mus.etag
        for name in placed:
            obj = plain_static[name][0]
            outcome.consumed[obj.key] = obj.etag
            outcome.shared_keys.append(obj.key)
        if md_seed:
            outcome.consumed[md_seed[0].key] = md_seed[0].etag
        if index_seed:
            outcome.consumed[index_seed[0].key] = index_seed[0].etag
            if index_seed[0].name == 'index.md':
                outcome.shared_keys.append(index_seed[0].key)
        self._rescan_and_verify(outcome)
        if self.config.push_catalog:
            outcome.catalog_verified = self._verify_catalog(item, store_dir / f'{item.basename}{H5AD_SUFFIX}', sha,
                                                            outcome)

    def _resume_path(self, item: Item) -> Path:
        return resume_path(self.config.journal_dir, slug(item.key))

    def _load_resume(self, item: Item) -> Optional[Dict[str, Any]]:
        path = self._resume_path(item)
        try:
            return json.loads(path.read_text()) if path.exists() else None
        except ValueError:
            return None

    def has_pending(self, item: Item) -> bool:
        """A publication of this item whose push or post steps have not finished."""
        return self._resume_path(item).exists()

    def _update_resume(self, item: Item, **fields: Any) -> None:
        marker = self._load_resume(item)
        if marker is not None:
            marker.update(fields)
            write_resume(self.config.journal_dir, slug(item.key), marker)

    def _finish_pending(self, item: Item, marker: Dict[str, Any], store_dir: Path) -> Outcome:
        """Finish the publication a marker describes: verify serving, push if needed, the remaining post steps."""
        self.log(f'finishing {item.display} {marker.get("to")}: push and post steps were interrupted')
        pending = Outcome(state='live', kind=str(marker.get('kind') or 'new'), db_id=marker.get('db_id'),
                          version=str(marker.get('to')), previous_version=marker.get('from'),
                          sha256=marker.get('sha256'), previous_sha256=marker.get('previous_sha256'),
                          archived_local=list(marker.get('archived_local') or []),
                          written=dict(marker.get('written') or {}), resumed=True,
                          store_h5ad=str(store_dir / f'{item.basename}{H5AD_SUFFIX}'))
        work = self.config.work_root / f'{slug(item.key)}_finish'
        work.mkdir(parents=True, exist_ok=True)
        self._rescan_and_verify(pending)
        if self.config.push_catalog:
            if not marker.get('pushed'):
                self._push_catalog(item, pending, store_dir, include_h5ad=pending.kind != 'update_patch')
                self._update_resume(item, pushed=True)
            else:
                pending.catalog_verified = self._verify_catalog(item, Path(pending.store_h5ad), pending.sha256 or '',
                                                                pending)
        self._post(item, pending, store_dir, work)
        shutil.rmtree(work, ignore_errors=True)
        return pending

    def _rescan_and_verify(self, outcome: Outcome) -> None:
        """Rescan, then check that the server answers with the new sha256 and version."""
        try:
            result = self.api.rescan()
        except TransientError as exc:
            outcome.warnings.append(f'rescan failed ({exc}); the dataset is placed but not yet served')
            return
        outcome.served = self.verify_served(outcome.db_id, outcome.sha256, outcome.version, result, outcome.warnings)

    def verify_served(self, db_id: Optional[str], sha: Optional[str], version: Optional[str],
                      rescan_result: Dict[str, Any], warnings: List[str]) -> bool:
        if not db_id or db_id not in served_ids(rescan_result):
            warnings.append(f'{db_id} is not listed after the rescan')
            return False
        try:
            info = self.api.info(db_id)
        except TransientError as exc:
            warnings.append(str(exc))
            return False
        if str(info.get('sha256') or '') != str(sha or '') or str(info.get('version') or '') != str(version or ''):
            warnings.append(f'{db_id} is served as version {info.get("version")} / sha256 '
                            f'{str(info.get("sha256"))[:12]}, expected {version} / {str(sha)[:12]}')
            return False
        return True

    # S3 catalog -------------------------------------------------------------

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
        """HEAD the catalog h5ad: same size and verified content.

        The content counts as verified by our ``sha256`` metadata, or, for a
        legacy object without it, by an ETag recomputed from the store file (MD5,
        or a multipart ETag for the common part sizes). Only after such a match
        is the ``sha256`` metadata backfilled; a failing backfill (e.g. an object
        over 5 GB, where a self-copy needs a multipart copy) does not change the
        verdict. No match: not verified, nothing is deleted.
        """
        key = self._catalog_key(item, store_h5ad.name)
        try:
            head = self.s3.head_object(Bucket=self.config.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f'catalog copy {key} not found ({exc})')
            return False
        if int(head.get('ContentLength', -1)) != store_h5ad.stat().st_size:
            outcome.warnings.append(f'catalog copy {key} has another size than the store h5ad')
            return False
        remote_sha = (head.get('Metadata') or {}).get('sha256')
        if remote_sha:
            if remote_sha != sha:
                outcome.warnings.append(f'catalog copy {key} has sha256 {remote_sha[:12]}, expected {sha[:12]}')
                return False
            return True
        if sha256_file(store_h5ad) != sha:
            outcome.warnings.append(f'the store h5ad does not have the expected sha256 {sha[:12]}')
            return False
        etag = str(head.get('ETag', '')).strip('"')
        if not etag or not etag_matches_file(etag, store_h5ad):
            outcome.warnings.append(f'catalog copy {key} has no sha256 metadata and its ETag {etag} does not match '
                                    f'the store h5ad for any common part size: not verified')
            return False
        try:
            metadata = dict(head.get('Metadata') or {})
            metadata['sha256'] = sha
            self.s3.copy_object(Bucket=self.config.bucket, Key=key, CopySource={'Bucket': self.config.bucket, 'Key': key},
                                Metadata=metadata, MetadataDirective='REPLACE')
            outcome.warnings.append(f'catalog copy {key} verified by its ETag; sha256 metadata backfilled')
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f'catalog copy {key} verified by its ETag; sha256 metadata not backfilled ({exc})')
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
                if str(store_mus) in outcome.written:
                    outcome.written[str(store_mus)] = sha256_file(store_mus)
                    self._update_resume(item, written=dict(outcome.written))   # the re-stamp is the route's own
            mus_key = self._catalog_key(item, store_mus.name)
            self._upload(store_mus, mus_key)
            outcome.catalog_keys.append(mus_key)
            for path in [store_dir / f'{item.basename}.duckdb', *sorted(store_dir.glob(f'{item.basename}.layer.*.parquet')),
                         store_dir / f'{item.basename}.de.parquet']:
                if path.exists():
                    key = self._catalog_key(item, path.name)
                    self._upload(path, key)
                    outcome.catalog_keys.append(key)
        except Exception as exc:  # noqa: BLE001 - publication already happened; retried next poll
            outcome.warnings.append(f'push to the catalog failed: {exc}')
            return
        outcome.catalog_verified = self._verify_catalog(item, store_h5ad, outcome.sha256 or '', outcome)

    def repush(self, item: Item, kind: str, sha: str) -> Tuple[Optional[bool], List[str]]:
        """Retry the catalog push of a live item whose push or verification failed.

        Returns ``(None, [reason])`` when the store h5ad no longer has the
        sha256 that was published: nothing is pushed, a person decides.
        """
        store_dir = self.config.store_root / item.rel_dir
        store_h5ad = store_dir / f'{item.basename}{H5AD_SUFFIX}'
        if not store_h5ad.exists() or sha256_file(store_h5ad) != sha:
            return None, [f'{store_h5ad} is no longer the published file (sha256 {sha[:12]}): not pushed']
        outcome = Outcome(sha256=sha)
        self._push_catalog(item, outcome, store_dir, include_h5ad=kind != 'update_patch')
        return outcome.catalog_verified, outcome.warnings

    # after publication -----------------------------------------------------

    def _post(self, item: Item, outcome: Outcome, store_dir: Path, work: Path) -> None:
        """Post steps, DE import, derived push and store commit, each recorded in the resume marker when done.

        A stop request between or during steps raises :class:`TransientError`:
        the marker keeps the steps still to run and the next pass runs them. A
        step that fails on its own is a warning (``post_failed``) and counts as
        done. The marker is removed only when every step has run.
        """
        cfg = self.config
        store = cfg.store_root
        db_id = outcome.db_id
        steps: List[str] = ['manifest'] if outcome.kind == 'update_patch' else \
            [step for step in cfg.post_steps if step in POST_STEPS]
        if not db_id:
            outcome.warnings.append('no db id: post steps skipped')
            steps = []
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
        marker = self._load_resume(item) or {}
        done = set(marker.get('done_steps') or [])
        for step in steps + ['de', 'push_derived', 'git']:
            if step in done:
                continue
            if self.stop_requested():
                raise TransientError(f'stop requested before post step {step}; the next run continues from there')
            started = time.time()
            if step == 'manifest':
                self.log('post step manifest')
                self._manifest(outcome)
            elif step == 'de':
                self._ingest_de(item, outcome, store_dir, work)
            elif step == 'push_derived':
                if cfg.push_catalog:
                    for suffix in ('.scores.parquet', '.de.parquet'):
                        path = store_dir / f'{item.basename}{suffix}'
                        if path.exists():
                            try:
                                self._upload(path, self._catalog_key(item, path.name))
                            except Exception as exc:  # noqa: BLE001
                                outcome.warnings.append(f'push of {path.name} failed: {exc}')
            elif step == 'git':
                if cfg.store_git_commit:
                    self._git_commit(item, outcome, store_dir)
            else:
                self.log(f'post step {step}')
                code, tail = self.runner.run(f'post:{step}', commands[step], cwd=work, timeout=cfg.post_step_timeout)
                if code != 0 and self.stop_requested():
                    raise TransientError(f'post step {step} stopped (exit {code}); it runs again on the next start')
                if code != 0:
                    outcome.warnings.append(f'post step {step} failed (exit {code}): '
                                            f'{tail.splitlines()[-1] if tail else ""}')
                    outcome.post_failed.append(step)
                self._timed(outcome, f'post:{step}', started)
            done.add(step)
            self._update_resume(item, done_steps=sorted(done), post_failed=sorted(
                set(marker.get('post_failed') or []) | set(outcome.post_failed)))
        outcome.post_failed = sorted(set(marker.get('post_failed') or []) | set(outcome.post_failed))
        try:
            self.api.rescan()
        except TransientError as exc:
            outcome.warnings.append(f'final rescan failed: {exc}')
        self._resume_path(item).unlink(missing_ok=True)

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
        sources_root = self.config.store_root / item.data_project / 'de_sources'
        for obj in data_files:
            mus_obj = by_key.get(obj.key + '.mus')
            name = obj.name
            if mus_obj is None or not name.endswith(DE_DATA_EXTS):
                continue
            parent = obj.key.rsplit('/', 2)[-2]
            if not valid_segment(parent) or not valid_segment(name):
                continue
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
            source_dir = sources_root / parent
            ensure_inside(source_dir / name, sources_root)
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
        """Commit the catalogue files the route wrote, and only when they are exactly what it wrote.

        Skipped (with a warning) when a file changed after the route wrote it, or
        when the curator has staged changes of those paths: the index is never
        touched then. A failed commit resets only these paths, which had no
        staged changes before, so the index is back where it was.
        """
        store = self.config.store_root
        if not outcome.written:
            return                                   # nothing written by the route (an unchanged deposit)
        if not (store / '.git').exists():
            outcome.warnings.append(f'{store} is not a git repository: no commit')
            return
        changed_since = [path for path, sha in outcome.written.items()
                         if not Path(path).exists() or sha256_file(Path(path)) != sha]
        if changed_since:
            outcome.warnings.append('not committed: changed after the route wrote them: '
                                    + ', '.join(Path(path).name for path in changed_since))
            return
        rel = [str(Path(path).relative_to(store)) for path in sorted(outcome.written)]
        code, staged = self.runner.run('git diff --cached', ['git', '-C', store, 'diff', '--cached', '--name-only',
                                                             '--', *rel])
        if code != 0 or staged.strip():
            outcome.warnings.append(f'not committed: {staged.strip() or "git diff failed"} already staged by '
                                    f'someone else; commit by hand')
            return
        name, _, email = self.config.git_identity.partition('<')
        committer = ['-c', f'user.name={name.strip()}', '-c', f'user.email={email.rstrip(">").strip()}']
        sidecar_path = store_dir / f'{item.basename}{MUS_SUFFIX}'
        depositor = md_fill.depositor_of(parse_sidecar_text(sidecar_path.read_text())) if sidecar_path.exists() else None
        message = (f'ingest-watch: {item.display} {outcome.version} ({outcome.kind})\n\n'
                   f'Deposit {item.h5ad.key} (ETag {item.h5ad.etag}), sha256 {outcome.sha256}.\n')
        code, tail = self.runner.run('git add', ['git', '-C', store, 'add', '--', *rel])
        if code == 0:
            cmd = ['git', '-C', store, *committer, 'commit', '--only', '-m', message,
                   '--author', depositor or self.config.git_identity, '--', *rel]
            code, tail = self.runner.run('git commit', cmd)
        if code != 0:
            outcome.warnings.append(f'git commit failed: {tail}')
            self.runner.run('git reset', ['git', '-C', store, 'reset', '-q', '--', *rel])
            return
        code, head = self.runner.run('git rev-parse', ['git', '-C', store, 'rev-parse', 'HEAD'])
        outcome.git_commit = head.strip().splitlines()[-1] if code == 0 and head.strip() else None
        if self.config.store_git_push:
            code, ahead = self.runner.run('git rev-list', ['git', '-C', store, 'rev-list', '--count', '@{u}..HEAD'])
            if code != 0 or ahead.strip().splitlines()[-1:] != ['1']:
                outcome.warnings.append(f'store_git_push: not pushed, the store branch has other unpushed commits '
                                        f'or no upstream ({ahead.strip()[:80]})')
                return
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
