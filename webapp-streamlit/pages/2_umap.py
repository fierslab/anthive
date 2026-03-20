import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import seaborn as sns
import streamlit as st
import util
from components import (categorical_selector, database_selector, gene_selector,
                        numerical_selector, showplot)

import antclient

client = antclient.AnthiveClient()  # Auto-detects from environment or browser location

with st.sidebar:
    databases = client.get_databases()
    # Filter databases with embeddings (check if embeddings dict exists and is not empty)
    db2 = [x for x in databases
        if x.get('embeddings') and len(x.get('embeddings', {})) > 0]
    db = database_selector(client, databases=db2)

    # Get embeddings dict from new API format
    embeddings = client.get_embeddings(db['id'])
    # embeddings is now a dict like {"X_umap": {"name": "Umap", "dimensions": 2}}
    emname = {key: info['name'] for key, info in embeddings.items()}
    emb_name = util.p_selectbox(
        'Embedding',
        options=list(embeddings.keys()),
        key='emb', default_value='X_umap',
        format_func=lambda x: emname.get(x, x))


data = client.get_embedding_data(
    db['id'], emb_name, format='dataframe')
data = data.set_index(data.columns[0])


ekey0 = data.columns[-2]
ekey1 = data.columns[-1]

with st.sidebar:
    colorby = util.p_selectbox(
        "Color by", key='colby',
        options=['Gene', 'Numerical', 'Categorical']
    )


colorfield: str = ""
if colorby == 'Gene':
    with st.sidebar:
        colorfield = gene_selector(client, db)
    gd = client.get_cells(
        db['id'], genes=[colorfield], format='dataframe' )
elif colorby == 'Categorical':
    with st.sidebar:
        colorfield = categorical_selector(client, db)
    gd = client.get_cells(
        db['id'], metadata=[colorfield], format='dataframe')
elif colorby == 'Numerical':
    with st.sidebar:
        colorfield = numerical_selector(client, db)
    gd = client.get_cells(
        db['id'], metadata=[colorfield],
        format='dataframe')
else:
    st.stop()


gd = gd.set_index(gd.columns[0])
data[colorfield] = gd


def plotfunc(ax):
    kwargs = dict(x=ekey0, y=ekey1, linewidth=0, ax=ax,
                  hue=colorfield)

    with st.expander("Plot Tweaks", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            kwargs['s'] = util.p_number_input(
                'Point size', value=3, min_value=1,
                max_value=100, key='ps')
        with c2:
            kwargs['alpha'] = util.p_number_input(
                'Alpha', key='a', value=0.9, max_value=1.0,
                min_value=0.0)

    if colorby in ['Gene', 'Numerical']:
        kwargs['palette'] = 'viridis_r'
        kwargs['legend'] = False
        kwargs['norm'] = mcolors.Normalize(
            vmin=data[colorfield].quantile(0.05),
            vmax=data[colorfield].quantile(0.95))
        sm = cm.ScalarMappable(cmap="viridis",
                               norm=kwargs['norm'])

    sns.scatterplot(data, **kwargs)
    if colorby in ['Gene', 'Numerical']:
        ax.figure.colorbar(sm, ax=ax, label=colorfield)
    elif colorby == 'Categorical':
        sns.move_legend(ax, loc='upper left', bbox_to_anchor=(1, 1),
                        frameon=False, title=colorfield, fontsize=8)


showplot(
    plotfunc,
    basename=f"{db['groupname']}_{db['title']}_umap",
    rawdata=data,
)
