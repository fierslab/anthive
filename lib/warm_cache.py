"""Persistent cache of ant-serve's expensive derived tables (facet warm-up).

ant-serve's start-up warm-up builds the cross-dataset facet tables (gene and module tables, replication
families, per-dataset summaries) from the ``_facet_tests`` result files. Building them takes a minute or more
and the result only changes when a result file, the cohort table, a dataset attribute or the code changes, so
the values are kept on disk under ``<tmp_dir>/facet_cache`` (``config.get_tmp_dir``) and reused by the next
start.

Correctness rules:
    * Every entry is stored with the full key it was built for (a sha256 over everything the value depends
      on) and the cache format; a load whose stored key differs from the asked key is a miss. The file name
      carries a prefix of the key only to find it quickly.
    * The key always contains ``code_version()``: a hash of every ``.py`` and ``.yaml`` file of ``lib/`` plus
      the Python, numpy, pandas and pyarrow versions, so a new release or a library upgrade never reuses a
      value computed by other code.
    * A write is atomic: the value is written to a unique temporary file in the same folder and renamed over
      the target, under an exclusive ``flock`` per entry name, so several workers or processes can share the
      folder. A reader never sees a partial file; an unreadable file is a miss.
    * The callers re-check their inputs after a build and skip the write when a file changed meanwhile.

File format (no pickle: loading a file never runs code, whoever wrote it; the scratch volume is shared by
the api and ingest containers). One file per entry::

    MAGIC (8 bytes) | header length (8 bytes, big endian) | header (UTF-8 JSON) | parquet blob | parquet blob ...

The header holds ``format``, ``name``, ``key`` (checked before anything else is read), the value as tagged
JSON (``encode``) and the byte ranges of the blobs. Every DataFrame of the value is one parquet blob (pyarrow,
pandas metadata kept: dtypes, categoricals, index); an object column whose values are not all strings or None
is stored as one tagged JSON string per row and decoded on load. Types outside JSON, DataFrames, tuples,
sets and numpy scalars are refused at write time (the entry is then not cached). pyarrow before 14.0.1 is
refused (CVE-2023-47248: code execution through a crafted extension type).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import io
import os
import platform
import struct
import re
import tempfile
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Bump when the layout of a stored payload changes.
CACHE_FORMAT = 2
MAGIC = b'ANTWC02\n'
SUFFIX = '.awc'
#: Parquet compression of the blobs (lz4: fast to write and read).
COMPRESSION = 'lz4'
#: A header (tagged JSON of everything but the DataFrames) larger than this is refused on load.
MAX_HEADER_BYTES = 512 * 1024 * 1024
#: Temporary files older than this (seconds) are left-overs of a crashed writer and are removed.
STALE_TEMPORARY_SECONDS = 3600

LIB_DIR = Path(__file__).resolve().parent

MISSING = object()


@lru_cache(maxsize=1)
def code_version() -> str:
    """sha256 over the source of ``lib/`` (every ``*.py`` and ``*.yaml``) and the versions of the libraries
    that write and read the stored values. Computed once per process."""
    hasher = hashlib.sha256()
    for path in sorted(list(LIB_DIR.glob('*.py')) + list(LIB_DIR.glob('*.yaml'))):
        hasher.update(path.name.encode())
        hasher.update(b'\0')
        hasher.update(path.read_bytes())
        hasher.update(b'\0')
    versions = [platform.python_version(), str(CACHE_FORMAT)]
    for module_name in ('numpy', 'pandas', 'pyarrow'):
        try:
            module = __import__(module_name)
            versions.append(f'{module_name}={module.__version__}')
        except ImportError:
            versions.append(f'{module_name}=absent')
    hasher.update('|'.join(versions).encode())
    return hasher.hexdigest()


def _jsonable(value: Any) -> Any:
    """Canonical JSON form of the values found in keys: sets sorted, numpy scalars unwrapped, the rest repr."""
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    item = getattr(value, 'item', None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return repr(value)


def _normalise(value: Any) -> Any:
    """``value`` with sets sorted and NaN spelled one way, recursively (JSON would accept it otherwise)."""
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalise(item) for item in value), key=repr)
    if isinstance(value, float) and math.isnan(value):
        return 'NaN'
    return value


def digest(*parts: Any) -> str:
    """Stable sha256 of ``parts`` (canonical JSON: sorted keys, sets sorted). The same input gives the same
    digest in every process: never use ``hash()`` (randomised per process for strings) in a disk key."""
    text = json.dumps(_normalise(list(parts)), sort_keys=True, default=_jsonable, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _safe_name(name: str) -> str:
    """File-system safe form of an entry name; a short hash keeps distinct names distinct."""
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('._')[:80] or 'entry'
    return f'{cleaned}-{hashlib.sha1(name.encode()).hexdigest()[:8]}'


# ── value encoding (tagged JSON + parquet, never pickle) ─────────────────────

_TAG_KEYS = ('__df__', '__tuple__', '__set__', '__dict__', '__float__')


def _plain(value: Any) -> Any:
    """A JSON-encodable form of one value; DataFrames go to ``frames`` (see ``encode``)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return {'__float__': repr(value)}
        return value
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        raise TypeError('container reached _plain')
    item = getattr(value, 'item', None)       # numpy scalar
    if callable(item) and type(value).__module__ == 'numpy':
        return _plain(item())
    raise TypeError(f'cannot store a {type(value).__name__} in the warm cache')


def _encode(value: Any, frames: List[Any]) -> Any:
    import pandas as pd  # noqa: PLC0415
    if isinstance(value, pd.DataFrame):
        frames.append(value)
        return {'__df__': len(frames) - 1}
    if isinstance(value, list):
        return [_encode(item, frames) for item in value]
    if isinstance(value, tuple):
        return {'__tuple__': [_encode(item, frames) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {'__set__': sorted((_encode(item, frames) for item in value), key=repr)}
    if isinstance(value, dict):
        if all(isinstance(key, str) and key not in _TAG_KEYS for key in value):
            return {key: _encode(item, frames) for key, item in value.items()}
        return {'__dict__': [[_encode(key, frames), _encode(item, frames)] for key, item in value.items()]}
    return _plain(value)


def _decode(value: Any, frames: List[Any]) -> Any:
    if isinstance(value, list):
        return [_decode(item, frames) for item in value]
    if isinstance(value, dict):
        if len(value) == 1:
            (tag, inner), = value.items()
            if tag == '__df__':
                return frames[inner]
            if tag == '__tuple__':
                return tuple(_decode(item, frames) for item in inner)
            if tag == '__set__':
                return frozenset(_decode(item, frames) for item in inner)
            if tag == '__dict__':
                return {_decode(key, frames): _decode(item, frames) for key, item in inner}
            if tag == '__float__':
                return float(inner)
        return {key: _decode(item, frames) for key, item in value.items()}
    return value


def _cell_json(value: Any) -> str:
    if value is None or (type(value) is list and all(type(item) is str for item in value)):
        return json.dumps(value, separators=(',', ':'))           # the common cells: no tags needed
    return json.dumps(_encode(value, _NO_FRAMES), separators=(',', ':'), allow_nan=False)


class _NoFrames(list):
    def append(self, item: Any) -> None:  # noqa: D102 - a DataFrame inside a cell is not supported
        raise TypeError('a DataFrame inside a DataFrame cell cannot be stored')


_NO_FRAMES = _NoFrames()


def _frame_blob(frame: Any) -> Tuple[bytes, List[str]]:
    """Parquet bytes of one DataFrame and the names of the columns stored as tagged JSON strings."""
    import pandas as pd  # noqa: PLC0415
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415
    if not all(isinstance(column, str) for column in frame.columns):
        raise TypeError('only string column names can be stored')
    json_columns: List[str] = []
    replaced: Dict[str, Any] = {}
    for column in frame.columns:
        series = frame[column]
        if series.dtype != object:
            continue
        if all(cell is None or isinstance(cell, str) for cell in series):
            continue
        replaced[column] = pd.array([_cell_json(cell) for cell in series], dtype=object)
        json_columns.append(column)
    if replaced:
        frame = frame.assign(**replaced)
    table = pa.Table.from_pandas(frame)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression=COMPRESSION)
    return sink.getvalue(), json_columns


def _cell_hook(value: Dict[str, Any]) -> Any:
    """``json.loads`` object hook that turns the tags of ``_encode`` back into tuples, frozensets, dicts with
    non-string keys and non-finite floats (inner values are already decoded)."""
    if len(value) == 1:
        (tag, inner), = value.items()
        if tag == '__tuple__':
            return tuple(inner)
        if tag == '__set__':
            return frozenset(inner)
        if tag == '__dict__':
            return {key: item for key, item in inner}
        if tag == '__float__':
            return float(inner)
        if tag == '__df__':
            raise ValueError('a DataFrame tag inside a cell')
    return value


def _frame_from_blob(buffer: Any, json_columns: List[str]) -> Any:
    import pandas as pd  # noqa: PLC0415
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415
    table = pq.read_table(pa.BufferReader(buffer))
    decoded = {}
    for column in json_columns:
        # one parse per column (a JSON array of the cells), not one per cell: 600k cells take 0.3 s, not 3 s
        cells = table.column(column).to_pylist()
        decoded[column] = json.loads('[' + ','.join(cells) + ']', object_hook=_cell_hook)
    frame = table.to_pandas()
    for column, values in decoded.items():
        # dtype object keeps None apart from NaN and lists intact (pandas 3 would infer a string column)
        frame[column] = pd.Series(values, index=frame.index, dtype=object)
    return frame


def _pyarrow_is_safe() -> bool:
    try:
        import pyarrow  # noqa: PLC0415
    except ImportError:
        return False
    parts = []
    for piece in pyarrow.__version__.split('.')[:3]:
        digits = re.match(r'\d+', piece)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts))) >= (14, 0, 1)


class DiskCache:
    """A folder of stored values (tagged JSON + parquet, see the module doc), each with the key it was built for.

    Args:
        directory: The cache folder (created on the first write).
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.enabled = _pyarrow_is_safe()
        if not self.enabled:
            log.warning('warm cache: pyarrow older than 14.0.1 (or missing); the disk cache is off')
        #: The latest ``(name, 'hit' | 'miss' | 'write')`` events, for logs and tests.
        self.events: deque = deque(maxlen=2000)

    def _path(self, name: str, key: str) -> Path:
        return self.directory / f'{_safe_name(name)}~{key[:40]}{SUFFIX}'

    def load(self, name: str, key: str) -> Any:
        """The value stored for ``(name, key)``, or ``MISSING``."""
        if not self.enabled:
            return self._miss(name)
        path = self._path(name, key)
        try:
            import pyarrow as pa  # noqa: PLC0415
            with open(path, 'rb') as handle:
                if handle.read(len(MAGIC)) != MAGIC:
                    return self._miss(name)
                (length,) = struct.unpack('>Q', handle.read(8))
                if length > MAX_HEADER_BYTES:
                    raise ValueError(f'header of {length} bytes')
                header = json.loads(handle.read(length).decode('utf-8'))
            if (not isinstance(header, dict) or header.get('format') != CACHE_FORMAT
                    or header.get('key') != key or header.get('name') != name):
                return self._miss(name)
            source = pa.memory_map(str(path), 'r')
            try:
                frames = []
                for offset, size, json_columns in header['frames']:
                    source.seek(offset)
                    frames.append(_frame_from_blob(source.read_buffer(size), json_columns))
            finally:
                source.close()
            value = _decode(header['value'], frames)
        except FileNotFoundError:
            return self._miss(name)
        except Exception as exc:  # noqa: BLE001 - truncated, foreign or incompatible file: rebuild
            log.warning('warm cache: cannot read %s (%s); rebuilding', path.name, exc)
            return self._miss(name)
        self.hits += 1
        self.events.append((name, 'hit'))
        return value

    def _miss(self, name: str) -> Any:
        self.misses += 1
        self.events.append((name, 'miss'))
        return MISSING

    @contextlib.contextmanager
    def _locked(self, name: str) -> Iterator[None]:
        lock_path = self.directory / f'.{_safe_name(name)}.lock'
        with open(lock_path, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, name: str, key: str, value: Any, keep: int = 2) -> bool:
        """Store ``value`` for ``(name, key)`` atomically and keep only the ``keep`` newest entries of ``name``.

        Returns False (and logs) when the value holds a type the format cannot store or the folder is not
        writable; the caller keeps its value in memory.
        """
        if not self.enabled:
            return False
        path = self._path(name, key)
        temporary: Optional[str] = None
        try:
            frames: List[Any] = []
            encoded = _encode(value, frames)          # refuses unsupported types before any file is touched
            blobs = [_frame_blob(frame) for frame in frames]
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._locked(name):
                handle, temporary = tempfile.mkstemp(dir=self.directory, prefix='.tmp-', suffix=SUFFIX)
                with os.fdopen(handle, 'wb') as stream:
                    header = _header_bytes(name, key, encoded, blobs)
                    stream.write(MAGIC)
                    stream.write(struct.pack('>Q', len(header)))
                    stream.write(header)
                    for blob, _columns in blobs:
                        stream.write(blob)
                os.replace(temporary, path)
                temporary = None
                self._prune(name, path, keep)
            self.writes += 1
            self.events.append((name, 'write'))
            return True
        except Exception as exc:  # noqa: BLE001 - unsupported value, full or read-only disk: never fatal
            log.warning('warm cache: cannot write %s: %s', path, exc)
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def _prune(self, name: str, current: Path, keep: int) -> None:
        """Remove older entries of ``name`` beyond the ``keep`` newest, and stale temporary files."""
        entries = []
        for path in self.directory.glob(f'{_safe_name(name)}~*{SUFFIX}'):
            with contextlib.suppress(OSError):
                entries.append((path.stat().st_mtime_ns, path))
        entries.sort(reverse=True)
        kept = 1
        for _mtime, path in entries:
            if path == current:
                continue
            if kept < keep:
                kept += 1
                continue
            with contextlib.suppress(OSError):
                path.unlink()
        cutoff = time.time() - STALE_TEMPORARY_SECONDS
        for path in self.directory.glob('.tmp-*'):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    def stats(self) -> dict:
        return {'directory': str(self.directory), 'hits': self.hits, 'misses': self.misses, 'writes': self.writes}


def _header_bytes(name: str, key: str, encoded: Any, blobs: List[Tuple[bytes, List[str]]]) -> bytes:
    """The JSON header with the absolute byte offset of every blob. The offsets depend on the header length,
    which depends on the offsets' digits: iterate until the length is stable (two rounds in practice)."""
    length = 0
    for _round in range(10):
        offset = len(MAGIC) + 8 + length
        frames = []
        for blob, json_columns in blobs:
            frames.append([offset, len(blob), json_columns])
            offset += len(blob)
        header = json.dumps({'format': CACHE_FORMAT, 'name': name, 'key': key, 'value': encoded, 'frames': frames},
                            separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(header) == length:
            return header
        length = len(header)
    raise RuntimeError('header length did not settle')
