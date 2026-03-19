
import io
from functools import partial
import matplotlib.pyplot as plt
import streamlit as st
import util
import seaborn as sns


def showplot(func, basename, rawdata=None, legend=None,
             xtabs=None, **kwargs):
    fig = plt.figure()
    ax = fig.gca()

    if xtabs is None:
        xtabs_ = []
    else:
        xtabs_ = xtabs

    if rawdata is not None:
        tabnames = ["Plot", "Data", "Download"] + xtabs_
    else:
        tabnames = ["Plot", "Download"] + xtabs_

    tabs = st.tabs(tabnames)
    if rawdata is not None:
        tab_plot, tab_data, tab_download, *moretabs = tabs
    else:
        tab_plot, tab_download, *moretabs = tabs

    with tab_plot:

        # create the plot!
        func(ax, **kwargs)
        sns.despine()

        st.pyplot(fig)
        if legend is not None:
            st.markdown(f"**Legend:** {legend}")
    if rawdata is not None:
        with tab_data:  # type: ignore
            st.dataframe(rawdata.head(200))

    with tab_download:

        whatlist = ['Image/png', 'Image/svg']
        if rawdata is not None:
            whatlist.append('Raw data/tsv')

        what = st.selectbox('What would you like to download?',
                            whatlist)

        def prep_download_image(fmt):
            # Save to bytes buffer
            buf = io.BytesIO()
            fig.savefig(buf, format=fmt, dpi=180, bbox_inches='tight')
            plt.close(fig)  # Important: close figure to free memory
            buf.seek(0)  # Reset buffer position
            return buf.getvalue()

        def dataframe_to_tsv_bytes():
            """Convert pandas DataFrame to TSV bytes for download"""
            import pandas as pd
            buf = io.StringIO()
            assert isinstance(rawdata, pd.DataFrame)
            rawdata.to_csv(buf, sep='\t', index=False)
            buf.seek(0)
            return buf.getvalue().encode('utf-8')  # Convert string to bytes

        if what == 'Image/png':
            prep_function = partial(prep_download_image, 'png')
            filename = f"{basename}.png"
            mime = "image/png"
        elif what == 'Image/svg':
            prep_function = partial(prep_download_image, 'svg')
            filename = f"{basename}.svg"
            mime = "image/svg+xml"
        elif what == "Raw data/tsv":
            prep_function = dataframe_to_tsv_bytes
            filename = f"{basename}.tsv"
            mime = "text/tsv"
        else:
            st.stop()

        # Single prepare button
        if st.button("Prepare Download", type="primary"):
            with st.spinner("Preparing {what} for download ..."):
                raw = prep_function()
                st.download_button(
                    f'Download {filename}',
                    data=raw, file_name=filename, mime=mime)

    return moretabs


def database_selector(client, databases=None, key: str = "db"):
    """Database selector component.

    Presents group → (optional experiment) → dataset → layer dropdowns.
    Uses URL query params to persist selections across reruns.

    Args:
        client: AnthiveClient instance
        databases: list of database dicts from get_databases(); fetched if None
        key: URL param key prefix (e.g. "db" → params dbg, dbt, dbl)

    Returns:
        Selected database dict with added keys:
            - ``layer``: selected layer name
            - ``groupname``: selected group label
    """
    if databases is None:
        databases = client.get_databases()

    if not databases:
        st.warning("No databases found")
        st.stop()

    # Use friendly group name from collection metadata, fall back to group_id
    def _group(db):
        return db.get('group') or db.get('group_id', 'default')

    groups = sorted(set(_group(db) for db in databases))
    group = util.p_selectbox("Group", groups, f"{key}g")

    databases_ = [x for x in databases if _group(x) == group]

    # Optional experiment sub-grouping (from collection metadata)
    experiments = sorted(set(
        x.get('collection', {}).get('experiment') or ''
        for x in databases_
    ))
    named_exps = [e for e in experiments if e]
    groupname = group
    if len(named_exps) > 1:
        exp = util.p_selectbox("Experiment", named_exps, f"{key}x")
        groupname = f"{group}/{exp}"
        databases_ = [
            x for x in databases_
            if x.get('collection', {}).get('experiment', '') == exp
        ]

    titles = sorted([x['title'] for x in databases_])
    assert len(titles) == len(set(titles))
    title = util.p_selectbox("DB", titles, f"{key}t")

    final_ = [x for x in databases_ if x['title'] == title]
    assert len(final_) == 1
    db = final_[0]

    # Layer selection: layers is a dict {key: description}
    layer_info = db.get('layers', {})
    if not layer_info:
        st.warning("No layers in database")
        st.stop()

    layers = list(layer_info.keys())
    default_layer = db.get('default_layer')
    if len(layers) > 1:
        layer = util.p_selectbox(
            "Layer", layers, f"{key}l",
            default_value=default_layer,
            format_func=lambda x: layer_info[x])
    else:
        layer = layers[0]
        st.write(f"Layer: {layer}")

    db['groupname'] = groupname
    db['layer'] = layer
    return db


def gene_selector(client, database: dict, key: str = "gene"):
    """Gene autocomplete selector component

    Args:
        client: AnthiveClient instance
        database: Selected database dict
        key: URL param key prefix

    Returns:
        Selected gene name
    """
    if not database:
        st.warning("Select a database first")
        st.stop()

    # Load all genes for autocomplete (cached)
    genes = client.search_genes(
        db_id=database['id'],
        q="",
        limit=1e9)

    if not genes:
        st.warning("No genes found in database")
        st.stop()

    gene = util.p_selectbox('Gene', genes, key)

    return gene


def numerical_selector(client, database: dict, key: str = "num"):
    """Numerical field selector component

    Returns:
        Selected numerical metadata field name
    """
    if not database:
        st.warning("Select a database first")
        st.stop()

    mdfields = database.get('obs', {}).get('numerical', [])

    if not mdfields:
        st.warning("No num. fields found in database")
        st.stop()

    num = util.p_selectbox('Num', mdfields, key)
    return num


def categorical_selector(client, database: dict, key: str = "cat"):
    """Categorical field selector component

    Returns:
        Selected categorical metadata field name
    """
    if not database:
        st.warning("Select a database first")
        st.stop()

    mdfields = client.get_metadata_fields(
        db_id=database['id'])['categorical']

    if not mdfields:
        st.warning("No cat. fields found in database")
        st.stop()

    cat = util.p_selectbox('Cat', mdfields, key)
    return cat
