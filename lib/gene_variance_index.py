"""In-memory (dataset, gene, facet) -> variance stats lookup, backed by
``<h5adstore>/gene_variance_index.parquet``.

Precomputed by ``bin/annot/ant-gene-variance-precompute``. ant-serve loads
the parquet at startup and after ``POST /admin/rescan``; the
``/gene-variance-by-facet`` endpoint hits the parquet first and only
falls back to live DuckDB compute when the (dataset, facet) triple is
absent (typically because the dataset is remote, the facet was recently
added, or the precompute has not run).

Schema (wide, one row per (dataset, gene, facet)):

  dataset, gene, facet, facet_type, layer, n_cells, n_groups,
  eta_squared, F_stat, signal, total_var, group_means_json

``signal`` mirrors the existing endpoint's ranking metric: ``VAR_SAMP``
across group means. ``eta_squared`` is the between-group variance share
(SS_between / SS_total). ``group_means_json`` is a JSON string mapping
group label -> group mean, hydrating the endpoint response without a
DuckDB round-trip.

Not thread-safe on ``load()`` -- only the startup/rescan hook should
call it. Per-lookup access is thread-safe (read-only slicing).
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

import pandas as pd


#: Columns copied from a parquet row into a response entry. Kept in sync
#: with the schema written by ``ant-gene-variance-precompute``.
STAT_COLUMNS = (
    'eta_squared', 'F_stat', 'signal', 'total_var',
    'n_cells', 'n_groups',
)


class GeneVarianceIndex:
    """Frozen (dataset, gene, facet) -> variance stats backed by parquet.

    Two access patterns:

    * :meth:`top_by_facet` -- rank genes by ``signal`` for one
      (dataset, facet). Serves the ``/gene-variance-by-facet`` endpoint.
    * :meth:`lookup` -- pull one (dataset, gene, facet) row. Not
      currently used by the endpoint but handy for ad-hoc probes.

    Attributes:
        path: Path to the source parquet.
        loaded_at: Wall-clock time the parquet was last read (epoch
            seconds), or ``None`` if the file was absent.
        n_rows: Row count of the loaded frame (0 if absent).
        n_datasets: Distinct dataset count (0 if absent).
        n_genes: Distinct gene count (0 if absent).
        n_facets: Distinct facet count (0 if absent).
    """

    def __init__(self, path: Path) -> None:
        """Bind the parquet path; does NOT read the file (call :meth:`load`)."""
        self.path = path
        self._lock = RLock()
        self._df: Optional[pd.DataFrame] = None
        # (dataset, facet) -> sub-DataFrame sorted by signal desc.
        # Built lazily on first query and cached, so repeated top-K
        # calls skip the group-by cost.
        self._by_pair: Dict[tuple, pd.DataFrame] = {}
        # (dataset, gene, facet) -> integer row index.
        self._by_triple: Dict[tuple, int] = {}
        self.loaded_at: Optional[float] = None
        self.n_rows: int = 0
        self.n_datasets: int = 0
        self.n_genes: int = 0
        self.n_facets: int = 0

    # ------------------------------------------------------------------ IO

    def load(self) -> None:
        """(Re)load the parquet. Silently no-ops if the file is absent.

        A missing file is a supported state: it means "the precompute
        hasn't been run yet, or is disabled". Callers should fall back
        to the live compute path in that case.
        """
        import time
        with self._lock:
            self._by_pair.clear()
            self._by_triple.clear()
            if not self.path.exists():
                self._df = None
                self.n_rows = self.n_datasets = self.n_genes = self.n_facets = 0
                self.loaded_at = None
                return
            df = pd.read_parquet(self.path)
            df['dataset'] = df['dataset'].astype(str)
            df['gene'] = df['gene'].astype(str)
            df['facet'] = df['facet'].astype(str)
            self._df = df.reset_index(drop=True)

            for idx, (dataset, gene, facet) in enumerate(
                zip(
                    self._df['dataset'].values,
                    self._df['gene'].values,
                    self._df['facet'].values,
                )
            ):
                self._by_triple[(dataset, gene, facet)] = idx

            self.n_rows = int(len(self._df))
            self.n_datasets = int(self._df['dataset'].nunique())
            self.n_genes = int(self._df['gene'].nunique())
            self.n_facets = int(self._df['facet'].nunique())
            self.loaded_at = time.time()

    def is_loaded(self) -> bool:
        return self._df is not None

    # ------------------------------------------------------------------ Lookup

    def lookup(
        self, db_id: str, gene: str, facet: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the cached row for one triple, or ``None``.

        Args:
            db_id: Canonical ``group/file`` id.
            gene: Gene name (case-sensitive; the precompute wrote stored names).
            facet: The ``ant_*`` obs column name.

        Returns:
            Dict of the wide row's fields, or ``None`` if absent.
        """
        if self._df is None:
            return None
        idx = self._by_triple.get((db_id, gene, facet))
        if idx is None:
            return None
        return self._row_to_dict(self._df.iloc[idx])

    def has_pair(self, db_id: str, facet: str) -> bool:
        """Whether (dataset, facet) has any rows in the index."""
        if self._df is None:
            return False
        sub = self._pair_sub(db_id, facet)
        return not sub.empty

    def top_by_facet(
        self, db_id: str, facet: str, top: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return top ``top`` genes for (db_id, facet), ranked by ``signal``.

        Args:
            db_id: Canonical ``group/file`` id.
            facet: The ``ant_*`` obs column name.
            top: Cap on returned rows.

        Returns:
            A list of gene entries shaped like
            ``{'gene': str, 'signal': float, 'eta_squared': float,
               'F_stat': float, 'total_var': float, 'n_cells': int,
               'n_groups': int, 'group_means': {group: mean}}``
            or ``None`` if the index is empty for that pair (caller
            should live-compute).
        """
        if self._df is None:
            return None
        sub = self._pair_sub(db_id, facet)
        if sub.empty:
            return None
        head = sub.head(int(top))
        out: List[Dict[str, Any]] = []
        for _, row in head.iterrows():
            out.append(self._row_to_entry(row))
        return out

    def facet_metadata(
        self, db_id: str, facet: str,
    ) -> Optional[Dict[str, Any]]:
        """Return (facet_type, layer, n_cells, n_groups, groups) for a pair.

        Populated from the first row of the pair (all rows share these).
        Groups are inferred from ``group_means_json`` if present.

        Args:
            db_id: Canonical ``group/file`` id.
            facet: The ``ant_*`` obs column name.

        Returns:
            Dict or ``None`` if the pair has no rows.
        """
        if self._df is None:
            return None
        sub = self._pair_sub(db_id, facet)
        if sub.empty:
            return None
        row = sub.iloc[0]
        gm = _parse_group_means(row.get('group_means_json'))
        groups = sorted(gm.keys()) if gm else []
        return {
            'facet_type': str(row.get('facet_type') or ''),
            'layer': str(row.get('layer') or ''),
            'n_cells': int(row.get('n_cells') or 0),
            'n_groups': int(row.get('n_groups') or 0),
            'groups': groups,
        }

    def stats(self) -> Dict[str, Any]:
        """Summary payload for ``/admin/gene-variance-index/stats``."""
        return {
            'path':       str(self.path),
            'exists':     self.path.exists(),
            'loaded':     self.is_loaded(),
            'n_rows':     self.n_rows,
            'n_datasets': self.n_datasets,
            'n_genes':    self.n_genes,
            'n_facets':   self.n_facets,
            'loaded_at':  self.loaded_at,
        }

    # ------------------------------------------------------------------ internal

    def _pair_sub(self, db_id: str, facet: str) -> pd.DataFrame:
        """Return the (dataset, facet) slice sorted by signal desc.

        Cached in ``self._by_pair`` so repeated top-K calls are O(top).
        """
        key = (db_id, facet)
        cached = self._by_pair.get(key)
        if cached is not None:
            return cached
        assert self._df is not None
        mask = (self._df['dataset'] == db_id) & (self._df['facet'] == facet)
        sub = self._df.loc[mask].sort_values(
            'signal', ascending=False, na_position='last', kind='mergesort',
        )
        self._by_pair[key] = sub
        return sub

    @staticmethod
    def _row_to_dict(row: pd.Series) -> Dict[str, Any]:
        """Serialise a wide row into a plain dict (no group_means expansion)."""
        out: Dict[str, Any] = {
            'dataset':    str(row['dataset']),
            'gene':       str(row['gene']),
            'facet':      str(row['facet']),
            'facet_type': str(row.get('facet_type') or ''),
            'layer':      str(row.get('layer') or ''),
        }
        for col in STAT_COLUMNS:
            val = row.get(col)
            if val is None:
                continue
            try:
                if pd.isna(val):
                    out[col] = None
                elif col in ('n_cells', 'n_groups'):
                    out[col] = int(val)
                else:
                    out[col] = float(val)
            except (TypeError, ValueError):
                out[col] = val
        return out

    @staticmethod
    def _row_to_entry(row: pd.Series) -> Dict[str, Any]:
        """Serialise a wide row into the endpoint's per-gene entry shape."""
        signal = row.get('signal')
        eta = row.get('eta_squared')
        f_stat = row.get('F_stat')
        total_var = row.get('total_var')
        entry: Dict[str, Any] = {
            'gene': str(row['gene']),
            'signal': (
                None if signal is None or pd.isna(signal) else float(signal)
            ),
            'eta_squared': (
                None if eta is None or pd.isna(eta) else float(eta)
            ),
            'F_stat': (
                None if f_stat is None or pd.isna(f_stat) else float(f_stat)
            ),
            'total_var': (
                None if total_var is None or pd.isna(total_var)
                else float(total_var)
            ),
            'group_means': _parse_group_means(row.get('group_means_json')),
        }
        return entry


def _parse_group_means(raw: Any) -> Dict[str, float]:
    """Parse a JSON string of ``{group: mean}`` into a dict of floats.

    Returns an empty dict on any failure (missing / malformed / None).
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, str) and raw:
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(obj, dict):
            return {
                str(k): float(v)
                for k, v in obj.items()
                if v is not None
            }
    return {}
