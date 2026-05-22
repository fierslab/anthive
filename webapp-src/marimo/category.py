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
    df = pd.DataFrame()
    err = None
    if not (db_id and cat_selector and cat_selector.value):
        return df, err
    cat = cat_selector.value
    yval = y_field.value.strip() if y_type.value != "Count" else None
    try:
        if y_type.value == "Gene":
            df = client.get_cells(db_id=db_id, genes=[yval], metadata=[cat], format="dataframe")
        elif y_type.value == "Numerical":
            df = client.get_cells(db_id=db_id, metadata=[cat, yval], format="dataframe")
        else:
            df = client.get_cells(db_id=db_id, metadata=[cat], format="dataframe")
    except Exception as exc:
        err = f"`get_cells` failed: `{exc}`"
        mo.md(f"❌ {err}")
        return df, err
    if cat not in df.columns:
        err = f"Field `{cat}` not in returned columns ({list(df.columns)})."
        mo.md(f"❌ {err}")
        return df, err
    if yval and yval not in df.columns:
        err = f"Field `{yval}` not in returned columns ({list(df.columns)})."
        mo.md(f"❌ {err}")
        return df, err
    mo.md(f"**fetched** {len(df):,} cells")
    return df, err


@app.cell
def _(cat_selector, df, err, plot_kind, y_field, y_type):
    """Render the plot."""
    if err or df.empty:
        return
    import matplotlib.pyplot as plt
    import seaborn as sns

    cat = cat_selector.value
    kind = plot_kind.value
    fig, ax = plt.subplots(figsize=(8, 4.5))
    order = df[cat].value_counts().index.tolist()

    if y_type.value == "Count":
        counts = df[cat].value_counts().reindex(order)
        sns.barplot(x=counts.index, y=counts.values, ax=ax,
                    order=order, color="#4c72b0")
        ax.set_xlabel(cat); ax.set_ylabel("cell count")
        ax.set_title(f"cells per {cat}")
    else:
        y = y_field.value.strip()
        if kind == "box":
            sns.boxplot(data=df, x=cat, y=y, order=order, ax=ax, showfliers=False)
        elif kind == "violin":
            sns.violinplot(data=df, x=cat, y=y, order=order, ax=ax)
        else:
            sns.barplot(data=df, x=cat, y=y, order=order, ax=ax)
        ax.set_title(f"{y} by {cat} — {kind}")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()
    fig
    return


@app.cell
def _(cat_selector, df, err, mo, y_field, y_type):
    """Per-category summary stats."""
    if err or df.empty:
        return
    cat = cat_selector.value
    if y_type.value == "Count":
        counts = df[cat].value_counts().rename("count")
        out = counts.reset_index()
        out.columns = [cat, "count"]
        out["%"] = (100 * out["count"] / out["count"].sum()).round(2)
        mo.md("### Counts per category")
        out
    else:
        y = y_field.value.strip()
        stats = (df.groupby(cat)[y]
                 .agg(count="count", mean="mean", std="std", median="median")
                 .round(4))
        mo.md(f"### {y} stats per {cat}")
        stats
    return


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
