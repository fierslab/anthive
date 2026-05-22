"""
Anthive — UMAP / embedding scatter

Plots a 2-D embedding with cells colored by gene, numerical obs, or
categorical obs. Mirrors webapp-streamlit/pages/2_umap.py.

URL parameters: ?db_id=…&gene=…&api=…
"""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Anthive — UMAP")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # UMAP / embedding scatter

        Pick a dataset with a stored 2-D embedding, then colour cells by
        gene expression, a numerical obs field, or a categorical field.
        """
    )
    return


@app.cell
async def _packages():
    import sys
    if sys.platform == "emscripten":
        import micropip
        await micropip.install(["antclient", "matplotlib", "seaborn"])
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
    """Pick from datasets that actually have an embedding."""
    db_list_err = None
    candidates = []
    try:
        dbs = client.get_databases()
        candidates = [d for d in dbs if (d.get("embeddings") or {})]
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in candidates}
    db_status = (
        mo.md(f"❌ {db_list_err}") if db_list_err
        else (mo.md("_(no datasets expose an embedding)_") if not options
              else mo.md(f"_{len(options)} dataset(s) with embeddings_"))
    )
    db_status
    return candidates, options, db_list_err


@app.cell
def _(default_db, mo, options):
    initial_key = (
        next((k for k, v in options.items() if v == default_db), None)
        or (next(iter(options)) if options else None)
    )
    db_selector = mo.ui.dropdown(
        options=options, value=initial_key,
        label=f"Dataset with embedding ({len(options)})", searchable=True,
    )
    db_selector
    return (db_selector,)


@app.cell
def _(client, db_selector, mo):
    """Fetch embeddings + obs schema for the chosen dataset."""
    db_id = db_selector.value
    embeddings = {}
    db_info = {}
    obs_cat = []
    obs_num = []
    if db_id:
        embeddings = client.get_embeddings(db_id) or {}
        db_info = client.get_database_info(db_id)
        obs = db_info.get("obs") or {}
        obs_cat = obs.get("categorical") or []
        obs_num = obs.get("numerical") or []
    schema_status = (
        mo.md(f"_dataset has {len(embeddings)} embedding(s), "
              f"{len(obs_cat)} categorical / {len(obs_num)} numerical obs_")
        if db_id and embeddings
        else mo.md("_(pick a dataset that has an embedding)_")
    )
    schema_status
    return db_id, db_info, embeddings, obs_cat, obs_num


@app.cell
def _(embeddings, mo):
    if not embeddings:
        return (None,)
    emb_options = {
        info.get("name", key): key for key, info in embeddings.items()
    }
    default_emb_key = next(
        (k for k, v in emb_options.items() if v == "X_umap"),
        next(iter(emb_options))
    )
    emb_selector = mo.ui.dropdown(
        options=emb_options, value=default_emb_key, label="Embedding",
    )
    emb_selector
    return (emb_selector,)


@app.cell
def _(mo):
    color_by = mo.ui.dropdown(
        options=["Gene", "Numerical", "Categorical"],
        value="Gene", label="Color by",
    )
    color_by
    return (color_by,)


@app.cell
def _(color_by, default_gene, mo, obs_cat, obs_num):
    """Selector for the actual field to color by — depends on color_by."""
    if color_by.value == "Gene":
        field_input = mo.ui.text(value=default_gene, label="Gene symbol")
    elif color_by.value == "Numerical":
        if not obs_num:
            mo.md("_(this dataset has no numerical obs fields)_")
            field_input = mo.ui.text(value="", label="Numerical field")
        else:
            field_input = mo.ui.dropdown(
                options=sorted(obs_num),
                value=obs_num[0], label="Numerical field", searchable=True,
            )
    else:  # Categorical
        if not obs_cat:
            mo.md("_(this dataset has no categorical obs fields)_")
            field_input = mo.ui.text(value="", label="Categorical field")
        else:
            field_input = mo.ui.dropdown(
                options=sorted(obs_cat),
                value=obs_cat[0], label="Categorical field", searchable=True,
            )
    field_input
    return (field_input,)


@app.cell
def _(client, color_by, db_id, emb_selector, field_input, mo):
    """Fetch embedding coordinates AND the colour values, join by cell."""
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    field = field_input.value.strip() if field_input.value else ""
    if db_id and emb_selector and emb_selector.value and field:
        emb_name = emb_selector.value
        try:
            emb = client.get_embedding_data(db_id, emb_name, format="dataframe")
            if color_by.value == "Gene":
                color_df = client.get_cells(db_id=db_id, genes=[field], format="dataframe")
            else:
                color_df = client.get_cells(db_id=db_id, metadata=[field], format="dataframe")
            emb = emb.set_index(emb.columns[0])
            color_df = color_df.set_index(color_df.columns[0])
            if field not in color_df.columns:
                fetch_err = (
                    f"Field `{field}` not in returned columns "
                    f"({list(color_df.columns)})."
                )
            else:
                emb[field] = color_df[field]
                plot_df = emb.dropna(subset=[emb.columns[0], emb.columns[1]])
        except Exception as exc:
            fetch_err = f"Fetch failed: `{exc}`"
    fetch_status = (
        mo.md(f"❌ {fetch_err}") if fetch_err
        else mo.md(f"**fetched** {len(plot_df):,} cells")
    )
    fetch_status
    return field, fetch_err, plot_df


@app.cell
def _(color_by, field, fetch_err, mo, plot_df):
    """Render the scatter plot."""
    scatter_fig = None
    if not fetch_err and not plot_df.empty:
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
        import matplotlib.colors as mcolors
        import seaborn as sns

        xcol = plot_df.columns[0]
        ycol = plot_df.columns[1]
        fig, ax = plt.subplots(figsize=(7, 6))
        kwargs = dict(x=xcol, y=ycol, s=4, linewidth=0, ax=ax)
        if color_by.value in ("Gene", "Numerical"):
            values = plot_df[field].astype(float)
            norm = mcolors.Normalize(
                vmin=values.quantile(0.05), vmax=values.quantile(0.95),
            )
            sns.scatterplot(data=plot_df, hue=field, palette="viridis",
                            legend=False, hue_norm=norm, **kwargs)
            fig.colorbar(cm.ScalarMappable(norm=norm, cmap="viridis"),
                         ax=ax, label=field, shrink=0.7)
        else:
            sns.scatterplot(data=plot_df, hue=field, palette="tab20", **kwargs)
            sns.move_legend(ax, loc="upper left", bbox_to_anchor=(1, 1),
                            frameon=False, title=field, fontsize=8)
        ax.set_xlabel(xcol)
        ax.set_ylabel(ycol)
        fig.tight_layout()
        scatter_fig = fig
    scatter_fig if scatter_fig is not None else mo.md("_(no plot yet)_")
    return (scatter_fig,)


@app.cell
def _(api_input, db_selector, default_api, field_input, color_by, params):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if color_by.value == "Gene" and field_input.value.strip():
        params["gene"] = field_input.value.strip()
    if api_input.value.strip() and api_input.value != default_api:
        params["api"] = api_input.value
    return


if __name__ == "__main__":
    app.run()
