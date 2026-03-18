"""Anthive Client Package

Python client for the Anthive REST API — query single-cell expression databases.

Quick start:
    from antclient import AnthiveClient
    from anthelper import find_database, find_metadata

    client = AnthiveClient()
    find_database(client, "brain")
    find_metadata(client, "LC5/SPM")
    df = client.get_cells("LC5/SPM", genes=["Apoe"], format="dataframe")

Note:
    When installed via pip (py_modules packaging), `antclient` and `anthelper`
    are independent top-level modules. This __init__.py is used only when the
    antclient/ directory is added directly to sys.path during local development.
"""

from .antclient import AnthiveClient, AntClient
from .anthelper import find_database, find_metadata

__version__ = "1.1.3"
__all__ = ["AnthiveClient", "AntClient", "find_database", "find_metadata"]
