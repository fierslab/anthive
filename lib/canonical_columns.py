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

try:  # lib/ on sys.path (every consumer uses the shim)
    from obscol_tags import facet_registry as _facet_registry
except ImportError:  # imported as a package module
    from .obscol_tags import facet_registry as _facet_registry  # type: ignore


_REGISTRY = _facet_registry()

#: The facet tags, registered and deprecated, in vocab order. Derived from
#: ``lib/obscol_tags_vocab.yaml`` (the ``status`` field of each ``facet:``
#: block), which is the single source of the closed ``ant_*`` namespace
#: (decision L1, 2026-10-04). Every tag here has a matching ``ant_<tag>``
#: view column when the dataset's ``.md`` declares the mapping in
#: ``api.obs_rename``, ``api.obs_derive`` or ``api.obs_constant``.
CANONICAL_TAGS: tuple[str, ...] = tuple(_REGISTRY)

#: Tags that may be published. ``ant-md-check`` rejects any other target.
REGISTERED_TAGS: tuple[str, ...] = tuple(
    tag for tag, entry in _REGISTRY.items() if entry['status'] == 'registered')

#: Tags still served while datasets migrate; ``ant-md-check`` warns.
DEPRECATED_TAGS: tuple[str, ...] = tuple(
    tag for tag, entry in _REGISTRY.items() if entry['status'] == 'deprecated')

#: Numerical (ordinal / continuous) tags, from the vocab's ``kind``.
NUMERICAL_TAGS: frozenset[str] = frozenset(
    tag for tag, entry in _REGISTRY.items() if entry['kind'] == 'numerical')

#: Categorical tags — everything else in CANONICAL_TAGS.
CATEGORICAL_TAGS: frozenset[str] = frozenset(CANONICAL_TAGS) - NUMERICAL_TAGS

#: The 13 bare tag names of the 2026-08 ``ant_*`` migration. Only these get
#: the one-release REST alias (``?color=braak`` -> ``ant_braak``) and are
#: rewritten by the one-shot migration scripts; facets registered later
#: never had a bare-name form.
LEGACY_BARE_TAGS: tuple[str, ...] = (
    'braak', 'braak_group', 'sex', 'age', 'cluster', 'disease', 'condition',
    'pmi', 'donor', 'apoe', 'region', 'genotype', 'host_sex',
)


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
    if target in LEGACY_BARE_TAGS:
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
    **{tag: canonical_column(tag) for tag in LEGACY_BARE_TAGS},
    **{legacy: canonical_column(canonical) for legacy, canonical in LEGACY_ALIASES.items()},
}


__all__ = [
    'CANONICAL_TAGS',
    'REGISTERED_TAGS',
    'DEPRECATED_TAGS',
    'LEGACY_BARE_TAGS',
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
