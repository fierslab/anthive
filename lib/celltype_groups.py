"""Map facet-test stratum labels to cell type groups for replication counting.

The table is ``lib/celltype_groups.yaml`` (documented there and in
``doc/DESIGN_facet_testing.md``). ``group_of(label)`` returns the group of a
raw stratum label; ``lib/facet_results.py`` counts replication per group and
keeps the raw label on every unit.

Matching order: the exact label, the label folded to lower case, the
longest matching prefix; anything else is ``unmapped``.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

TABLE_PATH = Path(__file__).resolve().parent / 'celltype_groups.yaml'
UNMAPPED = 'unmapped'


@lru_cache(maxsize=1)
def load_table() -> Dict[str, Any]:
    """The parsed table, validated: every label and prefix names a group."""
    with open(TABLE_PATH) as handle:
        table = yaml.safe_load(handle) or {}
    groups = table.get('groups') or {}
    if UNMAPPED not in groups:
        raise ValueError(f'{TABLE_PATH}: the group {UNMAPPED!r} must be defined')
    for section in ('labels', 'prefixes'):
        for key, group in (table.get(section) or {}).items():
            if group not in groups:
                raise ValueError(f'{TABLE_PATH}: {section}[{key!r}] names unknown group {group!r}')
    folded: Dict[str, str] = {}
    for label, group in (table.get('labels') or {}).items():
        folded.setdefault(str(label).casefold(), group)
    table['_folded'] = folded
    table['_prefixes'] = sorted(((str(p), g) for p, g in (table.get('prefixes') or {}).items()),
                                key=lambda item: -len(item[0]))
    return table


def group_of(label: Any) -> str:
    """Group of one raw stratum label (``unmapped`` when nothing matches)."""
    if label is None:
        return UNMAPPED
    text = str(label).strip()
    table = load_table()
    labels = table.get('labels') or {}
    if text in labels:
        return labels[text]
    folded = table['_folded'].get(text.casefold())
    if folded is not None:
        return folded
    for prefix, group in table['_prefixes']:
        if text.startswith(prefix):
            return group
    return UNMAPPED


def group_info(group: str) -> Dict[str, Optional[str]]:
    """``{group, cl_id, cl_label, note}`` of a group (None fields when absent)."""
    entry = (load_table().get('groups') or {}).get(group) or {}
    return {'group': group, 'cl_id': entry.get('cl_id'), 'cl_label': entry.get('cl_label'),
            'note': entry.get('note')}


def groups() -> List[str]:
    """Group names in table order."""
    return list((load_table().get('groups') or {}).keys())


__all__ = ['TABLE_PATH', 'UNMAPPED', 'load_table', 'group_of', 'group_info', 'groups']
