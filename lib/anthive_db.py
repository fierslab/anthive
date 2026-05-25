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

    def _get_numerical_fields(self) -> List[str]:
        """Get numerical metadata fields"""

        # Check if obsnum view exists
        exists = (self._conn.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'obsnum'
        """).fetchone() or (0,))[0]

        if not exists:
            return []

        # Get columns except cell_name and cell_id
        result = self._conn.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'obsnum'
            AND column_name NOT IN ('cell_name', 'cell_id')
        """).fetchall()

        return [row[0] for row in result]

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

        # Build SELECT clause
        select_parts = ["c.cell_name"]

        # Add gene expression columns if requested
        if genes:
            layer_table = safe_ident(f"layer__{layer}")
            for gene in genes:
                # NOTE: gene-name string interpolation here is a separate
                # known issue (see QA findings) — needs callers to be
                # refactored to thread bound parameters through. The layer
                # identifier is now safely quoted.
                gene_alias = safe_ident(gene)
                select_parts.append(f"""
                    (SELECT value
                     FROM {layer_table}
                     WHERE cell_name = c.cell_name
                     AND gene_name = '{gene}') AS {gene_alias}
                """)

        # Add metadata columns if requested
        metadata_joins = []
        if metadata_fields:
            if metadata_fields == ["*"]:
                # Add all numerical fields
                num_fields = self._get_numerical_fields()
                for field in num_fields:
                    select_parts.append(f'n."{field}"')
                if num_fields:
                    metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")

                # Add all categorical fields
                cat_fields = self._get_categorical_fields()
                for field in cat_fields:
                    select_parts.append(f'cat."{field}"')
                if cat_fields:
                    metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")
            else:
                # Add specific fields
                num_fields = self._get_numerical_fields()
                cat_fields = self._get_categorical_fields()

                has_num = any(f in num_fields for f in metadata_fields)
                has_cat = any(f in cat_fields for f in metadata_fields)

                if has_num:
                    metadata_joins.append("LEFT JOIN obsnum n ON c.cell_name = n.cell_name")
                if has_cat:
                    metadata_joins.append("LEFT JOIN obscat cat ON c.cell_name = cat.cell_name")

                for field in metadata_fields:
                    if field in num_fields:
                        select_parts.append(f'n."{field}"')
                    elif field in cat_fields:
                        select_parts.append(f'cat."{field}"')

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

                if isinstance(value, tuple) and len(value) == 2:
                    # Range filter: field:(min, max)
                    where_parts.append(f'{prefix}."{field}" BETWEEN {value[0]} AND {value[1]}')
                elif isinstance(value, list):
                    # IN list: field:[v1, v2, ...]
                    quoted = ', '.join(f"'{v}'" for v in value)
                    where_parts.append(f'{prefix}."{field}" IN ({quoted})')
                else:
                    # Exact match
                    if isinstance(value, str):
                        where_parts.append(f'{prefix}."{field}" = \'{value}\'')
                    else:
                        where_parts.append(f'{prefix}."{field}" = {value}')

        # Build inner SQL (filters applied here, sampling wraps this)
        inner_sql = f"""
            SELECT {', '.join(select_parts)}
            FROM cells c
            {' '.join(metadata_joins)}
        """

        if where_parts:
            inner_sql += f" WHERE {' AND '.join(where_parts)}"

        # Wrap in USING SAMPLE when requested (applied after filters)
        if sample:
            sql = f"SELECT * FROM ({inner_sql}) USING SAMPLE {sample} ROWS (reservoir)"
        else:
            sql = inner_sql

        if limit:
            sql += f" LIMIT {limit}"

        # Execute and return
        df = self._conn.execute(sql).fetchdf()
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
                       case_sensitive: bool = True) -> List[Dict[str, Any]]:
        """
        Get expression statistics for one or more genes.

        Statistics over all cells (zeros included): mean, std.
        Statistics over expressing cells only: mean, std, median, q10, q90, max.

        When the database has a ``__gene_bins`` table (built by ant-duckdb-check for
        databases with a default_layer) two additional ranking fields are included:

        - ``rank_mean`` (0–1): percentile rank of this gene's mean expression among
          all genes in the dataset (0 = lowest, 1 = highest).
        - ``scaled_mean`` (0–1): mean expression rescaled and clipped to the
          [q05, q95] range of all gene means, giving an intuitive abundance score.

        Both metrics are derived from ``__gene_bins.mean_expr``, which is the
        mean expression over all cells (zeros included) using the default layer.
        Databases without ``__gene_bins`` do not include these fields.

        Args:
            gene_names: List of gene names
            layer: Layer name (default: 'X')
            case_sensitive: If False, resolve gene names case-insensitively (default: True).
                            Results include 'gene_found' with the actual stored name when
                            it differs from the queried name.

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

        # ── Step 2: batch rank/scale from __gene_bins (one query total) ───────
        rank_ctx: Dict[str, Dict[str, float]] = {}   # stored_name → rank fields
        has_bins = (self._conn.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = '__gene_bins'
        """).fetchone() or (0,))[0]

        if has_bins:
            q_row = self._conn.execute("""
                SELECT QUANTILE_CONT(mean_expr, 0.05), QUANTILE_CONT(mean_expr, 0.95)
                FROM __gene_bins
            """).fetchone() or (0.0, 1.0)
            q05, q95 = float(q_row[0]), float(q_row[1])
            q_range = max(q95 - q05, 1e-9)

            stored_names = [s for s in resolved.values() if s is not None]
            if stored_names:
                placeholders = ','.join('?' * len(stored_names))
                bin_rows = self._conn.execute(f"""
                    WITH ranked AS (
                        SELECT gene_id,
                               PERCENT_RANK() OVER (ORDER BY mean_expr) AS rank_mean,
                               mean_expr
                        FROM __gene_bins
                    )
                    SELECT g.gene_name, r.rank_mean, r.mean_expr
                    FROM ranked r
                    JOIN __genes g USING (gene_id)
                    WHERE g.gene_name IN ({placeholders})
                """, stored_names).fetchall()

                for gname, rank_mean, mean_expr in bin_rows:
                    scaled = max(0.0, min(1.0, (float(mean_expr) - q05) / q_range))
                    rank_ctx[gname] = {
                        'rank_mean':   round(float(rank_mean), 4),
                        'scaled_mean': round(scaled, 4),
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

            layer_table = safe_ident(f"layer__{layer}")
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
                FROM {layer_table}
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
        layer_table = safe_ident(f"__layer__{layer}")

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
                -- SUM/n_genes (not AVG) because sparse table omits zeros
                SELECT cell_id,
                       SUM(value) / {len(genes_found)}.0 AS m
                FROM {layer_table}
                WHERE gene_id IN ({gene_id_list})
                GROUP BY cell_id
            ),
            control_avg AS (
                SELECT cell_id,
                       SUM(value) / (SELECT n FROM n_control_total)::DOUBLE AS c
                FROM {layer_table}
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

_ANNO_COL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_anno_col_name(col_name: str) -> None:
    """Stricter than safe_ident — annotator-created columns must be bare
    identifiers (letters, digits, underscores; start with letter/underscore).
    No quoting needed in raw SQL anywhere."""
    if not isinstance(col_name, str) or not _ANNO_COL_RE.fullmatch(col_name):
        raise ValueError(
            f"Annotation column name must match {_ANNO_COL_RE.pattern!r} "
            f"(bare SQL identifier); got {col_name!r}"
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
    url = f"{base_url}{path}"
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
