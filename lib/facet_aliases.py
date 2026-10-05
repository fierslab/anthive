"""Deprecated facet names kept as aliases for one release.

``apoe_carrier`` was renamed ``apoe4_carrier`` (2026-10-05). The old name
keeps working in URLs, facet arguments and stored facet-test results; the
obs views carry an ``ant_apoe_carrier`` alias column. Remove this module's
entries together with the alias column after one release.
"""
from __future__ import annotations

from typing import Dict

#: Deprecated ``ant_*`` column name -> current name.
DEPRECATED_FACET_COLUMNS: Dict[str, str] = {'ant_apoe_carrier': 'ant_apoe4_carrier'}

#: Deprecated bare facet tag -> current tag.
DEPRECATED_FACET_TAGS: Dict[str, str] = {'apoe_carrier': 'apoe4_carrier'}


def canonical_facet(name: str) -> str:
    """Map a deprecated facet name (``ant_`` prefixed or bare) to its current name.

    Args:
        name: A facet column (``ant_apoe_carrier``) or tag (``apoe_carrier``).

    Returns:
        The current name in the same form; unknown names come back unchanged.
    """
    return DEPRECATED_FACET_COLUMNS.get(name) or DEPRECATED_FACET_TAGS.get(name) or name


def is_deprecated_facet_column(name: str) -> bool:
    """True for a deprecated facet column name that exists only as a view alias."""
    return name in DEPRECATED_FACET_COLUMNS
