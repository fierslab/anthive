
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


def database_selector(client, databases=None,
                      key: str = "db"):
    """Database selector component

    Args:
        client: AntClient instance
        key: Unique key for the selectbox

    Returns:
        Selected database dict or None
    """

    if databases is None:
        databases = client.get_databases()

    if not databases:
        st.warning("No databases found")
        st.stop()

    # Add groupname and shortname fields by parsing id
    groups = set()
    for db_item in databases:
        if not 'collection' in db_item:
            st.warning("invalid db structure")
            st.write(db_item)
            st.stop()

        groups.add(db_item['collection'].get('group', 'default'))

    groups = sorted(groups)
    group = util.p_selectbox("Group", groups, f"{key}g")

    groupname = group

    databases_ = [
            x for x in databases
            if x['collection'].get('group') == group]

    exps = sorted(set([x['collection'].get('experiment', 'na')
                       for x in databases_]))
    if len(exps) > 1:
        exp = util.p_selectbox("Experiment", exps, f"{key}x")
        groupname = f"{groupname}/{exp}"

        databases_ = [
                x for x in databases_
                if x['collection'].get('experiment') == exp]

    titles = sorted([x['title'] for x in databases_])
    assert len(titles) == len(set(titles))
    title = util.p_selectbox("DB", titles, f"{key}t")

    final_ = [x for x in databases_ if x['title'] == title]
    assert len(final_) == 1
    db = final_[0]

    layer_info = db['layers']
    if len(layer_info) == 0:
        st.warning("No layers in database?")
        st.stop()

    layers = list(layer_info.keys())
    if len(layers) > 1:
        layer = util.p_selectbox(
            "Layer", layers, f"{key}l",
            format_func = lambda x: layer_info[x])
    else:
        layer = layers[0]
        st.write(f"Layer: {layer}")

    # patch the db object
    db['groupname'] = groupname
    db['layer'] = layer
    return db


def gene_selector(client, database: dict, key: str = "gene"):
    """Gene autocomplete selector component

    Args:
        client: AntClient instance
        database: Selected database dict
        key: Unique key for the selectbox

    Returns:
        Selected gene name or None
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
    """Numerical autocomplete selector component

    Returns:
        Selected cat name or None
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
    """Categorical autocomplete selector component

    Returns:
        Selected cat name or None
    """
    if not database:
        st.warning("Select a database first")
        st.stop()

    # Load all genes for autocomplete (cached)
    mdfields = client.get_metadata_fields(
        db_id=database['id'])['categorical']

    if not mdfields:
        st.warning("No cat. fields found in database")
        st.stop()

    cat = util.p_selectbox('Cat', mdfields, key)
    return cat


# def categorical_selector(client: AntClient, database: dict, key: str = "categorical"):
#     """Categorical field selector component

#     Args:
#         client: AntClient instance
#         database: Selected database dict
#         key: Unique key for the selectbox

#     Returns:
#         Selected categorical field name or None
#     """
#     if not database:
#         st.warning("Select a database first")
#         return None

#     # Get categorical fields (cached)
#     metadata_fields = _fetch_metadata_fields(client, database['id'])
#     fields = metadata_fields.get('categorical', [])

#     if not fields:
#         st.warning("No categorical fields found in this database")
#         return None

#     # Get initial value from URL params
#     url_params = st.query_params
#     initial_category = url_params.get("category")
#     initial_index = 0

#     if initial_category and initial_category in fields:
#         initial_index = fields.index(initial_category)

#     selected = st.selectbox(
#         "Group By",
#         options=fields,
#         index=initial_index,
#         key=key
#     )

#     return selected
