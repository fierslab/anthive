"""
Anthive — Categorical breakdown

Box/violin/bar plot of a gene's expression (or a numerical obs field) split
by a categorical obs field — or a simple cell-count bar chart per category.
Mirrors webapp-streamlit/pages/4_cat.py.

URL parameters: ?db_id=…&gene=…&api=…
"""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Anthive — Category")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # Categorical breakdown

        Split a gene or numerical field across the levels of a categorical
        obs field. Or just count cells per category.
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
    return (AnthiveClient(api_input.value),)


@app.cell
def _(client, mo):
    db_list_err = None
    dbs = []
    try:
        dbs = client.get_databases()
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    db_status = (
        mo.md(f"❌ {db_list_err}") if db_list_err
        else mo.md(f"_{len(dbs)} datasets loaded_")
    )
    db_status
    return dbs, options, db_list_err


@app.cell
def _(default_db, mo, options):
    initial_key = (
        next((k for k, v in options.items() if v == default_db), None)
        or (next(iter(options)) if options else None)
    )
    db_selector = mo.ui.dropdown(
        options=options, value=initial_key,
        label=f"Dataset ({len(options)})", searchable=True,
    )
    db_selector
    return (db_selector,)


@app.cell
def _(client, db_selector):
    db_id = db_selector.value
    obs_cat = []
    obs_num = []
    if db_id:
        info = client.get_database_info(db_id)
        obs = info.get("obs") or {}
        obs_cat = obs.get("categorical") or []
        obs_num = obs.get("numerical") or []
    return db_id, obs_cat, obs_num


@app.cell
def _(mo, obs_cat):
    if not obs_cat:
        mo.md("_(this dataset has no categorical obs fields)_")
        return (None,)
    cat_selector = mo.ui.dropdown(
        options=sorted(obs_cat), value=obs_cat[0],
        label="Group by (categorical)", searchable=True,
    )
    cat_selector
    return (cat_selector,)


@app.cell
def _(mo):
    y_type = mo.ui.dropdown(
        options=["Gene", "Numerical", "Count"], value="Gene", label="Y axis",
    )
    y_type
    return (y_type,)


@app.cell
def _(default_gene, mo, obs_num, y_type):
    if y_type.value == "Gene":
        y_field = mo.ui.text(value=default_gene, label="Gene symbol")
    elif y_type.value == "Numerical":
        if not obs_num:
            mo.md("_(no numerical obs fields)_")
            y_field = mo.ui.text(value="", label="Numerical field")
        else:
            y_field = mo.ui.dropdown(
                options=sorted(obs_num), value=obs_num[0],
                label="Numerical field", searchable=True,
            )
    else:  # Count — no extra field
        y_field = mo.ui.text(value="", label="(unused for Count)", disabled=True)
    y_field
    return (y_field,)


@app.cell
def _(mo, y_type):
    if y_type.value == "Count":
        plot_choices = ["bar"]
    else:
        plot_choices = ["box", "violin", "bar"]
    plot_kind = mo.ui.dropdown(
        options=plot_choices, value=plot_choices[0], label="Plot kind",
    )
    plot_kind
    return (plot_kind,)


@app.cell
def _(cat_selector, client, db_id, mo, y_field, y_type):
    """Fetch the data we need."""
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    yval = y_field.value.strip() if y_type.value != "Count" else None
    if db_id and cat_selector and cat_selector.value:
        cat = cat_selector.value
        try:
            if y_type.value == "Gene":
                plot_df = client.get_cells(
                    db_id=db_id, genes=[yval], metadata=[cat], format="dataframe")
            elif y_type.value == "Numerical":
                plot_df = client.get_cells(
                    db_id=db_id, metadata=[cat, yval], format="dataframe")
            else:
                plot_df = client.get_cells(
                    db_id=db_id, metadata=[cat], format="dataframe")
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and cat not in plot_df.columns:
            fetch_err = f"Field `{cat}` not in returned columns ({list(plot_df.columns)})."
        if not fetch_err and yval and yval not in plot_df.columns:
            fetch_err = f"Field `{yval}` not in returned columns ({list(plot_df.columns)})."
    fetch_status = (
        mo.md(f"❌ {fetch_err}") if fetch_err
        else mo.md(f"**fetched** {len(plot_df):,} cells")
    )
    fetch_status
    return plot_df, fetch_err, yval


@app.cell
def _(cat_selector, fetch_err, mo, plot_df, plot_kind, y_field, y_type):
    """Render the plot."""
    cat_fig = None
    if not fetch_err and not plot_df.empty:
        import matplotlib.pyplot as plt
        import seaborn as sns
        cat = cat_selector.value
        kind = plot_kind.value
        fig, ax = plt.subplots(figsize=(8, 4.5))
        order = plot_df[cat].value_counts().index.tolist()
        if y_type.value == "Count":
            counts = plot_df[cat].value_counts().reindex(order)
            sns.barplot(x=counts.index, y=counts.values, ax=ax,
                        order=order, color="#4c72b0")
            ax.set_xlabel(cat); ax.set_ylabel("cell count")
            ax.set_title(f"cells per {cat}")
        else:
            y = y_field.value.strip()
            if kind == "box":
                sns.boxplot(data=plot_df, x=cat, y=y, order=order, ax=ax, showfliers=False)
            elif kind == "violin":
                sns.violinplot(data=plot_df, x=cat, y=y, order=order, ax=ax)
            else:
                sns.barplot(data=plot_df, x=cat, y=y, order=order, ax=ax)
            ax.set_title(f"{y} by {cat} — {kind}")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        fig.tight_layout()
        cat_fig = fig
    cat_fig if cat_fig is not None else mo.md("_(no plot yet)_")
    return (cat_fig,)


@app.cell
def _(cat_selector, fetch_err, mo, plot_df, y_field, y_type):
    """Per-category summary stats."""
    stats_out = None
    if not fetch_err and not plot_df.empty:
        cat = cat_selector.value
        if y_type.value == "Count":
            counts = plot_df[cat].value_counts().rename("count")
            out = counts.reset_index()
            out.columns = [cat, "count"]
            out["%"] = (100 * out["count"] / out["count"].sum()).round(2)
            stats_out = mo.vstack([mo.md("### Counts per category"), out])
        else:
            y = y_field.value.strip()
            stats = (plot_df.groupby(cat)[y]
                     .agg(count="count", mean="mean", std="std", median="median")
                     .round(4))
            stats_out = mo.vstack([mo.md(f"### {y} stats per {cat}"), stats])
    stats_out if stats_out is not None else mo.md("")
    return (stats_out,)


@app.cell
def _(api_input, db_selector, default_api, params, y_field, y_type):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if y_type.value == "Gene" and y_field.value.strip():
        params["gene"] = y_field.value.strip()
    if api_input.value.strip() and api_input.value != default_api:
        params["api"] = api_input.value
    return


if __name__ == "__main__":
    app.run()
