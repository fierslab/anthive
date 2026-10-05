"""Donor exclusion rules for the donor-level facet tests.

A dataset can contain persons who are also in another cohort that the facet
analysis counts as its own independent cohort (Lee2026 PsychAD and FreshMG
contain ROSMAP participants, and ROSMAP is already counted through the Mathys
and Green datasets). Counting both as independent replication overstates
independent evidence. The dataset ``.md`` can therefore declare which donors
must be left out of the counted run::

    facet_donor_exclusions:
      reason: "Rush (ROSMAP) brain bank donors, counted through the ROSMAP cohort"
      obs:                      # donors with at least one cell in these values
        Source: [R]
      cohorts:                  # donors whose ant_donor_uid starts with <cohort>:
        - rosmap

``bin/annot/ant-facet-test`` and ``bin/annot/ant-facet-test-modules`` apply
the rule to the per-cell table before any donor is planned, so every number of
the counted run (donor counts, strata, composition proportions, library sizes
of the limma fit) refers to the remaining donors only. The full run is kept as
a supplementary, non-counted unit (``--all-donors``) under
``<results>/<group>/supplementary_all_donors/``. The manifests record counts of
excluded donors, never identifiers.

Pure functions, no I/O: ``tests/test_donor_exclusions.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd

MD_KEY = 'facet_donor_exclusions'
SUPPLEMENTARY_DIRNAME = 'supplementary_all_donors'
VARIANT_COUNTED = 'counted'
VARIANT_ALL_DONORS = 'all_donors'


@dataclass(frozen=True)
class ExclusionRule:
    """Which donors a counted facet run leaves out.

    Attributes:
        reason: Free text, copied into the manifest.
        obs: ``{obs column: values}``; a donor with at least one cell in any of
            the values is excluded.
        cohorts: Cohort names; a donor whose ``ant_donor_uid`` starts with
            ``<cohort>:`` is excluded.
        origin: ``md`` (front matter) or ``cli`` (command line options added).
    """
    reason: str = ''
    obs: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    cohorts: Tuple[str, ...] = ()
    origin: str = 'md'

    @property
    def is_empty(self) -> bool:
        return not self.obs and not self.cohorts

    @property
    def obs_columns(self) -> List[str]:
        return sorted(self.obs)

    def as_json(self) -> Dict[str, Any]:
        return {'reason': self.reason, 'obs': {k: list(v) for k, v in sorted(self.obs.items())},
                'cohorts': list(self.cohorts), 'origin': self.origin}


def rule_from_md(md_yaml: Mapping[str, Any]) -> Optional[ExclusionRule]:
    """The rule declared in a dataset ``.md`` (None when there is none).

    Raises:
        ValueError: The key is present but malformed (a silent skip would let
            overlapping donors be counted).
    """
    raw = md_yaml.get(MD_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f'{MD_KEY} must be a mapping (reason, obs, cohorts)')
    unknown = set(raw) - {'reason', 'obs', 'cohorts'}
    if unknown:
        raise ValueError(f'{MD_KEY}: unknown keys {sorted(unknown)}')
    obs_raw = raw.get('obs') or {}
    if not isinstance(obs_raw, dict):
        raise ValueError(f'{MD_KEY}.obs must map an obs column to a list of values')
    obs: Dict[str, Tuple[str, ...]] = {}
    for column, values in obs_raw.items():
        if isinstance(values, (str, int, float)):
            values = [values]
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f'{MD_KEY}.obs.{column} needs a non-empty list of values')
        obs[str(column)] = tuple(str(v) for v in values)
    cohorts_raw = raw.get('cohorts') or []
    if isinstance(cohorts_raw, str):
        cohorts_raw = [cohorts_raw]
    if not isinstance(cohorts_raw, (list, tuple)):
        raise ValueError(f'{MD_KEY}.cohorts must be a list of cohort names')
    cohorts = tuple(str(c).strip() for c in cohorts_raw if str(c).strip())
    rule = ExclusionRule(reason=' '.join(str(raw.get('reason') or '').split()), obs=obs,
                         cohorts=cohorts, origin='md')
    if rule.is_empty:
        raise ValueError(f'{MD_KEY} declares neither obs values nor cohorts')
    return rule


def parse_where(items: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
    """``['Source=R|RS', 'batch=B1']`` -> ``{'Source': ('R', 'RS'), 'batch': ('B1',)}``."""
    out: Dict[str, List[str]] = {}
    for item in items:
        column, sep, values = item.partition('=')
        column = column.strip()
        if not sep or not column or not values.strip():
            raise ValueError(f'--exclude-donors-where needs COLUMN=VALUE[|VALUE...], got {item!r}')
        out.setdefault(column, []).extend(v for v in (x.strip() for x in values.split('|')) if v)
    return {k: tuple(v) for k, v in out.items()}


def combine(md_rule: Optional[ExclusionRule], cli_cohorts: Sequence[str] = (),
            cli_where: Sequence[str] = ()) -> Optional[ExclusionRule]:
    """The md rule plus the command line additions (None when both are empty)."""
    extra_obs = parse_where(cli_where)
    extra_cohorts = tuple(c.strip() for item in cli_cohorts for c in item.split(',') if c.strip())
    if not extra_obs and not extra_cohorts:
        return md_rule
    base_obs = dict(md_rule.obs) if md_rule else {}
    for column, values in extra_obs.items():
        base_obs[column] = tuple(dict.fromkeys(base_obs.get(column, ()) + values))
    cohorts = tuple(dict.fromkeys((md_rule.cohorts if md_rule else ()) + extra_cohorts))
    return ExclusionRule(reason=(md_rule.reason if md_rule else 'command line'),
                         obs=base_obs, cohorts=cohorts, origin='cli' if not md_rule else 'md+cli')


def excluded_donors(cells: pd.DataFrame, rule: ExclusionRule) -> Tuple[Set[str], Dict[str, int]]:
    """Donors (``ant_donor`` as text) the rule excludes, and per-criterion counts.

    Args:
        cells: Per-cell frame with ``ant_donor``, ``ant_donor_uid`` (needed for
            cohort rules) and every column named in ``rule.obs``.
        rule: The rule.

    Returns:
        ``(donors, counts)``; ``counts`` maps ``"<column>=<values>"`` and
        ``"cohort:<name>"`` to the number of donors that criterion excludes
        (criteria overlap, so the counts need not add up to the set size).

    Raises:
        ValueError: A named obs column or ``ant_donor_uid`` is missing.
    """
    keyed = cells[cells['ant_donor'].notna()]
    donor_text = keyed['ant_donor'].astype(str)
    excluded: Set[str] = set()
    counts: Dict[str, int] = {}
    for column, values in sorted(rule.obs.items()):
        if column not in cells.columns:
            raise ValueError(f'exclusion column {column!r} is not an obs column of this dataset')
        hit = keyed[column].astype(str).isin(set(values)) & keyed[column].notna()
        donors = set(donor_text[hit])
        counts[f'{column}={"|".join(values)}'] = len(donors)
        excluded |= donors
    for cohort in rule.cohorts:
        if 'ant_donor_uid' not in cells.columns:
            raise ValueError('a cohort exclusion needs ant_donor_uid, which this dataset does not publish')
        uid = keyed['ant_donor_uid']
        hit = uid.notna() & uid.astype(str).str.startswith(f'{cohort}:')
        donors = set(donor_text[hit])
        counts[f'cohort:{cohort}'] = len(donors)
        excluded |= donors
    return excluded, counts


def apply_rule(cells: pd.DataFrame, rule: Optional[ExclusionRule], variant: str = VARIANT_COUNTED
               ) -> Tuple[pd.DataFrame, Optional[Dict[str, Any]]]:
    """Drop the excluded donors' cells (``variant`` counted) or only report them
    (``all_donors``).

    Returns:
        ``(cells, report)``. ``cells`` has a fresh 0..n-1 index so that
        positional joins downstream stay valid. ``report`` (None without a
        rule) is the manifest block ``donor_exclusion``: variant, ``counted``,
        the rule, donor and cell counts before and after, per-criterion
        counts. No identifiers.
    """
    if rule is None:
        return cells, None
    donors_before = int(cells['ant_donor'].dropna().astype(str).nunique())
    excluded, per_criterion = excluded_donors(cells, rule)
    report: Dict[str, Any] = {
        'variant': variant, 'counted': variant == VARIANT_COUNTED,
        'rule': rule.as_json(), 'n_donors_before': donors_before, 'n_cells_before': int(len(cells)),
        'n_excluded_by_criterion': per_criterion, 'n_donors_excluded': len(excluded),
    }
    if variant == VARIANT_ALL_DONORS:
        report.update(n_donors_after=donors_before, n_cells_after=int(len(cells)),
                      note='full donor set, not counted; the counted run drops the donors above')
        return cells, report
    keep = ~cells['ant_donor'].astype(str).isin(excluded) | cells['ant_donor'].isna()
    kept = cells[keep].reset_index(drop=True)
    report.update(n_donors_after=int(kept['ant_donor'].dropna().astype(str).nunique()),
                  n_cells_after=int(len(kept)))
    return kept, report


def add_cli_arguments(parser) -> None:
    """The three options shared by both facet test tools."""
    parser.add_argument('--exclude-donors-from', action='append', default=[], metavar='COHORT',
                        help='also exclude donors whose ant_donor_uid starts with COHORT: (comma list, '
                             'repeatable); needs an explicit --out-root')
    parser.add_argument('--exclude-donors-where', action='append', default=[], metavar='COLUMN=V1|V2',
                        help='also exclude donors with a cell in these values of an obs column '
                             '(repeatable); needs an explicit --out-root')
    parser.add_argument('--all-donors', action='store_true',
                        help='ignore the facet_donor_exclusions rule of the .md and test every donor; '
                             'written to <group>/' + SUPPLEMENTARY_DIRNAME + '/ as a non-counted unit. '
                             'Datasets without a rule are skipped.')


def result_dir(out_root, study: str, rule_in_md: bool, all_donors: bool):
    """Folder of a dataset's outputs: the normal one, or the supplementary one
    for an ``--all-donors`` run of a dataset that has a rule."""
    from pathlib import Path  # noqa: PLC0415
    base = Path(out_root) / study
    return base / SUPPLEMENTARY_DIRNAME if (all_donors and rule_in_md) else base


# ── per-facet age floor and declared batch column (md keys) ───────────────────

MIN_AGE_KEY = 'facet_min_age'
BATCH_KEY = 'facet_batch_column'


def min_age_floors(md_yaml: Mapping[str, Any]) -> Dict[str, float]:
    """``facet_min_age: {disease: 60, braak: 60}`` -> ``{'disease': 60.0, 'braak': 60.0}``.

    Facet names may carry the ``ant_`` prefix. A donor younger than the floor is
    left out of the tests of that facet only (sex and the other facets keep it).

    Raises:
        ValueError: not a mapping of facet name to a non-negative number.
    """
    raw = md_yaml.get(MIN_AGE_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f'{MIN_AGE_KEY} must map a facet name to an age in years')
    floors: Dict[str, float] = {}
    for name, value in raw.items():
        key = str(name)
        key = key[4:] if key.startswith('ant_') else key
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f'{MIN_AGE_KEY}.{name} must be a non-negative number of years')
        floors[key] = float(value)
    return floors


def below_age_floor(donor_table: pd.DataFrame, floors: Mapping[str, float]
                    ) -> Tuple[Dict[str, Set[str]], Optional[Dict[str, Any]]]:
    """Donors under each facet's age floor, and the manifest block (counts only).

    Donors with an unknown age are kept (an unknown age is not evidence of being
    young) and counted in ``n_age_unknown_kept``.
    """
    if not floors:
        return {}, None
    age = pd.to_numeric(donor_table['age'], errors='coerce')
    below: Dict[str, Set[str]] = {}
    report: Dict[str, Any] = {}
    for facet, floor in sorted(floors.items()):
        young = set(donor_table.loc[age < floor, 'donor'].astype(str))
        below[facet] = young
        report[facet] = {'floor_years': floor, 'n_donors_below_floor': len(young),
                         'n_age_unknown_kept': int(age.isna().sum()), 'n_donors': int(len(donor_table))}
    return below, report


def stratum_donors(donors: pd.Series, facet: str, below: Mapping[str, Set[str]]) -> pd.Series:
    """``donors`` without those under the age floor of ``facet`` (unchanged when it has none)."""
    young = below.get(facet)
    if not young:
        return donors
    return donors[~donors.astype(str).isin(young)]


def declared_columns(md_yaml: Mapping[str, Any]) -> List[str]:
    """Obs columns the md asks the tools to load (the declared batch column)."""
    column = md_yaml.get(BATCH_KEY)
    return [str(column)] if column else []


def declared_batch_column(md_yaml: Mapping[str, Any], cells: pd.DataFrame) -> Optional[str]:
    """The batch column named by ``facet_batch_column`` (None without the key).

    It replaces the name-based guess. The donor table still keeps it only where
    a donor has a single value, and ``plan_job`` still drops it when it has fewer
    than two levels among the analysed donors or is confounded with the facet.

    Raises:
        ValueError: the named column is not an obs column of the dataset.
    """
    column = md_yaml.get(BATCH_KEY)
    if not column:
        return None
    if not isinstance(column, str) or column not in cells.columns:
        raise ValueError(f'{BATCH_KEY} {column!r} is not an obs column of this dataset')
    return column
