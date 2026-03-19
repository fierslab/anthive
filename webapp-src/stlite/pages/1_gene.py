import seaborn as sns
import streamlit as st
import util
from components import database_selector, gene_selector, showplot

import antclient

client = antclient.AnthiveClient()  # Auto-detects from environment or browser location

with st.sidebar:
    db = database_selector(client)
    gene = gene_selector(client, db)

    gd = client.get_cells(
        db['id'], layer=db['layer'], genes=[gene], format='dataframe'
    )

    plot_type = util.p_selectbox('Plot', ['ecdf', 'kde', 'histogram'], key='plot')

legend = f"""
{plot_type.capitalize()} plot of *{gene}* gene in layer "{db['layer']}"
from the "{db['groupname']}" - "{db['title']}" database.
"""

def plotfunc(ax, plot_type):

    if plot_type == 'ecdf':
        sns.ecdfplot(gd, x=gene, ax=ax)
    elif plot_type == 'kde':
        sns.kdeplot(gd, x=gene, ax=ax)
    elif plot_type == 'histogram':
        with st.sidebar:
            nobins = util.p_number_input(
                "Bins", key="bins",
                min_value=3, max_value=100, value=5)

        sns.histplot(gd, x=gene, ax=ax, bins=nobins)

stat_tab, = showplot(
    plotfunc,
    basename=f"{db['groupname']}_{db['title']}_gene_{gene}",
    rawdata=gd,
    legend=legend,
    xtabs=['Stats'],
    plot_type=plot_type)

with stat_tab:
    import pandas as pd
    nonzero = gd[gd[gene] > 0]

    stats = pd.Series({
        "Gene": gene,
        "No entries": f"{len(gd):,d}",
        "No non zero": f"{len(nonzero):,d}",
        "% non zero": f"{100 * nonzero.shape[0] / gd.shape[0]:.2f}%",
        "Mean": f"{gd[gene].mean():.3g}",
        "Std": f"{gd[gene].std():.3g}",
        "Median": f"{gd[gene].median():.3g}",
        "Min": f"{gd[gene].min():.3g}",
        "10% quantile": f"{gd[gene].quantile(0.1):.3g}",
        "90% quantile": f"{gd[gene].quantile(0.9):.3g}",
        "Max": f"{gd[gene].max():.3g}",
    })
    stats.name = 'value'
    st.write(stats)
