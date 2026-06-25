"""System-prompt builder for the chat assistant.

Built in three layers:

  1. **Static preamble** — role, persona, action protocol, when to call
     each tool. Lives entirely in this module; rebuilt is cheap.
  2. **Route map** — URL patterns the assistant can ``navigate`` to.
     Hand-curated from the actual FastAPI routes; updated when web
     ships a new page type.
  3. **Catalog digest** — compact ``one-line-per-dataset`` summary of
     the current ``/databases`` content (db_id, organism, cell_type,
     n_cells, title). The LLM doesn't have to call ``list_datasets``
     just to know what's in the catalog.

Per-turn context (the current URL the user is looking at) is added by
the agent loop, not here — it's a USER message envelope, not part of
the system prompt.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


PREAMBLE = """\
You are the in-app assistant for **anthive**, a single-cell RNA-seq data \
browser. You help the user explore the catalogue, find datasets, look up \
gene expression, and navigate the web UI.

## How you act

You have two kinds of tools:

  - **Client actions** — `navigate(url)` and `set_plot_param(params)`. When you \
    call these the user's browser is redirected (or the plot UI updated). Use \
    these to take the user somewhere they asked you to go. Prefer building a \
    plot URL with `build_plot_url(...)` and chaining it through `navigate(...)` \
    over giving the user a long URL to copy. After a `navigate`, the page \
    will reload — keep any text reply you want the user to see SHORT and \
    write it BEFORE calling navigate.

  - **Server data tools** — `list_datasets`, `get_dataset_info`, `gene_stats`, \
    `list_de_studies`, `build_plot_url`. These query the anthive REST API \
    and feed the result back to you. Call them when you need data you don't \
    already have from the system prompt.

## Style

  - Be concise. The user has the page right next to your reply.
  - When unsure between two datasets, list both and let the user pick.
  - When the user asks about a gene without specifying a dataset, use \
    `gene_stats` with the appropriate organism + cell_type filter (the \
    user's current path is in the per-turn context — extract it from there).
  - When a gene query returns a gene-not-found result for some datasets, \
    say so briefly — it's normal for the catalogue to span organisms.
  - Use `scaled_mean` (0-1, cross-dataset-comparable) when ranking the \
    catalogue by abundance, NOT raw `mean` (which is biased by library size).
  - DON'T hallucinate file_ids or dataset names; if you can't find one, \
    say so and offer to list candidates.
"""


# Route map — kept small and current. Update when web adds a route type.
ROUTE_MAP = """\
## Routes you can `navigate` to

ALL user-facing routes start with `/`. The `/api/...` and `/databases/...` \
paths are the REST API surface — DO NOT navigate the user there (they'll \
get a 404 or a JSON blob, not a page).

  - `/`                                       — landing page (catalogue browser)
  - `/about`                                  — about / help
  - `/browse/`                                — dataset list
  - `/browse/<db_id>`                         — dataset detail page (e.g. `/browse/S25/V6W`)
  - `/browse/<db_id>/plot/<geom>?...`         — interactive plot. Geoms: \
scatter, violin, box, bar, histogram, ecdf, kde, hexbin, kde2d, heatmap, \
rolling, volcano, ma, forest, de_heatmap, de_quadrant
  - `/genes/<gene>`                           — single-gene atlas page (cross-catalogue ranking)
  - `/genes/<gene1>,<gene2>,...`              — multi-gene heatmap atlas

When in doubt: call `build_plot_url(...)` and hand its result to \
`navigate` — never hand-build the URL.
"""


def build_catalog_digest(databases: Iterable[Dict[str, Any]],
                        *, max_rows: int = 200) -> str:
    """Compact one-line-per-dataset summary, ~12-15 KB for a 100-dataset catalog.

    Format:
      db_id  organism  cell_type  n_cells  title
    Truncated to ``max_rows`` so the system prompt stays bounded — the LLM
    can fall back to ``list_datasets`` when it needs more.
    """
    rows: List[str] = []
    total_seen = 0
    for d in databases:
        total_seen += 1
        if len(rows) >= max_rows:
            continue
        db_id = str(d.get('db_id') or d.get('id') or '?')
        organism = str(d.get('organism') or '?')
        cell_type = str(d.get('cell_type') or '?')
        n_cells = d.get('n_cells') or 0
        title = str(d.get('title') or '')[:60]
        rows.append(f"  {db_id:<14}  {organism:<10}  {cell_type:<22}  {n_cells:>8}  {title}")

    body = "\n".join(rows) if rows else "  (catalogue empty)"
    note = ""
    if total_seen > len(rows):
        note = (f"\n  … and more ({len(rows)} of {total_seen} shown — use "
                f"`list_datasets` for the rest).")
    return (
        "## Current catalogue digest\n"
        "\n"
        "Columns: db_id, organism, cell_type, n_cells, title\n"
        "\n"
        f"{body}{note}\n"
    )


def build_system_prompt(databases: Iterable[Dict[str, Any]]) -> str:
    """Assemble the full system message for one chat turn.

    Cheap — ~12 KB string built per request. Cache externally if it
    ever shows up in profiling.
    """
    return f"{PREAMBLE}\n{ROUTE_MAP}\n{build_catalog_digest(databases)}"


def build_user_envelope(message: str, context: Dict[str, Any]) -> str:
    """Wrap the user's message with per-turn context (URL they're on).

    The web client sends ``context.path`` per pika #217 §9.3. We put it
    as a small prefix so the model knows where the user is looking without
    treating it as a free-text instruction.
    """
    parts = [message.strip()]
    path = (context or {}).get('path')
    if path:
        parts.append(f"\n\n(user is currently at: {path})")
    return "".join(parts)
