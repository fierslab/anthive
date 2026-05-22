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
    err = None
    try:
        dbs = client.get_databases()
    except Exception as exc:
        dbs, err = [], f"Could not reach API: `{exc}`"
        mo.md(f"❌ {err}")
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    return (dbs, options, err)


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
    if not db_id:
        return db_id, [], []
    info = client.get_database_info(db_id)
    obs = info.get("obs") or {}
    return db_id, obs.get("categorical") or [], obs.get("numerical") or []


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
    df = pd.DataFrame()
    err = None
    if not (db_id and num_selector and num_selector.value):
        return df, err
    num = num_selector.value
    fields = [num]
    if group_by.value == "Categorical" and cat_selector.value:
        fields.append(cat_selector.value)
    try:
        df = client.get_cells(db_id=db_id, metadata=fields, format="dataframe")
    except Exception as exc:
        err = f"`get_cells` failed: `{exc}`"
        mo.md(f"❌ {err}")
        return df, err
    if num not in df.columns:
        err = f"Field `{num}` not in columns ({list(df.columns)})."
        mo.md(f"❌ {err}")
        return df, err
    mo.md(f"**fetched** {len(df):,} cells")
    return df, err


@app.cell
def _(cat_selector, df, err, group_by, num_selector, plot_kind):
    """Render."""
    if err or df.empty:
        return
    import matplotlib.pyplot as plt
    import seaborn as sns
    num = num_selector.value
    kind = plot_kind.value

    fig, ax = plt.subplots(figsize=(8, 4.5))

    if group_by.value == "Categorical" and cat_selector.value:
        cat = cat_selector.value
        order = df[cat].value_counts().index.tolist()
        if kind == "box":
            sns.boxplot(data=df, x=cat, y=num, order=order, ax=ax, showfliers=False)
        elif kind == "violin":
            sns.violinplot(data=df, x=cat, y=num, order=order, ax=ax)
        else:
            sns.barplot(data=df, x=cat, y=num, order=order, ax=ax)
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        ax.set_title(f"{num} by {cat} — {kind}")
    else:
        values = df[num].astype(float).dropna()
        if kind == "ecdf":
            sns.ecdfplot(x=values, ax=ax)
        elif kind == "kde":
            sns.kdeplot(x=values, ax=ax, fill=True)
        else:
            sns.histplot(x=values, bins=60, ax=ax)
        ax.set_xlabel(num)
        ax.set_title(f"{num} — {kind} (n={len(values):,})")
    fig.tight_layout()
    fig
    return


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
