"""Per-dataset freshness fingerprints for the precomputed fleet indexes.

``ant-gene-stats-precompute`` and ``ant-gene-variance-precompute`` stamp
every output row with a ``src_fingerprint`` column: a JSON list of
``[relpath, size_bytes, mtime_ns]`` for the source files the dataset's
numbers were computed from (paths relative to the h5adstore root, i.e.
the index parquet's parent directory). One distinct value per dataset,
so parquet dictionary encoding keeps it negligible on disk; the loaders
fold it into a per-dataset dict and drop the column.

A dataset is fresh when every recorded file still exists, has the same
size, and either has the same mtime or is not newer than the index file
itself. The second clause is what makes the check portable across hosts:
``ant-sync pull`` sets local mtimes to the S3 upload time, so on a pulled
host the recorded mtimes never match exactly, but sources pulled before
the index are older than it. A source re-ingested and re-uploaded after
the index was built is newer than the index and reads stale.

Stale datasets are reported as a lookup miss so ant-serve falls through
to its SQLite / live compute path. Index files written before the column
existed carry no fingerprints; every dataset in them is treated as fresh.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

FINGERPRINT_COLUMN = 'src_fingerprint'

#: Sentinel stamped on datasets whose cache the precompute itself considers
#: out of date (e.g. skipped by ``--only``). Never matches a real file.
STALE_FINGERPRINT = json.dumps([['<stale>', -1, -1]])

#: How long a per-dataset verdict is reused before re-stat'ing the files.
DEFAULT_TTL_SECONDS = 30.0

FileRecord = Tuple[Path, int, int]   # (absolute path, size_bytes, mtime_ns)


def build_fingerprint(root: Path, paths: Iterable[Path]) -> str:
    """Snapshot ``paths`` into the JSON string stored in ``src_fingerprint``.

    Args:
        root: h5adstore root; recorded paths are made relative to it.
        paths: Source files the dataset's rows are computed from.

    Returns:
        JSON list of ``[relpath, size_bytes, mtime_ns]``. A missing file
        yields :data:`STALE_FINGERPRINT` (nothing sensible to record).
    """
    root_resolved = root.resolve()
    records: list = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return STALE_FINGERPRINT
        resolved = path.resolve()
        try:
            rel = str(resolved.relative_to(root_resolved))
        except ValueError:
            rel = str(resolved)
        records.append([rel, int(stat.st_size), int(stat.st_mtime_ns)])
    return json.dumps(records)


def _parse_fingerprint(raw: object, root: Path) -> Optional[List[FileRecord]]:
    """Decode one ``src_fingerprint`` value; ``None`` means "unknown"."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        items = json.loads(str(raw))
    except (TypeError, ValueError):
        return [(Path('<unparseable>'), -1, -1)]
    records: List[FileRecord] = []
    for rel, size, mtime_ns in items:
        path = Path(rel)
        if not path.is_absolute():
            path = root / path
        records.append((path, int(size), int(mtime_ns)))
    return records


class FreshnessChecker:
    """Decides whether one dataset's precomputed rows still match its sources.

    Immutable after construction apart from the verdict cache, so a loader
    can build a new checker off to the side and swap it in atomically.
    """

    def __init__(
        self,
        fingerprints: Dict[str, Optional[List[FileRecord]]],
        index_mtime_ns: int,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Bind per-dataset fingerprints and the index file's own mtime.

        Args:
            fingerprints: ``dataset -> records`` (``None`` = unknown/fresh).
            index_mtime_ns: mtime of the index parquet at load time.
            ttl_seconds: Verdict reuse window; defaults to the module-level
                :data:`DEFAULT_TTL_SECONDS` read at construction time.
        """
        self._fingerprints = fingerprints
        self._index_mtime_ns = index_mtime_ns
        self._ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._verdicts: Dict[str, Tuple[float, bool]] = {}

    @property
    def has_fingerprints(self) -> bool:
        return any(v is not None for v in self._fingerprints.values())

    def is_fresh(self, dataset: str) -> bool:
        """Whether ``dataset``'s rows can be served (cached for ``ttl``)."""
        now = time.monotonic()
        cached = self._verdicts.get(dataset)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        verdict = self._check(dataset)
        self._verdicts[dataset] = (now, verdict)
        return verdict

    def _check(self, dataset: str) -> bool:
        records = self._fingerprints.get(dataset)
        if records is None:
            return True
        for path, size, mtime_ns in records:
            try:
                stat = path.stat()
            except OSError:
                return False
            if stat.st_size != size:
                return False
            if stat.st_mtime_ns != mtime_ns and stat.st_mtime_ns > self._index_mtime_ns:
                return False
        return True

    def stale_datasets(self) -> List[str]:
        """All datasets currently judged stale (bypasses the verdict cache)."""
        return sorted(ds for ds in self._fingerprints if not self._check(ds))


def extract_fingerprints(
    df: pd.DataFrame, root: Path, index_path: Path, label: str,
) -> Tuple[pd.DataFrame, FreshnessChecker]:
    """Split the fingerprint column off a freshly read index frame.

    Args:
        df: Frame as read from the index parquet (``dataset`` already str).
        root: Directory relative paths resolve against.
        index_path: The parquet file (its mtime feeds the checker).
        label: Name used in the one-time "no fingerprints" log line.

    Returns:
        ``(df_without_column, checker)``.
    """
    try:
        index_mtime_ns = index_path.stat().st_mtime_ns
    except OSError:
        index_mtime_ns = 0
    if FINGERPRINT_COLUMN not in df.columns:
        log.warning(
            '%s: %s has no %s column; freshness gating disabled until the '
            'precompute is re-run', label, index_path, FINGERPRINT_COLUMN,
        )
        return df, FreshnessChecker({}, index_mtime_ns)
    per_dataset = (
        df[['dataset', FINGERPRINT_COLUMN]]
        .drop_duplicates('dataset')
        .itertuples(index=False, name=None)
    )
    fingerprints = {
        str(dataset): _parse_fingerprint(raw, root)
        for dataset, raw in per_dataset
    }
    return df.drop(columns=[FINGERPRINT_COLUMN]), FreshnessChecker(
        fingerprints, index_mtime_ns,
    )
