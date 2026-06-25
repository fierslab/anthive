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
#
# ``set_plot_param`` USED to live here but was removed: the browser-side
# hook (``window.__antApplyPlotParam``) was never wired in plot.html, so
# the tool's "navigate"-equivalent fallback was the only thing that ever
# happened. Re-add when web ships the hook (tracked in pika to
# anthive-web).
CLIENT_ACTION_TOOLS: Set[str] = {'navigate'}


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
                "STEP 1 of DE-quadrant drilldown: list differential-expression "
                "studies registered against a dataset. Returns study_id + method "
                "+ count of terms and contrasts. To get the actual TERM names, "
                "follow up with get_de_study_terms(db_id, study_id)."
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
            'name': 'get_de_study_terms',
            'description': (
                "STEP 2 of DE-quadrant drilldown: list the TERM names of one "
                "DE study (e.g. 'sex_mouse', 'Sex_donor', 'patient_geno_short', "
                "'batch_group', …). Each term has its own set of contrasts; "
                "follow up with list_de_contrasts(db_id, study_id, term) to "
                "get the contrast names."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id':    {'type': 'string', 'description': 'Dataset id.'},
                    'study_id': {'type': 'string', 'description': 'Study id from list_de_studies.'},
                },
                'required': ['db_id', 'study_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_de_contrasts',
            'description': (
                "STEP 3 of DE-quadrant drilldown: list the CONTRAST names "
                "within one (study, term). Each contrast is one comparison "
                "(often a single level, e.g. term='Sex_donor' → contrasts=['F'] "
                "means 'F vs reference'). REQUIRED before building a de_quadrant "
                "URL — contrast names look like single levels ('F', 'm', "
                "'APOE4_vs_APOE3'), NOT term+level concatenations ('Sex_donor_M'). "
                "If you guess, the de_quadrant page renders empty."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id':    {'type': 'string'},
                    'study_id': {'type': 'string'},
                    'term':     {'type': 'string', 'description': 'Term name from get_de_study_terms.'},
                },
                'required': ['db_id', 'study_id', 'term'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'module_score',
            'description': (
                "Compute Seurat-style per-cell module scores on the fly for an "
                "arbitrary gene list against the dataset's log-normalized layer. "
                "Score = mean(module genes) − mean(expression-matched control genes); "
                "controls drawn from the same expression bins via __gene_bins.\n"
                "\n"
                "Returns summary stats {mean, std, q25, median, q75, min, max}, "
                "n_cells, genes_found, genes_missing, and a session_id that can be "
                "passed to subsequent analytics queries / visualisations.\n"
                "\n"
                "Useful for: 'how does the DAM signature distribute in this dataset?', "
                "'score these microglia activation genes across the cells', etc. "
                "Requires __gene_bins on the target dataset (only some datasets have "
                "it precomputed today — 400 means the dataset can't run module_score "
                "yet)."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {
                        'type': 'string',
                        'description': "Dataset id, e.g. 'S25/V6W'.",
                    },
                    'genes': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': "Gene names to score (e.g. ['APOE','TREM2','CST7','GPNMB']).",
                    },
                    'n_control': {
                        'type': 'integer',
                        'description': 'Control genes sampled per expression bin (default 100).',
                    },
                },
                'required': ['db_id', 'genes'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_genes',
            'description': (
                "Substring-match gene names in one dataset. Useful when the "
                "user types a partial name ('apo', 'trem') or you're unsure "
                "of the exact capitalization. Returns up to `limit` matches. "
                "Mouse genes are lower-case (Apoe, Trem2); human are upper "
                "(APOE, TREM2) — this tool matches case-insensitively by "
                "default."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {'type': 'string', 'description': 'Dataset id, e.g. "S25/V6W".'},
                    'q':     {'type': 'string', 'description': 'Substring or prefix.'},
                    'limit': {'type': 'integer', 'description': 'Max results (default 20).'},
                },
                'required': ['db_id', 'q'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'analytics_schema',
            'description': (
                "Return the DuckDB tables in a dataset — every table's "
                "columns, types, row count, and a 3-row sample. ALWAYS call "
                "this before writing an `analytics_query` SQL statement, "
                "because the obs columns vary per dataset and you need the "
                "EXACT column names + types.\n"
                "\n"
                "Tables you'll typically see:\n"
                "  - `obscat`  — categorical obs columns (varchar)\n"
                "  - `obsnum`  — numerical obs columns + module scores\n"
                "  - `__genes`, `__cells` — gene/cell lookup tables\n"
                "  - `layer__<name>` — sparse expression views (cell_id, gene_id, value)"
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {'type': 'string'},
                },
                'required': ['db_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'analytics_query',
            'description': (
                "Run a SELECT-only DuckDB query against the dataset. THE most "
                "powerful tool — use it when no other tool answers the "
                "question. Examples of questions only this can answer:\n"
                "  • 'what % of cells in cluster M3 express APOE > 2?'\n"
                "  • 'median DAM_score by cell type, ranked by sample'\n"
                "  • 'distribution of cell counts per donor'\n"
                "  • 'cells where Disease == AD AND CD68 > 1 AND TREM2 < 0.5'\n"
                "\n"
                "WORKFLOW:\n"
                "  1. Call `analytics_schema(db_id)` first to learn the exact "
                "     column names + types in `obscat` and `obsnum`.\n"
                "  2. Write a focused SQL. The result is capped at 500 000 "
                "     rows and 120s; aggregate when you can.\n"
                "  3. To query module scores you just computed via "
                "     `module_score`, pass its session_id in `attach_sessions` "
                "     — the scores arrive as a table called `scores` with "
                "     columns (cell_name, module_score). Join on cell_name.\n"
                "\n"
                "ONLY a small preview (first 10 rows + column metadata) is "
                "returned to you — DON'T expect the full 500k. The query "
                "result is persisted server-side; the `session_id` it "
                "returns can be chained into further queries or visualised."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'db_id': {'type': 'string'},
                    'sql':   {
                        'type': 'string',
                        'description': 'SELECT or WITH … SELECT only. Write/DDL is rejected.',
                    },
                    'attach_sessions': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': "Optional session_ids from prior module_score / analytics_query calls. The first attached session becomes table `scores`.",
                    },
                },
                'required': ['db_id', 'sql'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_genesets',
            'description': (
                "Browse the geneset catalogue — named signatures (WGCNA "
                "modules, DAM markers, IFN signatures, …) curated as "
                "{study, experiment, name} triples. Each entry includes "
                "`scored_in`: a list of db_ids where the signature is "
                "already a pre-scored obs column (no on-the-fly module_score "
                "needed). Filter to relevant ones."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'organism': {'type': 'string', 'description': 'Optional filter.'},
                    'method':   {'type': 'string', 'description': 'Optional method filter (WGCNA, DEG, …).'},
                    'study':    {'type': 'string', 'description': 'Optional study filter.'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_geneset',
            'description': (
                "Get the full gene list of one named geneset, plus its "
                "`scored_in` (datasets where it's pre-scored as an obs "
                "column called `<study>::<experiment>::<name>`). Useful "
                "as input to `module_score` on a dataset that doesn't have "
                "the signature pre-scored yet."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'study':      {'type': 'string'},
                    'experiment': {'type': 'string'},
                    'name':       {'type': 'string'},
                },
                'required': ['study', 'experiment', 'name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'build_plot_url',
            'description': (
                "Construct (don't render) a USER-FACING plot URL. Returns a "
                "URL of the form /browse/<db_id>/plot/<geom>?<params> — the "
                "web UI route, NOT the REST API. Pass the result straight to "
                "`navigate`. Never hand-construct plot URLs.\n"
                "\n"
                "Common geoms: scatter, violin, hexbin, kde2d, histogram, "
                "heatmap, volcano, ma, forest, de_quadrant, de_heatmap.\n"
                "\n"
                "Scatter param shapes (the most common geom):\n"
                "  - Embedding scatter (UMAP, t-SNE, PCA, …): pass\n"
                "      params={'x': '<embedding_key>', 'color': '<gene_or_obs>'}\n"
                "    The embedding key is the full obsm name (e.g. 'X_umap',\n"
                "    'X_tsne', 'X_pca'). Do NOT set 'y' — there is no\n"
                "    'Y_umap' / 'Y_tsne' / etc; the embedding is 2-D and the\n"
                "    server reads both axes from the single key. ALWAYS call\n"
                "    get_dataset_info first to confirm which embedding keys\n"
                "    are actually present — datasets may have X_umap, X_tsne,\n"
                "    X_pca, X_spatial, or a custom name, and only some.\n"
                "  - Gene-vs-gene scatter: params={'x': '<gene>', 'y': '<gene>'}.\n"
                "  - Default (no x/y): the dataset's default embedding."
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
        'list_datasets':      _list_datasets,
        'get_dataset_info':   _get_dataset_info,
        'gene_stats':         _gene_stats,
        'search_genes':       _search_genes,
        'list_de_studies':    _list_de_studies,
        'get_de_study_terms': _get_de_study_terms,
        'list_de_contrasts':  _list_de_contrasts,
        'analytics_schema':   _analytics_schema,
        'analytics_query':    _analytics_query,
        'list_genesets':      _list_genesets,
        'get_geneset':        _get_geneset,
        'build_plot_url':     _build_plot_url,
        'module_score':       _module_score,
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


async def _get_de_study_terms(
    ctx: ToolContext, db_id: str, study_id: str,
) -> Dict[str, Any]:
    """GET /databases/<db>/de/<study> — returns the terms array with names + counts."""
    quoted_db = db_id.replace('/', '%2F')
    quoted_st = study_id.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted_db}/de/{quoted_st}',
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Trim — bot just needs terms[].term + n_contrasts per term.
    return {
        'study_id': data.get('study_id'),
        'method':   data.get('method'),
        'description': data.get('description'),
        'terms':    data.get('terms') or [],
    }


async def _list_de_contrasts(
    ctx: ToolContext, db_id: str, study_id: str, term: str,
) -> Dict[str, Any]:
    """GET /databases/<db>/de/<study>/terms/<term> — returns the contrasts array."""
    quoted_db = db_id.replace('/', '%2F')
    quoted_st = study_id.replace('/', '%2F')
    quoted_tm = term.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted_db}/de/{quoted_st}/terms/{quoted_tm}',
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _search_genes(
    ctx: ToolContext, db_id: str, q: str, limit: int = 20,
) -> Dict[str, Any]:
    """GET /databases/<db>/genes?q=…&limit=… — substring/prefix gene search."""
    quoted = db_id.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted}/genes',
        params={'q': q, 'limit': int(limit)},
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _analytics_schema(ctx: ToolContext, db_id: str) -> Dict[str, Any]:
    """GET /databases/<db>/analytics/schema — table/column inventory."""
    quoted = db_id.replace('/', '%2F')
    resp = await ctx.client.get(
        f'{ctx.base_url}/databases/{quoted}/analytics/schema',
        auth=ctx.auth, timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Trim sample rows to first 2 each to keep the model context bounded —
    # full schemas with 100+ obs columns can blow past 30 KB otherwise.
    tables = data.get('tables') or []
    trimmed_tables = []
    for table in tables:
        sample = table.get('sample') or []
        trimmed_tables.append({
            'name':      table.get('name'),
            'row_count': table.get('row_count'),
            'columns':   table.get('columns'),
            'sample':    sample[:2],
        })
    return {'db_id': data.get('db_id'), 'tables': trimmed_tables}


async def _analytics_query(
    ctx: ToolContext,
    db_id: str,
    sql: str,
    attach_sessions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """POST /databases/<db>/analytics/query — SELECT-only SQL.

    Returns the small preview the REST endpoint produces (first ~10 rows +
    column metadata + session_id). The full result set is persisted
    server-side; the model can chain it via further attach_sessions.
    """
    quoted = db_id.replace('/', '%2F')
    payload: Dict[str, Any] = {'sql': sql}
    if attach_sessions:
        payload['attach_sessions'] = list(attach_sessions)
    try:
        resp = await ctx.client.post(
            f'{ctx.base_url}/databases/{quoted}/analytics/query',
            json=payload, auth=ctx.auth, timeout=150.0,  # >server-side 120s timeout
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get('detail', str(exc))
        except ValueError:
            detail = str(exc)
        return {'error': f'analytics_query failed: {detail}'}
    return resp.json()


async def _list_genesets(
    ctx: ToolContext,
    organism: Optional[str] = None,
    method: Optional[str] = None,
    study: Optional[str] = None,
) -> Dict[str, Any]:
    """GET /genesets — browse the curated geneset registry."""
    params: Dict[str, Any] = {}
    if organism:
        params['organism'] = organism
    if method:
        params['method'] = method
    if study:
        params['study'] = study
    resp = await ctx.client.get(
        f'{ctx.base_url}/genesets', params=params,
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Trim — drop verbose `markdown` bodies if any creep in.
    genesets = data.get('genesets') or []
    trimmed = []
    for geneset in genesets:
        trimmed.append({
            k: v for k, v in geneset.items()
            if k in ('geneset_id', 'study', 'experiment', 'name',
                     'organism', 'method', 'n_genes', 'scored_in')
        })
    return {'count': data.get('count'), 'genesets': trimmed}


async def _get_geneset(
    ctx: ToolContext, study: str, experiment: str, name: str,
) -> Dict[str, Any]:
    """GET /genesets/<study>/<experiment>/<name> — one geneset's gene list."""
    from urllib.parse import quote
    resp = await ctx.client.get(
        f'{ctx.base_url}/genesets/{quote(study)}/{quote(experiment)}/{quote(name)}',
        auth=ctx.auth, timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Keep only the keys the model needs to reason about.
    return {
        k: v for k, v in data.items()
        if k in ('geneset_id', 'study', 'experiment', 'name', 'organism',
                 'method', 'n_genes', 'genes', 'scored_in', 'column_name',
                 'description', 'metadata')
    }


async def _module_score(
    ctx: ToolContext,
    db_id: str,
    genes: List[str],
    n_control: int = 100,
) -> Dict[str, Any]:
    """POST /databases/<db>/module_score → on-the-fly Seurat AddModuleScore.

    Filters the response to what the LLM actually needs (summary + ids +
    found/missing gene lists + session_id). Surfaces the 'requires
    __gene_bins' 400 as a structured error so the model can suggest a
    sibling dataset that has them.
    """
    quoted = db_id.replace('/', '%2F')
    payload = {'genes': list(genes), 'n_control': int(n_control)}
    try:
        resp = await ctx.client.post(
            f'{ctx.base_url}/databases/{quoted}/module_score',
            json=payload, auth=ctx.auth, timeout=180.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # 400 commonly means '__gene_bins missing on this duckdb' — surface
        # that clearly so the model can suggest an alternative dataset.
        try:
            detail = exc.response.json().get('detail', str(exc))
        except ValueError:
            detail = str(exc)
        return {'error': f'module_score on {db_id} failed: {detail}'}
    data = resp.json()
    return {
        'session_id':      data.get('session_id'),
        'db_id':           data.get('db_id'),
        'n_cells':         data.get('n_cells'),
        'genes_found':     data.get('genes_found'),
        'genes_missing':   data.get('genes_missing'),
        'n_control_genes': data.get('n_control_genes'),
        'summary':         data.get('summary'),
    }


async def _build_plot_url(
    ctx: ToolContext,
    db_id: str,
    geom: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Pure URL construction — no HTTP call needed.
    #
    # IMPORTANT: must emit the WEB UI path (/browse/...), NOT the REST API
    # path (/databases/...). The /databases/<db_id>/plot/<geom> form is the
    # ant-serve REST endpoint (returns PNG bytes); the user-facing route
    # at the same path-shape but rooted at /browse/<db_id>/plot/<geom>
    # is what `navigate` should hand to the client. Web sees a 404 if we
    # mix them up — bug surfaced by anthive-web #228.
    #
    # db_id is already "group/file" so it slots straight into the path. We
    # leave the slash unquoted because the web router consumes it as a
    # path parameter, not a single segment.
    url = f'/browse/{db_id}/plot/{geom}'
    if params:
        from urllib.parse import urlencode
        url = f'{url}?{urlencode(params, doseq=True)}'
    return {'url': url}
