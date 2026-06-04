import matplotlib.pyplot as plt
import seaborn as sns
import streamlit as st
import util
from components import (categorical_selector, database_selector,
                        numerical_selector, showplot)

import antclient

client = antclient.AnthiveClient()

with st.sidebar:
    db = database_selector(client)
    num_field = numerical_selector(client, db)

    group_by = util.p_selectbox(
        'Group by', ['None', 'Categorical'], key='grp')

    cat_field = None
    if group_by == 'Categorical':
        cat_field = categorical_selector(client, db)
        plot_types = ['box', 'violin', 'bar']
    else:
        plot_types = ['ecdf', 'kde', 'histogram']

    plot_type = util.p_selectbox('Plot', plot_types, key='plot')


# Fetch data
if cat_field:
    gd = client.get_cells(
        db['id'], metadata=[num_field, cat_field],
        format='dataframe')
else:
    gd = client.get_cells(
        db['id'], metadata=[num_field],
        format='dataframe')


def plotfunc(ax, plot_type, num_field, cat_field):
    if cat_field:
        cat_order = gd[cat_field].value_counts().index.tolist()
        if plot_type == 'box':
            sns.boxplot(data=gd, x=cat_field, y=num_field,
                        order=cat_order, ax=ax, showfliers=False)
        elif plot_type == 'violin':
            sns.violinplot(data=gd, x=cat_field, y=num_field,
                           order=cat_order, ax=ax)
        elif plot_type == 'bar':
            sns.barplot(data=gd, x=cat_field, y=num_field,
                        order=cat_order, ax=ax)
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
    elif plot_type == 'ecdf':
        sns.ecdfplot(gd, x=num_field, ax=ax)
    elif plot_type == 'kde':
        sns.kdeplot(gd, x=num_field, ax=ax)
    elif plot_type == 'histogram':
        with st.sidebar:
            nobins = util.p_number_input(
                "Bins", key="bins",
                min_value=3, max_value=100, value=20)
        sns.histplot(gd, x=num_field, ax=ax, bins=nobins)


if cat_field:
    legend = (f"{plot_type.capitalize()} plot of *{num_field}* (numerical) "
              f"per *{cat_field}* category "
              f"from \"{db['groupname']}\" - \"{db['title']}\".")
    basename_suffix = f"{num_field}_by_{cat_field}"
else:
    legend = (f"{plot_type.capitalize()} plot of *{num_field}* (numerical) "
              f"from \"{db['groupname']}\" - \"{db['title']}\".")
    basename_suffix = num_field


stats_tab, = showplot(
    plotfunc,
    basename=f"{db['groupname']}_{db['title']}_num_{basename_suffix}",
    rawdata=gd,
    legend=legend,
    xtabs=['Stats'],
    plot_type=plot_type,
    num_field=num_field,
    cat_field=cat_field,
)

with stats_tab:
    import pandas as pd
    if cat_field:
        stats_df = (gd.groupby(cat_field)[num_field]
                    .agg(count='count', mean='mean', std='std', median='median')
                    .round(4))
        st.dataframe(stats_df)
    else:
        stats = pd.Series({
            "Field": num_field,
            "Count": f"{len(gd):,d}",
            "Mean": f"{gd[num_field].mean():.3g}",
            "Std": f"{gd[num_field].std():.3g}",
            "Median": f"{gd[num_field].median():.3g}",
            "Min": f"{gd[num_field].min():.3g}",
            "10% quantile": f"{gd[num_field].quantile(0.1):.3g}",
            "90% quantile": f"{gd[num_field].quantile(0.9):.3g}",
            "Max": f"{gd[num_field].max():.3g}",
        })
        stats.name = 'value'
        st.write(stats)
