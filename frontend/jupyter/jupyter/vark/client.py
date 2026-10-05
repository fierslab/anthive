"""AnthiveClient — Python REST client for Anthive (single-cell RNA-seq).

Targets the anthive REST API contract. See
`https://codeberg.org/mfiers/pyvark` for sources and
`https://codeberg.org/mfiers/vark` for the Go CLI that ships the same surface.

API surface verified against **REST API 2.42.1** plus the unreleased facet
test, donor-group and exclude_secondary additions on anthive main
(2026-10-05). Older servers still work for every endpoint they expose; new params degrade
to "server ignores it" rather than client-side errors.

Works in standard Python 3.9+, in Pyodide / JupyterLite, and inside
Streamlit (auto-detects environment).

Requirements:
    requests (required)
    pandas   (optional — for ``format='dataframe'``)
"""

from __future__ import annotations

import json as _json
import os
import re
import sys
from pathlib import Path
from typing import (Any, BinaryIO, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple, Union)

import requests

from ._credentials import (
    VarkConfigError,
    find_vark_credentials_for,
    find_vark_server_by_name,
)
from .exceptions import raise_for_upload_status


__version__ = "0.8.0"
__all__ = ["AnthiveClient", "VarkConfigError"]


def _is_pyodide() -> bool:
    """Detect Pyodide / Emscripten so we can skip filesystem credential
    discovery in the browser (``~/.config/vark/`` doesn't exist there)."""
    if 'pyodide' in sys.modules:
        return True
    import platform
    return platform.system() == 'Emscripten'


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


def _csv_param(value: Union[str, Sequence[str]]) -> str:
    """A list (or a ready comma-separated string) as one ``a,b,c`` value."""
    if isinstance(value, str):
        return value
    return ','.join(str(item) for item in value)


def _filename_from_disposition(header: str) -> Optional[str]:
    """File name from a Content-Disposition header, basename only."""
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", header, re.I)
    if not match:
        match = re.search(r'filename="?([^";]+)"?', header, re.I)
    if not match:
        return None
    from urllib.parse import unquote
    name = Path(unquote(match.group(1).strip())).name
    return name or None


def _metadata_to_text(metadata: Union[Mapping[str, Any], str, Path], *,
                      role: Optional[str] = None,
                      papers_for: Any = None) -> str:
    """The ``metadata`` form field: JSON for a dict, the text itself for a
    string (JSON or ``.md``), the file content for a path."""
    if isinstance(metadata, os.PathLike):
        metadata = Path(metadata).read_text(encoding='utf-8')
    fields: Optional[Dict[str, Any]] = None
    if isinstance(metadata, Mapping):
        fields = dict(metadata)
    elif isinstance(metadata, str):
        stripped = metadata.lstrip()
        if stripped.startswith('{'):
            try:
                parsed = _json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                fields = parsed
    else:
        raise TypeError(
            f"metadata must be a dict, str or Path, got {type(metadata).__name__}")
    if role is None and papers_for is None:
        return _json.dumps(fields) if fields is not None else str(metadata)
    if fields is None:
        raise ValueError(
            "role/papers_for can only be added to dict or JSON metadata; "
            "put anthive_role / papers_for in the .md front matter instead")
    if role is not None:
        fields['anthive_role'] = role
    if papers_for is not None:
        # Sent as given; the server accepts a mapping, a list of ids
        # (``id`` or ``id:role``) and a comma-separated string.
        if isinstance(papers_for, Mapping):
            fields['papers_for'] = dict(papers_for)
        elif isinstance(papers_for, str):
            fields['papers_for'] = papers_for
        else:
            fields['papers_for'] = list(papers_for)
    return _json.dumps(fields)


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
            ``requests.auth.AuthBase`` subclass. **Explicit ``auth=``
            always wins** — vark-config discovery never overrides it.
            Without it the client uses, in this order: the
            ``ANTHIVE_USER`` + ``ANTHIVE_PASSWORD`` environment variables
            (unless ``vark_config`` is ``True`` or a path), the Go vark
            CLI's stored credentials, and finally ``~/.netrc`` (read by
            ``requests`` itself).
        verify: TLS-verify flag forwarded to ``requests`` (default ``True``).
            Set to ``False`` when hitting a server with a self-signed cert.
        vark_config: How to handle credential discovery from the Go vark
            CLI's local config (``~/.config/vark/`` by default). Only
            consulted when ``auth`` is ``None``.

            - ``None`` (default): try discovery, silently fall back when
              the vark config is absent or the ``vark-config`` extras
              aren't installed.
            - ``True``: require discovery — raise
              :class:`VarkConfigError` when no credentials for
              ``base_url`` are found.
            - ``False``: skip discovery entirely (don't read anything).
            - ``str | Path``: use that directory instead of
              ``~/.config/vark/``.

            When discovery finds a server with ``insecure: true``, the
            client's ``verify`` is demoted to ``False`` (unless the
            caller passed an explicit ``verify=`` already).

    Example::

        import getpass
        from vark import AnthiveClient
        c = AnthiveClient("https://my.anthive.example/api",
                          auth=("me", getpass.getpass()))
        info = c.get_version()
        print(info["version"], info["plot_geoms"])

    Example (reuse Go vark CLI credentials)::

        # Requires the `vark-config` extras + the Go CLI's local config.
        c = AnthiveClient("https://my.anthive.example/api")
        # OR by registered server name:
        c = AnthiveClient.from_vark_server("intern")
    """

    #: Anthive REST API version this client release was last verified against.
    API_TARGET = "2.42.1"

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
        # DE-quadrant (REST API 2.12+)
        'de_quadrant',
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
        vark_config: Union[str, Path, bool, None] = None,
    ):
        if base_url is None:
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

        # ANTHIVE_USER / ANTHIVE_PASSWORD: used when no explicit auth= is
        # given and vark-config discovery was not explicitly requested.
        env_user = os.environ.get('ANTHIVE_USER')
        env_password = os.environ.get('ANTHIVE_PASSWORD')
        if (auth is None and vark_config in (None, False)
                and env_user and env_password is not None):
            self.auth = (env_user, env_password)
            auth = self.auth

        # vark-config discovery — only when caller did NOT pass explicit auth.
        # Explicit `auth=` always wins; vark_config=False skips entirely;
        # browser/Pyodide always skips (no filesystem to read).
        # With no auth at all, requests itself still reads ~/.netrc.
        if auth is None and vark_config is not False and not _is_pyodide():
            if vark_config is True:
                # Require: raise loudly if nothing matches.
                required = True
                cfg_dir: Optional[Union[str, Path]] = None
            elif vark_config is None:
                required = False
                cfg_dir = None
            else:
                # A path → treat as explicit opt-in (require).
                required = True
                cfg_dir = vark_config
            server = find_vark_credentials_for(
                self.base_url, config_dir=cfg_dir, required=required,
            )
            if server is not None:
                self.auth = (server.user, server.password)
                # Only demote verify when the caller didn't ask for a
                # specific TLS posture. `verify is True` is the default;
                # any other value (False, a CA path) is the caller's
                # deliberate choice and we leave it alone.
                if verify is True and server.insecure:
                    self.verify = False

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

    def _request(self, method: str, path: str, *,
                 timeout: Optional[float] = None,
                 **kwargs: Any) -> 'requests.Response':
        """Send one request and return the raw response WITHOUT checking
        its status (callers decide how to map errors)."""
        url = f"{self.base_url}{path}"
        return requests.request(
            method, url, headers=self.headers,
            timeout=self.timeout if timeout is None else timeout,
            auth=self.auth, verify=self.verify, **kwargs,
        )

    # ── Info endpoints ───────────────────────────────────────────────────

    def get_root(self) -> Dict:
        """``GET /``. API root info (name, version, links)."""
        return self._get('/')

    def get_health(self) -> Dict:
        """``GET /health``. Health + latency telemetry.

        Includes ``mean_response_ms`` / ``p50_response_ms`` / ``n_samples``
        (server-side wall-clock over the last 1024 non-probe requests).
        Use :meth:`pick_fastest` to choose between replicas.
        """
        return self._get('/health')

    def get_metrics(self) -> Dict:
        """``GET /metrics``. Full performance metrics (request stats,
        connection-pool stats, memory throttling)."""
        return self._get('/metrics')

    def get_version(self) -> Dict:
        """``GET /version``. Structured API contract (REST API 2.3+).

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
        """``GET /changelog``. API changelog as a Markdown string
        (REST API 2.3+). Raises ``HTTPError`` 404 on older servers."""
        return self._get_raw('/changelog').text

    # ── Database discovery ──────────────────────────────────────────────

    @_cache_short(ttl=60)
    def get_databases(_self, refresh: bool = False,
                      format: str = "list",
                      primary_only: bool = False,
                      only_facet: Optional[Dict[str, List[str]]] = None,
                      ) -> Union[List[Dict], Any]:
        """``GET /databases``. List every available dataset.

        Each row carries an ``is_secondary`` boolean (REST API 2.37+). A
        value of ``True`` means the dataset re-aggregates cells that are
        already counted in another study and should be excluded from
        meta-analyses. ``False`` marks a fully primary study. Servers
        older than 2.37 omit the field, in which case the client treats
        the row as primary.

        Lineage fields (anthive main after 2.42.1, unreleased at the
        time of pyvark 0.8.0): ``designation`` (``'primary'``,
        ``'reanalysis'`` or ``'unresolved'``, from ``is_secondary:
        false`` / ``true`` / no key in the dataset's ``.md``),
        ``reanalysis_of`` (list of db_ids, empty unless reanalysis),
        ``donor_group`` (slug of the donor pool the dataset shares with
        others, or ``None``) and ``has_donor_uid`` (the dataset
        publishes ``ant_donor_uid``). Datasets of one donor group share
        persons, so do not count them as independent donors; see
        :meth:`get_donor_groups`.

        Args:
            refresh: Force a fresh catalog scan on the server.
            format: ``'list'`` (default) or ``'dataframe'`` (needs pandas).
            primary_only: If True, drop rows where ``is_secondary`` is
                truthy. The filter runs client-side, so it works against
                servers that already return the field and is a no-op on
                older servers (nothing to drop).
            only_facet: Optional ``{facet_name: [allowed_values]}`` filter
                on the ``collection`` block. A row is kept when its
                ``collection[facet_name]`` value is in the allowed list.
                Rows missing the ``collection`` block or the requested
                key are dropped. Runs client-side, one facet per call.
                Example: ``only_facet={"ant_disease": ["AD", "control"]}``.
        """
        params = {'refresh': 'true'} if refresh else None
        response = _self._get('/databases', params=params)
        databases = (response.get('databases', response)
                     if isinstance(response, dict) else response)
        if primary_only:
            databases = [db for db in databases
                         if not db.get('is_secondary', False)]
        if only_facet:
            def _match(row: Dict) -> bool:
                collection = row.get('collection')
                if not isinstance(collection, dict):
                    return False
                for facet_name, allowed in only_facet.items():
                    if facet_name not in collection:
                        return False
                    if collection[facet_name] not in allowed:
                        return False
                return True
            databases = [db for db in databases if _match(db)]
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(databases)
        return databases

    @_cache_short(ttl=60)
    def get_database_info(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/info``. Full dataset metadata.

        The ``collection`` block carries ``group_id``, ``doi``, ``pmid``,
        ``accession``, and ``description`` (markdown body of the nearest
        ``index.md``) on REST API 2.5+.

        The response also carries an ``is_secondary`` boolean (REST API
        2.37+). ``True`` flags a re-aggregation of cells already counted
        in another dataset, so meta-analyses can skip it. ``False`` is a
        fully primary study. Older servers omit the key; treat absence
        as primary.

        The returned dict also exposes a top-level ``description`` field
        with the dataset's own prose description, distinct from the
        group-level description on ``collection``. The ``collection``
        sub-dict may include a ``title`` key that acts as the display
        override for the group name (fall back to ``group`` when
        absent), and a ``date`` key with the full publication date as
        ``YYYY-MM-DD``.

        Newer servers add the lineage fields ``designation``,
        ``reanalysis_of``, ``donor_group`` and ``has_donor_uid``
        (described under :meth:`get_databases`).
        """
        return _self._get(f'/databases/{db_id}/info')

    def get_donor_groups(self, donor_group: Optional[str] = None,
                         format: str = 'json') -> Union[Dict, Any]:
        """``GET /donor-groups``. Datasets grouped by the donor pool they
        share.

        A donor group (``donor_group:`` in a dataset's ``.md``) names
        datasets that hold cells of the same persons: complementary
        splits of one study (cell classes, regions, libraries) or
        several studies of one cohort. They must not be counted as
        independent donors.

        Returns ``{n_groups, groups, n_ungrouped}``. Each group has
        ``donor_group``, ``n_datasets``, ``n_primary``,
        ``n_reanalysis``, ``n_unresolved``, ``n_cells`` (primaries
        only, so no cell is counted twice), ``has_donor_uid`` (every
        dataset publishes ``ant_donor_uid``) and ``datasets`` (``db_id``,
        ``title``, ``group``, ``designation``, ``is_secondary``,
        ``reanalysis_of``, ``has_donor_uid``, ``n_cells``).
        ``n_ungrouped`` counts datasets without a donor group. Servers
        that count distinct donors add those counts to each group as
        well; the client passes every field through unchanged.

        Args:
            donor_group: Only this group (slug). The server answers 404
                for an unknown slug.
            format: ``'json'`` (the server's dict) or ``'dataframe'``:
                one row per (group, dataset) with the group's counts
                prefixed ``group_`` (needs pandas).
        """
        params = {'donor_group': donor_group} if donor_group else None
        result = self._get('/donor-groups', params=params)
        if format == 'dataframe':
            import pandas as pd
            rows = []
            for group in result.get('groups', []):
                group_fields = {f'group_{key}': value
                                for key, value in group.items()
                                if key not in ('datasets', 'donor_group')}
                for dataset in group.get('datasets', []):
                    rows.append({'donor_group': group.get('donor_group'),
                                 **group_fields, **dataset})
            return pd.DataFrame(rows)
        return result

    @_cache_short(ttl=60)
    def get_group(_self, group_id: str) -> Dict:
        """``GET /groups/{group_id}``. Collection-level metadata
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
        """``GET /databases/{db_id}/genes``. Gene-name search.

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
        """``GET /databases/{db_id}/genes/{gene_id}``. Info + layers."""
        return _self._get(f'/databases/{db_id}/genes/{gene_id}')

    def get_gene_stats(self,
                       db_id: str,
                       genes: Union[str, List[str], None] = None,
                       layer: Optional[str] = None,
                       format: str = "json",
                       top: Optional[int] = None) -> Union[List[Dict], Any]:
        """``GET /databases/{db_id}/gene_stats``. Per-gene expression stats.

        Args:
            db_id: Dataset id.
            genes: Gene names (list or comma-separated string). Omit and
                pass ``top`` to get the top-N most variable genes instead.
            layer: Expression layer. ``None`` (default) lets the server
                use the dataset's ``default_layer`` (else ``X``). Before
                pyvark 0.7 the client always sent ``layer=X``, which
                overrode a dataset's own default layer.
            format: ``'json'`` (list of dicts) or ``'dataframe'``.
            top: With ``genes`` omitted, the N most variable genes (by
                std; the server caps it at 200).
        """
        params: Dict[str, Any] = {}
        if genes:
            params['genes'] = ','.join(genes) if isinstance(genes, list) else genes
        if layer is not None:
            params['layer'] = layer
        if top is not None:
            params['top'] = top
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
                           format: str = "json",
                           *,
                           organism: Union[str, List[str], None] = None,
                           cell_type: Union[str, List[str], None] = None,
                           source: Union[str, List[str], None] = None,
                           access: Union[str, List[str], None] = None,
                           tissue: Union[str, List[str], None] = None,
                           include_remote: bool = False,
                           refresh: bool = False,
                           exclude_secondary: bool = True,
                           ) -> Union[List[Dict], Any]:
        """``GET /gene_stats_all``. Per-gene stats across every dataset
        that has a default layer registered.

        The keyword-only filters are forwarded as comma-separated lists
        (a list or a ready string both work): ``organism`` (e.g.
        ``'human,mouse'``), ``cell_type`` (mus labels), ``source``
        (``internal`` / ``external``), ``access`` (``internal`` /
        ``public``) and ``tissue``. ``include_remote=True`` also queries
        datasets that only live on S3 (slow); ``refresh=True`` bypasses
        the server cache.

        ``exclude_secondary`` (default ``True``, the server's default on
        anthive main after 2.42.1): secondary datasets (reanalyses of
        cells another dataset serves) are left out of ``results`` and of
        ``cross_db_cv``; the response then carries
        ``n_excluded_secondary`` and ``exclude_secondary`` and each
        result ``is_secondary``. Pass ``False`` to include them. The
        client sends the parameter only when it is ``False``; older
        servers ignore it and include every dataset.
        """
        genes_str = ','.join(genes) if isinstance(genes, list) else genes
        params: Dict[str, Any] = {'genes': genes_str,
                                  'case_sensitive': case_sensitive}
        for key, value in (('organism', organism), ('cell_type', cell_type),
                           ('source', source), ('access', access),
                           ('tissue', tissue)):
            if value:
                params[key] = _csv_param(value)
        if include_remote:
            params['include_remote'] = True
        if refresh:
            params['refresh'] = True
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
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
                        'is_secondary': record.get('is_secondary', False),
                        **stat,
                    })
            return pd.DataFrame(rows)
        return response

    # ── Layers / metadata / embeddings ──────────────────────────────────

    @_cache_long(ttl=3600)
    def get_layers(_self, db_id: str) -> Dict[str, str]:
        """``GET /databases/{db_id}/layers``. Available data layers.

        The server answers a mapping of layer key to display name, e.g.
        ``{"X": "X", "counts": "counts"}`` (only the layers listed in the
        dataset's ``layers_include``). Iterating it or testing ``in``
        works on the keys. Earlier pyvark docs said this was a list; the
        server has always sent the mapping and the client passes it on
        unchanged.
        """
        response = _self._get(f'/databases/{db_id}/layers')
        return (response.get('layers', response)
                if isinstance(response, dict) else response)

    @_cache_long(ttl=3600)
    def get_metadata_fields(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/metadata/fields``. Numerical +
        categorical obs columns."""
        return _self._get(f'/databases/{db_id}/metadata/fields')

    @_cache_long(ttl=3600)
    def get_embeddings(_self, db_id: str) -> Dict[str, Dict]:
        """``GET /databases/{db_id}/embeddings``. Available embeddings.

        The server answers a mapping of embedding key to its info (display
        name and dimensions), for the embeddings in the dataset's
        ``obsm_include``. Iterating it gives the keys to pass to
        :meth:`get_embedding_data`. Earlier pyvark docs said this was a
        list; the server has always sent the mapping.
        """
        response = _self._get(f'/databases/{db_id}/embeddings')
        return (response.get('embeddings', response)
                if isinstance(response, dict) else response)

    def get_embedding_data(self,
                           db_id: str,
                           embedding_id: str,
                           n_dims: int = 2,
                           limit: Optional[int] = None,
                           format: str = "json") -> Union[Dict, Any]:
        """``GET /databases/{db_id}/embeddings/{embedding_id}``. Coords."""
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
        """``GET /databases/{db_id}/cells``. Cell table with expression
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
        """``POST /databases/{db_id}/query/sql``. Raw SQL.

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
                 bin_color: Optional[int] = None,
                 bin_color_method: Optional[str] = None,
                 font_family: Optional[str] = None,
                 font_size: Optional[float] = None,
                 attach_sessions: Union[str, List[str], None] = None,
                 **extra: Any) -> Dict[str, Any]:
        """``GET /databases/{db_id}/plot/{geom}``. Render a figure server-side.

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
            bin_color: If set (2..20), bin a continuous ``color`` field
                     (numerical obs or gene) into N categorical bins
                     server-side before plotting (REST API 2.34+ for
                     scatter/hexbin, 2.34 rolling, 2.35 kde2d, 2.36
                     histogram/ecdf/kde). On scatter / hexbin this is
                     optional; on rolling / kde2d / histogram / ecdf /
                     kde a continuous ``color`` REQUIRES ``bin_color``
                     (server returns 400 otherwise).
            bin_color_method: ``'quantile'`` (default — equal-count
                     bins, `pd.qcut`) or ``'width'`` (equal-width bins,
                     `pd.cut`). Only meaningful when ``bin_color`` is
                     set.
            font_family, font_size: figure typography.
            attach_sessions: Session id(s) from :meth:`module_score` or
                     :meth:`analytics_query`. Their per-cell columns become
                     plottable fields (``module_score`` for a module-score
                     session; later sessions get ``session_1::`` ...
                     prefixes). Use them as ``x``, ``y`` or ``color``.
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
        if bin_color is not None:
            params['bin_color'] = bin_color
        if bin_color_method is not None:
            params['bin_color_method'] = bin_color_method
        if font_family is not None:
            params['font_family'] = font_family
        if font_size is not None:
            params['font_size'] = font_size
        if attach_sessions:
            params['attach_sessions'] = ([attach_sessions]
                                         if isinstance(attach_sessions, str)
                                         else list(attach_sessions))
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
        """``GET /databases/{db_id}/de``. List DE studies on a dataset."""
        return self._get(f'/databases/{db_id}/de').get('studies', [])

    def get_de_study(self, db_id: str, study_id: str) -> Dict:
        """``GET /databases/{db_id}/de/{study_id}``. One study + its
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
        {contrast}``. Paged DE rows."""
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
        """``GET /databases/{db_id}/de/by-gene/{gene}``. Every DE row for
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
        """``GET /databases/{db_id}/analytics/schema``. Tables + columns +
        3-row samples visible to the analytics sandbox."""
        return self._get(f'/databases/{db_id}/analytics/schema')

    def analytics_query(self,
                        db_id: str,
                        sql: str,
                        limit: Optional[int] = None,
                        format: str = "json",
                        *,
                        session_id: Optional[str] = None,
                        timeout: Optional[int] = None,
                        attach_sessions: Union[str, List[str], None] = None,
                        ) -> Union[Dict, Any]:
        """``POST /databases/{db_id}/analytics/query``. SELECT-only SQL.

        The full result is saved server-side as Parquet under a
        ``session_id`` (use it with :meth:`analytics_viz`, or as
        ``attach_sessions`` on :meth:`get_plot`). The answer carries
        ``session_id``, ``columns``, ``row_count`` and a 10-row
        ``preview``; ``format='dataframe'`` turns the preview into a
        DataFrame.

        Args:
            db_id: Dataset id.
            sql: A ``SELECT`` / ``WITH ... SELECT`` statement.
            limit: Ignored. The server has no such field (results are
                capped server-side at 500 000 rows); kept so older call
                sites keep working. Put a ``LIMIT`` in the SQL instead.
            format: ``'json'`` or ``'dataframe'``.
            session_id: Reuse this 32-hex session id instead of a new one.
            timeout: Query timeout in seconds (the server clamps it).
            attach_sessions: Session ids of earlier :meth:`module_score`
                or analytics results to attach. A module-score session
                appears as table ``scores`` with ``(cell_name,
                module_score)``.
        """
        payload: Dict[str, Any] = {'sql': sql}
        if session_id is not None:
            payload['session_id'] = session_id
        if timeout is not None:
            payload['timeout'] = timeout
        if attach_sessions:
            payload['attach_sessions'] = ([attach_sessions]
                                          if isinstance(attach_sessions, str)
                                          else list(attach_sessions))
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
        """``POST /analytics/viz``. Render a matplotlib figure server-side
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
                     format: str = "json",
                     *,
                     n_control: Optional[int] = None,
                     seed: Optional[int] = None,
                     subset: Optional[List[str]] = None) -> Union[Dict, Any]:
        """``POST /databases/{db_id}/module_score``. Seurat-style module
        score for an arbitrary gene list, computed on the fly.

        The server answers with summary statistics, not per-cell values::

            {"db_id", "session_id", "genes_found", "genes_missing",
             "n_cells", "n_control_genes", "layer", "n_control", "seed",
             "summary": {"mean", "std", "min", "q25", "median", "q75",
                         "max"}}

        The per-cell scores are stored server-side under ``session_id``.
        Plot them by passing ``attach_sessions=session_id`` to
        :meth:`get_plot` with ``x``/``y``/``color='module_score'``, or
        query them with :meth:`analytics_query` (table ``scores``).

        Args:
            db_id: Dataset id.
            genes: Module genes.
            name: Ignored by the server (it has no such field); kept so
                older call sites keep working.
            layer: Expression layer (default ``X``).
            format: ``'json'`` (the dict above) or ``'dataframe'`` (a
                one-row DataFrame of ``summary``).
            n_control: Control genes per bin (server default 100).
            seed: Random seed for the control genes (server default 42).
            subset: ``field:value`` filters, as on :meth:`get_plot`.
        """
        payload: Dict[str, Any] = {'genes': list(genes), 'layer': layer}
        if n_control is not None:
            payload['n_control'] = n_control
        if seed is not None:
            payload['seed'] = seed
        if subset:
            payload['subset'] = list(subset)
        result = self._post(
            f'/databases/{db_id}/module_score', json=payload,
        )
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame([result.get('summary', {})])
        return result

    @_cache_short(ttl=60)
    def list_module_scores(_self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/module_scores``. Pre-computed
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
    def list_genesets(_self,
                      organism: Optional[str] = None,
                      method: Optional[str] = None,
                      study: Optional[str] = None) -> Dict:
        """``GET /genesets``. Every geneset, optionally filtered.

        Returns ``{"count", "column_naming", "genesets": [...]}``; each
        geneset carries ``scored_in`` (datasets where it is precomputed).
        Answers ``{}`` when the server has no geneset store.
        """
        params = {key: value for key, value in
                  (('organism', organism), ('method', method), ('study', study))
                  if value}
        try:
            return _self._get('/genesets', params=params or None)
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
        """``POST /genesets/rescan``. Re-scan geneset store + rebuild."""
        return self._post('/genesets/rescan')

    @_cache_long(ttl=3600)
    def list_geneset_studies(_self) -> Dict:
        """``GET /genesets/studies``. Studies with their experiments."""
        return _self._get('/genesets/studies')

    def get_genesets_by_gene(self, gene: str) -> Dict:
        """``GET /genesets/by-gene/{gene}``. Every geneset containing
        ``gene``."""
        return self._get(f'/genesets/by-gene/{gene}')

    def get_module_variability(self,
                               db_id: Optional[str] = None,
                               *,
                               source: Optional[str] = None,
                               catalog: Optional[str] = None,
                               module: Optional[str] = None,
                               tag: Optional[str] = None,
                               tag_column: Optional[str] = None,
                               statistic: Optional[str] = None,
                               min_value: Optional[float] = None,
                               max_value: Optional[float] = None,
                               min_abs: Optional[float] = None,
                               sort: str = 'value:desc',
                               limit: int = 500,
                               format: str = 'json',
                               exclude_secondary: bool = True,
                               ) -> Union[Dict, Any]:
        """``GET /module-variability``. Fleet-wide module variability
        table (dataset x source x catalog x module x tag x statistic).

        Args:
            db_id: Restrict to one dataset.
            source: ``'geneset'`` or ``'starcat'``.
            catalog: e.g. ``'Sierksma2025/WGCNA'`` or ``'MICROGLIA_V1'``.
            module: Module / program name.
            tag, tag_column: Obs-column tag or original column name.
            statistic: ``eta2``, ``omega2``, ``R2``, ``spearman``, ``F``,
                ``mean_range``, ``total_var`` or ``mean_score``.
            min_value, max_value: Bounds on ``value`` (sent as ``min`` /
                ``max``).
            min_abs: Lower bound on ``|value|``.
            sort: ``'value:desc'`` (default), ``'value:asc'`` or
                ``'abs_value:desc'``.
            limit: Max rows (server default 500).
            format: ``'json'`` (the server's dict), ``'dataframe'``
                (needs pandas), ``'csv'`` or ``'tsv'`` (text).
            exclude_secondary: Default ``True`` (the server's default on
                anthive main after 2.42.1): rows of secondary datasets
                (reanalyses) are left out unless ``db_id`` names one.
                JSON then carries ``n_excluded_secondary``, CSV / TSV the
                ``X-Excluded-Secondary`` header. Sent only when ``False``.
        """
        params: Dict[str, Any] = {'sort': sort, 'limit': limit}
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        for key, value in (('dataset', db_id), ('source', source),
                           ('catalog', catalog), ('module', module),
                           ('tag', tag), ('tag_column', tag_column),
                           ('statistic', statistic), ('min', min_value),
                           ('max', max_value), ('min_abs', min_abs)):
            if value is not None:
                params[key] = value
        if format in ('csv', 'tsv'):
            params['format'] = format
            return self._get_raw('/module-variability', params=params).text
        result = self._get('/module-variability', params=params)
        if format == 'dataframe':
            import pandas as pd
            rows = (result.get('rows', result)
                    if isinstance(result, dict) else result)
            return pd.DataFrame(rows)
        return result

    def rescan_module_variability(self) -> Dict:
        """``POST /module-variability/rescan``. Reload the parquet."""
        return self._post('/module-variability/rescan')

    # ── Catalogs (REST API 2.30+) ───────────────────────────────────────
    #
    # Unified view over every module catalog in the anthive stack —
    # currently ``source='geneset'`` (Sierksma/WGCNA-style, plain gene
    # lists) and ``source='starcat'`` (weighted starCAT programs). The
    # geneset side is a re-shaping of the older ``/genesets`` tree; the
    # starcat side had no REST surface before 2.30.
    #
    # ``catalog_id`` shape:
    #   - starcat  → one segment  (e.g. ``"MICROGLIA_V1_0"``)
    #   - geneset  → two segments (e.g. ``"Sierksma2025/WGCNA"``)
    # Both work on the same route templates (the server matches both
    # arities via a helper).

    @_cache_long(ttl=3600)
    def list_catalogs(_self,
                      source: Optional[str] = None) -> Dict:
        """``GET /catalogs[?source=geneset|starcat]``. Every module
        catalog on the server.

        Returns the discovery envelope::

            {"count": N,
             "catalogs": [{"source": "starcat", "catalog_id": "...",
                           "n_modules": ..., "n_scores": ...,
                           "n_genes": ...}, ...]}

        Args:
            source: Optional filter — ``'geneset'`` or ``'starcat'``.
                When ``None``, both sources are returned.
        """
        params = {'source': source} if source else None
        return _self._get('/catalogs', params=params)

    @_cache_long(ttl=3600)
    def get_catalog(_self, source: str, catalog_id: str) -> Dict:
        """``GET /catalogs/{source}/{catalog_id}``. One catalog with
        its module summaries + score recipes + metadata.

        ``catalog_id`` is a path-style key: ``'MICROGLIA_V1_0'`` for
        starcat (one segment), ``'Sierksma2025/WGCNA'`` for geneset
        (two segments) — both route to the same handler server-side.

        The response carries::

            {..., "modules": [{"name", "n_genes", "group", ...}, ...],
                  "scores":  [{"name", "kind", ...}, ...]}
        """
        return _self._get(f'/catalogs/{source}/{catalog_id}')

    @_cache_long(ttl=3600)
    def get_catalog_module(_self,
                           source: str,
                           catalog_id: str,
                           module_name: str,
                           top: Optional[int] = None) -> Dict:
        """``GET /catalogs/{source}/{catalog_id}/modules/{name}`` —
        one module's gene list ± weights.

        For starcat modules the response includes ``weights`` (a
        ``{gene: weight}`` dict, only when the requested slice covers
        the full module) and ``top_genes`` (list of
        ``{gene, weight, rank}`` ordered by ``abs(weight)``).

        For geneset modules the weights are ``None`` and ``top_genes``
        is the plain gene list.

        Args:
            source: ``'geneset'`` or ``'starcat'``.
            catalog_id: Path-style catalog key (see :meth:`get_catalog`).
            module_name: The module / program name inside the catalog.
            top: If set, return only the top-N genes (by ``|weight|``
                for starcat, first-N for geneset). Server clamps to
                ``1..5000``.
        """
        params = {'top': top} if top is not None else None
        return _self._get(
            f'/catalogs/{source}/{catalog_id}/modules/{module_name}',
            params=params,
        )

    @_cache_long(ttl=3600)
    def get_catalog_score(_self,
                          source: str,
                          catalog_id: str,
                          score_name: str) -> Dict:
        """``GET /catalogs/{source}/{catalog_id}/scores/{name}`` —
        one derived-score recipe.

        Returns the recipe dict (shape depends on the score ``kind``):

        - **composite** — linear combination of usage columns::

              {"name": "...", "kind": "composite",
               "columns": [...], "coefficients": [...],
               "normalization": "normalized"}

        - **discrete** — binary threshold (e.g. ``Border_CAM_pos``)::

              {"name": "Border_CAM_pos", "kind": "discrete",
               "columns": ["Microglia_Border_CAM"],
               "threshold": 0.0448,
               "normalization": "normalized"}

        - **multinomial** — argmax over a set of columns.

        Lets callers reproduce the score client-side, or explain what
        a binary ``*_pos`` sidecar column actually means.
        """
        return _self._get(
            f'/catalogs/{source}/{catalog_id}/scores/{score_name}',
        )

    def rescan_catalogs(self) -> Dict:
        """``POST /catalogs/rescan``. Invalidate the starcat + geneset
        catalog cache and reload from disk.

        Returns ``{"count": N, "catalogs": [...]}`` — the same shape
        as :meth:`list_catalogs` for the freshly reloaded registry.
        """
        return self._post('/catalogs/rescan')

    # ── Facets (fleet-wide ant_* obs columns) ───────────────────────────

    @_cache_short(ttl=60)
    def list_facets(_self, full: bool = False) -> Union[List[Dict], Dict]:
        """``GET /facets``. Fleet-wide inventory of ``ant_*`` obs columns.

        Returns a list of dicts, one per facet, with keys ``facet``,
        ``type`` (``'categorical'`` or ``'numerical'``), ``type_mixed``
        (True when the facet is categorical on some datasets and
        numerical on others), ``n_datasets`` (how many datasets carry
        this facet), and ``signal_strength`` (max variance-explained
        recorded in ``module_variability.parquet`` for this facet, or
        zero when no signal is on record; secondary datasets are
        ignored on newer servers).

        Newer servers (anthive main after 2.42.1) add ``label``,
        ``summary`` and ``defined`` (the published definition) and
        ``hidden_by_config``: True for facets outside the site's
        ``facets_visible`` list. The web UI hides those, the API still
        lists them.

        Rows are pre-sorted by ``n_datasets`` descending, then by facet
        name.

        Args:
            full: ``False`` (default) returns the ``facets`` list only,
                as before. ``True`` returns the whole response:
                ``{n_facets, facets, facets_visible,
                definitions_status}``. ``facets_visible`` is the site's
                list (empty means all are shown); ``definitions_status``
                (``state``, ``note``) says whether the definitions are
                settled (``'under_audit'`` while the facet audit runs).

        Example::

            facets = client.list_facets()
            for facet in facets[:5]:
                print(facet['facet'], facet['n_datasets'],
                      facet['signal_strength'])
        """
        response = _self._get('/facets')
        if full:
            if isinstance(response, dict):
                return response
            return {'n_facets': len(response), 'facets': response}
        return (response.get('facets', response)
                if isinstance(response, dict) else response)

    @_cache_short(ttl=60)
    def get_facet(_self, facet_name: str,
                  top_modules: int = 200,
                  per_facet_value_limit: Optional[int] = None,
                  mapping: bool = True,
                  exclude_secondary: bool = True) -> Dict:
        """``GET /facets/{facet_name}``. Detail page for one facet.

        Returns a dict describing this facet across every dataset that
        carries it. Keys: ``facet``, ``type``, ``type_mixed``,
        ``n_datasets``, ``total_n_cells``, ``datasets`` (per-dataset
        summaries with ``db_id``, ``title``, ``group``, ``group_id``,
        ``file_id``, ``n_cells``, ``kind_on_this_dataset``),
        ``value_distribution`` (categorical: per-value ``n_cells`` +
        ``n_datasets``; numerical: fleet min/max plus per-dataset
        min/max/mean), and ``modules`` (top rows from
        ``module_variability.parquet`` filtered on this facet, wrapped
        as ``{rows, n_total, truncated}``).

        Newer servers (anthive main after 2.42.1) add:

        - ``definition`` (``label``, ``summary``, ``definition``,
          ``kind``, ``values``, ``missing``, ``hint_keywords``,
          ``defined``, ``status`` (``registered`` or ``deprecated``),
          ``scope``, ``replaced_by``), ``definitions_status`` and
          ``hidden_by_config``. Registered facets include
          ``ant_cognitive_status``, ``ant_comorbidity``, ``ant_adnc``
          and ``ant_donor_uid``; treatment arms are a dataset-local
          ``treatment`` column, not a facet.
        - With ``mapping=True`` each ``datasets[]`` row also carries how
          the dataset projects onto the facet (``mapping_kind``:
          ``renamed``, ``derived``, ``constant``, ``native`` or
          ``computed``; ``source_columns``, ``sources``, ``value_map``,
          ``unmapped``, ``default``, ``constant_value``, ``n_missing``,
          ``range``, ``redacted`` ...; see
          :meth:`get_database_facets`), and ``non_carriers`` lists every
          other dataset with ``candidate_columns`` and ``declared``.
        - With ``exclude_secondary=True`` secondary datasets stay in
          ``datasets`` with ``is_secondary: True`` and ``counted:
          False`` but are left out of ``total_n_cells``,
          ``value_distribution`` and ``modules``;
          ``n_excluded_secondary`` counts them.

        Raises ``HTTPError`` 400 when ``facet_name`` does not start with
        ``ant_``, or 404 when no dataset carries it.

        Args:
            facet_name: Obs column name, must start with ``ant_``.
            top_modules: Cap on module-variability rows returned (1 to
                5000, default 200).
            per_facet_value_limit: Cap on distinct categorical values
                (1 to 2000; server default 200).
            mapping: ``False`` skips the per-dataset mapping and the
                non-carrier list (``non_carriers`` is then ``None``;
                faster). Sent only when ``False``.
            exclude_secondary: ``False`` counts secondary datasets in
                the pooled numbers. Sent only when ``False``.

        Example::

            detail = client.get_facet('ant_disease')
            print(detail['n_datasets'], detail['total_n_cells'])
            for value in detail['value_distribution']['values'][:5]:
                print(value['value'], value['n_cells'])
        """
        params: Dict[str, Any] = {'top_modules': top_modules}
        if per_facet_value_limit is not None:
            params['per_facet_value_limit'] = per_facet_value_limit
        if not mapping:
            params['mapping'] = 'false'
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        return _self._get(f'/facets/{facet_name}', params=params)

    def get_database_facets(self, db_id: str, counts: bool = True) -> Dict:
        """``GET /databases/{db_id}/facets``. How one dataset's author
        columns project onto the ``ant_*`` facets (anthive main after
        2.42.1).

        Returns ``{db_id, n_facets, facets, hidden_sources_visible,
        definitions_status}``. One ``facets`` entry per facet the
        dataset declares or serves, with ``facet``, ``label``,
        ``summary``, ``defined``, ``hidden_by_config``,
        ``mapping_kind`` (``renamed``, ``derived``, ``constant``,
        ``native`` or ``computed``, the last with ``computed_rule``),
        ``live``, ``dtype``, ``source_columns``, ``sources``
        (``column``, ``published_as``, ``hidden``), ``value_map`` (rows
        ``from``, ``to``, ``rule``, ``n_cells``; ``rule`` is ``mapped``,
        ``hidden``, ``passthrough`` or ``default``), ``unmapped``,
        ``default``, ``constant_value``, ``n_cells``, ``n_missing``,
        ``n_distinct_source_values``, ``value_map_truncated`` and
        ``redacted``.

        Columns in the dataset's ``api.obs_hidden`` stay private: for
        non-admins ``source_columns`` is empty, ``redacted`` is True and
        ``value_map`` rows have ``from: None``.

        Args:
            db_id: Dataset id.
            counts: ``False`` skips the cell counts (one ``GROUP BY``
                per facet on the server). Sent only when ``False``.
        """
        params = {'counts': 'false'} if not counts else None
        return self._get(f'/databases/{db_id}/facets', params=params)

    @_cache_short(ttl=60)
    def get_gene_variance_by_facet(_self,
                                   db_id: str,
                                   facet: str,
                                   top: int = 50) -> Dict:
        """``GET /databases/{db_id}/gene-variance-by-facet``. Rank genes
        by between-group variance of their mean expression across the
        groups defined by an ``ant_*`` facet on one dataset.

        Numerical facets are bucketed into five quantile bins (``q1``
        to ``q5``) server-side via DuckDB ``NTILE``. Categorical facets
        are grouped verbatim by value.

        Returns a dict with ``db_id``, ``facet``, ``facet_type``,
        ``layer``, ``n_groups``, ``groups`` (sorted group labels),
        ``n_cells_per_group`` (label to cell count), ``n_genes_returned``,
        and ``genes`` (list of ``{gene, signal, group_means}`` sorted by
        ``signal`` descending). ``signal`` is the sample variance across
        per-group mean expression. Genes only appear when they have
        non-zero expression in at least two groups.

        Raises ``HTTPError`` 400 when ``facet`` is not an ``ant_*``
        column, is all-NULL on the dataset, or resolves to a single
        group. 404 when the dataset or facet is not present.

        Args:
            db_id: Composite ``group/file`` dataset identifier.
            facet: Obs column to group by. Must start with ``ant_``.
            top: Cap on returned genes, sorted by signal descending
                (1 to 500, default 50).

        Example::

            top_genes = client.get_gene_variance_by_facet(
                'Blackburn2026/main', 'ant_disease', top=20,
            )
            for gene in top_genes['genes'][:5]:
                print(gene['gene'], gene['signal'], gene['group_means'])
        """
        params = {'facet': facet, 'top': top}
        return _self._get(
            f'/databases/{db_id}/gene-variance-by-facet', params=params,
        )

    # ── Donor-level facet tests (anthive main after 2.42.1) ─────────────
    #
    # Read-only views over the output of ant-facet-test (genes, limma-voom
    # on donor pseudobulks) and ant-facet-test-modules (module scores, OLS
    # plus label permutations). See FACET_TESTS_CAVEATS below and
    # doc/DESIGN_facet_testing.md in anthive4.

    #: Facets with donor-level test results. The facet endpoints take the
    #: short name or the ``ant_`` column (``'sex'`` or ``'ant_sex'``).
    TESTED_FACETS = ('sex', 'disease', 'apoe_carrier', 'braak')

    #: How to read facet test results. Repeated in each method's docstring
    #: so ``help()`` on one method is enough.
    FACET_TESTS_CAVEATS = (
        "Facet test results are exploratory. The unit of analysis is the "
        "donor: raw counts are summed per (donor, cell type) and each "
        "(dataset, cell type) unit is tested on its own; nothing is pooled "
        "and there is no meta-analysis. Datasets share donors, so rows are "
        "not independent replications. The disease contrast (AD vs control) "
        "is provisional: the vocabulary is not settled and 'control' means "
        "different things between studies. descriptive_only units have a "
        "level with fewer than 5 donors (Braak: fewer than 10); p, q, t and "
        "CI are null and only the effect estimate is reported. counted "
        "means the unit counts towards replication; not_counted_reason says "
        "why not. Replication = q < 0.05 with the same sign in k of m "
        "independent cohorts (DRAFT cohort table, a cohort counts once, cell "
        "types counted by group, secondary datasets and descriptive-only "
        "units never count)."
    )

    def _facet_rows(self, path: str, params: Dict[str, Any],
                    format: str) -> Union[Dict, str, Any]:
        """Shared GET for the facet-test row endpoints (json / csv /
        dataframe)."""
        if format not in ('json', 'csv', 'dataframe'):
            raise ValueError(
                f"format must be 'json', 'csv' or 'dataframe', got {format!r}")
        clean = {key: value for key, value in params.items()
                 if value is not None}
        if format == 'csv':
            clean['format'] = 'csv'
            return self._get_raw(path, params=clean).text
        result = self._get(path, params=clean)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('rows', []))
        return result

    def get_facet_tests(self, db_id: str) -> Dict:
        """``GET /databases/{db_id}/facet-tests``. Donor-level facet test
        summary of one dataset: one unit per (facet, cell type stratum).

        Exploratory, donor-level; the disease contrast is provisional.
        Genes are tested with limma-voom on donor pseudobulks
        (``expression ~ facet + sex + age + pmi + batch`` where
        available), module scores with OLS on donor-level scores plus
        label permutations. Contrasts: sex (m vs f), disease (AD vs
        control, provisional), apoe_carrier (e4 carrier vs
        non-carrier), braak (linear trend per stage).

        Top level: ``status`` (``tested``, ``not_tested``, ``not_run``),
        ``reason``, ``n_units``, ``n_units_tested``, ``q_threshold``,
        ``dataset_changed`` (the dataset's sha256 differs from the one
        the run used: results may be stale), ``files`` and ``runs``
        (tool, timestamp, seed, package versions, git commits, counts
        layer, batch column, permutations).

        Each unit: ``facet``, ``cell_type``, ``celltype_source``,
        ``tested`` (``reason`` when not), ``contrast``, ``level_ref`` /
        ``level_test``, ``level_counts`` (donors per level),
        ``n_donors(_ref/_test)``, ``covariates``, ``flags``
        (``descriptive_only``, ``n_lt5``, ``provisional``,
        ``composition_flag``, ``composition_p``, ``batch_used``,
        ``batch_confounded``), ``genes`` (``n_tested``, ``n_q05``),
        ``modules`` (per method and source: ``n_modules``,
        ``n_q05_param``, ``n_q05_perm``) and ``model``.

        ``model`` is built from the run manifests: ``stratum``,
        ``facet_term``, ``donors`` (per level, analysed, dropped for a
        missing covariate, minimum cells per donor),
        ``descriptive_only_rule`` and, per run (``genes``,
        ``modules``), ``unit_of_analysis``, ``formula`` (for example
        ``expression ~ braak + sex + age_z + pmi_z``), ``covariates``
        (each with ``status`` included / dropped / not used and the
        ``reason``), ``method``, ``multiple_testing``, ``tool``,
        ``seed``, ``run_date``, ``versions``. Anything a manifest does
        not record reads ``'not recorded'``.

        Reading the flags: ``descriptive_only`` means a level has fewer
        than 5 donors (Braak: fewer than 10), so p, q, t and CI are null
        and only the effect is reported. ``composition_flag`` means the
        share of the donor's cells in this stratum differs with the
        facet (p < 0.05, uncorrected): an effect may reflect composition
        rather than expression. ``provisional`` marks the disease
        contrast. Absence of hits is not evidence of no effect: typical
        units have 8 to 90 donors, so only large effects are detected in
        the small ones.
        """
        return self._get(f'/databases/{db_id}/facet-tests')

    def get_facet_test_genes(self,
                             db_id: str,
                             facet: Optional[str] = None,
                             cell_type: Optional[str] = None,
                             celltype_source: Optional[str] = None,
                             gene: Optional[str] = None,
                             *,
                             q: float = 0.05,
                             sort: str = 'q',
                             limit: int = 100,
                             format: str = 'json') -> Union[Dict, str, Any]:
        """``GET /databases/{db_id}/facet-tests/genes``. Gene-level donor
        pseudobulk results (limma-voom) of one dataset.

        Exploratory, donor-level (n = donors, not cells); the disease
        contrast is provisional. q is Benjamini-Hochberg within one
        (dataset, stratum, facet), so q values of different units are
        not comparable families.

        Rows: ``facet, cell_type, celltype_source, contrast, level_ref,
        level_test, gene, logFC, se, ci_low, ci_high, t, p_value,
        q_value, ave_expr`` (mean log2 CPM), ``n_donors, n_donors_ref,
        n_donors_test, descriptive_only, provisional``.
        ``descriptive_only`` rows have null p, q, t and CI (a level has
        too few donors) and only appear with ``q=1``.

        Args:
            db_id: Dataset id.
            facet: ``sex``, ``disease``, ``apoe_carrier`` or ``braak``.
            cell_type: Cell type stratum as in :meth:`get_facet_tests`.
            celltype_source: Stratifier, e.g. ``constant_all_cells``.
            gene: One gene symbol (case-insensitive).
            q: Keep ``q_value < q`` (default 0.05); ``q=1`` keeps every
                row, descriptive-only ones included.
            sort: ``'q'`` (then ``|logFC|``), ``'effect'`` (``|logFC|``),
                ``'p'`` or ``'gene'``.
            limit: Rows returned (server max 2000 for json, 100000 for
                csv; larger values give HTTP 400).
            format: ``'json'`` (``{db_id, <filters>, n_matching,
                n_returned, rows}``), ``'dataframe'`` (the rows, needs
                pandas) or ``'csv'`` (text).

        Raises:
            requests.HTTPError: 404 when the dataset has no gene results,
                400 for a bad ``sort`` or ``limit``.
        """
        return self._facet_rows(
            f'/databases/{db_id}/facet-tests/genes',
            {'facet': facet, 'cell_type': cell_type,
             'celltype_source': celltype_source, 'gene': gene,
             'q': q, 'sort': sort, 'limit': limit}, format)

    def get_facet_test_modules(self,
                               db_id: str,
                               facet: Optional[str] = None,
                               cell_type: Optional[str] = None,
                               celltype_source: Optional[str] = None,
                               *,
                               method: Optional[str] = None,
                               source: Optional[str] = None,
                               module: Optional[str] = None,
                               q: float = 0.05,
                               q_by: str = 'either',
                               sort: str = 'q',
                               limit: int = 100,
                               format: str = 'json') -> Union[Dict, str, Any]:
        """``GET /databases/{db_id}/facet-tests/modules``. Module-level
        donor results (OLS on donor-level module scores, with
        permutation p values) of one dataset.

        Exploratory, donor-level; the disease contrast is provisional.
        BH families are (dataset, stratum, facet, method, source).
        ``p_perm`` cannot go below 1/(n + 1) for n permutations (10,000
        by default, recorded per run), so ``q_perm`` has a floor; read
        ``q_param`` and ``p_perm`` together.

        Rows: ``facet, cell_type, celltype_source, contrast, level_ref,
        level_test, method, source, catalog, module, estimate, se,
        ci_low, ci_high, std_effect`` (estimate / SD of the donor-level
        score), ``t, df, p_param, q_param, p_perm, q_perm,
        n_perm_effective, n_genes_requested, n_genes_found,
        n_donors(_ref/_test), descriptive_only, provisional,
        no_counts``.

        Args:
            db_id, facet, cell_type, celltype_source: As in
                :meth:`get_facet_test_genes`.
            method: ``'cell_mean'`` or ``'pseudobulk'``.
            source: ``'geneset'``, ``'ucell'`` or ``'gene_list'``.
            module: One module name (case-insensitive).
            q: q threshold (default 0.05); ``q=1`` keeps every row.
            q_by: Which q the threshold applies to: ``'param'``,
                ``'perm'`` or ``'either'`` (default).
            sort: ``'q'`` (``q_param``, then ``|std_effect|``),
                ``'q_perm'``, ``'effect'`` or ``'module'``.
            limit: Rows returned (max 2000 json, 100000 csv).
            format: ``'json'``, ``'dataframe'`` or ``'csv'``.
        """
        return self._facet_rows(
            f'/databases/{db_id}/facet-tests/modules',
            {'facet': facet, 'cell_type': cell_type,
             'celltype_source': celltype_source, 'method': method,
             'source': source, 'module': module, 'q': q, 'q_by': q_by,
             'sort': sort, 'limit': limit}, format)

    def get_facet_test_volcano(self, db_id: str, facet: str, cell_type: str,
                               celltype_source: Optional[str] = None,
                               dest: Union[str, Path, None] = None,
                               ) -> Union[bytes, Path]:
        """``GET /databases/{db_id}/facet-tests/volcano.png``. Small
        volcano plot (log2 fold change against -log10 p) of one unit's
        gene results; q < 0.05 in colour. Exploratory.

        Args:
            db_id: Dataset id.
            facet: ``sex``, ``disease``, ``apoe_carrier`` or ``braak``.
            cell_type: Cell type stratum.
            celltype_source: Stratifier, when a cell type occurs under
                several.
            dest: ``None`` returns the PNG bytes; a path writes them
                there and returns the path.

        Raises:
            requests.HTTPError: 404 when the unit has no p values
                (missing results or a descriptive-only unit).
        """
        params: Dict[str, Any] = {'facet': facet, 'cell_type': cell_type}
        if celltype_source is not None:
            params['celltype_source'] = celltype_source
        content = self._get_raw(f'/databases/{db_id}/facet-tests/volcano.png',
                                params=params).content
        if dest is None:
            return content
        target = Path(dest)
        target.write_bytes(content)
        return target

    def get_facet_tests_across_datasets(self, facet: str,
                                        top_genes: int = 5,
                                        exclude_secondary: bool = True,
                                        format: str = 'json',
                                        ) -> Union[Dict, Any]:
        """``GET /facets/{facet}/tests``. Every tested (dataset, cell
        type stratum) of one facet, sorted by donors analysed.

        Exploratory, donor-level; the disease contrast is provisional.
        No pooling: datasets are tested independently and share donors
        (ROSMAP, SEA-AD, MADRC, ...), so rows are not independent
        replications.

        Returns ``{facet, facet_column, exclude_secondary, n_rows,
        n_datasets_tested, rows, not_tested, q_threshold}``. Each row
        carries the unit fields of :meth:`get_facet_tests` (contrast,
        donors per level, covariates, flags, genes and modules at
        q < 0.05, ``model``) plus ``db_id``, ``title``, ``group``,
        ``top_genes`` (``gene``, ``logFC``, ``q_value``), ``run_date``,
        ``dataset_changed``, ``cohort``, ``cohort_label``, ``role``,
        ``counted``, ``is_secondary`` and ``celltype_group``.
        ``not_tested`` lists datasets whose units for this facet could
        not be tested, with the reason.

        Args:
            facet: ``'sex'`` / ``'ant_sex'``, also disease, apoe_carrier,
                braak (see :attr:`TESTED_FACETS`). Others give 404.
            top_genes: Genes at q < 0.05 listed per row (0 to 50).
            exclude_secondary: Secondary datasets are listed with
                ``is_secondary: True`` and ``counted: False`` either way;
                ``False`` lets them count. Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas; nested fields stay as objects).
        """
        params: Dict[str, Any] = {'top_genes': top_genes}
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        result = self._get(f'/facets/{facet}/tests', params=params)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('rows', []))
        return result

    def get_facet_gene_effects(self, facet: str, gene: str,
                               cell_type: Optional[str] = None,
                               exclude_secondary: bool = True,
                               format: str = 'json') -> Union[Dict, Any]:
        """``GET /facets/{facet}/effects?gene=``. Result card of one
        gene for one facet: every tested (dataset, cell type) unit.

        Exploratory, donor-level; each unit is one dataset and cell
        type tested separately (limma-voom, n = donors); nothing is
        pooled and there is no meta-analysis. The disease contrast is
        provisional. The cohort table is a DRAFT.

        Returns ``{query, facet, gene, n_units, units, replication,
        cell_types, notes, apoe_locus, definition}``.

        - ``facet``: ``name``, ``column``, ``contrast``,
          ``effect_unit`` (e.g. log2 fold change, male minus female),
          ``provisional``, ``status``.
        - ``units``: ``db_id``, ``title``, ``cell_type`` (raw label),
          ``celltype_source``, ``celltype_group``, ``cohort``,
          ``cohort_label``, ``role``, ``counted``,
          ``not_counted_reason``, ``is_secondary``, ``logFC``,
          ``ci_low``, ``ci_high``, ``q_value``, ``q_counted``,
          ``n_sibling_strata``, ``significant``, donors per level,
          ``descriptive_only``, ``flags``, ``model`` (the formula),
          ``method``, ``covariates_left_out``, ``run_date``, ``tool``.
        - ``replication``: one entry per cell type group with
          ``celltype_group`` (also in ``cell_type``), ``cl_id``,
          ``labels`` (the raw labels it covers), ``cohorts_tested``
          (m), ``cohorts_sig_same_sign`` (k), ``direction``,
          ``sign_discordant``, ``cohorts_up``, ``cohorts_down`` and
          ``badge`` (the sentence the web UI shows).

        How to read it: ``counted`` means the unit counts towards
        replication (counted cohort, not descriptive only, not a
        secondary dataset, the dataset's most direct cell type source
        for the group); ``not_counted_reason`` says why not. When
        several strata of one dataset fall into one group, their q is
        multiplied by ``n_sibling_strata`` (Bonferroni) into
        ``q_counted`` before the q < 0.05 call. ``definition`` holds the
        full replication rule: q < 0.05 with the same sign in k of m
        independent cohorts, a cohort counts once.
        ``descriptive_only`` units have no p or q. For APOE locus genes
        (APOE, APOC1, TOMM40, NECTIN2, ...) an e4 effect can be
        allele-linked expression or read mapping, not downstream
        biology (``notes``).

        Args:
            facet: ``'sex'`` / ``'ant_sex'``, also disease, apoe_carrier,
                braak.
            gene: Gene symbol (case-insensitive).
            cell_type: A cell type group (``'microglia'``) or a raw
                stratum label (``'Mic P2RY12'``).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the units, needs
                pandas).
        """
        params: Dict[str, Any] = {'gene': gene}
        if cell_type is not None:
            params['cell_type'] = cell_type
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        result = self._get(f'/facets/{facet}/effects', params=params)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('units', []))
        return result

    def get_facet_recurring_genes(self, facet: str,
                                  cell_type: Optional[str] = None,
                                  n: int = 50,
                                  exclude_secondary: bool = True,
                                  format: str = 'json') -> Union[Dict, Any]:
        """``GET /facets/{facet}/effects/top``. Genes that change with a
        facet in the most independent cohorts, within one cell type
        group.

        Exploratory, donor-level, no meta-analysis; the disease contrast
        is provisional and the cohort table is a DRAFT. A high rank is a
        count of separate significant results, not a pooled estimate.

        Ranking (``ranking`` in the response): (1) k, the cohorts with
        q < 0.05 and the same sign, highest first; (2) ``agreement``,
        the fraction of the m tested cohorts whose effect has that sign
        (significant or not), highest first; (3) ``median_abs_logFC``
        over the m tested cohorts, largest first. The number of cohorts
        tested is never a ranking key by itself, so "2 of 2" does not
        outrank "2 of 4".

        Returns ``{facet, cell_type, celltype_group, cl_id, labels,
        cohorts_in_cell_type, cell_types, rows, definition, ranking,
        exclude_secondary, cohort_table_status}``. Rows: ``gene,
        cohorts_sig_same_sign`` (k), ``cohorts_tested`` (m),
        ``cohorts_same_sign, agreement, direction, sign_discordant,
        units_up, units_down, median_abs_logFC, max_abs_logFC, cohorts,
        apoe_locus``. ``cell_types`` lists the groups with their raw
        ``labels`` and counted cohorts.

        Args:
            facet: ``'sex'`` / ``'ant_sex'``, also disease, apoe_carrier,
                braak.
            cell_type: A cell type group (``'microglia'``) or a raw
                label; default: the group with most counted cohorts.
            n: Rows returned (1 to 500).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas).
        """
        params: Dict[str, Any] = {'n': n}
        if cell_type is not None:
            params['cell_type'] = cell_type
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        result = self._get(f'/facets/{facet}/effects/top', params=params)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('rows', []))
        return result

    def get_gene_facet_effects(self, gene: str,
                               exclude_secondary: bool = True) -> Dict:
        """``GET /genes/{gene}/facet-effects``. Result cards of one gene
        for every tested facet (sex, disease, apoe_carrier, braak).

        Returns ``{gene, facets: {facet: card}}``; each card has the
        shape of :meth:`get_facet_gene_effects`. Exploratory,
        donor-level, no meta-analysis; the disease card is provisional.
        The first call per facet reads every result file on the server
        (a few seconds); later calls are cached.

        Args:
            gene: Gene symbol (case-insensitive).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
        """
        params = {'exclude_secondary': 'false'} if not exclude_secondary else None
        return self._get(f'/genes/{gene}/facet-effects', params=params)

    def get_facet_cohorts(self, format: str = 'json') -> Union[Dict, Any]:
        """``GET /facets/cohorts``. The DRAFT cohort selection table used
        to count independent cohorts.

        Returns ``{status: 'DRAFT', n_datasets, datasets}``; one row per
        dataset with ``db_id``, ``cohort``, ``cohort_label``, ``role``,
        ``cell_class``, ``counted`` and ``note``. Datasets of one cohort
        share donors and count once towards replication; roles such as
        duplicates, supplements and alternatives do not count. Empty
        when the server has no table.

        Args:
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas).
        """
        result = self._get('/facets/cohorts')
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('datasets', []))
        return result

    # ── Dataset summaries + downloads ───────────────────────────────────

    def get_obs_summary(self, db_id: str, top_n: int = 3) -> Dict:
        """``GET /databases/{db_id}/obs_summary``. Light stats for every
        obs column in one call (categorical: ``n_unique``, ``pct_missing``,
        top values; numerical: ``min``, ``max``, ``median``)."""
        return self._get(f'/databases/{db_id}/obs_summary',
                         params={'top_n': top_n})

    def get_obs_top(self, db_id: str, n: int = 8) -> Dict:
        """``GET /databases/{db_id}/obs_top``. The obs columns users click
        most often on this dataset."""
        return self._get(f'/databases/{db_id}/obs_top', params={'n': n})

    @_cache_long(ttl=3600)
    def get_mus_vocab(_self) -> Dict:
        """``GET /mus/vocab``. The mus annotation vocabulary the server
        validates ``.h5ad.mus`` fields against."""
        return _self._get('/mus/vocab')

    def list_database_files(self, db_id: str) -> List[Dict]:
        """``GET /databases/{db_id}/files``. Downloadable files of one
        dataset: the ``.h5ad`` and the collection's ``static/`` files.

        Each row is ``{kind, filename, size_bytes, source}``; static
        files have ``kind='static'``. Only ``kind='h5ad'`` can be fetched
        with :meth:`download_database_file`; static files go through
        :meth:`download_static_file`.
        """
        response = self._get(f'/databases/{db_id}/files')
        return (response.get('files', response)
                if isinstance(response, dict) else response)

    def download_database_file(self, db_id: str, kind: str = 'h5ad',
                               dest: Union[str, Path, None] = None,
                               timeout: Optional[float] = None,
                               ) -> Union[bytes, Path]:
        """``GET /databases/{db_id}/download/{kind}``. Download the
        dataset's ``.h5ad`` (the only kind the server serves).

        S3-backed files come as a redirect to a short-lived presigned URL,
        which ``requests`` follows (dropping the Basic-auth header for the
        other host). See :meth:`download_paper_pdf` for ``dest``.
        ``timeout`` defaults to no read timeout, since h5ad files can be
        many GB.
        """
        return self._download(f'/databases/{db_id}/download/{kind}', dest,
                              default_name=f"{db_id.replace('/', '_')}.{kind}",
                              timeout=timeout)

    def download_static_file(self, db_id: str, filename: str,
                             dest: Union[str, Path, None] = None,
                             ) -> Union[bytes, Path]:
        """``GET /databases/{db_id}/download-static/{filename}``. One file
        from the collection's ``static/`` folder (supplementary tables,
        protocols). Paper PDFs live in the paper library instead
        (:meth:`download_paper_pdf`)."""
        return self._download(
            f'/databases/{db_id}/download-static/{filename}', dest,
            default_name=filename)

    # ── Paper library (REST API 2.39+) ──────────────────────────────────
    #
    # One BibLaTeX-compatible record per paper (any document type: articles,
    # preprints, theses, reports, proposals), linked to datasets with a role.
    # Everyone who can sign in to the site sees the whole library (2.41).

    def list_papers(self,
                    *,
                    role: Optional[str] = None,
                    celltype: Optional[str] = None,
                    tissue: Optional[str] = None,
                    disease: Optional[str] = None,
                    method: Optional[str] = None,
                    cohort: Optional[str] = None,
                    organism: Optional[str] = None,
                    author: Optional[str] = None,
                    year: Union[int, str, None] = None,
                    q: Optional[str] = None,
                    format: str = 'list') -> Union[List[Dict], Any]:
        """``GET /papers``. Papers in the library, newest first.

        Every filter is optional and they combine. ``year`` is ``2024`` or
        a range ``'2020..2024'``; ``q`` matches words against title,
        authors, DOI, summary and topics. ``role`` is one of ``source``,
        ``reanalysis``, ``method``, ``related``, ``background``.

        Returns the list of paper summaries (the server's ``n`` count is
        ``len(result)``), or a DataFrame with ``format='dataframe'``.
        """
        params = {key: value for key, value in (
            ('role', role), ('celltype', celltype), ('tissue', tissue),
            ('disease', disease), ('method', method), ('cohort', cohort),
            ('organism', organism), ('author', author),
            ('year', None if year is None else str(year)), ('q', q),
        ) if value is not None}
        response = self._get('/papers', params=params or None)
        papers = (response.get('papers', [])
                  if isinstance(response, dict) else response)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(papers)
        return papers

    def get_paper(self, citekey: str) -> Dict:
        """``GET /papers/{citekey}``. One paper: bibliographic fields,
        notes, linked datasets and gene sets by role, related papers.
        Raises ``HTTPError`` 404 for an unknown citekey."""
        return self._get(f'/papers/{citekey}')

    def get_related_papers(self, citekey: str,
                           kind: Union[str, Sequence[str], None] = None,
                           limit: int = 25) -> List[Dict]:
        """``GET /papers/{citekey}/related``. Related papers, each with
        the kind, weight and evidence of the relation.

        Args:
            citekey: The paper.
            kind: Restrict to these relation kinds: ``explicit``,
                ``cohort``, ``celltype``, ``tissue``, ``disease``,
                ``method``, ``author``, ``cites``, ``cited_by``.
            limit: 1 to 200 (default 25).
        """
        params: Dict[str, Any] = {'limit': limit}
        if kind:
            params['kind'] = _csv_param(kind)
        return self._get(f'/papers/{citekey}/related',
                         params=params).get('related', [])

    def get_paper_page(self, citekey: str, page: int) -> Dict:
        """``GET /papers/{citekey}/pages/{page}``. Plain text of one page
        from the literature index: ``{citekey, page, n_pages, text}``."""
        return self._get(f'/papers/{citekey}/pages/{page}')

    def get_paper_bibtex(self, citekey: str, classic: bool = False) -> str:
        """``GET /papers/{citekey}.bib``. One paper as a BibLaTeX entry
        (``classic=True``: plain BibTeX)."""
        params = {'classic': 'true'} if classic else None
        return self._get_raw(f'/papers/{citekey}.bib', params=params).text

    def get_library_bibtex(self,
                           citekeys: Union[str, Sequence[str], None] = None,
                           classic: bool = False) -> str:
        """``GET /library.bib``. The whole library, or the given
        citekeys, as one BibLaTeX (``classic=True``: BibTeX) file."""
        params: Dict[str, Any] = {}
        if citekeys:
            params['citekeys'] = _csv_param(citekeys)
        if classic:
            params['classic'] = 'true'
        return self._get_raw('/library.bib', params=params or None).text

    def download_paper_pdf(self, citekey: str,
                           dest: Union[str, Path, None] = None,
                           ) -> Union[bytes, Path]:
        """``GET /papers/{citekey}/file``. The paper's PDF.

        The server streams it, or redirects to a presigned S3 link that
        ``requests`` follows (the Basic-auth header is not sent to the
        other host).

        Args:
            citekey: The paper.
            dest: ``None`` returns the bytes. A directory saves the PDF
                there under the server's file name (else
                ``<citekey>.pdf``); any other path is the target file.

        Returns:
            The PDF bytes, or the :class:`~pathlib.Path` written.

        Raises:
            requests.HTTPError: 404 when the paper is unknown or has no
                PDF.
        """
        return self._download(f'/papers/{citekey}/file', dest,
                              default_name=f'{citekey}.pdf')

    def search_literature(self, q: str,
                          k: int = 10,
                          mode: str = 'hybrid',
                          *,
                          section: Union[str, Sequence[str], None] = None,
                          citekey: Union[str, Sequence[str], None] = None,
                          year: Union[int, str, None] = None) -> Dict:
        """``GET /search/literature``. Passages from the library's papers.

        Returns ``{query, mode, results, warnings}``; each result has the
        citation, page, section and a highlighted snippet. Semantic and
        hybrid modes fall back to keyword search (with a warning) when the
        server has no query model.

        Args:
            q: Query text (at least 2 characters).
            k: Passages to return (1 to 50).
            mode: ``'hybrid'`` (default), ``'keyword'`` or ``'semantic'``.
            section: Restrict to ``abstract``, ``introduction``,
                ``methods``, ``results``, ``discussion``,
                ``figure_legend``.
            citekey: Search only within these papers.
            year: ``2024`` or ``'2020..2024'``.
        """
        if mode not in ('hybrid', 'keyword', 'semantic'):
            raise ValueError(
                f"mode must be 'hybrid', 'keyword' or 'semantic', got {mode!r}")
        params: Dict[str, Any] = {'q': q, 'k': k, 'mode': mode}
        if section:
            params['section'] = _csv_param(section)
        if citekey:
            params['citekey'] = _csv_param(citekey)
        if year is not None:
            params['year'] = str(year)
        return self._get('/search/literature', params=params)

    def get_database_papers(self, db_id: str) -> List[Dict]:
        """``GET /databases/{db_id}/papers``. Papers linked to a dataset
        (its own and its collection's ``papers:``), source papers first,
        each with its role."""
        return self._get(f'/databases/{db_id}/papers').get('papers', [])

    def get_database_context(self, db_id: str, level: str = 'brief',
                             budget_tokens: int = 4000) -> Dict:
        """``GET /databases/{db_id}/context``. Background pack for
        analysing a dataset with an LLM: metadata, caveats, obs columns,
        module scores, source papers with methods passages and related
        papers, trimmed to about ``budget_tokens`` (200 to 50000).
        ``level`` is ``'brief'`` or ``'full'``."""
        return self._get(f'/databases/{db_id}/context',
                         params={'level': level,
                                 'budget_tokens': budget_tokens})

    def get_library_status(self) -> Dict:
        """``GET /library/status``. Library and literature-index health:
        counts, issues, unresolved references, embedding model, and
        ``upload_index`` (state of the last upload's background index
        update)."""
        return self._get('/library/status')

    # ── Paper upload (REST API 2.40+, uploaders only) ───────────────────
    #
    # Only callers in the site's `library_upload_groups` (default admins;
    # in basic-auth mode the users in `auth.admins`) may see or use these.
    # Everyone else gets PaperUploadForbiddenError (403). A site whose
    # library is read-only answers PaperUploadDisabledError (503).

    def get_paper_upload_requirements(self) -> Dict:
        """``GET /papers/upload/requirements``. What a record must and
        should contain.

        Keys: ``notice`` (who sees the library; its text may go to AI
        providers), ``uploads_enabled``, ``required``,
        ``required_detail``, ``recommended``, ``fields`` (description,
        example, type, vocab per field), ``roles`` (role to meaning),
        ``default_role`` (``background``), ``folders``, ``entrytypes``
        (entrytype to description), ``document_types`` (suggested
        ``type`` values per entrytype), ``max_pdf_mb``.

        Raises:
            PaperUploadForbiddenError: the caller may not upload.
        """
        response = self._request('GET', '/papers/upload/requirements')
        raise_for_upload_status(response)
        return response.json()

    def upload_paper(self,
                     metadata: Union[Mapping[str, Any], str, Path],
                     pdf: Union[str, Path, bytes, BinaryIO, None] = None,
                     *,
                     folder: Optional[str] = None,
                     role: Optional[str] = None,
                     papers_for: Union[Mapping[str, str], Sequence[str],
                                       str, None] = None,
                     enrich: bool = True,
                     dry_run: bool = False,
                     update: bool = False,
                     pdf_filename: Optional[str] = None,
                     timeout: Optional[float] = 300) -> Dict:
        """``POST /papers/upload``. Add one paper (record + optional PDF)
        to the library.

        The server validates the record, fills empty fields from CrossRef
        and OpenAlex (``enrich``), refuses duplicates (by DOI, PMID, PDF
        sha256 and citekey), places it in a folder and commits it with the
        caller as author.

        Args:
            metadata: The record. A dict (sent as JSON), a ``str`` holding
                JSON or the text of a ``.md`` record with YAML front
                matter, or a :class:`~pathlib.Path` to such a file.
                Required: ``entrytype``, ``title``, ``author`` (list of
                ``'Last, First'``), ``date`` or ``year``, and an
                identifier (``doi``, ``pmid``, ``eprint`` +
                ``eprinttype``, or ``url``) for article, online, book,
                inbook, incollection and inproceedings; report, thesis,
                unpublished and misc need ``institution`` or ``note``.
                See :meth:`get_paper_upload_requirements`.
            pdf: The PDF as a path, bytes or a binary file object.
            folder: ``lab``, ``datasets``, ``methods``, ``reading`` or
                ``proposals``; default chosen by the server's rules.
            role: Sets ``anthive_role`` (``source``, ``reanalysis``,
                ``method``, ``related``, ``background``). Without it the
                server uses ``background``. Use ``source`` or
                ``reanalysis`` only when the data in anthive come from
                the paper.
            papers_for: Datasets or collections this paper belongs to, as
                ``{'L26/FMG': 'source'}``, ``['L26/FMG', 'L26:source']``
                or a comma-separated string; a bare id gets the link role
                ``source`` from the server. Stored on the paper as a
                proposal (``anthive_proposed_links``) for a curator; it
                does not change ``anthive_role``.
            enrich: Fill empty fields from CrossRef / OpenAlex.
            dry_run: Validate and enrich, write nothing.
            update: Merge into the existing record when the paper is
                already in the library (instead of a 409).
            pdf_filename: File name sent for the PDF part.
            timeout: Request timeout in seconds (default 300; enrichment
                calls external services).

        Returns:
            The server's answer: ``status`` (``created``, ``updated`` or
            ``dry_run``), ``citekey``, ``folder``, ``path``,
            ``pdf_sha256``, ``record``, ``filled_fields``, ``warnings``,
            ``missing_recommended``.

        Raises:
            PaperUploadForbiddenError: 403, the caller may not upload.
            PaperUploadDisabledError: 503, uploads are off on this site.
            PaperDuplicateError: 409, see ``existing_citekey`` and
                ``matched_on``.
            PaperInvalidError: 422, see ``errors`` and
                ``missing_required``.
            PaperTooLargeError: 413.
            PaperNotPDFError: 415.
            ValueError: ``role`` / ``papers_for`` given with ``.md``
                text metadata (put them in the front matter instead).
        """
        metadata_text = _metadata_to_text(metadata, role=role,
                                          papers_for=papers_for)
        data: Dict[str, Any] = {
            'metadata': metadata_text,
            'enrich': 'true' if enrich else 'false',
            'dry_run': 'true' if dry_run else 'false',
            'update': 'true' if update else 'false',
        }
        if folder:
            data['folder'] = folder
        files = None
        opened = None
        if pdf is not None:
            if isinstance(pdf, (str, os.PathLike)):
                pdf_path = Path(pdf)
                opened = open(pdf_path, 'rb')
                content: Any = opened
                name = pdf_filename or pdf_path.name
            elif isinstance(pdf, (bytes, bytearray)):
                content = bytes(pdf)
                name = pdf_filename or 'paper.pdf'
            else:
                content = pdf
                name = pdf_filename or Path(
                    getattr(pdf, 'name', '') or 'paper.pdf').name
            files = {'file': (name, content, 'application/pdf')}
        else:
            # Force multipart/form-data even without a file part.
            files = {'metadata': (None, data.pop('metadata'))}
        try:
            response = self._request('POST', '/papers/upload', data=data,
                                     files=files, timeout=timeout)
        finally:
            if opened is not None:
                opened.close()
        raise_for_upload_status(response)
        return response.json()

    def upload_papers(self,
                      items: Iterable[Union[Mapping[str, Any],
                                            Tuple[Any, Any]]],
                      *,
                      stop_on_error: bool = False,
                      **defaults: Any) -> List[Dict]:
        """Upload several papers, one request each (the server has no bulk
        endpoint).

        Args:
            items: Each item is a dict of :meth:`upload_paper` arguments
                (at least ``metadata``; ``pdf``, ``folder``, ``role``,
                ``papers_for``, ...), or a ``(metadata, pdf)`` tuple.
                :meth:`paper_items_from_folder` builds these from a
                folder of ``.md`` records with sibling PDFs.
            stop_on_error: Re-raise the first refused upload instead of
                recording it and going on.
            **defaults: :meth:`upload_paper` keyword arguments applied to
                every item (an item's own value wins), e.g.
                ``dry_run=True``.

        Returns:
            One dict per item, in order: ``{"index", "ok", "status",
            "citekey", "result"}`` on success, ``{"index", "ok": False,
            "status", "http_status", "error", "body"}`` on a refused
            upload (``status`` is the server's, e.g. ``duplicate`` or
            ``invalid``).

        Raises:
            PaperUploadForbiddenError, PaperUploadDisabledError: always
                raised at once; no later item could succeed either.
        """
        from .exceptions import (PaperUploadDisabledError,
                                 PaperUploadError,
                                 PaperUploadForbiddenError)
        outcomes: List[Dict] = []
        for index, item in enumerate(items):
            if isinstance(item, tuple):
                metadata, pdf = item
                arguments: Dict[str, Any] = {'metadata': metadata, 'pdf': pdf}
            else:
                arguments = dict(item)
            merged = {**defaults, **arguments}
            try:
                result = self.upload_paper(**merged)
            except (PaperUploadForbiddenError, PaperUploadDisabledError):
                raise
            except PaperUploadError as exc:
                if stop_on_error:
                    raise
                outcomes.append({
                    'index': index, 'ok': False,
                    'status': exc.body.get('status', 'error'),
                    'http_status': exc.status_code,
                    'error': exc.message, 'body': exc.body,
                })
                continue
            outcomes.append({
                'index': index, 'ok': True, 'status': result.get('status'),
                'citekey': result.get('citekey'), 'result': result,
            })
        return outcomes

    @staticmethod
    def paper_items_from_folder(folder: Union[str, Path],
                                pattern: str = '*.md') -> List[Dict]:
        """Items for :meth:`upload_papers` from a folder of ``.md`` records:
        each ``name.md`` with its sibling ``name.pdf`` when there is one.
        ``README.md`` and ``REVIEW.md`` are skipped."""
        folder = Path(folder)
        items: List[Dict] = []
        for record in sorted(folder.glob(pattern)):
            if record.name in ('README.md', 'REVIEW.md'):
                continue
            pdf = record.with_suffix('.pdf')
            items.append({'metadata': record,
                          'pdf': pdf if pdf.exists() else None})
        return items

    # ── download helper ─────────────────────────────────────────────────

    def _download(self, path: str, dest: Union[str, Path, None],
                  default_name: str,
                  timeout: Optional[float] = None) -> Union[bytes, Path]:
        """GET ``path`` (following redirects); return bytes, or stream to
        ``dest`` (a file, or a directory to save under the server's
        file name)."""
        read_timeout = timeout if timeout is not None else (
            self.timeout if dest is None else None)
        response = self._request('GET', path, stream=dest is not None,
                                 timeout=read_timeout, allow_redirects=True)
        try:
            response.raise_for_status()
            if dest is None:
                return response.content
            target = Path(dest)
            if target.is_dir():
                name = _filename_from_disposition(
                    response.headers.get('Content-Disposition', ''))
                target = target / (name or default_name)
            with open(target, 'wb') as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)
            return target
        finally:
            response.close()

    # ── Plot cache admin ────────────────────────────────────────────────

    def get_plot_cache_stats(self) -> Dict:
        """``GET /admin/plot-cache/stats``."""
        return self._get('/admin/plot-cache/stats')

    def clear_plot_cache(self) -> Dict:
        """``POST /admin/plot-cache/clear``."""
        return self._post('/admin/plot-cache/clear')

    # ── Skill (MCP collaborator install path) ───────────────────────────

    def get_skill(self) -> str:
        """``GET /skill``. Fetch the anthive Claude Code SKILL.md."""
        return self._get_raw('/skill').text

    # ── Admin ───────────────────────────────────────────────────────────

    def rescan_databases(self) -> Dict:
        """``POST /admin/rescan``. Re-scan data folder + reload caches."""
        return self._post('/admin/rescan')

    def admin_release(self, db_id: str,
                      duration_seconds: Optional[float] = None,
                      drain_timeout_seconds: Optional[float] = None) -> Dict:
        """``POST /admin/release/{db_id}``. Drop ant-serve's r/o handle so
        a writer can open the duckdb r/w (server defaults: 300 s hold,
        30 s drain)."""
        params = {key: value for key, value in
                  (('duration_seconds', duration_seconds),
                   ('drain_timeout_seconds', drain_timeout_seconds))
                  if value is not None}
        response = self._request('POST', f'/admin/release/{db_id}',
                                 params=params or None)
        response.raise_for_status()
        return response.json()

    def admin_restore(self, db_id: str) -> Dict:
        """``POST /admin/restore/{db_id}``. Inverse of admin_release."""
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
    def from_vark_server(
        cls,
        name: str,
        *,
        config_dir: Optional[Union[str, Path]] = None,
        **kwargs: Any,
    ) -> 'AnthiveClient':
        """Construct a client wired to a registered Go vark CLI server.

        Looks up the server by ``name`` in ``~/.config/vark/config.yaml``
        (or ``config_dir`` if provided), decrypts its password from
        ``secrets.age``, and returns an ``AnthiveClient`` already
        authenticated against the right ``base_url`` with ``verify``
        following the server's ``insecure`` flag.

        Args:
            name: The ``name:`` field of the entry in ``config.yaml``.
            config_dir: Override ``~/.config/vark/``.
            **kwargs: Extra constructor kwargs passed through to
                :class:`AnthiveClient`. ``auth`` / ``verify`` / ``base_url``
                given here override the values discovered from the vark
                registry.

        Raises:
            VarkConfigError: when the ``vark-config`` extras are missing,
                no server is named ``name``, or the server has no stored
                password.

        Example::

            client = AnthiveClient.from_vark_server('intern')
            print(client.get_version()['version'])
        """
        server = find_vark_server_by_name(name, config_dir=config_dir)
        defaults: Dict[str, Any] = {
            'base_url': server.url,
            'auth': (server.user, server.password),
            'verify': not server.insecure,
        }
        defaults.update(kwargs)
        # vark_config has already done its job — don't re-trigger discovery
        # from the constructor for a client we've already authenticated.
        defaults.setdefault('vark_config', False)
        return cls(**defaults)

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
