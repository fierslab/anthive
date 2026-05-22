"""
Anthive — Gene expression distribution

A single-gene view: histogram / ECDF / KDE of expression across cells,
plus summary statistics. Mirrors webapp-streamlit/pages/1_gene.py.

URL parameters: ?db_id=…&gene=…&api=…
"""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Anthive — Gene")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # Gene expression

        Distribution of a single gene's expression across cells in one dataset.
        URL params: `?db_id=…&gene=…&api=…` — widgets and URL stay in sync.
        """
    )
    return


@app.cell
async def _packages():
    """Install client dependencies under Pyodide (no-op outside WASM)."""
    import sys
    if sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib", "seaborn"])
    return


@app.cell
def _(mo):
    """Read URL params; auto-detect API origin in the browser."""
    params = mo.query_params()
    try:
        import js
        auto_api = js.location.origin + "/api"
    except Exception:
        auto_api = "http://localhost:8080/api"
    default_api  = params.get("api", auto_api)
    default_db   = params.get("db_id", "P26/HMI")
    default_gene = params.get("gene", "TREM2")
    return default_api, default_db, default_gene, params


@app.cell
def _(default_api, mo):
    api_input = mo.ui.text(value=default_api, label="API base URL", full_width=True)
    api_input
    return (api_input,)


@app.cell
def _(api_input):
    from antclient import AnthiveClient
    client = AnthiveClient(api_input.value)
    return (client,)


@app.cell
def _(client, mo):
    """Fetch dataset list once, surface any error inline."""
    db_list_err = None
    dbs = []
    try:
        dbs = client.get_databases()
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    mo.md(f"❌ {db_list_err}") if db_list_err else mo.md(
        f"_loaded {len(dbs)} dataset(s) from the API_"
    )
    return (dbs, options, db_list_err)


@app.cell
def _(default_db, mo, options):
    """Dataset picker (searchable dropdown)."""
    initial_key = (
        next((k for k, v in options.items() if v == default_db), None)
        or (next(iter(options)) if options else None)
    )
    db_selector = mo.ui.dropdown(
        options=options, value=initial_key,
        label=f"Dataset ({len(options)} available)", searchable=True,
    )
    db_selector
    return (db_selector,)


@app.cell
def _(default_gene, mo):
    gene_input = mo.ui.text(value=default_gene, label="Gene symbol", full_width=True)
    plot_type  = mo.ui.dropdown(
        options=["histogram", "ecdf", "kde"], value="histogram", label="Plot type",
    )
    mo.hstack([gene_input, plot_type])
    return gene_input, plot_type


@app.cell
def _(client, db_selector, mo):
    """Dataset info card."""
    db_id = db_selector.value
    db_info = {}
    if db_id:
        db_info = client.get_database_info(db_id)
    if db_id and db_info:
        layers = list((db_info.get("layers") or {}).keys())
        info_card = mo.md(
            f"### `{db_id}` — {db_info.get('title','')}\n\n"
            f"**{db_info.get('n_cells', 0):,}** cells × "
            f"**{db_info.get('n_genes', 0):,}** genes  ·  "
            f"organism: {db_info.get('organism','?')}  ·  "
            f"layers: {', '.join(layers) or '—'}"
        )
    else:
        info_card = mo.md("_(pick a dataset above)_")
    info_card
    return db_id, db_info


@app.cell
def _(client, db_id, gene_input, mo):
    """Verify the gene exists in this dataset."""
    gene = gene_input.value.strip()
    matches = []
    search_err = None
    if db_id and gene:
        try:
            matches = client.search_genes(db_id, q=gene, limit=8)
        except Exception as exc:
            search_err = f"`search_genes` failed: `{exc}`"
    hit_str = ", ".join(f"`{g}`" for g in matches[:8]) or "_(none)_"
    search_status = mo.md(
        f"**search** → {len(matches)} match{'es' if len(matches)!=1 else ''}: {hit_str}"
        + (f"\n\n❌ {search_err}" if search_err else "")
    )
    search_status
    return gene, matches, search_err


@app.cell
def _(client, db_id, gene, mo):
    """Pull the gene's expression vector across all cells (single column)."""
    import pandas as pd
    cells_df = pd.DataFrame()
    fetch_err = None
    if db_id and gene:
        try:
            cells_df = client.get_cells(db_id=db_id, genes=[gene], format="dataframe")
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and gene not in cells_df.columns:
            fetch_err = (
                f"Gene `{gene}` not in returned columns ({list(cells_df.columns)}). "
                "Try one of the search hits above."
            )
    fetch_status = (
        mo.md(f"❌ {fetch_err}") if fetch_err
        else mo.md(f"**fetched** {len(cells_df):,} cells")
    )
    fetch_status
    return cells_df, fetch_err


@app.cell
def _(cells_df, fetch_err, gene, mo, plot_type):
    """Render the distribution plot."""
    plot_fig = None
    if not (fetch_err or cells_df.empty or gene not in cells_df.columns):
        import matplotlib.pyplot as plt
        import seaborn as sns
        values = cells_df[gene].astype(float).dropna()
        fig, ax = plt.subplots(figsize=(7, 4))
        plot_kind = plot_type.value
        if plot_kind == "ecdf":
            sns.ecdfplot(x=values, ax=ax)
        elif plot_kind == "kde":
            sns.kdeplot(x=values, ax=ax, fill=True)
        else:
            sns.histplot(x=values, bins=60, ax=ax)
        ax.set_xlabel(gene)
        ax.set_title(f"{gene} expression — {plot_kind} (n={len(values):,})")
        fig.tight_layout()
        plot_fig = fig
    plot_fig if plot_fig is not None else mo.md("_(no plot yet)_")
    return (plot_fig,)


@app.cell
def _(cells_df, fetch_err, gene, mo):
    """Tabular summary stats — matches the streamlit page."""
    stats_md = None
    if not (fetch_err or cells_df.empty or gene not in cells_df.columns):
        values = cells_df[gene]
        nonzero = values[values > 0]
        rows = [
            ("entries", f"{len(values):,}"),
            ("non-zero", f"{len(nonzero):,}"),
            ("% non-zero", f"{100*len(nonzero)/max(len(values),1):.2f}%"),
            ("mean", f"{values.mean():.3g}"),
            ("std", f"{values.std():.3g}"),
            ("median", f"{values.median():.3g}"),
            ("min", f"{values.min():.3g}"),
            ("10%", f"{values.quantile(0.1):.3g}"),
            ("90%", f"{values.quantile(0.9):.3g}"),
            ("max", f"{values.max():.3g}"),
        ]
        table = "| field | value |\n|---|---:|\n" + "\n".join(
            f"| {k} | {v} |" for k, v in rows
        )
        stats_md = mo.md("### Stats\n\n" + table)
    stats_md if stats_md is not None else mo.md("")
    return (stats_md,)


@app.cell
def _(api_input, db_selector, default_api, gene_input, params, plot_type):
    """Two-way bind widget state into the URL."""
    if db_selector.value:
        params["db_id"] = db_selector.value
    if gene_input.value.strip():
        params["gene"] = gene_input.value.strip()
    if plot_type.value:
        params["plot"] = plot_type.value
    if api_input.value.strip() and api_input.value != default_api:
        params["api"] = api_input.value
    return


if __name__ == "__main__":
    app.run()
