
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

    # Support deep-linking via short db_id (e.g. ?db=S25/SGM)
    default_group_id = None
    default_exp = None
    default_file_id = None
    db_id_param = st.query_params.get(key)
    if db_id_param:
        matched = [x for x in databases if x.get('id') == db_id_param]
        if matched:
            default_group_id = matched[0].get('group_id') or matched[0]['id'].split('/')[0]
            default_exp = matched[0]['collection'].get('experiment')
            default_file_id = matched[0].get('file_id') or matched[0]['id'].split('/')[1]

    # Build group_id → long display name mapping
    group_id_to_name: dict = {}
    for db_item in databases:
        if 'collection' not in db_item:
            st.warning("invalid db structure")
            st.write(db_item)
            st.stop()
        gid = db_item.get('group_id') or db_item['id'].split('/')[0]
        long_name = db_item['collection'].get('group', gid)
        group_id_to_name[gid] = long_name

    group_ids = sorted(group_id_to_name.keys())
    selected_group_id = util.p_selectbox(
        "Group", group_ids, f"{key}g",
        default_value=default_group_id,
        format_func=lambda gid: group_id_to_name[gid],
    )

    groupname = group_id_to_name[selected_group_id]

    databases_ = [
        x for x in databases
        if (x.get('group_id') or x['id'].split('/')[0]) == selected_group_id
    ]

    # Resolve default_file_id from group metadata (only if not set from URL param)
    if default_file_id is None:
        coll_default = databases_[0]['collection'].get('default_dataset') if databases_ else None
        if coll_default and any(x['title'] == coll_default for x in databases_):
            default_file_id = next(
                x.get('file_id') or x['id'].split('/')[1]
                for x in databases_ if x['title'] == coll_default
            )
        else:
            # Fall back to most recently dated dataset
            dated = sorted(databases_, key=lambda x: x.get('date') or '', reverse=True)
            if dated:
                default_file_id = dated[0].get('file_id') or dated[0]['id'].split('/')[1]

    exps = sorted(set([x['collection'].get('experiment', 'na')
                       for x in databases_]))
    if len(exps) > 1:
        exp = util.p_selectbox("Experiment", exps, f"{key}x", default_value=default_exp)
        groupname = f"{groupname}/{exp}"

        databases_ = [
            x for x in databases_
            if x['collection'].get('experiment') == exp
        ]

    # Build file_id → title mapping for the DB selectbox
    file_id_to_title = {
        (x.get('file_id') or x['id'].split('/')[1]): x['title']
        for x in databases_
    }
    file_ids = sorted(file_id_to_title.keys())
    selected_file_id = util.p_selectbox(
        "DB", file_ids, f"{key}t",
        default_value=default_file_id,
        format_func=lambda fid: file_id_to_title[fid],
    )

    final_ = [
        x for x in databases_
        if (x.get('file_id') or x['id'].split('/')[1]) == selected_file_id
    ]
    assert len(final_) == 1
    db = final_[0]

    layer_info = db['layers']
    if len(layer_info) == 0:
        st.warning("No layers in database?")
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
