import matplotlib.pyplot as plt
import numpy as np
import streamlit as st
import util
from components import (categorical_selector, database_selector, gene_selector,
                        numerical_selector, showplot)
from scipy import stats

import antclient

client = antclient.AnthiveClient()  # Auto-detects from environment or browser location

with st.sidebar:
    databases = client.get_databases()
    # Filter databases that have at least one embedding
    db2 = [x for x in databases if x.get('embeddings')]
    db = database_selector(client, databases=db2)

    getcellkwargs = {}

    sorton = util.p_selectbox(
        "Sort on", key='sortby',
        options=['Numerical', 'Gene'])
    if sorton == 'Gene':
        sortfield = gene_selector(client, db, key='gen2')
        getcellkwargs['genes'] = [sortfield]
    elif sorton == 'Numerical':
        sortfield = numerical_selector(client, db, key='num2')
        getcellkwargs['metadata'] = [sortfield]

    ploton = util.p_selectbox(
        "Plot", key='ploton',
        options=['Numerical', 'Gene'])
    if ploton == 'Gene':
        plotfield = gene_selector(client, db)
        getcellkwargs['genes'] = getcellkwargs.get('genes', []) + [plotfield]
    elif ploton == 'Numerical':
        plotfield = numerical_selector(client, db)
        getcellkwargs['metadata'] = getcellkwargs.get('metadata', []) + [plotfield]

df = client.get_cells(
    db['id'], layer=db['layer'], format='dataframe', **getcellkwargs)

df = (df
      .set_index(df.columns[0])
      .sort_values(by=sortfield))
df['__x'] = range(len(df))


def plotfunc(ax):
    kwargs = dict()
    with st.expander("Plot Tweaks", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            rankify = st.toggle('rank X', value=True)
        with c2:
            windowsize = util.p_number_input(
                'Window size', key='ws',
                min_value=5, max_value=len(df) // 2,
                value=len(df) // 10)
            kwargs['alpha'] = util.p_number_input(
                'Alpha', key='a', value=0.9, max_value=1.0,
                min_value=0.0)

    roll = df[plotfield].rolling(windowsize, center=True, min_periods=4)
    df['__roll_mean'] = roll.mean()
    df['__roll_std'] = roll.std()
    df['__roll_n'] = roll.count()

    df["__roll_ci95"] = df.apply(
        lambda r: stats.t.ppf(0.975, r["__roll_n"] - 1) * r["__roll_std"] / np.sqrt(r["__roll_n"])
        if r["__roll_n"] > 1 else np.nan,
        axis=1,
    )
    xcol = '__x' if rankify else sortfield

    ax.fill_between(
        df[xcol],
        df["__roll_mean"] - df["__roll_ci95"],
        df["__roll_mean"] + df["__roll_ci95"],
        alpha=0.3,
        label="95% CI",
    )

    ax.plot(df[xcol], df["__roll_mean"], label=f"{plotfield} (smooth)")

    ax.set_xlabel(sortfield)
    ax.set_ylabel(plotfield)
    ax.legend()


showplot(
    plotfunc,
    basename=f"{db['groupname']}_{db['title']}_rolling",
    rawdata=df,
)
