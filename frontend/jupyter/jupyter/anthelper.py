"""Anthive Helper Functions for Jupyter Notebooks

Convenience functions for working with Anthive in Jupyter notebooks.
Simplifies common tasks like searching databases and displaying metadata.

Functions:
    find_database: Search for databases by name or title
    find_metadata:  Find and display metadata fields for a database

Author: Anthive Team
License: MIT
"""

from functools import lru_cache
from typing import Optional


@lru_cache(32)
def find_database(client,
                  search_string: Optional[str] = None,
                  n: int = 20,
                  verbose: bool = True):
    """Search for databases by name or title.

    Prints matching databases sorted by year (most recent first).
    Results are cached (up to 32 different searches).

    Args:
        client: AnthiveClient instance
        search_string: Substring to search in id, title, group (case-insensitive).
                       None returns the first n databases.
        n: Maximum number of databases to show (default: 20)
        verbose: Show full details per database (default: True)

    Examples:
        >>> find_database(client, "brain")
        >>> find_database(client, "Blackburn")
        >>> find_database(client)           # show all (up to n)
    """
    dbs = client.get_databases()

    if search_string is not None:
        q = search_string.lower()
        dbs = [x for x in dbs
               if q in x['id'].lower()
               or q in x.get('title', '').lower()
               or q in x.get('group', '').lower()]

    if not dbs:
        print("No databases found")
        return

    for r in sorted(dbs, key=lambda x: -x.get('year', 0))[:n]:
        if verbose:
            print(f"{r['id']}")
            print(f"  - group       : {r.get('group', '')}")
            print(f"  - title       : {r.get('title', '')} ({r.get('year', '')})")
            print(f"  - size        : {r.get('n_cells', '?')} cells / {r.get('n_genes', '?')} genes")
            layers = r.get('layers', {})
            layer_names = ', '.join(layers.keys()) if isinstance(layers, dict) else ', '.join(layers)
            print(f"  - layers      : {layer_names}")
            obs = r.get('obs', {})
            numcol = obs.get('numerical', [])
            catcol = obs.get('categorical', [])
            print(f"  - categorical : {len(catcol)} — {', '.join(catcol[:3])}{', ...' if len(catcol) > 3 else ''}")
            print(f"  - numerical   : {len(numcol)} — {', '.join(numcol[:3])}{', ...' if len(numcol) > 3 else ''}")
        else:
            print(r['id'])


def find_metadata(client, db_id: str, search_string: Optional[str] = None):
    """Find and display metadata fields for a database.

    Prints numerical and categorical obs fields, optionally filtered.

    Args:
        client: AnthiveClient instance
        db_id: Database identifier (e.g. "LC5/SPM")
        search_string: Substring to filter field names (case-insensitive).
                       None shows all fields.

    Examples:
        >>> find_metadata(client, "LC5/SPM")
        >>> find_metadata(client, "LC5/SPM", "cell")
        >>> find_metadata(client, "LC5/SPM", "count")
    """
    import textwrap

    md = client.get_metadata_fields(db_id)

    if search_string is not None:
        q = search_string.lower()
        md = {
            'numerical':   [x for x in md['numerical']   if q in x.lower()],
            'categorical': [x for x in md['categorical'] if q in x.lower()],
        }

    nmd = md['numerical']
    cmd = md['categorical']

    if not nmd and not cmd:
        print("No results found")
        return

    def _wrap(fields):
        return "    " + "\n    ".join(textwrap.wrap(", ".join(fields), width=70))

    if nmd:
        print("# Numerical")
        print(_wrap(nmd))
    if cmd:
        print("# Categorical")
        print(_wrap(cmd))
