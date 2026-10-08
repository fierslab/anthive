"""In-memory (dataset, gene) → stats lookup, backed by
``<h5adstore>/gene_stats_index.parquet``.

Precomputed by ``bin/annot/ant-gene-stats-precompute``. ant-serve loads
the parquet at startup and after ``POST /admin/rescan``; queries against
``/gene_stats_all`` hit the parquet first and only fan out to live
duckdb calls for (dataset, gene) pairs not represented here.

``load()`` publishes a new snapshot atomically, so lookups running
concurrently with a rescan never see an empty or half-built index.
Rows for datasets whose source files changed after the precompute are
treated as misses (see ``index_freshness``).
"""
from __future__ import annotations

import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

import pandas as pd

from index_freshness import FreshnessChecker, extract_fingerprints


#: Columns copied from a parquet row into a response ``stats`` entry. Kept
#: in sync with the schema written by ``ant-gene-stats-precompute``.
_STAT_COLUMNS = (
    'n_cells', 'n_nonzero', 'pct_nonzero',
    'mean', 'std', 'cv',
    'mean_nonzero', 'std_nonzero', 'median_nonzero',
    'q10_nonzero', 'q90_nonzero', 'max',
    'mean_expr_layer', 'rank_mean', 'scaled_mean',
)


def _row_to_stats(row: pd.Series, gene_query: str) -> Dict[str, Any]:
    """Convert a parquet row to the JSON shape returned by ``/gene_stats``.

    Args:
        row: One (dataset, gene) row.
        gene_query: The caller's queried gene name — echoed back as
            ``gene`` (so the response ordering aligns with ``gene_list``).

    Returns:
        Dict matching the fields ``AnthiveDB.get_gene_stats`` returns
        (per-gene). ``gene_found`` is added when the stored name differs
        from the queried name.
    """
    stored = str(row['gene'])
    stats: Dict[str, Any] = {
        'gene':  gene_query,
        'layer': str(row['layer']),
    }
    if stored != gene_query:
        stats['gene_found'] = stored
    for col in _STAT_COLUMNS:
        val = row.get(col)
        if val is None:
            continue
        try:
            if pd.isna(val):
                # Preserve NaN → None so the JSON encoder emits null instead
                # of crashing on NaN (FastAPI's default encoder handles None).
                stats[col] = None
            else:
                stats[col] = float(val) if col not in ('n_cells', 'n_nonzero') else int(val)
        except (TypeError, ValueError):
            stats[col] = val
    return stats


class _State:
    """One immutable snapshot of a loaded index; swapped in atomically."""

    __slots__ = ('df', 'by_key_ci', 'by_key_cs', 'freshness')

    def __init__(
        self,
        df: Optional[pd.DataFrame],
        by_key_ci: Dict[tuple, int],
        by_key_cs: Dict[tuple, int],
        freshness: FreshnessChecker,
    ) -> None:
        self.df = df
        self.by_key_ci = by_key_ci
        self.by_key_cs = by_key_cs
        self.freshness = freshness


def _empty_state() -> _State:
    return _State(None, {}, {}, FreshnessChecker({}, 0))


class GeneStatsIndex:
    """Frozen (dataset, gene) → stats lookup backed by a parquet file.

    The parquet is loaded once (or reloaded on rescan) into a DataFrame
    plus ``(dataset, gene)`` → row dicts, so lookups are O(1) per key.
    Case-insensitive gene lookup is the default because the endpoint is
    case-insensitive by default.

    Attributes:
        path: Path to the source parquet.
        loaded_at: Wall-clock time the parquet was last read (epoch
            seconds), or ``None`` if the file was absent.
        n_rows: Row count of the loaded frame (0 if absent).
        n_datasets: Distinct dataset count (0 if absent).
        n_genes: Distinct gene count (0 if absent).
    """

    def __init__(self, path: Path) -> None:
        """Bind the parquet path; does NOT read the file (call :meth:`load`)."""
        self.path = path
        self._lock = RLock()
        self._state: _State = _empty_state()
        self.loaded_at: Optional[float] = None
        self.n_rows: int = 0
        self.n_datasets: int = 0
        self.n_genes: int = 0

    # ------------------------------------------------------------------ IO

    def load(self) -> None:
        """(Re)load the parquet. Silently no-ops if the file is absent.

        A missing file is a supported state: it means "the precompute
        hasn't been run yet, or is disabled". Callers should treat every
        lookup as a miss in that case.
        """
        with self._lock:
            if not self.path.exists():
                self._state = _empty_state()
                self.n_rows = self.n_datasets = self.n_genes = 0
                self.loaded_at = None
                return
            df = pd.read_parquet(self.path)
            # Coerce identifier columns to plain str so `.lower()` is safe
            # even if pyarrow returned an object dtype.
            df['dataset'] = df['dataset'].astype(str)
            df['gene'] = df['gene'].astype(str)
            df, freshness = extract_fingerprints(
                df, self.path.parent, self.path, 'gene_stats_index',
            )
            df = df.reset_index(drop=True)
            datasets = df['dataset'].values
            genes = df['gene'].values
            by_key_ci = {
                (dataset, gene.lower()): idx
                for idx, (dataset, gene) in enumerate(zip(datasets, genes))
            }
            by_key_cs = {
                (dataset, gene): idx
                for idx, (dataset, gene) in enumerate(zip(datasets, genes))
            }
            self._state = _State(df, by_key_ci, by_key_cs, freshness)
            self.n_rows = int(len(df))
            self.n_datasets = int(df['dataset'].nunique())
            self.n_genes = int(df['gene'].nunique())
            self.loaded_at = time.time()

    def is_loaded(self) -> bool:
        return self._state.df is not None

    # ------------------------------------------------------------------ Lookup

    def lookup(
        self,
        db_id: str,
        gene: str,
        case_sensitive: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the cached stats dict for ``(db_id, gene)`` or ``None``.

        Args:
            db_id: Canonical ``group/file`` id.
            gene: Queried gene name.
            case_sensitive: If ``False`` (default), match on lowercased
                gene name (mirrors the endpoint default).

        Returns:
            Stats dict shaped like ``AnthiveDB.get_gene_stats`` output, or
            ``None`` if the pair is not in the index or the dataset's
            source files changed since the precompute ran.
        """
        state = self._state
        if state.df is None:
            return None
        key = (db_id, gene if case_sensitive else gene.lower())
        idx = (state.by_key_cs if case_sensitive else state.by_key_ci).get(key)
        if idx is None:
            return None
        if not state.freshness.is_fresh(db_id):
            return None
        return _row_to_stats(state.df.iloc[idx], gene)

    def frame(self) -> Optional[pd.DataFrame]:
        """The loaded frame (``dataset``, ``gene``, ``n_cells``, ``pct_nonzero``, ...), None when no file is
        loaded. The object changes on every ``load``, so ``id(frame)`` identifies a snapshot. Do not modify."""
        return self._state.df

    def is_fresh(self, db_id: str) -> bool:
        """Whether ``db_id``'s rows still match its source files."""
        return self._state.freshness.is_fresh(db_id)

    def stats(self) -> Dict[str, Any]:
        """Return a summary payload for `/admin/gene-stats-index/stats`."""
        freshness = self._state.freshness
        return {
            'path':             str(self.path),
            'exists':           self.path.exists(),
            'loaded':           self.is_loaded(),
            'n_rows':           self.n_rows,
            'n_datasets':       self.n_datasets,
            'n_genes':          self.n_genes,
            'loaded_at':        self.loaded_at,
            'has_fingerprints': freshness.has_fingerprints,
            'stale_datasets':   freshness.stale_datasets(),
        }
