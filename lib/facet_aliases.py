"""The retired facet name ``apoe_carrier`` (renamed ``apoe4_carrier`` on 2026-10-05).

API 2.43.0 kept the old name as an alias for one release; 2.45.0 removes it
(`doc/API_CHANGELOG.md`). What is left:

* ``canonical_facet`` no longer maps anything: a request naming
  ``apoe_carrier`` / ``ant_apoe_carrier`` gets the same answer as any other
  unknown facet. The function stays so callers keep one place to hook a
  future rename into.
* ``DEPRECATED_FACET_COLUMNS`` lets the facet listings hide an
  ``ant_apoe_carrier`` alias column that views built before 2.45 still carry
  (the views stop creating it; it disappears when a dataset's views are
  rebuilt).
* ``DEPRECATED_FACET_TAGS`` lets ``facet_results`` read result files written
  before the rename under the current name (stored data, not a request alias).
"""
from __future__ import annotations

from typing import Dict

#: Retired ``ant_*`` column name -> current name (only to hide leftover view columns).
DEPRECATED_FACET_COLUMNS: Dict[str, str] = {'ant_apoe_carrier': 'ant_apoe4_carrier'}

#: Retired bare facet tag -> current tag (only to read old result files).
DEPRECATED_FACET_TAGS: Dict[str, str] = {'apoe_carrier': 'apoe4_carrier'}


def canonical_facet(name: str) -> str:
    """The facet name to use for a request: the name itself (the alias is retired).

    Args:
        name: A facet column (``ant_apoe4_carrier``) or tag (``apoe4_carrier``).

    Returns:
        ``name`` unchanged.
    """
    return name


def is_deprecated_facet_column(name: str) -> bool:
    """True for a retired facet column name that can only exist as a leftover view alias."""
    return name in DEPRECATED_FACET_COLUMNS
