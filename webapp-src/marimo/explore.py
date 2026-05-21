"""
Anthive — Interactive Explorer (marimo WASM)

A reactive notebook that runs entirely in the browser via Pyodide.
Picks up `?db_id=…&gene=…&api=…` from the page URL on load, lets the user
override either with a dropdown / text input, and re-renders the metadata
card, gene stats and an expression plot whenever anything changes.

Build:
    uvx marimo export html-wasm webapp-src/marimo/explore.py \
        -o webapp/marimo --mode run

Embed:
    https://data.nevel.eu/marimo/?db_id=P26%2FHMI&gene=TREM2
"""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Anthive Explorer")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # Anthive Explorer

        Browse single-cell datasets and gene expression interactively.
        Everything below runs **in your browser** — no server round-trips except
        to the Anthive REST API. Share a link with the URL bar's query string
        (`?db_id=…&gene=…`) to point colleagues at the same view.
        """
    )
    return


@app.cell
async def _packages(mo):
    """Install the antclient package (and matplotlib for plotting) once."""
    import sys
    if sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib"])
    return


@app.cell
def _(mo):
    """Read query parameters once.  Defaults reflect a sensible starter dataset.

    The API URL defaults to *same origin* as the page so that browser
    credentials (basic-auth, cookies) flow naturally and CORS doesn't apply.
    A `?api=…` query param overrides this for cross-origin / local testing.
    """
    params = mo.query_params()

    # auto-detect: when running in the browser, use the current page's origin.
    auto_api = None
    try:
        import js  # only present under Pyodide / browser
        auto_api = js.location.origin + "/api"
    except Exception:
        auto_api = "http://localhost:8080/api"

    default_api  = params.get("api", auto_api)
    default_db   = params.get("db_id", "P26/HMI")
    default_gene = params.get("gene", "TREM2")
    return default_api, default_db, default_gene, params


@app.cell
def _(default_api, mo):
    """API endpoint input (editable in the UI; URL stays as-is otherwise)."""
    api_input = mo.ui.text(
        value=default_api, label="Anthive API base URL", full_width=True,
    )
    api_input
    return (api_input,)


@app.cell
def _(api_input):
    from antclient import AnthiveClient
    client = AnthiveClient(api_input.value)
    return (client,)


@app.cell
def _(client, mo):
    """List datasets and build the dropdown."""
    try:
        dbs = client.get_databases()
    except Exception as exc:
        mo.stop(True, mo.md(f"❌ Could not reach API: `{exc}`"))
        dbs = []
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    return (dbs, options)


@app.cell
def _(default_db, mo, options):
    db_selector = mo.ui.dropdown(
        options=options,
        value=next((k for k, v in options.items() if v == default_db), None)
              or (next(iter(options)) if options else None),
        label=f"Dataset ({len(options)} available)",
        searchable=True,
    )
    db_selector
    return (db_selector,)


@app.cell
def _(db_selector):
    db_id = db_selector.value
    return (db_id,)


@app.cell
def _(client, db_id, mo):
    """Dataset info card."""
    if not db_id:
        mo.stop(True, mo.md("(pick a dataset above)"))
    info = client.get_database_info(db_id)
    n_cells = info.get("n_cells")
    n_genes = info.get("n_genes")
    layers = list((info.get("layers") or {}).keys())
    obs = info.get("obs", {})
    obs_cat = obs.get("categorical", [])
    obs_num = obs.get("numerical", [])
    description = info.get("description") or info.get("title") or ""
    organism = info.get("organism") or info.get("annotation", {}).get("organism", "?")

    mo.md(
        f"""
        ### `{db_id}` — {info.get('title', '')}
        {description}

        | | |
        |---:|:---|
        | **cells** | {n_cells:,} |
        | **genes** | {n_genes:,} |
        | **organism** | {organism} |
        | **layers** | {', '.join(layers) or '—'} |
        | **categorical obs** | {len(obs_cat)} |
        | **numerical obs** | {len(obs_num)} |
        """
    )
    return info, obs_cat, obs_num


@app.cell
def _(default_gene, mo):
    """Gene name input."""
    gene_input = mo.ui.text(value=default_gene, label="Gene symbol", full_width=True)
    gene_input
    return (gene_input,)


@app.cell
def _(client, db_id, gene_input, mo):
    """Search hits — confirm the gene exists in this dataset before we plot."""
    if not gene_input.value.strip():
        mo.stop(True, mo.md("(enter a gene above)"))
    hits = client.search_genes(db_id, q=gene_input.value, limit=8)
    items = [h.get("gene_name") or h.get("gene") if isinstance(h, dict) else h
             for h in hits]
    mo.md(
        f"**search:** {len(hits)} match"
        + ("es" if len(hits) != 1 else "")
        + f" for `{gene_input.value}` → "
        + (", ".join(f"`{x}`" for x in items[:8]) if items else "(none)")
    )
    return (items,)


@app.cell
def _(client, db_id, gene_input, mo):
    """Gene-level summary stats (no full per-cell download)."""
    gene = gene_input.value.strip()
    if not gene:
        return None,
    try:
        stats = client.gene_stats(db_id, gene)
    except Exception as exc:
        mo.stop(True, mo.md(f"`gene_stats` failed: `{exc}`"))
        stats = None
    return (stats,)


@app.cell
def _(mo, stats):
    if not stats:
        return
    rows = stats if isinstance(stats, list) else [stats]
    if not rows:
        mo.stop(True, mo.md("Gene not found in this dataset."))
    rendered = "\n".join(
        f"- **{r.get('gene_name') or r.get('gene') or '?'}** — "
        f"{r.get('n_nonzero','?'):,} non-zero cells "
        f"(pct={r.get('pct_nonzero', 0):.1%}), "
        f"mean={r.get('mean', 0):.3f}, "
        f"std={r.get('std', 0):.3f}, "
        f"max={r.get('max', 0):.3f}"
        for r in rows
    )
    mo.md(f"### Gene stats\n{rendered}")
    return


@app.cell
def _(client, db_id, gene_input, mo, obs_cat):
    """Pull a manageable cell sample (gene + first categorical obs) for plotting."""
    gene = gene_input.value.strip()
    if not gene:
        return None, None
    color_by = obs_cat[0] if obs_cat else None
    sample_size = 5000
    try:
        cells = client.get_cells(
            db_id=db_id,
            genes=[gene],
            obs=[color_by] if color_by else None,
            limit=sample_size,
        )
    except Exception as exc:
        mo.stop(True, mo.md(f"`get_cells` failed: `{exc}`"))
        cells = None
    return cells, color_by


@app.cell
def _(cells, color_by, gene_input, mo):
    """Render expression histogram with matplotlib."""
    if cells is None or len(cells) == 0:
        mo.stop(True, mo.md("No cells returned."))

    import matplotlib.pyplot as plt
    import pandas as pd

    df = cells if isinstance(cells, pd.DataFrame) else pd.DataFrame(cells)
    gene = gene_input.value.strip()

    if gene not in df.columns:
        mo.stop(True, mo.md(f"Column `{gene}` not in returned cell table."))

    fig, axes = plt.subplots(1, 2 if color_by else 1, figsize=(10, 4), squeeze=False)
    ax = axes[0, 0]
    ax.hist(df[gene].astype(float), bins=60, color="#4c72b0", edgecolor="white")
    ax.set_title(f"{gene} (n={len(df):,} cells, sampled)")
    ax.set_xlabel(f"{gene} expression")
    ax.set_ylabel("cells")

    if color_by and color_by in df.columns:
        ax2 = axes[0, 1]
        groups = df.groupby(color_by)[gene].apply(list)
        # cap labels — top 8 by group size
        keys = sorted(groups.index, key=lambda k: -len(groups[k]))[:8]
        ax2.boxplot([groups[k] for k in keys], labels=[str(k)[:12] for k in keys],
                    showfliers=False)
        ax2.set_title(f"{gene} by {color_by}")
        ax2.tick_params(axis='x', rotation=30)

    fig.tight_layout()
    fig
    return


@app.cell
def _(api_input, db_selector, default_api, gene_input, mo, params):
    """Two-way bind widget state to URL — so anyone can copy-paste this link."""
    if db_selector.value:
        params["db_id"] = db_selector.value
    if gene_input.value.strip():
        params["gene"] = gene_input.value.strip()
    # Only record `api` in the URL when it differs from the auto-detected
    # same-origin default. Avoids cluttering shareable links.
    if api_input.value.strip() and api_input.value != default_api:
        params["api"] = api_input.value
    mo.md(
        "🔗 **Share this view:** copy the URL bar — it's updated live as you change widgets."
    )
    return


if __name__ == "__main__":
    app.run()
