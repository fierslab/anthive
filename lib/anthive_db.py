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
        self.conn = None

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()

    def connect(self):
        """Open database connection"""
        if self.conn is None:
            self.conn = duckdb.connect(str(self.db_path), read_only=True)

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def get_info(self) -> Dict[str, Any]:
        """
        Get database information

        Returns:
            Dictionary with database metadata
        """
        self.connect()

        # Get cell and gene counts
        n_cells = self.conn.execute("SELECT COUNT(*) FROM __cells").fetchone()[0]
        n_genes = self.conn.execute("SELECT COUNT(*) FROM __genes").fetchone()[0]

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
        self.connect()

        # Get all views that match pattern layer__*
        result = self.conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_type = 'VIEW'
            AND table_name LIKE 'layer__%'
        """).fetchall()

        layers = [row[0].replace('layer__', '') for row in result]
        return sorted(layers)

    def _get_embeddings(self) -> List[str]:
        """Get list of available embeddings"""
        self.connect()

        # Get all views that match pattern obsm__*
        result = self.conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_type = 'VIEW'
            AND table_name LIKE 'obsm__%'
        """).fetchall()

        embeddings = [row[0].replace('obsm__', '') for row in result]
        return sorted(embeddings)

    def _get_numerical_fields(self) -> List[str]:
        """Get numerical metadata fields"""
        self.connect()

        # Check if obsnum view exists
        exists = self.conn.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'obsnum'
        """).fetchone()[0]

        if not exists:
            return []

        # Get columns except cell_name and cell_id
        result = self.conn.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'obsnum'
            AND column_name NOT IN ('cell_name', 'cell_id')
        """).fetchall()

        return [row[0] for row in result]

    def _get_categorical_fields(self) -> List[str]:
        """Get categorical metadata fields"""
        self.connect()

        # Check if obscat view exists
        exists = self.conn.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'obscat'
        """).fetchone()[0]

        if not exists:
            return []

        # Get columns except cell_name and cell_id
        result = self.conn.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'obscat'
            AND column_name NOT IN ('cell_name', 'cell_id')
        """).fetchall()

        return [row[0] for row in result]

    def search_genes(self, query: str, case_sensitive: bool = False,
                    limit: Optional[int] = None) -> List[str]:
        """
        Search for genes by substring

        Args:
            query: Search string
            case_sensitive: Case-sensitive search
            limit: Maximum results

        Returns:
            List of matching gene names
        """
        self.connect()

        if case_sensitive:
            where_clause = f"WHERE gene_name LIKE '%{query}%'"
        else:
            where_clause = f"WHERE LOWER(gene_name) LIKE LOWER('%{query}%')"

        limit_clause = f"LIMIT {limit}" if limit else ""

        sql = f"""
            SELECT gene_name
            FROM genes
            {where_clause}
            ORDER BY gene_name
            {limit_clause}
        """

        result = self.conn.execute(sql).fetchall()
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
        self.connect()

        # Check if gene exists
        result = self.conn.execute("""
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
        self.connect()

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
        df = self.conn.execute(sql).fetchdf()
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
        self.connect()

        # Check if embedding exists
        table_name = f"obsm__{embedding_id}"
        exists = self.conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = '{table_name}'
        """).fetchone()[0]

        if not exists:
            raise ValueError(f"Embedding '{embedding_id}' not found")

        # Get all columns
        columns_result = self.conn.execute(f"""
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

        df = self.conn.execute(sql).fetchdf()
        return df

    def execute_sql(self, query: str, limit: Optional[int] = None) -> pd.DataFrame:
        """
        Execute raw SQL query

        Args:
            query: SQL query
            limit: Optional limit to append

        Returns:
            DataFrame with results
        """
        self.connect()

        if limit and 'LIMIT' not in query.upper():
            query = f"{query} LIMIT {limit}"

        df = self.conn.execute(query).fetchdf()
        return df
