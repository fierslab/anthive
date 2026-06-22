"""AnthiveClient — Python REST client for Anthive (single-cell RNA-seq).

Targets the anthive REST API contract. See
`https://codeberg.org/mfiers/pyvark` for sources and
`https://codeberg.org/mfiers/vark` for the Go CLI that ships the same surface.

API surface verified against **REST API 2.7.2** (2026-06-20). Older
servers still work for every endpoint they expose; new params degrade
to "server ignores it" rather than client-side errors.

Works in standard Python 3.9+, in Pyodide / JupyterLite, and inside
Streamlit (auto-detects environment).

Requirements:
    requests (required)
    pandas   (optional — for ``format='dataframe'``)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import requests


__version__ = "0.1.0"
__all__ = ["AnthiveClient"]


# ── Optional Streamlit caching ────────────────────────────────────────────────
#
# antclient supported a Streamlit caching path. Keep the shape so the
# legacy frontend/streamlit/ pages (now archived) and any third-party
# Streamlit users keep working, but default to a no-op decorator
# everywhere else.
try:
    import streamlit as _streamlit  # type: ignore[unused-import]
    _STREAMLIT_AVAILABLE = True

    def _cache_short(ttl: int = 60):
        return _streamlit.cache_data(ttl=ttl)

    def _cache_long(ttl: int = 3600):
        return _streamlit.cache_data(ttl=ttl)
except ImportError:
    _STREAMLIT_AVAILABLE = False

    def _cache_short(ttl: int = 60):
        def decorator(func):
            return func
        return decorator

    def _cache_long(ttl: int = 3600):
        def decorator(func):
            return func
        return decorator


# ── DataFrame coercion helper ─────────────────────────────────────────────────


def _to_dataframe(result: Dict[str, Any], fill_na_genes: bool = True):
    """Convert a ``{'data': [...], 'columns': [...]}`` API response to a
    pandas DataFrame.

    Args:
        result: Query response dict.
        fill_na_genes: If True, replace NaN with 0 in columns that look like
                       gene-expression columns (heuristic — skips known
                       metadata names + ``dim_*`` embeddings). Metadata NaN
                       is preserved.

    Raises:
        ImportError: pandas not installed.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "pandas is required for format='dataframe'. "
            "Install with: pip install pandas"
        ) from exc

    df = pd.DataFrame(result.get('data', []), columns=result.get('columns', []))

    if fill_na_genes and 'columns' in result:
        common_metadata = {
            'cell_name', 'cell_id', 'cell_type', 'celltype', 'tissue',
            'sample', 'donor', 'batch', 'condition', 'treatment',
            'cluster', 'seurat_clusters', 'n_genes', 'n_counts',
            'nCount_RNA', 'nFeature_RNA', 'percent.mt', 'percent_mito',
            'phase', 'doublet', 'orig.ident',
        }
        for col in df.columns:
            if col in common_metadata:
                continue
            if col.startswith('dim_'):
                continue
            if any(col.lower().startswith(prefix) for prefix in
                   ('n_', 'percent', 'pct_', 'log', 'total_')):
                continue
            if df[col].dtype in ('float64', 'float32', 'float16'):
                df[col] = df[col].fillna(0)
    return df


# ── Client ────────────────────────────────────────────────────────────────────


class AnthiveClient:
    """Lightweight REST client for the Anthive single-cell data browser.

    Args:
        base_url: Base URL — ``"https://host/api"`` or ``"http://localhost:8080"``.
            When ``None``, falls back to the ``ANTHIVE_API_URL`` env var,
            then to a browser-side ``js.window.location.origin``, then to
            ``http://localhost:8080``.
        timeout: per-request timeout (seconds). Default 30.
        auth: HTTP authentication, passed straight through to ``requests``.
            Use ``(user, password)`` for HTTP Basic; or any
            ``requests.auth.AuthBase`` subclass.
        verify: TLS-verify flag forwarded to ``requests`` (default ``True``).
            Set to ``False`` when hitting a server with a self-signed cert.

    Example::

        from vark import AnthiveClient
        c = AnthiveClient("https://my.anthive.example/api",
                          auth=("user", "pass"))
        info = c.get_version()
        print(info["version"], info["plot_geoms"])
    """

    #: Anthive REST API version this client release was last verified against.
    API_TARGET = "2.7.2"

    #: Server-side plot geoms the client knows about. Validation prevents a
    #: round-trip on typos. Update with every new server geom.
    PLOT_GEOMS = frozenset({
        'scatter', 'hexbin', 'kde2d',
        'violin', 'box', 'bar',
        'histogram', 'ecdf', 'kde',
        'heatmap', 'rolling',
        # DE-driven geoms (REST API 2.3+)
        'volcano', 'ma', 'forest',
        # DE-heatmap (REST API 2.7+)
        'de_heatmap',
    })

    #: Supported output formats. ``csv`` / ``tsv`` were added in 2.6.0 and
    #: return the same dataframe the plot was built from (not the image).
    PLOT_OUTPUT_FORMATS = frozenset({'png', 'svg', 'pdf', 'csv', 'tsv'})

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 30,
        auth: Optional[Any] = None,
        verify: Union[bool, str] = True,
    ):
        if base_url is None:
            import os
            base_url = os.environ.get('ANTHIVE_API_URL')
            if base_url is None:
                # Pyodide/browser: take origin from JS.
                try:
                    import js  # type: ignore
                    if hasattr(js, 'ANTHIVE_API_URL'):
                        base_url = js.ANTHIVE_API_URL
                    else:
                        origin = str(js.window.location.origin)
                        base_url = f"{origin}/api/"
                except (ImportError, AttributeError, Exception):
                    base_url = "http://localhost:8080"

        self.base_url = base_url.rstrip('/') if base_url else "http://localhost:8080"
        self.timeout = timeout
        self.headers: Dict[str, str] = {}
        self.auth = auth
        self.verify = verify

    # ── HTTP primitives ──────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = requests.get(
            url, params=params, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response.json()

    def _get_raw(self, path: str,
                 params: Optional[Dict] = None) -> 'requests.Response':
        """GET returning the raw response (for non-JSON bytes — plots,
        changelog)."""
        url = f"{self.base_url}{path}"
        response = requests.get(
            url, params=params, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response

    def _post(self, path: str, json: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = requests.post(
            url, json=json, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response.json()

    # ── Info endpoints ───────────────────────────────────────────────────

    def get_root(self) -> Dict:
        """``GET /`` — API root info (name, version, links)."""
        return self._get('/')

    def get_health(self) -> Dict:
        """``GET /health`` — health + latency telemetry.

        Includes ``mean_response_ms`` / ``p50_response_ms`` / ``n_samples``
        (server-side wall-clock over the last 1024 non-probe requests).
        Use :meth:`pick_fastest` to choose between replicas.
        """
        return self._get('/health')

    def get_metrics(self) -> Dict:
        """``GET /metrics`` — full performance metrics (request stats,
        connection-pool stats, memory throttling)."""
        return self._get('/metrics')

    def get_version(self) -> Dict:
        """``GET /version`` — structured API contract (REST API 2.3+).

        Returns a dict with ``version``, ``released``, ``plot_geoms``,
        ``plot_output_formats``, ``deprecated_endpoints``, etc. Falls back to
        a synthesised shape on pre-2.3 servers.
        """
        try:
            return self._get('/version')
        except Exception:
            health = self._get('/health')
            return {
                'version': health.get('api_version'),
                'released': None,
                'compatible_antclient': None,
                'openapi_url': '/openapi.json',
                'swagger_ui_url': '/docs',
                'redoc_url': '/redoc',
                'changelog_url': None,
                'plot_geoms': [],
                'plot_output_formats': ['png', 'svg', 'pdf'],
                'deprecated_endpoints': [],
            }

    def get_changelog(self) -> str:
        """``GET /changelog`` — API changelog as a Markdown string
        (REST API 2.3+). Raises ``HTTPError`` 404 on older servers."""
        return self._get_raw('/changelog').text

    # ── Database discovery ──────────────────────────────────────────────

    @_cache_short(ttl=60)
    def get_databases(_self, refresh: bool = False,
                      format: str = "list") -> Union[List[Dict], Any]:
        """``GET /databases`` — list every available dataset."""
        params = {'refresh': 'true'} if refresh else None
        response = _self._get('/databases', params=params)
        databases = (response.get('databases', response)
                     if isinstance(response, dict) else response)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(databases)
        return databases

    @_cache_short(ttl=60)
    def get_database_info(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/info`` — full dataset metadata.

        The ``collection`` block carries ``group_id``, ``doi``, ``pmid``,
        ``accession``, and ``description`` (markdown body of the nearest
        ``index.md``) on REST API 2.5+.
        """
        return _self._get(f'/databases/{db_id}/info')

    @_cache_short(ttl=60)
    def get_group(_self, group_id: str) -> Dict:
        """``GET /groups/{group_id}`` — collection-level metadata
        (REST API 2.5+).

        Returns the group prose body + frontmatter (group, experiment,
        authors, year, doi, pmid, accession, description) plus a list of
        ``{db_id, title, id}`` for every dataset in that group.

        Raises ``HTTPError`` 404 if the 3-char ``group_id`` is unknown.
        """
        return _self._get(f'/groups/{group_id}')

    # ── Gene operations ─────────────────────────────────────────────────

    @_cache_short(ttl=60)
    def search_genes(_self,
                     db_id: str,
                     q: str = "",
                     limit: int = 100,
                     case_sensitive: bool = False,
                     exact: bool = False) -> List[str]:
        """``GET /databases/{db_id}/genes`` — gene-name search.

        ``exact=True`` requires a full match (so ``q='APOE'`` returns
        ``['APOE']`` not ``['APOE', 'APOER2']``).
        """
        params = {
            'q': q, 'limit': limit,
            'case_sensitive': case_sensitive,
            'exact': exact,
        }
        response = _self._get(f'/databases/{db_id}/genes', params=params)
        return (response.get('genes', response)
                if isinstance(response, dict) else response)

    @_cache_long(ttl=3600)
    def get_gene_info(_self, db_id: str, gene_id: str) -> Dict:
        """``GET /databases/{db_id}/genes/{gene_id}`` — info + layers."""
        return _self._get(f'/databases/{db_id}/genes/{gene_id}')

    def get_gene_stats(self,
                       db_id: str,
                       genes: Union[str, List[str]],
                       layer: str = "X",
                       format: str = "json") -> Union[List[Dict], Any]:
        """``GET /databases/{db_id}/gene_stats`` — per-gene expression stats."""
        genes_str = ','.join(genes) if isinstance(genes, list) else genes
        params = {'genes': genes_str, 'layer': layer}
        response = self._get(f'/databases/{db_id}/gene_stats', params=params)
        stats = (response.get('stats', response)
                 if isinstance(response, dict) else response)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(stats)
        return stats

    def get_gene_stats_all(self,
                           genes: Union[str, List[str]],
                           case_sensitive: bool = False,
                           format: str = "json") -> Union[List[Dict], Any]:
        """``GET /gene_stats_all`` — per-gene stats across every dataset
        that has a default layer registered."""
        genes_str = ','.join(genes) if isinstance(genes, list) else genes
        params = {'genes': genes_str, 'case_sensitive': case_sensitive}
        response = self._get('/gene_stats_all', params=params)
        if format == 'dataframe':
            import pandas as pd
            rows = []
            for record in response.get('results', []):
                for stat in record.get('stats', []):
                    rows.append({
                        'db_id': record['db_id'],
                        'group': record.get('group', ''),
                        'title': record['title'],
                        'n_cells_db': record['n_cells'],
                        'layer': record['layer'],
                        **stat,
                    })
            return pd.DataFrame(rows)
        return response

    # ── Layers / metadata / embeddings ──────────────────────────────────

    @_cache_long(ttl=3600)
    def get_layers(_self, db_id: str) -> List[str]:
        """``GET /databases/{db_id}/layers`` — available data layers."""
        response = _self._get(f'/databases/{db_id}/layers')
        return (response.get('layers', response)
                if isinstance(response, dict) else response)

    @_cache_long(ttl=3600)
    def get_metadata_fields(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/metadata/fields`` — numerical +
        categorical obs columns."""
        return _self._get(f'/databases/{db_id}/metadata/fields')

    @_cache_long(ttl=3600)
    def get_embeddings(_self, db_id: str) -> List[str]:
        """``GET /databases/{db_id}/embeddings`` — embedding ids."""
        response = _self._get(f'/databases/{db_id}/embeddings')
        return (response.get('embeddings', response)
                if isinstance(response, dict) else response)

    def get_embedding_data(self,
                           db_id: str,
                           embedding_id: str,
                           n_dims: int = 2,
                           limit: Optional[int] = None,
                           format: str = "json") -> Union[Dict, Any]:
        """``GET /databases/{db_id}/embeddings/{embedding_id}`` — coords."""
        params: Dict[str, Any] = {'n_dims': n_dims}
        if limit is not None:
            params['limit'] = limit
        result = self._get(
            f'/databases/{db_id}/embeddings/{embedding_id}', params=params,
        )
        if format == 'dataframe':
            return _to_dataframe(result)
        return result

    # ── Cell-table retrieval ────────────────────────────────────────────

    def get_cells(self,
                  db_id: str,
                  genes: Optional[List[str]] = None,
                  metadata: Optional[Union[List[str], str]] = None,
                  layer: str = "X",
                  filters: Optional[List[str]] = None,
                  limit: Optional[int] = None,
                  format: str = "json",
                  fill_na: bool = True) -> Union[Dict, Any, str, bytes]:
        """``GET /databases/{db_id}/cells`` — cell table with expression
        and/or metadata.

        ``format`` is one of ``json`` (default), ``dataframe`` (requires
        pandas), ``csv`` (string), or ``parquet`` (bytes).
        """
        api_format = 'json' if format == 'dataframe' else format
        params: Dict[str, Any] = {'layer': layer, 'format': api_format}
        if genes:
            params['genes'] = ','.join(genes)
        if metadata:
            if isinstance(metadata, list):
                params['metadata'] = ','.join(metadata)
            else:
                params['metadata'] = metadata  # already "*"
        if filters:
            params['filter'] = filters
        if limit is not None:
            params['limit'] = limit

        if api_format == 'json':
            result = self._get(f'/databases/{db_id}/cells', params=params)
            if format == 'dataframe':
                return _to_dataframe(result, fill_na_genes=fill_na)
            return result
        url = f"{self.base_url}/databases/{db_id}/cells"
        response = requests.get(
            url, params=params, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response.content if format == 'parquet' else response.text

    # ── Raw SQL ─────────────────────────────────────────────────────────

    def execute_sql(self,
                    db_id: str,
                    query: str,
                    limit: Optional[int] = None,
                    format: str = "json",
                    fill_na: bool = True) -> Union[Dict, Any]:
        """``POST /databases/{db_id}/query/sql`` — raw SQL.

        For untrusted SQL prefer :meth:`analytics_query` (which is
        SELECT-only).
        """
        payload: Dict[str, Any] = {'query': query}
        if limit is not None:
            payload['limit'] = limit
        result = self._post(f'/databases/{db_id}/query/sql', json=payload)
        if format == 'dataframe':
            return _to_dataframe(result, fill_na_genes=fill_na)
        return result

    # ── Plot endpoint (per-geom) ────────────────────────────────────────

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
                 sample: Optional[int] = None,
                 subset: Optional[List[str]] = None,
                 # —— common visual knobs ——
                 palette_categorical: Optional[str] = None,
                 palette_continuous: Optional[str] = None,
                 palette_divergent: Optional[str] = None,
                 color_scale: Optional[str] = None,
                 font_family: Optional[str] = None,
                 font_size: Optional[float] = None,
                 **extra: Any) -> Dict[str, Any]:
        """``GET /databases/{db_id}/plot/{geom}`` — render a figure server-side.

        Hits the per-geom endpoint (REST API 2.2+) — each declares only
        the params relevant to that geom, so a 422 references the param
        that is actually wrong for that geom.

        Args:
            db_id:   Database identifier (``group/file``).
            geom:    One of :attr:`PLOT_GEOMS`.
            x, y, color: Field names (gene, obs column, embedding key).
            layer:   Expression layer (default ``"X"``).
            format:  Image (``png`` | ``svg`` | ``pdf``) **or** data export
                     (``csv`` | ``tsv`` — REST API 2.6+; returns the
                     dataframe the plot was built from).
            width, height, dpi: Figure styling.
            sample:  Reservoir-sample at most N cells server-side.
            subset:  Repeated ``field:value`` / ``field:v1,v2`` /
                     ``field:min..max`` filters.
            palette_categorical, palette_continuous, palette_divergent:
                     matplotlib colour maps.
            color_scale: ``'auto'`` (default) | ``'sequential'`` |
                     ``'divergent'`` — overrides the heuristic for
                     continuous colour.
            font_family, font_size: figure typography.
            **extra: Geom-specific params, forwarded verbatim:

                - ``bins`` (histogram)
                - ``ci`` (bar)
                - ``bar_mode`` ``'dodge'`` (default) | ``'stack'`` (bar)
                - ``normalize`` (stacked bar: scale to 100 %)
                - ``facet`` — 3rd categorical for small-multiples
                - ``group_by`` — bar: cluster x-bars by this categorical
                  (REST API 2.6+)
                - ``point_size``, ``alpha`` (scatter / hexbin)
                - ``n_genes``, ``zscore`` (heatmap)
                - ``gridsize``, ``mincnt``, ``vmin_quantile``,
                  ``vmax_quantile`` (hexbin)
                - ``log_x``, ``log_y`` — legacy log1p alias for
                  ``transform_x`` / ``transform_y``
                - ``transform_x``, ``transform_y``, ``asinh_scale``
                  (XY scatter / hexbin / kde2d)
                - ``vmin``, ``vmax`` — continuous colour-scale clip
                - ``window``, ``show_band`` (rolling)
                - ``kde_n``, ``kde_bw``, ``n_levels``, ``iso_overlay``,
                  ``point_overlay`` (kde2d)
                - ``marginals``, ``regline`` (XY scatter / hexbin / kde2d)
                - ``study``, ``term``, ``contrast``, ``gene``,
                  ``padj_threshold``, ``log2fc_threshold``, ``n_label``
                  (DE geoms volcano / ma / forest)
                - ``log2fc_clip`` (volcano / ma) — symmetric clamp of the
                  log2fc axis (REST API 2.7+)
                - ``neglog10p_clip`` (volcano) — clamp upper -log10(padj)
                  axis (REST API 2.7+)
                - ``logmean_clip`` (ma) — clamp upper log10(mean
                  expression) axis (REST API 2.7+)
                - ``study``, ``term``, ``contrasts``, ``genes``,
                  ``value``, ``sig_overlay``, ``padj_threshold``
                  (de_heatmap — REST API 2.7+)

        Returns:
            A dict::

                {
                    "bytes":      <raw bytes>,
                    "format":     "png" | "svg" | "pdf" | "csv" | "tsv",
                    "caption":    "<figure legend from X-Plot-Caption>",
                    "source_url": "<original URL the server saw>",
                    "cache":      "HIT" | "MISS",
                    "mime":       "image/png" | ...,
                    "etag":       '"<cache key>"' | "",
                }

            The ``caption`` field captures the **X-Plot-Caption** response
            header — anthive's canonical multi-sentence figure-legend
            description. Mirrors the Go vark v0.0.10+ behaviour: a
            non-empty caption means the server emitted one; an empty
            string means the server didn't (pre-API-2.7 or non-image
            geoms). Always check this — it's the only place the prose
            legend exists.

        Raises:
            ValueError: ``geom`` not in :attr:`PLOT_GEOMS`.
            requests.HTTPError: on non-2xx.

        Example::

            r = client.get_plot("S25/V6W", "bar", x="patient_geno", dpi=80)
            open("bar.png", "wb").write(r["bytes"])
            print(r["caption"])
        """
        if geom not in self.PLOT_GEOMS:
            raise ValueError(
                f"Unknown geom '{geom}'. Valid: {sorted(self.PLOT_GEOMS)}"
            )
        if format not in self.PLOT_OUTPUT_FORMATS:
            raise ValueError(
                f"Unknown format '{format}'. "
                f"Valid: {sorted(self.PLOT_OUTPUT_FORMATS)}"
            )
        params: Dict[str, Any] = {
            'format': format, 'layer': layer,
            'width': width, 'height': height, 'dpi': dpi,
        }
        if x      is not None: params['x']      = x
        if y      is not None: params['y']      = y
        if color  is not None: params['color']  = color
        if sample is not None: params['sample'] = sample
        if subset:
            # requests forwards lists as repeated query params.
            params['subset'] = subset
        if palette_categorical is not None:
            params['palette_categorical'] = palette_categorical
        if palette_continuous is not None:
            params['palette_continuous'] = palette_continuous
        if palette_divergent is not None:
            params['palette_divergent'] = palette_divergent
        if color_scale is not None:
            params['color_scale'] = color_scale
        if font_family is not None:
            params['font_family'] = font_family
        if font_size is not None:
            params['font_size'] = font_size
        for key, value in extra.items():
            if value is not None:
                params[key] = value
        response = self._get_raw(
            f'/databases/{db_id}/plot/{geom}', params=params,
        )
        return {
            'bytes':      response.content,
            'format':     response.headers.get('X-Plot-Format', format),
            # X-Plot-Caption (REST API 2.7.2+): the multi-sentence figure
            # legend. Empty when the server didn't emit one — clients
            # should treat absence as "no caption available", not an
            # error. Matches Go vark v0.0.10 capture semantics.
            'caption':    response.headers.get('X-Plot-Caption', ''),
            'source_url': response.headers.get('X-Source-URL', ''),
            'cache':      response.headers.get('X-Plot-Cache', ''),
            'mime':       response.headers.get('Content-Type', ''),
            'etag':       response.headers.get('ETag', ''),
        }

    # ── Differential expression (REST API 2.3+) ─────────────────────────

    def list_de_studies(self, db_id: str) -> List[Dict]:
        """``GET /databases/{db_id}/de`` — list DE studies on a dataset."""
        return self._get(f'/databases/{db_id}/de').get('studies', [])

    def get_de_study(self, db_id: str, study_id: str) -> Dict:
        """``GET /databases/{db_id}/de/{study_id}`` — one study + its
        ``(term, n_contrasts)`` list."""
        return self._get(f'/databases/{db_id}/de/{study_id}')

    def list_de_contrasts(self, db_id: str, study_id: str,
                          term: str) -> List[Dict]:
        """``GET /databases/{db_id}/de/{study_id}/terms/{term}``."""
        return self._get(
            f'/databases/{db_id}/de/{study_id}/terms/{term}'
        ).get('contrasts', [])

    def get_de_rows(self,
                    db_id: str,
                    study_id: str,
                    term: str,
                    contrast: str,
                    *,
                    sort: str = 'padj',
                    direction: str = 'both',
                    padj_max: Optional[float] = None,
                    abs_log2fc_min: Optional[float] = None,
                    limit: int = 200,
                    offset: int = 0,
                    format: str = 'json') -> Union[List[Dict], Any]:
        """``GET /databases/{db_id}/de/{study_id}/terms/{term}/contrasts/
        {contrast}`` — paged DE rows."""
        params: Dict[str, Any] = {
            'sort': sort, 'direction': direction,
            'limit': limit, 'offset': offset,
        }
        if padj_max is not None:
            params['padj_max'] = padj_max
        if abs_log2fc_min is not None:
            params['abs_log2fc_min'] = abs_log2fc_min
        result = self._get(
            f'/databases/{db_id}/de/{study_id}/terms/{term}/contrasts/{contrast}',
            params=params,
        )
        rows = result.get('rows', [])
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(rows)
        return rows

    def get_de_by_gene(self, db_id: str, gene: str,
                       format: str = 'json') -> Union[List[Dict], Any]:
        """``GET /databases/{db_id}/de/by-gene/{gene}`` — every DE row for
        one gene across all studies/terms/contrasts. Empty list if the
        gene isn't found."""
        result = self._get(f'/databases/{db_id}/de/by-gene/{gene}')
        rows = result.get('rows', [])
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(rows)
        return rows

    # ── Analytics sandbox ───────────────────────────────────────────────

    def analytics_schema(self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/analytics/schema`` — tables + columns +
        3-row samples visible to the analytics sandbox."""
        return self._get(f'/databases/{db_id}/analytics/schema')

    def analytics_query(self,
                        db_id: str,
                        sql: str,
                        limit: Optional[int] = None,
                        format: str = "json") -> Union[Dict, Any]:
        """``POST /databases/{db_id}/analytics/query`` — SELECT-only SQL.

        Result is saved as Parquet under a ``session_id`` (use with
        :meth:`analytics_viz`).
        """
        payload: Dict[str, Any] = {'sql': sql}
        if limit is not None:
            payload['limit'] = limit
        result = self._post(
            f'/databases/{db_id}/analytics/query', json=payload,
        )
        if format == 'dataframe':
            try:
                import pandas as pd
            except ImportError as exc:
                raise ImportError(
                    "pandas is required for format='dataframe'. "
                    "Install with: pip install pandas"
                ) from exc
            return pd.DataFrame(result.get('preview', []),
                                columns=result.get('columns'))
        return result

    def analytics_viz(self,
                      session_id: str,
                      code: str,
                      output_format: str = "png") -> Dict[str, Any]:
        """``POST /analytics/viz`` — render a matplotlib figure server-side
        against the Parquet result of a prior :meth:`analytics_query`.

        Pre-injected variables in the sandbox: ``df``, ``pd``, ``plt``,
        ``sns``, ``np``, ``DATA_PATH``, ``OUTPUT_PATH``.
        """
        payload = {
            'session_id': session_id,
            'code': code,
            'output_format': output_format,
        }
        return self._post('/analytics/viz', json=payload)

    # ── Module scores ───────────────────────────────────────────────────

    def module_score(self,
                     db_id: str,
                     genes: List[str],
                     name: Optional[str] = None,
                     layer: str = "X",
                     format: str = "json") -> Union[Dict, Any]:
        """``POST /databases/{db_id}/module_score`` — Seurat-style module
        score for an arbitrary gene list, computed on the fly."""
        payload: Dict[str, Any] = {'genes': genes, 'layer': layer}
        if name:
            payload['name'] = name
        result = self._post(
            f'/databases/{db_id}/module_score', json=payload,
        )
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('cells', result))
        return result

    @_cache_short(ttl=60)
    def list_module_scores(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/module_scores`` — pre-computed
        module-score columns already on the dataset (``obsnum``).

        Returns the full discovery dict::

            {
                "db_id": "...",
                "count": <N>,
                "column_naming": {"separator": "::", "template": "..."},
                "scored_genesets": [
                    {"study": ..., "experiment": ..., "name": ...,
                     "column": ..., "n_genes": ...}, ...
                ]
            }

        Use ``response["scored_genesets"]`` for the list itself.
        """
        return _self._get(f'/databases/{db_id}/module_scores')

    # ── Genesets ────────────────────────────────────────────────────────

    @_cache_long(ttl=3600)
    def list_genesets(_self) -> Dict:
        """``GET /genesets`` — catalog (studies → experiments → genesets)."""
        try:
            return _self._get('/genesets')
        except requests.HTTPError as exc:
            status = (exc.response.status_code
                      if exc.response is not None else 0)
            if status in (404, 503):
                return {}
            raise

    @_cache_long(ttl=3600)
    def get_geneset_experiment(_self, study: str, experiment: str) -> Dict:
        """``GET /genesets/{study}/{experiment}``."""
        return _self._get(f'/genesets/{study}/{experiment}')

    @_cache_long(ttl=3600)
    def get_geneset(_self, study: str, experiment: str, name: str) -> Dict:
        """``GET /genesets/{study}/{experiment}/{name}``."""
        return _self._get(f'/genesets/{study}/{experiment}/{name}')

    def rescan_genesets(self) -> Dict:
        """``POST /genesets/rescan`` — re-scan geneset store + rebuild."""
        return self._post('/genesets/rescan')

    # ── Plot cache admin ────────────────────────────────────────────────

    def get_plot_cache_stats(self) -> Dict:
        """``GET /admin/plot-cache/stats``."""
        return self._get('/admin/plot-cache/stats')

    def clear_plot_cache(self) -> Dict:
        """``POST /admin/plot-cache/clear``."""
        return self._post('/admin/plot-cache/clear')

    # ── Skill (MCP collaborator install path) ───────────────────────────

    def get_skill(self) -> str:
        """``GET /skill`` — fetch the anthive Claude Code SKILL.md."""
        return self._get_raw('/skill').text

    # ── Admin ───────────────────────────────────────────────────────────

    def rescan_databases(self) -> Dict:
        """``POST /admin/rescan`` — re-scan data folder + reload caches."""
        return self._post('/admin/rescan')

    def admin_release(self, db_id: str) -> Dict:
        """``POST /admin/release/{db_id}`` — drop ant-serve's r/o handle so
        a writer can open the duckdb r/w."""
        return self._post(f'/admin/release/{db_id}')

    def admin_restore(self, db_id: str) -> Dict:
        """``POST /admin/restore/{db_id}`` — inverse of admin_release."""
        return self._post(f'/admin/restore/{db_id}')

    # ── Convenience ─────────────────────────────────────────────────────

    def list_database_ids(self) -> List[str]:
        """Return just the IDs from :meth:`get_databases`."""
        return [db['id'] for db in self.get_databases()]

    def get_all_genes(self, db_id: str) -> List[str]:
        """Return every gene name in a database (uses a single big
        ``search_genes`` call)."""
        return self.search_genes(db_id, q="", limit=100000)

    @classmethod
    def pick_fastest(cls,
                     base_urls: List[str],
                     auth: Optional[Any] = None,
                     timeout: float = 2.0,
                     prefer: str = "p50",
                     min_samples: int = 10,
                     verify: Union[bool, str] = True) -> Optional[str]:
        """Probe several anthive servers' ``/health`` endpoints, return the
        ``base_url`` of the one most likely to answer fastest.

        Uses the server-side ``mean_response_ms`` / ``p50_response_ms``
        once it has accumulated ``>=min_samples`` real requests; falls
        back to the client-observed probe RTT otherwise.
        """
        import time
        if prefer not in ('mean', 'p50'):
            raise ValueError(
                f"prefer must be 'mean' or 'p50', got {prefer!r}"
            )
        field = f'{prefer}_response_ms'
        best, best_score = None, float('inf')
        for raw in base_urls:
            url = raw.rstrip('/')
            try:
                t0 = time.perf_counter()
                response = requests.get(
                    f"{url}/health", auth=auth, timeout=timeout,
                    verify=verify,
                )
                rtt_ms = (time.perf_counter() - t0) * 1000.0
                body = response.json() if response.status_code == 200 else {}
            except (requests.RequestException, ValueError):
                continue
            if (body.get('n_samples', 0) >= min_samples
                    and body.get(field) is not None):
                score = body[field]
            else:
                score = rtt_ms
            if score < best_score:
                best, best_score = url, score
        return best

    def __repr__(self) -> str:
        return f"AnthiveClient(base_url={self.base_url!r})"
