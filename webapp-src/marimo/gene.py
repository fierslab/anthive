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
    """Install deps + initialise the streaming-fetch worker.

    Without wait_for_streaming_ready() Pyodide's urllib3 falls back to XHR,
    which silently truncates responses above ~56 KB — manifesting as
    `JSONDecodeError: Unterminated string at column 56532` from antclient.
    """
    import sys
    if sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib", "seaborn"])
        try:
            from urllib3.contrib.emscripten.fetch import wait_for_streaming_ready
            await wait_for_streaming_ready()
        except Exception:
            pass  # older urllib3 — XHR fallback applies, may truncate large fetches
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
    return dbs, options, db_list_err


@app.cell
def _(default_db, default_gene, default_plot, mo, options):
    initial_key = (
        next((k for k, v in options.items() if v == default_db), None)
        or (next(iter(options)) if options else None)
    )
    db_selector = mo.ui.dropdown(
        options=options, value=initial_key,
        label="dataset", searchable=True, full_width=True,
    )
    gene_input = mo.ui.text(value=default_gene, label="gene")
    plot_type = mo.ui.dropdown(
        options=["histogram", "ecdf", "kde"], value=default_plot, label="plot",
    )
    mo.vstack([
        db_selector,
        mo.hstack([gene_input, plot_type], justify="start", gap=1),
    ])
    return db_selector, gene_input, plot_type


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
        mo.md(
            f"<small>{db_id} · {n_cells:,} cells × {db_info.get('n_genes', 0):,} genes · "
            f"organism: {db_info.get('organism','?')} · layers: {', '.join(layers) or '—'}</small>"
        )
    else:
        mo.md("_pick a dataset_")
    return


@app.cell
def _(client, db_id, gene_input, mo):
    gene = gene_input.value.strip()
    matches = []
    search_err = None
    if db_id and gene:
        try:
            matches = client.search_genes(db_id, q=gene, limit=8)
        except Exception as exc:
            search_err = f"`search_genes` failed: `{exc}`"
    status = None
    if search_err:
        status = mo.md(f"❌ {search_err}")
    elif gene and matches and gene not in matches:
        status = mo.md(f"<small>no exact match — closest: {', '.join(f'`{g}`' for g in matches[:5])}</small>")
    elif gene and not matches:
        status = mo.md(f"<small>gene `{gene}` not found in this dataset</small>")
    status if status is not None else mo.md("")
    return gene, matches, search_err


@app.cell
def _(client, db_id, gene):
    import pandas as pd
    cells_df = pd.DataFrame()
    fetch_err = None
    if db_id and gene:
        try:
            cells_df = client.get_cells(
                db_id=db_id, genes=[gene], limit=20000, format="dataframe"
            )
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and gene not in cells_df.columns:
            fetch_err = f"Gene `{gene}` not in returned columns."
    return cells_df, fetch_err


@app.cell
def _(cells_df, fetch_err, gene, mo, plot_type):
    if fetch_err:
        out = mo.md(f"❌ {fetch_err}")
    elif cells_df.empty or gene not in cells_df.columns:
        out = mo.md("_no data yet_")
    else:
        import matplotlib.pyplot as plt
        import seaborn as sns
        _values = cells_df[gene].astype(float).dropna()
        fig, ax = plt.subplots(figsize=(6, 3.2))
        kind = plot_type.value
        if kind == "ecdf":
            sns.ecdfplot(x=_values, ax=ax, color="#2c5aa0")
        elif kind == "kde":
            sns.kdeplot(x=_values, ax=ax, fill=True, color="#2c5aa0")
        else:
            sns.histplot(x=_values, bins=50, ax=ax, color="#4c72b0")
        ax.set_xlabel(gene)
        ax.set_title(f"{gene} — {kind} (n={len(_values):,} sampled)", fontsize=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.tight_layout()
        out = fig
    out
    return


@app.cell
def _(cells_df, fetch_err, gene, mo):
    if fetch_err or cells_df.empty or gene not in cells_df.columns:
        stats_md = mo.md("")
    else:
        _vals = cells_df[gene]
        _nz = _vals[_vals > 0]
        _pct = 100 * len(_nz) / max(len(_vals), 1)
        stats_md = mo.md(
            f"<small>**{gene}** · non-zero {len(_nz):,}/{len(_vals):,} ({_pct:.1f}%) · "
            f"mean {_vals.mean():.3g} · median {_vals.median():.3g} · "
            f"std {_vals.std():.3g} · range [{_vals.min():.3g}, {_vals.max():.3g}]</small>"
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
