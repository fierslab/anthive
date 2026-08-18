"""Canonical annotation columns — the `ant_*` prefix contract.

Canonical columns are anthive-managed, cross-dataset standardised
annotations exposed in the ``obsnum`` / ``obscat`` VIEWs. They are
declared per-dataset via ``api.obs_rename`` / ``api.obs_derive`` in each
``.md`` frontmatter, and are named ``ant_<tag>`` where ``<tag>`` is the
short semantic identifier from ``lib/obscol_tags_vocab.yaml``.

The user's original ``.h5ad`` obs columns are **never overwritten** —
they remain in the physical ``__obscat`` / ``__obsnum`` tables under
their original names and are exposed alongside their ``ant_*`` alias.

See ``doc/DESIGN_canonical_columns.md`` for the migration rationale.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Set


#: The canonical tags. Kept in sync with ``obscol_tags_vocab.yaml``.
#: Every tag here has a matching ``ant_<tag>`` view column when the
#: dataset's ``.md`` declares the mapping in ``api.obs_rename`` or
#: ``api.obs_derive``.
CANONICAL_TAGS: tuple[str, ...] = (
    'braak',
    'braak_group',
    'sex',
    'age',
    'cluster',
    'disease',
    'condition',
    'pmi',
    'donor',
    'apoe',
    'region',
    'genotype',
    'host_sex',
)

#: Numerical (ordinal / continuous) tags. Rest are categorical.
NUMERICAL_TAGS: frozenset[str] = frozenset({'age', 'braak', 'pmi'})

#: Categorical tags — everything else in CANONICAL_TAGS.
CATEGORICAL_TAGS: frozenset[str] = frozenset(CANONICAL_TAGS) - NUMERICAL_TAGS


#: One-off pre-migration aliases. When a legacy ``.md`` names the target
#: differently from the canonical tag id, map it here. The migration
#: script consumes this to rewrite the `.md` files exactly once.
LEGACY_ALIASES: Mapping[str, str] = {
    'donor_id': 'donor',   # tag is `donor`; legacy target was `donor_id`
}


def canonical_column(tag: str) -> str:
    """Return the view column name for a canonical ``tag``.

    ``canonical_column('braak')`` → ``'ant_braak'``.
    ``canonical_column('donor')`` → ``'ant_donor'``.
    """
    return f'ant_{tag}'


def is_canonical_column(name: str) -> bool:
    """True if ``name`` is a canonical view column (``ant_<tag>``)."""
    if not name.startswith('ant_'):
        return False
    return name[len('ant_'):] in CANONICAL_TAGS


def tag_of_canonical_column(name: str) -> str | None:
    """Reverse: ``ant_braak`` → ``braak``. Returns None if not canonical."""
    if not is_canonical_column(name):
        return None
    return name[len('ant_'):]


def canonicalise_legacy_target(target: str) -> str:
    """Migration helper.

    Rewrite a legacy ``obs_rename`` target (like ``braak`` or ``donor_id``)
    to the new ``ant_<tag>`` form. Non-canonical targets pass through
    unchanged.

    >>> canonicalise_legacy_target('braak')
    'ant_braak'
    >>> canonicalise_legacy_target('donor_id')
    'ant_donor'
    >>> canonicalise_legacy_target('Cell_type_full')
    'Cell_type_full'
    """
    if target in CANONICAL_TAGS:
        return canonical_column(target)
    if target in LEGACY_ALIASES:
        return canonical_column(LEGACY_ALIASES[target])
    return target


def strip_ant_prefix(name: str) -> str:
    """Display transform: ``ant_braak`` → ``braak``. Non-canonical → unchanged."""
    if is_canonical_column(name):
        return name[len('ant_'):]
    return name


#: REST-layer back-compat: legacy bare-tag column names that should
#: alias to the new canonical ``ant_*`` column for one release. The API
#: middleware rewrites ``?tag=braak`` / ``?color=braak`` → ``ant_braak``
#: when ``braak`` is not itself a real column in the view.
BACKCOMPAT_LEGACY_NAMES: Mapping[str, str] = {
    **{tag: canonical_column(tag) for tag in CANONICAL_TAGS},
    **{legacy: canonical_column(canonical) for legacy, canonical in LEGACY_ALIASES.items()},
}


__all__ = [
    'CANONICAL_TAGS',
    'NUMERICAL_TAGS',
    'CATEGORICAL_TAGS',
    'LEGACY_ALIASES',
    'BACKCOMPAT_LEGACY_NAMES',
    'canonical_column',
    'is_canonical_column',
    'tag_of_canonical_column',
    'canonicalise_legacy_target',
    'strip_ant_prefix',
]
