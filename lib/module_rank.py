"""Rank of a module score among all modules scored in the same dataset.

A mean module score is not comparable between datasets (each dataset has its own
expression scale, depth and background). It is comparable between the modules of
one dataset, so the question this module answers is: where does the mean score of
this gene set fall among ALL gene sets scored on the same cells of the same dataset?

Method
    * Input is the per-cell score sidecar (``<stem>.scores.parquet``: ``cell_id,
      source, catalog, module, score``). A module only has rows for the cells of its
      cell type scope, a module of scope ``all`` has rows for every cell.
    * Cells are grouped into strata (the cell type group of ``ant_cell_type``, or one
      stratum ``''`` for a dataset without cell types). Per source, module and stratum
      the number of scored cells and the mean score are stored (``stratum_means``).
    * In a stratum a module is compared with every OTHER module of the same source that
      has at least ``MIN_CELLS`` cells there, whatever its catalog. The rank is the
      fraction of those peers with a lower mean (ties count half), from 0 (lowest) to
      1 (highest), and only given when there are at least ``MIN_PEERS`` peers.
    * The dataset rank is the mean of the stratum ranks weighted by the cells of the
      module in each stratum (a module of one cell type has one stratum).

Source ``geneset`` (scanpy ``score_genes``) subtracts a matched control gene set, so its
means of different modules are comparable within a dataset. ``ucell`` is a rank
statistic that depends on the number of genes of the set; it is returned as a
secondary view only. Ranks are never compared between datasets, only averaged for display.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PRIMARY_SOURCE = 'geneset'
SECONDARY_SOURCE = 'ucell'
MIN_CELLS = 20
MIN_PEERS = 10
UNLABELLED = 'unlabelled'
MEANS_COLUMNS = ['source', 'catalog', 'module', 'stratum', 'n', 'mean', 'std']

_STRATUM_SQL = (
    "SELECT s.source, s.catalog, s.module, {stratum} AS stratum, "
    "COUNT(*) AS n, AVG(s.score) AS mean, STDDEV_SAMP(s.score) AS std "
    "FROM read_parquet(?) s {join} WHERE s.score IS NOT NULL GROUP BY 1, 2, 3, 4"
)
#: Bump when ``cell_strata`` or ``stratum_means`` compute something else: the cached tables are then rebuilt.
#: (The SQL above, the columns and the cell type group table are part of the cache key on their own.)
TABLE_FORMAT = 2


@lru_cache(maxsize=1)
def table_version() -> str:
    """Short hash of what a cached ``stratum_means`` table depends on besides its dataset: ``TABLE_FORMAT``, the
    SQL, the columns and the cell type group table (``celltype_groups.py`` and ``.yaml``, which define the strata)."""
    hasher = hashlib.sha256(f'{TABLE_FORMAT}|{_STRATUM_SQL}|{MEANS_COLUMNS}|{UNLABELLED}'.encode())
    lib_dir = Path(__file__).resolve().parent
    for name in ('celltype_groups.py', 'celltype_groups.yaml'):
        hasher.update(name.encode())
        hasher.update((lib_dir / name).read_bytes())
    return hasher.hexdigest()[:16]


def table_fingerprint(sidecar_stat: os.stat_result, duckdb_stat: os.stat_result) -> str:
    """Cache key of a dataset's table: ``table_version()``, the sidecar's mtime and size and the duckdb's mtime
    (cell types). No path, pid or start time: the same files give the same key in every process and container."""
    return f'{table_version()}-{sidecar_stat.st_mtime_ns}-{sidecar_stat.st_size}-{duckdb_stat.st_mtime_ns}'


def cell_strata(conn: Any) -> Optional[pd.DataFrame]:
    """Per-cell stratum (cell type group) of a dataset, read through its ``obscat`` view.

    Args:
        conn: DuckDB connection on the dataset (``__cells`` and ``obscat`` visible).

    Returns:
        Frame ``cell_id, stratum`` or None when the dataset has no ``ant_cell_type``.
        A cell without a label gets ``UNLABELLED``.
    """
    import celltype_groups  # noqa: PLC0415  (lib shim, imported lazily to keep this module light)

    columns = {row[0] for row in conn.execute('DESCRIBE obscat').fetchall()}
    if 'ant_cell_type' not in columns:
        return None
    frame = conn.execute(
        'SELECT c.cell_id AS cell_id, o.ant_cell_type AS label FROM __cells c '
        'JOIN obscat o ON o.cell_name = c.cell_name').df()
    group_of = {label: celltype_groups.group_of(label) for label in frame['label'].dropna().unique()}
    frame['stratum'] = [UNLABELLED if pd.isna(label) else group_of[label] for label in frame['label']]
    return frame[['cell_id', 'stratum']]


def stratum_means(conn: Any, sidecar: Path, strata: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Cells and mean score per (source, catalog, module, stratum) of a score sidecar.

    Args:
        conn: DuckDB connection allowed to read the sidecar (and to ``register`` a frame).
        sidecar: path of ``<stem>.scores.parquet``.
        strata: result of ``cell_strata`` (None: one stratum ``''`` for all cells).

    Returns:
        Frame with ``MEANS_COLUMNS``.
    """
    if strata is None:
        sql = _STRATUM_SQL.format(stratum="''", join='')
        return conn.execute(sql, [str(sidecar)]).df()[MEANS_COLUMNS]
    conn.register('module_rank_strata', strata)
    try:
        sql = _STRATUM_SQL.format(stratum='t.stratum', join='JOIN module_rank_strata t ON t.cell_id = s.cell_id')
        return conn.execute(sql, [str(sidecar)]).df()[MEANS_COLUMNS]
    finally:
        conn.unregister('module_rank_strata')


def _empty(reason: str) -> Dict[str, Any]:
    return {'available': False, 'reason': reason, 'rank': None, 'n_modules_compared': 0,
            'n_cells': 0, 'mean': None, 'std': None, 'cell_types': []}


def _finite(value: Any) -> Optional[float]:
    """Float of a possibly missing or NaN number, else None."""
    return None if value is None or pd.isna(value) else float(value)


def _pooled_std(mine: pd.DataFrame, overall_mean: float) -> Optional[float]:
    """Standard deviation over all scored cells of a module from its per-stratum n, mean and std."""
    total = int(mine['n'].sum())
    if total < 2:
        return None
    spread = 0.0
    for row in mine.itertuples():
        within = 0.0 if pd.isna(row.std) else float(row.std) ** 2 * (row.n - 1)
        spread += within + row.n * (float(row.mean) - overall_mean) ** 2
    return float(np.sqrt(spread / (total - 1)))


def module_rank(means: pd.DataFrame, source: str, catalog: str, module: str,
                min_cells: int = MIN_CELLS, min_peers: int = MIN_PEERS) -> Dict[str, Any]:
    """Rank of one module's mean score among the other modules of the same source.

    Args:
        means: ``stratum_means`` of the dataset.
        source: score source (``geneset`` or ``ucell``).
        catalog: ``Study/Experiment`` of the module.
        module: module name.
        min_cells: a stratum needs this many scored cells of the module and of each peer.
        min_peers: a stratum needs this many peer modules to give a rank.

    Returns:
        ``{available, reason, rank, n_modules_compared, n_cells, mean, std, cell_types}``;
        ``rank`` runs from 0 (lowest mean) to 1 (highest), ``n_modules_compared`` is the number of
        peers in the stratum with most cells of the module, ``mean`` the mean over all its
        scored cells, ``cell_types`` one ``{cell_type, n_cells, mean, rank, n_modules_compared}``
        per stratum ('' = all cells; rank is null where the stratum is too small or has too few peers).
    """
    pool = means[means['source'] == source]
    mine = pool[(pool['catalog'] == catalog) & (pool['module'] == module)]
    if mine.empty:
        return _empty('no score of this gene set was found in the score file of this dataset')
    total_cells = int(mine['n'].sum())
    overall_mean = float((mine['n'] * mine['mean']).sum() / total_cells)
    others = pool.drop(mine.index)
    others = others[others['n'] >= min_cells]
    strata: List[Dict[str, Any]] = []
    for row in mine.sort_values('n', ascending=False).itertuples():
        peers = others.loc[others['stratum'] == row.stratum, 'mean'].to_numpy()
        entry: Dict[str, Any] = {'cell_type': row.stratum, 'n_cells': int(row.n), 'mean': float(row.mean), 'std': _finite(row.std),
                                 'rank': None, 'n_modules_compared': int(len(peers))}
        if row.n >= min_cells and len(peers) >= min_peers:
            entry['rank'] = float((np.sum(peers < row.mean) + 0.5 * np.sum(peers == row.mean)) / len(peers))
        strata.append(entry)
    ranked = [entry for entry in strata if entry['rank'] is not None]
    if not ranked:
        return {**_empty('too few cells or too few other modules scored on the same cells to rank'),
                'n_cells': total_cells, 'mean': overall_mean, 'std': _pooled_std(mine, overall_mean), 'cell_types': strata,
                'n_modules_compared': strata[0]['n_modules_compared']}
    weights = np.array([entry['n_cells'] for entry in ranked], dtype=float)
    rank = float(np.average([entry['rank'] for entry in ranked], weights=weights))
    return {'available': True, 'reason': None, 'rank': rank,
            'n_modules_compared': ranked[0]['n_modules_compared'],
            'n_cells': total_cells, 'mean': overall_mean, 'std': _pooled_std(mine, overall_mean), 'cell_types': strata}


class ModuleRankStore:
    """Per-dataset ``stratum_means`` tables: in memory, backed by parquet files on disk.

    A table is rebuilt only when the fingerprint (score sidecar and duckdb stat) changes. Builds read
    the whole sidecar (tens of millions of rows for the big datasets), so at most ``max_parallel`` run
    at once and each dataset is built once however many requests ask for it.
    """

    def __init__(self, cache_dir: Path, max_parallel: int = 2) -> None:
        self.cache_dir = Path(cache_dir)
        self._memory: Dict[str, Any] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._slots = threading.Semaphore(max_parallel)

    def _file(self, db_id: str, fingerprint: str) -> Path:
        return self.cache_dir / f"{db_id.replace('/', '__')}~{fingerprint}.parquet"

    def get(self, db_id: str, fingerprint: str, build: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        """Return the table of a dataset, building (and caching) it when its fingerprint is new.

        Args:
            db_id: dataset id.
            fingerprint: string that changes whenever the scores or cell types change (``table_fingerprint``).
            build: zero-argument function returning the ``stratum_means`` frame.
        """
        return self.get_with_status(db_id, fingerprint, build)[0]

    def get_with_status(self, db_id: str, fingerprint: str, build: Callable[[], pd.DataFrame]
                        ) -> Tuple[pd.DataFrame, bool]:
        """``get``, and whether this call built the table (False: memory or disk)."""
        with self._guard:
            cached = self._memory.get(db_id)
            if cached and cached[0] == fingerprint:
                return cached[1], False
            lock = self._locks.setdefault(db_id, threading.Lock())
        with lock:
            cached = self._memory.get(db_id)
            if cached and cached[0] == fingerprint:
                return cached[1], False
            path = self._file(db_id, fingerprint)
            if path.exists():
                try:
                    frame = pd.read_parquet(path)
                except (OSError, ValueError):
                    frame = None
                if frame is not None and not set(MEANS_COLUMNS) <= set(frame.columns):
                    frame = None      # written before a column was added: rebuild
            else:
                frame = None
            built = frame is None
            if built:
                with self._slots:
                    frame = build()
                self._write(db_id, path, frame)
            self._memory[db_id] = (fingerprint, frame)
            return frame, built

    def _write(self, db_id: str, path: Path, frame: pd.DataFrame) -> None:
        """Write atomically (unique temporary file, then rename) under an exclusive lock per dataset, so several
        workers or processes sharing the folder never see or produce a partial file, and drop the files of older
        fingerprints of the same dataset. A folder that is not writable only costs the disk copy."""
        stem = db_id.replace('/', '__')
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_dir / f'.{stem}.lock', 'a') as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
                handle, temporary = tempfile.mkstemp(dir=self.cache_dir, prefix=f'.{stem}~', suffix='.tmp')
                os.close(handle)
                frame.to_parquet(temporary, index=False)
                os.replace(temporary, path)
                temporary = None
                for old in self.cache_dir.glob(stem + '~*.parquet'):
                    if old != path:
                        old.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - a full or read-only disk, or a frame parquet cannot store
            pass
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
