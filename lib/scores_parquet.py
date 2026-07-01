"""Long-format per-cell score sidecar (`<basename>.scores.parquet`).

Lives next to the `.duckdb` / `.h5ad` in the h5adstore so it syncs
alongside them. Decoupled from the main duckdb so annotators (module
scoring, starcat, future) can rebuild without touching the served DB.

Schema
------
=======  ========  ================================================
column   type      meaning
=======  ========  ================================================
cell_id  BIGINT    joins ``__cells.cell_id`` in the sibling duckdb
source   VARCHAR   annotator family ('geneset', 'starcat', ...)
catalog  VARCHAR   catalog id ('Sierksma2025/WGCNA', 'MICROGLIA_V1')
module   VARCHAR   module / program name within the catalog
score    DOUBLE    per-cell score
=======  ========  ================================================

Write semantics
---------------
:func:`upsert_scores` is the sole public writer:

* removes every existing row matching ``(source, catalog)`` from the
  sidecar, then appends the caller's rows;
* is all-or-nothing — writes to a ``*.tmp`` file then ``os.replace``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd


SCHEMA_COLUMNS = ('cell_id', 'source', 'catalog', 'module', 'score')


def scores_path(duckdb_path: Path) -> Path:
    """Sidecar path for a given duckdb (both live in the same folder)."""
    name = duckdb_path.name
    if not name.endswith('.duckdb'):
        raise ValueError(f"expected a .duckdb path, got {duckdb_path}")
    return duckdb_path.with_name(name[: -len('.duckdb')] + '.scores.parquet')


def read_scores(
    duckdb_path: Path,
    source: Optional[str] = None,
    catalog: Optional[str] = None,
    module: Optional[str] = None,
) -> pd.DataFrame:
    """Read the sidecar, optionally filtered on (source, catalog, module).

    Returns an empty DataFrame with the canonical columns if no sidecar
    exists yet. Uses DuckDB for predicate pushdown on parquet.
    """
    sidecar = scores_path(duckdb_path)
    if not sidecar.exists():
        return pd.DataFrame({col: [] for col in SCHEMA_COLUMNS})

    predicates = []
    params: list = []
    if source is not None:
        predicates.append('source = ?')
        params.append(source)
    if catalog is not None:
        predicates.append('catalog = ?')
        params.append(catalog)
    if module is not None:
        predicates.append('module = ?')
        params.append(module)
    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ''

    conn = duckdb.connect(':memory:')
    try:
        return conn.execute(
            f"SELECT cell_id, source, catalog, module, score "
            f"FROM read_parquet(?) {where_clause}",
            [str(sidecar), *params],
        ).df()
    finally:
        conn.close()


def upsert_scores(
    duckdb_path: Path,
    source: str,
    catalog: str,
    rows: pd.DataFrame,
) -> int:
    """Replace all (source, catalog) rows in the sidecar with ``rows``.

    ``rows`` must have columns cell_id, module, score. Any extra columns
    are dropped. The (source, catalog) pair is stamped on every row by
    this function — callers should not include them.

    Returns the number of rows written (post-upsert row count for this
    source+catalog).
    """
    if not {'cell_id', 'module', 'score'}.issubset(rows.columns):
        raise ValueError(
            f"rows must have columns [cell_id, module, score]; got {list(rows.columns)}"
        )

    fresh = rows[['cell_id', 'module', 'score']].copy()
    fresh['cell_id'] = fresh['cell_id'].astype('int64')
    fresh['module'] = fresh['module'].astype(str)
    fresh['score'] = fresh['score'].astype('float64')
    fresh['source'] = source
    fresh['catalog'] = catalog
    fresh = fresh[list(SCHEMA_COLUMNS)]

    sidecar = scores_path(duckdb_path)
    tmp = sidecar.with_suffix(sidecar.suffix + '.tmp')

    conn = duckdb.connect(':memory:')
    try:
        conn.register('fresh_rows', fresh)
        if sidecar.exists():
            # keep rows that are NOT (source, catalog), then union fresh
            conn.execute(
                "CREATE TABLE combined AS "
                "SELECT cell_id, source, catalog, module, score "
                "FROM read_parquet(?) "
                "WHERE NOT (source = ? AND catalog = ?) "
                "UNION ALL BY NAME "
                "SELECT * FROM fresh_rows",
                [str(sidecar), source, catalog],
            )
        else:
            conn.execute("CREATE TABLE combined AS SELECT * FROM fresh_rows")

        conn.execute(
            "COPY combined TO ? (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)",
            [str(tmp)],
        )
    finally:
        conn.close()

    os.replace(tmp, sidecar)
    return len(fresh)


def summarize(duckdb_path: Path) -> pd.DataFrame:
    """One-row-per-(source, catalog, module) summary of the sidecar."""
    sidecar = scores_path(duckdb_path)
    if not sidecar.exists():
        return pd.DataFrame(columns=['source', 'catalog', 'module', 'n_cells',
                                     'min_score', 'max_score', 'mean_score'])
    conn = duckdb.connect(':memory:')
    try:
        return conn.execute(
            "SELECT source, catalog, module, "
            "  COUNT(*) AS n_cells, "
            "  MIN(score) AS min_score, "
            "  MAX(score) AS max_score, "
            "  AVG(score) AS mean_score "
            "FROM read_parquet(?) "
            "GROUP BY source, catalog, module "
            "ORDER BY source, catalog, module",
            [str(sidecar)],
        ).df()
    finally:
        conn.close()
