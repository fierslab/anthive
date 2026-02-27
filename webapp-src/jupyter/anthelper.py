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


@lru_cache(32)
def find_database(client, search_string: str | None = None):
    """Search for databases by name or title

    Returns a DataFrame with databases that match the search string.
    The search is case-insensitive and matches against both the database ID and title.

    Args:
        client: AnthiveClient instance
        search_string: Text to search for in database ID or title (case-insensitive)
                      If None, returns all databases

    Returns:
        pandas.DataFrame: DataFrame with columns 'id', 'title', 'n_cells', 'n_genes'
                         showing matching databases

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
        - Search matches substrings, so "cell" will find "single_cell", "bcell", etc.
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

    # Convert to DataFrame with relevant columns
    rv = pd.DataFrame.from_records(rv)[['id', 'title', 'n_cells', 'n_genes']]
    return rv


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
