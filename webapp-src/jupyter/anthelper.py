"""Anthive Helper Functions for Jupyter Notebooks

This module provides convenience functions for working with Anthive in Jupyter notebooks.
These helper functions simplify common tasks like searching for databases and metadata fields.

Functions:
    find_database: Search for databases by name or title
    find_metadata: Find and display metadata fields for a database

Author: Anthive Team
License: MIT
"""

from functools import lru_cache
import ipywidgets as widgets
from IPython.display import display


@lru_cache(32)
def find_database(client, 
                  search_string: str | None = None,
                  n: int = 20, 
                 verbose: bool = False):
    """Search for databases by name or title

    Returns a DataFrame with databases that match the search string.
    The search is case-insensitive and matches against both the database ID 
    and title.

    Note, always prints the first `n` databases

    Args:
        client: AnthiveClient instance
        n: No db's to return
        verbose: show more information
        search_string: Text to search for in database ID or title 
                       (case-insensitive). If None, returns the 20 
                       databases.

    Returns:
        None

    Examples:
        >>> # Find all databases with "brain" in the name
        >>> brain_dbs = find_database(client, "brain")
        >>>
        >>> # Find databases by a specific author/study
        >>> blackburn_dbs = find_database(client, "Blackburn")
        >>>
        >>> # Get all available databases
        >>> all_dbs = find_database(client, None)

    Notes:
        - Results are cached for performance (up to 32 different searches)
        - Search matches substrings, so "cell" will find 
          "single_cell", "bcell", etc.
        - Both database ID and title are searched

    See also:
        - client.get_databases() for the raw API call
        - Pandas DataFrame documentation: https://pandas.pydata.org/docs/
    """
    import pandas as pd

    # Get all databases from API
    dbs = client.get_databases()

    # Filter by search string if provided
    if search_string is not None:
        search_string = search_string.lower()
        rv = [x for x in dbs
              if (search_string in x['id'].lower()
                  or search_string in x['title'].lower())
             ]
    else:
        rv = dbs

    if len(rv) == 0:
        print("No databases found")
        return

    for r in sorted(rv, key=lambda x: -x.get('year', 0))[:n]:
        if verbose:
            print(f"{r['id']}")
            print(f"  - title  : {r['title']} ({r['year']})")
            print(f"  - size   : {r['n_cells']} cells / {r['n_genes']} genes")
            layers = ", ".join(r['layers'].keys())
            print(f"  - layers : {layers}")
            numcol = r['obs']['numerical']
            catcol = r['obs']['categorical']
            print(f"  - categ  : {len(catcol)} - {', '.join(catcol[:4])}, ...")
            print(f"  - numer  : {len(numcol)} - {', '.join(numcol[:4])}, ...")
        else:
            print(r['id'])
#    # Convert to DataFrame with relevant columns
#    rv = pd.DataFrame.from_records(rv)[['id', 'title', 'n_cells', 'n_genes']]
#    return rv


def find_metadata(client, db_id: str, search_string: str | None = None):
    """Find and display metadata fields for a database

    Retrieves metadata fields (both numerical and categorical) from a database
    and displays them in a readable, wrapped format. Optionally filters by search string.

    Args:
        client: AnthiveClient instance
        db_id: Database identifier (e.g., "Blackburn2026/seurat_obj_final")
        search_string: Optional text to search for in metadata field names
                      (case-insensitive). If None, shows all metadata fields.

    Returns:
        None (prints formatted output to console)

    Output Format:
        Prints two sections:
        - "# Numerical" - fields with numeric values (counts, scores, fractions)
        - "# Categorical" - fields with categories (cell types, conditions, batches)

    Examples:
        >>> # Show all metadata fields
        >>> find_metadata(client, "Blackburn2026/seurat_obj_final")

        >>> # Find metadata fields related to cell types
        >>> find_metadata(client, "mydb", "cell")

        >>> # Find quality control metrics
        >>> find_metadata(client, "mydb", "count")

    Metadata Field Types:
        Numerical fields typically include:
        - nCount_RNA, nFeature_RNA: Number of RNA molecules/features per cell
        - percent.mt: Percentage of mitochondrial genes
        - Quality control metrics
        - Dimensionality reduction scores

        Categorical fields typically include:
        - cell_type, cell_state: Cell classification
        - tissue, organ: Anatomical location
        - sample, batch: Experimental groupings
        - condition, treatment: Experimental conditions

    Notes:
        - Output is wrapped to fit readable line lengths
        - If no matches found, prints "No results found"
        - Search is case-insensitive and matches substrings

    See also:
        - client.get_metadata_fields() for the raw API call
        - Single-cell metadata standards: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10653056/

    Resources for Understanding Metadata:
        - Cell Ontology: http://www.obofoundry.org/ontology/cl.html
        - Single Cell Portal: https://singlecell.broadinstitute.org/
    """
    import textwrap

    # Get metadata fields from API
    md = client.get_metadata_fields(db_id)

    # Filter by search string if provided
    if search_string is not None:
        search_string = search_string.lower()
        md = dict(
            numerical=[x for x in md['numerical']
                       if search_string in x.lower()],
            categorical=[x for x in md['categorical']
                       if search_string in x.lower()],
        )

    nmd = md['numerical']
    cmd = md['categorical']

    # Check if any results found
    if len(nmd) == 0 and len(cmd) == 0:
        print("No results found")
        return

    def pwrap(l):
        """Helper to wrap long lists of fields"""
        return "\n".join(
            textwrap.wrap(
                ", ".join(l),
                width=70))

    # Print results in formatted sections
    if len(nmd) > 0:
        print("# Numerical")
        print(pwrap(nmd))
    if len(cmd) > 0:
        print("# Categorical")
        print(pwrap(cmd))



        
# import ipywidgets as widgets
# from IPython.display import display

# def database_selector(client, databases=None, key: str = "db"):
#     """
#     Database selector component using ipywidgets.
#     Returns an output widget; access .result for the selected db dict.
#     """
#     if databases is None:
#         databases = client.get_databases()

#     if not databases:
#         print("No databases found")
#         return None

#     # Validate and extract groups
#     for db_item in databases:
#         if 'collection' not in db_item:
#             print(f"Invalid db structure: {db_item}")
#             return None

#     groups = sorted(
#         set(db['collection'].get('group', 'default') 
#             for db in databases))

#     # --- Widgets ---
#     w_group = widgets.Dropdown(
#         description="Group:", options=groups, 
#         layout=widgets.Layout(width='400px'))
#     w_exp = widgets.Dropdown(
#         description="Experiment:", 
#         layout=widgets.Layout(width='400px'))
#     w_db = widgets.Dropdown(
#         description="DB:", 
#         layout=widgets.Layout(width='400px'))
#     w_layer = widgets.Dropdown(
#         description="Layer:", 
#         layout=widgets.Layout(width='400px'))

#     exp_box   = widgets.VBox([w_exp])    # hidden until needed
#     layer_box = widgets.VBox([w_layer])  # hidden until needed

#     output = widgets.Output()
#     container = widgets.VBox([w_group, exp_box, w_db, layer_box, output])

#     # Store result on the container for external access
#     container.result = None

#     def get_filtered_dbs(group, exp=None):
#         dbs = [x for x in databases 
#                if x['collection'].get('group') == group]
#         if exp is not None:
#             dbs = [x for x in dbs 
#                    if x['collection'].get('experiment') == exp]
#         return dbs

#     def update_experiments(change=None):
#         group = w_group.value
#         filtered = get_filtered_dbs(group)
#         exps = sorted(set(x['collection'].get('experiment', 'na') 
#                           for x in filtered))

#         if len(exps) > 1:
#             w_exp.options = exps
#             exp_box.layout.display = ''       # show
#         else:
#             w_exp.options = exps if exps else ['na']
#             exp_box.layout.display = 'none'   # hide

#         update_titles()

#     def update_titles(change=None):
#         group = w_group.value
#         exp   = w_exp.value if exp_box.layout.display != 'none' else None
#         filtered = get_filtered_dbs(group, exp)

#         titles = sorted(x['title'] for x in filtered)
#         assert len(titles) == len(set(titles)), "Duplicate titles found"
#         w_db.options = titles if titles else ['']

#         update_layers()

#     def update_layers(change=None):
#         group = w_group.value
#         exp   = w_exp.value if exp_box.layout.display != 'none' else None
#         filtered = get_filtered_dbs(group, exp)

#         title_matches = [x for x in filtered if x['title'] == w_db.value]
#         if not title_matches:
#             return

#         db = title_matches[0]
#         layer_info = db.get('layers', {})
#         layers = list(layer_info.keys())

#         if len(layers) > 1:
#             w_layer.options = layers
#             w_layer.style.description_width = 'initial'
#             # Show display names as labels, values as keys
#             w_layer._options_labels = [layer_info[l] for l in layers]
#             w_layer.options = [(layer_info[l], l) for l in layers]
#             layer_box.layout.display = ''
#         elif len(layers) == 1:
#             w_layer.options = [(layers[0], layers[0])]
#             layer_box.layout.display = 'none'
#             with output:
#                 output.clear_output()
#                 print(f"Layer: {layers[0]}")
#         else:
#             with output:
#                 output.clear_output()
#                 print("Warning: No layers in database")
#             return

#         update_result()

#     def update_result(change=None):
#         group = w_group.value
#         exp_shown = exp_box.layout.display != 'none'
#         exp   = w_exp.value if exp_shown else None
#         filtered = get_filtered_dbs(group, exp)

#         title_matches = [x for x in filtered if x['title'] == w_db.value]
#         if not title_matches:
#             container.result = None
#             return

#         db = title_matches[0].copy()
#         groupname = group
#         if exp_shown and exp:
#             groupname = f"{group}/{exp}"

#         layer = w_layer.value
#         db['groupname'] = groupname
#         db['layer'] = layer
#         container.result = db

#     # Wire up observers
#     w_group.observe(update_experiments, names='value')
#     w_exp.observe(update_titles,        names='value')
#     w_db.observe(update_layers,         names='value')
#     w_layer.observe(update_result,      names='value')

#     # Initialize
#     update_experiments()

#     display(container)
#     return container