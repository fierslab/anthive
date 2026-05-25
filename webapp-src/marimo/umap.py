"""Anthive — UMAP / embedding scatter (compact)."""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="UMAP")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
async def _packages():
    """Install deps + best-effort init of streaming-fetch worker (5s timeout)."""
    import sys as _sys
    streaming_ready = False
    if _sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib", "seaborn"])
        try:
            import asyncio as _aio
            from urllib3.contrib.emscripten.fetch import wait_for_streaming_ready
            await _aio.wait_for(wait_for_streaming_ready(), timeout=5.0)
            streaming_ready = True
        except Exception:
            streaming_ready = False
    return (streaming_ready,)


@app.cell
def _(mo):
    params = mo.query_params()
    try:
        import js
        auto_api = js.location.origin + "/api/"
    except Exception:
        auto_api = "http://localhost:8080/api/"
    default_api  = params.get("api", auto_api)
    default_db   = params.get("db_id", "P26/HMI")
    default_gene = params.get("gene", "TREM2")
    return default_api, default_db, default_gene, params


@app.cell
def _(default_api):
    from antclient import AnthiveClient
    client = AnthiveClient(default_api, timeout=120)
    return (client,)


@app.cell
def _(client, streaming_ready):
    _ = streaming_ready
    db_list_err = None
    candidates = []
    try:
        dbs = client.get_databases()
        candidates = [d for d in dbs if (d.get("embeddings") or {})]
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc!r}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in candidates}
    return candidates, db_list_err, options


@app.cell
def _(default_api, db_list_err, mo, options, streaming_ready):
    if db_list_err:
        banner = mo.md(
            f"❌ {db_list_err}<br>"
            f"<small>API URL: `{default_api}` · streaming_ready: {streaming_ready}</small>"
        )
    elif not options:
        banner = mo.md(
            f"⚠ No datasets with embeddings returned by the API.<br>"
            f"<small>API URL: `{default_api}` · streaming_ready: {streaming_ready}</small>"
        )
    else:
        banner = mo.md("")
    banner
    return


@app.cell
def _(default_db, mo, options):
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
def _(client, db_selector):
    db_id = db_selector.value
    embeddings = {}
    obs_cat = []
    obs_num = []
    db_info = {}
    if db_id:
        embeddings = client.get_embeddings(db_id) or {}
        db_info = client.get_database_info(db_id)
        obs = db_info.get("obs") or {}
        obs_cat = obs.get("categorical") or []
        obs_num = obs.get("numerical") or []
    return db_id, db_info, embeddings, obs_cat, obs_num


@app.cell
def _(default_gene, embeddings, mo, obs_cat, obs_num):
    emb_options = {info.get("name", key): key for key, info in embeddings.items()}
    default_emb = next(
        (k for k, v in emb_options.items() if v == "X_umap"),
        next(iter(emb_options)) if emb_options else None,
    )
    emb_selector = mo.ui.dropdown(
        options=emb_options, value=default_emb, label="embedding",
    ) if emb_options else mo.ui.text(value="", label="(no embedding)", disabled=True)
    color_by = mo.ui.dropdown(
        options=["Gene", "Numerical", "Categorical"],
        value="Gene", label="color by",
    )
    gene_input = mo.ui.text(value=default_gene, label="gene")
    num_input = mo.ui.dropdown(
        options=sorted(obs_num), value=obs_num[0] if obs_num else None,
        label="numerical field", searchable=True,
    ) if obs_num else mo.ui.text(value="", label="(no numerical)", disabled=True)
    cat_input = mo.ui.dropdown(
        options=sorted(obs_cat), value=obs_cat[0] if obs_cat else None,
        label="categorical field", searchable=True,
    ) if obs_cat else mo.ui.text(value="", label="(no categorical)", disabled=True)
    mo.hstack([emb_selector, color_by], justify="start", gap=1)
    return cat_input, color_by, emb_selector, gene_input, num_input


@app.cell
def _(cat_input, color_by, gene_input, mo, num_input):
    """Conditional field picker — only one is shown."""
    if color_by.value == "Gene":
        field_widget = gene_input
    elif color_by.value == "Numerical":
        field_widget = num_input
    else:
        field_widget = cat_input
    field_widget
    return (field_widget,)


@app.cell
def _(db_id, db_info, mo):
    if db_info:
        mo.md(
            f"<small>{db_id} · {db_info.get('n_cells', 0):,} cells · "
            f"organism: {db_info.get('organism','?')}</small>"
        )
    else:
        mo.md("_pick a dataset_")
    return


@app.cell
def _(client, color_by, db_id, field_widget):
    """Resolve the requested field. For Gene, do a case-insensitive lookup
    and offer suggestions if there's no exact match."""
    raw = (field_widget.value or "").strip() if hasattr(field_widget, "value") else ""
    resolved = None
    suggestions = []
    if db_id and raw:
        if color_by.value == "Gene":
            try:
                exact_hits = client.search_genes(db_id, q=raw, limit=1, exact=True)
                if exact_hits:
                    resolved = exact_hits[0]
                else:
                    suggestions = client.search_genes(db_id, q=raw, limit=8)
                    if len(suggestions) == 1:
                        resolved = suggestions[0]
            except Exception:
                pass
        else:
            # Numerical / categorical — exact match expected (came from dropdown)
            resolved = raw
    return raw, resolved, suggestions


@app.cell
def _(color_by, mo, raw, resolved, suggestions):
    if color_by.value != "Gene":
        hint = mo.md("")
    elif raw and resolved is None and suggestions:
        hint = mo.md(
            f"<small>**`{raw}`** — no exact match. Did you mean: "
            f"{', '.join(f'`{g}`' for g in suggestions[:6])}?</small>"
        )
    elif raw and resolved is None:
        hint = mo.md(f"<small>**`{raw}`** — not found.</small>")
    elif resolved and resolved != raw:
        hint = mo.md(f"<small>resolved `{raw}` → `{resolved}` (case)</small>")
    else:
        hint = mo.md("")
    hint
    return


@app.cell
def _(client, color_by, db_id, emb_selector, resolved):
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    if db_id and emb_selector and getattr(emb_selector, "value", None) and resolved:
        emb_name = emb_selector.value
        try:
            emb = client.get_embedding_data(db_id, emb_name, format="dataframe")
            if color_by.value == "Gene":
                color_df = client.get_cells(
                    db_id=db_id, genes=[resolved], limit=20000, format="dataframe"
                )
            else:
                color_df = client.get_cells(
                    db_id=db_id, metadata=[resolved], limit=20000, format="dataframe"
                )
            emb = emb.set_index(emb.columns[0])
            color_df = color_df.set_index(color_df.columns[0])
            if resolved not in color_df.columns:
                fetch_err = f"Field `{resolved}` missing in response."
            else:
                emb[resolved] = color_df[resolved]
                plot_df = emb.dropna(subset=[emb.columns[0], emb.columns[1]])
        except Exception as exc:
            fetch_err = f"Fetch failed: `{exc}`"
    return fetch_err, plot_df


@app.cell
def _(color_by, fetch_err, mo, plot_df, resolved):
    if fetch_err:
        out = mo.md(f"❌ {fetch_err}")
    elif plot_df.empty or not resolved:
        out = mo.md("_pick a dataset, embedding, and colour field_")
    else:
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
        import matplotlib.colors as mcolors
        import seaborn as sns
        xcol = plot_df.columns[0]
        ycol = plot_df.columns[1]
        fig, ax = plt.subplots(figsize=(5.5, 5))
        kwargs = dict(x=xcol, y=ycol, s=3, linewidth=0, ax=ax)
        if color_by.value in ("Gene", "Numerical"):
            _values = plot_df[resolved].astype(float)
            norm = mcolors.Normalize(
                vmin=_values.quantile(0.05), vmax=_values.quantile(0.95)
            )
            sns.scatterplot(data=plot_df, hue=resolved, palette="viridis",
                            legend=False, hue_norm=norm, **kwargs)
            fig.colorbar(cm.ScalarMappable(norm=norm, cmap="viridis"),
                         ax=ax, label=resolved, shrink=0.6)
        else:
            sns.scatterplot(data=plot_df, hue=resolved, palette="tab20", **kwargs)
            sns.move_legend(ax, loc="upper left", bbox_to_anchor=(1, 1),
                            frameon=False, title=resolved, fontsize=8)
        ax.set_xlabel(xcol, fontsize=9)
        ax.set_ylabel(ycol, fontsize=9)
        ax.set_title(f"{resolved} · n={len(plot_df):,}", fontsize=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.tight_layout()
        out = fig
    out
    return


@app.cell
def _(color_by, db_selector, field_widget, params):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if color_by.value == "Gene" and getattr(field_widget, "value", "").strip():
        params["gene"] = field_widget.value.strip()
    return


if __name__ == "__main__":
    app.run()
