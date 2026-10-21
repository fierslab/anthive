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
    * A write is atomic: the value is pickled to a unique temporary file in the same folder and renamed over
      the target, under an exclusive ``flock`` per entry name, so several workers or processes can share the
      folder. A reader never sees a partial file; an unreadable file is a miss.
    * The callers re-check their inputs after a build and skip the write when a file changed meanwhile.

Values are pickles. The folder must only be writable by the user that runs ant-serve (the container's own
scratch volume; never a shared, world-writable folder): unpickling runs code chosen by whoever wrote the file.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import pickle
import platform
import re
import tempfile
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

#: Bump when the layout of a stored payload changes.
CACHE_FORMAT = 1
#: Temporary files older than this (seconds) are left-overs of a crashed writer and are removed.
STALE_TEMPORARY_SECONDS = 3600

LIB_DIR = Path(__file__).resolve().parent

MISSING = object()


@lru_cache(maxsize=1)
def code_version() -> str:
    """sha256 over the source of ``lib/`` (every ``*.py`` and ``*.yaml``) and the versions of the libraries
    whose objects are pickled. Computed once per process."""
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


class DiskCache:
    """A folder of pickled values, each stored with the key it was built for.

    Args:
        directory: The cache folder (created on the first write).
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.hits = 0
        self.misses = 0
        self.writes = 0
        #: The latest ``(name, 'hit' | 'miss' | 'write')`` events, for logs and tests.
        self.events: deque = deque(maxlen=2000)

    def _path(self, name: str, key: str) -> Path:
        return self.directory / f'{_safe_name(name)}~{key[:40]}.pkl'

    def load(self, name: str, key: str) -> Any:
        """The value stored for ``(name, key)``, or ``MISSING``."""
        path = self._path(name, key)
        try:
            with open(path, 'rb') as handle:
                payload = pickle.load(handle)
        except FileNotFoundError:
            return self._miss(name)
        except Exception as exc:  # noqa: BLE001 - truncated, foreign or incompatible file: rebuild
            log.warning('warm cache: cannot read %s (%s); rebuilding', path.name, exc)
            return self._miss(name)
        if (not isinstance(payload, dict) or payload.get('format') != CACHE_FORMAT
                or payload.get('key') != key or payload.get('name') != name):
            return self._miss(name)
        self.hits += 1
        self.events.append((name, 'hit'))
        return payload['value']

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

        Returns False (and logs) when the folder is not writable; the caller keeps its value in memory.
        """
        path = self._path(name, key)
        temporary: Optional[str] = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._locked(name):
                handle, temporary = tempfile.mkstemp(dir=self.directory, prefix='.tmp-', suffix='.pkl')
                with os.fdopen(handle, 'wb') as stream:
                    pickle.dump({'format': CACHE_FORMAT, 'name': name, 'key': key, 'value': value}, stream,
                                protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(temporary, path)
                temporary = None
                self._prune(name, path, keep)
            self.writes += 1
            self.events.append((name, 'write'))
            return True
        except Exception as exc:  # noqa: BLE001 - a full or read-only disk must not break the server
            log.warning('warm cache: cannot write %s: %s', path, exc)
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def _prune(self, name: str, current: Path, keep: int) -> None:
        """Remove older entries of ``name`` beyond the ``keep`` newest, and stale temporary files."""
        entries = []
        for path in self.directory.glob(f'{_safe_name(name)}~*.pkl'):
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
        for path in self.directory.glob('.tmp-*.pkl'):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    def stats(self) -> dict:
        return {'directory': str(self.directory), 'hits': self.hits, 'misses': self.misses, 'writes': self.writes}
