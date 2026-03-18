"""
Database access layer for Anthive DuckDB files

Provides a clean interface to query DuckDB databases created by ant-convert
"""

from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import duckdb
import pandas as pd


class AnthiveDB:
    """Interface to a single Anthive DuckDB database"""

    def __init__(self, db_path: Path):
        """
        Initialize database connection

        Args:
            db_path: Path to .duckdb file
        """
        self.db_path = db_path
        self.conn: Optional[duckdb.DuckDBPyConnection] = None

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
        if self.conn:
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
                       limit: Optional[int] = None) -> pd.DataFrame:
        """
        Get cell table with expression and metadata

        Args:
            genes: List of gene names to include
            metadata_fields: List of metadata fields, ["*"] for all, None for none
            layer: Layer name
            filters: Dictionary of field: value/range filters
            limit: Maximum number of cells

        Returns:
            DataFrame with cell data
        """

        # Build SELECT clause
        select_parts = ["c.cell_name"]

        # Add gene expression columns if requested
        if genes:
            for gene in genes:
                # Use subquery to get expression value for this gene
                select_parts.append(f"""
                    (SELECT value
                     FROM layer__{layer}
                     WHERE cell_name = c.cell_name
                     AND gene_name = '{gene}') AS "{gene}"
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
                    # Range filter
                    where_parts.append(f'{prefix}."{field}" BETWEEN {value[0]} AND {value[1]}')
                else:
                    # Exact match
                    if isinstance(value, str):
                        where_parts.append(f'{prefix}."{field}" = \'{value}\'')
                    else:
                        where_parts.append(f'{prefix}."{field}" = {value}')

        # Build SQL
        sql = f"""
            SELECT {', '.join(select_parts)}
            FROM cells c
            {' '.join(metadata_joins)}
        """

        if where_parts:
            sql += f" WHERE {' AND '.join(where_parts)}"

        if limit:
            sql += f" LIMIT {limit}"

        # Execute and return
        df = self._conn.execute(sql).fetchdf()
        return df

    def get_embedding_data(self, embedding_id: str,
                          limit: Optional[int] = None,
                          n_dims: Optional[int] = 2) -> pd.DataFrame:
        """
        Get embedding coordinates

        Args:
            embedding_id: Embedding name
            limit: Maximum cells
            n_dims: Number of dimensions (None = all)

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

        sql = f"""
            SELECT {', '.join(select_cols)}
            FROM {table_name}
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

        results = []
        for gene_name in gene_names:
            # Resolve stored gene name
            if case_sensitive:
                lookup = self._conn.execute(
                    "SELECT gene_name FROM genes WHERE gene_name = ?", [gene_name]
                ).fetchone()
            else:
                lookup = self._conn.execute(
                    "SELECT gene_name FROM genes WHERE LOWER(gene_name) = LOWER(?)", [gene_name]
                ).fetchone()

            if not lookup:
                results.append({'gene': gene_name, 'error': 'not found'})
                continue

            stored_name = lookup[0]

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
                FROM layer__{layer}
                WHERE gene_name = ?
            """, [stored_name]).fetchone()

            base = {'gene': gene_name, 'layer': layer, 'n_cells': n_cells}
            if stored_name != gene_name:
                base['gene_found'] = stored_name

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

            results.append({**base,
                'n_nonzero': int(n_nonzero),
                'pct_nonzero': round(n_nonzero * 100.0 / n_cells, 2),
                'mean': round(mean_all, 6),
                'std': round(std_all, 6),
                'mean_nonzero': round(float(mean_nz), 6),
                'std_nonzero': round(float(std_nz), 6),
                'median_nonzero': round(float(median_nz), 6),
                'q10_nonzero': round(float(q10_nz), 6),
                'q90_nonzero': round(float(q90_nz), 6),
                'max': round(float(max_val), 6),
            })

        return results

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
