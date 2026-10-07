"""Coarse tissue groups for the ``ant_tissue`` facet and the specific UBERON columns.

``ant_tissue`` is a coarse, comparable facet (``prefrontal cortex``, ``temporal cortex``, ``hippocampus``, ...). Every
dataset also keeps the most specific UBERON term it can support in two plain columns, ``tissue_uberon`` (label) and
``tissue_uberon_id``, which are never facets. The lookup table is ``lib/tissue_groups.yaml`` (one list of specific labels
per group, with UBERON ids); labels are matched case-insensitively with underscores read as spaces.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import yaml

TABLE_PATH = Path(__file__).resolve().parent / 'tissue_groups.yaml'


class TissueEntry(NamedTuple):
    """One specific label: its group, UBERON label and UBERON id (None when the term has no id)."""

    group: str
    label: str
    uberon_id: Optional[str]


def normalise_label(label: str) -> str:
    """Lower-case a label and read underscores as spaces, so ``Cerebral_cortex`` matches ``cerebral cortex``."""
    return ' '.join(str(label).replace('_', ' ').lower().split())


@lru_cache(maxsize=1)
def _table() -> Tuple[Dict[str, TissueEntry], Tuple[str, ...]]:
    with open(TABLE_PATH, encoding='utf-8') as handle:
        data = yaml.safe_load(handle)
    by_label: Dict[str, TissueEntry] = {}
    for group, entries in data['groups'].items():
        for entry in entries:
            key = normalise_label(entry['label'])
            if key in by_label:
                raise ValueError(f'tissue label {entry["label"]!r} is in two groups')
            by_label[key] = TissueEntry(group, entry['label'], entry.get('uberon_id'))
    return by_label, tuple(data['groups'])


def coarse_terms() -> List[str]:
    """The group terms ``ant_tissue`` may carry, in table order."""
    return list(_table()[1])


def lookup(label: Optional[str]) -> Optional[TissueEntry]:
    """The table entry of a specific label, or None when the label is empty or unknown."""
    if label is None or not str(label).strip():
        return None
    return _table()[0].get(normalise_label(label))


def coarse_tissue(label: Optional[str]) -> Optional[str]:
    """Group term of a specific label (None when unknown)."""
    entry = lookup(label)
    return entry.group if entry else None


def uberon_id(label: Optional[str]) -> Optional[str]:
    """UBERON id of a specific label (None when unknown or the term has no id)."""
    entry = lookup(label)
    return entry.uberon_id if entry else None
