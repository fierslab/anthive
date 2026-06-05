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
    >>> df = client.get_cells("LC5/SPM", genes=["CD4", "CD8"], format='dataframe')

Author: Anthive Team
License: MIT
Version: 1.4.0
"""

from typing import Any, Dict, List, Optional, Union
import requests

__version__ = "1.4.0"
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

    def __init__(self,
                 base_url: Optional[str] = None,
                 timeout: int = 30,
                 auth: Optional[Any] = None):
        """Create an Anthive REST client.

        Args:
            base_url: e.g. ``"http://localhost:8080"`` or ``"https://my.host/api"``.
                When None, falls back to the ``ANTHIVE_API_URL`` env var, then
                to a browser-side ``js.window.location.origin``, then to
                ``http://localhost:8080``.
            timeout: per-request timeout in seconds.
            auth: HTTP authentication, passed straight through to ``requests``.
                Use a ``(user, password)`` tuple for HTTP Basic auth, or any
                ``requests.auth.AuthBase`` subclass.  Required when the server
                is behind a Caddy basic-auth gate (see ``config/caddy-auth.example``).
        """
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
                        base_url = f"{origin}/api/"
                except (ImportError, AttributeError, Exception):
                    # Fallback for non-browser environments (direct API access)
                    base_url = "http://localhost:8080"

        self.base_url = base_url.rstrip('/') if base_url else "http://localhost:8080"
        self.timeout = timeout
        self.headers = {}
        self.auth = auth

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """GET → JSON.  Raises ``requests.HTTPError`` on non-2xx."""
        url = f"{self.base_url}{path}"
        response = requests.get(url, params=params, headers=self.headers,
                                timeout=self.timeout, auth=self.auth)
        response.raise_for_status()
        return response.json()

    def _get_raw(self, path: str, params: Optional[Dict] = None) -> 'requests.Response':
        """GET → raw ``requests.Response`` (for endpoints that return non-JSON
        bytes, e.g. the plot endpoint).  Caller is responsible for reading
        ``.content`` / ``.headers`` and for the HTTP status check (a leading
        ``raise_for_status()`` is done here so non-2xx already raises)."""
        url = f"{self.base_url}{path}"
        response = requests.get(url, params=params, headers=self.headers,
                                timeout=self.timeout, auth=self.auth)
        response.raise_for_status()
        return response

    def _post(self, path: str, json: Optional[Dict] = None) -> Any:
        """POST → JSON.  Raises ``requests.HTTPError`` on non-2xx."""
        url = f"{self.base_url}{path}"
        response = requests.post(url, json=json, headers=self.headers,
                                 timeout=self.timeout, auth=self.auth)
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
        """Get server health + speed signal.

        Returns:
            Dict with::

                {
                    "status": "healthy",
                    "databases": 98,
                    "database_errors": 0,
                    "uptime_seconds": 3600,
                    "mean_response_ms": 126.2,   # null until n_samples > 0
                    "p50_response_ms":  81.1,    # null until n_samples > 0
                    "n_samples":  31             # last-1024 non-probe requests
                }

            ``mean_response_ms`` / ``p50_response_ms`` are server-side
            wall-clock means over the last 1024 non-probe requests.
            Lower = faster.  Use them to pick between replicas — see
            :meth:`pick_fastest` for a ready-made helper.

        Example:
            >>> client.get_health()['status']
            'healthy'
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
            ...     print(f"{db['id']}: {db['title']} ({db['n_cells']} cells)")
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
            db_id: Database identifier in "{group_id}/{file_id}" format (e.g., "LC5/SPM")

        Returns:
            Dict with n_cells, n_genes, layers, embeddings, metadata_fields, etc.

        Example:
            >>> info = client.get_database_info("LC5/SPM")
            >>> print(f"Cells: {info['n_cells']}, Genes: {info['n_genes']}")
        """
        return _self._get(f'/databases/{db_id}/info')

    # ===== Gene Operations =====

    @_cache_short(ttl=60)
    def search_genes(_self,
                     db_id: str,
                     q: str = "",
                     limit: int = 100,
                     case_sensitive: bool = False,
                     exact: bool = False) -> List[str]:
        """Search genes by name.

        Args:
            db_id: Database identifier
            q: Search string (empty returns all up to limit)
            limit: Maximum results to return (default: 100)
            case_sensitive: Case-sensitive match (default: False)
            exact: If True, match full gene name only; if False (default), substring match.
                   Example: exact=True, q="APOE" returns ["APOE"] not ["APOE", "APOER2"].
                   Case insensitivity still applies unless case_sensitive=True.

        Returns:
            List of matching gene names

        Example:
            >>> genes = client.search_genes("LC5/SPM", q="CD4", limit=10)
            >>> print(genes)  # ['CD4', 'CD40', 'CD40LG', ...]
            >>> genes = client.search_genes("LC5/SPM", q="Apoe", exact=True)
            >>> print(genes)  # ['Apoe']  (or ['APOE'] depending on stored name)
        """
        params = {'q': q, 'limit': limit, 'case_sensitive': case_sensitive, 'exact': exact}
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
            >>> info = client.get_gene_info("LC5/SPM", "CD4")
            >>> print(info['layers'])
        """
        return _self._get(f'/databases/{db_id}/genes/{gene_id}')

    def get_gene_stats(self,
                       db_id: str,
                       genes: Union[str, List[str]],
                       layer: str = "X",
                       format: str = "json") -> Union[List[Dict], Any]:
        """Get expression statistics for one or more genes.

        Statistics are computed efficiently from sparse data without transferring
        all cells. Includes stats over all cells (zeros treated as 0) and over
        expressing cells only.

        Args:
            db_id: Database identifier
            genes: Gene name or list of gene names
            layer: Layer name (default: "X")
            format: "json" (default) or "dataframe" (requires pandas)

        Returns:
            List of dicts or DataFrame with columns:
            gene, layer, n_cells, n_nonzero, pct_nonzero,
            mean, std,  (all cells, zeros=0)
            mean_nonzero, std_nonzero, median_nonzero, q10_nonzero, q90_nonzero, max

        Example:
            >>> stats = client.get_gene_stats("LC5/SPM", "APOE")
            >>> df = client.get_gene_stats("LC5/SPM", ["APOE", "CLU"], format='dataframe')
        """
        if isinstance(genes, list):
            genes_str = ','.join(genes)
        else:
            genes_str = genes
        params = {'genes': genes_str, 'layer': layer}
        response = self._get(f'/databases/{db_id}/gene_stats', params=params)
        stats = response.get('stats', response) if isinstance(response, dict) else response

        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(stats)
        return stats

    def get_gene_stats_all(self,
                           genes: Union[str, List[str]],
                           case_sensitive: bool = False,
                           format: str = "json") -> Union[List[Dict], Any]:
        """Get expression statistics for genes across all databases with a default_layer.

        Each database is queried using its `default_layer` (the log-normalised layer
        detected by ant-duckdb-check). Databases without a default_layer are skipped.

        Gene names are matched case-insensitively by default, so `APOE` will match
        `Apoe` in mouse datasets. When the stored name differs from the queried name
        a `gene_found` field is present in the stat entry.

        Args:
            genes: Gene name or list of gene names (e.g. "APOE" or ["APOE", "TREM2"])
            case_sensitive: Case-sensitive gene name matching (default: False)
            format: "json" (default) — returns full response dict with `results` list.
                    "dataframe" — returns a flat pandas DataFrame with one row per
                    (database × gene), adding `db_id`, `title`, `n_cells_db`, `layer` columns.

        Returns:
            dict with keys: genes, case_sensitive, n_databases, n_skipped, results
            (or DataFrame when format='dataframe')

        Example:
            >>> resp = client.get_gene_stats_all(["APOE", "TREM2"])
            >>> for r in resp["results"]:
            ...     print(r["db_id"], r["stats"])

            >>> df = client.get_gene_stats_all("APOE", format="dataframe")
            >>> df[["db_id", "title", "pct_nonzero", "mean"]].sort_values("mean", ascending=False)
        """
        if isinstance(genes, list):
            genes_str = ','.join(genes)
        else:
            genes_str = genes
        params = {'genes': genes_str, 'case_sensitive': case_sensitive}
        response = self._get('/gene_stats_all', params=params)

        if format == 'dataframe':
            import pandas as pd
            rows = []
            for r in response.get('results', []):
                for s in r.get('stats', []):
                    rows.append({
                        'db_id': r['db_id'],
                        'group': r.get('group', ''),
                        'title': r['title'],
                        'n_cells_db': r['n_cells'],
                        'layer': r['layer'],
                        **s,
                    })
            return pd.DataFrame(rows)
        return response

    # ===== Layers and Metadata =====

    @_cache_long(ttl=3600)
    def get_layers(_self, db_id: str) -> List[str]:
        """Get available data layers for a database

        Args:
            db_id: Database identifier

        Returns:
            List of layer names

        Example:
            >>> layers = client.get_layers("LC5/SPM")
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
            >>> fields = client.get_metadata_fields("LC5/SPM")
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
            >>> embeddings = client.get_embeddings("LC5/SPM")
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
            >>> umap = client.get_embedding_data("LC5/SPM", "Umap", n_dims=2, limit=1000)
            >>> df = client.get_embedding_data("LC5/SPM", "Umap", format='dataframe')
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
            >>> result = client.get_cells("LC5/SPM", genes=["CD4", "CD8"], limit=100)
            >>>
            >>> # Get as DataFrame
            >>> df = client.get_cells("LC5/SPM",
            ...                       genes=["CD4"],
            ...                       metadata=["cell_type", "tissue"],
            ...                       format='dataframe')
            >>>
            >>> # With filters
            >>> df = client.get_cells("LC5/SPM",
            ...                       genes=["CD4"],
            ...                       metadata=["cell_type"],
            ...                       filters=["cell_type:T-cell", "n_counts:1000,5000"],
            ...                       format='dataframe')
            >>>
            >>> # Export to CSV
            >>> csv_data = client.get_cells("LC5/SPM", genes=["CD4"], format='csv')
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
            >>> result = client.execute_sql("LC5/SPM",
            ...                             "SELECT cell_name, celltype FROM obscat LIMIT 10")
            >>> df = client.execute_sql("LC5/SPM",
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

    # ===== Plot =====

    def get_plot(self,
                 db_id: str,
                 geom: str,
                 x: Optional[str] = None,
                 y: Optional[str] = None,
                 color: Optional[str] = None,
                 layer: str = "X",
                 format: str = "png",
                 width: float = 8,
                 height: float = 6,
                 dpi: int = 150,
                 palette: str = "tab20",
                 sample: Optional[int] = None,
                 subset: Optional[List[str]] = None,
                 **extra) -> Dict[str, Any]:
        """Render a plot server-side.  Returns image bytes + caption.

        Args:
            db_id:  Database identifier (``group/file``).
            geom:   ``scatter`` | ``violin`` | ``box`` | ``bar`` | ``histogram``
                    | ``ecdf`` | ``kde`` | ``heatmap``.
            x, y, color: field names (gene, obs column, or embedding).  Their
                    meaning is geom-specific — see the OpenAPI docs.
            layer:  Expression layer to plot from (default ``"X"``).
            format: ``png`` | ``svg`` | ``pdf``.
            width, height, dpi, palette: figure styling.
            sample: random-subsample N cells (server-side, reservoir sampling).
            subset: list of ``field:value`` or ``field:v1,v2`` filter strings;
                    sent as repeated ``?subset=`` query params.
            **extra: any other plot params, forwarded as query params:
                    ``bins``, ``ci``, ``font_family``, ``font_size``,
                    ``point_size``, ``alpha``, ``n_genes``,
                    ``gridsize``, ``mincnt`` (hexbin),
                    ``log_x``, ``log_y`` (xy_scatter / xy_hexbin),
                    ``vmin``, ``vmax`` (continuous colorbar clip),
                    ``palette_categorical``, ``palette_continuous``,
                    ``palette_divergent``.

        Returns:
            Dict with::

                {
                    "bytes":      <raw image bytes>,
                    "format":     "png" | "svg" | "pdf",
                    "caption":    "<informative figure legend>",
                    "source_url": "<original URL the server saw>",
                    "cache":      "HIT" | "MISS",
                    "mime":       "image/png" | ...,
                }

            Save with ``open(path, 'wb').write(result['bytes'])`` or pass to
            ``IPython.display.Image(result['bytes'])`` in a notebook.

        Example:
            >>> p = client.get_plot("S25/V6W", "bar", x="patient_geno", dpi=80)
            >>> open("bar.png", "wb").write(p["bytes"])
            >>> print(p["caption"])  # "Bar plot of ... n = 235,106 cells; 29 groups."
        """
        params: Dict[str, Any] = {
            'geom': geom, 'format': format, 'layer': layer,
            'width': width, 'height': height, 'dpi': dpi, 'palette': palette,
        }
        if x      is not None: params['x']      = x
        if y      is not None: params['y']      = y
        if color  is not None: params['color']  = color
        if sample is not None: params['sample'] = sample
        if subset:
            # FastAPI accepts repeated ?subset= params; `requests` does this
            # when the value is a list.
            params['subset'] = subset
        for key, value in extra.items():
            if value is not None:
                params[key] = value
        response = self._get_raw(f'/databases/{db_id}/plot', params=params)
        return {
            'bytes':      response.content,
            'format':     response.headers.get('X-Plot-Format', format),
            'caption':    response.headers.get('X-Plot-Caption', ''),
            'source_url': response.headers.get('X-Source-URL', ''),
            'cache':      response.headers.get('X-Plot-Cache', ''),
            'mime':       response.headers.get('Content-Type', ''),
        }

    # ===== Analytics (sandboxed SELECT) =====

    def analytics_schema(self, db_id: str) -> Dict:
        """Return the schema (tables + columns + 3-row samples) visible to the
        analytics sandbox.  Use this before constructing an analytics query —
        column names differ per dataset.

        Example:
            >>> schema = client.analytics_schema("S25/V6W")
            >>> [t['name'] for t in schema['tables']]
            ['obsnum', 'obscat', 'genes', 'cells', 'layer__X']
        """
        return self._get(f'/databases/{db_id}/analytics/schema')

    def analytics_query(self,
                        db_id: str,
                        sql: str,
                        limit: Optional[int] = None,
                        format: str = "json") -> Union[Dict, Any]:
        """Run a sandboxed SELECT-only query.  Safer than :meth:`execute_sql`
        for untrusted input — INSERT / UPDATE / DELETE / ATTACH are rejected.

        The full result set is saved server-side as Parquet under a
        ``session_id``; ``preview`` holds the first rows for inspection,
        and ``row_count`` is the true total.  Pass the ``session_id`` to
        downstream tools (e.g. ``/analytics/viz``) to visualise the same
        result without re-running the query.

        Args:
            db_id:  Database identifier.
            sql:    SELECT (or WITH … SELECT) statement.
            limit:  optional server-side row cap (applies to the preview
                    AND the saved Parquet).
            format: ``json`` (default) — returns the full response dict
                    (``session_id``, ``columns``, ``dtypes``, ``row_count``,
                    ``truncated``, ``full_table``, ``preview``).
                    ``dataframe`` — convert the ``preview`` rows to a
                    pandas DataFrame (requires pandas).

        Example:
            >>> r = client.analytics_query(
            ...     "S25/V6W",
            ...     "SELECT patient_geno, COUNT(*) AS n FROM obscat GROUP BY 1")
            >>> r["row_count"]
            29
            >>> r["preview"][:3]
            [{'patient_geno': '...', 'n': 12345}, ...]
        """
        payload: Dict[str, Any] = {'sql': sql}
        if limit is not None:
            payload['limit'] = limit
        result = self._post(f'/databases/{db_id}/analytics/query', json=payload)
        if format == 'dataframe':
            try:
                import pandas as pd
            except ImportError:
                raise ImportError(
                    "pandas is required for format='dataframe'. "
                    "Install with: pip install pandas"
                )
            return pd.DataFrame(result.get('preview', []),
                                columns=result.get('columns'))
        return result

    # ===== Module scores =====

    def module_score(self,
                     db_id: str,
                     genes: List[str],
                     name: Optional[str] = None,
                     layer: str = "X",
                     format: str = "json") -> Union[Dict, Any]:
        """Compute a Seurat-style module score for an arbitrary gene list,
        on the fly.  Result is per-cell.

        Args:
            db_id: Database identifier.
            genes: list of gene symbols (case-resolved against the dataset).
            name:  optional label for the score (echoed back in the response).
            layer: layer to score from (default ``"X"``).
            format: ``json`` (default) or ``dataframe`` (requires pandas).

        Example:
            >>> r = client.module_score("S25/V6W", ["P2RY12","CX3CR1","TMEM119"],
            ...                          name="homeostatic_MG")
        """
        payload: Dict[str, Any] = {'genes': genes, 'layer': layer}
        if name:
            payload['name'] = name
        result = self._post(f'/databases/{db_id}/module_score', json=payload)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('cells', result))
        return result

    @_cache_short(ttl=60)
    def list_module_scores(_self, db_id: str) -> List[Dict]:
        """List pre-computed module-score columns available on this dataset
        (the ones already in ``obsnum``).  Use this for catalog/discovery,
        then ``analytics_query`` to read the values.

        Example:
            >>> [m['name'] for m in client.list_module_scores("S25/V6W")][:3]
            ['Sierksma2025::WGCNA::Black_Mito', '…', '…']
        """
        return _self._get(f'/databases/{db_id}/module_scores')

    # ===== Genesets =====

    @_cache_long(ttl=3600)
    def list_genesets(_self) -> Dict:
        """Return the geneset catalog: studies → experiments → genesets.

        Returns ``{}`` if the geneset store isn't configured (server
        responds 503/404).  Any other HTTP error is propagated."""
        try:
            return _self._get('/genesets')
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (404, 503):
                return {}
            raise

    @_cache_long(ttl=3600)
    def get_geneset_experiment(_self, study: str, experiment: str) -> Dict:
        """Return one experiment from the geneset store, listing its genesets."""
        return _self._get(f'/genesets/{study}/{experiment}')

    @_cache_long(ttl=3600)
    def get_geneset(_self, study: str, experiment: str, name: str) -> Dict:
        """Return the full content of one geneset (gene list + ranks if .rnk)."""
        return _self._get(f'/genesets/{study}/{experiment}/{name}')

    def rescan_genesets(self) -> Dict:
        """Re-scan the server-side geneset store from disk and rebuild the
        score index.  Pair with :meth:`rescan_databases` after editing
        ``data/genesetstore/``.

        Returns counts of experiments / genesets / scored_databases plus
        validation errors + warnings."""
        return self._post('/genesets/rescan')

    # ===== Analytics visualization (driven by analytics_query session_id) =====

    def analytics_viz(self,
                      session_id: str,
                      code: str,
                      output_format: str = "png") -> Dict[str, Any]:
        """Run a Python visualisation script against the Parquet result of a
        prior :meth:`analytics_query`, return the rendered figure.

        The script runs server-side in a sandbox: AST-scanned for dangerous
        imports, killed after ~60 s wall-clock.  The sandbox **pre-injects**:

        - ``df``           — your analytics_query result, already loaded as a
                             pandas DataFrame (do NOT call ``pd.read_parquet`` —
                             it's blocked).
        - ``pd``, ``plt``, ``sns``, ``np`` — pre-imported.
        - ``DATA_PATH``    — path to the Parquet file (read-only).
        - ``OUTPUT_PATH``  — path the script MUST save its figure to, e.g.
                             ``plt.savefig(OUTPUT_PATH, bbox_inches='tight')``.

        Args:
            session_id:    the ``session_id`` from an earlier ``analytics_query``.
            code:          Python script body.  See pre-injected names above.
            output_format: ``png`` | ``svg`` | ``pdf``.

        Returns:
            Dict with ``session_id``, ``output_format``, ``mime_type``, and
            ``figure_b64`` (base64-encoded image bytes).  Decode with
            ``base64.b64decode(result['figure_b64'])``.

        Example:
            >>> q = c.analytics_query("S25/V6W",
            ...     "SELECT MEblack, MEblue FROM obsnum LIMIT 1000")
            >>> v = c.analytics_viz(q['session_id'], '''
            ...   df.plot.scatter(x="MEblack", y="MEblue")
            ...   plt.savefig(OUTPUT_PATH, bbox_inches="tight", dpi=150)
            ... ''')
            >>> import base64
            >>> open('out.png','wb').write(base64.b64decode(v['figure_b64']))
        """
        payload = {
            'session_id': session_id,
            'code': code,
            'output_format': output_format,
        }
        return self._post('/analytics/viz', json=payload)

    # ===== Plot cache admin =====

    def get_plot_cache_stats(self) -> Dict:
        """Return ant-serve's plot-response cache stats: ``size``, ``max_size``,
        ``memory_bytes``, ``hits``, ``misses``, ``evictions``, ``hit_rate``."""
        return self._get('/admin/plot-cache/stats')

    def clear_plot_cache(self) -> Dict:
        """Drop every entry from ant-serve's plot-response LRU cache.
        Returns ``{"cleared": N}``."""
        return self._post('/admin/plot-cache/clear')

    # ===== Skill (MCP collaborator install path) =====

    def get_skill(self) -> str:
        """Fetch the Anthive Claude Code skill file (``SKILL.md``) as plain
        Markdown.  Used to install the anthive skill into ``~/.claude/skills/``
        when collaborators want LLM clients to discover this API.

        Returns the SKILL.md content as a string (NOT a dict).  Raises
        ``HTTPError`` 404 if the file isn't present on the server."""
        response = self._get_raw('/skill')
        return response.text

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

    def admin_release(self, db_id: str) -> Dict:
        """Tell ant-serve to drop its read-only handle on a duckdb so a
        writer (ant-score / ant-starcat / ant-annotate / ant-convert) can
        open it read-write.  Pair with :meth:`admin_restore` when done.

        Used internally by ``lib.anthive_db.AnnotationWriter``.  You only
        need to call it directly if you're driving annotation from outside
        the standard tools.
        """
        return self._post(f'/admin/release/{db_id}')

    def admin_restore(self, db_id: str) -> Dict:
        """Inverse of :meth:`admin_release` — clears the eviction so
        ant-serve can serve from the database again."""
        return self._post(f'/admin/restore/{db_id}')

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
            >>> genes = client.get_all_genes("LC5/SPM")
            >>> print(f"Total genes: {len(genes)}")
        """
        # Use search with empty query and high limit
        return self.search_genes(db_id, q="", limit=100000)

    @classmethod
    def pick_fastest(cls,
                     base_urls: List[str],
                     auth: Optional[Any] = None,
                     timeout: float = 2.0,
                     prefer: str = "p50",
                     min_samples: int = 10) -> Optional[str]:
        """Probe several anthive servers' ``/health`` endpoints, return the
        ``base_url`` of the one most likely to answer fastest.

        For each URL we read ``mean_response_ms`` / ``p50_response_ms`` from
        ``/health``.  If the server has fewer than ``min_samples`` samples
        in its ring buffer (cold start), we fall back to the client-observed
        RTT of the probe itself.  Servers that 503 or time out are skipped.

        Args:
            base_urls:   list of ``http(s)://host[:port][/api]`` candidates.
            auth:        passed through to ``requests`` (e.g. ``("user","pw")``).
            timeout:     per-probe timeout, seconds.
            prefer:      ``"p50"`` (outlier-resistant; default) or ``"mean"``.
            min_samples: below this, ignore server-side stats and use RTT.

        Returns:
            The fastest base_url (str), or ``None`` if every endpoint failed.

        Example:
            >>> from antclient import AnthiveClient
            >>> best = AnthiveClient.pick_fastest(
            ...     ["http://anthive-a:8501", "http://anthive-b:8501"],
            ...     auth=("user", "pass"))
            >>> client = AnthiveClient(best, auth=("user", "pass"))
        """
        import time
        if prefer not in ('mean', 'p50'):
            raise ValueError(f"prefer must be 'mean' or 'p50', got {prefer!r}")
        field = f'{prefer}_response_ms'
        best, best_score = None, float('inf')
        for raw in base_urls:
            url = raw.rstrip('/')
            try:
                t0 = time.perf_counter()
                response = requests.get(
                    f"{url}/health", auth=auth, timeout=timeout,
                )
                rtt_ms = (time.perf_counter() - t0) * 1000.0
                body = response.json() if response.status_code == 200 else {}
            except (requests.RequestException, ValueError):
                continue
            # Use server-side stat once it has enough samples; otherwise
            # the client-observed RTT is the fairer signal.
            if body.get('n_samples', 0) >= min_samples and body.get(field) is not None:
                score = body[field]
            else:
                score = rtt_ms
            if score < best_score:
                best, best_score = url, score
        return best

    def __repr__(self):
        """String representation"""
        return f"AnthiveClient(base_url='{self.base_url}')"


# Backward compatibility alias
AntClient = AnthiveClient
