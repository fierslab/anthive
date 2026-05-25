"""Anthive — Gene expression distribution (compact, streamlit-replacement)."""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Gene")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
async def _packages():
    """Install deps + initialise Pyodide streaming-fetch worker."""
    import sys
    if sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib", "seaborn"])
        try:
            from urllib3.contrib.emscripten.fetch import wait_for_streaming_ready
            await wait_for_streaming_ready()
        except Exception:
            pass
    return


@app.cell
def _(mo):
    params = mo.query_params()
    try:
        import js
        auto_api = js.location.origin + "/api"
    except Exception:
        auto_api = "http://localhost:8080/api"
    default_api  = params.get("api", auto_api)
    default_db   = params.get("db_id", "P26/HMI")
    default_gene = params.get("gene", "TREM2")
    default_plot = params.get("plot", "histogram")
    return default_api, default_db, default_gene, default_plot, params


@app.cell
def _(default_api):
    from antclient import AnthiveClient
    client = AnthiveClient(default_api, timeout=120)
    return (client,)


@app.cell
def _(client):
    db_list_err = None
    dbs = []
    try:
        dbs = client.get_databases()
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    return db_list_err, options


@app.cell
def _(default_db, mo, options):
    """Dataset picker — its own cell so marimo definitely tracks reactivity."""
    initial_key = (
        next((k for k, v in options.items() if v == default_db), None)
        or (next(iter(options)) if options else None)
    )
    db_selector = mo.ui.dropdown(
        options=options, value=initial_key,
        label="dataset", searchable=True, full_width=True,
    )
    db_selector
    return (db_selector,)


@app.cell
def _(default_gene, mo):
    gene_input = mo.ui.text(value=default_gene, label="gene", full_width=True)
    return (gene_input,)


@app.cell
def _(default_plot, mo):
    plot_type = mo.ui.dropdown(
        options=["histogram", "ecdf", "kde"], value=default_plot, label="plot",
    )
    return (plot_type,)


@app.cell
def _(gene_input, mo, plot_type):
    """Compact row: gene + plot side by side."""
    mo.hstack([gene_input, plot_type], justify="start", gap=1)
    return


@app.cell
def _(client, db_selector):
    db_id = db_selector.value
    db_info = client.get_database_info(db_id) if db_id else {}
    return db_id, db_info


@app.cell
def _(db_id, db_info, mo):
    if db_info:
        n_cells = db_info.get('n_cells', 0)
        layers = list((db_info.get('layers') or {}).keys())
        meta_md = mo.md(
            f"<small>{db_id} · {n_cells:,} cells × {db_info.get('n_genes', 0):,} genes · "
            f"organism: {db_info.get('organism','?')} · layers: {', '.join(layers) or '—'}</small>"
        )
    else:
        meta_md = mo.md("_pick a dataset_")
    meta_md
    return


@app.cell
def _(client, db_id, gene_input):
    """Resolve the gene name (case-insensitive, then fuzzy fallback)."""
    raw = gene_input.value.strip()
    resolved_gene = None
    suggestions = []
    search_err = None
    if db_id and raw:
        try:
            # First try exact (case-insensitive)
            exact_hits = client.search_genes(db_id, q=raw, limit=1, exact=True)
            if exact_hits:
                resolved_gene = exact_hits[0]
            else:
                # Substring search returns top-N candidates
                suggestions = client.search_genes(db_id, q=raw, limit=8)
                if suggestions and len(suggestions) == 1:
                    resolved_gene = suggestions[0]
        except Exception as exc:
            search_err = f"`search_genes` failed: `{exc}`"
    return raw, resolved_gene, search_err, suggestions


@app.cell
def _(mo, raw, resolved_gene, search_err, suggestions):
    """Show a helpful status only when there's something to say."""
    if search_err:
        status = mo.md(f"❌ {search_err}")
    elif raw and resolved_gene is None and suggestions:
        # Multiple candidates — show as clickable hints (markdown for now)
        sug_str = ', '.join(f'`{g}`' for g in suggestions[:6])
        status = mo.md(
            f"<small>**`{raw}`** — no exact match. Did you mean: {sug_str}?</small>"
        )
    elif raw and resolved_gene is None and not suggestions:
        status = mo.md(
            f"<small>**`{raw}`** — not found in this dataset.</small>"
        )
    elif resolved_gene and resolved_gene != raw:
        status = mo.md(
            f"<small>resolved `{raw}` → `{resolved_gene}` (case)</small>"
        )
    else:
        status = mo.md("")
    status
    return


@app.cell
def _(client, db_id, resolved_gene):
    import pandas as pd
    cells_df = pd.DataFrame()
    fetch_err = None
    if db_id and resolved_gene:
        try:
            cells_df = client.get_cells(
                db_id=db_id, genes=[resolved_gene],
                limit=20000, format="dataframe"
            )
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
    return cells_df, fetch_err


@app.cell
def _(cells_df, fetch_err, mo, plot_type, resolved_gene):
    if fetch_err:
        out = mo.md(f"❌ {fetch_err}")
    elif not resolved_gene or cells_df.empty or resolved_gene not in cells_df.columns:
        out = mo.md("_enter a valid gene_")
    else:
        import matplotlib.pyplot as plt
        import seaborn as sns
        _values = cells_df[resolved_gene].astype(float).dropna()
        fig, ax = plt.subplots(figsize=(6, 3.2))
        kind = plot_type.value
        if kind == "ecdf":
            sns.ecdfplot(x=_values, ax=ax, color="#2c5aa0")
        elif kind == "kde":
            sns.kdeplot(x=_values, ax=ax, fill=True, color="#2c5aa0")
        else:
            sns.histplot(x=_values, bins=50, ax=ax, color="#4c72b0")
        ax.set_xlabel(resolved_gene)
        ax.set_title(f"{resolved_gene} — {kind} (n={len(_values):,} sampled)",
                     fontsize=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.tight_layout()
        out = fig
    out
    return


@app.cell
def _(cells_df, fetch_err, mo, resolved_gene):
    if fetch_err or not resolved_gene or cells_df.empty or resolved_gene not in cells_df.columns:
        stats_md = mo.md("")
    else:
        _vals = cells_df[resolved_gene]
        _nz = _vals[_vals > 0]
        _pct = 100 * len(_nz) / max(len(_vals), 1)
        stats_md = mo.md(
            f"<small>**{resolved_gene}** · non-zero {len(_nz):,}/{len(_vals):,} "
            f"({_pct:.1f}%) · mean {_vals.mean():.3g} · "
            f"median {_vals.median():.3g} · std {_vals.std():.3g} · "
            f"range [{_vals.min():.3g}, {_vals.max():.3g}]</small>"
        )
    stats_md
    return


@app.cell
def _(db_selector, gene_input, params, plot_type):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if gene_input.value.strip():
        params["gene"] = gene_input.value.strip()
    if plot_type.value:
        params["plot"] = plot_type.value
    return


if __name__ == "__main__":
    app.run()
