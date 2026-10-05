"""Distinct donor counting across datasets, keyed on ``ant_donor_uid``.

A donor's key is its ``ant_donor_uid`` when the dataset publishes one for
that donor, else ``<group_id>:<ant_donor>``: the dataset's own donor label,
scoped by the collection's group id so that two collections that both call a
donor ``D1`` stay apart. The fallback has the same shape as the template
form ``'{group_id}:{value}'`` of ``ant_donor_uid`` (``lib/obs_derive_forms``),
so a dataset with the template form and one without any uid count the same
donors once. Values are normalised as in ``obs_derive_forms.normalize_value``
(trimmed, ``1234.0`` as ``1234``, empty as missing).

The facet test tools record, per tested unit, the digests (``key_digest``)
of the analysed donors' keys, so ``facet_results`` can count distinct donors
across datasets without storing cohort identifiers in the run manifests.

Limitation, stated wherever counts are shown: the fallback cannot see that
two collections hold the same person. Donors without a uid in a reanalysis
filed under another group id are counted again. Only ``ant_donor_uid``
(cohort crosswalk) joins persons across collections.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

from obs_derive_forms import normalize_value

KIND_UID = 'uid'
KIND_FALLBACK = 'fallback'

COUNT_BASIS = ('distinct donors: ant_donor_uid when published, else <group_id>:<ant_donor>; the fallback '
               'cannot join the same person across collections')


def donor_key(uid: Any, donor: Any, group_id: Optional[str]) -> Optional[Tuple[str, str]]:
    """``(key, kind)`` of one donor, or None when it has neither a uid nor a label."""
    uid_text = normalize_value(uid)
    if uid_text is not None:
        return uid_text, KIND_UID
    donor_text = normalize_value(donor)
    if donor_text is None:
        return None
    return f'{group_id or ""}:{donor_text}', KIND_FALLBACK


def donor_keys(rows: Iterable[Tuple[Any, Any]], group_id: Optional[str]) -> Dict[str, str]:
    """``{key: kind}`` over ``(uid, donor)`` rows of one dataset. A key seen
    both as a uid and as a fallback counts as a uid."""
    keys: Dict[str, str] = {}
    for uid, donor in rows:
        found = donor_key(uid, donor, group_id)
        if found is None:
            continue
        key, kind = found
        if keys.get(key) != KIND_UID:
            keys[key] = kind
    return keys


def summarize(key_maps: Iterable[Mapping[str, str]]) -> Dict[str, int]:
    """Distinct donors over several datasets' ``{key: kind}`` maps.

    Returns ``{n_donors, n_donors_uid, n_donors_fallback}``.
    """
    union: Dict[str, str] = {}
    for keys in key_maps:
        for key, kind in keys.items():
            if union.get(key) != KIND_UID:
                union[key] = kind
    n_uid = sum(1 for kind in union.values() if kind == KIND_UID)
    return {'n_donors': len(union), 'n_donors_uid': n_uid, 'n_donors_fallback': len(union) - n_uid}


def donor_key_map(pairs: Iterable[Tuple[Any, Any]], group_id: Optional[str]
                  ) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Key per donor label of one dataset, from per-cell ``(ant_donor, ant_donor_uid)`` pairs.

    The map is keyed by ``str(ant_donor)`` (the label the facet tests use).
    A donor with exactly one uid gets it; a donor with none, or with several
    (a conflict), gets the fallback ``<group_id>:<ant_donor>``.

    Returns:
        ``(keys, stats)`` with ``stats`` ``{n_donors, n_donors_uid,
        n_uid_conflicts}``.
    """
    uids: Dict[str, Set[str]] = {}
    for donor, uid in pairs:
        if normalize_value(donor) is None:
            continue
        bucket = uids.setdefault(str(donor), set())
        uid_text = normalize_value(uid)
        if uid_text is not None:
            bucket.add(uid_text)
    keys: Dict[str, str] = {}
    n_uid = n_conflicts = 0
    for label, found in uids.items():
        if len(found) == 1:
            keys[label] = next(iter(found))
            n_uid += 1
        else:
            n_conflicts += len(found) > 1
            keys[label] = f'{group_id or ""}:{normalize_value(label)}'
    return keys, {'n_donors': len(keys), 'n_donors_uid': n_uid, 'n_uid_conflicts': n_conflicts}


def key_digest(key: str) -> str:
    """Short stable digest of a donor key (first 16 hex digits of SHA-256).

    The facet test manifests store digests, not keys: distinct counting only
    needs equality, and the keys can be controlled-access cohort ids.
    """
    return hashlib.sha256(str(key).encode('utf-8')).hexdigest()[:16]


def distinct_count(key_sets: Iterable[Optional[Set[str]]]) -> Optional[int]:
    """Size of the union of key sets; None when any set is missing (not recorded)."""
    union: Set[str] = set()
    for keys in key_sets:
        if keys is None:
            return None
        union |= set(keys)
    return len(union)


__all__ = ['KIND_UID', 'KIND_FALLBACK', 'COUNT_BASIS', 'donor_key', 'donor_keys', 'summarize',
           'donor_key_map', 'key_digest', 'distinct_count']
