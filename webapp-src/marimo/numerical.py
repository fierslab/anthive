"""Anthive — Numerical obs distribution (compact)."""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Numerical")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
async def _packages():
    """Install deps + initialise the streaming-fetch worker. See gene.py."""
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
    default_fld  = params.get("field", "")
    return default_api, default_db, default_fld, params


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
    obs_cat = []
    obs_num = []
    db_info = {}
    if db_id:
        db_info = client.get_database_info(db_id)
        obs = db_info.get("obs") or {}
        obs_cat = obs.get("categorical") or []
        obs_num = obs.get("numerical") or []
    return db_id, db_info, obs_cat, obs_num


@app.cell
def _(default_fld, mo, obs_cat, obs_num):
    default_value = default_fld if default_fld in obs_num else (obs_num[0] if obs_num else None)
    num_selector = mo.ui.dropdown(
        options=sorted(obs_num), value=default_value,
        label="numerical field", searchable=True,
    ) if obs_num else mo.ui.text(value="", label="(no numerical)", disabled=True)
    group_by = mo.ui.dropdown(
        options=["None"] + (["Categorical"] if obs_cat else []),
        value="None", label="group by",
    )
    cat_selector = mo.ui.dropdown(
        options=sorted(obs_cat), value=obs_cat[0] if obs_cat else None,
        label="categorical group", searchable=True,
    ) if obs_cat else mo.ui.text(value="", label="(no categorical)", disabled=True)
    return cat_selector, group_by, num_selector


@app.cell
def _(cat_selector, group_by, mo, num_selector):
    """Show group control only if grouping selected."""
    widgets = [num_selector, group_by]
    if group_by.value == "Categorical":
        widgets.append(cat_selector)
    mo.hstack(widgets, justify="start", gap=1)
    return


@app.cell
def _(group_by, mo):
    choices = (["box", "violin", "bar"] if group_by.value == "Categorical"
               else ["histogram", "ecdf", "kde"])
    plot_kind = mo.ui.dropdown(
        options=choices, value=choices[0], label="plot"
    )
    plot_kind
    return (plot_kind,)


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
def _(cat_selector, client, db_id, group_by, num_selector):
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    num = num_selector.value if hasattr(num_selector, "value") else None
    if db_id and num:
        fields = [num]
        if group_by.value == "Categorical" and hasattr(cat_selector, "value") and cat_selector.value:
            fields.append(cat_selector.value)
        try:
            plot_df = client.get_cells(
                db_id=db_id, metadata=fields, limit=20000, format="dataframe"
            )
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and num not in plot_df.columns:
            fetch_err = f"Field `{num}` not in returned columns."
    return fetch_err, num, plot_df


@app.cell
def _(cat_selector, fetch_err, group_by, mo, num, plot_df, plot_kind):
    if fetch_err:
        out = mo.md(f"❌ {fetch_err}")
    elif plot_df.empty:
        out = mo.md("_no data yet_")
    else:
        import matplotlib.pyplot as plt
        import seaborn as sns
        kind = plot_kind.value
        fig, ax = plt.subplots(figsize=(6, 3.5))
        if group_by.value == "Categorical" and hasattr(cat_selector, "value") and cat_selector.value:
            cat = cat_selector.value
            order = plot_df[cat].value_counts().index.tolist()[:20]
            if kind == "box":
                sns.boxplot(data=plot_df, x=cat, y=num, order=order, ax=ax,
                            showfliers=False, color="#4c72b0")
            elif kind == "violin":
                sns.violinplot(data=plot_df, x=cat, y=num, order=order, ax=ax,
                               color="#4c72b0")
            else:
                sns.barplot(data=plot_df, x=cat, y=num, order=order, ax=ax,
                            color="#4c72b0")
            plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
            ax.set_title(f"{num} by {cat}", fontsize=10)
        else:
            values = plot_df[num].astype(float).dropna()
            if kind == "ecdf":
                sns.ecdfplot(x=values, ax=ax, color="#2c5aa0")
            elif kind == "kde":
                sns.kdeplot(x=values, ax=ax, fill=True, color="#2c5aa0")
            else:
                sns.histplot(x=values, bins=50, ax=ax, color="#4c72b0")
            ax.set_xlabel(num, fontsize=9)
            ax.set_title(f"{num} — {kind} (n={len(values):,})", fontsize=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.tight_layout()
        out = fig
    out
    return


@app.cell
def _(db_selector, num_selector, params):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if hasattr(num_selector, "value") and num_selector.value:
        params["field"] = num_selector.value
    return


if __name__ == "__main__":
    app.run()
