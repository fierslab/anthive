"""Statistical fixes of the module layer (2026-10-09): per-facet effect gate on the confidence bound, parametric p
from 30 residual df, directionless sets out of the families, agreement and small flags, per-region anchors."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

import facet_module_results as fm  # noqa: E402
import facet_results as fr  # noqa: E402
import facet_rules  # noqa: E402
from test_facet_test_modules import mod  # noqa: E402
from test_facet_views_api import VIEW_DATASETS, _module_frame, _replication, reader, results, views  # noqa: E402,F401


# ── effect gate per facet ────────────────────────────────────────────────────

@pytest.mark.parametrize('facet, expected', [
    ('sex', 0.2), ('disease', 0.2), ('apoe4_carrier', 0.2), ('braak_threshold', 0.2), ('app_nlgf', 0.2),
    ('braak', 0.2 / 3), ('age', 0.2), ('age_adjusted', 0.2), ('apoe4_dose', 0.1)])
def test_module_gate_per_facet(facet, expected):
    assert facet_rules.module_gate(facet) == pytest.approx(expected)
    assert '(lower 95% confidence bound)' in facet_rules.module_gate_words(facet)


def test_gate_words_name_the_unit():
    assert 'per Braak stage' in facet_rules.module_gate_words('braak')
    assert 'per decade' in facet_rules.module_gate_words('age')
    assert 'per APOE e4 allele' in facet_rules.module_gate_words('apoe4_dose')
    assert 'per' not in facet_rules.module_gate_words('sex').replace('per cent', '')


def test_standardised_bounds_use_score_sd_then_estimate_over_effect_then_the_point_estimate():
    frame = pd.DataFrame({'estimate': [0.3, 0.3, 0.3], 'std_effect': [0.6, 0.6, 0.6],
                          'ci_low': [0.2, 0.2, np.nan], 'ci_high': [0.4, 0.4, np.nan],
                          'score_sd': [0.25, np.nan, 0.5]})
    low, high = fm.standardised_bounds(frame)
    assert low == pytest.approx([0.8, 0.4, 0.6]) and high == pytest.approx([1.6, 0.8, 0.6])
    old = frame.drop(columns='score_sd')
    assert fm.standardised_bounds(old)[0] == pytest.approx([0.4, 0.4, 0.6])


def test_gate_reads_the_lower_confidence_bound_not_the_point_estimate(views):
    frame = _module_frame('G1/ds1', 'braak', donors=18)
    frame['ci_low'] = frame['estimate'] - 0.29                 # lower bound 0.01 / 0.5 = 0.02 SD < 0.2 / 3
    frame.to_parquet(views / 'G1' / 'ds1.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views))
    top = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')
    row = next(r for r in top['rows'] if r['module'] == 'modA')
    assert row['replicated'] is True and row['gate'] is False and row['n_cohorts_ge_gate'] == 1
    assert row['effect_gate_tau'] == pytest.approx(0.2 / 3)
    assert row['effect_gate_unit'] == facet_rules.module_gate_words('braak')
    assert top['effect_gate_rule']['std_effect'] == pytest.approx(0.2 / 3)


def test_gate_per_facet_changes_the_threshold(views):
    # the same module result is a hit per Braak stage (0.2/3) but not per contrast (0.2) at lower bound 0.1 SD
    for stem, group in (('ds1', 'G1'), ('ds2', 'G2')):
        frame = _module_frame(f'{group}/{stem}', 'braak', donors=18)
        frame['ci_low'], frame['ci_high'] = frame['estimate'] - 0.25, frame['estimate'] + 0.25   # bound 0.05 / 0.5 = 0.1 SD
        frame.to_parquet(views / group / f'{stem}.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views))
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['n_effect_gate'] == 2
    sex = _module_frame('G1/ds1', 'sex', donors=18).assign(contrast='m_vs_f', ci_low=lambda f: f['estimate'] - 0.25,
                                                       ci_high=lambda f: f['estimate'] + 0.25)
    sex.to_parquet(views / 'G1' / 'ds1.facet_module_tests.parquet')
    sex2 = _module_frame('G2/ds2', 'sex', donors=18).assign(contrast='m_vs_f', ci_low=lambda f: f['estimate'] - 0.25,
                                                       ci_high=lambda f: f['estimate'] + 0.25)
    sex2.to_parquet(views / 'G2' / 'ds2.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views))
    top = modules.top_modules('sex', VIEW_DATASETS, cell_type='microglia')
    assert top['n_replicated'] == 2 and top['n_effect_gate'] == 0


# ── parametric or permutation p ──────────────────────────────────────────────

def test_select_p_uses_parametric_from_30_residual_df():
    frame = pd.DataFrame({'df': [29.0, 30.0, np.nan, 100.0], 'p_param': [0.01, 0.02, 0.03, np.nan],
                          'p_perm': [0.5, 0.6, 0.7, 0.8], 'q_param': [0.1, 0.2, 0.3, np.nan],
                          'q_perm': [0.9, 0.8, 0.7, 0.6]})
    p_used, q_used, parametric = fm.select_p(frame)
    assert parametric.tolist() == [False, True, False, True]
    assert p_used[:3].tolist() == [0.5, 0.02, 0.7] and np.isnan(p_used[3])
    assert q_used[:3].tolist() == [0.9, 0.2, 0.7]
    assert fm.select_p(frame.drop(columns='df'))[2].tolist() == [False] * 4


def test_p_methods_disagree_needs_a_tenfold_gap_and_a_small_p():
    param = np.array([0.001, 0.2, 1e-9, 0.04, np.nan, 0.3])
    perm = np.array([0.04, 0.9, 1e-4, 0.03, 0.5, 0.9])
    assert fm.p_methods_disagree(param, perm).tolist() == [True, False, False, False, False, False]


def _p_frames(views: Path, df: float, p_param: float, p_perm: float) -> fm.ModuleResults:
    for group, stem in (('G1', 'ds1'), ('G2', 'ds2')):
        frame = _module_frame(f'{group}/{stem}', 'braak', donors=40)
        frame['df'] = df
        frame['p_param'], frame['q_param'] = p_param, p_param
        frame['p_perm'], frame['q_perm'] = p_perm, p_perm
        frame.to_parquet(views / group / f'{stem}.facet_module_tests.parquet')
    return fm.ModuleResults(fr.FacetResults(views))


def test_replication_follows_the_parametric_p_with_enough_df_else_the_permutation_p(views):
    large = _p_frames(views, df=40.0, p_param=0.0001, p_perm=0.9)
    assert {r['module'] for r in large.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows']} >= {'modA'}
    small = _p_frames(views, df=20.0, p_param=0.0001, p_perm=0.9)
    assert small.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows'] == []


def test_both_p_values_stay_served_with_the_basis_and_the_disagreement(views):
    modules = _p_frames(views, df=40.0, p_param=0.0001, p_perm=0.04)
    card = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak')
    variant = _replication(card)['variants'][0]
    point = variant['forest'][0]['anchor']
    assert point['p_basis'] == 'parametric' and point['df_resid'] == 40.0
    assert point['p_param'] == pytest.approx(0.0001) and point['p_perm'] == pytest.approx(0.04)
    assert point['p_used'] == pytest.approx(0.0001)
    assert point['p_disagree'] is True and point['p_check_flag'] is True
    assert point['p_check_fraction'] == pytest.approx(1.0)
    assert variant['p_check_flag'] is True and variant['p_check_cohorts']
    small = _p_frames(views, df=12.0, p_param=0.0001, p_perm=0.0001)
    quiet = _replication(small.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak'))['variants'][0]
    assert quiet['forest'][0]['anchor']['p_basis'] == 'permutation' and quiet['p_check_flag'] is False


# ── directionless sets ───────────────────────────────────────────────────────

def test_default_directionless_is_a_gwas_study():
    assert fm.default_directionless('GWAS0000/GWAS/AD_Bellenguez') is True
    assert fm.default_directionless('Study/Exp/modA') is False


def _with_gwas(views: Path) -> None:
    for group, stem in (('G1', 'ds1'), ('G2', 'ds2')):
        frame = _module_frame(f'{group}/{stem}', 'braak', donors=18)
        gwas = frame[frame['module'] == 'modA'].assign(catalog='GWAS0000/GWAS', module='AD')
        pd.concat([frame, gwas]).to_parquet(views / group / f'{stem}.facet_module_tests.parquet')


def test_directionless_sets_leave_the_families_and_the_top_list(views):
    _with_gwas(views)
    modules = fm.ModuleResults(fr.FacetResults(views))
    top = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True)
    assert 'AD' not in {row['module'] for row in top['rows']} and top['n_modules_in_family'] == 3
    card = modules.module_effects('GWAS0000/GWAS', 'AD', VIEW_DATASETS, 'braak')
    assert card['directionless'] is True and card['n_units'] > 0
    variant = _replication(card)['variants'][0]
    assert variant['call'] == 'not_tested' and variant['directionless'] is True
    assert variant['nt_label'] == 'no expected direction' and variant['forest']


def test_directionless_predicate_and_file_column(views):
    _module_frame('G1/ds1', 'braak', donors=18).assign(directionless=lambda f: f['module'] == 'modC').to_parquet(
        views / 'G1' / 'ds1.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views), directionless=lambda key: key.endswith('modB'))
    top = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True)
    assert {row['module'] for row in top['rows']} == {'modA'}
    assert modules.module_effects('Study/Exp', 'modB', VIEW_DATASETS, 'braak')['directionless'] is True


def test_tool_leaves_directionless_modules_out_of_the_bh_family():
    rng = np.random.default_rng(1)
    donors = 20
    design = pd.DataFrame({'facet_value': np.repeat([0.0, 1.0], donors // 2)})
    columns = pd.MultiIndex.from_tuples([('geneset', 'Cat', f'm{i}') for i in range(4)])
    scores = pd.DataFrame(rng.normal(size=(donors, 4)), columns=columns)
    scores[('geneset', 'Cat', 'm0')] += design['facet_value'] * 3
    plain = mod.fit_modules(scores, design, False, False, ('k',), n_permutations=200)
    flagged = mod.fit_modules(scores, design, False, False, ('k',), n_permutations=200,
                              directionless=lambda key: key[2] == 'm3')
    assert not plain['directionless'].any()
    assert flagged['directionless'].tolist() == [False, False, False, True]
    assert np.isnan(flagged.loc[('geneset', 'Cat', 'm3'), 'q_param'])
    assert flagged.loc[('geneset', 'Cat', 'm0'), 'q_param'] <= plain.loc[('geneset', 'Cat', 'm0'), 'q_param']
    assert flagged['p_param'].equals(plain['p_param'])


def test_tool_directionless_from_the_registry_name():
    class Experiment:
        study, method, metadata = 'GWAS0000', 'other', {}

    class Geneset:
        experiment, metadata = Experiment(), {}

    class Registry:
        genesets = {'GWAS0000/GWAS/AD': Geneset()}

    assert mod.module_is_directionless(Registry(), 'GWAS0000/GWAS', 'AD') is True
    assert mod.module_is_directionless(Registry(), 'Study/Exp', 'x') is False


# ── agreement, n_cohorts_tested, n_ref / n_test, small ───────────────────────

@pytest.mark.parametrize('donors, n_ref, n_test, expected', [
    (30, 12, 18, False), (30, 9, 21, True), (30, 10, 20, False), (12, 12, 12, False),
    (30, None, None, False), (19, None, None, True), (19, np.nan, np.nan, True), (200, 3, 197, True)])
def test_unit_is_small_uses_the_smaller_level_of_a_two_group_facet(donors, n_ref, n_test, expected):
    assert fm.unit_is_small(donors, n_ref, n_test) is expected


def test_entries_serve_agreement_cohorts_tested_and_level_counts(views):
    frames = []
    for group, stem, test_count in (('G1', 'ds1', 6), ('G2', 'ds2', 25)):
        frame = _module_frame(f'{group}/{stem}', 'sex', donors=40).assign(
            contrast='m_vs_f', level_ref='f', level_test='m', n_donors_ref=30.0, n_donors_test=float(test_count))
        frame.to_parquet(views / group / f'{stem}.facet_module_tests.parquet')
        frames.append(frame)
    modules = fm.ModuleResults(fr.FacetResults(views))
    top = modules.top_modules('sex', VIEW_DATASETS, cell_type='microglia')
    row = next(r for r in top['rows'] if r['module'] == 'modA')
    assert row['agreement'] == pytest.approx(1.0) and row['n_cohorts_tested'] == 2
    assert row['n_small'] == 1 and row['small_cohorts'] == ['CohortA']
    assert row['cohorts_label'] == '2/2 cohorts, 1 small'
    point = _replication(modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'sex'))['variants'][0]['forest']
    by_cohort = {entry['cohort_label']: entry for entry in point}
    assert by_cohort['CohortA']['small'] is True and by_cohort['CohortA']['anchor']['n_test'] == 6
    assert by_cohort['CohortA']['anchor']['n_ref'] == 30
    assert by_cohort['CohortB']['small'] is False


def test_agreement_counts_the_tested_cohorts_that_point_the_replicated_way(views):
    frame = _module_frame('G2/ds2', 'braak', donors=30)
    frame.loc[frame['module'] == 'modA', ['estimate', 'std_effect']] = [-0.3, -0.6]
    frame.loc[(frame['module'] == 'modA'), 'p_perm'] = 0.9
    frame.to_parquet(views / 'G2' / 'ds2.facet_module_tests.parquet')
    modules = fm.ModuleResults(fr.FacetResults(views))
    top = modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia', include_not_replicated=True)
    row = next(r for r in top['rows'] if r['module'] == 'modA')
    assert row['n_cohorts_tested'] == 2 and row['agreement'] == pytest.approx(0.5)


def test_agreement_is_none_without_a_direction(views):
    only = _module_frame('G1/ds1', 'braak', donors=18)
    only.to_parquet(views / 'G1' / 'ds1.facet_module_tests.parquet')
    (views / 'G2' / 'ds2.facet_module_tests.parquet').unlink()
    modules = fm.ModuleResults(fr.FacetResults(views))
    card = modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak')
    variant = _replication(card)['variants'][0]
    assert variant['call'] == 'not_tested' and variant['n_cohorts_tested'] == 1


# ── pooled cohorts: module results per region only ───────────────────────────

def test_cohorts_pooled_for_genes_are_reported_as_per_region_for_modules(views):
    table = views / fr.COHORT_TABLE_NAME
    frame = pd.read_csv(table, sep='\t', dtype=str).fillna('')
    frame['pool_members'] = ''
    pool = {'status': 'DRAFT', 'cohort': 'CohortB', 'cell_class': 'microglia', 'db_id': 'pool:cohortb',
            'role': 'anchor (donor-pooled over the member files)', 'pool_members': 'G2/ds2'}
    pd.concat([frame, pd.DataFrame([pool])], ignore_index=True).fillna('').to_csv(table, sep='\t', index=False)
    cohorts = fr.FacetResults(views).cohorts()
    assert cohorts['pool:cohortb']['pool_members'] == ['G2/ds2']
    modules = fm.ModuleResults(fr.FacetResults(views))
    variant = _replication(modules.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak'))['variants'][0]
    assert variant['per_region_only_cohorts'] == ['CohortB']
    assert 'regional file' in variant['module_scope_note'] and 'CohortB' in variant['module_scope_note']
    assert variant['forest'][1]['anchor']['per_region_only'] is True
    assert modules.top_modules('braak', VIEW_DATASETS, cell_type='microglia')['rows'][0]['per_region_only_cohorts'] == ['CohortB']


def test_cohorts_without_a_pool_have_no_per_region_note(modules_unpooled):
    variant = _replication(modules_unpooled.module_effects('Study/Exp', 'modA', VIEW_DATASETS, 'braak'))['variants'][0]
    assert variant['per_region_only_cohorts'] == [] and variant['module_scope_note'] is None


@pytest.fixture
def modules_unpooled(reader) -> fm.ModuleResults:
    return fm.ModuleResults(reader)


# ── the tool's summarize replication ─────────────────────────────────────────

def _summary_table(df: float, p_param: float, p_perm: float, facet: str = 'braak') -> pd.DataFrame:
    rows = []
    for db_id in ('A/a', 'B/b', 'C/c'):
        for module, catalog in (('UP', 'Cat'), ('GW', 'GWAS0000/GWAS')) + tuple((f'N{i}', 'Cat') for i in range(20)):
            signal = module in ('UP', 'GW')
            rows.append({
                'facet_scope': 'human_postmortem', 'facet': facet, 'method': 'cell_mean', 'source': 'geneset',
                'db_id': db_id, 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells', 'n_donors': 40,
                'catalog': catalog, 'module': module, 'estimate': 0.5 if signal else 0.0, 'std_effect': 1.0 if signal else 0.0,
                'ci_low': 0.3 if signal else -0.2, 'ci_high': 0.7 if signal else 0.2, 'score_sd': 0.5, 'df': df,
                'p_param': p_param if signal else 0.7, 'p_perm': p_perm if signal else 0.7,
                'q_param': p_param if signal else 0.7, 'q_perm': p_perm if signal else 0.7, 'descriptive_only': False})
    return pd.DataFrame(rows)


def _cohort_files(tmp_path: Path) -> tuple:
    root = tmp_path / '_facet_tests'
    root.mkdir()
    lines = ['status\tcohort\tcell_class\trole\tdb_id'] + [
        f'DRAFT\tCohort{i}\tmicroglia\tanchor\t{db_id}' for i, db_id in enumerate(('A/a', 'B/b', 'C/c'))]
    path = root / 'cohort_selection.DRAFT.tsv'
    path.write_text('\n'.join(lines) + '\n')
    return root, path


@pytest.mark.parametrize('df, p_param, p_perm, replicated', [(40.0, 1e-5, 0.9, True), (20.0, 1e-5, 0.9, False),
                                                              (20.0, 0.9, 1e-5, True)])
def test_tool_replication_uses_the_parametric_p_from_30_df(tmp_path, df, p_param, p_perm, replicated):
    root, cohort_table = _cohort_files(tmp_path)
    result = mod.replicated_modules(_summary_table(df, p_param, p_perm), root, cohort_table)
    up = result[result['module'] == 'UP'].iloc[0]
    assert bool(up['replicated']) is replicated
    assert 'GW' not in set(result['module'])                       # directionless: out of the family


def test_tool_gate_is_per_facet_and_on_the_confidence_bound(tmp_path):
    root, cohort_table = _cohort_files(tmp_path)
    table = _summary_table(40.0, 1e-5, 1e-5)
    braak = mod.replicated_modules(table, root, cohort_table)
    assert bool(braak[braak['module'] == 'UP'].iloc[0]['effect_gate_passed'])
    weak = table.assign(ci_low=lambda f: f['estimate'] - 0.49)     # bound 0.01 / 0.5 = 0.02 SD
    assert not bool(mod.replicated_modules(weak, root, cohort_table).query("module == 'UP'").iloc[0]['effect_gate_passed'])
    # 0.1 SD bound: passes per Braak stage (0.2/3), fails per contrast (0.2)
    middle = table.assign(ci_low=lambda f: f['estimate'] - 0.45)
    assert bool(mod.replicated_modules(middle, root, cohort_table).query("module == 'UP'").iloc[0]['effect_gate_passed'])
    sex = middle.assign(facet='sex')
    assert not bool(mod.replicated_modules(sex, root, cohort_table).query("module == 'UP'").iloc[0]['effect_gate_passed'])
    assert bool(mod.replicated_modules(sex, root, cohort_table, min_std_effect=0.05).query(
        "module == 'UP'").iloc[0]['effect_gate_passed'])
