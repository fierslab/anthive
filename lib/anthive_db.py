"""
Database access layer for Anthive DuckDB files

Provides a clean interface to query DuckDB databases created by ant-convert
"""

import copy
import fcntl
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Literal
import duckdb
import numpy as np
import pandas as pd


# DuckDB identifiers we interpolate via f-strings have to be validated and
# quoted. The allowed character set covers every obs / var / layer column we
# have actually seen in the h5adstore (parentheses, spaces, hyphens, dots,
# colons — see e.g. "Antigen-presenting response (HLA)_microtype"), while
# excluding quote characters and any statement-terminator chars.
#
# Note that quoting is what makes this safe — DuckDB treats `"FOO BAR"` as
# one identifier name, never as SQL keywords. The regex just keeps quote
# characters out so the quote/escape can't be tricked.
_SAFE_IDENT_RE = re.compile(r'^[A-Za-z0-9_:.\-(), ]+$')


def safe_ident(name: str) -> str:
    """Return a double-quoted SQL identifier, validated against a strict regex.

    Raises ValueError if `name` contains any character outside the allowed
    set. Use at every f-string interpolation site that places `name` in
    identifier position — never bypass.
    """
    if not isinstance(name, str) or not _SAFE_IDENT_RE.fullmatch(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _q(name: str) -> str:
    """Permissive double-quote ident wrap for TRUSTED identifiers (names
    that come from information_schema or the caller has already validated
    against the duckdb schema). Legacy stored col names include ``#`` /
    spaces / ``.`` / etc. — ``safe_ident``'s strict regex refuses them,
    which is right for user-supplied strings but wrong when the string
    IS the stored name. Never use with user-supplied strings — those go
    through ``safe_ident``."""
    return '"' + str(name).replace('"', '""') + '"'


def build_obs_view_sql(
    view_name: str,
    underscore_table: str,
    columns: List[str],
    api_config: Optional[Dict[str, Any]] = None,
    sibling_underscore_table: Optional[str] = None,
    sibling_columns: Optional[List[str]] = None,
) -> str:
    """Return the ``CREATE VIEW`` SQL for the obscat/obsnum view.

    Applies the ``api_config`` rename+hide+derive to the column projection
    so the resulting view exposes only public names — internal renamed
    names are unreachable from any consumer that reads the view.
    ``cell_id`` is dropped (it's the join key, not a public col);
    ``cell_name`` is added.

    ``obs_derive`` publishes new columns via a value-map CASE expression.
    A derivation whose ``dtype`` matches this view is emitted here.
    If the derivation's ``source`` column lives in the sibling __obs*
    table (obsnum-sourced obscat derivation, or vice versa), pass the
    sibling table + column list so we can JOIN it in.

    Shared by ant-convert (initial view build) and AnnotationWriter
    (view rebuild after ALTER TABLE ADD COLUMN).
    """
    api = api_config or {}
    rename = api.get('obs_rename', {}) or {}
    hidden = set(api.get('obs_hidden', []) or [])
    value_rename = api.get('obs_value_rename', {}) or {}
    value_hidden = api.get('obs_value_hidden', {}) or {}
    derive_all = api.get('obs_derive', {}) or {}
    constant_all = api.get('obs_constant', {}) or {}

    # Match this view's dtype to the derivations that target it.
    view_dtype = 'categorical' if view_name.endswith('obscat') else 'numerical'
    own_cols = set(columns)
    sibling_cols = set(sibling_columns or [])
    active_derive = {}
    join_sibling = False
    for new_col, spec in derive_all.items():
        if spec.get('dtype') != view_dtype:
            continue
        source = spec.get('source')
        if source in own_cols:
            spec = {**spec, '_source_table': 'o'}     # this view's alias
        elif source in sibling_cols and sibling_underscore_table:
            spec = {**spec, '_source_table': 's'}     # sibling alias
            join_sibling = True
        else:
            # Source column not found anywhere — silently skip (source
            # may be an obs col not in either projection table yet).
            continue
        active_derive[new_col] = spec

    # Legacy stored col names include spaces / '#' / '.' etc. — see _q()
    # module-level docstring for the permissive-quote rationale. Public
    # rename targets still go through safe_ident (strict) so a malicious
    # .md can't inject SQL via a crafted alias.
    def _sql_str(v: Any) -> str:
        """Escape a Python value for a SQL string literal."""
        return "'" + str(v).replace("'", "''") + "'"

    def _projection_for(col: str) -> str:
        """Build the SELECT-list projection for a single stored column.

        Adds value-level rename+hide via a CASE expression when the col is
        configured in ``obs_value_rename`` / ``obs_value_hidden``. Ordering
        inside the CASE: hidden values → NULL FIRST, then rename branches,
        else passthrough. That way a value that's both hidden and renamed
        stays NULL (hide wins).
        """
        col_sql = f'o.{_q(col)}'
        alias = rename.get(col, col)
        # Public rename targets go through the strict safe_ident (defense
        # against SQL injection via a crafted .md). The default alias
        # (=stored col name from information_schema) is trusted and can
        # contain legacy characters like '#' or spaces — quote permissively.
        alias_sql = safe_ident(alias) if col in rename else _q(alias)
        vhide = list(value_hidden.get(col, []) or [])
        vren = dict(value_rename.get(col, {}) or {})

        if not vhide and not vren:
            # Plain projection — same as pre-phase-2 behavior.
            return f'{col_sql} AS {alias_sql}'

        branches = []
        if vhide:
            in_list = ', '.join(_sql_str(v) for v in vhide)
            branches.append(f'WHEN {col_sql} IN ({in_list}) THEN NULL')
        for old_val, new_val in vren.items():
            branches.append(
                f'WHEN {col_sql} = {_sql_str(old_val)} '
                f'THEN {"NULL" if new_val is None else _sql_str(new_val)}'
            )
        case_expr = f'CASE {" ".join(branches)} ELSE {col_sql} END'
        return f'{case_expr} AS {alias_sql}'

    projections = ['c.cell_name']
    for col in columns:
        if col == 'cell_id':
            continue
        if col in hidden:
            continue
        projections.append(_projection_for(col))

    # obs_constant projections — one literal per active constant column.
    # No source col, no CASE, just SELECT '<value>' AS <alias>. Used to
    # broadcast a dataset-level annotation (typically a mus.cell_type
    # scalar) into a per-cell facet-visible column.
    for new_col, spec in constant_all.items():
        if spec.get('dtype') != view_dtype:
            continue
        raw_value = spec.get('value')
        if view_dtype == 'numerical':
            value_sql = 'NULL' if raw_value is None else f'{float(raw_value)}'
        else:
            value_sql = 'NULL' if raw_value is None else _sql_str(str(raw_value))
        projections.append(f"{value_sql} AS {safe_ident(new_col)}")

    # obs_derive projections — one CASE expression per active derivation.
    # The alias is validated by safe_ident (strict) since it's a
    # user-supplied string that lands in the public schema.
    for new_col, spec in active_derive.items():
        src_table_alias = spec['_source_table']
        source = spec['source']
        col_sql = f"{src_table_alias}.{_q(source)}"
        mapping = spec.get('map', {}) or {}
        default = spec.get('default')
        dtype = spec.get('dtype')

        def _lit(val: Any) -> str:
            if val is None:
                return 'NULL'
            if dtype == 'numerical':
                return f'{float(val)}'
            return "'" + str(val).replace("'", "''") + "'"

        branches = []
        for old_val, new_val in mapping.items():
            branches.append(
                f"WHEN {col_sql} = {_sql_str(old_val)} THEN {_lit(new_val)}"
            )
        case_expr = (
            f"CASE {' '.join(branches)} ELSE {_lit(default)} END"
        )
        projections.append(f"{case_expr} AS {safe_ident(new_col)}")

    join_sql = f"JOIN __cells c ON o.cell_id = c.cell_id"
    if join_sibling and sibling_underscore_table:
        join_sql += (
            f" LEFT JOIN {_q(sibling_underscore_table)} s "
            f"ON o.cell_id = s.cell_id"
        )
    return (
        f"CREATE VIEW {safe_ident(view_name)} AS "
        f"SELECT {', '.join(projections)} "
        f"FROM {_q(underscore_table)} o "
        f"{join_sql}"
    )


# Process-wide obs_summary memo. Keyed on the file signatures of the duckdb,
# its WAL and the scores sidecar, so any write (ALTER TABLE, view refresh,
# re-ingest, new scores) produces a new key and the stale entry ages out.
_OBS_SUMMARY_MEMO: "OrderedDict[tuple, Dict[str, Dict[str, Any]]]" = OrderedDict()
_OBS_SUMMARY_MEMO_MAX_ENTRIES = 256
_OBS_SUMMARY_MEMO_LOCK = threading.Lock()

_NUMERIC_TYPE_PREFIXES = (
    'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT',
    'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT',
    'FLOAT', 'DOUBLE', 'REAL', 'DECIMAL', 'NUMERIC',
)

_OBS_SUMMARY_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
    """(mtime_ns, size) of a file, or None when it does not exist."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _is_numeric_type(data_type: str) -> bool:
    return str(data_type).upper().startswith(_NUMERIC_TYPE_PREFIXES)


def _numeric_filter_value(field: str, value: Any) -> float:
    """Coerce a filter value for a numerical column, or raise ValueError."""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Filter value {value!r} for numerical field {field!r} is not a number"
        ) from None


def _categorical_filter_values(values: List[Any]) -> List[str]:
    """Text forms to match for categorical filter values.

    Callers (e.g. /cells) turn "3" into 3.0 before we see it; an integral
    float therefore matches both "3" and "3.0" so the stored label is found
    whichever way it was written.
    """
    text_values: List[str] = []
    for value in values:
        if isinstance(value, float) and value.is_integer():
            candidates = [str(int(value)), str(value)]
        else:
            candidates = [str(value)]
        for candidate in candidates:
            if candidate not in text_values:
                text_values.append(candidate)
    return text_values


def clear_obs_summary_memo() -> None:
    """Drop every memoised obs_summary result (tests, admin rescans)."""
    with _OBS_SUMMARY_MEMO_LOCK:
        _OBS_SUMMARY_MEMO.clear()


def _load_api_config_for(duckdb_path: Path) -> Dict[str, Any]:
    """Load the api: section from the sibling .md (same stem as the duckdb).

    Best-effort: any error (missing file, YAML issue, validation failure) is
    swallowed and returns the empty-config dict, since view builds must not
    crash on a malformed .md. Callers wanting to validate an .md's api
    section should use ``metadata.get_api_config`` directly.
    """
    empty = {'obs_rename': {}, 'obs_hidden': []}
    try:
        # Local import to avoid a top-level cycle.
        from metadata import parse_md_file, get_api_config  # noqa: PLC0415
        md_path = Path(duckdb_path).with_suffix('.md')
        if not md_path.exists():
            return empty
        yaml_dict, _ = parse_md_file(md_path)
        return get_api_config(yaml_dict)
    except Exception:
        return empty


class AnthiveDB:
    """Interface to a single Anthive DuckDB database"""

    def __init__(self, db_path: Optional[Path] = None, conn=None, owns_conn: bool = True):
        """
        Initialize database connection.

        Args:
            db_path: Path to .duckdb file (used when conn is None)
            conn: Existing DuckDB connection to use (skips opening a new one)
            owns_conn: If False, close() will not close the connection (for pooled use)
        """
        self.db_path = db_path
        self.conn: Optional[duckdb.DuckDBPyConnection] = conn
        self._owns_conn = owns_conn
        # Loaded lazily on demand — currently only used when _refresh_view is
        # called from an r/w path (via AnnotationWriter). Read-only AnthiveDB
        # never rebuilds views, so this stays at the default empty config.
        self.api_config: Dict[str, Any] = {'obs_rename': {}, 'obs_hidden': []}

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Open database connection"""
        if self.conn is None:
            self.conn = duckdb.connect(str(self.db_path), read_only=True)
        return self.conn

    @property
    def _conn(self) -> duckdb.DuckDBPyConnection:
        """Return open connection, connecting lazily if needed."""
        return self.connect()

    def close(self):
        """Close database connection"""
        if self.conn and self._owns_conn:
            self._conn.close()
            self.conn = None

    def get_info(self) -> Dict[str, Any]:
        """
        Get database information

        Returns:
            Dictionary with database metadata
        """

        # Get cell and gene counts
        n_cells = (self._conn.execute("SELECT COUNT(*) FROM __cells").fetchone() or (0,))[0]
        n_genes = (self._conn.execute("SELECT COUNT(*) FROM __genes").fetchone() or (0,))[0]

        # Get available layers
        layers = self._get_layers()

        # Get available embeddings
        embeddings = self._get_embeddings()

        # Get metadata fields
        numerical_fields = self._get_numerical_fields()
        categorical_fields = self._get_categorical_fields()

        return {
            'n_cells': n_cells,
            'n_genes': n_genes,
            'layers': layers,
            'embeddings': embeddings,
            'metadata_fields': {
                'numerical': numerical_fields,
                'categorical': categorical_fields
            }
        }

    def _get_layers(self) -> List[str]:
        """Get list of available layers"""

        # Get all views that match pattern layer__*
        result = self._conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_type = 'VIEW'
            AND table_name LIKE 'layer__%'
        """).fetchall()

        layers = [row[0].replace('layer__', '') for row in result]
        return sorted(layers)

    def _get_embeddings(self) -> List[str]:
        """Get list of available embeddings"""

        # Get all views that match pattern obsm__*
        result = self._conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_type = 'VIEW'
            AND table_name LIKE 'obsm__%'
        """).fetchall()

        embeddings = [row[0].replace('obsm__', '') for row in result]
        return sorted(embeddings)

    # -------- score sidecar (external <basename>.scores.parquet) --------
    #
    # The sidecar holds long-format per-cell module scores that used to be
    # ALTER-TABLE'd into __obsnum. See lib/scores_parquet.py and
    # bin/annot/ant-score-ext / ant-starcat-ext for the write path.
    #
    # From ant-serve's perspective the sidecar is transparent: score rows
    # appear as virtual obsnum columns with names built by _score_col_name
    # (matching the legacy `Sierksma2025::WGCNA::<mod>` /
    # `starcat__MICROGLIA_V1__<prog>` convention). Cache invalidation
    # piggybacks on the sidecar's mtime.

    @staticmethod
    def _score_col_name(source: str, catalog: str, module: str) -> str:
        """Virtual obs-column name for a (source, catalog, module) triple."""
        if source == 'geneset':
            return f"{catalog.replace('/', '::')}::{module}"
        if source == 'starcat':
            return f"starcat__{catalog}__{module}"
        return f"{source}__{catalog}__{module}"

    def _scores_sidecar_path(self) -> Optional[Path]:
        """Return the sibling `.scores.parquet` if it exists, else None."""
        if self.db_path is None:
            return None
        p = Path(self.db_path)
        if not p.name.endswith('.duckdb'):
            return None
        sc = p.with_name(p.name[: -len('.duckdb')] + '.scores.parquet')
        return sc if sc.exists() else None

    def _load_score_index(self) -> Dict[str, Tuple[str, str, str]]:
        """Return {virtual_col_name: (source, catalog, module)}.

        Cached on the AnthiveDB instance (pooled connections reuse it).
        Cache key = sidecar mtime; on staleness we rebuild.
        """
        sidecar = self._scores_sidecar_path()
        if sidecar is None:
            self._score_index_mtime = None
            self._score_index_cache = {}
            return self._score_index_cache

        mtime = sidecar.stat().st_mtime
        prev_mtime = getattr(self, '_score_index_mtime', None)
        if prev_mtime == mtime and hasattr(self, '_score_index_cache'):
            return self._score_index_cache

        rows = self._conn.execute(
            "SELECT DISTINCT source, catalog, module FROM read_parquet(?) "
            "ORDER BY source, catalog, module",
            [str(sidecar)]
        ).fetchall()
        index = {
            self._score_col_name(source, catalog, module): (source, catalog, module)
            for source, catalog, module in rows
        }
        self._score_index_cache = index
        self._score_index_mtime = mtime
        return index

    def _get_score_fields(self) -> List[str]:
        """Sorted list of virtual score column names (empty if no sidecar)."""
        return sorted(self._load_score_index().keys())

    def _fetch_score_values(self, virtual_col: str) -> pd.DataFrame:
        """Return a DataFrame [cell_name, <virtual_col>] for one score column.

        Cells with no row in the sidecar for this triple get NaN.
        """
        idx = self._load_score_index()
        if virtual_col not in idx:
            raise KeyError(f"unknown score column: {virtual_col!r}")
        source, catalog, module = idx[virtual_col]
        sidecar = self._scores_sidecar_path()
        # Read only the matching triple's rows, join to __cells for the name.
        df = self._conn.execute(
            """
            SELECT c.cell_name, s.score AS "{col}"
            FROM __cells c
            LEFT JOIN (
                SELECT cell_id, score
                FROM read_parquet(?)
                WHERE source = ? AND catalog = ? AND module = ?
            ) s ON s.cell_id = c.cell_id
            ORDER BY c.cell_id
            """.format(col=virtual_col.replace('"', '""')),
            [str(sidecar), source, catalog, module],
        ).df()
        return df

    def _fetch_score_matrix(self, virtual_cols: List[str]) -> pd.DataFrame:
        """Return a DataFrame [cell_name, *virtual_cols] for many score columns.

        One read of the sidecar for all requested triples, scattered into a
        dense cells x columns array; replaces one parquet scan plus one
        pandas merge per column. Cells without a row get NaN. Rows are in
        __cells.cell_id order.
        """
        idx = self._load_score_index()
        unknown = [col for col in virtual_cols if col not in idx]
        if unknown:
            raise KeyError(f"unknown score column(s): {unknown!r}")
        sidecar = self._scores_sidecar_path()
        cells = self._conn.execute(
            "SELECT cell_id, cell_name FROM __cells ORDER BY cell_id"
        ).fetchnumpy()
        cell_ids = np.asarray(cells['cell_id'], dtype=np.int64)
        matrix = np.full((len(cell_ids), len(virtual_cols)), np.nan, dtype=np.float64)
        if virtual_cols and len(cell_ids):
            triple_values = ', '.join(['(?, ?, ?, ?)'] * len(virtual_cols))
            triple_params: List[Any] = []
            for col_position, col in enumerate(virtual_cols):
                triple_params.extend([col_position, *idx[col]])
            long_scores = self._conn.execute(
                f"""
                SELECT s.cell_id, t.col_position, s.score
                FROM read_parquet(?) s
                JOIN (VALUES {triple_values}) t(col_position, source, catalog, module)
                  ON s.source = t.source AND s.catalog = t.catalog AND s.module = t.module
                WHERE s.score IS NOT NULL
                """,
                [str(sidecar), *triple_params],
            ).fetchnumpy()
            score_cell_ids = np.asarray(long_scores['cell_id'], dtype=np.int64)
            row_positions = np.searchsorted(cell_ids, score_cell_ids)
            in_range = row_positions < len(cell_ids)
            matched = np.zeros(len(score_cell_ids), dtype=bool)
            matched[in_range] = cell_ids[row_positions[in_range]] == score_cell_ids[in_range]
            matrix[row_positions[matched],
                   np.asarray(long_scores['col_position'], dtype=np.int64)[matched]] = (
                np.asarray(long_scores['score'], dtype=np.float64)[matched]
            )
        frame = pd.DataFrame(matrix, columns=list(virtual_cols))
        frame.insert(0, 'cell_name', cells['cell_name'])
        return frame

    def _get_numerical_fields(self) -> List[str]:
        """Get numerical metadata fields (union of duckdb obsnum + parquet scores)."""

        # Check if obsnum view exists
        exists = (self._conn.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'obsnum'
        """).fetchone() or (0,))[0]

        duckdb_cols: List[str] = []
        if exists:
            result = self._conn.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'obsnum'
                AND column_name NOT IN ('cell_name', 'cell_id')
            """).fetchall()
            duckdb_cols = [row[0] for row in result]

        # Score-sidecar virtual columns get merged in — they behave exactly
        # like obsnum columns from the caller's perspective. Order: duckdb
        # native first (backwards-compatible), then score fields.
        score_fields = self._get_score_fields()
        seen = set(duckdb_cols)
        merged = list(duckdb_cols)
        for name in score_fields:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        return merged

    def _get_categorical_fields(self) -> List[str]:
        """Get categorical metadata fields"""

        # Check if obscat view exists
        exists = (self._conn.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'obscat'
        """).fetchone() or (0,))[0]

        if not exists:
            return []

        # Get columns except cell_name and cell_id
        result = self._conn.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'obscat'
            AND column_name NOT IN ('cell_name', 'cell_id')
        """).fetchall()

        return [row[0] for row in result]

    def _obs_summary_memo_key(self, top_n_categories: int) -> Optional[tuple]:
        """Memo key for obs_summary, or None when the dataset is not a local file."""
        if self.db_path is None:
            return None
        db_path = Path(self.db_path)
        db_signature = _file_signature(db_path)
        if db_signature is None:
            return None
        wal_signature = _file_signature(db_path.with_name(db_path.name + '.wal'))
        sidecar = self._scores_sidecar_path()
        sidecar_signature = _file_signature(sidecar) if sidecar is not None else None
        return (
            str(db_path.resolve()), db_signature, wal_signature,
            sidecar_signature, int(top_n_categories),
        )

    def obs_summary(self, top_n_categories: int = 3) -> Dict[str, Dict[str, Any]]:
        """Per-obs-column summary stats for the dataset-page right-column.

        Returns a dict keyed on column name:
          - categorical: {'type': 'cat', 'n_unique', 'pct_missing',
                          'top': [{value, count, pct}, ...]}
          - numerical:   {'type': 'num', 'min', 'q05', 'q25', 'mean',
                          'median', 'q75', 'q95', 'max', 'pct_missing'}

        ``n_unique`` is HyperLogLog-approximate (as DuckDB SUMMARIZE's
        ``approx_unique``); every quantile, the median included, is exact.
        Missing values are reported as ``pct_missing`` (0-100). Empty when
        neither obscat nor obsnum exists in this dataset.

        Results are memoised process-wide on the duckdb / WAL / scores-sidecar
        file signatures, so repeat calls are free and any write invalidates.
        """
        memo_key = self._obs_summary_memo_key(top_n_categories)
        if memo_key is not None:
            with _OBS_SUMMARY_MEMO_LOCK:
                cached = _OBS_SUMMARY_MEMO.get(memo_key)
                if cached is not None:
                    _OBS_SUMMARY_MEMO.move_to_end(memo_key)
                    return copy.deepcopy(cached)

        summary = self._compute_obs_summary(top_n_categories)

        if memo_key is not None:
            with _OBS_SUMMARY_MEMO_LOCK:
                _OBS_SUMMARY_MEMO[memo_key] = copy.deepcopy(summary)
                _OBS_SUMMARY_MEMO.move_to_end(memo_key)
                while len(_OBS_SUMMARY_MEMO) > _OBS_SUMMARY_MEMO_MAX_ENTRIES:
                    _OBS_SUMMARY_MEMO.popitem(last=False)
        return summary

    def _compute_obs_summary(self, top_n_categories: int) -> Dict[str, Dict[str, Any]]:
        """Uncached obs_summary: one aggregate scan per view, one top-N scan
        for all categorical columns, one grouped scan of the scores sidecar."""
        out: Dict[str, Dict[str, Any]] = {}
        skip_cols = {'cell_name', 'cell_id'}

        def _coerce_num(value):
            if value is None:
                return None
            try:
                return float(value)
            except (ValueError, TypeError):
                return None

        def _pct_missing(total: int, non_null: int) -> float:
            return round(100.0 * (total - non_null) / total, 2) if total else 0.0

        for view_name, kind in (('obscat', 'cat'), ('obsnum', 'num')):
            column_rows = self._conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [view_name],
            ).fetchall()
            columns = [(name, dtype) for name, dtype in column_rows if name not in skip_cols]
            if not columns:
                continue

            # Aggregate over the view (it carries the api rename / hide /
            # value-rename / derive projections), minus cell_name so the
            # string min/max/distinct work SUMMARIZE used to do on it is gone.
            has_cell_name = any(name == 'cell_name' for name, _ in column_rows)
            source_sql = (
                f"(SELECT * EXCLUDE (cell_name) FROM {safe_ident(view_name)})"
                if has_cell_name else safe_ident(view_name)
            )
            aggregate_exprs = ['COUNT(*)']
            for name, dtype in columns:
                ident = _q(name)   # trusted: from information_schema
                if kind == 'cat':
                    aggregate_exprs += [f'approx_count_distinct({ident})', f'COUNT({ident})']
                elif _is_numeric_type(dtype):
                    quantile_list = ', '.join(str(p) for p in _OBS_SUMMARY_QUANTILES)
                    aggregate_exprs += [
                        f'MIN({ident})', f'MAX({ident})', f'AVG({ident})',
                        f'quantile_cont({ident}, [{quantile_list}])', f'COUNT({ident})',
                    ]
                else:
                    # Non-numeric column in obsnum (e.g. BOOLEAN): min/max as
                    # text like SUMMARIZE reported them, no mean / quantiles.
                    aggregate_exprs += [
                        f'CAST(MIN({ident}) AS VARCHAR)', f'CAST(MAX({ident}) AS VARCHAR)',
                        'NULL', 'NULL', f'COUNT({ident})',
                    ]
            try:
                aggregate_row = self._conn.execute(
                    f"SELECT {', '.join(aggregate_exprs)} FROM {source_sql}"
                ).fetchone()
            except duckdb.Error:
                continue
            total_rows = int(aggregate_row[0] or 0)

            position = 1
            for name, _dtype in columns:
                if kind == 'cat':
                    n_unique, non_null = aggregate_row[position:position + 2]
                    position += 2
                    out[name] = {
                        'type': 'cat',
                        'n_unique': int(n_unique) if n_unique is not None else 0,
                        'pct_missing': _pct_missing(total_rows, int(non_null or 0)),
                        'non_null': int(non_null or 0),
                        'top': [],
                    }
                else:
                    min_value, max_value, mean_value, quantiles, non_null = (
                        aggregate_row[position:position + 5]
                    )
                    position += 5
                    q05 = q25 = median = q75 = q95 = None
                    if quantiles is not None:
                        q05, q25, median, q75, q95 = (_coerce_num(v) for v in quantiles)
                    out[name] = {
                        'type': 'num',
                        'min': _coerce_num(min_value),
                        'max': _coerce_num(max_value),
                        'median': median,
                        'mean': _coerce_num(mean_value),
                        'q05': q05, 'q25': q25, 'q75': q75, 'q95': q95,
                        'pct_missing': _pct_missing(total_rows, int(non_null or 0)),
                    }

            # Top-N values for every categorical column in ONE scan: unpivot
            # to (col, value) (UNPIVOT drops NULLs), count, keep the N most
            # frequent per column. Ties break on value for determinism.
            if kind == 'cat' and top_n_categories > 0:
                cast_list = ', '.join(
                    f'CAST({_q(name)} AS VARCHAR) AS {_q(name)}' for name, _ in columns
                )
                top_sql = (
                    f"WITH long_values AS ("
                    f"  UNPIVOT (SELECT {cast_list} FROM {source_sql}) "
                    f"  ON COLUMNS(*) INTO NAME col_name VALUE col_value"
                    f"), value_counts AS ("
                    f"  SELECT col_name, col_value, COUNT(*) AS n "
                    f"  FROM long_values GROUP BY col_name, col_value"
                    f") "
                    f"SELECT col_name, col_value, n FROM value_counts "
                    f"QUALIFY row_number() OVER ("
                    f"  PARTITION BY col_name ORDER BY n DESC, col_value) <= ? "
                    f"ORDER BY col_name, n DESC, col_value"
                )
                try:
                    top_rows = self._conn.execute(top_sql, [int(top_n_categories)]).fetchall()
                except duckdb.Error:
                    top_rows = []
                for col_name, col_value, count in top_rows:
                    entry = out.get(col_name)
                    if entry is None or entry['type'] != 'cat':
                        continue
                    non_missing = entry['non_null']
                    entry['top'].append({
                        'value': str(col_value) if col_value is not None else None,
                        'count': int(count),
                        'pct': round(100.0 * count / non_missing, 2) if non_missing else 0.0,
                    })
            for name, _ in columns:
                out.get(name, {}).pop('non_null', None)

        # Score-sidecar columns as numerical stats: one grouped scan of the
        # long-format parquet (cell_id, source, catalog, module, score).
        sidecar = self._scores_sidecar_path()
        if sidecar is not None:
            try:
                score_index = self._load_score_index()
                if score_index:
                    quantile_list = ', '.join(str(p) for p in _OBS_SUMMARY_QUANTILES)
                    triple_rows = self._conn.execute(f"""
                        SELECT source, catalog, module,
                               MIN(score), MAX(score), AVG(score),
                               quantile_cont(score, [{quantile_list}]),
                               COUNT(*), COUNT(*) - COUNT(score)
                        FROM read_parquet(?)
                        GROUP BY source, catalog, module
                        ORDER BY source, catalog, module
                    """, [str(sidecar)]).fetchall()
                    reverse_index = {v: k for k, v in score_index.items()}
                    for (source, catalog, module, min_value, max_value, mean_value,
                         quantiles, n_rows, n_null) in triple_rows:
                        col_name = reverse_index.get((source, catalog, module))
                        if not col_name or col_name in out:
                            continue
                        q05 = q25 = median = q75 = q95 = None
                        if quantiles is not None:
                            q05, q25, median, q75, q95 = (_coerce_num(v) for v in quantiles)
                        out[col_name] = {
                            'type': 'num',
                            'min': _coerce_num(min_value),
                            'max': _coerce_num(max_value),
                            'mean': _coerce_num(mean_value),
                            'median': median,
                            'q05': q05, 'q25': q25, 'q75': q75, 'q95': q95,
                            'pct_missing': round(100.0 * (n_null or 0) / n_rows, 2) if n_rows else 0.0,
                        }
            except duckdb.Error:
                pass   # sidecar shape drift must not kill the endpoint

        return out

    def search_genes(self, query: str, case_sensitive: bool = False,
                    limit: Optional[int] = None, exact: bool = False) -> List[str]:
        """
        Search for genes by name.

        Args:
            query: Search string
            case_sensitive: Case-sensitive match (default: False)
            limit: Maximum results
            exact: If True, match full gene name only; if False (default), substring match

        Returns:
            List of matching gene names
        """

        if exact:
            where_clause = "WHERE LOWER(gene_name) = LOWER(?)" if not case_sensitive else "WHERE gene_name = ?"
            param = query
        else:
            where_clause = "WHERE gene_name ILIKE ?" if not case_sensitive else "WHERE gene_name LIKE ?"
            param = f"%{query}%"

        limit_clause = f"LIMIT {limit}" if limit else ""

        sql = f"""
            SELECT gene_name
            FROM genes
            {where_clause}
            ORDER BY gene_name
            {limit_clause}
        """

        result = self._conn.execute(sql, [param]).fetchall()
        return [row[0] for row in result]

    def get_gene_info(self, gene_name: str) -> Dict[str, Any]:
        """
        Get information about a specific gene

        Args:
            gene_name: Gene name

        Returns:
            Dictionary with gene information

        Raises:
            ValueError: If gene not found
        """

        # Check if gene exists
        result = self._conn.execute("""
            SELECT gene_name
            FROM genes
            WHERE gene_name = ?
        """, [gene_name]).fetchone()

        if not result:
            raise ValueError(f"Gene '{gene_name}' not found")

        return {
            'gene_name': gene_name,
            'exists': True,
            'layers': self._get_layers()
        }

    def get_cell_table(self,
                       genes: Optional[List[str]] = None,
                       metadata_fields: Optional[List[str]] = None,
                       layer: str = 'X',
                       filters: Optional[Dict[str, Any]] = None,
                       limit: Optional[int] = None,
                       sample: Optional[int] = None) -> pd.DataFrame:
        """
        Get cell table with expression and metadata

        Args:
            genes: List of gene names to include
            metadata_fields: List of metadata fields, ["*"] for all, None for none
            layer: Layer name
            filters: Dictionary of field: value/range filters
            limit: Maximum number of cells
            sample: Random subsample size (absolute row count, system sampling).
                    Applied after filters. If larger than the result set, returns
                    all rows. Uses DuckDB USING SAMPLE (reservoir) for exact row count.

        Returns:
            DataFrame with cell data
        """

        # Build SELECT clause. All literal VALUES go into `params` and are
        # bound via DuckDB `?` placeholders — never f-string'd. Identifiers
        # (column names, layer table) go through `safe_ident()` and are
        # double-quoted; that's safe because the regex blocks every char
        # that would let an attacker break out of the identifier.
        select_parts = ["c.cell_name"]
        params: List[Any] = []

        # Add gene expression columns if requested. Resolve the layer source
        # from the duckdb's own directory so a duckdb pulled from S3 works on
        # any host (see _layer_source_with_names — do NOT read through the
        # persisted layer__X view; it bakes the migrating host's absolute
        # path into the SQL, breaking every intern refresh).
        if genes:
            layer_src = self._layer_source_with_names(layer)
            for gene in genes:
                gene_alias = safe_ident(gene)
                # COALESCE the scalar subquery to 0. The layer table is
                # sparse (only non-zero entries stored), so a cell absent
                # from that table means "expression == 0", NOT "missing
                # data". Dropping to NaN would (silently) filter zeros out
                # of every plot — biasing means, hiding the ~95% zero-mass
                # in violin/KDE, and producing "TREM2 by condition" means
                # that are actually mean_nonzero_by_condition. Zero-fill
                # here matches the scanpy / CellxGene convention.
                #
                # NOTE: only apply to gene columns. Obs columns MUST keep
                # NaN — a missing category is real missing data, not zero.
                select_parts.append(f"""
                    COALESCE(
                        (SELECT value
                         FROM {layer_src} _l
                         WHERE _l.cell_name = c.cell_name
                         AND _l.gene_name = ?),
                        0.0
                    ) AS {gene_alias}
                """)
                params.append(gene)

        # Add metadata columns if requested. Score fields (parquet sidecar)
        # are handled AFTER the SQL executes — they need a separate read.
        metadata_joins = []
        score_fields_to_merge: List[str] = []
        score_index = self._load_score_index()
        # num_fields returned by _get_numerical_fields already includes score
        # virtuals — separate them so we don't try to select them from obsnum.
        num_fields_all = self._get_numerical_fields()
        num_fields_real = [f for f in num_fields_all if f not in score_index]
        if metadata_fields:
            if metadata_fields == ["*"]:
                for field in num_fields_real:
                    select_parts.append(f'n.{_q(field)}')
                if num_fields_real:
                    metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")

                cat_fields = self._get_categorical_fields()
                for field in cat_fields:
                    select_parts.append(f'cat.{_q(field)}')
                if cat_fields:
                    metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

                # All score fields when the caller asks for "*"
                score_fields_to_merge = list(score_index.keys())
            else:
                cat_fields = self._get_categorical_fields()
                real_num_set = set(num_fields_real)
                cat_set = set(cat_fields)

                has_num = any(f in real_num_set for f in metadata_fields)
                has_cat = any(f in cat_set for f in metadata_fields)

                if has_num:
                    metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
                if has_cat:
                    metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

                for field in metadata_fields:
                    if field in real_num_set:
                        select_parts.append(f'n.{_q(field)}')
                    elif field in cat_set:
                        select_parts.append(f'cat.{_q(field)}')
                    elif field in score_index:
                        score_fields_to_merge.append(field)

        # Build WHERE clause from filters. Parameters are bound in SQL text
        # order: SELECT (gene subqueries) → JOIN (score sidecar) → WHERE.
        where_parts = []
        join_params: List[Any] = []
        where_params: List[Any] = []
        if filters:
            real_num_set = set(num_fields_real)
            cat_set = set(self._get_categorical_fields())
            filters = dict(filters)
            # cell_name restricts to an explicit cell list (the plot
            # endpoints pass the sampled embedding cells this way). Bound
            # as one LIST parameter so a 100k-cell sample is a hash semi-join.
            cell_name_filter = filters.pop('cell_name', None)
            if cell_name_filter is not None:
                cell_names = (list(cell_name_filter)
                              if isinstance(cell_name_filter, (list, tuple, set))
                              else [cell_name_filter])
                where_parts.append("c.cell_name IN (SELECT UNNEST(?::VARCHAR[]))")
                where_params.append([str(name) for name in cell_names])
            unknown = [f for f in filters if f not in real_num_set and f not in cat_set
                       and f not in score_index]
            if unknown:
                raise ValueError(f"Unknown filter field(s): {unknown!r}")

            joined = ' '.join(metadata_joins)
            if any(f in real_num_set for f in filters) and "LEFT JOIN obsnum n" not in joined:
                metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
            if any(f in cat_set for f in filters) and "LEFT JOIN obscat cat" not in joined:
                metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

            sidecar = self._scores_sidecar_path()
            for filter_position, (field, value) in enumerate(filters.items()):
                if field in real_num_set:
                    col_sql = f'n.{_q(field)}'   # trusted: matched against the schema
                    is_numeric = True
                elif field in cat_set:
                    col_sql = f'cat.{_q(field)}'
                    is_numeric = False
                else:
                    # Score-sidecar virtual column: join just that triple.
                    alias = f'score_filter_{filter_position}'
                    metadata_joins.append(
                        f"LEFT JOIN (SELECT cell_id, score FROM read_parquet(?) "
                        f"WHERE source = ? AND catalog = ? AND module = ?) {alias} "
                        f"ON {alias}.cell_id = c.cell_id"
                    )
                    join_params.extend([str(sidecar), *score_index[field]])
                    col_sql = f'{alias}.score'
                    is_numeric = True

                if is_numeric:
                    if isinstance(value, tuple) and len(value) == 2:
                        where_parts.append(f'{col_sql} BETWEEN ? AND ?')
                        where_params.extend(_numeric_filter_value(field, v) for v in value)
                    elif isinstance(value, list):
                        placeholders = ', '.join(['?'] * len(value))
                        where_parts.append(f'{col_sql} IN ({placeholders})')
                        where_params.extend(_numeric_filter_value(field, v) for v in value)
                    else:
                        where_parts.append(f'{col_sql} = ?')
                        where_params.append(_numeric_filter_value(field, value))
                else:
                    # Categorical values compare as text. A tuple here is the
                    # /cells "a,b" syntax parsed as a numeric range, which on
                    # a categorical column means the IN-list {a, b}.
                    raw_values = list(value) if isinstance(value, (list, tuple)) else [value]
                    text_values = _categorical_filter_values(raw_values)
                    placeholders = ', '.join(['?'] * len(text_values))
                    where_parts.append(f'{col_sql} IN ({placeholders})')
                    where_params.extend(text_values)

        params = params + join_params + where_params

        # Build inner SQL (filters applied here, sampling wraps this)
        inner_sql = f"""
            SELECT {', '.join(select_parts)}
            FROM cells c
            {' '.join(metadata_joins)}
        """

        if where_parts:
            inner_sql += f" WHERE {' AND '.join(where_parts)}"

        # Wrap in USING SAMPLE when requested (applied after filters).
        # `sample` / `limit` are coerced to int — they cannot carry SQL.
        if sample:
            sql = f"SELECT * FROM ({inner_sql}) USING SAMPLE {int(sample)} ROWS (reservoir)"
        else:
            sql = inner_sql

        if limit:
            sql += f" LIMIT {int(limit)}"

        df = self._conn.execute(sql, params).fetchdf()

        # Merge in the parquet-sidecar score columns the caller asked for:
        # one sidecar read for all of them, one merge on cell_name.
        if score_fields_to_merge:
            score_df = self._fetch_score_matrix(list(dict.fromkeys(score_fields_to_merge)))
            df = df.merge(score_df, on='cell_name', how='left')
        return df

    def get_embedding_data(self, embedding_id: str,
                          limit: Optional[int] = None,
                          n_dims: Optional[int] = 2,
                          filters: Optional[Dict[str, Any]] = None,
                          sample: Optional[int] = None) -> pd.DataFrame:
        """
        Get embedding coordinates

        Args:
            embedding_id: Embedding name
            limit: Maximum cells
            n_dims: Number of dimensions (None = all)
            filters: Optional metadata filters (same format as get_cell_table)
            sample: Random subsample size (absolute row count, system sampling).
                    Applied after filters. Uses DuckDB USING SAMPLE (reservoir).

        Returns:
            DataFrame with cell names and coordinates
        """

        # embedding_id arrives from the URL path: resolve it against the
        # schema (parameterised) and only ever interpolate the stored name.
        if not isinstance(embedding_id, str) or not embedding_id:
            raise ValueError("Embedding id must be a non-empty string")
        table_name = f"obsm__{embedding_id}"
        exists = (self._conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone() or (0,))[0]

        if not exists:
            raise ValueError(f"Embedding '{embedding_id}' not found")

        all_cols = [row[0] for row in self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()]

        if n_dims is None:
            select_cols = all_cols
        else:
            dim_cols = [c for c in all_cols if c.startswith('dim_')][:int(n_dims)]
            select_cols = ['cell_name'] + dim_cols
        select_sql = ', '.join(_q(c) for c in select_cols)
        table_sql = _q(table_name)

        limit_clause = f"LIMIT {int(limit)}" if limit else ""

        params: List[Any] = []
        filter_where = ""
        if filters:
            num_fields = set(self._get_numerical_fields())
            cat_fields = set(self._get_categorical_fields())
            score_fields = set(self._load_score_index())
            unknown = [f for f in filters
                       if f not in num_fields and f not in cat_fields]
            if unknown:
                raise ValueError(f"Unknown filter field(s): {', '.join(map(str, unknown))}")
            scored = [f for f in filters if f in score_fields and f not in cat_fields]
            if scored:
                raise ValueError(
                    f"Filtering on module-score columns is not supported: {', '.join(scored)}")
            sub_joins = []
            sub_where = []
            if any(f in num_fields for f in filters):
                sub_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
            if any(f in cat_fields for f in filters):
                sub_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

            def _bind(val: Any, numeric: bool) -> Any:
                if numeric:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"Filter value {val!r} is not numeric") from None
                return str(val)

            for field, value in filters.items():
                numeric = field in num_fields
                prefix = 'n' if numeric else 'cat'
                col_sql = f"{prefix}.{_q(field)}"  # field validated against schema above
                if isinstance(value, tuple) and len(value) == 2:
                    sub_where.append(f"{col_sql} BETWEEN ? AND ?")
                    params.extend([_bind(value[0], True), _bind(value[1], True)])
                elif isinstance(value, list):
                    if not value:
                        raise ValueError(f"Empty value list for filter {field!r}")
                    sub_where.append(f"{col_sql} IN ({', '.join(['?'] * len(value))})")
                    params.extend(_bind(v, numeric) for v in value)
                else:
                    sub_where.append(f"{col_sql} = ?")
                    params.append(_bind(value, numeric))
            if sub_where:
                subquery = (
                    f"SELECT c.cell_name FROM __cells c "
                    f"{' '.join(sub_joins)} "
                    f"WHERE {' AND '.join(sub_where)}"
                )
                filter_where = f"WHERE cell_name IN ({subquery})"

        if sample:
            inner = f"SELECT {select_sql} FROM {table_sql} {filter_where}"
            sql = f"SELECT * FROM ({inner}) USING SAMPLE {int(sample)} ROWS (reservoir) {limit_clause}"
        else:
            sql = f"""
                SELECT {select_sql}
                FROM {table_sql}
                {filter_where}
                {limit_clause}
            """

        df = self._conn.execute(sql, params).fetchdf()
        return df

    def require_gene_bins(self) -> None:
        """Raise ValueError if __gene_bins table is missing.

        Call this at the start of any method that needs module scoring.
        __gene_bins is built by ant-duckdb-check when a log-normalized layer
        is detected. Databases without a log-normalized layer will not have it.
        """
        exists = (self._conn.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = '__gene_bins'
        """).fetchone() or (0,))[0]
        if not exists:
            raise ValueError(
                "Module scoring is not available for this database: "
                "__gene_bins table is missing. "
                "Run 'ant duckdb-check' on a database that has a log-normalized layer."
            )

    def get_gene_stats(self, gene_names: List[str], layer: str = 'X',
                       case_sensitive: bool = True,
                       precomputed_dist: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Get expression statistics for one or more genes.

        Statistics over all cells (zeros included): mean, std.
        Statistics over expressing cells only: mean, std, median, q10, q90, max.

        Cross-dataset-comparable ranking fields included for every gene:

        - ``rank_mean`` (0–1): percentile rank of this gene's mean expression among
          all genes in the dataset (0 = lowest, 1 = highest).
        - ``scaled_mean`` (0–1): mean expression clipped to the [q05, q95] range
          of all gene means and rescaled to 0–1 — the canonical cross-dataset
          abundance score used by anthive-web's atlas page.
        - ``mean_expr_layer``: raw per-gene mean over the layer (zeros included)
          in the layer's native units (log-norm for ``.X``).

        Computed from the precomputed ``__gene_bins`` table when present (9/107
        datasets currently); otherwise from a full layer scan (~800 ms cold).
        Callers that batch many genes against the same dataset should cache the
        distribution and pass it via ``precomputed_dist`` to skip the scan:

            stats1 = db.get_gene_stats(['APOE'], layer='X')
            dist   = db._last_distribution        # populated when scan ran
            stats2 = db.get_gene_stats(['TREM2'], layer='X', precomputed_dist=dist)

        Args:
            gene_names: List of gene names
            layer: Layer name (default: 'X')
            case_sensitive: If False, resolve gene names case-insensitively (default: True).
                            Results include 'gene_found' with the actual stored name when
                            it differs from the queried name.
            precomputed_dist: Optional pre-cached distribution dict from a prior
                            call, of the form ``{'q05': ..., 'q95': ..., 'means':
                            {gene_name: mean_expr, ...}}``. If provided, the
                            method skips the layer-scan / __gene_bins lookup.

        Returns:
            List of dicts with stats per gene
        """
        import math

        n_cells = (self._conn.execute("SELECT COUNT(*) FROM cells").fetchone() or (0,))[0]

        # ── Step 1: resolve stored gene names (batched lookup) ────────────────
        # One query resolves every queried name at once. Keeps `resolved` a
        # queried_name → stored_name map (None when the gene is absent). We
        # also build resolved_ids so Step 3 can drive the batched aggregation
        # on gene_id (much faster than filtering by gene_name string).
        resolved: Dict[str, Optional[str]] = {n: None for n in gene_names}
        resolved_ids: Dict[str, Optional[int]] = {n: None for n in gene_names}
        stored_to_id: Dict[str, int] = {}
        if gene_names:
            placeholders = ','.join('?' * len(gene_names))
            if case_sensitive:
                lookup_rows = self._conn.execute(
                    f"SELECT gene_name, gene_id FROM __genes WHERE gene_name IN ({placeholders})",
                    gene_names,
                ).fetchall()
                stored_to_id = {r[0]: int(r[1]) for r in lookup_rows}
                for n in gene_names:
                    if n in stored_to_id:
                        resolved[n] = n
                        resolved_ids[n] = stored_to_id[n]
            else:
                lower_names = [n.lower() for n in gene_names]
                lookup_rows = self._conn.execute(
                    f"SELECT gene_name, gene_id FROM __genes WHERE LOWER(gene_name) IN ({placeholders})",
                    lower_names,
                ).fetchall()
                lower_to_stored: Dict[str, Tuple[str, int]] = {}
                for stored_n, gid in lookup_rows:
                    lower_to_stored[stored_n.lower()] = (stored_n, int(gid))
                for n in gene_names:
                    hit = lower_to_stored.get(n.lower())
                    if hit is not None:
                        stored, gid = hit
                        resolved[n] = stored
                        resolved_ids[n] = gid
                        stored_to_id[stored] = gid

        # ── Step 2: rank_mean + scaled_mean for the queried genes ─────────────
        #
        # rank_mean   = PERCENT_RANK of this gene's mean among all genes (0–1)
        # scaled_mean = mean clipped to [q05, q95] of the cross-gene distribution,
        #               rescaled to 0–1 — the cross-dataset-comparable abundance
        #               score used by the atlas page (anthive-web #193).
        # mean_expr_layer = raw per-gene mean over the layer (zeros included),
        #               in the layer's units (log-norm for .X).
        #
        # Three paths:
        #   (a) precomputed_dist passed in → constant-time lookup (the fast path
        #       /gene_stats_all hits after first call against the dataset)
        #   (b) __gene_bins table exists (9/107 datasets currently) → tiny window
        #       query against the precomputed gene_id → mean_expr table
        #   (c) neither → full layer scan to compute per-gene means + rank +
        #       q05/q95 in one SQL pass. ~800 ms cold on a 300k-cell, 25k-gene
        #       sparse layer. Result returned via self._last_distribution so
        #       ant-serve can cache it for paths (a) on subsequent queries.
        self._last_distribution: Optional[Dict[str, Any]] = None
        rank_ctx: Dict[str, Dict[str, float]] = {}
        stored_names = [s for s in resolved.values() if s is not None]

        if stored_names:
            placeholders = ','.join('?' * len(stored_names))

            if precomputed_dist is not None:
                # (a) Fast path: dist already cached at the call site.
                q05 = float(precomputed_dist.get('q05', 0.0))
                q95 = float(precomputed_dist.get('q95', 1.0))
                q_range = max(q95 - q05, 1e-9)
                means: Dict[str, float] = precomputed_dist.get('means', {}) or {}
                # PERCENT_RANK = (# below) / (n - 1) — needs sorted means.
                sorted_means = sorted(means.values())
                n_genes_total = max(len(sorted_means) - 1, 1)
                from bisect import bisect_left
                for sn in stored_names:
                    m = means.get(sn)
                    if m is None:
                        continue
                    rank = bisect_left(sorted_means, m) / n_genes_total
                    scaled = max(0.0, min(1.0, (m - q05) / q_range))
                    rank_ctx[sn] = {
                        'rank_mean':       round(rank, 4),
                        'scaled_mean':     round(scaled, 4),
                        'mean_expr_layer': round(m, 6),
                    }
            else:
                has_bins = (self._conn.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_name = '__gene_bins'
                """).fetchone() or (0,))[0]

                if has_bins:
                    # (b) Precomputed bins.
                    q_row = self._conn.execute("""
                        SELECT QUANTILE_CONT(mean_expr, 0.05),
                               QUANTILE_CONT(mean_expr, 0.95)
                        FROM __gene_bins
                    """).fetchone() or (0.0, 1.0)
                    q05, q95 = float(q_row[0]), float(q_row[1])
                    bin_rows = self._conn.execute(f"""
                        WITH ranked AS (
                            SELECT gene_id, mean_expr,
                                   PERCENT_RANK() OVER (ORDER BY mean_expr) AS rank_mean
                            FROM __gene_bins
                        )
                        SELECT g.gene_name, r.rank_mean, r.mean_expr
                        FROM ranked r
                        JOIN __genes g USING (gene_id)
                        WHERE g.gene_name IN ({placeholders})
                    """, stored_names).fetchall()
                else:
                    # (c) Full layer scan + retain the distribution so the
                    # caller can cache it. Read from the local parquet
                    # sidecar (via _layer_source_with_names) rather than the
                    # persisted view — the view has a host-baked absolute
                    # path that breaks on any host other than the migrator.
                    # Aggregate on gene_id straight off the layer and map to
                    # names afterwards: joining every nonzero value to
                    # __cells + __genes first forced a read of the (wide)
                    # cell_id column and a 10^8-10^10-row hash join.
                    gene_filter_src = self._layer_source_for_gene_filter(layer)
                    try:
                        all_means = self._conn.execute(f"""
                            SELECT g.gene_name, s.sum_value / {n_cells}.0 AS mean_expr
                            FROM (
                                SELECT gene_id, SUM(value) AS sum_value
                                FROM {gene_filter_src}
                                GROUP BY gene_id
                            ) s
                            JOIN __genes g ON g.gene_id = s.gene_id
                        """).fetchall()
                    except duckdb.BinderException:
                        # Legacy layer view with (cell_name, gene_name, value) only.
                        layer_src = self._layer_source_with_names(layer)
                        all_means = self._conn.execute(f"""
                            SELECT gene_name, SUM(value) / {n_cells}.0 AS mean_expr
                            FROM {layer_src}
                            GROUP BY gene_name
                        """).fetchall()
                    means_dict = {row[0]: float(row[1]) for row in all_means}
                    if means_dict:
                        sorted_vals = sorted(means_dict.values())
                        n = len(sorted_vals)
                        # q05/q95 by linear interpolation between the
                        # bracketing values (same as DuckDB's QUANTILE_CONT).
                        def _quant(p: float) -> float:
                            if n == 0:
                                return 0.0
                            idx = p * (n - 1)
                            lo = int(idx)
                            hi = min(lo + 1, n - 1)
                            frac = idx - lo
                            return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac
                        q05 = _quant(0.05)
                        q95 = _quant(0.95)
                    else:
                        q05, q95 = 0.0, 1.0

                    self._last_distribution = {
                        'q05': q05, 'q95': q95, 'means': means_dict,
                    }

                    # Build bin_rows for queried genes only.
                    sorted_vals = sorted(means_dict.values())
                    n_total = max(len(sorted_vals) - 1, 1)
                    from bisect import bisect_left
                    bin_rows = []
                    for sn in stored_names:
                        m = means_dict.get(sn)
                        if m is None:
                            continue
                        rank = bisect_left(sorted_vals, m) / n_total
                        bin_rows.append((sn, rank, m))

                q_range = max(q95 - q05, 1e-9)
                for gname, rank_mean, mean_expr in bin_rows:
                    scaled = max(0.0, min(1.0, (float(mean_expr) - q05) / q_range))
                    rank_ctx[gname] = {
                        'rank_mean':       round(float(rank_mean), 4),
                        'scaled_mean':     round(scaled, 4),
                        'mean_expr_layer': round(float(mean_expr), 6),
                    }

        # ── Step 3: per-gene expression statistics (BATCHED) ──────────────────
        # One aggregated SQL query for all requested genes instead of one query
        # per gene. On a 150k-cell parquet-backed dataset this drops the 200-
        # candidate top-var path from ~7 s to ~0.5 s (measured 2026-08-24) — the
        # by_gene parquet is sorted by gene_id, so a WHERE gene_id IN (...)
        # scans only the requested gene ranges.
        #
        # Path A (fast): by_gene parquet exists → query it directly by gene_id.
        # Path B (legacy): no parquet → __layer__<safe> table has gene_id too.
        # Path C (older legacy view with gene_name): fall back to per-gene loop.
        results: List[Dict[str, Any]] = []
        stats_by_stored: Dict[str, Dict[str, Any]] = {}

        # Collect the gene_ids we need in one go (from Step 1's resolved_ids).
        ids_for_query = [
            (n, resolved_ids[n], resolved[n])
            for n in gene_names
            if resolved_ids.get(n) is not None and resolved.get(n) is not None
        ]

        if ids_for_query:
            gene_id_list = [gid for _, gid, _ in ids_for_query]
            id_to_stored: Dict[int, str] = {gid: sn for _, gid, sn in ids_for_query}
            # Unique the ids (a queried duplicate should hit cache once).
            unique_ids = list(dict.fromkeys(gene_id_list))

            layer_source = self._layer_source_for_gene_filter(layer)
            id_placeholders = ','.join('?' * len(unique_ids))
            batch_sql = f"""
                SELECT
                    gene_id,
                    COUNT(*)                          AS n_nonzero,
                    SUM(value)                        AS sum_nz,
                    SUM(value * value)                AS sum_sq_nz,
                    AVG(value)                        AS mean_nz,
                    STDDEV_POP(value)                 AS std_nz,
                    MEDIAN(value)                     AS median_nz,
                    QUANTILE_CONT(value, 0.1)         AS q10_nz,
                    QUANTILE_CONT(value, 0.9)         AS q90_nz,
                    MAX(value)                        AS max_val
                FROM {layer_source}
                WHERE gene_id IN ({id_placeholders})
                GROUP BY gene_id
            """
            try:
                batch_rows = self._conn.execute(batch_sql, unique_ids).fetchall()
            except duckdb.BinderException:
                # Older legacy views only expose (cell_name, gene_name, value)
                # with no gene_id column — fall back to per-gene loop on those.
                batch_rows = None

            if batch_rows is not None:
                by_gid: Dict[int, Tuple[Any, ...]] = {int(r[0]): r for r in batch_rows}
                for gene_name, gid, stored_name in ids_for_query:
                    row = by_gid.get(int(gid))
                    if row is None or row[1] == 0:
                        stats_by_stored[stored_name] = {
                            'n_nonzero': 0, 'pct_nonzero': 0.0,
                            'mean': 0.0, 'std': 0.0,
                            'mean_nonzero': None, 'std_nonzero': None,
                            'median_nonzero': None, 'q10_nonzero': None, 'q90_nonzero': None,
                            'max': 0.0,
                        }
                        continue
                    (_gid, n_nonzero, sum_nz, sum_sq_nz, mean_nz, std_nz,
                     median_nz, q10_nz, q90_nz, max_val) = row
                    mean_all = float(sum_nz) / n_cells if n_cells else 0.0
                    var_all = max(0.0, float(sum_sq_nz) / n_cells - mean_all ** 2) if n_cells else 0.0
                    std_all = math.sqrt(var_all)
                    cv = round(std_all / mean_all, 4) if mean_all > 0 else None
                    stats_by_stored[stored_name] = {
                        'n_nonzero': int(n_nonzero),
                        'pct_nonzero': round(n_nonzero * 100.0 / n_cells, 2) if n_cells else 0.0,
                        'mean': round(mean_all, 6),
                        'std': round(std_all, 6),
                        'cv': cv,
                        'mean_nonzero': round(float(mean_nz), 6),
                        'std_nonzero': round(float(std_nz), 6),
                        'median_nonzero': round(float(median_nz), 6),
                        'q10_nonzero': round(float(q10_nz), 6),
                        'q90_nonzero': round(float(q90_nz), 6),
                        'max': round(float(max_val), 6),
                    }
            else:
                # Fallback: per-gene loop on legacy layer view (rare — pre-parquet
                # datasets). Keeps behavior identical for these edge cases.
                layer_src = self._layer_source_with_names(layer)
                for gene_name, _gid, stored_name in ids_for_query:
                    row = self._conn.execute(f"""
                        SELECT
                            COUNT(*)                          AS n_nonzero,
                            SUM(value)                        AS sum_nz,
                            SUM(value * value)                AS sum_sq_nz,
                            AVG(value)                        AS mean_nz,
                            STDDEV_POP(value)                 AS std_nz,
                            MEDIAN(value)                     AS median_nz,
                            QUANTILE_CONT(value, 0.1)         AS q10_nz,
                            QUANTILE_CONT(value, 0.9)         AS q90_nz,
                            MAX(value)                        AS max_val
                        FROM {layer_src}
                        WHERE gene_name = ?
                    """, [stored_name]).fetchone()
                    if row is None or row[0] == 0:
                        stats_by_stored[stored_name] = {
                            'n_nonzero': 0, 'pct_nonzero': 0.0,
                            'mean': 0.0, 'std': 0.0,
                            'mean_nonzero': None, 'std_nonzero': None,
                            'median_nonzero': None, 'q10_nonzero': None, 'q90_nonzero': None,
                            'max': 0.0,
                        }
                        continue
                    n_nonzero, sum_nz, sum_sq_nz, mean_nz, std_nz, median_nz, q10_nz, q90_nz, max_val = row
                    mean_all = float(sum_nz) / n_cells if n_cells else 0.0
                    var_all = max(0.0, float(sum_sq_nz) / n_cells - mean_all ** 2) if n_cells else 0.0
                    std_all = math.sqrt(var_all)
                    cv = round(std_all / mean_all, 4) if mean_all > 0 else None
                    stats_by_stored[stored_name] = {
                        'n_nonzero': int(n_nonzero),
                        'pct_nonzero': round(n_nonzero * 100.0 / n_cells, 2) if n_cells else 0.0,
                        'mean': round(mean_all, 6),
                        'std': round(std_all, 6),
                        'cv': cv,
                        'mean_nonzero': round(float(mean_nz), 6),
                        'std_nonzero': round(float(std_nz), 6),
                        'median_nonzero': round(float(median_nz), 6),
                        'q10_nonzero': round(float(q10_nz), 6),
                        'q90_nonzero': round(float(q90_nz), 6),
                        'max': round(float(max_val), 6),
                    }

        # ── Step 4: assemble the response in queried-name order ───────────────
        for gene_name in gene_names:
            stored_name = resolved.get(gene_name)
            if stored_name is None:
                results.append({'gene': gene_name, 'error': 'not found'})
                continue

            base: Dict[str, Any] = {'gene': gene_name, 'layer': layer, 'n_cells': n_cells}
            if stored_name != gene_name:
                base['gene_found'] = stored_name
            if stored_name in rank_ctx:
                base.update(rank_ctx[stored_name])

            row_stats = stats_by_stored.get(stored_name)
            if row_stats is None:
                # Defensive: gene resolved but no stats row (shouldn't happen).
                row_stats = {
                    'n_nonzero': 0, 'pct_nonzero': 0.0,
                    'mean': 0.0, 'std': 0.0,
                    'mean_nonzero': None, 'std_nonzero': None,
                    'median_nonzero': None, 'q10_nonzero': None, 'q90_nonzero': None,
                    'max': 0.0,
                }
            results.append({**base, **row_stats})

        return results

    def _layer_source_for_gene_filter(self, layer: str) -> str:
        """Return the SQL table/view/parquet expression best suited for a gene-filter query.

        For Parquet-backed datasets the by-gene parquet is sorted by gene_id, making
        gene-filter queries 4-100× faster than the by-cell ordering.  For legacy
        in-DuckDB datasets we return the ``__layer__<safe>`` table name directly so
        callers that do ``WHERE gene_id IN (...)`` continue to work.

        The rule is conservative: if we cannot find a Parquet sidecar, we fall
        back to the old ``__layer__`` table so this helper is safe to call from
        any context.

        Args:
            layer: Layer key (e.g. 'X', 'counts').

        Returns:
            A SQL expression suitable for use in ``FROM <expr>``, either a
            ``read_parquet('...')`` call (Parquet-backed) or a quoted table name
            (legacy).
        """
        safe = ''.join(c if c.isalnum() else '_' for c in layer)
        if self.db_path is not None:
            db_path = Path(self.db_path)
            stem = db_path.stem
            by_gene = db_path.parent / f"{stem}.layer.{layer}.by_gene.parquet"
            if by_gene.exists():
                # Escape single quotes in the path (SQL string-literal rules)
                # so a dataset in a directory named e.g. `O'Brien2025` still
                # produces a valid SELECT.
                escaped = str(by_gene.resolve()).replace("'", "''")
                return f"read_parquet('{escaped}')"
        # Fallback: legacy in-DuckDB table
        return f'"__layer__{safe}"'

    def _layer_source_with_names(self, layer: str) -> str:
        """Return a SQL ``FROM`` expression yielding ``(cell_name, gene_name, value)``.

        Portable across hosts: resolves the parquet sidecar from the duckdb's
        OWN directory at query time (never bakes a host-absolute path). Prefers
        the by_gene parquet (gene-filter queries dominate). Falls back to the
        persisted ``layer__<safe>`` view for legacy in-DuckDB datasets with no
        parquet sidecar.

        This is the anti-view read path: ``ant-migrate-to-parquet`` writes each
        ``layer__*`` view with a hardcoded absolute path to the sidecar, which
        breaks the moment the duckdb is copied to a host with a different
        h5adstore mount. Every gene-fetch, gene-stats and mean-expression query
        in this module now goes through this helper instead of ``FROM layer__X``.
        """
        safe = ''.join(c if c.isalnum() else '_' for c in layer)
        if self.db_path is not None:
            db_path = Path(self.db_path)
            stem = db_path.stem
            by_gene = db_path.parent / f"{stem}.layer.{layer}.by_gene.parquet"
            if by_gene.exists():
                escaped = str(by_gene.resolve()).replace("'", "''")
                # Predicate pushdown: DuckDB pushes WHERE gene_name = ? and
                # WHERE cell_name = ? down into the JOINs on __genes/__cells,
                # so a gene-filter query only touches the matching gene's rows
                # in the by_gene parquet.
                return (
                    f"(SELECT c.cell_name, g.gene_name, l.value "
                    f"FROM read_parquet('{escaped}') l "
                    f"JOIN __cells c ON l.cell_id = c.cell_id "
                    f"JOIN __genes g ON l.gene_id = g.gene_id)"
                )
        # Legacy fallback: the persisted layer__ view. The view carries a
        # (broken-on-non-migrator-hosts) absolute path if parquet-migrated,
        # but the branch above avoids that case entirely. For truly legacy
        # datasets that never went through migration, this is the in-DuckDB
        # table wrapped by a view with (cell_name, gene_name, value).
        return f'"layer__{safe}"'

    # Ordered candidates for a "human-readable symbol" column in __varcat,
    # searched when __genes.gene_name looks like Ensembl. Mirrors the
    # autodetect in ``bin/annot/ant-aucell``.
    _SYMBOL_COLUMN_CANDIDATES = (
        'feature_name', 'gene_symbol', 'Symbol', 'gene_symbols',
        'symbol', 'hgnc_symbol', 'mgi_symbol',
    )

    def _looks_like_ensembl(self, sample_size: int = 200) -> bool:
        """Return True when ``__genes.gene_name`` predominantly holds Ensembl IDs.

        Samples up to ``sample_size`` rows and returns True when at least half
        of the observed values start with ``ENSG``/``ENSMUS`` (human/mouse).
        Uses gene_id ordering so the sample is deterministic.
        """
        rows = self._conn.execute(
            "SELECT gene_name FROM __genes ORDER BY gene_id LIMIT ?",
            [int(sample_size)],
        ).fetchall()
        if not rows:
            return False
        n_ensembl = sum(
            1 for (name,) in rows
            if isinstance(name, str) and (name.startswith('ENSG') or name.startswith('ENSMUS'))
        )
        return n_ensembl / len(rows) >= 0.5

    def _find_symbol_column(self) -> Optional[str]:
        """Return the first available __varcat column that holds gene symbols.

        Returns None when __varcat does not exist or contains none of the
        candidate columns.
        """
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = '__varcat'"
        ).fetchall()
        available = {name for (name,) in rows}
        if not available:
            return None
        for cand in self._SYMBOL_COLUMN_CANDIDATES:
            if cand in available:
                return cand
        return None

    def _resolve_symbols_to_gene_ids(
        self, symbols: List[str]
    ) -> Tuple[Dict[str, int], Optional[str]]:
        """Map gene symbols → gene_ids, transparently handling Ensembl-indexed DBs.

        First tries the direct ``__genes.gene_name`` match. When the direct
        match yields no hits AND ``__genes.gene_name`` looks Ensembl AND a
        symbol column exists in ``__varcat``, retries via ``__varcat`` (case-
        insensitive on the symbol column).

        Args:
            symbols: Gene symbols to resolve.

        Returns:
            Tuple of (map from input symbol to gene_id, resolution route).
            Route ∈ ``{'gene_name', 'varcat:<column>', None}``. Missing
            symbols are absent from the map.
        """
        if not symbols:
            return {}, None

        placeholders = ', '.join('?' for _ in symbols)
        direct_rows = self._conn.execute(
            f"SELECT gene_name, gene_id FROM __genes WHERE gene_name IN ({placeholders})",
            symbols,
        ).fetchall()
        if direct_rows:
            return {name: gid for name, gid in direct_rows}, 'gene_name'

        # Ensembl fallback via __varcat.<symbol_column>.
        if not self._looks_like_ensembl():
            return {}, None
        symbol_column = self._find_symbol_column()
        if symbol_column is None:
            return {}, None
        upper_placeholders = ', '.join('?' for _ in symbols)
        varcat_rows = self._conn.execute(
            f"SELECT UPPER({safe_ident(symbol_column)}) AS sym, gene_id "
            f"FROM __varcat "
            f"WHERE UPPER({safe_ident(symbol_column)}) IN ({upper_placeholders})",
            [s.upper() for s in symbols],
        ).fetchall()
        if not varcat_rows:
            return {}, f'varcat:{symbol_column}'
        upper_map: Dict[str, int] = {}
        for sym_upper, gid in varcat_rows:
            upper_map.setdefault(sym_upper, gid)
        found: Dict[str, int] = {}
        for original in symbols:
            hit = upper_map.get(original.upper())
            if hit is not None:
                found[original] = hit
        return found, f'varcat:{symbol_column}'

    def score_module(
        self,
        module_genes: List[str],
        layer: str = 'X',
        n_control: int = 100,
        seed: int = 42,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Compute per-cell module scores (Scanpy score_genes equivalent).

        For each cell, score = mean(module genes) - mean(control genes),
        where control genes are sampled from the same expression bins as
        the module genes (using __gene_bins built by ant-duckdb-check).

        Ensembl-indexed duckdbs (``__genes.gene_name`` holds ENSG… IDs) are
        handled transparently: when a direct ``gene_name`` match yields
        nothing, the resolver retries via ``__varcat.feature_name`` (or the
        next available symbol column).

        Args:
            module_genes: List of gene symbols in the module.
            layer: Expression layer (default 'X'). Must be log-normalized.
            n_control: Control genes sampled per expression bin (default 100).
            seed: Integer seed for reproducible control gene selection.

        Returns:
            Tuple of (scores_df, meta_dict). ``scores_df`` has columns
            ``[cell_name, module_score]``. ``meta_dict`` has keys
            ``genes_found``, ``genes_missing``, ``n_control_genes``, and
            ``gene_resolution`` (``'gene_name'`` | ``'varcat:<col>'`` | ``None``).

        Raises:
            ValueError: If no module genes can be resolved to gene_ids.
        """
        self.require_gene_bins()

        found_map, resolution = self._resolve_symbols_to_gene_ids(module_genes)
        genes_found = [g for g in module_genes if g in found_map]
        genes_missing = [g for g in module_genes if g not in found_map]

        if not genes_found:
            raise ValueError(
                f"None of the module genes found in database: {module_genes}"
            )

        gene_id_list = ', '.join(str(found_map[g]) for g in genes_found)
        # score_module is always a gene-centric query (filter by gene_id) so
        # we use the by-gene view for Parquet-backed datasets.
        layer_source = self._layer_source_for_gene_filter(layer)

        # Count control genes that will be selected
        n_control_genes = self._conn.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT gb.gene_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY gb.expr_bin
                           ORDER BY md5(CAST(gb.gene_id AS VARCHAR) || CAST({seed} AS VARCHAR))
                       ) AS rn
                FROM __gene_bins gb
                WHERE gb.expr_bin IN (
                    SELECT DISTINCT expr_bin FROM __gene_bins
                    WHERE gene_id IN ({gene_id_list})
                )
                AND gb.gene_id NOT IN ({gene_id_list})
            ) WHERE rn <= {n_control}
        """).fetchone()[0]

        # Compute per-cell module scores
        scores_df = self._conn.execute(f"""
            WITH control AS (
                SELECT gene_id FROM (
                    SELECT
                        gb.gene_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY gb.expr_bin
                            ORDER BY md5(CAST(gb.gene_id AS VARCHAR) || CAST({seed} AS VARCHAR))
                        ) AS rn
                    FROM __gene_bins gb
                    WHERE gb.expr_bin IN (
                        SELECT DISTINCT expr_bin
                        FROM __gene_bins
                        WHERE gene_id IN ({gene_id_list})
                    )
                    AND gb.gene_id NOT IN ({gene_id_list})
                )
                WHERE rn <= {n_control}
            ),
            n_control_total AS (
                SELECT COUNT(*) AS n FROM control
            ),
            module_avg AS (
                -- SUM/n_genes (not AVG) because sparse storage omits zeros
                SELECT cell_id,
                       SUM(value) / {len(genes_found)}.0 AS m
                FROM {layer_source}
                WHERE gene_id IN ({gene_id_list})
                GROUP BY cell_id
            ),
            control_avg AS (
                SELECT cell_id,
                       SUM(value) / (SELECT n FROM n_control_total)::DOUBLE AS c
                FROM {layer_source}
                WHERE gene_id IN (SELECT gene_id FROM control)
                GROUP BY cell_id
            )
            -- Every cell gets a score: a cell with no nonzero module or
            -- control expression scores 0 - 0 = 0 (sparse storage omits zeros).
            SELECT
                cl.cell_name,
                COALESCE(ma.m, 0.0) - COALESCE(ca.c, 0.0) AS module_score
            FROM __cells cl
            LEFT JOIN module_avg  ma ON cl.cell_id = ma.cell_id
            LEFT JOIN control_avg ca ON cl.cell_id = ca.cell_id
            ORDER BY cl.cell_id
        """).fetchdf()

        meta = {
            'genes_found': genes_found,
            'genes_missing': genes_missing,
            'n_control_genes': int(n_control_genes),
            'gene_resolution': resolution,
        }
        return scores_df, meta

    def execute_sql(self, query: str, limit: Optional[int] = None) -> pd.DataFrame:
        """
        Execute raw SQL query

        Args:
            query: SQL query
            limit: Optional limit to append

        Returns:
            DataFrame with results
        """

        if limit and 'LIMIT' not in query.upper():
            query = f"{query} LIMIT {limit}"

        df = self._conn.execute(query).fetchdf()
        return df


# ---------------------------------------------------------------------------
# AnnotationWriter — incremental obs column additions via ALTER TABLE
# ---------------------------------------------------------------------------
#
# Tracks added columns in a `__pending_cols` table so that ant-merge-h5ad can
# later sync them back into the h5ad. See doc/DESIGN_annotation_updates.md.
#
# Annotator names for `annotator` field — KEEP IN SYNC with ant-convert's
# discard-pending guard and with ant-merge-h5ad's audit:
#   'ant-score', 'ant-annotate', 'ant-starcat'

_ANNO_COL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)*$')


def _validate_anno_col_name(col_name: str) -> None:
    """Annotator-created columns must be one or more bare identifiers
    (letters, digits, underscores; start with letter/underscore) joined by
    the ``::`` separator. Existing fleet columns follow the legacy
    ``<study>::<experiment>::<module>`` naming (e.g.
    ``Sierksma2025::WGCNA::Black_Mito``); we keep quoting via ``safe_ident``
    in every SQL path, so ``::`` is safe."""
    if not isinstance(col_name, str) or not _ANNO_COL_RE.fullmatch(col_name):
        raise ValueError(
            f"Annotation column name must match {_ANNO_COL_RE.pattern!r} "
            f"(bare identifier or ``::``-joined identifiers); got {col_name!r}"
        )
    if col_name.lower() in {'cell_id', 'cell_name', 'gene_id', 'gene_name'}:
        raise ValueError(f"Reserved column name: {col_name!r}")


class AnnotationWriterError(Exception):
    """Raised by AnnotationWriter for any contract violation."""


def _post_ant_serve(
    base_url: Optional[str],
    path: str,
    *,
    timeout: float = 35.0,
    on_failure: Literal['raise', 'warn', 'ignore'] = 'raise',
) -> Optional[Dict[str, Any]]:
    """POST to ant-serve and return the parsed JSON response.

    Returns None when `on_failure != 'raise'` and the call fails (server
    down, network error, etc). Re-raises AnnotationWriterError on failure
    when `on_failure == 'raise'`.
    """
    if not base_url:
        if on_failure == 'raise':
            raise AnnotationWriterError(f"no ant_serve_url given")
        return None
    # Normalize: strip any trailing slashes from base, ensure path has one
    # leading slash. This makes `_post_ant_serve('http://h/api/', '/admin/x')`
    # and `_post_ant_serve('http://h/api',  '/admin/x')` both produce
    # `http://h/api/admin/x` with no double slashes.
    url = base_url.rstrip('/') + '/' + path.lstrip('/')
    req = urllib.request.Request(url, method='POST',
                                 headers={'Content-Length': '0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            try:
                return json.loads(body.decode())
            except Exception:
                return {'ok': True, 'raw': body[:200]}
    except urllib.error.URLError as exc:
        msg = f"POST {url} failed: {exc}"
        if on_failure == 'raise':
            raise AnnotationWriterError(msg)
        if on_failure == 'warn':
            print(f"  ⚠ {msg}")
        return None


class AnnotationWriter:
    """Context manager for adding obs columns to a duckdb via ALTER TABLE.

    Use as:

        with AnnotationWriter(duckdb_path, annotator='ant-score',
                              run_id='ws-c1d2', catalog='WGCNA_M3') as w:
            w.add_numerical('module_score_M3', series)  # series.index = cell_name
            w.add_categorical('Identity_Label', other_series)
            w.commit()

    The with-block is one DuckDB transaction: on exception, or on exit
    without `.commit()`, added columns, overwritten values, view rebuilds and
    __pending_cols rows are all rolled back.

    Locking: a cooperative flock on `<duckdb>.lock` prevents two writers.
    ant-serve's read-only connections are unaffected (DuckDB MVCC).

    Refuses to open if `<duckdb>.rebuilding` sidecar exists (set by ant-convert).
    """

    LOCK_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        duckdb_path: Path,
        *,
        annotator: str,
        run_id: str,
        catalog: Optional[str] = None,
        db_id: Optional[str] = None,
        ant_serve_url: Optional[str] = None,
    ):
        """
        Args:
            duckdb_path: Path to the dataset's .duckdb file
            annotator:   Tool name (e.g. 'ant-score', 'ant-starcat')
            run_id:      Unique per-invocation id (for rollback / audit)
            catalog:     Optional catalog tag (e.g. 'MICROGLIA_V1')
            db_id:       If given together with ant_serve_url, the writer will
                         POST /admin/release/{db_id} when it gets a
                         "Conflicting lock" error from DuckDB, then retry.
            ant_serve_url: Base URL of a running ant-serve (e.g.
                         'http://localhost:8080/api'). Used together with
                         db_id to negotiate writer-vs-server lock contention.
        """
        # Slave-mode gate. Any AnnotationWriter creates a r/w handle on the
        # duckdb and would be overwritten on the next sync-pull. Refuse at
        # construction so the caller fails fast with a clear message.
        # Local import to avoid a cycle on module import.
        from config import is_slave  # noqa: PLC0415
        if is_slave():
            raise AnnotationWriterError(
                f"refusing to open {duckdb_path} for write — this deployment "
                f"is configured as a slave (sync_policy.role=slave in "
                f"anthive.yaml). Annotation writes happen on the primary."
            )

        self.duckdb_path = Path(duckdb_path)
        if not self.duckdb_path.exists():
            raise AnnotationWriterError(f"duckdb not found: {self.duckdb_path}")
        self.annotator = annotator
        self.run_id = run_id
        self.catalog = catalog
        self.db_id = db_id
        self.ant_serve_url = ant_serve_url.rstrip('/') if ant_serve_url else None
        # Load the .md's api: config so view rebuilds preserve the current
        # rename+hide. Best-effort — if the sibling .md is missing (fresh
        # ingest before .md written), we fall back to no-op api_config.
        self.api_config = _load_api_config_for(self.duckdb_path)
        self.lock_path = self.duckdb_path.with_suffix(self.duckdb_path.suffix + '.lock')
        self.rebuilding_path = self.duckdb_path.with_suffix(
            self.duckdb_path.suffix + '.rebuilding'
        )
        self._lock_fd = None
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._released_db = False  # True if we POSTed /admin/release/...
        # Track (col_name, target_table) for each column added in this txn,
        # for rollback. Cleared on commit().
        self._txn_cols: List[Tuple[str, str]] = []
        self._committed = False
        self._in_transaction = False

    # --------- context management ---------

    def __enter__(self) -> "AnnotationWriter":
        if self.rebuilding_path.exists():
            raise AnnotationWriterError(
                f"{self.duckdb_path.name} is being rebuilt by ant-convert "
                f"(sentinel: {self.rebuilding_path}). Refusing to open."
            )
        self._acquire_lock()
        try:
            self._conn = self._open_with_release_retry()
            self._ensure_pending_table()
            # One transaction for the whole with-block: ADD COLUMN, UPDATE,
            # view rebuilds and __pending_cols rows land together or not at
            # all (DuckDB DDL is transactional). A crash before commit()
            # leaves the duckdb exactly as it was.
            self._conn.execute("BEGIN TRANSACTION")
            self._in_transaction = True
        except Exception:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._release_lock()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is not None or not self._committed:
                # Either an exception was raised, or the user never called
                # commit() → rollback whatever we added in this transaction.
                self._rollback_txn()
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            # If we asked ant-serve to release the db, restore it now so
            # subsequent /databases/<db_id>/* requests work again.
            if self._released_db:
                _post_ant_serve(
                    self.ant_serve_url, f'/admin/restore/{self.db_id}',
                    on_failure='warn',
                )
                self._released_db = False
            self._release_lock()

    # --------- DuckDB open with release-retry ---------

    def _open_with_release_retry(self):
        """Open the duckdb r/w. If we hit DuckDB's cross-process lock error,
        attempt to coordinate with ant-serve once and retry."""
        try:
            return duckdb.connect(str(self.duckdb_path), read_only=False)
        except duckdb.IOException as exc:
            if 'Conflicting lock' not in str(exc):
                raise
            # Could be ant-serve holding the file. Try to coordinate.
            if not self.ant_serve_url or not self.db_id:
                raise AnnotationWriterError(
                    f"DuckDB lock conflict on {self.duckdb_path.name}, and no "
                    f"(db_id, ant_serve_url) given for coordination. Stop "
                    f"ant-serve manually, or pass db_id + ant_serve_url to "
                    f"AnnotationWriter.\n\nOriginal error: {exc}"
                )
            print(f"  ⚠ DuckDB lock held — asking ant-serve to release {self.db_id}…")
            result = _post_ant_serve(
                self.ant_serve_url, f'/admin/release/{self.db_id}',
                on_failure='raise',
            )
            if not (result and result.get('ok')):
                raise AnnotationWriterError(
                    f"ant-serve release of {self.db_id!r} did not succeed: {result}"
                )
            self._released_db = True
            # Retry once
            try:
                return duckdb.connect(str(self.duckdb_path), read_only=False)
            except duckdb.IOException as exc2:
                # Best-effort cleanup of the eviction we set
                _post_ant_serve(
                    self.ant_serve_url, f'/admin/restore/{self.db_id}',
                    on_failure='ignore',
                )
                self._released_db = False
                raise AnnotationWriterError(
                    f"DuckDB still locked after ant-serve release: {exc2}"
                )

    # --------- locking ---------

    def _acquire_lock(self) -> None:
        deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
        self._lock_fd = open(self.lock_path, 'w')
        while True:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._lock_fd.close()
                    self._lock_fd = None
                    raise AnnotationWriterError(
                        f"Could not acquire lock on {self.lock_path} within "
                        f"{self.LOCK_TIMEOUT_SECONDS}s — another writer is active."
                    )
                time.sleep(0.5)

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None
            try:
                # File is just a flock anchor; safe to remove.
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    # --------- schema bootstrap ---------

    def _ensure_pending_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS __pending_cols (
                col_name      VARCHAR PRIMARY KEY,
                target_table  VARCHAR NOT NULL,
                kind          VARCHAR NOT NULL,
                added_at      TIMESTAMP NOT NULL,
                annotator     VARCHAR NOT NULL,
                run_id        VARCHAR NOT NULL,
                catalog       VARCHAR,
                source_hash   VARCHAR
            )
        """)

    # --------- public API ---------

    def add_numerical(
        self,
        col_name: str,
        values: pd.Series,
        *,
        source_hash: Optional[str] = None,
        on_conflict: Literal['fail', 'overwrite'] = 'fail',
    ) -> None:
        """Add a DOUBLE obs column. `values.index` must be cell_name."""
        self._add_column(
            col_name=col_name,
            values=values,
            target='obsnum',
            sql_type='DOUBLE',
            kind='num',
            on_conflict=on_conflict,
            source_hash=source_hash,
        )

    def add_categorical(
        self,
        col_name: str,
        values: pd.Series,
        *,
        source_hash: Optional[str] = None,
        on_conflict: Literal['fail', 'overwrite'] = 'fail',
        max_cardinality: int = 10000,
    ) -> None:
        """Add a VARCHAR obs column. `values.index` must be cell_name.

        Refuses if non-null cardinality exceeds `max_cardinality` (default 10k)
        — protects against accidentally writing cell-id-like columns.
        """
        non_null = values.dropna()
        n_unique = non_null.nunique()
        if n_unique > max_cardinality:
            raise AnnotationWriterError(
                f"Categorical '{col_name}' has {n_unique:,} unique values "
                f"(>{max_cardinality:,} limit). Refusing as likely cell-id-like."
            )
        self._add_column(
            col_name=col_name,
            values=values,
            target='obscat',
            sql_type='VARCHAR',
            kind='cat',
            on_conflict=on_conflict,
            source_hash=source_hash,
        )

    def commit(self) -> None:
        """COMMIT the with-block's transaction and seal the writer."""
        if self._committed:
            return
        if self._conn is None:
            raise AnnotationWriterError("Writer not entered (use `with`).")
        if self._in_transaction:
            self._conn.execute("COMMIT")
            self._in_transaction = False
        self._txn_cols = []
        self._committed = True

    # --------- internals ---------

    def _add_column(
        self,
        *,
        col_name: str,
        values: pd.Series,
        target: str,                # 'obsnum' | 'obscat'
        sql_type: str,              # 'DOUBLE' | 'VARCHAR'
        kind: str,                  # 'num' | 'cat'
        on_conflict: str,
        source_hash: Optional[str],
    ) -> None:
        if self._conn is None:
            raise AnnotationWriterError("Writer not entered (use `with`).")
        if self._committed:
            raise AnnotationWriterError("Writer already committed.")

        _validate_anno_col_name(col_name)

        underscore_table = f"__{target}"  # __obsnum / __obscat

        # --- cell_name alignment check ---
        cell_rows = self._conn.execute(
            "SELECT cell_id, cell_name FROM __cells"
        ).fetchall()
        cell_id_by_name = {row[1]: row[0] for row in cell_rows}
        valid_names = set(cell_id_by_name)

        provided = set(values.index.astype(str))
        extra = provided - valid_names
        if extra:
            sample = sorted(extra)[:5]
            raise AnnotationWriterError(
                f"{len(extra):,} cell_names in values are not in __cells "
                f"(sample: {sample})"
            )
        missing = valid_names - provided
        if missing:
            # Allowed but warned — those rows will be NULL.
            print(
                f"  ⚠ {col_name}: {len(missing):,}/{len(valid_names):,} cells "
                f"missing from values; will be NULL"
            )

        # --- type coercion ---
        if kind == 'num':
            try:
                values = values.astype('float64')
            except Exception as exc:
                raise AnnotationWriterError(
                    f"Cannot coerce '{col_name}' to float64: {exc}"
                )
        else:
            # Categorical → object/string, NaN → None
            values = values.astype('object')
            values = values.where(values.notna(), None)
            values = values.map(lambda v: str(v) if v is not None else None)

        # --- existing column check ---
        underscore_exists = self._table_exists(underscore_table)
        if underscore_exists:
            existing = self._column_names(underscore_table)
            # DuckDB identifiers are case-insensitive when unquoted, but the
            # __pending_cols PK is exact. Check both ways to be safe.
            existing_lower = {c.lower() for c in existing}
            if col_name in existing or col_name.lower() in existing_lower:
                if on_conflict == 'fail':
                    raise AnnotationWriterError(
                        f"Column '{col_name}' already exists in {underscore_table}; "
                        f"pass on_conflict='overwrite' to replace."
                    )
                # Overwrite path: skip ADD COLUMN, just UPDATE.
                self._update_values(underscore_table, col_name, values, cell_id_by_name)
                self._record_pending(col_name, target, kind, source_hash)
                self._refresh_view(target, underscore_table)
                return

        # --- ensure base table exists (first annotation of this kind) ---
        if not underscore_exists:
            self._create_obs_base_table(underscore_table, col_name, sql_type, cell_rows)
            self._txn_cols.append((col_name, target))
            self._refresh_view(target, underscore_table)
            self._update_values(underscore_table, col_name, values, cell_id_by_name)
            self._record_pending(col_name, target, kind, source_hash)
            return

        # --- ALTER TABLE ADD COLUMN, then UPDATE ---
        safe_col = safe_ident(col_name)
        try:
            self._conn.execute(
                f"ALTER TABLE {safe_ident(underscore_table)} "
                f"ADD COLUMN {safe_col} {sql_type}"
            )
        except Exception as exc:
            raise AnnotationWriterError(
                f"ALTER TABLE add {col_name} on {underscore_table} failed: {exc}"
            )
        self._txn_cols.append((col_name, target))

        self._update_values(underscore_table, col_name, values, cell_id_by_name)
        self._record_pending(col_name, target, kind, source_hash)
        self._refresh_view(target, underscore_table)

    def _create_obs_base_table(
        self,
        underscore_table: str,
        col_name: str,
        sql_type: str,
        cell_rows: List[Tuple[int, str]],
    ) -> None:
        """Create __obsnum or __obscat with the new column; populate one row
        per cell (all NULL in the new column)."""
        self._conn.execute(
            f"CREATE TABLE {safe_ident(underscore_table)} ("
            f"  cell_id INTEGER PRIMARY KEY, "
            f"  {safe_ident(col_name)} {sql_type}"
            f")"
        )
        # Insert (cell_id, NULL) rows
        df = pd.DataFrame({'cell_id': [r[0] for r in cell_rows]})
        df[col_name] = None
        self._conn.register('temp_obs_init', df)
        self._conn.execute(
            f"INSERT INTO {safe_ident(underscore_table)} SELECT * FROM temp_obs_init"
        )
        self._conn.unregister('temp_obs_init')

    def _update_values(
        self,
        underscore_table: str,
        col_name: str,
        values: pd.Series,
        cell_id_by_name: Dict[str, int],
    ) -> None:
        """UPDATE underscore_table SET col = ta.val FROM temp_anno ta WHERE ..."""
        df = pd.DataFrame({
            'cell_id': [cell_id_by_name[str(n)] for n in values.index],
            'val':     list(values.values),
        })
        # Drop rows with None val to leave existing values / NULL in place
        # for cells the annotator didn't write. (For OVERWRITE we want this
        # too — explicit None means "set NULL".)
        self._conn.register('temp_anno', df)
        try:
            self._conn.execute(
                f"UPDATE {safe_ident(underscore_table)} t "
                f"SET {safe_ident(col_name)} = ta.val "
                f"FROM temp_anno ta WHERE t.cell_id = ta.cell_id"
            )
        finally:
            self._conn.unregister('temp_anno')

    def _refresh_view(self, view_name: str, underscore_table: str) -> None:
        """DROP & re-CREATE the obscat/obsnum view so new columns are visible.
        DuckDB binds ``o.*`` at view creation time, so re-CREATE also
        applies the current api-config rename+hide+derive from
        ``self.api_config``. Renamed cols surface under their public
        alias; hidden cols are omitted from the view entirely — that's
        how the API layer enforces "old internal names not accessible
        from outside". Derivation sources may live in the sibling
        __obs* table; look up sibling columns so cross-table CASE
        expressions can reference them."""
        cols = [row[0] for row in self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [underscore_table]
        ).fetchall()]
        sibling_underscore = (
            '__obsnum' if underscore_table.endswith('obscat') else '__obscat'
        )
        try:
            sibling_cols = [row[0] for row in self._conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?", [sibling_underscore]
            ).fetchall()]
        except Exception:
            sibling_cols = []
        self._conn.execute(f"DROP VIEW IF EXISTS {safe_ident(view_name)}")
        self._conn.execute(build_obs_view_sql(
            view_name, underscore_table, cols,
            getattr(self, 'api_config', None),
            sibling_underscore_table=sibling_underscore,
            sibling_columns=sibling_cols,
        ))

    def _record_pending(
        self,
        col_name: str,
        target: str,
        kind: str,
        source_hash: Optional[str],
    ) -> None:
        # If overwriting an existing pending row, replace it.
        self._conn.execute(
            "DELETE FROM __pending_cols WHERE col_name = ?", [col_name]
        )
        self._conn.execute(
            "INSERT INTO __pending_cols "
            "(col_name, target_table, kind, added_at, annotator, run_id, "
            " catalog, source_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                col_name, target, kind, datetime.now(timezone.utc),
                self.annotator, self.run_id, self.catalog, source_hash,
            ],
        )

    def _rollback_txn(self) -> None:
        """ROLLBACK the with-block's transaction (schema, values, views and
        __pending_cols all revert). Falls back to dropping the added columns
        by hand only if ROLLBACK itself fails."""
        if self._conn is None:
            return
        if self._in_transaction:
            try:
                self._conn.execute("ROLLBACK")
                self._in_transaction = False
                self._txn_cols = []
                return
            except Exception as exc:
                print(f"  ⚠ rollback: ROLLBACK failed ({exc}); dropping added columns manually")
                self._in_transaction = False
        if not self._txn_cols:
            return
        for col_name, target in reversed(self._txn_cols):
            underscore_table = f"__{target}"
            try:
                self._conn.execute(
                    f"ALTER TABLE {safe_ident(underscore_table)} "
                    f"DROP COLUMN {safe_ident(col_name)}"
                )
            except Exception as exc:
                print(f"  ⚠ rollback: drop {col_name} from {underscore_table}: {exc}")
            try:
                self._conn.execute(
                    "DELETE FROM __pending_cols WHERE col_name = ?", [col_name]
                )
            except Exception:
                pass
            try:
                self._refresh_view(target, underscore_table)
            except Exception:
                pass
        self._txn_cols = []

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = ? AND table_type = 'BASE TABLE'",
            [name],
        ).fetchone()
        return row is not None

    def _column_names(self, table_name: str) -> List[str]:
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            [table_name],
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Standalone helpers (used by ant-convert, ant-merge-h5ad)
# ---------------------------------------------------------------------------

def read_pending_cols(duckdb_path: Path) -> pd.DataFrame:
    """Read the __pending_cols table. Returns empty DataFrame if not present."""
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        exists = con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = '__pending_cols'"
        ).fetchone()
        if not exists:
            return pd.DataFrame(columns=[
                'col_name', 'target_table', 'kind', 'added_at',
                'annotator', 'run_id', 'catalog', 'source_hash',
            ])
        return con.execute("SELECT * FROM __pending_cols").fetchdf()
    finally:
        con.close()


def pending_state_diagnosis(
    duckdb_path: Path,
    h5ad_obs_keys: set,
) -> Dict[str, Any]:
    """Return diagnosis of a duckdb's pending-column state without raising.

    Used by ant-convert (refuse-on-pending guard) and ant-merge-h5ad
    (sanity check). Performs BOTH the `__pending_cols` table check and an
    independent column diff against the provided h5ad obs keys.

    Returns dict with:
      - clean (bool):           True iff both signals are empty
      - pending_table (DataFrame): contents of __pending_cols (may be empty)
      - duck_obs_cols (Set[str]): all obs cols present in the duckdb
      - only_in_duckdb (List[str]): obs cols in duckdb but not h5ad
                                    (the actual merge work-list)
      - already_merged (List[str]): __pending_cols rows whose col is ALREADY
                                    in h5ad (previous merge half-succeeded)
      - unrecorded (List[str]):  only_in_duckdb cols absent from __pending_cols
                                 (divergent — written without AnnotationWriter)
      - missing_from_duckdb (List[str]): __pending_cols rows for cols NOT in
                                    the duckdb obs (table out-of-sync)
    """
    pending_df = read_pending_cols(duckdb_path)
    try:
        duck_cols = duckdb_obs_columns(duckdb_path)
    except Exception:
        duck_cols = {'obsnum': [], 'obscat': []}
    duck_all = set(duck_cols.get('obsnum', [])) | set(duck_cols.get('obscat', []))
    only_in_duckdb = sorted(duck_all - h5ad_obs_keys)
    recorded = set(pending_df['col_name'].tolist())
    unrecorded = sorted(set(only_in_duckdb) - recorded)
    already_merged = sorted(recorded & h5ad_obs_keys)
    missing_from_duckdb = sorted(recorded - duck_all)
    clean = pending_df.empty and not only_in_duckdb
    return {
        'clean': clean,
        'pending_table': pending_df,
        'duck_obs_cols': duck_all,
        'only_in_duckdb': only_in_duckdb,
        'already_merged': already_merged,
        'unrecorded': unrecorded,
        'missing_from_duckdb': missing_from_duckdb,
    }


def duckdb_obs_columns(duckdb_path: Path) -> Dict[str, List[str]]:
    """Return {'obsnum': [...], 'obscat': [...]} of obs columns in this duckdb.
    Cell-id helper columns are excluded."""
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        out: Dict[str, List[str]] = {}
        for target in ('obsnum', 'obscat'):
            underscore_table = f"__{target}"
            rows = con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? AND column_name NOT IN "
                "('cell_id', 'cell_name')",
                [underscore_table],
            ).fetchall()
            out[target] = sorted(r[0] for r in rows)
        return out
    finally:
        con.close()
