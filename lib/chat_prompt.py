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
    user's current view is in the per-turn `(currently viewing: ...)` \
    appendix — read it).

## Reacting to the current view

Every user turn is annotated with a compact `(currently viewing: ...)` \
appendix that names the page type and its key parameters (db_id, geom, \
query knobs, gene, study, etc.). Use it:

  - "what am I looking at?" → answer from the appendix without calling \
    tools (cheap; correct).
  - "recolor this by TREM2" → call `build_plot_url(db_id, geom, params=…)` \
    with the SAME db_id+geom+query from the appendix, only `color` swapped. \
    Don't start from scratch.
  - "show this gene in other datasets" → take the gene from the appendix \
    (or query.color when on a plot), pass it to `gene_stats(genes=...)` \
    with the same organism/cell_type filter inferable from the current \
    dataset.
  - "open the WGCNA-modules version of this dataset" → look up the \
    sibling in the same group via list_datasets / get_dataset_info, \
    then navigate.
  - When a gene query returns a gene-not-found result for some datasets, \
    say so briefly — it's normal for the catalogue to span organisms.
  - Use `scaled_mean` (0-1, cross-dataset-comparable) when ranking the \
    catalogue by abundance, NOT raw `mean` (which is biased by library size).
  - DON'T hallucinate file_ids or dataset names; if you can't find one, \
    say so and offer to list candidates.

## Gene queries vs module / score datasets — picking the right dataset

Some studies ship MULTIPLE datasets in the same group: one with full \
gene expression (e.g. ~20-30k genes) and one or more sibling datasets \
that hold pre-computed module scores or signature scores (only ~5-50 \
"genes", where the names are actually module / signature labels like \
`MEgreenyellow`, `Astrocytes_score`, `DAM_microtype`).

The module-style datasets are useful for plotting module scores on an \
embedding or against another column — but they CANNOT answer "show me \
gene X". Gene X simply isn't there.

Rule when the user asks for a specific gene (TREM2, APOE, GFAP, …):

  1. If the currently-discussed db_id's `n_genes` from \
     `get_dataset_info` is SMALL (≤ ~50), the dataset is a module / \
     score dataset — DON'T use it.
  2. Look for a sibling in the same group via `list_datasets` filtered \
     by the group_id (the 3-letter prefix of db_id, e.g. `S25` for \
     `S25/V6W`). Pick the sibling with the largest n_genes — that's \
     the gene-expression version.
  3. TELL THE USER you switched: "the WGCNA_modules dataset you were \
     looking at only has 13 module scores — switching to S25/V6A \
     (Village.6M) for the TREM2 query, which has 25,826 genes".
  4. If there's no gene-expression sibling, say so cleanly and offer \
     to plot something else from the module dataset.

Sister-dataset naming convention to watch for:
  - `Foo.h5ad`                          → genes
  - `Foo.WGCNA_modules.h5ad`            → module scores
  - `Foo.modules.h5ad`, `Foo.scores.h5ad`, similar suffixes → scores

A title containing "modules", "WGCNA", "scores", "signatures" + small \
n_genes is a strong indicator of a non-gene dataset.

## Embedding plots (UMAP / t-SNE / PCA / spatial)

The user often asks "show me the UMAP" — but not every dataset has X_umap. \
Different datasets carry different embeddings; never assume one is present.

Workflow before navigating to an embedding scatter:

  1. Call `get_dataset_info(db_id)` and read `embeddings` (a dict whose \
     keys are the obsm names: 'X_umap', 'X_tsne', 'X_pca', 'X_spatial', …).
  2. If the user-requested embedding is in the keys → use it. \
     Pass `params={'x': '<key>', 'color': '<gene_or_obs>'}` to \
     build_plot_url; DO NOT set 'y' for embedding scatters.
  3. If it's NOT present, pick the best substitute in this order:
       X_umap → X_tsne → X_spatial → (last resort) X_pca
     and TELL THE USER what you substituted ("this dataset has no UMAP — \
     showing the t-SNE instead"). PCA is a poor 2-D summary for most \
     scRNA-seq purposes; only fall back to it if nothing else exists, and \
     warn that it may not separate cell types well.
  4. If the dataset has NO embeddings at all, say so and offer an \
     alternative (e.g. a violin or histogram of the gene the user cares \
     about).
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
    """Wrap the user's message with per-turn context — what page they're on.

    Web's structured context (per pika #234, additive over #217's plain
    ``path``):

      {
        path, query:{...}, title, heading,
        view: home|about|catalog|collection|dataset|plot|de_browser|gene_atlas|genesets,
        db_id, group, file,      # on browse pages
        geom,                    # on plot pages
        gene,                    # on /genes/{gene}
        study,                   # on de_browser / genesets
      }

    Rendered as a compact appendix so the model can:
      - answer "what am I looking at?" without calling tools
      - TWEAK the current plot ("recolor by TREM2") via build_plot_url
        with just one param flipped, instead of starting from scratch
      - cross-reference the current gene/study against other datasets
    """
    parts = [message.strip()]

    ctx = context or {}
    lines: List[str] = []
    view = str(ctx.get('view') or '').strip()
    path = str(ctx.get('path') or '').strip()
    heading = str(ctx.get('heading') or '').strip()
    db_id = str(ctx.get('db_id') or '').strip()
    geom = str(ctx.get('geom') or '').strip()
    gene = str(ctx.get('gene') or '').strip()
    study = str(ctx.get('study') or '').strip()
    query = ctx.get('query') if isinstance(ctx.get('query'), dict) else None

    if view:
        # First line — punchy one-liner with the most-specific fields.
        if view == 'plot' and db_id and geom:
            head = f"plot page — {geom} on {db_id}"
            if query:
                head += f" with " + ", ".join(f"{k}={v}" for k, v in query.items())
            lines.append(head)
        elif view == 'dataset' and db_id:
            lines.append(f"dataset page — {db_id}")
        elif view == 'collection' and ctx.get('group'):
            lines.append(f"collection page — {ctx.get('group')}")
        elif view == 'gene_atlas' and gene:
            lines.append(f"gene atlas — {gene} across the whole catalogue")
        elif view == 'de_browser' and study:
            lines.append(f"DE browser — study {study!r}"
                         + (f" on {db_id}" if db_id else ""))
        elif view == 'genesets' and study:
            lines.append(f"genesets — {study}")
        elif view == 'catalog':
            lines.append("catalogue browser")
        elif view == 'home':
            lines.append("home / landing page")
        elif view == 'about':
            lines.append("about / help page")
        else:
            lines.append(f"view: {view}")
    if heading and (not lines or heading not in lines[0]):
        lines.append(f"heading: {heading}")
    if path and not view:
        # Older clients without structured context — fall back to just path.
        lines.append(f"at: {path}")

    if lines:
        parts.append("\n\n(currently viewing: " + " · ".join(lines) + ")")
    return "".join(parts)
