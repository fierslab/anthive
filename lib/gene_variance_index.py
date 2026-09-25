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
  eta_squared, F_stat, signal, total_var, group_means_json,
  src_fingerprint (optional; split off at load, see ``index_freshness``)

``signal`` mirrors the existing endpoint's ranking metric: ``VAR_SAMP``
across group means. ``eta_squared`` is the between-group variance share
(SS_between / SS_total). ``group_means_json`` is a JSON string mapping
group label -> group mean, hydrating the endpoint response without a
DuckDB round-trip.

``load()`` builds a complete snapshot off to the side and publishes it
with one attribute assignment, so readers running during a rescan see
the old or the new index, never an empty one. Datasets whose source
files changed after the precompute read as misses (see
``index_freshness``), which sends the endpoint to its live path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from index_freshness import FreshnessChecker, extract_fingerprints


#: Columns copied from a parquet row into a response entry. Kept in sync
#: with the schema written by ``ant-gene-variance-precompute``.
STAT_COLUMNS = (
    'eta_squared', 'F_stat', 'signal', 'total_var',
    'n_cells', 'n_groups',
)


class _State:
    """One immutable snapshot of a loaded index; swapped in atomically.

    ``df`` is sorted by (dataset, facet, signal desc, NaN last) with a
    stable sort, so every (dataset, facet) pair is one contiguous slice
    recorded in ``by_pair`` as ``(start, stop)`` row positions.
    """

    __slots__ = ('df', 'by_pair', 'by_triple', 'freshness')

    def __init__(
        self,
        df: Optional[pd.DataFrame],
        by_pair: Dict[tuple, Tuple[int, int]],
        by_triple: Dict[tuple, int],
        freshness: FreshnessChecker,
    ) -> None:
        self.df = df
        self.by_pair = by_pair
        self.by_triple = by_triple
        self.freshness = freshness


def _empty_state() -> _State:
    return _State(None, {}, {}, FreshnessChecker({}, 0))


def _pair_bounds(df: pd.DataFrame) -> Dict[tuple, Tuple[int, int]]:
    """Map each (dataset, facet) run in a key-sorted frame to its slice."""
    if df.empty:
        return {}
    datasets = df['dataset'].values
    facets = df['facet'].values
    changed = (datasets[1:] != datasets[:-1]) | (facets[1:] != facets[:-1])
    starts = np.concatenate(([0], np.flatnonzero(changed) + 1))
    stops = np.concatenate((starts[1:], [len(df)]))
    return {
        (datasets[start], facets[start]): (int(start), int(stop))
        for start, stop in zip(starts, stops)
    }


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
        self._state: _State = _empty_state()
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
        with self._lock:
            if not self.path.exists():
                self._state = _empty_state()
                self.n_rows = self.n_datasets = self.n_genes = self.n_facets = 0
                self.loaded_at = None
                return
            df = pd.read_parquet(self.path)
            df['dataset'] = df['dataset'].astype(str)
            df['gene'] = df['gene'].astype(str)
            df['facet'] = df['facet'].astype(str)
            df, freshness = extract_fingerprints(
                df, self.path.parent, self.path, 'gene_variance_index',
            )
            # Stable multi-key sort: within a pair, rows keep file order on
            # signal ties, matching the old per-pair mergesort exactly.
            df = df.sort_values(
                ['dataset', 'facet', 'signal'],
                ascending=[True, True, False],
                na_position='last',
                kind='mergesort',
            ).reset_index(drop=True)
            by_triple = {
                (dataset, gene, facet): idx
                for idx, (dataset, gene, facet) in enumerate(
                    zip(df['dataset'].values, df['gene'].values, df['facet'].values)
                )
            }
            self._state = _State(df, _pair_bounds(df), by_triple, freshness)
            self.n_rows = int(len(df))
            self.n_datasets = int(df['dataset'].nunique())
            self.n_genes = int(df['gene'].nunique())
            self.n_facets = int(df['facet'].nunique())
            self.loaded_at = time.time()

    def is_loaded(self) -> bool:
        return self._state.df is not None

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
        state = self._state
        if state.df is None:
            return None
        idx = state.by_triple.get((db_id, gene, facet))
        if idx is None or not state.freshness.is_fresh(db_id):
            return None
        return self._row_to_dict(state.df.iloc[idx])

    def has_pair(self, db_id: str, facet: str) -> bool:
        """Whether (dataset, facet) has fresh rows in the index."""
        return self._pair_sub(self._state, db_id, facet) is not None

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
        sub = self._pair_sub(self._state, db_id, facet)
        if sub is None:
            return None
        head = sub.head(int(top))
        return [self._row_to_entry(row) for row in head.to_dict('records')]

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
        sub = self._pair_sub(self._state, db_id, facet)
        if sub is None:
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
            'has_fingerprints': self._state.freshness.has_fingerprints,
            'stale_datasets':   self._state.freshness.stale_datasets(),
        }

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _pair_sub(
        state: _State, db_id: str, facet: str,
    ) -> Optional[pd.DataFrame]:
        """Return the (dataset, facet) slice sorted by signal desc.

        ``None`` when the pair is absent or its dataset is stale. The
        slice is a view over the pre-sorted frame, so nothing is copied
        or cached per pair.
        """
        if state.df is None:
            return None
        bounds = state.by_pair.get((db_id, facet))
        if bounds is None or not state.freshness.is_fresh(db_id):
            return None
        start, stop = bounds
        return state.df.iloc[start:stop]

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
