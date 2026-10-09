"""Cross-cohort replication of module (gene set) facet effects, for the gene set and facet views.

``bin/annot/ant-facet-test-modules`` tests every module per dataset and cell type stratum (OLS on donor-level
module scores, permutation p values). This module combines those results across cohorts exactly the way
``lib/facet_results.py`` does for genes:

- one anchor unit per cohort and cell type group (``FacetResults.counting_units``), per facet scope;
- the cohort p value of a direction is the two-sided p halved when the estimate has that sign (1 minus the half
  otherwise); the p is the parametric OLS (t test) p of the unit when its residual df is at least
  ``facet_rules.MODULE_PARAMETRIC_MIN_DF`` (30), else the permutation p; the other stays served as a calibration
  check and units where the two disagree strongly are flagged (``p_check_flag``);
- the m - 1 largest of the m cohort p values are combined by Simes (partial conjunction, u = 2), x 2 for the two
  directions, Benjamini-Hochberg over the modules of one family gives ``r_value``; ``replicated`` is r < 0.05;
- the effect filter is separate and descriptive: at least 2 cohorts whose standardised effect has its lower 95 percent
  confidence bound in the replicated direction at or above the facet's gate (``facet_rules.module_gate``: 0.2 SD per
  contrast, 0.2/3 per Braak stage, 0.2 per decade, 0.1 per e4 allele). There is no strict tier for modules
  (``strict`` stays null);
- directionless sets (GWAS or MAGMA lists, ``directionless_reason``) are left out of every family: no BH family
  membership, no replicated or top list entry; their cards stay descriptive;
- a cohort is ``small`` when the smaller level of a two-group facet has fewer than 10 replicates (other facets: fewer
  than 20 replicates); flag only.

A family is one (facet scope, facet, cell type group, method, source): the two variants of a module are the
pseudobulk of raw counts (B, ``pseudobulk`` / ``gene_list``, needs a counts layer) and the mean of per-cell scores
(A, ``cell_mean`` / ``ucell`` or ``geneset``). Both are served; the view shows B where it exists, else A, labelled.
A and B disagree only when their directions differ and either is replicated or both effects reach the gate.

Reading the module result parquets happens once for every facet and scope (small files, a second or two) and is
cached single flight with the same signature as the gene tables; the families are cached per (facet, scope).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import facet_rules
import replication
from celltype_groups import UNMAPPED, group_info, group_of
from facet_aliases import canonical_facet
from facet_results import (
    FAMILY_CACHE_ENTRIES, PC_METHOD, POOL_PREFIX, READ_WORKERS, FacetResults, PreparedDatasets, _as_int, _clean_text,
    _file_key, _finite, _is_small, _not_counted_reason, _rename_deprecated_facets, _rounded, cohort_display,
    forest_view, json_safe, not_tested_reason, result_paths)

log = logging.getLogger(__name__)

Q_THRESHOLD = 0.05

#: Variants of a module result in the order a view prefers them: pseudobulk of raw counts (B), then the mean of
#: per-cell UCell scores, then the mean of per-cell score_genes scores (both A).
VARIANT_ORDER: Tuple[Tuple[str, str], ...] = (('pseudobulk', 'gene_list'), ('cell_mean', 'ucell'), ('cell_mean', 'geneset'))
VARIANT_LABELS = {
    ('pseudobulk', 'gene_list'): 'pseudobulk of raw counts (B)',
    ('cell_mean', 'ucell'): 'mean of per-cell UCell scores (A)',
    ('cell_mean', 'geneset'): 'mean of per-cell scores (A)',
}
#: Effect size (standardised) at which both variants must reach the gate for an opposite-sign call to count.
AB_EFFECT = 0.2
#: Smallest permutation p value a run of 10,000 shuffles can give; a parametric p below it is floored before the
#: two p values are compared.
PERMUTATION_P_FLOOR = 1.0 / 10001
#: The parametric and the permutation p values of a module disagree strongly when they differ by this factor
#: (after the floor) and the smaller one is below ``P_DISAGREE_ALPHA``.
P_DISAGREE_FOLD = 10.0
P_DISAGREE_ALPHA = 0.05
#: A unit is flagged (``p_check_flag``) when at least this share of its modules disagree strongly.
UNIT_P_CHECK_FRACTION = 0.10
#: Share of mitochondrial (or ribosomal) member genes from which a module is flagged quality-sensitive.
QUALITY_SHARE = 0.5

MODULE_ROW_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'method', 'source', 'catalog', 'module', 'estimate', 'se', 'ci_low',
    'ci_high', 'std_effect', 'score_sd', 'df', 'p_param', 'q_param', 'p_perm', 'q_perm', 'n_genes_requested',
    'n_genes_found', 'directionless', 'no_counts', 'n_donors', 'n_donors_ref', 'n_donors_test', 'descriptive_only', 'small_sample', 'n_lt5',
    'facet_scope', 'contrast', 'level_ref', 'level_test')
MODULE_UNIT_COLUMNS = (
    'unit', 'db_id', 'title', 'cell_type', 'celltype_source', 'n_donors', 'n_donors_ref', 'n_donors_test',
    'level_ref', 'level_test', 'descriptive_only', 'cohort', 'cohort_label', 'role', 'counted', 'independence',
    'overlap_unknown', 'pooled', 'disease_basis', 'exclusion_missing', 'is_secondary', 'celltype_group',
    'facet_scope', 'modality', 'modality_note', 'has_counts', 'analysis_level', 'design_subset', 'per_region_only',
    'p_check_fraction', 'p_check_flag')
_FLOAT_COLUMNS = ('estimate', 'se', 'ci_low', 'ci_high', 'std_effect', 'score_sd', 'df', 'p_param', 'q_param', 'p_perm',
                  'q_perm')


def default_directionless(module_key: str) -> bool:
    """Fallback for ``ModuleResults(directionless=...)``: a catalog whose study name contains ``GWAS``."""
    return 'gwas' in str(module_key).split('/', 1)[0].lower()


def select_p(frame: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The p and q a unit contributes to replication: parametric when its residual df (``df``) is at least
    ``facet_rules.MODULE_PARAMETRIC_MIN_DF``, else permutation (also when ``df`` is missing).

    Returns:
        ``(p, q, parametric)``: two-sided p, BH q of the unit and whether the parametric basis was used.
    """
    degrees = pd.to_numeric(frame['df'], errors='coerce').to_numpy(dtype='float64') if 'df' in frame.columns \
        else np.full(len(frame), np.nan)
    parametric = np.nan_to_num(degrees, nan=-1.0) >= facet_rules.MODULE_PARAMETRIC_MIN_DF
    def column(name: str) -> np.ndarray:
        return frame[name].to_numpy(dtype='float64') if name in frame.columns else np.full(len(frame), np.nan)

    p_param, p_perm, q_param, q_perm = (column(name) for name in ('p_param', 'p_perm', 'q_param', 'q_perm'))
    return np.where(parametric, p_param, p_perm), np.where(parametric, q_param, q_perm), parametric


def p_methods_disagree(p_param: np.ndarray, p_perm: np.ndarray) -> np.ndarray:
    """Strong disagreement of the parametric and the permutation p: a fold of ``P_DISAGREE_FOLD`` or more (the
    parametric p floored at the permutation resolution) with the smaller one below ``P_DISAGREE_ALPHA``."""
    floored = np.maximum(p_param, PERMUTATION_P_FLOOR)
    with np.errstate(divide='ignore', invalid='ignore'):
        fold = np.abs(np.log10(floored / np.maximum(p_perm, PERMUTATION_P_FLOOR)))
    wide = np.nan_to_num(fold, nan=0.0) >= np.log10(P_DISAGREE_FOLD)
    return wide & (np.fmin(floored, p_perm) < P_DISAGREE_ALPHA)


def standardised_bounds(frame: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """95 percent confidence bounds of the standardised effect (``ci / SD of the donor-level score``).

    The SD is ``score_sd`` or, in older files, ``estimate / std_effect``. A row without an interval (or SD) falls
    back to its point estimate on both sides, so the gate is then evaluated on the point estimate.
    """
    effect = frame['std_effect'].to_numpy(dtype='float64')
    with np.errstate(divide='ignore', invalid='ignore'):
        deviation = np.abs(frame['estimate'].to_numpy(dtype='float64') / effect)
    if 'score_sd' in frame.columns:
        recorded = frame['score_sd'].to_numpy(dtype='float64')
        deviation = np.where(np.isfinite(recorded), recorded, deviation)
    missing = np.full(len(frame), np.nan)
    with np.errstate(divide='ignore', invalid='ignore'):
        low = (frame['ci_low'].to_numpy(dtype='float64') if 'ci_low' in frame.columns else missing) / deviation
        high = (frame['ci_high'].to_numpy(dtype='float64') if 'ci_high' in frame.columns else missing) / deviation
    usable = np.isfinite(low) & np.isfinite(high)
    return np.where(usable, low, effect), np.where(usable, high, effect)


def unit_is_small(n_donors: Any, n_ref: Any = None, n_test: Any = None) -> bool:
    """A cohort is small (flag only, still counted) when the smaller level of a two-group facet has fewer than
    ``facet_rules.SMALL_LEVEL_REPLICATES`` replicates; without level counts (a trend) when it has fewer than
    ``SMALL_COHORT_REPLICATES`` replicates in all."""
    reference, tested = _as_int(n_ref), _as_int(n_test)
    if reference is not None and tested is not None:
        return min(reference, tested) < facet_rules.SMALL_LEVEL_REPLICATES
    return _is_small(n_donors)


def design_subset_of(celltype_source: Any) -> Optional[str]:
    """The ``app_nlgf`` design stratum of a unit (``timepoint=6m|line=APOE33``) from its ``celltype_source``
    (``ant_cell_type@timepoint=6m|line=APOE33``), or None for an unstratified unit."""
    text = str(celltype_source)
    return text.split('@', 1)[1] if '@' in text else None


def module_key(catalog: Any, module: Any) -> str:
    """``catalog/module`` (``Mancuso2024/Cell_states/DAM``), the id of a module and of a gene set."""
    return f'{catalog}/{module}'


def variant_label(method: str, source: str) -> str:
    """Plain label of a (method, source) variant."""
    return VARIANT_LABELS.get((method, source), f'{method} ({source})')


def ab_disagreement(a_entry: Optional[Dict[str, Any]], b_entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whether the cell-mean (A) and pseudobulk (B) results of one module contradict each other.

    Called with the two variant entries (``r_value``, ``median_std_effect``, ``r_direction``). The flag is raised
    only when the directions are opposite and either variant is replicated or both effects reach
    ``AB_EFFECT``; weak opposite-sign noise is not a disagreement. None when either variant is missing.
    """
    if a_entry is None or b_entry is None:
        return None
    effect_a, effect_b = a_entry.get('median_std_effect'), b_entry.get('median_std_effect')
    if effect_a is None or effect_b is None:
        return {'disagree': False, 'reason': None}
    opposite = effect_a * effect_b < 0
    either_replicated = any(entry.get('call') == 'replicated' for entry in (a_entry, b_entry))
    both_large = abs(effect_a) >= AB_EFFECT and abs(effect_b) >= AB_EFFECT
    disagree = bool(opposite and (either_replicated or both_large))
    reason = None
    if disagree:
        reason = ('opposite directions and one of the two is replicated' if either_replicated
                  else f'opposite directions with both effects of at least {AB_EFFECT} standard deviations')
    return {'disagree': disagree, 'reason': reason}


class ModuleResults:
    """Module replication over the facet results of a ``FacetResults`` reader.

    Args:
        reader: The shared ``FacetResults`` (its cohort table, summaries, file keys and single flight builds).
        directionless: ``module_key -> bool``, True for a set without an expected direction (left out of every
            family); default ``default_directionless`` (GWAS studies). A ``directionless`` column of the result file
            (written by ``ant-facet-test-modules``) counts as well.
    """

    def __init__(self, reader: FacetResults, directionless: Optional[Callable[[str], bool]] = None):
        self.reader = reader
        self.directionless = directionless or default_directionless
        self._tables: "OrderedDict[Tuple, Tuple[Tuple, Any]]" = OrderedDict()
        self._families: "OrderedDict[Tuple, Tuple[Tuple, Any]]" = OrderedDict()
        self._cards: "OrderedDict[Tuple, Tuple[Tuple, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    # ── reading ────────────────────────────────────────────────────────────

    def tables(self, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets"
               ) -> Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]]:
        """``(facet, scope) -> (rows, units)`` over every dataset with module results, cached until a file changes.

        ``rows``: ``unit`` (index into ``units``), ``method``, ``source``, ``module_key`` (``catalog/module``),
        ``catalog``, ``module``, ``estimate``, ``se``, ``ci_low``, ``ci_high``, ``std_effect``, ``p_param``,
        ``q_param``, ``p_perm``, ``q_perm``, ``n_genes_found``, ``n_genes_requested``, ``no_counts``.
        ``units``: one row per (dataset, stratum) with the fields of the gene unit table
        (``MODULE_UNIT_COLUMNS``). Donor-pooled units have no module counterpart.
        """
        prepared = self.reader.prepare(datasets)
        return self.reader._cached_build(self._tables, ('tables',), prepared.signature,
                                         lambda: self._build_tables(prepared.datasets), max_entries=1)

    def _read_dataset(self, dataset: Dict[str, Any], cohorts: Dict[str, Dict[str, Any]]
                      ) -> Dict[str, Tuple[List[Dict[str, Any]], List[pd.DataFrame]]]:
        """``facet -> (unit dicts, row frames)`` of one dataset's module results (empty when none)."""
        if str(dataset['db_id']).startswith(POOL_PREFIX):
            return {}
        path = result_paths(self.reader.results_dir, dataset['group_folder'], dataset['stem'])['modules']
        if _file_key(path) is None:
            return {}
        try:
            import pyarrow.parquet as pq
            names = set(pq.read_schema(path).names)
            frame = pd.read_parquet(path, columns=[c for c in MODULE_ROW_COLUMNS if c in names])
        except Exception as exc:  # partial write, corrupt file
            log.warning('facet module results: cannot read %s: %s', path, exc)
            return {}
        if frame.empty or 'facet' not in frame.columns:
            return {}
        frame = _rename_deprecated_facets(frame)
        for column in _FLOAT_COLUMNS:
            if column not in frame.columns:
                frame[column] = np.nan
        for column, default in (('no_counts', False), ('small_sample', False), ('descriptive_only', False),
                                ('n_lt5', False), ('directionless', False)):
            if column not in frame.columns:
                frame[column] = default
        manifest = self.reader._read_json(result_paths(self.reader.results_dir, dataset['group_folder'],
                                                       dataset['stem'])['module_manifest']) or {}
        cohort = cohorts.get(dataset['db_id']) or {}
        counted = bool(cohort.get('counted'))
        exclusion_missing = False
        if cohort.get('donor_exclusion_required'):
            applied = (manifest.get('donor_exclusion') or {}).get('counted') is True
            exclusion_missing = not applied
            counted = counted and applied
        pool_members = {member for record in cohorts.values() for member in record.get('pool_members') or []}
        out: Dict[str, Tuple[List[Dict[str, Any]], List[pd.DataFrame]]] = {}
        for (facet, cell_type, celltype_source), group in frame.groupby(
                ['facet', 'cell_type', 'celltype_source'], sort=False, observed=True):
            first = group.iloc[0]
            descriptive = bool(group['descriptive_only'].astype(bool).any())
            p_used, q_used, parametric = select_p(group)
            std_low, std_high = standardised_bounds(group)
            disagree = p_methods_disagree(group['p_param'].to_numpy(dtype='float64'),
                                          group['p_perm'].to_numpy(dtype='float64'))
            comparable = group['p_param'].notna().to_numpy() & group['p_perm'].notna().to_numpy()
            p_check_fraction = float(disagree.sum() / comparable.sum()) if comparable.any() else None
            scope = first['facet_scope'] if 'facet_scope' in group.columns and isinstance(first['facet_scope'], str) \
                else dataset['facet_scope']
            unit = {
                'db_id': dataset['db_id'], 'title': dataset.get('title') or dataset['db_id'],
                'cell_type': str(cell_type), 'celltype_source': str(celltype_source),
                'n_donors': _as_int(first.get('n_donors')), 'n_donors_ref': _as_int(first.get('n_donors_ref')),
                'n_donors_test': _as_int(first.get('n_donors_test')),
                'level_ref': first.get('level_ref') if isinstance(first.get('level_ref'), str) else None,
                'level_test': first.get('level_test') if isinstance(first.get('level_test'), str) else None,
                'descriptive_only': descriptive,
                'cohort': cohort.get('cohort') or None, 'cohort_label': cohort.get('cohort_label') or None,
                'role': cohort.get('role') or None, 'counted': counted,
                'independence': cohort.get('independence') or 'checked',
                'overlap_unknown': bool(cohort.get('overlap_unknown')), 'pooled': False,
                'disease_basis': dataset.get('disease_basis') or manifest.get('disease_basis'),
                'exclusion_missing': exclusion_missing,
                'is_secondary': bool(dataset.get('is_secondary')), 'celltype_group': group_of(cell_type),
                'facet_scope': scope, 'modality': dataset.get('modality'), 'modality_note': dataset.get('modality_note'),
                'has_counts': not bool(group['no_counts'].astype(bool).all()),
                'analysis_level': 'replicate' if not descriptive else 'cell',
                'design_subset': design_subset_of(celltype_source),
                'per_region_only': dataset['db_id'] in pool_members,
                'p_check_fraction': p_check_fraction,
                'p_check_flag': bool(p_check_fraction is not None and p_check_fraction >= UNIT_P_CHECK_FRACTION),
            }
            catalog = group['catalog'].astype(str)
            part = pd.DataFrame({
                'method': group['method'].astype(str).to_numpy(), 'source': group['source'].astype(str).to_numpy(),
                'module_key': (catalog + '/' + group['module'].astype(str)).to_numpy(),
                'catalog': catalog.to_numpy(), 'module': group['module'].astype(str).to_numpy(),
                **{column: pd.to_numeric(group[column], errors='coerce').to_numpy(dtype='float64')
                   for column in _FLOAT_COLUMNS},
                'n_genes_found': pd.to_numeric(group.get('n_genes_found'), errors='coerce').to_numpy(dtype='float64')
                if 'n_genes_found' in group.columns else np.nan,
                'n_genes_requested': pd.to_numeric(group.get('n_genes_requested'), errors='coerce').to_numpy(
                    dtype='float64') if 'n_genes_requested' in group.columns else np.nan,
                'no_counts': group['no_counts'].astype(bool).to_numpy(),
                'p_use': p_used, 'q_use': q_used, 'p_parametric': parametric, 'p_disagree': disagree,
                'std_ci_low': std_low, 'std_ci_high': std_high,
                'directionless': group['directionless'].fillna(False).astype(bool).to_numpy(),
            })
            entry = out.setdefault(str(facet), ([], []))
            entry[0].append(unit)
            entry[1].append(part)
        return out

    def _build_tables(self, datasets: Sequence[Dict[str, Any]]
                      ) -> Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]]:
        cohorts = self.reader.cohorts()
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            per_dataset = list(pool.map(lambda dataset: self._read_dataset(dataset, cohorts), datasets))
        unit_rows: Dict[str, List[Dict[str, Any]]] = {}
        parts: Dict[str, List[pd.DataFrame]] = {}
        for facet_tables in per_dataset:
            for facet, (unit_dicts, frames) in facet_tables.items():
                numbered = unit_rows.setdefault(facet, [])
                for unit_dict, frame in zip(unit_dicts, frames):
                    frame.insert(0, 'unit', np.full(len(frame), len(numbered), dtype='int32'))
                    numbered.append({'unit': len(numbered), **unit_dict})
                    parts.setdefault(facet, []).append(frame)
        tables: Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]] = {}
        for facet, facet_units in unit_rows.items():
            units = pd.DataFrame(facet_units, columns=MODULE_UNIT_COLUMNS)
            rows = pd.concat(parts[facet], ignore_index=True)
            for column in ('method', 'source', 'catalog', 'module'):
                rows[column] = rows[column].astype('category')
            rows['module_key'] = rows['module_key'].astype('category')
            flagged = {name: bool(self.directionless(name)) for name in rows['module_key'].cat.categories}
            rows['directionless'] = rows['directionless'].to_numpy(dtype=bool) \
                | rows['module_key'].map(flagged).astype(bool).to_numpy()
            for scope in sorted(set(units['facet_scope'])):
                scope_units = units[units['facet_scope'] == scope]
                tables[(facet, scope)] = (rows if len(scope_units) == len(units)
                                          else rows[rows['unit'].isin(scope_units['unit'])], scope_units)
        return tables

    # ── replication ────────────────────────────────────────────────────────

    def families(self, facet: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets",
                 exclude_secondary: bool = True, scope: Optional[str] = None
                 ) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """Replication of every module of one facet in one scope: ``(group, method, source) -> {frame, per_cohort, units}``.

        ``frame`` is indexed by ``catalog/module`` with ``p_pc``, ``r_value``, ``r_direction``, ``m``, ``k``
        (cohorts with q_perm < 0.05 in the direction), ``agreement``, ``median_std_effect``,
        ``median_estimate``, ``n_cohorts_ge_gate``, ``effect_gate_passed``, ``effect_gate_cohorts``,
        ``r_cohorts``, ``r_significant`` and ``replicated`` (= r < 0.05). Cached until a result file changes.
        """
        facet = canonical_facet(facet)
        scope = scope or FacetResults.default_scope(facet)
        prepared = self.reader.prepare(datasets)

        def build() -> Dict[Tuple[str, str, str], Dict[str, Any]]:
            rows, units = self.tables(prepared).get((facet, scope)) or (None, None)
            families: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            if rows is None or not len(units):
                return families
            units = FacetResults.counting_units(units, exclude_secondary, facet)
            counted = units[units['counted_effective']]
            with self.reader._family_slots:
                for group, group_units in counted.groupby('celltype_group'):
                    sub = rows[rows['unit'].isin(group_units['unit']) & ~rows['directionless']]
                    for (method, source), part in sub.groupby(['method', 'source'], observed=True):
                        families[(group, str(method), str(source))] = self._family(part, group_units, facet)
            return families

        return self.reader._cached_build(self._families, (facet, scope, bool(exclude_secondary)), prepared.signature,
                                         build, max_entries=FAMILY_CACHE_ENTRIES)

    @staticmethod
    def _family(sub: pd.DataFrame, group_units: pd.DataFrame, facet: str = '') -> Dict[str, Any]:
        """Replicability table of one (group, method, source) family (see ``families``) of ``facet``."""
        gate = facet_rules.module_gate(facet)
        sub = sub.merge(group_units[['unit', 'cohort', 'n_siblings']], on='unit', how='left')
        key_codes, key_names = pd.factorize(sub['module_key'].astype(str))
        sub = sub.assign(key=key_codes)
        p_two = sub['p_use']
        half = p_two / 2.0
        estimate = sub['estimate']
        sub['p_up'] = np.where(p_two.isna(), np.nan, np.where(estimate >= 0, half, 1.0 - half))
        sub['p_down'] = np.where(p_two.isna(), np.nan, np.where(estimate <= 0, half, 1.0 - half))
        q_adjusted = (sub['q_use'] * sub['n_siblings']).clip(upper=1.0)
        sub['up'] = (q_adjusted < Q_THRESHOLD) & (estimate > 0)
        sub['down'] = (q_adjusted < Q_THRESHOLD) & (estimate < 0)
        per_cohort = sub.groupby(['key', 'cohort'], observed=True).agg(
            p_up=('p_up', 'min'), p_down=('p_down', 'min'), n_siblings=('n_siblings', 'max'),
            up=('up', 'any'), down=('down', 'any'), effect=('std_effect', 'mean'), raw=('estimate', 'mean'),
            bound_low=('std_ci_low', 'mean'), bound_high=('std_ci_high', 'mean'), disagree=('p_disagree', 'any')
        ).reset_index()
        for column in ('p_up', 'p_down'):
            per_cohort[column] = (per_cohort[column] * per_cohort['n_siblings']).clip(upper=1.0)
        p_up = per_cohort.pivot(index='key', columns='cohort', values='p_up')
        p_down = per_cohort.pivot(index='key', columns='cohort', values='p_down')
        stats_ = replication.replicability(p_up.to_numpy(), p_down.to_numpy(), method=PC_METHOD)
        frame = pd.DataFrame({'p_pc': stats_['p_pc'], 'r_value': stats_['r_value'],
                              'r_direction': stats_['direction']}, index=p_up.index)
        frame['m'] = p_up.notna().sum(axis=1).astype(int)
        for source, target in (('up', 'k_up'), ('down', 'k_down')):
            frame[target] = per_cohort.groupby('key')[source].sum().reindex(frame.index).fillna(0).astype(int)
        frame['k'] = frame[['k_up', 'k_down']].max(axis=1)
        direction_sign = frame['r_direction'].map({'up': 1.0, 'down': -1.0})
        tested = per_cohort['p_up'].notna().to_numpy()
        sign = np.sign(per_cohort['effect'])
        matches = pd.Series(sign.to_numpy() == direction_sign.reindex(per_cohort['key']).to_numpy() * 1.0,
                            index=per_cohort.index) & tested
        n_tested = pd.Series(tested, index=per_cohort.index).groupby(per_cohort['key']).sum()
        frame['agreement'] = (matches.groupby(per_cohort['key']).sum() / n_tested.where(n_tested > 0)
                              ).reindex(frame.index).where(direction_sign.notna())
        frame['median_std_effect'] = per_cohort.groupby('key')['effect'].median().reindex(frame.index)
        frame['median_estimate'] = per_cohort.groupby('key')['raw'].median().reindex(frame.index)
        low = per_cohort.pivot(index='key', columns='cohort', values='bound_low').reindex(
            index=p_up.index, columns=p_up.columns)
        high = per_cohort.pivot(index='key', columns='cohort', values='bound_high').reindex(
            index=p_up.index, columns=p_up.columns)
        # the gate is read on the confidence bound nearer to zero in the replicated direction
        up_rows = (frame['r_direction'] == 'up').to_numpy()[:, None]
        signed = pd.DataFrame(np.where(up_rows, low.to_numpy(), -high.to_numpy()), index=p_up.index, columns=p_up.columns)
        carries = (signed >= gate) & direction_sign.notna().to_numpy()[:, None]
        frame['n_cohorts_ge_gate'] = carries.sum(axis=1).astype(int)
        frame['effect_gate_passed'] = frame['n_cohorts_ge_gate'] >= replication.DEFAULT_U
        names = np.asarray(p_up.columns, dtype=object)
        order = np.argsort(-np.nan_to_num(signed.to_numpy(), nan=-np.inf), axis=1)
        carried = carries.to_numpy()
        frame['effect_gate_cohorts'] = [[names[i] for i in row_order if row_carries[i]]
                                        for row_order, row_carries in zip(order, carried)]
        flagged_cohorts = per_cohort.pivot(index='key', columns='cohort', values='disagree').reindex(
            index=p_up.index, columns=p_up.columns).fillna(False).astype(bool)
        frame['p_check_cohorts'] = [[names[i] for i in np.flatnonzero(row)] for row in flagged_cohorts.to_numpy()]
        best = np.where((frame['r_direction'] == 'up').to_numpy()[:, None], p_up.to_numpy(), p_down.to_numpy())
        best = np.nan_to_num(best, nan=np.inf)
        carriers = np.argsort(best, axis=1, kind='stable')[:, :replication.DEFAULT_U]
        finite = np.isfinite(np.take_along_axis(best, carriers, axis=1))
        frame['r_cohorts'] = [[names[i] for i, ok in zip(row, flags) if ok]
                              for row, flags in zip(carriers.tolist(), finite.tolist())]
        frame['r_significant'] = frame['r_value'] < replication.R_THRESHOLD
        frame['replicated'] = frame['r_significant']
        frame.index = pd.Index(key_names[frame.index.to_numpy(dtype=int)], name='module_key')
        per_cohort['key'] = key_names[per_cohort['key'].to_numpy(dtype=int)]
        tested_by_key = per_cohort[per_cohort['p_up'].notna()].groupby('key')['cohort'].agg(set).to_dict()
        return {'frame': frame.sort_index(), 'per_cohort': per_cohort, 'units': group_units,
                'tested_cohorts': tested_by_key, 'gate': gate, 'facet': facet}

    # ── entries ────────────────────────────────────────────────────────────

    @staticmethod
    def _entry(record: pd.Series, units_of_group: pd.DataFrame, tested: Sequence[Any] = (),
               gate_sd: float = facet_rules.MODULE_STD_EFFECT_GATE, facet: str = '') -> Dict[str, Any]:
        """The replication entry of one module in one family from its ``frame`` row (JSON-safe values).

        ``tested`` are the cohorts that have a p value for the module (``family['tested_cohorts']``); ``gate_sd`` is the
        facet's effect gate in SD (``family['gate']``) and ``facet`` names its unit for the wording.
        """
        label_of = dict(zip(units_of_group['cohort'], units_of_group['cohort_label']))
        small_of = {cohort: unit_is_small(donors, n_ref, n_test) for cohort, donors, n_ref, n_test in zip(
            units_of_group['cohort'], units_of_group['n_donors'], units_of_group['n_donors_ref'],
            units_of_group['n_donors_test'])}
        per_region = sorted({str(label_of.get(c) or c) for c, flag in zip(
            units_of_group['cohort'], units_of_group['per_region_only']) if flag and c in set(tested)}) \
            if 'per_region_only' in units_of_group.columns else []

        def labels(cohorts: Any) -> List[str]:
            return [str(label_of.get(c) or c) for c in (cohorts if isinstance(cohorts, list) else [])]

        r_value = _finite(record.get('r_value'))
        m = _as_int(record.get('m')) or 0
        k = _as_int(record.get('k'))
        small = sorted({str(label_of.get(c) or c) for c in tested if small_of.get(c)})
        call = ('replicated' if r_value < replication.R_THRESHOLD else 'not_replicated') if (m >= 2 and r_value is not None) \
            else 'not_tested'
        gate = bool(call == 'replicated' and record.get('effect_gate_passed'))
        label = f'{k}/{m} cohorts' if k is not None and m else f'{m} cohort' + ('' if m == 1 else 's')
        if small:
            label += f', {len(small)} small'
        return {
            'r_value': r_value, 'p_pc': _finite(record.get('p_pc')), 'k': k, 'm': m,
            'r_direction': record.get('r_direction') if isinstance(record.get('r_direction'), str) else None,
            'replicated': call == 'replicated', 'r_significant': call == 'replicated', 'call': call,
            'nt_label': None if call != 'not_tested' else ('1 cohort' if m == 1 else 'not tested'),
            'effect_gate_passed': gate if call == 'replicated' else None, 'gate': gate,
            'effect_gate_tau': gate_sd,
            'effect_gate_unit': facet_rules.module_gate_words(facet) if facet else f'{gate_sd:g} standard deviations of the donor-level score',
            'effect_gate_cohorts': labels(record.get('effect_gate_cohorts')),
            'n_cohorts_ge_gate': _as_int(record.get('n_cohorts_ge_gate')),
            'strict': None, 'r_strong': None, 'replicated_strong': None, 'r_treat': None, 'replicated_effect': None,
            'median_std_effect': _finite(record.get('median_std_effect')),
            'median_estimate': _finite(record.get('median_estimate')),
            'agreement': _finite(record.get('agreement')), 'n_cohorts_tested': m,
            'p_check_cohorts': labels(record.get('p_check_cohorts')),
            'p_check_flag': bool(len(record.get('p_check_cohorts') or []) > 0) if 'p_check_cohorts' in record else False,
            'per_region_only_cohorts': per_region,
            'module_scope_note': ('Module results come from one regional file of these cohorts (their gene results are '
                                  'donor-pooled over regions): ' + ', '.join(per_region)) if per_region else None,
            'r_cohorts': labels(record.get('r_cohorts')), 'n_small': len(small), 'small_cohorts': small,
            'k_over_m': f'{k}/{m}' if k is not None and m else None, 'cohorts_label': label,
        }

    @staticmethod
    def _family_ranks(family: Dict[str, Any]) -> Dict[str, int]:
        """``module_key -> rank`` (1 = strongest) among the modules of one family that have an r-value.

        Ordered by ``p_pc`` (the r-value is its Benjamini-Hochberg adjustment, so the order agrees with the top list
        but is not tied where the r-value saturates at 1), then ``k``, ``agreement`` and the absolute median
        standardised effect. Built once per family and kept in it.
        """
        ranks = family.get('ranks')
        if ranks is None:
            frame = family['frame']
            usable = frame[frame['r_value'].notna()]
            ordered = usable.assign(_abs=usable['median_std_effect'].abs()).sort_values(
                ['p_pc', 'k', 'agreement', '_abs'], ascending=[True, False, False, False], kind='stable')
            ranks = family['ranks'] = {key: position for position, key in enumerate(ordered.index, start=1)}
        return ranks

    @staticmethod
    def _point(record: Dict[str, Any]) -> Dict[str, Any]:
        """One forest row of a module result (slim): estimate with its interval, standardised effect and p values."""
        return {
            'cohort_label': _clean_text(record.get('cohort_label')), 'db_id': record['db_id'],
            'title': record.get('title'), 'cell_type': record['cell_type'], 'celltype_source': record['celltype_source'],
            'design_subset': design_subset_of(record['celltype_source']), 'anchor_counted': bool(record.get('counted_effective')),
            'n_donors': _as_int(record.get('n_donors')), 'n_donors_ref': _as_int(record.get('n_donors_ref')),
            'n_donors_test': _as_int(record.get('n_donors_test')),
            'n_ref': _as_int(record.get('n_donors_ref')), 'n_test': _as_int(record.get('n_donors_test')),
            'small_sample': unit_is_small(record.get('n_donors'), record.get('n_donors_ref'), record.get('n_donors_test')),
            'estimate': _rounded(record.get('estimate'), 5), 'ci_low': _rounded(record.get('ci_low'), 5),
            'ci_high': _rounded(record.get('ci_high'), 5), 'std_effect': _rounded(record.get('std_effect'), 4),
            'p_param': _finite(record.get('p_param')), 'q_param': _finite(record.get('q_param')),
            'p_perm': _finite(record.get('p_perm')), 'q_perm': _finite(record.get('q_perm')),
            'df_resid': _finite(record.get('df')), 'p_used': _finite(record.get('p_use')),
            'p_basis': 'parametric' if record.get('p_parametric') is True else 'permutation',
            'p_disagree': bool(record.get('p_disagree') is True), 'p_check_flag': bool(record.get('p_check_flag') is True),
            'p_check_fraction': _rounded(record.get('p_check_fraction'), 4),
            'per_region_only': bool(record.get('per_region_only') is True),
            'has_counts': bool(record.get('has_counts')), 'descriptive_only': bool(record.get('descriptive_only')),
            'modality': record.get('modality'), 'modality_note': record.get('modality_note'),
            'n_genes_found': _as_int(record.get('n_genes_found')),
            'n_genes_requested': _as_int(record.get('n_genes_requested')),
            'not_counted_reason': None if record.get('counted_effective') else _not_counted_reason(record),
        }

    @staticmethod
    def _folded(record: Dict[str, Any]) -> Dict[str, Any]:
        """A unit folded under ``more_units``: estimate, interval and why it is not the anchor."""
        return {
            'db_id': record['db_id'], 'title': record.get('title'), 'cell_type': record['cell_type'],
            'celltype_source': record['celltype_source'], 'anchor_counted': bool(record.get('counted_effective')),
            'n_donors': _as_int(record.get('n_donors')), 'method': str(record.get('method')),
            'source': str(record.get('source')), 'estimate': _rounded(record.get('estimate'), 5),
            'ci_low': _rounded(record.get('ci_low'), 5), 'ci_high': _rounded(record.get('ci_high'), 5),
            'std_effect': _rounded(record.get('std_effect'), 4), 'p_perm': _finite(record.get('p_perm')),
            'modality': record.get('modality'),
            'not_counted_reason': None if record.get('counted_effective') else _not_counted_reason(record),
        }

    @classmethod
    def _forest(cls, merged: pd.DataFrame) -> List[Dict[str, Any]]:
        """One forest row per cohort (its counted anchor, other units folded in ``more_units``), counted first."""
        records_sorted = merged.to_dict(orient='records')
        records_sorted.sort(key=lambda r: (not r['counted_effective'], -(_as_int(r.get('n_donors')) or 0),
                                           str(r['db_id']), str(r['cell_type'])))
        by_cohort: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for record in records_sorted:
            by_cohort.setdefault(_clean_text(record.get('cohort')), []).append(record)
        entries: List[Dict[str, Any]] = []
        for cohort, records in by_cohort.items():
            counted = [record for record in records if record.get('counted_effective')]
            shown = counted[0] if counted else records[0]
            rest = [cls._folded(record) for record in records if record is not shown]
            point = cls._point(shown)
            entries.append({
                'cohort': cohort, 'cohort_label': point['cohort_label'],
                'cohort_display': cohort_display(point['cohort_label']), 'counted': bool(counted),
                'in_m': bool(counted) and point['p_perm'] is not None,
                'small': point['small_sample'] if counted else None,
                'anchor': point, 'more_units': rest, 'n_more_units': len(rest)})
        entries.sort(key=lambda e: (not e['counted'], str(e['cohort_display']).lower()))
        return entries

    @staticmethod
    def _preferred(variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The variant a view shows: the first of ``VARIANT_ORDER`` with a replication call (two or more cohorts),
        else the first with any estimate."""
        for variant in variants:
            if variant['call'] != 'not_tested':
                return variant
        return variants[0] if variants else None

    # ── one module ─────────────────────────────────────────────────────────

    def module_effects(self, catalog: str, module: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets",
                       facet: str, exclude_secondary: bool = True, scope: Optional[str] = None,
                       cell_type: Optional[str] = None) -> Dict[str, Any]:
        """The result card of one module for one facet in one scope, cached per module until a file changes.

        The returned dict is shared between callers: do not modify it.
        """
        facet = canonical_facet(facet)
        scope = scope or FacetResults.default_scope(facet)
        prepared = self.reader.prepare(datasets)
        return self.reader._cached_build(
            self._cards, (facet, module_key(catalog, module).lower(), cell_type, bool(exclude_secondary), scope),
            prepared.signature,
            lambda: self._module_effects(catalog, module, prepared, facet, exclude_secondary, scope, cell_type),
            max_entries=256)

    def _module_effects(self, catalog: str, module: str, prepared: PreparedDatasets, facet: str,
                        exclude_secondary: bool, scope: str, cell_type: Optional[str]) -> Dict[str, Any]:
        rows, units = self.tables(prepared).get((facet, scope)) or (None, None)
        families = self.families(facet, prepared, exclude_secondary, scope)
        key = module_key(catalog, module)
        wanted = key.lower()
        replication_entries: List[Dict[str, Any]] = []
        unit_cards: List[Dict[str, Any]] = []
        if rows is not None and len(units):
            keys = rows['module_key'].cat.categories
            hits = [name for name in keys if name.lower() == wanted]
            module_rows = rows[rows['module_key'].isin(hits)] if hits else rows.iloc[0:0]
            key = hits[0] if hits else key
            if len(module_rows):
                counting = FacetResults.counting_units(units, exclude_secondary, facet)
                merged = module_rows.merge(counting, on='unit', how='left')
                group_filter = None
                if cell_type:
                    group_filter = cell_type if cell_type in set(counting['celltype_group']) else group_of(cell_type)
                    merged = merged[merged['celltype_group'] == group_filter]
                for group, sub in merged.groupby('celltype_group', sort=False):
                    variants = []
                    for method, source in VARIANT_ORDER:
                        part = sub[(sub['method'].astype(str) == method) & (sub['source'].astype(str) == source)]
                        if part.empty:
                            continue
                        family = families.get((group, method, source))
                        record = family['frame'].loc[key] if family is not None and key in family['frame'].index else None
                        if record is None:
                            entry = self._entry(pd.Series({'m': 0}), counting[counting['celltype_group'] == group],
                                                gate_sd=facet_rules.module_gate(facet), facet=facet)
                            if sub['directionless'].any():
                                entry.update(directionless=True, nt_label='no expected direction')
                        else:
                            entry = self._entry(record, family['units'], family['tested_cohorts'].get(key, ()),
                                                gate_sd=family['gate'], facet=facet)
                            ranks = self._family_ranks(family)
                            entry['family_rank'] = ranks.get(key)
                            entry['family_size'] = len(ranks)
                        variants.append({
                            'method': method, 'source': source, 'label': variant_label(method, source),
                            'basis': 'pseudobulk' if method == 'pseudobulk' else 'cell_mean',
                            'forest': self._forest(part), **entry})
                    if not variants:
                        continue
                    primary = self._preferred(variants)
                    side_a = next((v for v in variants if v['method'] == 'cell_mean'), None)
                    side_b = next((v for v in variants if v['method'] == 'pseudobulk'), None)
                    replication_entries.append({
                        'cell_type': group, 'celltype_group': group, 'cl_id': group_info(group)['cl_id'],
                        'labels': sorted(set(sub['cell_type'])),
                        'primary': {'method': primary['method'], 'source': primary['source'], 'label': primary['label']},
                        'variants': variants, 'ab': ab_disagreement(side_a, side_b)})
                merged = merged.assign(_counted=~merged['counted_effective'].astype(bool),
                                       _donors=-pd.to_numeric(merged['n_donors'], errors='coerce').fillna(0))
                for record in merged.sort_values(['celltype_group', '_counted', 'cohort_label', '_donors', 'cell_type',
                                                  'method', 'source']).to_dict(orient='records'):
                    unit_cards.append({
                        **self._point(record), 'method': str(record['method']), 'source': str(record['source']),
                        'celltype_group': record['celltype_group'], 'facet_scope': record.get('facet_scope'),
                        'counted': bool(record.get('counted_effective')),
                        'is_secondary': bool(record.get('is_secondary')),
                        'level_ref': record.get('level_ref'), 'level_test': record.get('level_test')})
        return json_safe({
            'catalog': key.rsplit('/', 1)[0] if '/' in key else catalog, 'module': key.rsplit('/', 1)[-1],
            'module_key': key, 'facet': facet, 'scope': scope, 'cell_type': cell_type,
            'directionless': bool(self.directionless(key)), 'effect_gate': facet_rules.module_gate(facet),
            'replication': replication_entries, 'n_units': len(unit_cards), 'units': unit_cards})

    def scored_in(self, catalog: str, module: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets"
                  ) -> List[Dict[str, Any]]:
        """Datasets that carry facet test results for a module (the datasets it is tested in).

        One entry per dataset: ``db_id``, ``title``, ``facet_scope``, ``cohort_label``, ``cell_types`` (strata),
        ``groups``, ``variants`` (``method/source``), ``has_counts`` (the pseudobulk variant exists), ``counted``
        and ``modality``. Over every facet of the run; datasets without module results are absent.
        """
        key = module_key(catalog, module).lower()
        found: Dict[str, Dict[str, Any]] = {}
        for (facet, scope), (rows, units) in self.tables(datasets).items():
            hits = [name for name in rows['module_key'].cat.categories if name.lower() == key]
            if not hits:
                continue
            module_rows = rows[rows['module_key'].isin(hits)]
            joined = module_rows.merge(units, on='unit', how='left')
            for db_id, sub in joined.groupby('db_id', sort=False):
                first = sub.iloc[0]
                entry = found.setdefault(db_id, {
                    'db_id': db_id, 'title': first['title'], 'facet_scope': scope, 'cohort_label': first['cohort_label'],
                    'cell_types': set(), 'groups': set(), 'variants': set(), 'facets': set(), 'has_counts': False,
                    'counted': bool(first['counted']), 'modality': first['modality'], 'n_donors': {}})
                entry['cell_types'].update(sub['cell_type'].astype(str))
                entry['groups'].update(sub['celltype_group'].astype(str))
                entry['variants'].update(f'{m}/{s}' for m, s in zip(sub['method'].astype(str), sub['source'].astype(str)))
                entry['facets'].add(facet)
                entry['has_counts'] = entry['has_counts'] or bool((sub['method'].astype(str) == 'pseudobulk').any())
                entry['n_donors'][facet] = _as_int(sub['n_donors'].iloc[0])
        return json_safe([{**entry, 'cell_types': sorted(entry['cell_types']), 'groups': sorted(entry['groups']),
                           'variants': sorted(entry['variants']), 'facets': sorted(entry['facets'])}
                          for entry in sorted(found.values(), key=lambda e: (e['facet_scope'], e['db_id']))])

    # ── facet view: top modules ────────────────────────────────────────────

    def top_modules(self, facet: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets",
                    cell_type: Optional[str] = None, limit: int = 50, offset: int = 0,
                    exclude_secondary: bool = True, scope: Optional[str] = None, effect_gate: bool = False,
                    include_not_replicated: bool = False, catalog: Optional[str] = None) -> Dict[str, Any]:
        """Modules that replicate for one facet in one scope and cell type group (the module counterpart of
        ``FacetResults.recurring_genes``).

        Without ``cell_type`` the group with the most counted cohorts is used (unmapped never). Per module the view
        variant is chosen as in ``module_effects`` (pseudobulk where it exists, else the cell mean); the other
        variants are summarised in ``other_variants`` and ``ab_disagree`` flags a contradiction. Rows are ranked by
        ``r_value``, then ``k``, ``agreement`` and the absolute median standardised effect. ``limit`` has no upper
        bound; ``offset`` pages; ``n_matching`` counts the rows the filters keep.
        """
        facet = canonical_facet(facet)
        scope = scope or FacetResults.default_scope(facet)
        prepared = self.reader.prepare(datasets)
        families = self.families(facet, prepared, exclude_secondary, scope)
        groups: Dict[str, Dict[str, Any]] = {}
        for (group, method, source), family in families.items():
            options = groups.setdefault(group, {'cell_type': group, 'celltype_group': group,
                                                'cl_id': group_info(group)['cl_id'], 'labels': set(), 'n_cohorts': 0,
                                                'n_units': 0, 'variants': {}})
            group_units = family['units']
            options['labels'].update(group_units['cell_type'].astype(str))
            options['n_cohorts'] = max(options['n_cohorts'], int(group_units['cohort'].nunique()))
            options['n_units'] = max(options['n_units'], int(len(group_units)))
            options['variants'][(method, source)] = family
        for options in groups.values():
            combined = self._combined(options['variants'])
            usable = combined[combined['r_value'].notna()]
            replicated = usable[usable['replicated'].astype(bool)]
            options.update({'n_modules_in_family': int(len(usable)), 'n_replicated': int(len(replicated)),
                            'n_effect_gate': int(replicated['effect_gate_passed'].astype(bool).sum())})
        option_list = sorted(({**{k: v for k, v in options.items() if k != 'variants'},
                               'labels': sorted(options['labels'])} for options in groups.values()),
                             key=lambda o: (o['celltype_group'] == UNMAPPED, -o['n_cohorts'], -o['n_modules_in_family'],
                                            -o['n_units'], o['celltype_group']))
        chosen = None
        if cell_type:
            chosen = cell_type if cell_type in groups else group_of(cell_type)
        elif option_list:
            chosen = next((o['celltype_group'] for o in option_list if o['celltype_group'] != UNMAPPED), None)
        out_rows: List[Dict[str, Any]] = []
        n_matching = 0
        if chosen in groups:
            variants = groups[chosen]['variants']
            combined = self._combined(variants)
            usable = combined[combined['r_value'].notna()]
            kept = usable if include_not_replicated else usable[usable['replicated'].astype(bool)]
            if effect_gate:
                kept = kept[kept['effect_gate_passed'].astype(bool)]
            if catalog:
                kept = kept[kept['module_key'].str.lower().str.startswith(catalog.lower() + '/')]
            kept = kept.sort_values(['r_value', 'k', 'agreement', '_abs_effect'], ascending=[True, False, False, False])
            n_matching = int(len(kept))
            for _, record in kept.iloc[offset:offset + limit].iterrows():
                out_rows.append(self._top_row(record, variants))
        chosen_labels = next((o['labels'] for o in option_list if o['celltype_group'] == chosen), [])
        chosen_counts = next((o for o in option_list if o['celltype_group'] == chosen), {})
        return json_safe({
            'facet': facet, 'scope': scope, 'scopes': self.scopes_with_module_results(facet, prepared),
            'cell_type': chosen, 'celltype_group': chosen, 'labels': chosen_labels,
            'cohorts_in_cell_type': chosen_counts.get('n_cohorts', 0),
            'n_cohorts_tested': chosen_counts.get('n_cohorts', 0),
            'not_tested': not_tested_reason(facet, scope, chosen_counts.get('n_cohorts', 0),
                                            chosen_counts.get('n_modules_in_family', 0)),
            'n_modules_in_family': chosen_counts.get('n_modules_in_family', 0),
            'n_replicated': chosen_counts.get('n_replicated', 0), 'n_effect_gate': chosen_counts.get('n_effect_gate', 0),
            'offset': offset, 'n_matching': n_matching, 'n_returned': len(out_rows),
            'has_more': offset + len(out_rows) < n_matching, 'cell_types': option_list, 'rows': out_rows,
            'effect_gate': {'std_effect': facet_rules.module_gate(facet), 'wording': facet_rules.module_gate_words(facet)},
            'exclude_secondary': exclude_secondary})

    def scopes_with_module_results(self, facet: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets"
                                   ) -> List[str]:
        """Scopes (``facet_scopes.SCOPES`` order) with module results for ``facet``."""
        facet = canonical_facet(facet)
        import facet_scopes  # noqa: PLC0415
        present = {scope for (name, scope) in self.tables(datasets) if name == facet}
        return [scope for scope in facet_scopes.SCOPES if scope in present]

    @staticmethod
    def _combined(variants: Dict[Tuple[str, str], Dict[str, Any]]) -> pd.DataFrame:
        """One row per module with the replication of its preferred variant (``VARIANT_ORDER``) in a frame
        indexed by position; ``module_key``, ``method``, ``source`` and ``_abs_effect`` are columns."""
        parts = []
        for rank, variant in enumerate(VARIANT_ORDER):
            family = variants.get(variant)
            if family is None:
                continue
            frame = family['frame'].reset_index()
            frame['method'], frame['source'], frame['_rank'] = variant[0], variant[1], rank
            parts.append(frame[frame['r_value'].notna()])
        if not parts:
            return pd.DataFrame(columns=['module_key', 'r_value', 'replicated', 'effect_gate_passed', 'k', 'agreement',
                                         '_abs_effect', 'method', 'source'])
        combined = pd.concat(parts, ignore_index=True).sort_values('_rank', kind='stable')
        combined = combined.drop_duplicates('module_key', keep='first')
        combined['_abs_effect'] = combined['median_std_effect'].abs()
        return combined

    def _top_row(self, record: pd.Series, variants: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
        """One ranked row: the preferred variant's entry, the other variants in brief and the A/B flag."""
        key = record['module_key']
        entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for variant, family in variants.items():
            if key in family['frame'].index:
                entries[variant] = self._entry(family['frame'].loc[key], family['units'],
                                               family['tested_cohorts'].get(key, ()), gate_sd=family['gate'],
                                               facet=family['facet'])
        chosen = (record['method'], record['source'])
        main = entries[chosen]
        side_a = next((entries[v] for v in VARIANT_ORDER[1:] if v in entries), None)
        side_b = entries.get(VARIANT_ORDER[0])
        catalog, _, name = key.rpartition('/')
        return {
            'geneset_id': key, 'catalog': catalog, 'module': name, 'method': chosen[0], 'source': chosen[1],
            'variant_label': variant_label(*chosen), **main, **self._effect_range(variants.get(chosen), key),
            'other_variants': [{'method': v[0], 'source': v[1], 'label': variant_label(*v), 'call': e['call'],
                                'r_value': e['r_value'], 'median_std_effect': e['median_std_effect'],
                                'r_direction': e['r_direction'], 'cohorts_label': e['cohorts_label']}
                               for v, e in entries.items() if v != chosen],
            'ab': ab_disagreement(side_a, side_b)}

    @staticmethod
    def _effect_range(family: Optional[Dict[str, Any]], key: str) -> Dict[str, Optional[float]]:
        """``min_effect`` / ``max_effect``: the smallest and largest per-cohort standardised effect of a module."""
        effects = pd.Series(dtype=float)
        if family is not None:
            per_cohort = family['per_cohort']
            effects = per_cohort.loc[per_cohort['key'] == key, 'effect'].dropna()
        return {'min_effect': _rounded(effects.min(), 4) if len(effects) else None,
                'max_effect': _rounded(effects.max(), 4) if len(effects) else None}

    def warm(self, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets", exclude_secondary: bool = True) -> int:
        """Read the module tables and build every module family so the first request is a cache hit.
        Returns the number of (facet, scope) pairs built."""
        prepared = self.reader.prepare(datasets)
        keys = list(self.tables(prepared))
        for facet, scope in keys:
            self.families(facet, prepared, exclude_secondary, scope)
        return len(keys)


def quality_flags(genes: Sequence[str]) -> Dict[str, Any]:
    """Quality sensitivity of a module from its member gene names.

    A module whose members are mostly mitochondrial (``MT-``) or ribosomal genes tracks cell quality, not biology:
    ``mt_dominated`` / ``ribosomal_dominated`` when at least ``QUALITY_SHARE`` of the members are of that class, and
    ``quality_sensitive`` when either is. Shares are over the given symbols.
    """
    import gene_classes  # noqa: PLC0415
    total = len(genes)
    if not total:
        return {'mt_fraction': 0.0, 'ribosomal_fraction': 0.0, 'mt_dominated': False, 'ribosomal_dominated': False,
                'quality_sensitive': False}
    flagged = [gene_classes.flags(gene, annotation={}) for gene in genes]
    mt = sum('mitochondrial' in flags for flags in flagged) / total
    ribosomal = sum('ribosomal' in flags for flags in flagged) / total
    return {'mt_fraction': round(mt, 3), 'ribosomal_fraction': round(ribosomal, 3),
            'mt_dominated': mt >= QUALITY_SHARE, 'ribosomal_dominated': ribosomal >= QUALITY_SHARE,
            'quality_sensitive': mt >= QUALITY_SHARE or ribosomal >= QUALITY_SHARE}


DIRECTIONLESS_NOTE = ('A mean score is not suited to this set: its genes are a list of associations (for example '
                      'GWAS or MAGMA gene sets), not a program whose genes move together in one direction. Read the '
                      'member-gene counts instead.')


def directionless_reason(study: str, method: Optional[str], direction: Optional[str], software: Optional[str]
                         ) -> Optional[str]:
    """Why a gene set has no direction (a mean score of it is not meaningful), None when it has one.

    A set is directionless when its ``direction`` is ``bidirectional``, or it is an association list: a study whose
    name contains ``GWAS`` or a method ``other`` whose software names GWAS or MAGMA.
    """
    if str(direction or '').strip().lower() == 'bidirectional':
        return 'direction is bidirectional'
    if 'gwas' in str(study or '').lower():
        return 'GWAS gene list'
    if str(method or '').strip().lower() == 'other' and any(
            token in str(software or '').lower() for token in ('gwas', 'magma')):
        return 'association gene list'
    return None


def module_card_forest(card: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """A module card with every variant forest in the requested detail (``facet_results.forest_view``); a copy."""
    entries = [{**entry, 'variants': [{**variant, 'forest': forest_view(variant['forest'], mode)}
                                      for variant in entry['variants']]} for entry in card.get('replication') or []]
    return {**card, 'replication': entries}
