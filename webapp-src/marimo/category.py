"""Anthive — Categorical breakdown (compact)."""
import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium", app_title="Category")


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
    dbs = []
    try:
        dbs = client.get_databases()
    except Exception as exc:
        db_list_err = f"Could not reach API: `{exc!r}`"
    options = {f"{d['id']} — {d.get('title','')}": d['id'] for d in dbs}
    return db_list_err, options


@app.cell
def _(default_api, db_list_err, mo, options, streaming_ready):
    if db_list_err:
        banner = mo.md(
            f"❌ {db_list_err}<br>"
            f"<small>API URL: `{default_api}` · streaming_ready: {streaming_ready}</small>"
        )
    elif not options:
        banner = mo.md(
            f"⚠ API returned 0 datasets.<br>"
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
def _(default_gene, mo, obs_cat, obs_num):
    cat_selector = mo.ui.dropdown(
        options=sorted(obs_cat), value=obs_cat[0] if obs_cat else None,
        label="group by", searchable=True,
    ) if obs_cat else mo.ui.text(value="", label="(no categorical)", disabled=True)
    y_type = mo.ui.dropdown(
        options=["Gene", "Numerical", "Count"], value="Gene", label="y axis",
    )
    gene_input = mo.ui.text(value=default_gene, label="gene")
    num_input = mo.ui.dropdown(
        options=sorted(obs_num), value=obs_num[0] if obs_num else None,
        label="numerical field", searchable=True,
    ) if obs_num else mo.ui.text(value="", label="(no numerical)", disabled=True)
    return cat_selector, gene_input, num_input, y_type


@app.cell
def _(gene_input, num_input, y_type):
    """Show only the relevant y-field input."""
    if y_type.value == "Gene":
        y_widget = gene_input
    elif y_type.value == "Numerical":
        y_widget = num_input
    else:
        y_widget = None  # Count needs no field
    return (y_widget,)


@app.cell
def _(cat_selector, mo, y_type, y_widget):
    mo.hstack(
        [cat_selector, y_type] + ([y_widget] if y_widget is not None else []),
        justify="start", gap=1,
    )
    return


@app.cell
def _(mo, y_type):
    plot_choices = ["bar"] if y_type.value == "Count" else ["box", "violin", "bar"]
    plot_kind = mo.ui.dropdown(
        options=plot_choices, value=plot_choices[0], label="plot",
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
def _(client, db_id, y_type, y_widget):
    """Resolve y-field (case-insensitive gene lookup with suggestions)."""
    raw = (y_widget.value or "").strip() if y_widget and y_type.value != "Count" else ""
    resolved = None
    suggestions = []
    if db_id and raw:
        if y_type.value == "Gene":
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
            resolved = raw
    return raw, resolved, suggestions


@app.cell
def _(mo, raw, resolved, suggestions, y_type):
    if y_type.value != "Gene":
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
def _(cat_selector, client, db_id, resolved, y_type):
    import pandas as pd
    plot_df = pd.DataFrame()
    fetch_err = None
    cat = cat_selector.value if hasattr(cat_selector, "value") else None
    if db_id and cat:
        try:
            if y_type.value == "Gene" and resolved:
                plot_df = client.get_cells(
                    db_id=db_id, genes=[resolved], metadata=[cat],
                    limit=20000, format="dataframe",
                )
            elif y_type.value == "Numerical" and resolved:
                plot_df = client.get_cells(
                    db_id=db_id, metadata=[cat, resolved],
                    limit=20000, format="dataframe",
                )
            elif y_type.value == "Count":
                plot_df = client.get_cells(
                    db_id=db_id, metadata=[cat],
                    limit=20000, format="dataframe",
                )
        except Exception as exc:
            fetch_err = f"`get_cells` failed: `{exc}`"
        if not fetch_err and not plot_df.empty and cat not in plot_df.columns:
            fetch_err = f"Field `{cat}` not in returned columns."
        if not fetch_err and resolved and not plot_df.empty and resolved not in plot_df.columns:
            fetch_err = f"Field `{resolved}` not in returned columns."
    return cat, fetch_err, plot_df


@app.cell
def _(cat, fetch_err, mo, plot_df, plot_kind, resolved, y_type):
    if fetch_err:
        out = mo.md(f"❌ {fetch_err}")
    elif plot_df.empty:
        out = mo.md("_pick a dataset and group_")
    else:
        import matplotlib.pyplot as plt
        import seaborn as sns
        kind = plot_kind.value
        fig, ax = plt.subplots(figsize=(7, 4))
        order = plot_df[cat].value_counts().index.tolist()[:20]
        if y_type.value == "Count":
            _counts = plot_df[cat].value_counts().reindex(order)
            sns.barplot(x=_counts.index, y=_counts.values, ax=ax,
                        order=order, color="#4c72b0")
            ax.set_ylabel("cell count")
            ax.set_title(f"cells per {cat}", fontsize=10)
        else:
            if kind == "box":
                sns.boxplot(data=plot_df, x=cat, y=resolved, order=order, ax=ax,
                            showfliers=False, color="#4c72b0")
            elif kind == "violin":
                sns.violinplot(data=plot_df, x=cat, y=resolved, order=order, ax=ax,
                               color="#4c72b0")
            else:
                sns.barplot(data=plot_df, x=cat, y=resolved, order=order, ax=ax,
                            color="#4c72b0")
            ax.set_title(f"{resolved} by {cat} — {kind}", fontsize=10)
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.tight_layout()
        out = fig
    out
    return


@app.cell
def _(cat, fetch_err, mo, plot_df, resolved, y_type):
    if fetch_err or plot_df.empty:
        summary = mo.md("")
    else:
        if y_type.value == "Count":
            _ct = plot_df[cat].value_counts()
            _top3 = " · ".join(f"{k}={v:,}" for k, v in _ct.head(3).items())
            summary = mo.md(
                f"<small>**{cat}** · {len(_ct)} unique values, "
                f"top: {_top3}</small>"
            )
        else:
            _stats = (plot_df.groupby(cat)[resolved]
                     .agg(n="count", mean="mean", median="median")
                     .round(3))
            _top3 = " · ".join(
                f"{k}: n={r['n']:.0f} μ={r['mean']:.3g}"
                for k, r in _stats.head(3).iterrows()
            )
            summary = mo.md(
                f"<small>**{resolved} by {cat}** · {len(_stats)} groups · {_top3}</small>"
            )
    summary
    return


@app.cell
def _(db_selector, gene_input, params, y_type):
    if db_selector.value:
        params["db_id"] = db_selector.value
    if y_type.value == "Gene" and gene_input.value.strip():
        params["gene"] = gene_input.value.strip()
    return


if __name__ == "__main__":
    app.run()
