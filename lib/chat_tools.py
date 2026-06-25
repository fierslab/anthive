"""Tool definitions + dispatch for the chat assistant.

Two classes of tools, the agent loop branches on the name set:

  - **Client actions** (``CLIENT_ACTION_TOOLS``): the LLM "calls" them
    but the server never executes — instead it emits an SSE ``action``
    frame the web client consumes (e.g. ``navigate``,
    ``set_plot_param``). The tool's "result" fed back to the LLM is a
    trivial ``{"queued": true}`` so the model knows it acted.
  - **Server data tools**: the server executes by self-calling the
    existing REST API via httpx (keeps the tools symmetric with what
    the LLM "thinks" it's doing — easier audit + debug). Result is
    JSON-serialised and handed to the LLM for the next turn.

NO raw SQL / DuckDB access from tools — locked per web's pika #217.
Tools only call the public ``/api/...`` surface.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import httpx


# ── Client actions — server NEVER executes, emits SSE action frame ──────────

CLIENT_ACTION_TOOLS: Set[str] = {'navigate', 'set_plot_param'}


# ── OpenAI-style tool schemas ────────────────────────────────────────────────
#
# Same JSON shape OpenAI / OpenRouter / Anthropic-via-OpenAI all accept.
# Schemas kept minimal — only the fields the agent actually needs the LLM to
# reason about. Optional params get ``"type": "string"`` without ``required``.

TOOL_DEFS: List[Dict[str, Any]] = [
    # ── Client actions ──
    {
        'type': 'function',
        'function': {
            'name': 'navigate',
            'description': (
                "Send the user's browser to another URL on this anthive "
                "deployment. Use ONLY for routes you know exist (the route "
                "map is in the system prompt). The client waits ~400 ms "
                "after receiving this action so the assistant's reply fully "
                "renders before the page changes."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                        'description': 'Absolute path on this deployment, '
                                       'e.g. "/databases/S25/V6W/plot/scatter?color=APOE_SNP".',
                    },
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_plot_param',
            'description': (
                "Update a plot parameter on the current plot page. The web "
                "client's hook for this isn't fully wired yet (locked per "
                "pika #217); prefer `navigate` with a fully-built plot URL "
                "until this lights up."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'params': {
                        'type': 'object',
                        'description': 'Map of plot-param-name → value '
                                       '(e.g. {"color": "APOE_SNP", "log_x": true}).',
                    },
                },
                'required': ['params'],
            },
        },
    },
    # ── Server data tools ──
    {
        'type': 'function',
        'function': {
            'name': 'list_datasets',
            'description': (
                "List every database in the anthive catalog. Returns each "
                "entry's db_id, title, organism, cell_type and n_cells. "
                "Use this only when the system-prompt digest is insufficient "
                "(it's the same shape, just full instead of truncated)."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'organism': {
                        'type': 'string',
                        'description': 'Optional filter (mouse / human / …).',
                    },
                    'cell_type': {
                        'type': 'string',
                        'description': 'Optional mus-vocab cell_type filter '
                                       '(e.g. "microglial cell").',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_dataset_info',
            'description': (
                "Full info for one dataset: obs columns (categorical + "
                "numerical), embedding names, layers, n_cells, n_genes."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {
                        'type': 'string',
                        'description': 'Dataset id, e.g. "S25/V6W".',
                    },
                },
                'required': ['db_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'gene_stats',
            'description': (
                "Cross-catalog gene expression statistics for one or more "
                "genes. Returns per-(db, gene) mean, scaled_mean (0-1 "
                "cross-dataset-comparable), rank_mean, pct_nonzero, "
                "mean_nonzero. Use scaled_mean when ranking the catalog "
                "by abundance — raw mean isn't comparable across datasets."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'genes': {
                        'type': 'string',
                        'description': 'Comma-separated gene names, '
                                       'e.g. "APOE,TREM2".',
                    },
                    'organism': {
                        'type': 'string',
                        'description': 'Optional filter.',
                    },
                    'cell_type': {
                        'type': 'string',
                        'description': 'Optional mus-vocab cell_type filter.',
                    },
                },
                'required': ['genes'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_de_studies',
            'description': (
                "List differential-expression studies registered against one "
                "dataset. Returns study_id, method, n_contrasts, terms."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {
                        'type': 'string',
                        'description': 'Dataset id, e.g. "S25/V6W".',
                    },
                },
                'required': ['db_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'build_plot_url',
            'description': (
                "Construct (don't render) a plot URL for the user. Use when "
                "you want to give the user a clickable link, or chain into "
                "`navigate` to auto-redirect. The URL takes the form "
                "/databases/<db_id>/plot/<geom>?<params>. Common geoms: "
                "scatter, violin, hexbin, kde2d, histogram, heatmap, volcano, "
                "ma, forest, de_quadrant, de_heatmap. Optional params depend "
                "on the geom — see /api/version for the contract."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {'type': 'string'},
                    'geom': {'type': 'string'},
                    'params': {
                        'type': 'object',
                        'description': 'Geom-specific query params, e.g. '
                                       '{"color": "APOE_SNP", "x": "X_umap"}.',
                    },
                },
                'required': ['db_id', 'geom'],
            },
        },
    },
]


# ── Server-tool dispatch ────────────────────────────────────────────────────


class ToolContext:
    """Carry per-request state into tool handlers.

    ``base_url`` is the internal anthive REST root (typically
    ``http://localhost:8080/api`` — the same host:port the server is
    listening on, so the tool's HTTP call doesn't traverse Caddy +
    Authelia). ``auth`` may be ``None`` (when bound to localhost the
    REST API can be reached directly, no auth required). The agent
    passes its async HTTP client in via ``client`` to share connections.
    """

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        auth: Optional[tuple] = None,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.client = client
        self.auth = auth


async def dispatch_server_tool(
    name: str, args: Dict[str, Any], ctx: ToolContext,
) -> Dict[str, Any]:
    """Run one server-side data tool and return its result as a dict.

    Raises ``ValueError`` on unknown tool name. Errors from the
    underlying REST call (4xx / 5xx / timeout) are caught and wrapped
    in ``{"error": "..."}`` so the LLM can recover gracefully.
    """
    handlers = {
        'list_datasets':    _list_datasets,
        'get_dataset_info': _get_dataset_info,
        'gene_stats':       _gene_stats,
        'list_de_studies':  _list_de_studies,
        'build_plot_url':   _build_plot_url,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"unknown server tool: {name!r}")
    try:
        return await handler(ctx, **args)
    except httpx.HTTPError as exc:
        return {'error': f'tool {name!r} failed: {exc}'}
    except TypeError as exc:
        # Wrong arg shape from the LLM — return an actionable error.
        return {'error': f'tool {name!r} called with invalid args: {exc}'}


# ── Individual server-tool implementations ──────────────────────────────────


async def _list_datasets(
    ctx: ToolContext,
    organism: Optional[str] = None,
    cell_type: Optional[str] = None,
) -> Dict[str, Any]:
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases',
        params={'per_page': 500},
        auth=ctx.auth,
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    dbs = data if isinstance(data, list) else data.get('databases', [])

    # Apply filters client-side (cheap, ≤200 datasets).
    if organism:
        dbs = [d for d in dbs if str(d.get('organism', '')).lower() == organism.lower()]
    if cell_type:
        dbs = [d for d in dbs if str(d.get('cell_type', '')).lower() == cell_type.lower()]

    # Trim to the fields the model needs — keeps the tool result small.
    trimmed = [
        {
            'db_id':     d.get('db_id') or d.get('id'),
            'title':     d.get('title'),
            'organism':  d.get('organism'),
            'cell_type': d.get('cell_type'),
            'n_cells':   d.get('n_cells'),
        }
        for d in dbs
    ]
    return {'n': len(trimmed), 'datasets': trimmed}


async def _get_dataset_info(ctx: ToolContext, db_id: str) -> Dict[str, Any]:
    quoted = db_id.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted}/info',
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _gene_stats(
    ctx: ToolContext,
    genes: str,
    organism: Optional[str] = None,
    cell_type: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {'genes': genes}
    if organism:
        params['organism'] = organism
    if cell_type:
        params['cell_type'] = cell_type
    resp = await ctx.client.get(
        f'{ctx.base_url}/gene_stats_all',
        params=params, auth=ctx.auth, timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Strip noisy fields the model doesn't need to reason about.
    trimmed_results = []
    for entry in data.get('results', []):
        stats = [
            {k: v for k, v in s.items()
             if k in ('gene', 'mean', 'scaled_mean', 'rank_mean',
                      'pct_nonzero', 'mean_nonzero', 'cross_db_cv',
                      'gene_found', 'error')}
            for s in entry.get('stats', [])
        ]
        trimmed_results.append({
            'db_id':   entry.get('db_id'),
            'title':   entry.get('title'),
            'n_cells': entry.get('n_cells'),
            'stats':   stats,
        })
    return {
        'genes':       data.get('genes'),
        'n_databases': data.get('n_databases'),
        'results':     trimmed_results,
    }


async def _list_de_studies(ctx: ToolContext, db_id: str) -> Dict[str, Any]:
    quoted = db_id.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted}/de',
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _build_plot_url(
    ctx: ToolContext,
    db_id: str,
    geom: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Pure URL construction — no HTTP call needed. We URL-encode the params
    # via httpx's params machinery to get correct quoting.
    quoted_db = db_id.replace('/', '%2F')
    url = f'/databases/{quoted_db}/plot/{geom}'
    if params:
        # httpx.QueryParams handles all the encoding correctly.
        from urllib.parse import urlencode
        url = f'{url}?{urlencode(params, doseq=True)}'
    return {'url': url}
