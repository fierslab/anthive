"""
Database access layer for Anthive DuckDB files

Provides a clean interface to query DuckDB databases created by ant-convert
"""

import fcntl
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Literal
import duckdb
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
            "SELECT DISTINCT source, catalog, module FROM read_parquet(?)",
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

    def obs_summary(self, top_n_categories: int = 3) -> Dict[str, Dict[str, Any]]:
        """Per-obs-column summary stats for the dataset-page right-column.

        Uses DuckDB's ``SUMMARIZE`` on ``obscat`` and ``obsnum`` for the
        cheap aggregates, plus two follow-up queries to fill in the stats
        SUMMARIZE doesn't carry:

          - q05 / q95 on numerical columns (one bulk SELECT with
            quantile_cont calls — one scan over obsnum).
          - top-N value frequency on categorical columns (one
            small GROUP BY per column; per-column scan but fast on
            obscat which is narrow).

        Returns a dict keyed on column name:
          - categorical: {'type': 'cat', 'n_unique', 'pct_missing',
                          'top': [{value, count, pct}, ...]}
          - numerical:   {'type': 'num', 'min', 'q05', 'mean', 'median',
                          'q95', 'max', 'pct_missing'}

        Missing values are reported as ``pct_missing`` (0-100). Empty
        when neither obscat nor obsnum exists in this dataset.
        """
        out: Dict[str, Dict[str, Any]] = {}
        skip_cols = {'cell_name', 'cell_id'}

        def _coerce_num(value):
            if value is None:
                return None
            try:
                return float(value)
            except (ValueError, TypeError):
                return None

        for view_name, kind in (('obscat', 'cat'), ('obsnum', 'num')):
            exists = (self._conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                [view_name],
            ).fetchone() or (0,))[0]
            if not exists:
                continue

            try:
                rows = self._conn.execute(
                    f"SUMMARIZE SELECT * FROM {view_name}"
                ).fetchall()
            except Exception:
                continue

            cols = [
                desc[0] for desc in self._conn.execute(
                    f"SUMMARIZE SELECT * FROM {view_name}"
                ).description
            ]

            # Find the indices we care about — SUMMARIZE's column set is
            # stable in DuckDB but we look them up by name to be robust.
            try:
                ix_name = cols.index('column_name')
                ix_min = cols.index('min')
                ix_max = cols.index('max')
                ix_q50 = cols.index('q50')
                ix_avg = cols.index('avg')
                ix_unique = cols.index('approx_unique')
                ix_nullpct = cols.index('null_percentage')
            except ValueError:
                continue

            # Collect the per-column rows + the column-name list (we need
            # it for the follow-up q05/q95 + top-categories queries).
            col_names: list = []
            for row in rows:
                name = row[ix_name]
                if name in skip_cols:
                    continue
                col_names.append(name)
                null_pct_raw = row[ix_nullpct]
                pct_missing = float(null_pct_raw) if null_pct_raw is not None else 0.0
                if kind == 'cat':
                    out[name] = {
                        'type': 'cat',
                        'n_unique': int(row[ix_unique]) if row[ix_unique] is not None else 0,
                        'pct_missing': round(pct_missing, 2),
                        'top': [],   # populated below
                    }
                else:
                    out[name] = {
                        'type': 'num',
                        'min': _coerce_num(row[ix_min]),
                        'max': _coerce_num(row[ix_max]),
                        'median': _coerce_num(row[ix_q50]),
                        'mean': _coerce_num(row[ix_avg]),
                        # q05 / q95 are filled in by the follow-up scan below.
                        'q05': None, 'q95': None,
                        'pct_missing': round(pct_missing, 2),
                    }

            # ─── follow-up #1: q05 / q95 on numerical columns (bulk) ───
            if kind == 'num' and col_names:
                quant_exprs = []
                for name in col_names:
                    ident = safe_ident(name)
                    quant_exprs.append(f'quantile_cont({ident}, 0.05)')
                    quant_exprs.append(f'quantile_cont({ident}, 0.95)')
                try:
                    quant_row = self._conn.execute(
                        f"SELECT {', '.join(quant_exprs)} FROM {view_name}"
                    ).fetchone()
                except Exception:
                    quant_row = None
                if quant_row is not None:
                    for idx, name in enumerate(col_names):
                        out[name]['q05'] = _coerce_num(quant_row[idx * 2])
                        out[name]['q95'] = _coerce_num(quant_row[idx * 2 + 1])

            # ─── follow-up #2: top-N categorical values per column ───
            # Per-column GROUP BY query. obscat is narrow and DuckDB is
            # fast; with ~50 categorical columns this completes in
            # ~hundreds of ms. The endpoint is called once per dataset-
            # page load, not per user interaction.
            if kind == 'cat' and col_names and top_n_categories > 0:
                # Total non-missing count (per column) needed for `pct`.
                # We re-derive it from pct_missing instead of issuing
                # another COUNT — saves a roundtrip.
                total_rows = (self._conn.execute(
                    f"SELECT COUNT(*) FROM {view_name}"
                ).fetchone() or (0,))[0]
                for name in col_names:
                    ident = safe_ident(name)
                    try:
                        rows_top = self._conn.execute(
                            f"SELECT {ident}, COUNT(*) AS n "
                            f"FROM {view_name} WHERE {ident} IS NOT NULL "
                            f"GROUP BY {ident} ORDER BY n DESC LIMIT ?",
                            [top_n_categories],
                        ).fetchall()
                    except Exception:
                        continue
                    non_missing = max(
                        0,
                        int(total_rows * (1.0 - out[name]['pct_missing'] / 100.0)),
                    ) if total_rows else 0
                    top_items = []
                    for value, count in rows_top:
                        pct = (100.0 * count / non_missing) if non_missing else 0.0
                        top_items.append({
                            'value': str(value) if value is not None else None,
                            'count': int(count),
                            'pct': round(pct, 2),
                        })
                    out[name]['top'] = top_items

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
                select_parts.append(f"""
                    (SELECT value
                     FROM {layer_src} _l
                     WHERE _l.cell_name = c.cell_name
                     AND _l.gene_name = ?) AS {gene_alias}
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
                    select_parts.append(f'n."{field}"')
                if num_fields_real:
                    metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")

                cat_fields = self._get_categorical_fields()
                for field in cat_fields:
                    select_parts.append(f'cat."{field}"')
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
                        select_parts.append(f'n."{field}"')
                    elif field in cat_set:
                        select_parts.append(f'cat."{field}"')
                    elif field in score_index:
                        score_fields_to_merge.append(field)

        # Build WHERE clause from filters
        where_parts = []
        if filters:
            num_fields = self._get_numerical_fields()
            cat_fields = self._get_categorical_fields()

            # Ensure we have joins for filtered fields
            needs_num_join = any(f in num_fields for f in filters.keys())
            needs_cat_join = any(f in cat_fields for f in filters.keys())

            if needs_num_join and "LEFT JOIN obsnum n" not in ' '.join(metadata_joins):
                metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
            if needs_cat_join and "LEFT JOIN obscat cat" not in ' '.join(metadata_joins):
                metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

            for field, value in filters.items():
                if field in num_fields:
                    prefix = 'n'
                elif field in cat_fields:
                    prefix = 'cat'
                else:
                    continue

                # `field` was validated against num_fields / cat_fields (which
                # come from the duckdb schema), so safe_ident() will accept it
                # and produce a properly quoted identifier.
                col_sql = safe_ident(field)

                if isinstance(value, tuple) and len(value) == 2:
                    # Range filter: field:(min, max)
                    where_parts.append(f'{prefix}.{col_sql} BETWEEN ? AND ?')
                    params.extend([value[0], value[1]])
                elif isinstance(value, list):
                    # IN list: field:[v1, v2, ...]
                    placeholders = ', '.join(['?'] * len(value))
                    where_parts.append(f'{prefix}.{col_sql} IN ({placeholders})')
                    params.extend(value)
                else:
                    where_parts.append(f'{prefix}.{col_sql} = ?')
                    params.append(value)

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

        # Merge in any parquet-sidecar score columns the caller asked for.
        # We fetch per column so predicate pushdown in DuckDB keeps the read
        # narrow; then merge on cell_name so downstream code doesn't have to
        # know the difference.
        if score_fields_to_merge:
            for virtual_col in score_fields_to_merge:
                score_df = self._fetch_score_values(virtual_col)
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

        # Check if embedding exists
        table_name = f"obsm__{embedding_id}"
        exists = (self._conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = '{table_name}'
        """).fetchone() or (0,))[0]

        if not exists:
            raise ValueError(f"Embedding '{embedding_id}' not found")

        # Get all columns
        columns_result = self._conn.execute(f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = '{table_name}'
            ORDER BY ordinal_position
        """).fetchall()

        all_cols = [row[0] for row in columns_result]

        # Select columns based on n_dims
        if n_dims is None:
            # All dimensions
            select_cols = all_cols
        else:
            # First n_dims + cell_name
            dim_cols = [c for c in all_cols if c.startswith('dim_')][:n_dims]
            select_cols = ['cell_name'] + dim_cols

        limit_clause = f"LIMIT {limit}" if limit else ""

        # Build optional WHERE clause from metadata filters
        filter_where = ""
        if filters:
            num_fields = self._get_numerical_fields()
            cat_fields = self._get_categorical_fields()
            sub_joins = []
            sub_where = []
            if any(f in num_fields for f in filters):
                sub_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
            if any(f in cat_fields for f in filters):
                sub_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")
            for field, value in filters.items():
                if field in num_fields:
                    prefix = 'n'
                elif field in cat_fields:
                    prefix = 'cat'
                else:
                    continue
                if isinstance(value, tuple) and len(value) == 2:
                    sub_where.append(f'{prefix}."{field}" BETWEEN {value[0]} AND {value[1]}')
                elif isinstance(value, list):
                    quoted = ', '.join(f"'{v}'" for v in value)
                    sub_where.append(f'{prefix}."{field}" IN ({quoted})')
                else:
                    if isinstance(value, str):
                        sub_where.append(f'{prefix}."{field}" = \'{value}\'')
                    else:
                        sub_where.append(f'{prefix}."{field}" = {value}')
            if sub_where:
                subquery = (
                    f"SELECT c.cell_name FROM __cells c "
                    f"{' '.join(sub_joins)} "
                    f"WHERE {' AND '.join(sub_where)}"
                )
                filter_where = f"WHERE cell_name IN ({subquery})"

        if sample:
            inner = f"SELECT {', '.join(select_cols)} FROM {table_name} {filter_where}"
            sql = f"SELECT * FROM ({inner}) USING SAMPLE {sample} ROWS (reservoir) {limit_clause}"
        else:
            sql = f"""
                SELECT {', '.join(select_cols)}
                FROM {table_name}
                {filter_where}
                {limit_clause}
            """

        df = self._conn.execute(sql).fetchdf()
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

        # ── Step 1: resolve stored gene names (single lookup per gene) ────────
        resolved: Dict[str, Optional[str]] = {}   # queried_name → stored_name or None
        for gene_name in gene_names:
            if case_sensitive:
                row = self._conn.execute(
                    "SELECT gene_name FROM genes WHERE gene_name = ?", [gene_name]
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT gene_name FROM genes WHERE LOWER(gene_name) = LOWER(?)", [gene_name]
                ).fetchone()
            resolved[gene_name] = row[0] if row else None

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

        # ── Step 3: per-gene expression statistics ────────────────────────────
        results = []
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

            # Local parquet resolution — see _layer_source_with_names.
            layer_src = self._layer_source_with_names(layer)
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
                results.append({**base,
                    'n_nonzero': 0, 'pct_nonzero': 0.0,
                    'mean': 0.0, 'std': 0.0,
                    'mean_nonzero': None, 'std_nonzero': None,
                    'median_nonzero': None, 'q10_nonzero': None, 'q90_nonzero': None,
                    'max': 0.0,
                })
                continue

            n_nonzero, sum_nz, sum_sq_nz, mean_nz, std_nz, median_nz, q10_nz, q90_nz, max_val = row

            mean_all = float(sum_nz) / n_cells
            var_all = max(0.0, float(sum_sq_nz) / n_cells - mean_all ** 2)
            std_all = math.sqrt(var_all)

            cv = round(std_all / mean_all, 4) if mean_all > 0 else None

            results.append({**base,
                'n_nonzero': int(n_nonzero),
                'pct_nonzero': round(n_nonzero * 100.0 / n_cells, 2),
                'mean': round(mean_all, 6),
                'std': round(std_all, 6),
                'cv': cv,
                'mean_nonzero': round(float(mean_nz), 6),
                'std_nonzero': round(float(std_nz), 6),
                'median_nonzero': round(float(median_nz), 6),
                'q10_nonzero': round(float(q10_nz), 6),
                'q90_nonzero': round(float(q90_nz), 6),
                'max': round(float(max_val), 6),
            })

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

        Args:
            module_genes: List of gene names in the module.
            layer: Expression layer (default 'X'). Must be log-normalized.
            n_control: Control genes sampled per expression bin (default 100).
            seed: Integer seed for reproducible control gene selection.

        Returns:
            Tuple of (scores_df, meta_dict).
            scores_df: DataFrame with columns [cell_name, module_score].
            meta_dict: Dict with genes_found, genes_missing, n_control_genes.
        """
        self.require_gene_bins()

        # Resolve gene names → integer IDs (parameterized, no SQL injection)
        placeholders = ', '.join('?' for _ in module_genes)
        found_rows = self._conn.execute(
            f"SELECT gene_name, gene_id FROM __genes WHERE gene_name IN ({placeholders})",
            module_genes,
        ).fetchall()
        found_map = {name: gid for name, gid in found_rows}
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
            SELECT
                cl.cell_name,
                COALESCE(ma.m, 0.0) - COALESCE(ca.c, 0.0) AS module_score
            FROM __cells cl
            LEFT JOIN module_avg  ma ON cl.cell_id = ma.cell_id
            LEFT JOIN control_avg ca ON cl.cell_id = ca.cell_id
            WHERE ma.cell_id IS NOT NULL OR ca.cell_id IS NOT NULL
        """).fetchdf()

        meta = {
            'genes_found': genes_found,
            'genes_missing': genes_missing,
            'n_control_genes': int(n_control_genes),
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

    On exception inside the `with` block, columns added during this transaction
    are dropped. On clean exit without `.commit()`, also rolls back.

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
        except Exception:
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
        """Mark the transaction as committed; nothing to flush — DuckDB
        DDL is autocommit. Just clears the rollback list and seals."""
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
        DuckDB binds `o.*` at view creation time."""
        self._conn.execute(f"DROP VIEW IF EXISTS {safe_ident(view_name)}")
        self._conn.execute(
            f"CREATE VIEW {safe_ident(view_name)} AS "
            f"SELECT c.cell_name, o.* "
            f"FROM {safe_ident(underscore_table)} o "
            f"JOIN __cells c ON o.cell_id = c.cell_id"
        )

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
        """Drop columns added in this transaction; clear their pending rows."""
        if self._conn is None or not self._txn_cols:
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
