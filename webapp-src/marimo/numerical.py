"""
Anthive — Numerical obs distribution

Distribution of a numerical obs field (e.g. n_counts, percent.mt, a module
score) across cells. Optionally split by a categorical field.

URL parameters: ?db_id=…&field=…&api=…
"""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Anthive — Numerical")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # Numerical obs distribution

        Pick a numerical obs field and visualise it. Useful for QC metrics
        (UMI counts, percent mito) and module scores (e.g. WGCNA modules).
        Optional grouping by a categorical field.
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
    default_fld  = params.get("field", "")
    return default_api, default_db, default_fld, params


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
def _(default_fld, mo, obs_num):
    if not obs_num:
        mo.md("_(this dataset has no numerical obs fields)_")
        return (None,)
    default_value = default_fld if default_fld in obs_num else obs_num[0]
    num_selector = mo.ui.dropdown(
        options=sorted(obs_num), value=default_value,
        label="Numerical field", searchable=True,
    )
    num_selector
    return (num_selector,)


@app.cell
def _(mo, obs_cat):
    group_choices = ["None"] + (["Categorical"] if obs_cat else [])
    group_by = mo.ui.dropdown(
        options=group_choices, value="None", label="Group by",
    )
    group_by
    return (group_by,)


@app.cell
def _(group_by, mo, obs_cat):
    if group_by.value != "Categorical" or not obs_cat:
        cat_selector = mo.ui.text(value="", label="(no grouping)", disabled=True)
    else:
        cat_selector = mo.ui.dropdown(
            options=sorted(obs_cat), value=obs_cat[0],
            label="Categorical group", searchable=True,
        )
    cat_selector
    return (cat_selector,)


@app.cell
def _(group_by, mo):
    if group_by.value == "Categorical":
        choices = ["box", "violin", "bar"]
    else:
        choices = ["histogram", "ecdf", "kde"]
    plot_kind = mo.ui.dropdown(options=choices, value=choices[0], label="Plot")
    plot_kind
    return (plot_kind,)


@app.cell
def _(cat_selector, client, db_id, group_by, mo, num_selector):
    """Fetch the data."""
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    if db_id and num_selector and num_selector.value:
        num = num_selector.value
        fields = [num]
        if group_by.value == "Categorical" and cat_selector.value:
            fields.append(cat_selector.value)
        try:
            plot_df = client.get_cells(db_id=db_id, metadata=fields, format="dataframe")
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and num not in plot_df.columns:
            fetch_err = f"Field `{num}` not in columns ({list(plot_df.columns)})."
    fetch_status = (
        mo.md(f"❌ {fetch_err}") if fetch_err
        else mo.md(f"**fetched** {len(plot_df):,} cells")
    )
    fetch_status
    return plot_df, fetch_err


@app.cell
def _(cat_selector, fetch_err, group_by, mo, num_selector, plot_df, plot_kind):
    """Render."""
    num_fig = None
    if not fetch_err and not plot_df.empty:
        import matplotlib.pyplot as plt
        import seaborn as sns
        num = num_selector.value
        kind = plot_kind.value
        fig, ax = plt.subplots(figsize=(8, 4.5))
        if group_by.value == "Categorical" and cat_selector.value:
            cat = cat_selector.value
            order = plot_df[cat].value_counts().index.tolist()
            if kind == "box":
                sns.boxplot(data=plot_df, x=cat, y=num, order=order, ax=ax, showfliers=False)
            elif kind == "violin":
                sns.violinplot(data=plot_df, x=cat, y=num, order=order, ax=ax)
            else:
                sns.barplot(data=plot_df, x=cat, y=num, order=order, ax=ax)
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
            ax.set_title(f"{num} by {cat} — {kind}")
        else:
            values = plot_df[num].astype(float).dropna()
            if kind == "ecdf":
                sns.ecdfplot(x=values, ax=ax)
            elif kind == "kde":
                sns.kdeplot(x=values, ax=ax, fill=True)
            else:
                sns.histplot(x=values, bins=60, ax=ax)
            ax.set_xlabel(num)
            ax.set_title(f"{num} — {kind} (n={len(values):,})")
        fig.tight_layout()
        num_fig = fig
    num_fig if num_fig is not None else mo.md("_(no plot yet)_")
    return (num_fig,)


@app.cell
def _(api_input, db_selector, default_api, num_selector, params):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if num_selector and num_selector.value:
        params["field"] = num_selector.value
    if api_input.value.strip() and api_input.value != default_api:
        params["api"] = api_input.value
    return


if __name__ == "__main__":
    app.run()
