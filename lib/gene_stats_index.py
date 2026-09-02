"""In-memory (dataset, gene) → stats lookup, backed by
``<h5adstore>/gene_stats_index.parquet``.

Precomputed by ``bin/annot/ant-gene-stats-precompute``. ant-serve loads
the parquet at startup and after ``POST /admin/rescan``; queries against
``/gene_stats_all`` hit the parquet first and only fan out to live
duckdb calls for (dataset, gene) pairs not represented here.

Not thread-safe on ``load()`` — only the startup/rescan hook should
call it. Per-lookup access is thread-safe (read-only DataFrame slicing).
"""
from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

import pandas as pd


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


class GeneStatsIndex:
    """Frozen (dataset, gene) → stats lookup backed by a parquet file.

    The parquet is loaded once (or reloaded on rescan) into a pandas
    DataFrame indexed on ``(dataset, gene_lower)``. Lookups are O(1) per
    key. Case-insensitive gene lookup is the default because the endpoint
    is case-insensitive by default.

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
        self._df: Optional[pd.DataFrame] = None
        self._by_key_ci: Dict[tuple, int] = {}      # (dataset, gene_lower) → row_idx
        self._by_key_cs: Dict[tuple, int] = {}      # (dataset, gene) → row_idx
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
        import time
        with self._lock:
            if not self.path.exists():
                self._df = None
                self._by_key_ci.clear()
                self._by_key_cs.clear()
                self.n_rows = self.n_datasets = self.n_genes = 0
                self.loaded_at = None
                return
            df = pd.read_parquet(self.path)
            # Coerce identifier columns to plain str so `.map(str.lower)` is
            # safe even if pyarrow returned an object dtype.
            df['dataset'] = df['dataset'].astype(str)
            df['gene'] = df['gene'].astype(str)
            self._df = df.reset_index(drop=True)

            self._by_key_ci = {
                (ds, gene.lower()): idx
                for idx, (ds, gene) in enumerate(
                    zip(self._df['dataset'].values, self._df['gene'].values)
                )
            }
            self._by_key_cs = {
                (ds, gene): idx
                for idx, (ds, gene) in enumerate(
                    zip(self._df['dataset'].values, self._df['gene'].values)
                )
            }
            self.n_rows = int(len(self._df))
            self.n_datasets = int(self._df['dataset'].nunique())
            self.n_genes = int(self._df['gene'].nunique())
            self.loaded_at = time.time()

    def is_loaded(self) -> bool:
        return self._df is not None

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
            ``None`` if the pair is not in the index.
        """
        if self._df is None:
            return None
        key = (db_id, gene if case_sensitive else gene.lower())
        idx = (self._by_key_cs if case_sensitive else self._by_key_ci).get(key)
        if idx is None:
            return None
        return _row_to_stats(self._df.iloc[idx], gene)

    def has_dataset(self, db_id: str) -> bool:
        """Whether ``db_id`` appears in the loaded index at all."""
        if self._df is None:
            return False
        # Precompute this once for speed if it becomes hot; today the call
        # site is only /gene_stats_all which pays this per-request per-db.
        return bool((self._df['dataset'] == db_id).any())

    def stats(self) -> Dict[str, Any]:
        """Return a summary payload for `/admin/gene-stats-index/stats`."""
        return {
            'path':       str(self.path),
            'exists':     self.path.exists(),
            'loaded':     self.is_loaded(),
            'n_rows':     self.n_rows,
            'n_datasets': self.n_datasets,
            'n_genes':    self.n_genes,
            'loaded_at':  self.loaded_at,
        }

    def genes_for_dataset(self, db_id: str, case_sensitive: bool) -> set[str]:
        """Return the set of hot genes indexed for one dataset.

        Used by the endpoint to bucket queried genes into
        "served-from-parquet" vs "needs-live-query" before firing off any
        thread work.

        Args:
            db_id: Canonical ``group/file`` id.
            case_sensitive: If ``False``, returned names are lowercased.

        Returns:
            Set of gene names in the index for that dataset. Empty set if
            the dataset is not indexed or the parquet is not loaded.
        """
        if self._df is None:
            return set()
        # Cheap boolean mask over an in-memory DataFrame — acceptable at
        # /gene_stats_all fan-out scale (~185 dbs × 1 mask each per call).
        # A per-db pre-index would micro-optimise this but complicates load.
        sub = self._df[self._df['dataset'] == db_id]['gene']
        if case_sensitive:
            return set(sub.astype(str))
        return {g.lower() for g in sub.astype(str)}
