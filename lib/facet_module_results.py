"""Cross-cohort replication of module (gene set) facet effects, for the gene set and facet views.

``bin/annot/ant-facet-test-modules`` tests every module per dataset and cell type stratum (OLS on donor-level
module scores, permutation p values). This module combines those results across cohorts exactly the way
``lib/facet_results.py`` does for genes:

- one anchor unit per cohort and cell type group (``FacetResults.counting_units``), per facet scope;
- the cohort p value of a direction is the two-sided permutation p halved when the estimate has that sign (1 minus
  the half otherwise);
- the m - 1 largest of the m cohort p values are combined by Simes (partial conjunction, u = 2), x 2 for the two
  directions, Benjamini-Hochberg over the modules of one family gives ``r_value``; ``replicated`` is r < 0.05;
- the effect filter is separate and descriptive: at least 2 cohorts with a signed standardised effect
  (``std_effect``) of at least ``facet_rules.MODULE_STD_EFFECT_GATE`` (0.2) in the replicated direction. There is
  no strict tier for modules (``strict`` stays null).

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
AB_EFFECT = facet_rules.MODULE_STD_EFFECT_GATE
#: Share of mitochondrial (or ribosomal) member genes from which a module is flagged quality-sensitive.
QUALITY_SHARE = 0.5

MODULE_ROW_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'method', 'source', 'catalog', 'module', 'estimate', 'se', 'ci_low',
    'ci_high', 'std_effect', 'p_param', 'q_param', 'p_perm', 'q_perm', 'n_genes_requested', 'n_genes_found',
    'no_counts', 'n_donors', 'n_donors_ref', 'n_donors_test', 'descriptive_only', 'small_sample', 'n_lt5',
    'facet_scope', 'contrast', 'level_ref', 'level_test')
MODULE_UNIT_COLUMNS = (
    'unit', 'db_id', 'title', 'cell_type', 'celltype_source', 'n_donors', 'n_donors_ref', 'n_donors_test',
    'level_ref', 'level_test', 'descriptive_only', 'cohort', 'cohort_label', 'role', 'counted', 'independence',
    'overlap_unknown', 'pooled', 'disease_basis', 'exclusion_missing', 'is_secondary', 'celltype_group',
    'facet_scope', 'modality', 'modality_note', 'has_counts', 'analysis_level', 'design_subset')
_FLOAT_COLUMNS = ('estimate', 'se', 'ci_low', 'ci_high', 'std_effect', 'p_param', 'q_param', 'p_perm', 'q_perm')


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
    """

    def __init__(self, reader: FacetResults):
        self.reader = reader
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
                                ('n_lt5', False)):
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
        out: Dict[str, Tuple[List[Dict[str, Any]], List[pd.DataFrame]]] = {}
        for (facet, cell_type, celltype_source), group in frame.groupby(
                ['facet', 'cell_type', 'celltype_source'], sort=False, observed=True):
            first = group.iloc[0]
            descriptive = bool(group['descriptive_only'].astype(bool).any())
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
                    sub = rows[rows['unit'].isin(group_units['unit'])]
                    for (method, source), part in sub.groupby(['method', 'source'], observed=True):
                        families[(group, str(method), str(source))] = self._family(part, group_units)
            return families

        return self.reader._cached_build(self._families, (facet, scope, bool(exclude_secondary)), prepared.signature,
                                         build, max_entries=FAMILY_CACHE_ENTRIES)

    @staticmethod
    def _family(sub: pd.DataFrame, group_units: pd.DataFrame) -> Dict[str, Any]:
        """Replicability table of one (group, method, source) family (see ``families``)."""
        sub = sub.merge(group_units[['unit', 'cohort', 'n_siblings']], on='unit', how='left')
        key_codes, key_names = pd.factorize(sub['module_key'].astype(str))
        sub = sub.assign(key=key_codes)
        p_two = sub['p_perm']
        half = p_two / 2.0
        estimate = sub['estimate']
        sub['p_up'] = np.where(p_two.isna(), np.nan, np.where(estimate >= 0, half, 1.0 - half))
        sub['p_down'] = np.where(p_two.isna(), np.nan, np.where(estimate <= 0, half, 1.0 - half))
        q_adjusted = (sub['q_perm'] * sub['n_siblings']).clip(upper=1.0)
        sub['up'] = (q_adjusted < Q_THRESHOLD) & (estimate > 0)
        sub['down'] = (q_adjusted < Q_THRESHOLD) & (estimate < 0)
        per_cohort = sub.groupby(['key', 'cohort'], observed=True).agg(
            p_up=('p_up', 'min'), p_down=('p_down', 'min'), n_siblings=('n_siblings', 'max'),
            up=('up', 'any'), down=('down', 'any'), effect=('std_effect', 'mean'), raw=('estimate', 'mean')
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
        sign = np.sign(per_cohort['effect'])
        agree = pd.Series(sign.to_numpy() == direction_sign.reindex(per_cohort['key']).to_numpy(), index=per_cohort.index)
        frame['agreement'] = (agree.groupby(per_cohort['key']).sum()
                              / per_cohort.groupby('key')['effect'].size()).reindex(frame.index)
        frame['median_std_effect'] = per_cohort.groupby('key')['effect'].median().reindex(frame.index)
        frame['median_estimate'] = per_cohort.groupby('key')['raw'].median().reindex(frame.index)
        effects = per_cohort.pivot(index='key', columns='cohort', values='effect').reindex(
            index=p_up.index, columns=p_up.columns)
        signed = effects.mul(direction_sign.reindex(p_up.index), axis=0)
        carries = signed >= facet_rules.MODULE_STD_EFFECT_GATE
        frame['n_cohorts_ge_gate'] = carries.sum(axis=1).astype(int)
        frame['effect_gate_passed'] = frame['n_cohorts_ge_gate'] >= replication.DEFAULT_U
        names = np.asarray(p_up.columns, dtype=object)
        order = np.argsort(-np.nan_to_num(signed.to_numpy(), nan=-np.inf), axis=1)
        carried = carries.to_numpy()
        frame['effect_gate_cohorts'] = [[names[i] for i in row_order if row_carries[i]]
                                        for row_order, row_carries in zip(order, carried)]
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
                'tested_cohorts': tested_by_key}

    # ── entries ────────────────────────────────────────────────────────────

    @staticmethod
    def _entry(record: pd.Series, units_of_group: pd.DataFrame, tested: Sequence[Any] = ()) -> Dict[str, Any]:
        """The replication entry of one module in one family from its ``frame`` row (JSON-safe values).

        ``tested`` are the cohorts that have a p value for the module (``family['tested_cohorts']``).
        """
        label_of = dict(zip(units_of_group['cohort'], units_of_group['cohort_label']))
        donors_of = dict(zip(units_of_group['cohort'], units_of_group['n_donors']))

        def labels(cohorts: Any) -> List[str]:
            return [str(label_of.get(c) or c) for c in (cohorts if isinstance(cohorts, list) else [])]

        r_value = _finite(record.get('r_value'))
        m = _as_int(record.get('m')) or 0
        k = _as_int(record.get('k'))
        small = sorted({str(label_of.get(c) or c) for c in tested if _is_small(donors_of.get(c))})
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
            'effect_gate_tau': facet_rules.MODULE_STD_EFFECT_GATE,
            'effect_gate_unit': f'{facet_rules.MODULE_STD_EFFECT_GATE:g} standard deviations of the donor-level score',
            'effect_gate_cohorts': labels(record.get('effect_gate_cohorts')),
            'n_cohorts_ge_gate': _as_int(record.get('n_cohorts_ge_gate')),
            'strict': None, 'r_strong': None, 'replicated_strong': None, 'r_treat': None, 'replicated_effect': None,
            'median_std_effect': _finite(record.get('median_std_effect')),
            'median_estimate': _finite(record.get('median_estimate')),
            'agreement': _finite(record.get('agreement')),
            'r_cohorts': labels(record.get('r_cohorts')), 'n_small': len(small), 'small_cohorts': small,
            'k_over_m': f'{k}/{m}' if k is not None and m else None, 'cohorts_label': label,
        }

    @staticmethod
    def _point(record: Dict[str, Any]) -> Dict[str, Any]:
        """One forest row of a module result (slim): estimate with its interval, standardised effect and p values."""
        return {
            'cohort_label': _clean_text(record.get('cohort_label')), 'db_id': record['db_id'],
            'title': record.get('title'), 'cell_type': record['cell_type'], 'celltype_source': record['celltype_source'],
            'design_subset': design_subset_of(record['celltype_source']), 'anchor_counted': bool(record.get('counted_effective')),
            'n_donors': _as_int(record.get('n_donors')), 'n_donors_ref': _as_int(record.get('n_donors_ref')),
            'n_donors_test': _as_int(record.get('n_donors_test')), 'small_sample': _is_small(record.get('n_donors')),
            'estimate': _rounded(record.get('estimate'), 5), 'ci_low': _rounded(record.get('ci_low'), 5),
            'ci_high': _rounded(record.get('ci_high'), 5), 'std_effect': _rounded(record.get('std_effect'), 4),
            'p_param': _finite(record.get('p_param')), 'q_param': _finite(record.get('q_param')),
            'p_perm': _finite(record.get('p_perm')), 'q_perm': _finite(record.get('q_perm')),
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
                            entry = self._entry(pd.Series({'m': 0}), counting[counting['celltype_group'] == group])
                        else:
                            entry = self._entry(record, family['units'], family['tested_cohorts'].get(key, ()))
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
            'effect_gate': {'std_effect': facet_rules.MODULE_STD_EFFECT_GATE},
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
                                               family['tested_cohorts'].get(key, ()))
        chosen = (record['method'], record['source'])
        main = entries[chosen]
        side_a = next((entries[v] for v in VARIANT_ORDER[1:] if v in entries), None)
        side_b = entries.get(VARIANT_ORDER[0])
        catalog, _, name = key.rpartition('/')
        return {
            'geneset_id': key, 'catalog': catalog, 'module': name, 'method': chosen[0], 'source': chosen[1],
            'variant_label': variant_label(*chosen), **main,
            'other_variants': [{'method': v[0], 'source': v[1], 'label': variant_label(*v), 'call': e['call'],
                                'r_value': e['r_value'], 'median_std_effect': e['median_std_effect'],
                                'r_direction': e['r_direction'], 'cohorts_label': e['cohorts_label']}
                               for v, e in entries.items() if v != chosen],
            'ab': ab_disagreement(side_a, side_b)}

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
