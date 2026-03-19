import matplotlib.pyplot as plt
import seaborn as sns
import streamlit as st
import util
from components import (categorical_selector, database_selector, gene_selector,
                        numerical_selector, showplot)

import antclient

client = antclient.AnthiveClient()

with st.sidebar:
    db = database_selector(client)
    cat_field = categorical_selector(client, db)

    y_type = util.p_selectbox(
        'Y axis', ['Count', 'Gene', 'Numerical'], key='ytype')

    y_field = None
    if y_type == 'Gene':
        y_field = gene_selector(client, db)
        plot_types = ['box', 'violin', 'bar']
    elif y_type == 'Numerical':
        y_field = numerical_selector(client, db)
        plot_types = ['box', 'violin', 'bar']
    else:
        plot_types = ['bar']

    plot_type = util.p_selectbox('Plot', plot_types, key='plot')


# Fetch data
if y_type == 'Gene':
    gd = client.get_cells(
        db['id'], genes=[y_field], metadata=[cat_field],
        layer=db['layer'], format='dataframe')
elif y_type == 'Numerical':
    gd = client.get_cells(
        db['id'], metadata=[cat_field, y_field],
        layer=db['layer'], format='dataframe')
else:
    gd = client.get_cells(
        db['id'], metadata=[cat_field],
        layer=db['layer'], format='dataframe')


cat_order = gd[cat_field].value_counts().index.tolist()


def plotfunc(ax, plot_type, y_type, cat_field, y_field):
    if y_type == 'Count':
        counts = gd[cat_field].value_counts().reindex(cat_order)
        sns.barplot(x=counts.index, y=counts.values, ax=ax)
        ax.set_xlabel(cat_field)
        ax.set_ylabel('Cell count')
    elif plot_type == 'box':
        sns.boxplot(data=gd, x=cat_field, y=y_field,
                    order=cat_order, ax=ax, showfliers=False)
    elif plot_type == 'violin':
        sns.violinplot(data=gd, x=cat_field, y=y_field,
                       order=cat_order, ax=ax)
    elif plot_type == 'bar':
        sns.barplot(data=gd, x=cat_field, y=y_field,
                    order=cat_order, ax=ax)

    plt.setp(ax.get_xticklabels(), rotation=45, ha='right')


if y_type == 'Count':
    legend = (f"Cell counts per *{cat_field}* category "
              f"from \"{db['groupname']}\" - \"{db['title']}\".")
    basename_suffix = "count"
else:
    legend = (f"{plot_type.capitalize()} plot of *{y_field}* ({y_type.lower()}) "
              f"per *{cat_field}* category "
              f"from \"{db['groupname']}\" - \"{db['title']}\".")
    basename_suffix = y_field


stats_tab, = showplot(
    plotfunc,
    basename=f"{db['groupname']}_{db['title']}_cat_{cat_field}_{basename_suffix}",
    rawdata=gd,
    legend=legend,
    xtabs=['Stats'],
    plot_type=plot_type,
    y_type=y_type,
    cat_field=cat_field,
    y_field=y_field,
)

with stats_tab:
    if y_type == 'Count':
        counts = gd[cat_field].value_counts().rename('count')
        stats_df = counts.reset_index()
        stats_df.columns = [cat_field, 'count']
        stats_df['percent'] = (100 * stats_df['count'] / stats_df['count'].sum()).round(2)
        st.dataframe(stats_df)
    else:
        stats_df = (gd.groupby(cat_field)[y_field]
                    .agg(count='count', mean='mean', std='std', median='median')
                    .round(4))
        st.dataframe(stats_df)
