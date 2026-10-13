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
* is all-or-nothing — writes to a per-process ``*.<pid>.tmp`` file then
  ``os.replace``;
* holds an exclusive ``flock`` on ``<sidecar>.lock`` for the whole
  read-modify-replace, so concurrent writers serialise instead of
  overwriting each other's catalogs.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
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
    # Per-process temp name: two writers must never share one .tmp file.
    tmp = sidecar.with_name(f'{sidecar.name}.{os.getpid()}.tmp')

    # The read-modify-replace below is only atomic per writer; without the
    # lock two concurrent scorers each rebuild from the same old sidecar
    # and the later os.replace silently drops the other's catalog.
    with open(_lock_path(sidecar), 'a') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
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
                    "COPY combined TO ? "
                    "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)",
                    [str(tmp)],
                )
            finally:
                conn.close()
            os.replace(tmp, sidecar)
        finally:
            if tmp.exists():
                tmp.unlink()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return len(fresh)


def delete_scores(duckdb_path: Path, source: str, catalog: str) -> int:
    """Remove every (source, catalog) row from the sidecar (no-op without a sidecar).

    Used when a catalog no longer applies to a dataset (no cell of the dataset
    belongs to the catalog's cell type scope), so stale rows do not stay behind.
    Same all-or-nothing, flock protected rewrite as :func:`upsert_scores`; a
    sidecar left without any row is removed.

    Returns:
        Number of rows removed.
    """
    sidecar = scores_path(duckdb_path)
    if not sidecar.exists():
        return 0
    tmp = sidecar.with_name(f'{sidecar.name}.{os.getpid()}.tmp')
    with open(_lock_path(sidecar), 'a') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            conn = duckdb.connect(':memory:')
            try:
                removed = conn.execute(
                    "SELECT COUNT(*) FROM read_parquet(?) WHERE source = ? AND catalog = ?",
                    [str(sidecar), source, catalog]).fetchone()[0]
                if removed:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM read_parquet(?) WHERE NOT (source = ? AND catalog = ?)",
                        [str(sidecar), source, catalog]).fetchone()[0]
                    if remaining:
                        conn.execute(
                            "CREATE TABLE kept AS SELECT cell_id, source, catalog, module, score "
                            "FROM read_parquet(?) WHERE NOT (source = ? AND catalog = ?)",
                            [str(sidecar), source, catalog])
                        conn.execute(
                            "COPY kept TO ? (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)",
                            [str(tmp)])
                        os.replace(tmp, sidecar)
                    else:
                        sidecar.unlink()
            finally:
                conn.close()
        finally:
            if tmp.exists():
                tmp.unlink()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return int(removed)


def _lock_path(sidecar: Path) -> Path:
    """Advisory lock file serialising writers of one sidecar."""
    return sidecar.with_name(sidecar.name + '.lock')


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


# ── Read side for the served API (ant-serve /module_scores, analytics) ────────
#
# The sidecar is not part of the served duckdb, so SQL on ``obsnum`` cannot see
# its scores. ant-serve exposes them without writing to the duckdb:
#
# * ``/databases/{id}/module_scores`` lists sidecar scores next to the obsnum
#   score columns (``storage: 'scores_sidecar'``) with per-module statistics
#   from :func:`sidecar_score_stats`;
# * the analytics sandbox and ``/query/sql`` get the TEMP view
#   ``score_sidecar`` over the parquet (:func:`score_view_statements`): long
#   format, one row per cell and score, joined to ``__cells`` for cell_name.
#   It is lazy (creating it reads nothing) and a query that filters on
#   source, catalog and module reads only the matching row groups
#   (milliseconds; G24/MMG holds 28 million rows).
#
# A wide view (one column per score, like obsnum) was tried and dropped: any
# query on it aggregates the whole sidecar, and on G24/MMG (319 scores x
# 86,612 cells) it ran out of the 2 GB sandbox memory or took minutes even
# for one column. Several scores side by side: conditional aggregation over
# score_sidecar (see score_views_schema's example_sql).

SCORE_SIDECAR_LONG_VIEW = 'score_sidecar'

_TRIPLE_CACHE: dict = {}
_STATS_CACHE: dict = {}
_READ_CACHE_LOCK = threading.Lock()
_STATS_PATH_LOCKS: dict = {}


def virtual_column_name(source: str, catalog: str, module: str) -> str:
    """Obs-column name a (source, catalog, module) score is served under.

    Same rule as ``AnthiveDB._score_col_name`` and the ``/info`` column list:
    gene set scores use the legacy ``Study::Experiment::module`` form, other
    sources ``<source>__<catalog>__<module>``.
    """
    if source == 'geneset':
        return f"{catalog.replace('/', '::')}::{module}"
    if source == 'starcat':
        return f"starcat__{catalog}__{module}"
    return f"{source}__{catalog}__{module}"


def _virtual_column_sql(alias: str) -> str:
    """SQL expression computing :func:`virtual_column_name` from sidecar columns."""
    return (
        f"CASE {alias}.source "
        f"WHEN 'geneset' THEN replace({alias}.catalog, '/', '::') || '::' || {alias}.module "
        f"WHEN 'starcat' THEN 'starcat__' || {alias}.catalog || '__' || {alias}.module "
        f"ELSE {alias}.source || '__' || {alias}.catalog || '__' || {alias}.module END"
    )


def sidecar_for_db_url(db_url: str) -> Optional[Path]:
    """Local sidecar of a served dataset, or None (missing sidecar, or an s3:// dataset).

    S3-backed datasets are skipped on purpose, like the score index in
    ant-serve: probing the bucket on every request is too slow.
    """
    location = str(db_url or '')
    if location.startswith('s3://') or not location.endswith('.duckdb'):
        return None
    if location.startswith('file://'):
        location = location[len('file://'):]
    sidecar = scores_path(Path(location))
    return sidecar if sidecar.exists() else None


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def sidecar_triples(sidecar: Path) -> list:
    """Distinct (source, catalog, module) rows of a sidecar, cached on its mtime.

    Returns an empty list when the file is missing or unreadable.
    """
    sidecar_key = str(sidecar)
    try:
        mtime = os.path.getmtime(sidecar_key)
    except OSError:
        return []
    with _READ_CACHE_LOCK:
        cached = _TRIPLE_CACHE.get(sidecar_key)
    if cached and cached[0] == mtime:
        return cached[1]
    conn = duckdb.connect(':memory:')
    try:
        triples = [
            (str(source), str(catalog), str(module))
            for source, catalog, module in conn.execute(
                "SELECT DISTINCT source, catalog, module FROM read_parquet(?) "
                "ORDER BY source, catalog, module", [sidecar_key]).fetchall()
        ]
    except duckdb.Error:
        return []
    finally:
        conn.close()
    with _READ_CACHE_LOCK:
        _TRIPLE_CACHE[sidecar_key] = (mtime, triples)
    return triples


def sidecar_score_stats(sidecar: Path) -> dict:
    """Per-score statistics of a sidecar: {(source, catalog, module): stats}.

    ``stats`` is ``{n, min, max, mean, std, median}`` over the cells that have
    a score (``n`` counts non-null scores; cells absent from the sidecar are
    not counted). One grouped scan of the whole file, cached on its mtime;
    concurrent first requests for one file wait for a single scan.
    """
    sidecar_key = str(sidecar)
    try:
        mtime = os.path.getmtime(sidecar_key)
    except OSError:
        return {}
    with _READ_CACHE_LOCK:
        cached = _STATS_CACHE.get(sidecar_key)
        path_lock = _STATS_PATH_LOCKS.setdefault(sidecar_key, threading.Lock())
    if cached and cached[0] == mtime:
        return cached[1]
    with path_lock:
        with _READ_CACHE_LOCK:
            cached = _STATS_CACHE.get(sidecar_key)
        if cached and cached[0] == mtime:
            return cached[1]
        conn = duckdb.connect(':memory:')
        try:
            rows = conn.execute(
                "SELECT source, catalog, module, COUNT(score), MIN(score), MAX(score), "
                "AVG(score), STDDEV(score), MEDIAN(score) "
                "FROM read_parquet(?) GROUP BY source, catalog, module",
                [sidecar_key]).fetchall()
        finally:
            conn.close()

        def _number(value):
            return float(value) if value is not None else None

        stats = {
            (str(source), str(catalog), str(module)): {
                'n': int(count), 'min': _number(low), 'max': _number(high),
                'mean': _number(mean), 'std': _number(std), 'median': _number(median),
            }
            for source, catalog, module, count, low, high, mean, std, median in rows
        }
        with _READ_CACHE_LOCK:
            _STATS_CACHE[sidecar_key] = (mtime, stats)
        return stats


def score_view_statements(sidecar: Path, database_name: str = 'db') -> list:
    """CREATE TEMP VIEW statement exposing a sidecar to hand-written SQL.

    Args:
        sidecar: The dataset's ``.scores.parquet``.
        database_name: Catalog the dataset's duckdb is attached under (for ``__cells``).

    Returns:
        ``[(view_name, sql)]`` for ``score_sidecar`` (cell_id, cell_name,
        source, catalog, module, column_name, score). ``column_name`` is
        :func:`virtual_column_name`, the obs-column name the plot endpoints
        and /module_scores use.
    """
    path_sql = _sql_literal(str(sidecar))
    cells = f"{_sql_identifier(database_name)}.main.__cells"
    long_sql = (
        f"CREATE OR REPLACE TEMP VIEW {SCORE_SIDECAR_LONG_VIEW} AS "
        f"SELECT s.cell_id, c.cell_name, s.source, s.catalog, s.module, "
        f"{_virtual_column_sql('s')} AS column_name, s.score "
        f"FROM read_parquet({path_sql}) s JOIN {cells} c ON c.cell_id = s.cell_id"
    )
    return [(SCORE_SIDECAR_LONG_VIEW, long_sql)]


def score_query_sql(source: str, catalog: str, module: str) -> str:
    """SELECT reading one sidecar score per cell through the ``score_sidecar`` view."""
    return (
        f"SELECT cell_name, score AS {_sql_identifier(virtual_column_name(source, catalog, module))} "
        f"FROM {SCORE_SIDECAR_LONG_VIEW} WHERE source = {_sql_literal(source)} "
        f"AND catalog = {_sql_literal(catalog)} AND module = {_sql_literal(module)}"
    )


def install_score_views(conn: duckdb.DuckDBPyConnection, db_url: str, database_name: str = 'db') -> list:
    """Create the sidecar TEMP views on ``conn`` (best effort). Returns the names installed.

    ``conn`` must be allowed to read the sidecar (the analytics sandbox allows
    ``<stem>.*.parquet``; pooled connections allow the data folder).
    """
    sidecar = sidecar_for_db_url(db_url)
    if sidecar is None:
        return []
    installed = []
    for view_name, create_sql in score_view_statements(sidecar, database_name):
        try:
            conn.execute(create_sql)
            installed.append(view_name)
        except duckdb.Error as exc:
            logging.getLogger('anthive.scores_parquet').warning(
                'score view %s not installed for %s: %s', view_name, db_url, exc)
    return installed


def score_views_schema(db_url: str, group_column: Optional[str] = None) -> dict:
    """Describe the ``score_sidecar`` view for the analytics schema endpoint.

    Reads only the cached list of scores, never the scores themselves.
    Returns ``{'available': False, 'reason': ...}`` without a local sidecar.
    ``group_column`` (an obscat column) is used in the grouped example query;
    without it that example is left out.
    """
    location = str(db_url or '')
    if location.startswith('s3://'):
        return {'available': False,
                'reason': 'dataset is read from object storage; its scores sidecar is not exposed to SQL'}
    sidecar = sidecar_for_db_url(location)
    if sidecar is None:
        return {'available': False, 'reason': 'this dataset has no scores sidecar'}
    triples = sidecar_triples(sidecar)
    by_source: dict = {}
    for source, _catalog, _module in triples:
        by_source[source] = by_source.get(source, 0) + 1
    example_sql = []
    first = next((t for t in triples if t[0] == 'geneset'), None) or (triples[0] if triples else None)
    if first is not None:
        source, catalog, module = first
        example_sql.append(score_query_sql(source, catalog, module))
        if group_column:
            example_sql.append(
                f"SELECT o.{_sql_identifier(group_column)}, AVG(s.score) FROM {SCORE_SIDECAR_LONG_VIEW} s "
                f"JOIN obscat o USING (cell_name) WHERE s.source = {_sql_literal(source)} "
                f"AND s.catalog = {_sql_literal(catalog)} AND s.module = {_sql_literal(module)} GROUP BY 1")
        same_catalog = [t for t in triples if t[0] == source and t[1] == catalog][:3]
        columns = ', '.join(
            f"MAX(score) FILTER (WHERE module = {_sql_literal(t[2])}) AS {_sql_identifier(virtual_column_name(*t))}"
            for t in same_catalog)
        example_sql.append(
            f"SELECT cell_name, {columns} FROM {SCORE_SIDECAR_LONG_VIEW} "
            f"WHERE source = {_sql_literal(source)} AND catalog = {_sql_literal(catalog)} GROUP BY cell_name")
    return {
        'available': True,
        'view': SCORE_SIDECAR_LONG_VIEW,
        'note': ('Module scores of this dataset are kept in its scores sidecar, not in obsnum. Query them '
                 f'through the view {SCORE_SIDECAR_LONG_VIEW} (one row per cell and score); always filter on '
                 'source, catalog and module (fast), join other tables on cell_name. column_name is the '
                 'name /module_scores and the plot endpoints use. Several scores side by side: '
                 'MAX(score) FILTER (WHERE module = ...) per score, GROUP BY cell_name.'),
        'columns': [{'name': name, 'type': kind} for name, kind in (
            ('cell_id', 'BIGINT'), ('cell_name', 'VARCHAR'), ('source', 'VARCHAR'), ('catalog', 'VARCHAR'),
            ('module', 'VARCHAR'), ('column_name', 'VARCHAR'), ('score', 'DOUBLE'))],
        'n_scores': len(triples),
        'n_scores_by_source': by_source,
        'example_sql': example_sql,
    }
