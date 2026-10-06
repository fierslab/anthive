"""Facet scope: the (organism, experimental system) universe a facet is tested and counted in.

Facets never mix human and mouse. A dataset belongs to exactly one *facet scope*
(or to none, in which case the facet tests skip it):

* ``human_postmortem``: human cells from in vivo tissue (post-mortem, biopsy);
* ``mouse_in_vivo``: mouse cells from mouse tissue;
* ``xenograft_human_in_mouse``: human cells (iPSC derived, chimera) grown in a mouse host;
* ``in_vitro``: dish and organoid cultures (listed, never tested).

The scope is derived from the resolved ``.h5ad.mus`` annotation (``organism``,
``experimental_system``) and the published facets of the dataset (``ant_host_sex``
marks a host animal). Replication families, recurring genes, cohort counting and
the cross-dataset facet views of ``lib/facet_results.py`` are computed per scope;
genes are never merged across scopes (mouse and human symbols differ).

Which scopes a facet may be published in is a registry field of
``lib/obscol_tags_vocab.yaml`` (``facet_scopes``); ``facet_lint`` reports a dataset
that maps a facet outside its scopes.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional

SCOPE_HUMAN = 'human_postmortem'
SCOPE_MOUSE = 'mouse_in_vivo'
SCOPE_XENOGRAFT = 'xenograft_human_in_mouse'
SCOPE_IN_VITRO = 'in_vitro'

#: All scopes, in display order.
SCOPES = (SCOPE_HUMAN, SCOPE_MOUSE, SCOPE_XENOGRAFT, SCOPE_IN_VITRO)
#: Scopes in which facet tests are run (in vitro cultures are listed but not tested).
TESTED_SCOPES = (SCOPE_HUMAN, SCOPE_MOUSE, SCOPE_XENOGRAFT)
#: Scope answered when an endpoint expects one answer and the caller names none.
DEFAULT_SCOPE = SCOPE_HUMAN

SCOPE_LABELS = {
    SCOPE_HUMAN: 'human post-mortem and biopsy tissue',
    SCOPE_MOUSE: 'mouse in vivo',
    SCOPE_XENOGRAFT: 'human cells xenografted into mouse',
    SCOPE_IN_VITRO: 'in vitro culture',
}

#: ``experimental_system`` values that are tissue taken from the living or dead subject.
IN_VIVO_SYSTEMS = frozenset({'post_mortem_tissue', 'postmortem_tissue', 'fresh_biopsy', 'live_biopsy', 'in_vivo'})
XENOGRAFT_SYSTEMS = frozenset({'xenograft', 'chimera'})
IN_VITRO_SYSTEMS = frozenset({'in_vitro_2d', 'organoid'})

#: Datasets whose ``.h5ad.mus`` lacks ``organism`` but which are human by their own group
#: description (Wachter2024: 32 human donors, BioProject PRJNA916657). A missing organism
#: field is a metadata gap, not evidence of a non-human sample.
ASSUME_HUMAN_DB_IDS = frozenset({'WA4/MEC', 'WA4/MIT', 'WA4/MPF', 'WA4/MV1', 'WA4/MV2'})

#: Facet marker of a host animal: its presence makes a human dataset a xenograft.
HOST_FACET = 'ant_host_sex'


class ScopeError(ValueError):
    """An unknown scope name (maps to HTTP 400)."""


def _text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip().lower() for item in value if item is not None and str(item).strip()]
    text = str(value).strip().lower()
    return [text] if text else []


def scope_of(annotation: Mapping[str, Any], published_columns: Iterable[str] = (),
             db_id: Optional[str] = None) -> Optional[str]:
    """Facet scope of one dataset, or None when it belongs to no scope.

    Args:
        annotation: Resolved ``.h5ad.mus`` annotation (``organism``, ``experimental_system``).
        published_columns: ``ant_*`` columns the dataset's ``api:`` section publishes
            (``ant_host_sex`` marks a host animal).
        db_id: Dataset id; a dataset of ``ASSUME_HUMAN_DB_IDS`` without an organism is human.

    Returns:
        One of ``SCOPES``; None for a missing or mixed organism, bulk tissue (no cells)
        and unknown systems. Mouse cells labelled ``xenograft`` or ``chimera`` (a labelling
        inconsistency in the store) are mouse in vivo.
    """
    organisms = _text_list(annotation.get('organism'))
    if not organisms and db_id in ASSUME_HUMAN_DB_IDS:
        organisms = ['human']
    systems = set(_text_list(annotation.get('experimental_system')))
    if len(organisms) != 1 or organisms[0] not in ('human', 'mouse'):
        return None
    organism = organisms[0]
    if systems & IN_VITRO_SYSTEMS:
        return SCOPE_IN_VITRO
    if 'bulk_tissue' in systems:
        return None
    if organism == 'human':
        if systems & XENOGRAFT_SYSTEMS or HOST_FACET in set(published_columns):
            return SCOPE_XENOGRAFT
        if not systems or systems <= IN_VIVO_SYSTEMS:
            return SCOPE_HUMAN
        return None
    if not systems or systems <= (IN_VIVO_SYSTEMS | XENOGRAFT_SYSTEMS):
        return SCOPE_MOUSE
    return None


def check_scope(scope: Optional[str]) -> str:
    """Validate a ``scope`` query value.

    Args:
        scope: Scope name; None or empty selects ``DEFAULT_SCOPE``.

    Returns:
        The scope name.

    Raises:
        ScopeError: ``scope`` is not one of ``SCOPES``.
    """
    if not scope:
        return DEFAULT_SCOPE
    if scope not in SCOPES:
        raise ScopeError(f'scope must be one of {", ".join(SCOPES)}')
    return scope


def facet_allowed_scopes(facet: str) -> List[str]:
    """Scopes a facet may be published in (``facet_scopes`` of the registry; all when unset).

    Args:
        facet: Facet tag (``braak``) or column (``ant_braak``).
    """
    from obscol_tags import facet_registry_entry  # noqa: PLC0415
    entry = facet_registry_entry(facet)
    scopes = list((entry or {}).get('facet_scopes') or [])
    return scopes or list(SCOPES)


def facet_in_scope(facet: str, scope: Optional[str]) -> bool:
    """True when ``facet`` may be published in ``scope`` (a dataset without scope passes)."""
    return scope is None or scope in facet_allowed_scopes(facet)
