"""Donor checks: the donor uid form, its resolution through the crosswalk, and overlap with stored donors.

The crosswalk (``donor_crosswalk.tsv``) is controlled-access data. These
checks read it only to count how many donor values resolve; nothing from it
(aliases, canonical ids, donor labels) is put into a report. Overlap with
stored donors is reported as counts, digests and dataset ids (admin only).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from . import HELD_DONOR_OVERLAP, Outcome, no_database, check, fail, note, ok, skip, warn
from . import thresholds

#: Keys that declare how a dataset relates to donors in other datasets.
DESIGNATION_KEYS = ('is_secondary', 'reanalysis_of', 'donor_group', 'facet_donor_exclusions')


def designation(metadata: Mapping[str, Any]) -> List[str]:
    """Names of the donor designation keys the ``.md`` declares."""
    found = []
    for key in DESIGNATION_KEYS:
        value = metadata.get(key)
        if key == 'is_secondary':
            if value is True:
                found.append(key)
        elif value:
            found.append(key)
    return found


def _is_human_donor_dataset(ctx) -> bool:
    from replicates import ReplicateError, declared_replicate  # noqa: PLC0415

    try:
        unit = declared_replicate(ctx.md)[0]
    except ReplicateError:
        unit = None
    return ctx.organism == 'human' and unit in (None, 'donor')


def _uid_spec(ctx) -> Optional[Dict[str, Any]]:
    spec = (ctx.api.get('obs_derive') or {}).get('ant_donor_uid')
    return spec if isinstance(spec, dict) else None


@check('donor.uid_form')
def donor_uid_form(ctx) -> Outcome:
    """``api.obs_derive.ant_donor_uid`` is a well-formed template or join derive."""
    from obs_derive_forms import DeriveFormError, derive_form, validate_spec  # noqa: PLC0415

    spec = _uid_spec(ctx)
    if spec is None:
        if _is_human_donor_dataset(ctx):
            return fail('human donor dataset without ant_donor_uid',
                        "Add api.obs_derive.ant_donor_uid: {source: <donor column>, template: '{group_id}:{value}'}. "
                        'A person can then be recognised across datasets.')
        return skip('not a human donor dataset: ant_donor_uid is optional')
    try:
        clean = validate_spec('ant_donor_uid', spec)
    except DeriveFormError as exc:
        return fail(f'ant_donor_uid is malformed: {exc}', "Use the template form {source, template: '{group_id}:{value}'} "
                    'or the join form named in the requirements.')
    source = str(clean.get('source') or '')
    if ctx.h5ad_path is not None and source and source not in set(map(str, ctx.adata.obs.columns)):
        return fail(f'the source column {source!r} of ant_donor_uid is not an obs column',
                    'Name the obs column that holds the donor id.', form=derive_form(spec))
    return ok(f'ant_donor_uid uses the {derive_form(spec)} form', form=derive_form(spec))


@check('donor.uid_resolves', needs=('h5ad',))
def donor_uid_resolves(ctx) -> Outcome:
    """Join form only: the cohort exists in the crosswalk and few donor values fall back (counts only)."""
    from obs_derive_forms import (FORM_JOIN, RULE_CROSSWALK, RULE_FALLBACK, RULE_UNMATCHED, DeriveFormError,  # noqa: PLC0415
                                  derive_form, translate_value, validate_spec)

    spec = _uid_spec(ctx)
    if spec is None or derive_form(spec) != FORM_JOIN:
        return skip('no join form for ant_donor_uid')
    try:
        clean = validate_spec('ant_donor_uid', spec)
    except DeriveFormError:
        return skip('ant_donor_uid is malformed (see donor.uid_form)')
    crosswalk, problem = ctx.crosswalk_for(clean)
    if crosswalk is None:
        outcome = fail('the donor id table named in the join does not exist on this site',
                       'Use the template form, or ask an admin which donor table the site offers.')
        outcome.admin_detail = {'problem': problem}
        return outcome
    if problem is not None:
        outcome = fail('the cohort named in the join is unknown to the donor id table',
                       'Use the cohort name the server lists in the requirements, or the template form.')
        outcome.admin_detail = {'problem': problem, 'cohorts_available': crosswalk.cohorts()}
        return outcome
    source = str(clean['source'])
    if source not in set(map(str, ctx.adata.obs.columns)):
        return skip('the join source column is not an obs column (see donor.uid_form)')
    values = ctx.adata.obs[source].dropna().astype(str).unique()
    counts = {RULE_CROSSWALK: 0, RULE_FALLBACK: 0, RULE_UNMATCHED: 0}
    for value in values:
        _published, rule = translate_value(clean, value, ctx.group_id, crosswalk)
        counts[rule] = counts.get(rule, 0) + 1
    total = max(len(values), 1)
    fallback_fraction = (counts[RULE_FALLBACK] + counts[RULE_UNMATCHED]) / total
    details = {'n_donor_values': len(values), 'n_resolved': counts[RULE_CROSSWALK],
               'n_fallback': counts[RULE_FALLBACK], 'n_unmatched': counts[RULE_UNMATCHED]}
    outcome = (warn(f'{fallback_fraction:.0%} of the donor values are not in the donor id table and use the fallback',
                    'Check that the cohort in the join is the right one for this paper.', **details)
               if fallback_fraction > thresholds.UID_FALLBACK_WARN_FRACTION
               else ok(f'{counts[RULE_CROSSWALK]} of {len(values)} donor values resolve through the donor id table', **details))
    outcome.admin_detail = {'cohort': str(clean.get('cohort')), 'cohorts_available': crosswalk.cohorts()}
    return outcome


def shared_donors(new_digests: set, index_donors: pd.DataFrame, own_group: str, own_key: str) -> Dict[str, int]:
    """``{db_key: n shared persons}`` between the new donors and other groups' donors (uid keys only)."""
    other = index_donors[(index_donors['group_id'] != own_group) & (index_donors['db_key'] != own_key)
                         & (index_donors['key_kind'] == 'uid')]
    hits = other[other['key_digest'].isin(new_digests)]
    return {str(key): int(count) for key, count in hits.groupby('db_key').size().items()}


@check('donor.overlap_ids', needs=('duckdb', 'index'))
def donor_overlap_ids(ctx) -> Outcome:
    """Persons (donor uid digests) shared with datasets of other groups need a declared designation."""
    index = ctx.index
    if index.is_empty():
        return skip('no overlap index; an admin runs `ant ingest-index build`')
    digests = ctx.new_digests()
    if digests is None:
        return no_database(ctx)
    donors = digests.donors
    uid_digests = set(donors.loc[donors['key_kind'] == 'uid', 'key_digest']) if len(donors) else set()
    if not uid_digests:
        return ok('no cross-dataset donor ids (ant_donor_uid) to compare', n_donors=int(len(donors)))
    per_dataset = shared_donors(uid_digests, index.donors(), ctx.group_id or '', digests.db_key)
    partners = ctx.qc_partners()                     # a QC companion and its final dataset share every donor
    per_dataset = {key: value for key, value in per_dataset.items() if key not in partners} \
        if isinstance(per_dataset, dict) else [key for key in per_dataset if key not in partners]
    shared_persons = set()
    if per_dataset:
        other = index.donors()
        shared_persons = set(other[other['db_key'].isin(per_dataset) & other['key_digest'].isin(uid_digests)]['key_digest'])
    if not per_dataset:
        return ok('no donor is shared with a dataset of another group', n_donors=int(len(donors)))
    declared = designation(ctx.md)
    details = {'n_shared_persons': len(shared_persons), 'n_datasets': len(per_dataset),
               'n_groups': len({key.split('/')[0] for key in per_dataset}), 'designation': declared}
    admin = {'shared_per_dataset': per_dataset}
    if declared:
        outcome = note(f'{len(shared_persons)} persons also occur in {len(per_dataset)} other dataset(s); '
                       f'declared through {", ".join(declared)}', **details)
    else:
        outcome = fail(f'{len(shared_persons)} persons also occur in {len(per_dataset)} dataset(s) of other groups',
                       'Say how this dataset relates to them in the .md: is_secondary: true with reanalysis_of '
                       '(a re-analysis of the same samples), donor_group (one cohort split over several files), '
                       'or facet_donor_exclusions (leave the shared donors out of the counted facet tests). '
                       'Otherwise ask an admin.', **details)
    outcome.admin_detail = admin
    return outcome


# ── demographic matching ─────────────────────────────────────────────────────

def _known(array: np.ndarray) -> np.ndarray:
    return ~pd.isna(array)


def candidate_matrix(new: pd.DataFrame, old: pd.DataFrame, age_shift: float = 0.0
                     ) -> Tuple[np.ndarray, np.ndarray]:
    """Which stored donors could be the same person as each new donor, and how close they are.

    A pair is a candidate when sex and age are known and agree (age within
    ``DEMOGRAPHIC_AGE_TOLERANCE_YEARS``; two censored ages, 90+, agree), no
    other attribute (Braak, APOE genotype, PMI within the tolerance) is
    known and different, and at least two of these three are known and equal.

    Args:
        new: Donors of the submission (``overlap_index.DONOR_COLUMNS``).
        old: Stored donors.
        age_shift: Added to the new ages (background estimate, see ``match_cohort``).

    Returns:
        ``(candidates, distance)``, both ``(len(new), len(old))``. The distance
        is the age gap in tolerances plus the PMI gap in tolerances (a missing
        PMI counts half a tolerance); it separates candidates that tie.
    """
    n_new, n_old = len(new), len(old)
    if n_new == 0 or n_old == 0:
        return np.zeros((n_new, n_old), dtype=bool), np.zeros((n_new, n_old))

    def pair(column: str):
        left = new[column].to_numpy(dtype=object)[:, None]
        right = old[column].to_numpy(dtype=object)[None, :]
        return left, right, _known(left) & _known(right)

    sex_left, sex_right, sex_known = pair('sex')
    sex_ok = sex_known & (sex_left == sex_right)
    age_new = new['age'].to_numpy(dtype=float)[:, None] + age_shift
    age_old = old['age'].to_numpy(dtype=float)[None, :]
    censored_new = (new['age_censored'].to_numpy(dtype=bool) | (new['age'].to_numpy(dtype=float)
                    >= thresholds.AGE_CENSOR_THRESHOLD))[:, None]
    censored_old = (old['age_censored'].to_numpy(dtype=bool) | (old['age'].to_numpy(dtype=float)
                    >= thresholds.AGE_CENSOR_THRESHOLD))[None, :]
    both_censored = censored_new & censored_old
    age_gap = np.abs(age_new - age_old) / thresholds.DEMOGRAPHIC_AGE_TOLERANCE_YEARS
    age_ok = ((age_gap <= 1.0) | both_censored) & ~np.isnan(age_new) & ~np.isnan(age_old)

    equal_count = np.zeros((n_new, n_old), dtype=int)
    contradiction = np.zeros((n_new, n_old), dtype=bool)
    for column in ('braak', 'apoe'):
        left, right, known = pair(column)
        agree = known & (left == right)
        equal_count += agree
        contradiction |= known & ~agree
    pmi_new = new['pmi'].to_numpy(dtype=float)[:, None]
    pmi_old = old['pmi'].to_numpy(dtype=float)[None, :]
    pmi_known = ~np.isnan(pmi_new) & ~np.isnan(pmi_old)
    pmi_gap = np.abs(pmi_new - pmi_old) / thresholds.DEMOGRAPHIC_PMI_TOLERANCE_HOURS
    pmi_agree = pmi_known & (pmi_gap <= 1.0)
    equal_count += pmi_agree
    contradiction |= pmi_known & ~pmi_agree
    distance = np.where(both_censored, 0.0, np.nan_to_num(age_gap, nan=1.0)) + np.where(pmi_known, pmi_gap, 0.5)
    return sex_ok & age_ok & ~contradiction & (equal_count >= 2), distance


def unique_matches(candidates: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Boolean per new donor: exactly one candidate, or exactly one *close* candidate.

    Close means a distance of at most ``DEMOGRAPHIC_CLOSE_DISTANCE``
    tolerances (decimal ages and PMI that agree to a fraction of the
    tolerance). That separates true pairs from chance candidates when many
    donors are top-coded at 90 or share a sex, Braak stage and genotype.
    """
    n_candidates = candidates.sum(axis=1)
    n_close = (candidates & (distance <= thresholds.DEMOGRAPHIC_CLOSE_DISTANCE)).sum(axis=1)
    return (n_candidates == 1) | (n_close == 1)


def match_cohort(new: pd.DataFrame, old: pd.DataFrame) -> Dict[str, Any]:
    """Demographic overlap of the new donors with one stored cohort.

    Returns ``n_new``, ``n_old``, ``n_unique`` (new donors with a unique best
    candidate, see ``unique_matches``), ``fraction``, ``background`` (the same
    statistic with the new ages shifted by 6, 9 and 12 years either way, which
    no real person matches: the chance level of this cohort), ``corrected``
    (fraction minus background, at least 0) and ``excess`` (corrected number
    of donors).
    """
    old = old.drop_duplicates('key_digest')
    n_unique = int(unique_matches(*candidate_matrix(new, old)).sum())
    shifts = [-12.0, -9.0, -6.0, 6.0, 9.0, 12.0]
    background = float(np.mean([unique_matches(*candidate_matrix(new, old, shift)).mean() for shift in shifts])) \
        if len(new) else 0.0
    fraction = n_unique / len(new) if len(new) else 0.0
    corrected = max(0.0, fraction - background)
    return {'n_new': int(len(new)), 'n_old': int(len(old)), 'n_unique': n_unique, 'fraction': round(fraction, 4),
            'background': round(background, 4), 'corrected': round(corrected, 4),
            'excess': round(corrected * len(new), 1)}


@check('donor.overlap_demographics', needs=('duckdb', 'index'))
def donor_overlap_demographics(ctx) -> Outcome:
    """Donors whose sex, age, APOE, Braak and PMI match one donor of a stored cohort."""
    index = ctx.index
    if index.is_empty():
        return skip('no overlap index; an admin runs `ant ingest-index build`')
    digests = ctx.new_digests()
    if digests is None:
        return no_database(ctx)
    new = digests.donors
    if len(new) < thresholds.DEMOGRAPHIC_MIN_DONORS:
        return skip(f'{len(new)} donors: too few to judge demographic overlap')
    stored = index.donors()
    stored = stored[(stored['group_id'] != (ctx.group_id or '')) & (stored['db_key'] != digests.db_key)]
    table = {group: match_cohort(new, rows) for group, rows in stored.groupby('group_id') if len(rows)}
    if not table:
        return ok('no stored cohort with donor demographics to compare', n_donors=int(len(new)))
    ranked = sorted(table.items(), key=lambda item: item[1]['corrected'], reverse=True)
    best_group, best = ranked[0]
    declared = designation(ctx.md)
    details = {'n_donors': int(len(new)), 'n_cohorts_compared': len(table), 'best_fraction': best['fraction'],
               'best_fraction_beyond_chance': best['corrected'], 'best_excess_donors': best['excess'],
               'designation': declared}
    admin = {'cohorts': {group: stats for group, stats in ranked[:10]}, 'best_cohort': best_group}
    # held: at least half of the donors have a unique match AND that is well above the chance level of the cohort
    held = best['fraction'] >= thresholds.DEMOGRAPHIC_HELD_FRACTION \
        and best['corrected'] >= thresholds.DEMOGRAPHIC_WARN_FRACTION
    flagged = best['corrected'] >= thresholds.DEMOGRAPHIC_WARN_FRACTION or best['excess'] >= thresholds.DEMOGRAPHIC_WARN_MIN_EXCESS
    if declared and (held or flagged):
        outcome = note(f'about {best["excess"]:.0f} donors match one stored cohort by demographics beyond chance; '
                       f'declared through {", ".join(declared)}', **details)
    elif held:
        outcome = warn(f'{best["fraction"]:.0%} of the donors have a unique demographic match in one stored cohort '
                       f'(chance level {best["background"]:.0%}); an admin decides before publication',
                       'If these are the same persons, declare it in the .md (is_secondary / reanalysis_of / '
                       'donor_group / facet_donor_exclusions). Otherwise tell the admin why they are different people.',
                       **details)
        outcome.held.append(HELD_DONOR_OVERLAP)
    elif flagged:
        outcome = warn(f'about {best["excess"]:.0f} donors match one donor of a stored cohort by demographics '
                       f'beyond chance ({best["fraction"]:.0%} matched, chance level {best["background"]:.0%})',
                       'If these are the same persons, declare it in the .md (see donor.overlap_ids).', **details)
    else:
        outcome = ok('no cohort with more demographic matches than chance', **details)
    outcome.admin_detail = admin
    return outcome
