"""AnthiveClient — Python REST client for Anthive (single-cell RNA-seq).

Targets the anthive REST API contract. See
`https://codeberg.org/mfiers/pyvark` for sources and
`https://codeberg.org/mfiers/vark` for the Go CLI that ships the same surface.

API surface verified against **REST API 2.46.0** (read-only dataset
deposit status for depositors and admins, facet tests with
facet scopes, replication with an effect gate and forest rows, module
replication, per-donor values and plots, donor variability, gene set
ranks, gene detection, donor groups, exclude_secondary, replicate
fields). Older servers still work for every
endpoint they expose; new params degrade to "server ignores it" rather
than client-side errors.

Works in standard Python 3.9+, in Pyodide / JupyterLite, and inside
Streamlit (auto-detects environment).

Requirements:
    requests (required)
    pandas   (optional — for ``format='dataframe'``)
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import (Any, BinaryIO, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple, Union)

import requests

from ._credentials import (
    VarkConfigError,
    find_vark_credentials_for,
    find_vark_server_by_name,
)
from .exceptions import (AnthiveWarmingUpError, ServerWarmingUpWarning,
                         raise_for_deposit_status, raise_for_upload_status)

_LOG = logging.getLogger('vark')


__version__ = "0.11.0"
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
        warmup_max_wait: Seconds to keep retrying while the server answers
            503 ``{"status": "warming_up", "retry_after": N}`` (REST API
            2.46.1+, facet routes right after a restart). The client
            sleeps ``retry_after`` (else the ``Retry-After`` header)
            between tries, logs a warning on the ``vark`` logger and
            emits a :class:`ServerWarmingUpWarning` once per wait, and
            raises :class:`AnthiveWarmingUpError` when the server is
            still warming up after this many seconds. Default 180; ``0``
            raises at once. Other 503 answers are not retried. Also an
            attribute you can change later.

    Access (REST API 2.45.0+): datasets that are not ``access: public``
    are hidden from callers without an identity. They are missing from
    every listing, and every route that names one answers
    ``404 {"detail": "Database '<id>' not found"}``, the same answer as
    for a dataset the site does not have. Sign in (``auth=`` or the
    credential sources above) to see them. The server ignores
    ``X-Remote-User`` / ``X-Remote-Groups`` request headers; only the
    proxy's ``Remote-User`` counts, so setting identity headers in
    :attr:`headers` does not sign you in. A call on loopback without a
    proxy (a local maintenance tool) counts as identified.

    Dataset deposits (REST API 2.46.0+): upload with the Go CLI,
    ``vark datasets upload``; pyvark only reads deposit state
    (:meth:`list_deposits`, :meth:`get_deposit`,
    :meth:`get_deposit_report`, :meth:`get_deposit_vocab`,
    :meth:`get_deposit_group`), for members of the site's
    ``datasets_upload_groups``.

    Admin-only methods (403 for other signed-in users, 2.45.0+):
    :meth:`rescan_databases`, :meth:`rescan_genesets`,
    :meth:`rescan_module_variability`, :meth:`rescan_catalogs`,
    :meth:`get_plot_cache_stats`, :meth:`clear_plot_cache`,
    :meth:`admin_release` and :meth:`admin_restore`. They need an admin
    account, a host-local call or the internal secret.

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
    API_TARGET = "2.46.0"

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
        warmup_max_wait: float = 180.0,
    ):
        self.warmup_max_wait = float(warmup_max_wait)
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

    @staticmethod
    def _warming_up(response: 'requests.Response'
                    ) -> Optional[Tuple[float, Dict[str, Any]]]:
        """``(retry_after, body)`` when ``response`` is a 503
        ``warming_up`` answer (REST API 2.46.1+), else ``None``."""
        if response.status_code != 503:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get('status') != 'warming_up':
            return None
        retry_after: Any = body.get('retry_after')
        if retry_after is None:
            retry_after = response.headers.get('Retry-After')
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            seconds = 5.0
        return max(seconds, 1.0), body

    def _send(self, method: str, url: str, **kwargs: Any
              ) -> 'requests.Response':
        """Send one request; while the server answers 503 ``warming_up``,
        sleep and retry for up to :attr:`warmup_max_wait` seconds, then
        raise :class:`AnthiveWarmingUpError`. Every other answer is
        returned unchecked."""
        sender = {'GET': requests.get, 'POST': requests.post}.get(method)
        waited = 0.0
        warned = False
        while True:
            if sender is not None:
                response = sender(url, **kwargs)
            else:
                response = requests.request(method, url, **kwargs)
            warming = self._warming_up(response)
            if warming is None:
                return response
            retry_after, body = warming
            remaining = self.warmup_max_wait - waited
            if remaining <= 0:
                raise AnthiveWarmingUpError(
                    f"{url}: the server is still warming up after "
                    f"{waited:.0f} s (warmup_max_wait="
                    f"{self.warmup_max_wait:g}); retry later or raise "
                    f"warmup_max_wait", retry_after=retry_after,
                    waited=waited, body=body, response=response)
            pause = min(retry_after, remaining)
            _LOG.warning('anthive server is warming up (%s); retrying in '
                         '%.0f s (waited %.0f of %g s)', url, pause, waited,
                         self.warmup_max_wait)
            if not warned:
                warnings.warn(
                    f"anthive server is warming up; waiting up to "
                    f"{self.warmup_max_wait:g} s", ServerWarmingUpWarning,
                    stacklevel=3)
                warned = True
            time.sleep(pause)
            waited += pause

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self._send(
            'GET', url, params=params, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response.json()

    def _get_raw(self, path: str,
                 params: Optional[Dict] = None) -> 'requests.Response':
        """GET returning the raw response (for non-JSON bytes — plots,
        changelog)."""
        url = f"{self.base_url}{path}"
        response = self._send(
            'GET', url, params=params, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response

    def _post(self, path: str, json: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self._send(
            'POST', url, json=json, headers=self.headers,
            timeout=self.timeout, auth=self.auth, verify=self.verify,
        )
        response.raise_for_status()
        return response.json()

    def _request(self, method: str, path: str, *,
                 timeout: Optional[float] = None,
                 **kwargs: Any) -> 'requests.Response':
        """Send one request and return the raw response WITHOUT checking
        its status (callers decide how to map errors). Only GET waits
        for a warming-up server; a POST with a file body is sent once."""
        url = f"{self.base_url}{path}"
        common = dict(headers=self.headers,
                      timeout=self.timeout if timeout is None else timeout,
                      auth=self.auth, verify=self.verify, **kwargs)
        if method.upper() == 'GET':
            return self._send('GET', url, **common)
        return requests.request(method, url, **common)

    # ── Info endpoints ───────────────────────────────────────────────────

    def get_root(self) -> Dict:
        """``GET /``. API root info (name, version, links)."""
        return self._get('/')

    def get_health(self) -> Dict:
        """``GET /health``. Health + latency telemetry.

        Includes ``mean_response_ms`` / ``p50_response_ms`` / ``n_samples``
        (server-side wall-clock over the last 1024 non-probe requests).
        Use :meth:`pick_fastest` to choose between replicas.

        A running server with an empty catalogue answers 200 with
        ``databases: 0`` and ``note: "empty catalog"`` (REST API 2.46.0+;
        2.45.1 and older answered 503 "No databases available"). A 503
        (``HTTPError``) now means a real failure: the catalogue is not
        loaded yet or the registry is unreadable.
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

        REST API 2.45.0+ adds ``compatible_pyvark`` (for example
        ``'>=0.9.0'``), the oldest pyvark release that works with the
        server; ``compatible_antclient`` carries the same pin, is
        deprecated and goes away in 2.46. ``released`` is ``None`` until
        a release is cut.
        """
        try:
            return self._get('/version')
        except Exception:
            health = self._get('/health')
            return {
                'version': health.get('api_version'),
                'released': None,
                'compatible_antclient': None,
                'compatible_pyvark': None,
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

        Lineage fields (REST API 2.43.0+): ``designation`` (``'primary'``,
        ``'reanalysis'`` or ``'unresolved'``, from ``is_secondary:
        false`` / ``true`` / no key in the dataset's ``.md``),
        ``reanalysis_of`` (list of db_ids, empty unless reanalysis),
        ``donor_group`` (slug of the donor pool the dataset shares with
        others, or ``None``) and ``has_donor_uid`` (the dataset
        publishes ``ant_donor_uid``). Datasets of one donor group share
        persons, so do not count them as independent donors; see
        :meth:`get_donor_groups`.

        Replicate fields (REST API 2.43.0+): ``replicate_unit``
        (``'donor'``, ``'animal'``, ``'sample'`` or ``'none'``, what a
        biological replicate is in this dataset), ``replicate_column``
        (the obs column that identifies a replicate),
        ``replicate_basis`` (``'declared'`` in the dataset's ``.md`` or
        ``'inferred'``) and ``disease_basis`` (how the disease label was
        defined, e.g. ``'braak_cerad'`` or ``'author_dx'``). Each is a
        plain column in ``format='dataframe'``; servers that do not
        serve them (or datasets without a value) leave them absent or
        ``None``.

        Facet fields (REST API 2.44.0+): ``facet_scope`` (the scope the
        dataset's facet results belong to, see :attr:`FACET_SCOPES`;
        ``None`` when the dataset has no facet layer: pure mouse, bulk
        and other datasets outside the three scopes) and
        ``control_basis`` (for datasets with ``ant_app_nlgf``: what the
        control animal is, ``'wt_host'``, ``'humanised_app_host'`` or
        ``'other'``).

        ``facet_exclude`` (bool) and ``facet_exclude_reason`` (REST API
        2.44.0+) say whether the dataset is outside the facet layer by
        data type: the dataset's ``.md`` says ``facet_exclude: true``
        (metacell sets, sets the curators set aside; the reason is
        ``facet_exclude_reason``), or its technology is spatial or
        proteomics. Such datasets have no facet results and no cohort
        rows; :meth:`get_facet_tests` answers them with ``status:
        'not_tested'`` and the reason.

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
        (described under :meth:`get_databases`) and the replicate
        fields ``replicate_unit``, ``replicate_column``,
        ``replicate_basis`` and ``disease_basis``, plus (REST API
        2.44.0+) ``facet_scope``, ``control_basis``, ``facet_exclude`` and
        ``facet_exclude_reason`` (described under
        :meth:`get_databases`). ``replicate_unit`` says what the n of a
        test is (donors, animals or samples), ``replicate_basis``
        whether that was declared by the curators or inferred,
        ``disease_basis`` how the disease label was defined (so that
        "control" can be compared between studies).
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
        REST API 2.43.0+): secondary datasets (reanalyses of
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
        response = self._send(
            'GET', url, params=params, headers=self.headers,
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

    def get_de_study_summary(self, db_id: str, study_id: str,
                             padj_max: Optional[float] = None,
                             format: str = 'json') -> Union[Dict, Any]:
        """``GET /databases/{db_id}/de/{study_id}/summary``. Significant
        gene counts of every contrast of one DE study in one query
        (REST API 2.45.0+).

        Returns the study metadata of :meth:`get_de_study` plus
        ``subset_filter``, ``cluster_obs_column`` (``None`` on schema v1
        duckdbs), ``padj_max`` and ``contrasts``: a list of ``{term,
        contrast, n_genes, n_sig, n_up, n_down, efftype}``. ``n_sig``
        counts genes at ``padj < padj_max``, split into ``n_up`` /
        ``n_down`` by the sign of ``effsize``. These are the counts the
        study authors' own test produced; padj is adjusted within each
        contrast, so counts of different contrasts are separate
        families.

        Args:
            db_id: Dataset id.
            study_id: DE study id (see :meth:`list_de_studies`).
            padj_max: Significance threshold (server default 0.05). Sent
                only when given.
            format: ``'json'`` (the dict) or ``'dataframe'`` (the
                ``contrasts`` rows, needs pandas).

        Raises:
            requests.HTTPError: 404 for an unknown dataset (was 500
                before 2.45.0) or study.
        """
        params: Dict[str, Any] = {}
        if padj_max is not None:
            params['padj_max'] = padj_max
        result = self._get(f'/databases/{db_id}/de/{study_id}/summary',
                           params=params or None)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('contrasts', []))
        return result

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
        3-row samples visible to the analytics sandbox.

        Returns ``{db_id, tables, score_sidecar}``. ``score_sidecar``
        (REST API 2.45.0+) describes the view of the same name that
        exposes the dataset's sidecar module scores to SQL:
        ``{available, view, columns, n_scores, n_scores_by_source,
        note, example_sql}``, or ``{available: False, reason}``. The
        view has one row per cell and score: ``cell_id, cell_name,
        source`` (``geneset`` | ``ucell``), ``catalog``, ``module``,
        ``column_name, score``. It is described without being scanned.

        Querying it with :meth:`analytics_query`: always filter on
        ``source``, ``catalog`` and ``module`` (only matching row groups
        are read) and join other tables on ``cell_name``. For several
        scores side by side, aggregate per cell, for example::

            SELECT cell_name,
                   MAX(score) FILTER (WHERE module = 'bellenguez') AS a,
                   MAX(score) FILTER (WHERE module = 'royalblue') AS b
            FROM score_sidecar
            WHERE source = 'geneset'
              AND module IN ('bellenguez', 'royalblue')
            GROUP BY cell_name

        A wide view with one column per score is not offered (it would
        aggregate the whole sidecar on every query). The ``query_sql``
        of each :meth:`list_module_scores` row is a ready query for one
        score.
        """
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
        gene set (module) scores of a dataset, wherever they are stored.

        Scores live either as ``obsnum`` columns of the duckdb or, on
        most current datasets, only in the dataset's scores sidecar
        (``<dataset>.scores.parquet``). REST API 2.45.0+ lists both in
        the same row shape (older servers listed the ``obsnum`` columns
        only, so they answered ``count: 0`` for sidecar-only datasets).
        A duckdb column wins over a sidecar row of the same set.
        S3-backed datasets list duckdb columns only.

        Returns the full discovery dict::

            {
                "db_id": "...",
                "count": <N>,
                "count_by_storage": {"duckdb": <n>, "scores_sidecar": <n>},
                "column_naming": {"separator": "::", "template": "..."},
                "query_tables": {"duckdb": "obsnum",
                                 "scores_sidecar": "score_sidecar"},
                "sidecar_error": "...",   # only when unreadable
                "scored_genesets": [
                    {"geneset_id": ..., "column_name": ...,
                     "storage": "duckdb" | "scores_sidecar",
                     "query_table": "obsnum" | "score_sidecar",
                     "query_sql": "SELECT cell_name, score AS ... ",
                     "n_genes": ..., "stats": {n, min, max, mean, std,
                                               median},
                     "ucell_column_name": ..., "ucell_stats": ...}, ...
                ]
            }

        ``query_sql`` is a ready SELECT returning ``cell_name`` and the
        score, for :meth:`analytics_query`; ``column_name`` is the name
        the plot routes take. ``ucell_column_name`` / ``ucell_stats``
        appear when the sidecar also holds the set's UCell score.
        ``stats`` are over cells (descriptive, not donor-level). Use
        ``response["scored_genesets"]`` for the list itself.
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
        """``POST /genesets/rescan``. Re-scan geneset store + rebuild.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
        return self._post('/genesets/rescan')

    @_cache_long(ttl=3600)
    def list_geneset_studies(_self) -> Dict:
        """``GET /genesets/studies``. Studies with their experiments."""
        return _self._get('/genesets/studies')

    def get_genesets_by_gene(self, gene: str) -> Dict:
        """``GET /genesets/by-gene/{gene}``. Every geneset containing
        ``gene``."""
        return self._get(f'/genesets/by-gene/{gene}')

    def get_geneset_rank(self, study: str, experiment: str, name: str,
                         db_id: Optional[str] = None) -> Dict:
        """``GET /genesets/{study}/{experiment}/{name}/rank``. Where the
        mean score of one gene set ranks among all gene sets scored in
        the same dataset (REST API 2.45.0+).

        A mean module score is not comparable between datasets; a rank
        within one dataset is. Never compare ranks or means between
        datasets.

        Returns ``{geneset_id, primary_source: 'geneset',
        secondary_source: 'ucell', min_cells, min_peers, datasets,
        not_available, help_topics}``. Each ``datasets`` entry is
        ``{db_id, geneset: R, ucell: R}`` with ``R = {available, reason,
        rank, n_modules_compared, n_cells, mean, std, cell_types}``;
        ``cell_types`` holds the same fields per cell type group.
        ``rank`` runs from 0 (lowest mean) to 1 (highest): the fraction
        of the other modules of the same source with a lower mean (ties
        count half), taken per cell type group and averaged weighted by
        cells for the dataset value. At least ``min_peers`` (10) peers
        and ``min_cells`` (20) cells per module and stratum are needed,
        else ``rank`` is ``None`` with a ``reason``. ``std`` is the
        standard deviation of the score over cells (spread between
        cells, not donors). ``geneset`` (scanpy ``score_genes``) is the
        primary ranking; ``ucell`` depends on gene set size and is
        secondary. ``not_available`` lists ``{db_id, reason}``.

        The first request for a dataset reads its whole scores sidecar
        on the server (seconds to about a minute); later requests are
        cached.

        Args:
            study, experiment, name: Gene set id parts.
            db_id: One dataset from the gene set's ``scored_in``.
                Default: every dataset that scored it.
        """
        params = {'db_id': db_id} if db_id is not None else None
        return self._get(f'/genesets/{study}/{experiment}/{name}/rank',
                         params=params)

    def get_module_ranks(self, db_id: str,
                         format: str = 'json') -> Union[Dict, Any]:
        """``GET /databases/{db_id}/module-ranks``. The rank of every
        gene set scored in one dataset (REST API 2.45.0+; the dataset
        view of :meth:`get_geneset_rank`).

        Returns ``{db_id, available, reason, min_cells, min_peers,
        modules, help_topics}``; one ``modules`` row per gene set
        (``geneset`` source): ``{geneset_id, study, experiment, name,
        display_name, column_name, available, reason, rank,
        n_modules_compared, n_cells, mean, std}``. ``available`` is
        ``False`` with a ``reason`` for a dataset without per-cell
        scores. Same rules as :meth:`get_geneset_rank`: within one
        dataset only, ``std`` is over cells, not donors.

        Args:
            db_id: Dataset id.
            format: ``'json'`` (the dict) or ``'dataframe'`` (the
                ``modules`` rows, needs pandas).
        """
        result = self._get(f'/databases/{db_id}/module-ranks')
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('modules', []))
        return result

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
                               scope: Optional[str] = None,
                               ) -> Union[Dict, Any]:
        """``GET /module-variability``. Fleet-wide module variability
        table (dataset x source x catalog x module x tag x statistic).

        Exploratory and per-cell (descriptive): the statistics are
        computed over cells, and cells are not replicates, so they say
        nothing about donor-level reproducibility (use the donor-level
        facet tests for that). Newer servers state this in ``basis``
        (``'per-cell (descriptive)'``; ``X-Basis`` header for CSV /
        TSV) and add a ``replicate_unit`` column to the rows. With
        ``format='dataframe'`` the basis is kept in ``df.attrs['basis']``.

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
                REST API 2.43.0+): rows of secondary datasets
                (reanalyses) are left out unless ``db_id`` names one.
                JSON then carries ``n_excluded_secondary``, CSV / TSV the
                ``X-Excluded-Secondary`` header. Sent only when ``False``.
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Keeps only the rows of the
                datasets in that scope (REST API 2.44.0+). Default: every
                scope. Unknown names give HTTP 400.
                Sent only when given.
        """
        params: Dict[str, Any] = {'sort': sort, 'limit': limit}
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
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
            frame = pd.DataFrame(rows)
            if isinstance(result, dict) and 'basis' in result:
                frame.attrs['basis'] = result['basis']
            return frame
        return result

    def rescan_module_variability(self) -> Dict:
        """``POST /module-variability/rescan``. Reload the parquet.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
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

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
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
        ignored on newer servers). ``signal_strength`` is per-cell
        (descriptive) and deprecated: do not use it to rank facets.

        Newer servers (REST API 2.43.0+) add ``label``,
        ``summary`` and ``defined`` (the published definition) and
        ``hidden_by_config``: True for facets outside the site's
        ``facets_visible`` list. The web UI hides those, the API still
        lists them.

        Rows also carry ``scopes`` (REST API 2.44.0+): the facet scopes
        in which the facet is published.

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
                  exclude_secondary: bool = True,
                  scope: Optional[str] = None) -> Dict:
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

        Newer servers (REST API 2.43.0+) add:

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

        REST API 2.44.0+ answers inside one facet scope (``scope``): the
        datasets, cell counts, value distribution, per-cell module
        statistics and non-carriers are those of that scope, and the
        response carries ``scope`` and ``scopes`` (a list of
        ``{scope, n_datasets, n_cells}`` for every scope that has the
        facet). Mouse and human values are different things; do not
        pool them. New facets: ``ant_age``; ``ant_app_nlgf``
        (``nlgf`` or ``control``, xenograft hosts); ``ant_host_genotype``
        (genotype of the host mouse); ``ant_tissue`` (21 umbrella
        terms, human post-mortem only; the specific anatomical term is
        the plain obs column ``tissue_uberon`` with
        ``tissue_uberon_id``); and one author-cluster facet per cell
        type (``ant_mg_author_cluster``, ``ant_astro_author_cluster``,
        ``ant_oligo_author_cluster``, ``ant_opc_author_cluster``,
        ``ant_exn_author_cluster``, ``ant_inn_author_cluster``,
        ``ant_endo_author_cluster``). Author clusters are defined by
        each study, so they are dataset-scoped: they say nothing across
        datasets and have no gene tests. ``ant_cell_type`` is null for
        cells the authors left unassigned or called doublets. The
        facets ``region``, ``author_cell_type``, ``author_cluster``,
        ``lesion``, ``ms_type``, ``cause_of_death``, ``ethnicity``,
        ``source_institution``, ``technology`` and ``group`` are
        deprecated (:attr:`DEPRECATED_FACETS` lists the replacements).

        Raises ``HTTPError`` 400 when ``facet_name`` does not start with
        ``ant_`` or ``scope`` is unknown, or 404 when no dataset carries
        it.

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
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Facets are tested, counted and
                replicated inside one scope, so human and mouse results
                never mix. Default: ``human_postmortem``
                (``xenograft_human_in_mouse`` for ``app_nlgf``);
                ``'all'`` pools every scope (not recommended).
                Unknown names give HTTP 400. Sent only when given.

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
        if scope is not None:
            params['scope'] = scope
        return _self._get(f'/facets/{facet_name}', params=params)

    def get_database_facets(self, db_id: str, counts: bool = True) -> Dict:
        """``GET /databases/{db_id}/facets``. How one dataset's author
        columns project onto the ``ant_*`` facets (REST API 2.43.0+).

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

        Exploratory and per-cell (descriptive): cells are not
        replicates, so ``signal`` ranks genes by between-group variance
        of cell-level means and is no evidence of a donor-level effect;
        use :meth:`get_facet_tests` / :meth:`get_facet_test_genes` for
        that. Newer servers say so in ``basis``
        (``'per-cell (descriptive)'``), ``replicate_unit`` and
        ``replicate_level_analysis``.

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

    # ── Donor-level facet tests (REST API 2.43.0+) ─────────────
    #
    # Read-only views over the output of ant-facet-test (genes, limma-voom
    # on donor pseudobulks) and ant-facet-test-modules (module scores, OLS
    # plus label permutations). See FACET_TESTS_CAVEATS below and
    # doc/DESIGN_facet_testing.md in anthive4.

    #: Facets with donor-level test results (the server's order). The facet
    #: endpoints take the short name or the ``ant_`` column (``'sex'`` or
    #: ``'ant_sex'``). ``apoe4_dose`` (e4 allele dose), ``braak_threshold``
    #: (early versus late Braak) and ``age_adjusted`` (age with the
    #: pathology term in the model) are variants of ``apoe4_carrier``,
    #: ``braak`` and ``age``. ``app_nlgf`` (App-NL-G-F knock-in versus
    #: control) exists in the xenograft scope only.
    #: ``graft_sex`` (REST API 2.45.0+) is the sex of the donor line of
    #: the grafted human cells in xenografts (column ``ant_sex``) and
    #: ``host_sex`` the sex of the mouse host (``ant_host_sex``); ``sex``
    #: is human post-mortem only.
    TESTED_FACETS = ('sex', 'graft_sex', 'host_sex', 'disease',
                     'apoe4_carrier', 'apoe4_dose', 'braak',
                     'braak_threshold', 'age', 'age_adjusted', 'app_nlgf')

    #: Facet scopes. Facets are tested, counted and replicated inside one
    #: scope; human and mouse (xenograft host) results never mix. In
    #: ``in_vitro`` cultures facets are listed but not tested. A scope
    #: name the server does not know (including the retired
    #: ``mouse_in_vivo``) gives HTTP 400.
    FACET_SCOPES = ('human_postmortem', 'xenograft_human_in_mouse',
                    'in_vitro')

    #: Scope a single-answer endpoint uses when ``scope`` is not given
    #: (``app_nlgf`` is not allowed there and answers in
    #: ``xenograft_human_in_mouse``).
    DEFAULT_FACET_SCOPE = 'human_postmortem'

    #: Deprecated aliases the server still accepts and answers with the
    #: new name. Empty since REST API 2.45.0 (see
    #: :attr:`REMOVED_FACET_ALIASES`).
    DEPRECATED_FACET_ALIASES: Dict[str, str] = {}

    #: Aliases the server no longer accepts (REST API 2.45.0 removed
    #: ``apoe_carrier``): an old name is an unknown facet (404 or 400).
    #: Map old code with this table.
    REMOVED_FACET_ALIASES = {'apoe_carrier': 'apoe4_carrier',
                             'ant_apoe_carrier': 'ant_apoe4_carrier'}

    #: Values of the ``forest`` argument (REST API 2.45.0+): ``full``
    #: (every unit, folded units in ``more_units``), ``anchors`` (one
    #: row per cohort, folded units left out but counted in
    #: ``n_more_units``) or ``none``.
    FOREST_DETAIL = ('full', 'anchors', 'none')

    #: Image formats of :meth:`get_facet_donor_plot`.
    DONOR_PLOT_FORMATS = ('png', 'svg', 'pdf')

    #: Donor variability design rungs (REST API 2.45.0+): ``raw`` (all
    #: replicate differences), ``D0`` technical block, ``D1`` + sex,
    #: age, PMI, batch, ``D2`` + disease (host genotype for xenografts),
    #: ``D3`` + APOE e4, ``D4`` + Braak, ``F`` + 3 to 5 latent factors.
    DONOR_VARIABILITY_RUNGS = ('raw', 'D0', 'D1', 'D2', 'D3', 'D4', 'F')

    #: Facets that are deprecated registry entries (not accepted as a new
    #: name for anything), mapped to the facets that take over. An empty
    #: list means no replacement.
    DEPRECATED_FACETS = {
        'apoe_carrier': ['apoe4_carrier'],
        'region': ['tissue'],
        'author_cluster': ['mg_author_cluster', 'astro_author_cluster',
                           'oligo_author_cluster', 'opc_author_cluster',
                           'exn_author_cluster', 'inn_author_cluster',
                           'endo_author_cluster'],
        'author_cell_type': [], 'lesion': [], 'ms_type': [],
        'cause_of_death': [], 'ethnicity': [], 'source_institution': [],
        'technology': [], 'group': [],
    }

    #: How to read facet test results. Repeated in each method's docstring
    #: so ``help()`` on one method is enough.
    FACET_TESTS_CAVEATS = (
        "Facet test results are exploratory. The unit of analysis is the "
        "replicate (a donor for human data, an animal for mouse hosts; "
        "see replicate_unit): raw counts are summed per (replicate, cell "
        "type) and each (dataset, cell type) unit is tested on its own; "
        "nothing is pooled and there is no meta-analysis. Datasets share "
        "donors, so rows are not independent replications. Facets are "
        "tested, counted and replicated per scope (human_postmortem, "
        "xenograft_human_in_mouse): human and mouse never mix. The "
        "disease contrast (AD vs control) is provisional: 'control' means "
        "different things between studies (see disease_basis). Small "
        "cohorts (a level with fewer than 5 replicates, numeric facets "
        "fewer than 10) are tested and counted but flagged small_sample; "
        "they have little power and unstable estimates. Units with "
        "analysis_level 'below_floor' (fewer than 4 replicates) or 'cell' "
        "are descriptive only: no p value, never counted. counted means "
        "the unit counts towards replication; not_counted_reason says why "
        "not. replicated = partial-conjunction r_value < 0.05 (the effect "
        "is present in at least 2 independent cohorts, same direction; "
        "Benjamini and Heller, Simes combination of the one-sided limma "
        "p values of one anchor unit per cohort); it says the direction "
        "reproduces, not that the effect is large. effect_gate_passed "
        "adds the size: at least 2 cohorts each with a log2 fold change "
        "of at least tau (10 percent change) in the replicated "
        "direction. replicated_effect is the stricter second tier "
        "(r_treat < 0.05, the partial conjunction on limma treat p "
        "values that test against that minimum effect). k of m is "
        "descriptive: cohorts individually at q_value < 0.05 in the "
        "replicated direction among those tested. The cohort table is a "
        "DRAFT, a cohort counts once, cell types are counted by group, "
        "secondary datasets never count. lambda_gc above 1.3 (inflated) "
        "means the test statistics are inflated, so p values of that unit "
        "are too small; treat it as unmodelled structure, not signal. "
        "Absence of a hit is not evidence of no effect."
    )

    def _check_forest(self, forest: str) -> str:
        """Validate a ``forest`` argument against :attr:`FOREST_DETAIL`."""
        if forest not in self.FOREST_DETAIL:
            raise ValueError(
                f"forest must be one of {self.FOREST_DETAIL}, got {forest!r}")
        return forest

    def _paged_get(self, path: str, params: Dict[str, Any],
                   fetch_all: bool) -> Dict:
        """GET a ranked list that pages with ``n`` / ``offset`` /
        ``has_more`` (REST API 2.45.0+). With ``fetch_all`` the pages are
        joined into one response: ``rows`` holds every row from the
        first ``offset``, ``n_returned`` counts them and ``has_more`` is
        ``False``. The other fields are those of the first page."""
        result = self._get(path, params=params)
        if not fetch_all or not isinstance(result, dict):
            return result
        rows = list(result.get('rows', []))
        offset = int(params.get('offset', 0))
        page = result
        while page.get('has_more') and page.get('rows'):
            offset += len(page['rows'])
            page = self._get(path, params={**params, 'offset': offset})
            rows.extend(page.get('rows', []))
        merged = dict(result)
        merged['rows'] = rows
        merged['n_returned'] = len(rows)
        merged['has_more'] = False
        return merged

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
        Responses carry ``help_topics`` (REST API 2.44.0+): topic ids for
        :meth:`get_help_topic` that explain how to read the numbers.
        Genes are tested with limma-voom on donor pseudobulks
        (``expression ~ facet + sex + age + pmi + batch`` where
        available), module scores with OLS on donor-level scores plus
        label permutations. Contrasts: sex (m vs f), disease (AD vs
        control, provisional), apoe4_carrier (e4 carrier vs
        non-carrier), braak (linear trend per stage).

        Top level: ``status`` (``tested``, ``not_tested``, ``not_run``),
        ``reason``, ``n_units``, ``n_units_tested``, ``q_threshold``,
        ``dataset_changed`` (the dataset's sha256 differs from the one
        the run used: results may be stale), ``files`` and ``runs``
        (tool, timestamp, seed, package versions, git commits, counts
        layer, batch column, permutations).

        REST API 2.44.0+: the summary and every unit, gene row and
        module row carry ``facet_scope`` (the dataset's scope, see
        :attr:`FACET_SCOPES`; ``None`` for a dataset in no scope). The
        summary also carries ``facet_exclude`` and
        ``facet_exclude_reason`` (see :meth:`get_databases`); a
        not-tested dataset gives its current ``reason`` (the exclusion
        reason, else ``no facet scope: ...``). ``facet`` also takes ``age`` (log2 fold
        change per decade of age; the effect includes disease
        prevalence), ``age_adjusted`` (the same with the pathology term
        in the model; ``age`` gene rows then carry
        ``persists_after_pathology_adjustment``, True when the age
        effect is still there after adjustment), ``apoe4_dose``,
        ``braak_threshold`` and ``app_nlgf`` (``nlgf`` vs ``control``;
        replicate = the animal, stratified by timepoint and graft line,
        refused when confounded). Age facets use donors from 60 years.

        Each unit: ``facet``, ``cell_type``, ``celltype_source``,
        ``tested`` (``reason`` when not), ``contrast``, ``level_ref`` /
        ``level_test``, ``level_counts`` (donors per level),
        ``n_donors(_ref/_test)``, ``covariates``, ``flags``
        (``descriptive_only``, ``n_lt5``, ``provisional``,
        ``composition_flag``, ``composition_p``, ``batch_used``,
        ``batch_confounded``), ``genes`` (``n_tested``, ``n_q05``),
        ``modules`` (per method and source: ``n_modules``,
        ``n_q05_param``, ``n_q05_perm``) and ``model``. REST API 2.45.0+
        adds ``n_up`` and ``n_down`` to ``genes`` (genes at q < 0.05
        with a positive or negative logFC, that is up or down in the
        test level or with the trend) and ``n_q05``, ``n_up``,
        ``n_down`` to every ``modules`` entry (gene sets significant by
        the rule module replication uses: ``q_param`` when the residual
        df is at least 30, else ``q_perm``, below 0.05, split by the
        sign of ``std_effect``). All are ``None`` when only the run
        manifest has the counts. Newer servers
        add ``analysis_level`` (``'replicate'`` or ``'cell'``),
        ``analysis_level_label`` (``"cell-level, descriptive only"`` for
        cell level), ``analysis_level_reason`` and ``tau_log2``.
        ``analysis_level`` is ``'replicate'`` (tested on replicate
        pseudobulks, the proper analysis), ``'cell'`` (descriptive; cells
        are not replicates) or, from 2.44.0, ``'below_floor'`` (fewer
        than 4 replicates: effect sizes only, no p values). From 2.44.0
        units also carry ``n_small`` and ``flags.small_sample`` (a level
        with fewer than 5 replicates, numeric facets fewer than 10;
        small cohorts are tested and counted but have little power),
        and model quality fields: ``lambda_gc`` (genomic inflation
        factor of the test statistics, about 1 when well calibrated),
        ``inflated`` (``lambda_gc`` above 1.3: the p values of the unit
        are too small and should not be trusted), ``df_total_median``
        (median residual degrees of freedom of the genes),
        ``covariates_dropped`` (covariates left out because there were
        too few residual degrees of freedom), ``n_sex_checked`` and
        ``n_sex_discordant`` (replicates whose recorded sex disagrees
        with sex-gene expression), ``age_top_coded`` (ages capped in the
        source), ``control_basis``, ``design_subset`` and ``notes``. The
        summary and units also carry ``replicate_unit``,
        ``replicate_column``, ``replicate_basis``, ``disease_basis`` and
        ``pool``.

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

        Reading the flags: ``descriptive_only`` marks units without
        usable p values (cell-level or below the replicate floor), so
        p, q, t and CI are null and only the effect is reported. ``composition_flag`` means the
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
        n_donors_test, descriptive_only, provisional``. Newer servers
        add ``p_treat``, ``q_treat`` (limma treat: H0 is
        ``|logFC| <= tau_log2``) and ``tau_log2``.
        ``descriptive_only`` rows have null p, q, t and CI (a level has
        too few donors) and only appear with ``q=1``.

        Args:
            db_id: Dataset id.
            facet: One of :attr:`TESTED_FACETS`, for example
                ``sex``, ``disease``, ``braak`` or ``age``.
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
        no_counts``; newer servers add ``passes_effect_gate``
        (``|std_effect| >= 0.2``).

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
            facet: One of :attr:`TESTED_FACETS`, for example
                ``sex``, ``disease``, ``braak`` or ``age``.
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
                                        scope: Optional[str] = None,
                                        ) -> Union[Dict, Any]:
        """``GET /facets/{facet}/tests``. Every tested (dataset, cell
        type stratum) of one facet, sorted by donors analysed.

        Exploratory, donor-level; the disease contrast is provisional.
        No pooling: datasets are tested independently and share donors
        (ROSMAP, SEA-AD, MADRC, ...), so rows are not independent
        replications. REST API 2.44.0+ answers for one facet scope
        (``scope``; ``'all'`` lists every scope, rows then carry
        ``facet_scope`` and counting stays per scope); the response
        carries ``scope`` and ``scopes``.

        Responses carry ``help_topics`` (REST API 2.44.0+): topic ids for
        :meth:`get_help_topic` that explain how to read the numbers.

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
            facet: ``'sex'`` / ``'ant_sex'``, or any of
                :attr:`TESTED_FACETS`. Others give 404.
            top_genes: Genes at q < 0.05 listed per row (0 to 50).
            exclude_secondary: Secondary datasets are listed with
                ``is_secondary: True`` and ``counted: False`` either way;
                ``False`` lets them count. Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas; nested fields stay as objects).
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Facets are tested, counted and
                replicated inside one scope, so human and mouse results
                never mix. Default: ``human_postmortem``
                (``xenograft_human_in_mouse`` for ``app_nlgf``);
                ``'all'`` lists every scope. Unknown names give
                HTTP 400. Sent only when given.
        """
        params: Dict[str, Any] = {'top_genes': top_genes}
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
        result = self._get(f'/facets/{facet}/tests', params=params)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('rows', []))
        return result

    def get_facet_gene_effects(self, facet: str, gene: str,
                               cell_type: Optional[str] = None,
                               exclude_secondary: bool = True,
                               format: str = 'json',
                               scope: Optional[str] = None,
                               forest: Optional[str] = None,
                               ) -> Union[Dict, Any]:
        """``GET /facets/{facet}/effects?gene=``. Result card of one
        gene for one facet: every tested (dataset, cell type) unit.

        Exploratory, donor-level; each unit is one dataset and cell
        type tested separately (limma-voom, n = donors); nothing is
        pooled and there is no meta-analysis. The disease contrast is
        provisional. The cohort table is a DRAFT.

        Responses carry ``help_topics`` (REST API 2.44.0+): topic ids for
        :meth:`get_help_topic` that explain how to read the numbers.

        Returns ``{query, scope, scopes, facet, gene, gene_flags,
        n_units, units, replication, cell_types, notes, apoe_locus,
        definition, donor_count_definition}``. ``scope`` is the facet
        scope answered (:attr:`FACET_SCOPES`), ``scopes`` the scopes
        that have results for this facet. A gene symbol is never
        matched across scopes. ``gene_flags`` lists gene classes that
        are over-represented among hits for technical or locus reasons
        (``mitochondrial``, ``ribosomal``, ``lncrna_or_clone``,
        ``sex_chromosome``, ``long_gene`` (over 500 kb),
        ``apoe_locus``); weigh such hits accordingly.

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
          Newer servers add ``anchor`` (the unit that represents its
          cohort), ``pooled`` (donor-pooled unit, ``db_id``
          ``pool:<id>``), ``p_treat``, ``q_treat``, ``tau_log2`` (the
          limma treat threshold), ``significant_treat``,
          ``independence``, ``disease_basis`` and ``analysis_level``
          (``'replicate'`` or ``'cell'``; cell-level is descriptive
          only; ``analysis_level_reason`` says why). From 2.44.0:
          ``analysis_level`` may be ``'below_floor'`` (fewer than 4
          replicates, descriptive), and units add ``p_value``,
          ``facet_scope``, ``small_sample``, ``lambda_gc``,
          ``inflated``, ``df_total_median``, ``covariates_dropped``,
          ``n_sex_checked``, ``n_sex_discordant``, ``age_top_coded``,
          ``control_basis``, ``design_subset``, ``notes`` and (age
          facets) ``persists_after_pathology_adjustment``; see
          :meth:`get_facet_tests` for how to read them.
        - ``replication``: one entry per cell type group with
          ``celltype_group`` (also in ``cell_type``), ``cl_id``,
          ``labels`` (the raw labels it covers), ``cohorts_tested``
          (m), ``cohorts_sig_same_sign`` (k), ``direction``,
          ``sign_discordant``, ``cohorts_up``, ``cohorts_down`` (the
          old fields, q_value < 0.05 on the anchor units, kept for one
          release) and, from facet engine v2: ``r_value`` (partial
          conjunction, u = 2, BH over the genes of the facet x cell
          type group), ``p_pc``, ``k`` (cohorts with q_treat < 0.05 in
          the replicated direction), ``m`` (cohorts with a treat p
          value), ``replicated`` (``r_value < 0.05``),
          ``r_direction``, ``anchors`` (one per counted cohort:
          ``cohort``, ``db_id``, ``pooled``, ``logFC``, ``p_treat``,
          ``q_treat``), ``overlap_unknown`` and
          ``cohorts_overlap_unknown`` (cohorts whose donor
          independence was never checked), ``replication_u`` and
          ``replication_method`` (``'simes'``). ``badge`` is the
          sentence the web UI shows; the pre-v2 text is
          ``legacy_badge``.

          REST API 2.44.0 changes and adds, per entry:

          * ``r_value`` and ``p_pc`` now come from the ordinary
            one-sided limma p values (``replication_p``); the treat p
            values stay in ``p_treat`` / ``q_treat``. ``k`` counts
            cohorts with ``q_value < 0.05`` in the replicated
            direction, ``k_treat`` those with ``q_treat < 0.05``.
            Numbers differ from earlier servers; field names do not.
          * ``replicated`` is ``r_value < 0.05``: the direction of the
            effect is reproduced in at least 2 independent cohorts. It
            says nothing about size. ``r_significant`` repeats that
            test.
          * ``effect_gate_passed``: at least 2 cohorts each have a
            signed log2 fold change of at least ``effect_gate_tau`` in
            the replicated direction (``effect_gate_unit`` words the
            threshold, for example ``'10%'``, ``'10% per decade'``).
            ``effect_gate_cohorts`` names those cohorts,
            ``n_cohorts_ge_tau`` counts them, ``median_logFC`` is the
            median effect over the anchors. ``r_cohorts`` are the two
            cohorts that carry the r-value.
          * ``r_treat`` / ``replicated_effect``: second tier. The
            partial conjunction on the limma treat p values, which test
            against a minimum effect of 10 percent;
            ``replicated_effect`` is True when ``r_treat < 0.05``, so
            the effect is replicated and at least that large.
            ``r_strong`` / ``replicated_strong`` do the same with a
            25 percent threshold. All three are ``None`` when a tier
            could not be computed.
          * ``n_small``: how many of the ``m`` cohorts have fewer than
            ``small_cohort_replicates`` replicates (small cohorts give
            noisy estimates but count).
          * ``cohorts_up`` / ``cohorts_down`` and ``cohorts_sig_same_sign``
            are the old counts, kept for compatibility.

          REST API 2.45.0 adds, per entry: ``call`` (``replicated`` |
          ``not_replicated`` | ``not_tested``) with ``nt_label``
          (``'1 cohort'`` or ``'not tested'``), ``gate`` (at least
          10 percent in 2 cohorts), ``strict`` (also the strict
          minimum-effect test), ``k_over_m`` (``'1/4'``),
          ``cohorts_label`` (``'3/4 cohorts, 1 small'``),
          ``small_cohorts``, ``detection`` (``{pct_median, n_datasets,
          low_detection}`` or ``None``) and ``low_detection`` (median
          share of cells expressing the gene below 5 percent: a hit is
          then carried by few cells), ``gene_flags`` with the context
          flag ``foreign_lineage_marker``, ``links`` (relative URLs of
          the per-donor plot and values, see
          :meth:`get_facet_donor_values`) and ``forest``: one row per
          cohort, ``{cohort, cohort_label, cohort_display, counted,
          in_m, small, anchor, more_units, n_more_units}``, where
          ``anchor`` carries ``logFC``, ``ci_low`` / ``ci_high`` (limma
          95 percent interval), ``p_value``, ``q_value``, ``lambda_gc``,
          ``inflated``, ``small_sample``, ``modality`` (nuclei or cells)
          and ``not_counted_reason``. Counted cohorts come first; the
          counted rows are the cohorts behind ``m``. The word "small" is
          not a call. Unit cards add ``anchor_counted``,
          ``cohort_display``, ``modality``, ``modality_note`` and
          ``small_sample``.

        How to read it: ``counted`` means the unit counts towards
        replication (counted cohort, not descriptive only, not a
        secondary dataset, the anchor unit of its cohort for the group);
        ``not_counted_reason`` says why not. ``replicated`` is the
        partial-conjunction call (``r_value < 0.05``: the effect is
        present in at least 2 independent cohorts, same direction);
        "k of m" is only the descriptive count of cohorts individually
        at ``q_value < 0.05`` in the replicated direction. For a result
        worth following up, read ``replicated`` together with
        ``effect_gate_passed`` and, strictest, ``replicated_effect``.
        ``definition`` holds the full rule; a cohort counts once.
        ``descriptive_only`` units have no p or q. For APOE locus genes
        (APOE, APOC1, TOMM40, NECTIN2, ...) an e4 effect can be
        allele-linked expression or read mapping, not downstream
        biology (``notes``).

        Args:
            facet: ``'sex'`` / ``'ant_sex'``, or any of
                :attr:`TESTED_FACETS`.
            gene: Gene symbol (case-insensitive).
            cell_type: A cell type group (``'microglia'``) or a raw
                stratum label (``'Mic P2RY12'``).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the units, needs
                pandas).
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Facets are tested, counted and
                replicated inside one scope, so human and mouse results
                never mix. Default: the server's single-answer scope,
                ``human_postmortem`` (``xenograft_human_in_mouse`` for
                ``app_nlgf``). Unknown names give HTTP 400. Sent only
                when given.
            forest: Detail of the ``forest`` rows, one of
                :attr:`FOREST_DETAIL` (REST API 2.45.0+). Server
                default here: ``'full'``. Sent only when given.
        """
        params: Dict[str, Any] = {'gene': gene}
        if cell_type is not None:
            params['cell_type'] = cell_type
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
        if forest is not None:
            params['forest'] = self._check_forest(forest)
        result = self._get(f'/facets/{facet}/effects', params=params)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('units', []))
        return result

    def get_facet_recurring_genes(self, facet: str,
                                  cell_type: Optional[str] = None,
                                  n: int = 50,
                                  exclude_secondary: bool = True,
                                  format: str = 'json',
                                  scope: Optional[str] = None,
                                  *,
                                  offset: int = 0,
                                  effect_gate: bool = False,
                                  strict: bool = False,
                                  fetch_all: bool = False,
                                  ) -> Union[Dict, Any]:
        """``GET /facets/{facet}/effects/top``. Genes that change with a
        facet in the most independent cohorts, within one cell type
        group.

        Exploratory, donor-level, no meta-analysis; the disease contrast
        is provisional and the cohort table is a DRAFT. A high rank is a
        count of separate significant results, not a pooled estimate.

        Responses carry ``help_topics`` (REST API 2.44.0+): topic ids for
        :meth:`get_help_topic` that explain how to read the numbers.

        Only replicated genes are listed (partial-conjunction
        ``r_value < 0.05``: effect present in at least 2 independent
        cohorts, same direction; REST API 2.44.0+). ``effect_gate=True``
        keeps those that also pass the effect gate (at least 2 cohorts
        with a log2 fold change of at least ``effect_gate_tau``),
        ``strict=True`` those that also pass the strict minimum-effect
        test (``replicated_effect``); ``n_matching`` counts the rows
        the filters keep. Ranking (``ranking`` in the response):
        ``r_value`` ascending, then k (cohorts individually at
        q_value < 0.05 in the replicated direction; 2.44.0+),
        ``agreement``
        (fraction of the m tested cohorts with that sign) and
        ``median_abs_logFC``. k of m is descriptive, not the
        replication call.

        Returns ``{facet, cell_type, celltype_group, cl_id, labels,
        scope, scopes, cohorts_in_cell_type, n_replicated,
        n_r_significant, n_effect_gate, n_replicated_effect,
        n_replicated_strong, n_genes_in_family, donors_in_cell_type,
        donor_count_definition, cell_types, rows, definition, ranking,
        exclude_secondary, cohort_table_status}``; ``n_replicated``
        counts the replicated genes of the family,
        ``n_r_significant`` the genes with ``r_value < 0.05`` before
        the gate, ``n_effect_gate`` those that also pass it,
        ``n_replicated_effect`` those that replicate at the 10 percent
        minimum effect (``r_treat``), ``n_replicated_strong`` those at
        the 25 percent tier (``r_strong``; null where a run has no
        strong p values), ``n_genes_in_family`` the genes
        tested in at least 2 cohorts, and ``rows`` holds at most ``n``
        rows from ``offset``. REST API 2.45.0+ adds ``headline``
        (``n_replicated``, ``n_effect_gate``, ``n_strict``,
        ``strict_genes``, ``n_genes_in_family``, ``cohorts``,
        ``effect_gate_unit``), ``offset``, ``n_matching`` (rows the
        filters keep), ``n_returned``, ``has_more``, ``n_cohorts_tested``
        and ``not_tested`` (why a cell type group has no family); rows
        add ``n_small``, ``small_cohorts``, ``detection``,
        ``low_detection``, ``min_logFC`` / ``max_logFC`` (smallest and
        largest per-cohort effect) and context ``gene_flags``; every
        ``cell_types`` entry adds ``n_genes_in_family``,
        ``n_replicated``, ``n_effect_gate`` and ``n_strict``. Rows:
        ``gene, r_value, p_pc, k, k_treat, m, replicated, direction,
        agreement, median_abs_logFC, median_logFC,
        effect_gate_passed, effect_gate_tau, effect_gate_cohorts,
        r_cohorts, r_treat, replicated_effect, r_strong,
        replicated_strong, persists_after_pathology_adjustment (facet
        ``age`` only: the gene also replicates, same direction, in
        ``age_adjusted`` of the same scope and cell type group; null
        for other facets or when there is no such group), gene_flags,
        cohorts,
        cohorts_tested_labels, overlap_unknown,
        cohorts_overlap_unknown`` (see :meth:`get_facet_gene_effects`
        for the meaning) plus the old
        ``cohorts_sig_same_sign, cohorts_tested, cohorts_same_sign,
        sign_discordant, apoe_locus``. The v1 fields ``units_up``,
        ``units_down`` and ``max_abs_logFC`` are gone.
        ``cell_types`` lists the groups with their raw ``labels`` and
        counted cohorts.

        Args:
            facet: ``'sex'`` / ``'ant_sex'``, or any of
                :attr:`TESTED_FACETS`.
            cell_type: A cell type group (``'microglia'``) or a raw
                label; default: the group with most counted cohorts.
            n: Rows returned (1 to 100000 on REST API 2.45.0+; servers
                before 2.45.0 accept at most 500).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas; the family counts ``n_replicated``,
                ``n_genes_in_family``, ``n_r_significant``,
                ``n_effect_gate``, ``n_replicated_effect``,
                ``n_replicated_strong`` and, on 2.45.0+, ``n_matching``,
                ``has_more`` and ``headline`` go to ``df.attrs``).
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Facets are tested, counted and
                replicated inside one scope, so human and mouse results
                never mix. Default: the server's single-answer scope,
                ``human_postmortem`` (``xenograft_human_in_mouse`` for
                ``app_nlgf``). Unknown names give HTTP 400. Sent only
                when given.
            offset: Ranked rows to skip (REST API 2.45.0+). Sent only
                when above 0.
            effect_gate: Only genes whose effect gate passed (REST API
                2.45.0+). Sent only when ``True``.
            strict: Only genes that also pass the strict minimum-effect
                test (REST API 2.45.0+). Sent only when ``True``.
            fetch_all: Page through every matching row from ``offset``
                (pages of ``n`` rows, following ``has_more``) and return
                them as one response with ``has_more`` ``False``.

        :meth:`get_facet_top_genes` is the same method under the name
        the MCP tool uses.
        """
        params: Dict[str, Any] = {'n': n}
        if cell_type is not None:
            params['cell_type'] = cell_type
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
        if offset:
            params['offset'] = offset
        if effect_gate:
            params['effect_gate'] = 'true'
        if strict:
            params['strict'] = 'true'
        result = self._paged_get(f'/facets/{facet}/effects/top', params,
                                 fetch_all)
        if format == 'dataframe':
            import pandas as pd
            frame = pd.DataFrame(result.get('rows', []))
            for key in ('n_replicated', 'n_genes_in_family', 'n_r_significant',
                        'n_effect_gate', 'n_replicated_effect',
                        'n_replicated_strong', 'n_matching', 'has_more',
                        'headline'):
                if key in result:
                    frame.attrs[key] = result[key]
            return frame
        return result

    def get_gene_facet_effects(self, gene: str,
                               exclude_secondary: bool = True,
                               scope: Optional[str] = None,
                               facet: Union[str, Sequence[str], None] = None,
                               include_units: bool = True,
                               forest: Optional[str] = None) -> Dict:
        """``GET /genes/{gene}/facet-effects``. Result cards of one gene
        for every tested facet (:attr:`TESTED_FACETS`).

        The response carries ``help_topics`` (see
        :meth:`get_help_topic`).

        Returns ``{gene, scope, facets: {facet: card}}``; each card has
        the shape of :meth:`get_facet_gene_effects`. Without ``scope``
        (REST API 2.44.0+) ``facets`` holds one card per facet in its
        default scope and ``by_scope`` holds ``{scope: {facet: card}}``
        so human and xenograft results can be shown separately;
        ``strongest_facet`` (``{facet, cell_type, r_value, scope}``)
        names the facet with the strongest replication, to open on. A
        gene symbol is never matched across scopes. Exploratory,
        donor-level, no meta-analysis; the disease card is provisional.
        The first call per facet reads every result file on the server
        (a few seconds); later calls are cached.

        The all-facets answer is large (about 5 MB for a gene such as
        PTPRG, because every card lists every unit). Ask for fewer
        facets with ``facet`` and/or drop the unit lists with
        ``include_units=False`` (about 0.5 MB): each card then keeps
        ``n_units``, ``replication``, ``cell_types`` and ``notes`` but
        its ``units`` list is empty. Fetch the units of one facet with
        :meth:`get_facet_gene_effects`. Unit cards also carry
        ``replicate_unit`` and ``replicate_column`` (what a replicate
        is in that unit, for example donor or animal, and the obs
        column that names it).

        Args:
            gene: Gene symbol (case-insensitive).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            scope: Facet scope, one of :attr:`FACET_SCOPES`
                (``'human_postmortem'``, ``'xenograft_human_in_mouse'``,
                ``'in_vitro'``). Facets are tested, counted and
                replicated inside one scope, so human and mouse results
                never mix. Default: every scope, in
                ``by_scope``. Unknown names give HTTP 400. Sent only
                when given.
            facet: One facet or a list of facets from
                :attr:`TESTED_FACETS` (sent as a comma separated list).
                Default: all. An untested facet gives HTTP 404;
                ``strongest_facet`` is chosen among those asked for.
            include_units: ``False`` empties ``units`` in every card
                (see above). Sent only when ``False``.
            forest: Detail of the replication ``forest`` rows, one of
                :attr:`FOREST_DETAIL` (REST API 2.45.0+). Server default
                here: ``'anchors'`` (folded units left out, counted in
                ``n_more_units``). Sent only when given.
        """
        params: Dict[str, Any] = {}
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
        if facet is not None:
            params['facet'] = _csv_param(facet)
        if not include_units:
            params['include_units'] = 'false'
        if forest is not None:
            params['forest'] = self._check_forest(forest)
        return self._get(f'/genes/{gene}/facet-effects',
                         params=params or None)

    #: Same as :meth:`get_facet_recurring_genes`, under the name the MCP
    #: tool ``facet_top_genes`` uses.
    get_facet_top_genes = get_facet_recurring_genes

    # ── Facets: modules, overview, donor values (REST API 2.45.0+) ──────

    def get_facet_top_modules(self, facet: str,
                              cell_type: Optional[str] = None,
                              n: int = 50,
                              *,
                              offset: int = 0,
                              effect_gate: bool = False,
                              include_not_replicated: bool = False,
                              catalog: Optional[str] = None,
                              exclude_secondary: bool = True,
                              scope: Optional[str] = None,
                              format: str = 'json',
                              fetch_all: bool = False,
                              ) -> Union[Dict, Any]:
        """``GET /facets/{facet}/modules/top``. Gene sets (modules) that
        replicate for one facet within one cell type group (REST API
        2.45.0+).

        Exploratory, donor-level. Module replication uses the same
        partial conjunction as genes (Simes, u = 2) on the cohort p
        values of the donor-level module scores (parametric p when the
        unit has at least 30 residual df, else the permutation p), BH
        over the modules of one (scope, facet, cell type group, method,
        source) family. The effect gate is a signed standardised effect
        of at least ``effect_gate_tau`` (0.2 SD per contrast, 0.2/3 per
        Braak stage, 0.2 per decade, 0.1 per e4 allele) in 2 cohorts,
        read on the lower 95 percent confidence bound. There is no
        strict tier for modules: rows have no ``strict``, ``r_strong``,
        ``replicated_strong``, ``r_treat`` or ``replicated_effect`` keys
        (servers before the 2.45.0 release sent them as ``None``).
        Directionless sets (GWAS, MAGMA, bidirectional)
        are left out. Ranked by r-value, then k, agreement and the
        standardised effect.

        Returns ``{facet, scope, scopes, cell_type, celltype_group,
        labels, cohorts_in_cell_type, n_cohorts_tested, not_tested,
        n_modules_in_family, n_replicated, n_effect_gate, offset,
        n_matching, n_returned, has_more, cell_types, rows,
        effect_gate_rule, exclude_secondary, help_topics}``.
        ``effect_gate_rule`` is ``{std_effect, wording}`` (named
        ``effect_gate`` on servers before the 2.45.0 release). A row is
        ``{geneset_id, catalog, module, method, source, variant_label,
        r_value, p_pc, k, m, r_direction, replicated, call, nt_label,
        gate, effect_gate_passed, effect_gate_tau, effect_gate_unit,
        effect_gate_cohorts, n_cohorts_ge_gate, median_std_effect,
        median_estimate, min_effect, max_effect, agreement, r_cohorts,
        n_small, small_cohorts, k_over_m, cohorts_label,
        n_cohorts_tested, p_check_cohorts, p_check_flag,
        per_region_only_cohorts, module_scope_note, other_variants, ab,
        quality_sensitive, directionless, n_genes}``. ``ab.disagree``
        flags opposite directions of the count pseudobulk (B) and the
        per-cell score mean (A) variants.

        Args:
            facet: One of :attr:`TESTED_FACETS` (or the ``ant_`` column).
            cell_type: Cell type group or raw label; default: the group
                with most counted cohorts.
            n: Rows returned (server default 50).
            offset: Ranked rows to skip. Sent only when above 0.
            effect_gate: Only modules whose effect gate passed. Sent only
                when ``True``.
            include_not_replicated: Also list modules that do not
                replicate. Sent only when ``True``.
            catalog: Restrict to one gene set catalog, for example
                ``'Sierksma2025/WGCNA'``.
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            scope: Facet scope, one of :attr:`FACET_SCOPES`. Default:
                the server's single-answer scope. Sent only when given.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas; ``n_replicated``, ``n_effect_gate``,
                ``n_modules_in_family``, ``n_matching``, ``has_more`` and
                ``effect_gate_rule`` go to ``df.attrs``).
            fetch_all: Page through every matching row (see
                :meth:`get_facet_recurring_genes`).
        """
        params: Dict[str, Any] = {'n': n}
        if cell_type is not None:
            params['cell_type'] = cell_type
        if offset:
            params['offset'] = offset
        if effect_gate:
            params['effect_gate'] = 'true'
        if include_not_replicated:
            params['include_not_replicated'] = 'true'
        if catalog is not None:
            params['catalog'] = catalog
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if scope is not None:
            params['scope'] = scope
        result = self._paged_get(f'/facets/{facet}/modules/top', params,
                                 fetch_all)
        if format == 'dataframe':
            import pandas as pd
            frame = pd.DataFrame(result.get('rows', []))
            for key in ('n_replicated', 'n_effect_gate', 'n_modules_in_family',
                        'n_matching', 'has_more', 'effect_gate_rule'):
                if key in result:
                    frame.attrs[key] = result[key]
            return frame
        return result

    def get_facet_effects_overview(self, facet: str, n: int = 10,
                                   scope: Optional[str] = None,
                                   exclude_secondary: bool = True) -> Dict:
        """``GET /facets/{facet}/effects/overview``. The simplified facet
        view in one call: replicated genes per cell type group (REST API
        2.45.0+). Exploratory, donor-level.

        Returns ``{facet, scope, scopes, headline, cell_types,
        quiet_groups, n_genes_unique}``. ``headline`` is
        ``{celltype_group, cohorts, n_genes_in_family, n_replicated,
        n_effect_gate, n_strict, strict_genes, effect_gate_unit}``.
        Each ``cell_types`` entry is ``{celltype_group, cl_id,
        n_cohorts, n_genes_in_family, n_replicated, n_effect_gate,
        n_strict, n_assigned, genes}`` with gene rows ``{gene, r_value,
        direction, median_logFC, k, m, effect_gate_passed, strict,
        also_in, detection, low_detection}``. A gene replicated in
        several groups is listed once, in the group where it is
        strongest (``also_in`` names the others). ``quiet_groups`` lists
        groups without replicated genes with their counts and
        ``low_power``; no replicated gene there is not evidence of no
        effect.

        Args:
            facet: One of :attr:`TESTED_FACETS`.
            n: Genes listed per cell type group (server default 10).
            scope: Facet scope, one of :attr:`FACET_SCOPES`. Sent only
                when given.
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
        """
        params: Dict[str, Any] = {'n': n}
        if scope is not None:
            params['scope'] = scope
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        return self._get(f'/facets/{facet}/effects/overview', params=params)

    def get_geneset_facet_effects(self, study: str, experiment: str,
                                  name: str,
                                  facet: Union[str, Sequence[str], None] = None,
                                  scope: Optional[str] = None,
                                  cell_type: Optional[str] = None,
                                  exclude_secondary: bool = True,
                                  include_units: bool = False,
                                  forest: Optional[str] = None) -> Dict:
        """``GET /genesets/{study}/{experiment}/{name}/facet-effects``.
        Result card of one gene set: its module replication per facet
        next to the member genes that replicate for the same facet
        (REST API 2.45.0+). Exploratory, donor-level.

        Returns ``{geneset_id, catalog, module, display_name,
        description, organism, n_genes, celltypes, role, direction,
        quality, directionless, directionless_reason,
        directionless_note, member_match, scored_in, scope, facets,
        by_scope, effect_gate_rule, help_topics}``. ``facets`` maps each
        facet to ``{facet, scope, module, members}``; ``by_scope`` is
        present only without ``scope``. ``effect_gate_rule`` is
        ``{std_effect: 0.2, per_facet: {facet: tau}}`` (named
        ``effect_gate`` before the 2.45.0 release).

        ``module`` is ``{catalog, module, module_key, facet, scope,
        cell_type, directionless, effect_gate_rule: {std_effect,
        wording}, replication, n_units, units}``; each ``replication``
        entry has ``celltype_group``, ``cl_id``, ``labels``, ``primary``
        (the view variant: the count pseudobulk B where the module has
        one, else the per-cell score mean A, UCell then score_genes),
        ``variants`` (one per method and source, with the module top
        row fields plus ``label``, ``basis``, ``forest``,
        ``family_rank`` / ``family_size`` and ``links``) and ``ab``
        (``{disagree, reason}``: True only for opposite directions with
        one variant replicated or both effects at least 0.2). A module
        forest row carries ``estimate``, ``ci_low``, ``ci_high``,
        ``std_effect``, ``p_param``, ``q_param``, ``p_perm``,
        ``q_perm``, ``df_resid``, ``p_used``, ``p_basis``
        (``parametric`` | ``permutation``) and ``has_counts``.
        Directionless sets have ``call`` ``not_tested`` with
        ``nt_label`` ``'no expected direction'``: never read them as
        "not replicated".

        ``members`` is ``{facet, scope, scopes, n_members,
        n_sex_chromosome_excluded, groups}``, never truncated; per cell
        type group ``{n_members, n_in_data, n_tested, n_replicated,
        n_up, n_down, n_effect_gate, n_strict, genes_up, genes_down,
        genes_gate, genes}``. For ``sex`` the member counts leave out
        chrX and chrY genes. ``members`` is ``None`` when the gene set
        store has no such set.

        Args:
            study, experiment, name: Gene set id parts.
            facet: One facet or a list from :attr:`TESTED_FACETS` (sent
                as a comma separated list). Default: all.
            scope: Facet scope, one of :attr:`FACET_SCOPES`. Default:
                every scope, in ``by_scope``. Sent only when given.
            cell_type: Restrict to one cell type group or raw label.
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            include_units: ``True`` adds the flat unit list (server
                default ``False``). Sent only when ``True``.
            forest: Forest row detail, one of :attr:`FOREST_DETAIL`
                (server default here ``'anchors'``). Sent only when
                given.
        """
        params: Dict[str, Any] = {}
        if facet is not None:
            params['facet'] = _csv_param(facet)
        if scope is not None:
            params['scope'] = scope
        if cell_type is not None:
            params['cell_type'] = cell_type
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        if include_units:
            params['include_units'] = 'true'
        if forest is not None:
            params['forest'] = self._check_forest(forest)
        return self._get(
            f'/genesets/{study}/{experiment}/{name}/facet-effects',
            params=params or None)

    def get_gene_detection(self, gene: str,
                           format: str = 'json') -> Union[Dict, Any]:
        """``GET /genes/{gene}/detection``. How many cells express a gene
        per dataset and per (scope, cell type group) (REST API 2.45.0+).

        The source of the low-detection pill on facet results: a gene
        detected in under ``low_detection_pct`` (5) percent of cells
        (median over the datasets of a group) gives replication results
        carried by few cells. Read from precomputed gene statistics,
        never a live scan.

        Returns ``{gene, low_detection_pct, min_cells, n_candidates,
        n_missing, partial, summary, datasets}``. ``summary`` rows:
        ``{scope, celltype_group, n_datasets, pct_median, pct_min,
        pct_max, n_cells, low_detection}``; ``datasets`` rows:
        ``{db_id, title, scope, celltype_group, cell_type, n_cells, pct,
        modality, modality_note, technology, mean, median_nonzero,
        q10_nonzero, q90_nonzero, rank_mean}``. Strata below
        ``min_cells`` (50) cells are left out. ``partial`` is True when
        some datasets had no statistics (``n_missing``).

        Args:
            gene: Gene symbol.
            format: ``'json'`` or ``'dataframe'`` (the ``datasets``
                rows, needs pandas; ``summary`` goes to
                ``df.attrs['summary']``).
        """
        result = self._get(f'/genes/{gene}/detection')
        if format == 'dataframe':
            import pandas as pd
            frame = pd.DataFrame(result.get('datasets', []))
            frame.attrs['summary'] = result.get('summary', [])
            return frame
        return result

    def _donor_value_params(self, cell_type: str, gene: Optional[str],
                            module: Union[str, Sequence[str], None],
                            method: Optional[str], source: Optional[str],
                            scope: Optional[str], adjusted: bool,
                            include_uncounted: bool,
                            exclude_secondary: bool,
                            db_id: Optional[str]) -> Dict[str, Any]:
        """Shared query of the donor-values and donor-plot routes."""
        if (gene is None) == (module is None):
            raise ValueError('give exactly one of gene or module')
        params: Dict[str, Any] = {'cell_type': cell_type}
        if gene is not None:
            params['gene'] = gene
        else:
            params['module'] = (module if isinstance(module, str)
                                else '/'.join(module))
        for key, value in (('method', method), ('source', source),
                           ('scope', scope), ('db_id', db_id)):
            if value is not None:
                params[key] = value
        if adjusted:
            params['adjusted'] = 'true'
        if include_uncounted:
            params['include_uncounted'] = 'true'
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        return params

    def get_facet_donor_values(self, facet: str, cell_type: str,
                               gene: Optional[str] = None,
                               module: Union[str, Sequence[str], None] = None,
                               *,
                               method: Optional[str] = None,
                               source: Optional[str] = None,
                               scope: Optional[str] = None,
                               adjusted: bool = False,
                               include_uncounted: bool = False,
                               exclude_secondary: bool = True,
                               db_id: Optional[str] = None,
                               format: str = 'json') -> Union[Dict, Any]:
        """``GET /facets/{facet}/donor-values``. The replicate-level
        (donor or animal) values behind one facet result: one number per
        donor per counted cohort, with the model's estimate (REST API
        2.45.0+). Check these before trusting an estimate: a few donors
        can carry an effect.

        Returns ``{facet, scope, subject, cell_type, facet_kind
        (two_group | numeric), levels (two-group: [reference, tested]),
        x_label, scale, adjusted, n_units, n_counted, note,
        anonymisation, units, help_topics}``. A unit is ``{db_id, title,
        cohort, cohort_label, cohort_display, cell_type,
        celltype_source, counted, not_counted_reason, pooled,
        descriptive_only, replicate_unit, available, reason, covariates,
        n_donors, scale, facet_kind, model: {estimate, ci_low, ci_high,
        p_value, q_value, unit}, line, donors}``; a donor is ``{label,
        facet_value, group, value, n_cells}``. ``label`` is a
        sequential ``D01``, ``D02``, ... per unit; no donor id or
        per-donor covariate is ever returned. A gene ``value`` is the
        pseudobulk on the scale the test fits (log2 CPM with TMM
        library sizes, the voom input); a module ``value`` is the
        replicate score the module test fitted. ``adjusted=True``
        removes the covariate part by ordinary least squares; the test
        also uses voom precision weights, so a drawn trend can differ a
        little from ``model.estimate``. A unit run before per-replicate
        values were stored answers ``available: False`` with a
        ``reason``.

        Args:
            facet: One of :attr:`TESTED_FACETS`.
            cell_type: Cell type group (``'microglia'``) or stratum
                label. Required.
            gene: A gene symbol. Give exactly one of ``gene`` and
                ``module``.
            module: A gene set as ``'Study/Experiment/name'`` (or a
                3-item sequence).
            method, source: The module variant (``'pseudobulk'`` or
                ``'cell_mean'``; ``'geneset'``, ``'ucell'`` or
                ``'gene_list'``).
            scope: Facet scope, one of :attr:`FACET_SCOPES`.
            adjusted: Remove the covariate part. Sent only when ``True``.
            include_uncounted: Add the units that do not carry a
                cohort's vote, with the reason. Sent only when ``True``.
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            db_id: Only the units of that dataset, counted or not, and
                the exact ``cell_type`` stratum when one matches (404
                when the dataset has no unit).
            format: ``'json'`` or ``'dataframe'`` (one row per donor
                with the unit's ``db_id``, ``cohort``, ``cell_type`` and
                ``counted``; needs pandas).

        Raises:
            ValueError: Neither or both of ``gene`` and ``module``.
            requests.HTTPError: 400 for a bad ``scope`` or a malformed
                ``module``, 404 when the result was not tested.
        """
        params = self._donor_value_params(
            cell_type, gene, module, method, source, scope, adjusted,
            include_uncounted, exclude_secondary, db_id)
        result = self._get(f'/facets/{facet}/donor-values', params=params)
        if format == 'dataframe':
            import pandas as pd
            rows = []
            for unit in result.get('units', []):
                for donor in unit.get('donors') or []:
                    rows.append({'db_id': unit.get('db_id'),
                                 'cohort': unit.get('cohort'),
                                 'cell_type': unit.get('cell_type'),
                                 'counted': unit.get('counted'),
                                 **donor})
            frame = pd.DataFrame(rows)
            for key in ('scale', 'facet_kind', 'levels', 'adjusted'):
                if key in result:
                    frame.attrs[key] = result[key]
            return frame
        return result

    def get_facet_donor_plot(self, facet: str, cell_type: str,
                             gene: Optional[str] = None,
                             module: Union[str, Sequence[str], None] = None,
                             *,
                             fmt: str = 'png',
                             method: Optional[str] = None,
                             source: Optional[str] = None,
                             scope: Optional[str] = None,
                             adjusted: bool = False,
                             include_uncounted: bool = False,
                             exclude_secondary: bool = True,
                             db_id: Optional[str] = None,
                             width: Optional[float] = None,
                             height: Optional[float] = None,
                             dpi: Optional[int] = None,
                             dest: Union[str, Path, None] = None) -> Dict:
        """``GET /facets/{facet}/donor-plot.{fmt}``. Small multiples of
        the values of :meth:`get_facet_donor_values`, one panel per
        counted cohort on a shared y axis (REST API 2.45.0+).

        The image is clean for reuse: panel titles are only the cohort
        and ``n``. The effect, interval and q of every cohort, the scale
        and the model covariates are in the ``caption`` (the
        ``X-Plot-Caption`` header), never drawn.

        Args:
            facet, cell_type, gene, module, method, source, scope,
                adjusted, include_uncounted, exclude_secondary, db_id:
                As in :meth:`get_facet_donor_values`.
            fmt: One of :attr:`DONOR_PLOT_FORMATS`.
            width, height: Inches per panel (server defaults 2.6, 3.0).
            dpi: Raster resolution (server default 150).
            dest: A path to write the image to (also returned as
                ``path``).

        Returns:
            ``{bytes, format, caption, mime, path}``; ``path`` is
            ``None`` without ``dest``.

        Raises:
            ValueError: Bad ``fmt``, or neither or both of ``gene`` and
                ``module``.
            requests.HTTPError: 404 with the reasons when no unit has
                stored values.
        """
        if fmt not in self.DONOR_PLOT_FORMATS:
            raise ValueError(f"fmt must be one of {self.DONOR_PLOT_FORMATS}, "
                             f"got {fmt!r}")
        params = self._donor_value_params(
            cell_type, gene, module, method, source, scope, adjusted,
            include_uncounted, exclude_secondary, db_id)
        for key, value in (('width', width), ('height', height),
                           ('dpi', dpi)):
            if value is not None:
                params[key] = value
        response = self._get_raw(f'/facets/{facet}/donor-plot.{fmt}',
                                 params=params)
        target: Optional[Path] = None
        if dest is not None:
            target = Path(dest)
            target.write_bytes(response.content)
        return {
            'bytes': response.content,
            'format': response.headers.get('X-Plot-Format', fmt),
            'caption': response.headers.get('X-Plot-Caption', ''),
            'mime': response.headers.get('Content-Type', ''),
            'path': target,
        }

    # ── Donor variability (REST API 2.45.0+) ────────────────────────────
    #
    # How much a gene or gene set varies between replicates (donors;
    # animals in xenografts) after each rung of a design
    # (:attr:`DONOR_VARIABILITY_RUNGS`). Relative and exploratory: the
    # excess ranking is a rank, not a test.

    def get_database_donor_variability(self, db_id: str,
                                       feature_type: Optional[str] = None,
                                       cell_type: Optional[str] = None,
                                       *,
                                       celltype_source: Optional[str] = None,
                                       feature: Optional[str] = None,
                                       method: Optional[str] = None,
                                       sort: Optional[str] = None,
                                       rung: Optional[str] = None,
                                       min_reliability: Optional[float] = None,
                                       limit: Optional[int] = None,
                                       format: str = 'json',
                                       ) -> Union[Dict, Any]:
        """``GET /databases/{db_id}/donor-variability``. Donor
        (replicate) variability of one dataset, per cell type stratum
        with at least 12 replicates of at least 40 cells (REST API
        2.45.0+). A replicate is the dataset's ``replicate_unit``
        (donor; animal in xenografts, where it is host-to-host
        variability). Exploratory.

        Without ``feature_type``: the summary ``{db_id, status
        (analysed | not_analysed | not_run), reason, facet_scope,
        replicate_unit, replicate_column, files, run, parameters, units,
        help_topics}``; each unit lists ``cell_type``,
        ``celltype_group``, ``n_replicates``, ``rung_deepest``,
        ``rungs`` (``{name, terms, n, p, residual_df, computed,
        reason}``), ``median_reliability``, ``median_sd_donor``,
        ``n_genes``, ``n_modules`` and ``counted_in_cross_cohort``.

        With ``feature_type`` and ``cell_type`` it adds ``{unit, rung,
        n_matching, n_returned, rows}``. A gene row is ``{gene,
        n_replicates, reliability, reliability_ci, r_half, v_donor,
        v_donor_ci, sd_donor (log2 units), fold_range_donor, var_half,
        rungs, deepest_rung, share_design, share_design_ci,
        share_blocks, lmg, lmg_rung, r_composition_pc1, ave_expr,
        half_count_q70}``; ``rungs`` maps each rung to ``{n, p, terms,
        v, sd, fold_range, share_design, excess_z, p_excess}`` or
        ``None`` (a rung the cohort cannot support). ``v`` / ``sd`` are
        what the rung's design leaves unexplained; ``excess_z`` /
        ``p_excess`` rank the unexplained variance against the
        mean-variance trend within the unit (a rank, not a test;
        ``None`` below 20 replicates or reliability 0.3). Module rows
        add ``module``, ``method`` (``pseudobulk`` primary,
        ``cell_mean`` secondary), ``source`` and ``n_genes_found`` and
        have no ``excess_z``.

        Args:
            db_id: Dataset id.
            feature_type: ``'gene'`` or ``'module'``; needs
                ``cell_type``.
            cell_type: Stratum label or its cell type group
                (``'microglia'``; the exact label wins, else the unit of
                that group with the most replicates).
            celltype_source: Stratifier, when a label occurs under
                several.
            feature: One gene or module.
            method: Module method (``'pseudobulk'`` or ``'cell_mean'``).
            sort: ``'excess'`` (server default), ``'sd'``,
                ``'reliability'`` or ``'feature'``.
            rung: One of :attr:`DONOR_VARIABILITY_RUNGS` (server default
                ``'D1'``).
            min_reliability: Lower bound on reliability (server default
                0).
            limit: Rows returned (server default 100, at most 2000).
            format: ``'json'`` or ``'dataframe'`` (the ``rows``, or the
                ``units`` of the summary; needs pandas).

        All optional arguments are sent only when given.

        Raises:
            requests.HTTPError: 400 for a bad ``rung``, ``sort`` or
                ``feature_type`` or ``feature_type`` without
                ``cell_type``; 404 for an unknown dataset or unit.
        """
        params: Dict[str, Any] = {}
        for key, value in (('feature_type', feature_type),
                           ('cell_type', cell_type),
                           ('celltype_source', celltype_source),
                           ('feature', feature), ('method', method),
                           ('sort', sort), ('rung', rung),
                           ('min_reliability', min_reliability),
                           ('limit', limit)):
            if value is not None:
                params[key] = value
        result = self._get(f'/databases/{db_id}/donor-variability',
                           params=params or None)
        if format == 'dataframe':
            import pandas as pd
            key = 'rows' if 'rows' in result else 'units'
            return pd.DataFrame(result.get(key, []))
        return result

    def get_gene_donor_variability(self, gene: str,
                                   scope: Optional[str] = None,
                                   cell_type: Optional[str] = None,
                                   rung: Optional[str] = None,
                                   exclude_secondary: bool = True) -> Dict:
        """``GET /genes/{gene}/donor-variability``. One gene's donor
        variability in every analysed unit, with a cross-cohort ranking
        (REST API 2.45.0+). Relative and exploratory, not a test.

        Returns ``{feature, feature_type, scope, rung, rung_label,
        units, by_group, help_topics}``. A unit is ``{db_id, title,
        cohort, cohort_label, cell_type, celltype_source,
        celltype_group, n_replicates, replicate_unit, rung_deepest,
        counted, not_counted_reason, values}`` with ``values`` the gene
        row of :meth:`get_database_donor_variability`. A unit is
        ``counted`` when it is the anchor of a counted cohort for the
        group with at least 30 replicates. ``by_group`` rows are
        ``{celltype_group, m, k, m_with_gene, p_rank_product, r_value,
        rung}``: ``m`` counted cohorts reaching the rung, ``k`` cohorts
        with the gene in the top 5 percent of the excess ranking,
        ``p_rank_product`` Fisher's combination of the cohorts'
        ``p_excess`` (at least 2 cohorts) and ``r_value`` its BH
        adjustment over the genes of the (scope, group, rung) family.
        The null is that a gene's ranks in different cohorts are
        independent, so the pair measures whether the excess
        reproduces, not whether the gene is variable in absolute terms.

        Args:
            gene: Gene symbol.
            scope: Facet scope, one of :attr:`FACET_SCOPES` (server
                default ``human_postmortem``; scopes are never mixed).
            cell_type: Cell type group or stratum label.
            rung: One of :attr:`DONOR_VARIABILITY_RUNGS` (server default
                ``'D1'``).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.

        Raises:
            requests.HTTPError: 404 when no analysed unit has the gene
                in the scope.
        """
        params: Dict[str, Any] = {}
        for key, value in (('scope', scope), ('cell_type', cell_type),
                           ('rung', rung)):
            if value is not None:
                params[key] = value
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        return self._get(f'/genes/{gene}/donor-variability',
                         params=params or None)

    def get_geneset_donor_variability(self, study: str, experiment: str,
                                      name: str,
                                      scope: Optional[str] = None,
                                      cell_type: Optional[str] = None,
                                      rung: Optional[str] = None,
                                      method: Optional[str] = None,
                                      source: Optional[str] = None,
                                      exclude_secondary: bool = True,
                                      ) -> Dict:
        """``GET /genesets/{study}/{experiment}/{name}/donor-variability``.
        The card of :meth:`get_gene_donor_variability` for a gene set
        (REST API 2.45.0+). The ``pseudobulk`` variant unless ``method``
        is given. ``k``, ``p_rank_product`` and ``r_value`` are ``None``:
        there is no ranking test for modules. Exploratory.

        Args:
            study, experiment, name: Gene set id parts.
            scope, cell_type, rung, exclude_secondary: As in
                :meth:`get_gene_donor_variability`.
            method: ``'pseudobulk'`` or ``'cell_mean'``.
            source: ``'gene_list'``, ``'geneset'`` or ``'ucell'``.
        """
        params: Dict[str, Any] = {}
        for key, value in (('scope', scope), ('cell_type', cell_type),
                           ('rung', rung), ('method', method),
                           ('source', source)):
            if value is not None:
                params[key] = value
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        return self._get(
            f'/genesets/{study}/{experiment}/{name}/donor-variability',
            params=params or None)

    def get_donor_variability_top(self, cell_type: Optional[str] = None,
                                  scope: Optional[str] = None,
                                  feature_type: Optional[str] = None,
                                  rung: Optional[str] = None,
                                  limit: Optional[int] = None,
                                  min_cohorts: Optional[int] = None,
                                  exclude_secondary: bool = True,
                                  format: str = 'json',
                                  ) -> Union[Dict, Any]:
        """``GET /donor-variability/top``. Genes or gene sets that vary
        between replicates in several cohorts at one rung (REST API
        2.45.0+). Relative and exploratory, not a test.

        Returns ``{scope, celltype_group, feature_type, rung,
        rung_label, cohorts, m_cohorts, concordance,
        min_replicates_per_cohort, high_tail, not_enough_cohorts, rows,
        note, help_topics}``. ``celltype_group`` was ``cell_type_group``
        before the 2.45.0 release. ``concordance`` is the mean Spearman
        correlation of ``excess_z`` between the cohort anchors
        (``{mean_spearman, n_pairs, pairs}``) or ``None``. Gene rows:
        ``{gene, m, k, p_rank_product, r_value, median_excess_z,
        median_reliability, median_sd, fold_range}``, sorted by
        ``r_value`` then ``median_excess_z``; module rows: ``{module,
        method, source, m, k (cohorts with reliability >= 0.3),
        median_reliability, median_sd, median_share_design,
        fold_range}``, sorted by ``median_sd``. With fewer counted
        cohorts (30 or more replicates, rung reached) than
        ``min_cohorts``, ``not_enough_cohorts`` is True and ``rows`` is
        empty.

        Args:
            cell_type: Cell type group (server default ``'microglia'``).
            scope: Facet scope, one of :attr:`FACET_SCOPES`.
            feature_type: ``'gene'`` (server default) or ``'module'``.
            rung: One of :attr:`DONOR_VARIABILITY_RUNGS` (server default
                ``'D1'``).
            limit: Rows returned (server default 50).
            min_cohorts: Cohorts needed (server default 2).
            exclude_secondary: ``False`` lets secondary datasets count.
                Sent only when ``False``.
            format: ``'json'`` or ``'dataframe'`` (the rows, needs
                pandas).
        """
        params: Dict[str, Any] = {}
        for key, value in (('cell_type', cell_type), ('scope', scope),
                           ('feature_type', feature_type), ('rung', rung),
                           ('limit', limit), ('min_cohorts', min_cohorts)):
            if value is not None:
                params[key] = value
        if not exclude_secondary:
            params['exclude_secondary'] = 'false'
        result = self._get('/donor-variability/top', params=params or None)
        if format == 'dataframe':
            import pandas as pd
            return pd.DataFrame(result.get('rows', []))
        return result

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

    # ── Plain-language help (REST API 2.44.0+) ──────────────────────────
    #
    # The maintainers' own explanations of facets, replication, scopes,
    # caveats and the glossary (the same texts as the web help page and
    # the MCP ``explain`` tool). Facet, facet-test and dataset-context
    # responses carry ``help_topics``: topic ids to pass to
    # :meth:`get_help_topic`.

    def get_help(self) -> Dict:
        """``GET /help``. Index of the plain-language help (REST API
        2.44.0+).

        Returns ``{topics, glossary}``: ``topics`` is a list of
        ``{id, title, summary}`` and ``glossary`` a list of
        ``{slug, term}``. Responses of the facet endpoints, the facet
        tests and :meth:`get_database_context` carry ``help_topics``,
        a list of topic ids to read with :meth:`get_help_topic` before
        you interpret the numbers. Quote these texts rather than
        paraphrasing them from memory.
        """
        return self._get('/help')

    def get_help_topic(self, topic: str,
                       include_children: bool = True) -> Dict:
        """``GET /help/{topic}``. One help topic (REST API 2.44.0+).

        Returns ``{id, title, summary, body, children, related, terms,
        anchor}``. ``body`` is markdown; ``children`` are the ids of
        subsection topics (already appended to ``body`` unless
        ``include_children`` is ``False``); ``related`` are topic ids
        and ``terms`` glossary slugs for :meth:`get_glossary_term`.
        The topics named in ``help_topics`` of the facet and context
        responses are ``facet-effects``, ``facet-counting``,
        ``facet-replicate-unit``, ``facet-howto``, ``facet-scopes``,
        ``donor-groups``, ``dataset-caveats``, ``per-cell-per-donor``
        and ``dataset-versions``.

        Args:
            topic: Topic id from :meth:`get_help` or ``help_topics``.
            include_children: ``False`` leaves the subsections out of
                ``body``. Sent only when ``False``.

        Raises:
            requests.HTTPError: 404 for an unknown id (the message lists
                close matches).
        """
        params = {'include_children': 'false'} if not include_children else None
        return self._get(f'/help/{topic}', params=params)

    def get_glossary(self, format: str = 'markdown') -> Dict:
        """``GET /help/glossary``. Every glossary term with its
        definition (REST API 2.44.0+).

        Returns ``{format, terms}`` with ``terms`` a list of ``{slug,
        term, definition}``.

        Args:
            format: ``'markdown'`` (default) or ``'html'``.
        """
        if format not in ('markdown', 'html'):
            raise ValueError(
                f"format must be 'markdown' or 'html', got {format!r}")
        params = {'format': format} if format != 'markdown' else None
        return self._get('/help/glossary', params=params)

    def get_glossary_term(self, term: str) -> Dict:
        """``GET /help/glossary/{term}``. One glossary term by slug, for
        example ``'replicate-unit'`` or ``'r-value'`` (REST API
        2.44.0+).

        Returns ``{slug, term, definition, anchor}``.

        Raises:
            requests.HTTPError: 404 for an unknown slug (the message
                lists close matches).
        """
        return self._get(f'/help/glossary/{term}')

    def search_help(self, q: str) -> Dict:
        """``GET /help/search?q=``. Fuzzy match of ``q`` against topic
        ids, titles and glossary terms (REST API 2.44.0+).

        Returns ``{query, matches}``; ``matches`` is a list of ``{id,
        kind, score, title}`` ordered best first, ``kind`` being
        ``'topic'`` (fetch with :meth:`get_help_topic`) or ``'term'``
        (fetch with :meth:`get_glossary_term`).
        """
        return self._get('/help/search', params={'q': q})

    def explain(self, topic_or_term: str = '') -> str:
        """Plain-language explanation of an Anthive concept as markdown
        (REST API 2.44.0+). Mirrors the MCP ``explain`` tool.

        Searches the help (:meth:`search_help`) and returns the best
        matching topic (title, summary, body, related topics and
        glossary terms) or glossary term, followed by the other close
        matches. An empty or unmatched query returns the list of topics
        and glossary terms. Use it before explaining facets,
        replication, scopes, replicate units or caveats, and quote the
        text instead of improvising.

        Args:
            topic_or_term: Topic id, title or glossary term, matched
                fuzzily (``'facet-counting'``, ``'replicate unit'``,
                ``'r-value'``).

        Returns:
            Markdown text.
        """
        query = (topic_or_term or '').strip()
        if query:
            matches = self.search_help(query).get('matches', [])
            if matches:
                best = matches[0]
                if best['kind'] == 'term':
                    data = self.get_glossary_term(best['id'])
                    text = f"# {data['term']} (glossary)\n\n{data['definition']}"
                else:
                    data = self.get_help_topic(best['id'])
                    parts = [f"# {data['title']}", '', data['summary'], '',
                             data['body']]
                    if data.get('related'):
                        parts += ['', 'Related topics: ' + ', '.join(
                            f'`{ident}`' for ident in data['related'])]
                    if data.get('terms'):
                        parts += ['Glossary terms: ' + ', '.join(
                            f'`{slug}`' for slug in data['terms'])]
                    text = '\n'.join(parts)
                others = [f"{hit['kind']} `{hit['id']}`"
                          for hit in matches[1:]]
                if others:
                    text += '\n\nOther close matches: ' + ', '.join(others)
                return text
        prefix = (f"No help topic or glossary term matches '{query}'.\n\n"
                  if query else '')
        index = self.get_help()
        lines = ['# Anthive help topics', '']
        lines += [f"- `{topic['id']}`: {topic['title']}. {topic['summary']}"
                  for topic in index.get('topics', [])]
        terms = ', '.join(f"`{term['slug']}`"
                          for term in index.get('glossary', []))
        lines += ['', '## Glossary terms', '', terms or '(none)']
        return prefix + '\n'.join(lines)

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
        ``level`` is ``'brief'`` or ``'full'``. REST API 2.44.0+ adds
        ``help_topics`` (``dataset-caveats``, ``facet-replicate-unit``,
        ``per-cell-per-donor``, ``dataset-versions``) for
        :meth:`get_help_topic`."""
        return self._get(f'/databases/{db_id}/context',
                         params={'level': level,
                                 'budget_tokens': budget_tokens})

    def get_library_status(self) -> Dict:
        """``GET /library/status``. Library and literature-index health:
        counts, issues, embedding model, and ``upload_index`` (state of
        the last upload's background index update).

        Everybody gets ``papers``, ``linked_datasets``, ``index``
        (without ``path``), ``issues`` and, from REST API 2.44.0,
        ``n_errors`` and ``n_warnings`` (counts of the issues).
        Administrators additionally get the issue texts
        (``issues.errors`` and ``issues.warnings``),
        ``unresolved_references`` (``papers:`` links to citekeys the
        site's library does not hold), ``library_dir`` and
        ``index.path``. For everybody else (and on sites without login,
        where nobody is an administrator) ``unresolved_references``,
        ``library_dir`` and ``index.path`` are absent and the issue
        lists are empty, so read ``n_errors`` / ``n_warnings`` for the
        counts."""
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

    # ── Dataset deposits, read-only (REST API 2.46.0+) ──────────────────
    #
    # Datasets are deposited with the Go CLI: ``vark datasets upload``
    # (presigned S3 uploads, complete, withdraw). pyvark only reads the
    # state of deposits. Every route answers 403 ``forbidden`` to callers
    # who are not signed-in members of the site's
    # ``datasets_upload_groups`` (default ``admins``, ``curators``), and
    # 503 ``disabled`` where deposits are switched off. Errors raise
    # :class:`DatasetDepositError` (``status`` holds the status word).

    def _deposit_get(self, path: str,
                     params: Optional[Dict[str, Any]] = None) -> Dict:
        """GET a deposit route; non-2xx raises DatasetDepositError."""
        response = self._request('GET', path, params=params)
        raise_for_deposit_status(response)
        return response.json()

    def list_deposits(self, all: bool = False) -> Dict:
        """``GET /datasets/deposit``. Your dataset deposits (REST API
        2.46.0+). Uploading is done with ``vark datasets upload``, not
        pyvark.

        Returns ``{deposits, all}``; each deposit is the summary of
        :meth:`get_deposit` (``deposit_id``, ``data_project``,
        ``basename``, ``state``, ``deposit_kind``, ``db_id``,
        ``version``, ``kind``, ``held_reasons``, ``update_of``,
        ``continues``, ``total_bytes``, ``created``, ``updated``,
        ``completed``, ``expires``; admins also ``user``).

        Args:
            all: ``True`` lists every user's deposits (admins only;
                :meth:`get_deposit_vocab` says ``caller.may_list_all_deposits``).
                Sent only when ``True``.

        Raises:
            DatasetDepositError: 403 ``forbidden`` for callers outside
                ``datasets_upload_groups`` (or ``all=True`` for a
                non-admin), 503 ``disabled``.
        """
        return self._deposit_get('/datasets/deposit',
                                 {'all': 'true'} if all else None)

    def get_deposit(self, deposit_id: str) -> Dict:
        """``GET /datasets/deposit/{deposit_id}``. Status of one deposit
        (REST API 2.46.0+).

        The summary of :meth:`list_deposits` plus ``error``,
        ``warnings``, ``files`` (``{name, kind, key, size, sha256,
        method, state}``; ``method`` ``reuse`` for published files a
        metadata update reuses), ``watch`` (``{state, served,
        catalog_verified, incoming_deleted}`` or ``None``), ``report``
        (``{result, publish, held_reasons, counts, problems: [{id,
        status, summary, fix}]}`` or ``None``), ``report_available``,
        ``events`` (``{time, action, actor, detail}``), ``approval``
        and, while ``completing``, ``progress``; a failed background
        complete shows ``complete_error``.

        ``state`` is ``uploading``, ``completing``, ``uploaded``
        (complete, not yet seen by the watcher), ``seen``, ``stable``,
        ``checking``, ``importing`` (DE tables), ``live``, ``held``,
        ``failed``, ``needs_decision``, ``rejected``, ``withdrawn``,
        ``expired`` or ``superseded``. ``deposit_kind`` is ``dataset``,
        ``de_only`` or ``metadata_update``; ``kind`` is what the watcher
        made of it (``new``, ``update_patch``, ...).

        Raises:
            DatasetDepositError: 404 ``not_found`` (also for another
                user's deposit), 403 ``forbidden``.
        """
        return self._deposit_get(f'/datasets/deposit/{deposit_id}')

    def get_deposit_report(self, deposit_id: str) -> Dict:
        """``GET /datasets/deposit/{deposit_id}/report``. The redacted
        ingest check report of a deposit (schema
        ``anthive.ingest_check/1``, curator audience, no server paths;
        REST API 2.46.0+).

        Raises:
            DatasetDepositError: 404 ``no_report`` before the check ran
                (the body then carries ``state`` and ``reason``), 404
                ``not_found``, 403 ``forbidden``.
        """
        return self._deposit_get(f'/datasets/deposit/{deposit_id}/report')

    def get_deposit_vocab(self) -> Dict:
        """``GET /datasets/deposit/vocab``. Everything a depositor needs
        to prepare a deposit (REST API 2.46.0+): ``uploads_enabled``,
        ``limits``, ``naming``, ``file_kinds``, ``prefix``, ``mus``
        (required and recommended sidecar fields, the vocabulary,
        ``field_info``), ``sidecar_keys``, ``seed_md`` and ``index_md``
        (with templates and ``required_spec``), ``facets``,
        ``de_presets``, ``de_sidecar_keys``, ``held_reasons``,
        ``update_policy``, ``states`` and ``caller`` (``{user,
        is_admin, may_replace_de_study, may_list_all_deposits,
        may_update_any_dataset}``).

        Raises:
            DatasetDepositError: 403 ``forbidden`` for callers outside
                ``datasets_upload_groups``.
        """
        return self._deposit_get('/datasets/deposit/vocab')

    def get_deposit_group(self, data_project: str,
                          basename: Optional[str] = None) -> Dict:
        """``GET /datasets/deposit/groups/{data_project}``. Whether you
        may deposit into a group, and update a dataset in it (REST API
        2.46.0+).

        Returns ``{data_project, exists, group_id?, may_create?,
        reason?, dataset_exists?, may_update?, db_id?, inactive?}``.
        ``db_id`` is given only when you may deposit a new version;
        ``may_create`` / ``reason`` for a group that does not exist yet.
        A retired dataset is never offered to non-admins.

        Args:
            data_project: Group (collection folder) name.
            basename: A dataset basename in that group. Sent only when
                given.

        Raises:
            DatasetDepositError: 429 ``rate_limited`` (per user), 403
                ``forbidden``.
        """
        return self._deposit_get(
            f'/datasets/deposit/groups/{data_project}',
            {'basename': basename} if basename is not None else None)

    # ── Plot cache admin ────────────────────────────────────────────────

    def get_plot_cache_stats(self) -> Dict:
        """``GET /admin/plot-cache/stats``.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
        return self._get('/admin/plot-cache/stats')

    def clear_plot_cache(self) -> Dict:
        """``POST /admin/plot-cache/clear``.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
        return self._post('/admin/plot-cache/clear')

    # ── Skill (MCP collaborator install path) ───────────────────────────

    def get_skill(self) -> str:
        """``GET /skill``. Fetch the anthive Claude Code SKILL.md."""
        return self._get_raw('/skill').text

    # ── Admin ───────────────────────────────────────────────────────────

    def rescan_databases(self) -> Dict:
        """``POST /admin/rescan``. Re-scan data folder + reload caches.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
        return self._post('/admin/rescan')

    def admin_release(self, db_id: str,
                      duration_seconds: Optional[float] = None,
                      drain_timeout_seconds: Optional[float] = None) -> Dict:
        """``POST /admin/release/{db_id}``. Drop ant-serve's r/o handle so
        a writer can open the duckdb r/w (server defaults: 300 s hold,
        30 s drain).

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
        params = {key: value for key, value in
                  (('duration_seconds', duration_seconds),
                   ('drain_timeout_seconds', drain_timeout_seconds))
                  if value is not None}
        response = self._request('POST', f'/admin/release/{db_id}',
                                 params=params or None)
        response.raise_for_status()
        return response.json()

    def admin_restore(self, db_id: str) -> Dict:
        """``POST /admin/restore/{db_id}``. Inverse of admin_release.

        Admin only (REST API 2.45.0+): needs an admin account, a
        host-local call or the internal secret; other signed-in users
        get HTTP 403.
        """
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
