"""Anthive Client - Lightweight Python client for Anthive REST API

Simple, secure, and fast client for querying single-cell expression databases.

Works in standard Python, Pyodide, and Streamlit environments.

Requirements:
    - requests (required)
    - pandas (optional, for format='dataframe' support)

Example:
    >>> from antclient import AnthiveClient
    >>> client = AnthiveClient("http://localhost:8080")
    >>> databases = client.get_databases()
    >>> df = client.get_cells("mydb", genes=["CD4", "CD8"], format='dataframe')

Author: Anthive Team
License: MIT
Version: 1.0.0
"""

from typing import Any, Dict, List, Optional, Union
import requests

__version__ = "1.0.0"
__all__ = ["AnthiveClient"]


# Streamlit caching support (optional)
try:
    import streamlit as st
    _STREAMLIT_AVAILABLE = True

    def _cache_short(ttl=60):
        """Short-term cache decorator (active in Streamlit only)"""
        return st.cache_data(ttl=ttl)

    def _cache_long(ttl=3600):
        """Long-term cache decorator (active in Streamlit only)"""
        return st.cache_data(ttl=ttl)
except ImportError:
    _STREAMLIT_AVAILABLE = False

    def _cache_short(ttl=60):
        """Dummy cache decorator (Streamlit not available)"""
        def decorator(func):
            return func
        return decorator

    def _cache_long(ttl=3600):
        """Dummy cache decorator (Streamlit not available)"""
        def decorator(func):
            return func
        return decorator


def _to_dataframe(result: Dict[str, Any], fill_na_genes: bool = True):
    """Convert query result to pandas DataFrame (late import).

    Args:
        result: Query result dict with 'data' and 'columns' keys
        fill_na_genes: If True, fill NaN values with 0 for gene expression columns.
                       Metadata columns are left as NaN. (default: True)

    Returns:
        pandas DataFrame

    Raises:
        ImportError: If pandas is not installed

    Note:
        Gene expression NaN values represent undetected genes (sparse data)
        and should be 0. Metadata NaN values represent missing data and are
        preserved as NaN.
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError(
            "pandas is required for format='dataframe'. "
            "Install with: pip install pandas"
        )

    df = pd.DataFrame(result['data'], columns=result.get('columns', []))

    if fill_na_genes and 'columns' in result:
        # Identify gene columns (not cell_name, not dim_*, not typical metadata)
        # This is heuristic-based: gene names are typically alphanumeric/symbols,
        # not common metadata field names
        import re

        common_metadata = {
            'cell_name', 'cell_id', 'cell_type', 'celltype', 'tissue', 'sample',
            'donor', 'batch', 'condition', 'treatment', 'cluster', 'seurat_clusters',
            'n_genes', 'n_counts', 'nCount_RNA', 'nFeature_RNA', 'percent.mt',
            'percent_mito', 'phase', 'doublet', 'orig.ident'
        }

        for col in df.columns:
            # Skip if it's a known metadata field
            if col in common_metadata:
                continue
            # Skip if it starts with dim_ (embeddings)
            if col.startswith('dim_'):
                continue
            # Skip if it looks like a common metadata pattern
            if any(col.lower().startswith(prefix) for prefix in
                   ['n_', 'percent', 'pct_', 'log', 'total_']):
                continue

            # Assume it's a gene column - fill NaN with 0
            if df[col].dtype in ['float64', 'float32', 'float16']:
                df[col] = df[col].fillna(0)

    return df


class AnthiveClient:
    """Lightweight client for Anthive REST API (Phase 2)

    Provides a simple Python interface to query single-cell expression databases
    served by the Anthive REST API.

    Args:
        base_url: Base URL of the REST API (e.g., "http://localhost:8080")
                  If None, tries ANTHIVE_API_URL environment variable or auto-detects
        timeout: Default timeout for requests in seconds (default: 30)

    Example:
        >>> client = AnthiveClient("http://localhost:8080")
        >>> databases = client.get_databases()
        >>> print(f"Found {len(databases)} databases")

    Environment Variables:
        ANTHIVE_API_URL: Default base URL if not specified

    Browser Auto-detection:
        In Pyodide/browser environments, automatically detects origin
    """

    def __init__(self, base_url: str = None, timeout: int = 30):
        # Auto-detect base_url if not provided
        if base_url is None:
            import os
            base_url = os.environ.get('ANTHIVE_API_URL')
            if base_url is None:
                # Try to detect browser/Pyodide environment
                try:
                    import js  # type: ignore
                    if hasattr(js, 'ANTHIVE_API_URL'):
                        base_url = js.ANTHIVE_API_URL
                    else:
                        # In browser: construct absolute URL from current origin
                        origin = str(js.window.location.origin)
                        base_url = f"{origin}/api"
                except (ImportError, AttributeError, Exception):
                    # Fallback for non-browser environments (direct API access)
                    base_url = "http://localhost:8080"

        self.base_url = base_url.rstrip('/') if base_url else "http://localhost:8080"
        self.timeout = timeout
        self.headers = {}

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """Execute GET request

        Args:
            path: API path (e.g., "/databases")
            params: Optional query parameters

        Returns:
            JSON response

        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}{path}"
        response = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, json: Optional[Dict] = None) -> Any:
        """Execute POST request

        Args:
            path: API path
            json: Optional JSON payload

        Returns:
            JSON response

        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}{path}"
        response = requests.post(url, json=json, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    # ===== Info Endpoints =====

    def get_root(self) -> Dict:
        """Get API root information

        Returns:
            Dict with API name, version, database count, documentation links

        Example:
            >>> info = client.get_root()
            >>> print(f"API version: {info['version']}")
        """
        return self._get('/')

    def get_health(self) -> Dict:
        """Get server health status

        Returns:
            Dict with status, database count, errors, uptime

        Example:
            >>> health = client.get_health()
            >>> print(f"Status: {health['status']}")
        """
        return self._get('/health')

    def get_metrics(self) -> Dict:
        """Get server performance metrics (Phase 2)

        Returns:
            Dict with uptime, request stats, connection pool stats, cache stats

        Example:
            >>> metrics = client.get_metrics()
            >>> print(f"Cache hit rate: {metrics['connection_pool']['hit_rate']:.2%}")
        """
        return self._get('/metrics')

    # ===== Database Discovery =====

    @_cache_short(ttl=60)
    def get_databases(_self, refresh: bool = False, format: str = "list") -> Union[List[Dict], Any]:
        """List all available databases

        Args:
            refresh: Force refresh from disk (default: False)
            format: Output format - "list" (default) or "dataframe" (requires pandas)

        Returns:
            List of database info dicts or DataFrame if format='dataframe'

        Example:
            >>> databases = client.get_databases()
            >>> for db in databases:
            ...     print(f"{db['id']}: {db['n_cells']} cells")
        """
        params = {'refresh': 'true'} if refresh else None
        response = _self._get('/databases', params=params)
        databases = response.get('databases', response) if isinstance(response, dict) else response

        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(databases)
        return databases

    @_cache_short(ttl=60)
    def get_database_info(_self, db_id: str) -> Dict:
        """Get detailed database information

        Args:
            db_id: Database identifier (e.g., "mygroup/mydatabase")

        Returns:
            Dict with n_cells, n_genes, layers, embeddings, metadata_fields, etc.

        Example:
            >>> info = client.get_database_info("Voet2025/slide_st.counts")
            >>> print(f"Cells: {info['n_cells']}, Genes: {info['n_genes']}")
        """
        return _self._get(f'/databases/{db_id}/info')

    # ===== Gene Operations =====

    @_cache_short(ttl=60)
    def search_genes(_self,
                     db_id: str,
                     q: str = "",
                     limit: int = 100,
                     case_sensitive: bool = False) -> List[str]:
        """Search genes by substring

        Args:
            db_id: Database identifier
            q: Search string (empty returns all up to limit)
            limit: Maximum results to return (default: 100)
            case_sensitive: Whether search is case-sensitive (default: False)

        Returns:
            List of matching gene names

        Example:
            >>> genes = client.search_genes("mydb", q="CD4", limit=10)
            >>> print(genes)  # ['CD4', 'CD40', 'CD40LG', ...]
        """
        params = {'q': q, 'limit': limit, 'case_sensitive': case_sensitive}
        response = _self._get(f'/databases/{db_id}/genes', params=params)
        return response.get('genes', response) if isinstance(response, dict) else response

    @_cache_long(ttl=3600)
    def get_gene_info(_self, db_id: str, gene_id: str) -> Dict:
        """Get information about a specific gene

        Args:
            db_id: Database identifier
            gene_id: Gene name/identifier

        Returns:
            Dict with gene info and available layers

        Example:
            >>> info = client.get_gene_info("mydb", "CD4")
            >>> print(info['layers'])
        """
        return _self._get(f'/databases/{db_id}/genes/{gene_id}')

    # ===== Layers and Metadata =====

    @_cache_long(ttl=3600)
    def get_layers(_self, db_id: str) -> List[str]:
        """Get available data layers for a database

        Args:
            db_id: Database identifier

        Returns:
            List of layer names

        Example:
            >>> layers = client.get_layers("mydb")
            >>> print(layers)  # ['X', 'counts', 'normalized']
        """
        response = _self._get(f'/databases/{db_id}/layers')
        return response.get('layers', response) if isinstance(response, dict) else response

    @_cache_long(ttl=3600)
    def get_metadata_fields(_self, db_id: str) -> Dict:
        """List all metadata fields with types

        Args:
            db_id: Database identifier

        Returns:
            Dict with 'numerical' and 'categorical' field lists

        Example:
            >>> fields = client.get_metadata_fields("mydb")
            >>> print(f"Numerical: {fields['numerical']}")
            >>> print(f"Categorical: {fields['categorical']}")
        """
        return _self._get(f'/databases/{db_id}/metadata/fields')

    # ===== Embeddings =====

    @_cache_long(ttl=3600)
    def get_embeddings(_self, db_id: str) -> List[str]:
        """Get available embeddings for a database

        Args:
            db_id: Database identifier

        Returns:
            List of embedding names

        Example:
            >>> embeddings = client.get_embeddings("mydb")
            >>> print(embeddings)  # ['Umap', 'Pca']
        """
        response = _self._get(f'/databases/{db_id}/embeddings')
        return response.get('embeddings', response) if isinstance(response, dict) else response

    def get_embedding_data(self,
                          db_id: str,
                          embedding_id: str,
                          n_dims: int = 2,
                          limit: Optional[int] = None,
                          format: str = "json") -> Union[Dict, Any]:
        """Get embedding coordinates

        Args:
            db_id: Database identifier
            embedding_id: Embedding name (e.g., "Umap")
            n_dims: Number of dimensions to return (default: 2, 0 = all)
            limit: Maximum cells to return
            format: Output format - "json" (default) or "dataframe" (requires pandas)

        Returns:
            Dict with embedding data or DataFrame if format='dataframe'

        Example:
            >>> umap = client.get_embedding_data("mydb", "Umap", n_dims=2, limit=1000)
            >>> df = client.get_embedding_data("mydb", "Umap", format='dataframe')
        """
        params = {'n_dims': n_dims}
        if limit is not None:
            params['limit'] = limit

        result = self._get(f'/databases/{db_id}/embeddings/{embedding_id}', params=params)

        if format == 'dataframe':
            return _to_dataframe(result)
        return result

    # ===== Data Retrieval =====

    def get_cells(self,
                  db_id: str,
                  genes: Optional[List[str]] = None,
                  metadata: Optional[Union[List[str], str]] = None,
                  layer: str = "X",
                  filters: Optional[List[str]] = None,
                  limit: Optional[int] = None,
                  format: str = "json",
                  fill_na: bool = True) -> Union[Dict, Any, str, bytes]:
        """Get cell table with expression and metadata

        Args:
            db_id: Database identifier
            genes: List of gene names to include (None = no genes)
            metadata: List of metadata fields, "*" for all, None for none
            layer: Data layer to query (default: "X")
            filters: List of filters in format "field:value" or "field:min,max"
            limit: Maximum cells to return (None = all matching cells)
            format: Output format - "json" (default), "dataframe", "csv", or "parquet"
            fill_na: For DataFrame format, fill NaN in gene columns with 0 (default: True)
                     Gene expression NaN = undetected = 0. Metadata NaN preserved.

        Returns:
            - "json": Dict with n_cells, columns, data
            - "dataframe": pandas DataFrame (requires pandas)
            - "csv": CSV string
            - "parquet": bytes

        Example:
            >>> # Get expression data
            >>> result = client.get_cells("mydb", genes=["CD4", "CD8"], limit=100)
            >>>
            >>> # Get as DataFrame
            >>> df = client.get_cells("mydb",
            ...                       genes=["CD4"],
            ...                       metadata=["cell_type", "tissue"],
            ...                       format='dataframe')
            >>>
            >>> # With filters
            >>> df = client.get_cells("mydb",
            ...                       genes=["CD4"],
            ...                       metadata=["cell_type"],
            ...                       filters=["cell_type:T-cell", "n_counts:1000,5000"],
            ...                       format='dataframe')
            >>>
            >>> # Export to CSV
            >>> csv_data = client.get_cells("mydb", genes=["CD4"], format='csv')
        """
        # Handle dataframe format
        api_format = 'json' if format == 'dataframe' else format
        params = {'layer': layer, 'format': api_format}

        if genes:
            params['genes'] = ','.join(genes)

        if metadata:
            if isinstance(metadata, list):
                params['metadata'] = ','.join(metadata)
            else:
                params['metadata'] = metadata  # Already a string like "*"

        if filters:
            params['filter'] = filters  # Multiple filter params

        if limit is not None:
            params['limit'] = limit

        if api_format == 'json':
            result = self._get(f'/databases/{db_id}/cells', params=params)
            if format == 'dataframe':
                return _to_dataframe(result, fill_na_genes=fill_na)
            return result
        else:
            # For csv/parquet, return raw response content
            url = f"{self.base_url}/databases/{db_id}/cells"
            response = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            return response.content if format == 'parquet' else response.text

    # ===== SQL Queries =====

    def execute_sql(self,
                    db_id: str,
                    query: str,
                    limit: Optional[int] = None,
                    format: str = "json",
                    fill_na: bool = True) -> Union[Dict, Any]:
        """Execute raw SQL query

        Args:
            db_id: Database identifier
            query: SQL query string
            limit: Optional row limit
            format: Output format - "json" (default) or "dataframe" (requires pandas)
            fill_na: For DataFrame format, fill NaN in gene columns with 0 (default: True)

        Returns:
            Dict with query, n_rows, columns, data or DataFrame if format='dataframe'

        Example:
            >>> result = client.execute_sql("mydb",
            ...                             "SELECT cell_name, celltype FROM obscat LIMIT 10")
            >>> df = client.execute_sql("mydb",
            ...                         "SELECT * FROM obsnum WHERE n_counts > 5000",
            ...                         format='dataframe')
        """
        payload = {'query': query}
        if limit is not None:
            payload['limit'] = limit
        result = self._post(f'/databases/{db_id}/query/sql', json=payload)

        if format == 'dataframe':
            return _to_dataframe(result, fill_na_genes=fill_na)
        return result

    # ===== Admin Operations =====

    def rescan_databases(self) -> Dict:
        """Force rescan of data folder and reload metadata cache

        Returns:
            Dict with status, previous_count, current_count, added, removed, databases

        Example:
            >>> result = client.rescan_databases()
            >>> print(f"Databases: {result['previous_count']} -> {result['current_count']}")
        """
        return self._post('/admin/rescan')

    # ===== Convenience Methods =====

    def list_database_ids(self) -> List[str]:
        """Get list of database IDs

        Returns:
            List of database ID strings

        Example:
            >>> ids = client.list_database_ids()
            >>> print(ids)  # ['group1/db1', 'group2/db2', ...]
        """
        databases = self.get_databases()
        return [db['id'] for db in databases]

    def get_all_genes(self, db_id: str) -> List[str]:
        """Get all gene names for a database

        Args:
            db_id: Database identifier

        Returns:
            List of all gene names (may be large!)

        Example:
            >>> genes = client.get_all_genes("mydb")
            >>> print(f"Total genes: {len(genes)}")
        """
        # Use search with empty query and high limit
        return self.search_genes(db_id, q="", limit=100000)

    def __repr__(self):
        """String representation"""
        return f"AnthiveClient(base_url='{self.base_url}')"


# Backward compatibility alias
AntClient = AnthiveClient
