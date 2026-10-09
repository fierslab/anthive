"""Donor variability reader (``lib/donor_variability.py``), the four ant-serve routes, the MCP tool and the help topic.

The result files are small fixtures in the layout ``ant-donor-variability`` writes, next to the facet test fixtures of
``test_facet_results.py`` (two human cohorts, one xenograft). The routes are extracted from ant-serve with ``ast``.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import donor_variability as dvr  # noqa: E402
import facet_results as fr  # noqa: E402
import help_content  # noqa: E402
from scipy.stats import chi2  # noqa: E402
from test_facet_results import (  # noqa: E402,F401  (fixtures shared with the facet tests)
    DATASETS, MCP_PATH, SERVE_PATH, build_serve, results)

NULL_GENES = [f'NULL{i:02d}' for i in range(30)]
RUNG_TERMS = {'D0': ['technical', 'composition'], 'D1': ['technical', 'composition', 'sex', 'age', 'pmi'],
              'F': ['technical', 'composition', 'sex', 'age', 'pmi', 'latent']}
CELL_TYPE = 'microglial cell'


def unit_manifest(n_replicates: int, computed=('D0', 'D1', 'F'), deepest: str = 'D1', cell_type: str = CELL_TYPE,
                  source: str = 'constant_all_cells') -> Dict[str, Any]:
    rungs = []
    for name in ('D0', 'D1', 'D2', 'D3', 'D4', 'F'):
        if name in computed:
            rungs.append({'name': name, 'terms': RUNG_TERMS[name], 'n': n_replicates, 'p': 10, 'residual_df': n_replicates - 10,
                          'computed': True, 'reason': None})
        else:
            rungs.append({'name': name, 'terms': [], 'n': 0, 'p': 0, 'residual_df': 0, 'computed': False,
                          'reason': 'no new term'})
    return {'cell_type': cell_type, 'celltype_source': source, 'stratum': f'{source}::{cell_type}',
            'celltype_group': 'microglia', 'n_replicates': n_replicates, 'status': 'ok', 'rung_deepest': deepest,
            'rungs': rungs, 'terms': ['technical', 'composition', 'sex', 'age', 'pmi'], 'terms_omitted': {},
            'technical_covariates': ['log_n_cells'], 'composition_pcs': 3, 'n_factors': 3, 'lmg_rung': deepest,
            'median_cells_per_replicate': 120.0, 'n_genes_gate': 31, 'median_v_donor': 0.04, 'median_reliability': 0.6}


def feature_columns(n_replicates: int, v_donor: float, reliability: float, p_excess: float, excess_z: float,
                    share: float, lmg_sex: float) -> Dict[str, Any]:
    sd = float(np.sqrt(max(v_donor, 0.0)))
    row: Dict[str, Any] = {
        'n_replicates': n_replicates, 'rung_deepest': 'D1', 'ave_expr': 5.0, 'half_count_q70': 40.0, 'v_donor': v_donor,
        'v_donor_lo': v_donor * 0.6, 'v_donor_hi': v_donor * 1.5, 'sd_donor': sd, 'var_half': v_donor / max(reliability, 0.1),
        'r_half': reliability / (2 - reliability), 'reliability': reliability, 'reliability_lo': reliability - 0.1,
        'reliability_hi': min(reliability + 0.1, 1.0), 'share_design_lo': max(share - 0.2, 0.0),
        'share_design_hi': min(share + 0.2, 1.0), 'v_unexplained_lo': 0.0, 'v_unexplained_hi': v_donor,
        'share_technical': 0.1, 'share_covariates': share - 0.1, 'share_latent': 0.05, 'r_composition_pc1': 0.2,
        'lmg_technical': 0.1, 'lmg_composition': 0.0, 'lmg_sex': lmg_sex, 'lmg_age': 0.0, 'lmg_pmi': 0.0,
        'excess_z_raw': excess_z, 'p_excess_raw': p_excess}
    for rung, factor in (('D0', 0.9), ('D1', 1.0), ('F', 0.8)):
        row[f'v_unexplained_{rung}'] = v_donor * (1 - share) * factor
        row[f'share_design_{rung}'] = share if rung != 'D0' else 0.1
        row[f'excess_z_{rung}'] = excess_z
        row[f'p_excess_{rung}'] = p_excess
    return row


def gene_rows(db_id: str, stem: str, n_replicates: int, scope: str, unit: str,
              specs: Dict[str, Dict[str, float]]) -> pd.DataFrame:
    rows = []
    for gene, spec in sorted({**{g: {} for g in NULL_GENES}, **specs}.items()):
        values = {'v_donor': 0.03, 'reliability': 0.5, 'p_excess': 0.5, 'excess_z': 0.0, 'share': 0.1, 'lmg_sex': 0.0, **spec}
        rows.append({'db_id': db_id, 'dataset': stem, 'cell_type': CELL_TYPE, 'celltype_source': 'constant_all_cells',
                     'celltype_group': 'microglia', 'facet_scope': scope, 'replicate_unit': unit, 'gene': gene,
                     **feature_columns(n_replicates, **values)})
    return pd.DataFrame(rows)


def module_rows(db_id: str, stem: str, n_replicates: int, scope: str, unit: str, sd_by_module: Dict[str, float]) -> pd.DataFrame:
    rows = []
    for method, source in (('pseudobulk', 'gene_list'), ('cell_mean', 'ucell')):
        for module, sd in sd_by_module.items():
            rows.append({'db_id': db_id, 'dataset': stem, 'cell_type': CELL_TYPE, 'celltype_source': 'constant_all_cells',
                         'celltype_group': 'microglia', 'facet_scope': scope, 'replicate_unit': unit, 'method': method,
                         'source': source, 'catalog': 'Study/Exp', 'module': module, 'n_genes_found': 20,
                         **feature_columns(n_replicates, v_donor=sd ** 2, reliability=0.8, p_excess=np.nan,
                                           excess_z=np.nan, share=0.4, lmg_sex=0.0)})
    return pd.DataFrame(rows)


def write_unit(root: Path, group: str, stem: str, db_id: str, n_replicates: int, scope: str, unit: str,
               specs: Dict[str, Dict[str, float]], modules: Optional[Dict[str, float]] = None) -> None:
    folder = root / group
    folder.mkdir(exist_ok=True, parents=True)
    genes = gene_rows(db_id, stem, n_replicates, scope, unit, specs).sort_values(['gene'])
    genes.to_parquet(folder / f'{stem}.donor_variability.parquet', index=False, row_group_size=10)
    if modules:
        module_rows(db_id, stem, n_replicates, scope, unit, modules).sort_values(['module']).to_parquet(
            folder / f'{stem}.donor_variability_modules.parquet', index=False)
    manifest = {'tool': 'ant-donor-variability/1.0', 'db_id': db_id, 'status': 'done', 'facet_scope': scope,
                'replicate_unit': unit, 'replicate_column': 'ant_donor', 'seed': 1, 'versions': {'numpy': '2'},
                'md_sha256': 'abc', 'parameters': {'min_replicates': 12}, 'n_strata_analysed': 1,
                'units': [unit_manifest(n_replicates)]}
    (folder / f'{stem}.donor_variability_manifest.json').write_text(json.dumps(manifest))


HIGH = {'v_donor': 1.2, 'reliability': 0.97, 'p_excess': 0.001, 'excess_z': 4.5, 'share': 0.02}


@pytest.fixture
def variability(results):
    """Facet fixtures plus donor variability: HLA-DRB5 high in both human cohorts, GSTM1 in one, XIST sex driven."""
    write_unit(results, 'G1', 'ds1', 'G1/ds1', 40, 'human_postmortem', 'donor', {
        'HLA-DRB5': {**HIGH, 'p_excess': 0.001}, 'GSTM1': {**HIGH, 'p_excess': 0.002, 'v_donor': 0.9},
        'XIST': {'v_donor': 20.0, 'reliability': 0.999, 'share': 0.99, 'lmg_sex': 0.98, 'p_excess': 0.9, 'excess_z': -1.0}},
        modules={'modA': 0.5, 'modB': 0.2})
    write_unit(results, 'G2', 'ds2', 'G2/ds2', 35, 'human_postmortem', 'donor', {
        'HLA-DRB5': {**HIGH, 'p_excess': 0.002}, 'GSTM1': {'p_excess': 0.6}}, modules={'modA': 0.4})
    write_unit(results, 'M1', 'mus', 'M1/mus', 40, 'xenograft_human_in_mouse', 'animal', {
        'HLA-DRB5': {**HIGH, 'p_excess': 0.001}})
    return results


@pytest.fixture
def reader(variability):
    return dvr.DonorVariability(fr.FacetResults(variability))


DATASETS_ALL = DATASETS + [{'db_id': 'M1/mus', 'group_folder': 'M1', 'stem': 'mus', 'title': 'Mouse dataset'}]


# ---- reader ---------------------------------------------------------------------------------------------------------

def test_dataset_summary_states(reader, variability):
    summary = reader.dataset_summary('G1/ds1', 'G1', 'ds1')
    assert summary['status'] == 'analysed' and summary['replicate_unit'] == 'donor'
    unit = summary['units'][0]
    assert unit['n_genes'] == len(NULL_GENES) + 3 and unit['n_modules'] == 4 and unit['counted_in_cross_cohort']
    assert unit['rung_deepest'] == 'D1' and [r['name'] for r in unit['rungs']] == ['D0', 'D1', 'D2', 'D3', 'D4', 'F']
    assert unit['median_sd_donor'] == pytest.approx(0.2)
    assert reader.dataset_summary('G3/ds3', 'G3', 'ds3', skip_reason='no raw counts layer')['reason'] == 'no raw counts layer'
    assert reader.dataset_summary('G3/ds3', 'G3', 'ds3')['status'] == 'not_run'
    (variability / 'G3').mkdir()
    (variability / 'G3' / 'ds3.donor_variability_manifest.json').write_text(
        json.dumps({'status': 'skipped', 'reason': '9 replicates (< 12)', 'facet_scope': 'human_postmortem'}))
    skipped = reader.dataset_summary('G3/ds3', 'G3', 'ds3')
    assert skipped['status'] == 'not_analysed' and skipped['reason'] == '9 replicates (< 12)'
    assert not reader.available('G3', 'ds3') and reader.available('G1', 'ds1')


def test_summary_survives_nan_numbers_in_a_manifest(reader, variability):
    path = variability / 'G1' / 'ds1.donor_variability_manifest.json'
    manifest = json.loads(path.read_text())
    manifest['units'][0]['median_reliability'] = float('nan')
    manifest['units'][0]['median_v_donor'] = float('nan')
    path.write_text(json.dumps(manifest))                      # json writes NaN as a bare token
    unit = reader.dataset_summary('G1/ds1', 'G1', 'ds1')['units'][0]
    assert unit['median_reliability'] is None and unit['median_sd_donor'] is None
    json.dumps(reader.dataset_summary('G1/ds1', 'G1', 'ds1'), allow_nan=False)


def test_feature_record_rungs_and_derived_numbers(reader):
    rows = reader.dataset_rows('G1', 'ds1', 'gene', CELL_TYPE, feature='hla-drb5')
    record = rows['rows'][0]
    assert record['gene'] == 'HLA-DRB5' and rows['n_matching'] == 1
    assert record['reliability'] == pytest.approx(0.97) and record['sd_donor'] == pytest.approx(np.sqrt(1.2), abs=1e-3)
    assert record['fold_range_donor'] == pytest.approx(2 ** (2 * np.sqrt(1.2)), abs=1e-2)
    assert set(record['rungs']) == {'raw', 'D0', 'D1', 'D2', 'D3', 'D4', 'F'}
    assert record['rungs']['D2'] is None and record['rungs']['D1']['n'] == 40
    assert record['rungs']['D1']['sd'] == pytest.approx(np.sqrt(1.2 * 0.98), abs=1e-3)
    assert record['rungs']['raw']['p_excess'] == pytest.approx(0.001) and record['rungs']['raw']['share_design'] is None
    assert record['deepest_rung'] == 'D1' and record['share_design'] == pytest.approx(0.02)
    assert record['lmg']['sex'] == 0.0 and record['lmg_rung'] == 'D1'
    assert record['share_blocks']['technical'] == pytest.approx(0.1)


def test_dataset_rows_sort_filter_and_errors(reader):
    top = reader.dataset_rows('G1', 'ds1', 'gene', CELL_TYPE, sort='excess', rung='D1', limit=3)
    assert [r['gene'] for r in top['rows'][:2]] == sorted(['GSTM1', 'HLA-DRB5'])  # equal excess, stable order
    assert top['n_returned'] == 3 and top['n_matching'] == len(NULL_GENES) + 3 and top['unit']['n_replicates'] == 40
    by_sd = reader.dataset_rows('G1', 'ds1', 'gene', CELL_TYPE, sort='sd', rung='raw', limit=1)
    assert by_sd['rows'][0]['gene'] == 'XIST'
    reliable = reader.dataset_rows('G1', 'ds1', 'gene', CELL_TYPE, min_reliability=0.9, sort='feature')
    assert [r['gene'] for r in reliable['rows']] == ['GSTM1', 'HLA-DRB5', 'XIST']
    modules = reader.dataset_rows('G1', 'ds1', 'module', CELL_TYPE, method='pseudobulk', sort='sd', rung='D1')
    assert [r['module'] for r in modules['rows']] == ['Study/Exp/modA', 'Study/Exp/modB']
    one = reader.dataset_rows('G1', 'ds1', 'module', CELL_TYPE, feature='Study/Exp/modB', method='cell_mean')
    assert one['rows'][0]['source'] == 'ucell' and one['rows'][0]['n_genes_found'] == 20
    assert reader.dataset_rows('G1', 'ds1', 'gene', 'nonexistent')['unit'] is None
    assert reader.dataset_rows('G9', 'none', 'gene', CELL_TYPE)['n_matching'] == 0
    for kwargs in ({'rung': 'D9'}, {'sort': 'nope'}, {'feature_type': 'protein'}):
        with pytest.raises(dvr.DonorVariabilityError):
            reader.dataset_rows('G1', 'ds1', **{'feature_type': 'gene', 'cell_type': CELL_TYPE, **kwargs})


def test_units_anchors_scopes_and_reasons(reader, variability):
    human = reader.units(DATASETS_ALL, 'human_postmortem')
    assert {u['db_id'] for u in human} == {'G1/ds1', 'G2/ds2'} and all(u['counted'] for u in human)
    assert {u['cohort_label'] for u in human} == {'CohortA', 'CohortB'}
    assert 'D1' in human[0]['rung_reached'] and 'D2' not in human[0]['rung_reached'] and 'raw' in human[0]['rung_reached']
    xeno = reader.units(DATASETS_ALL, 'xenograft_human_in_mouse')
    assert [u['db_id'] for u in xeno] == ['M1/mus'] and xeno[0]['replicate_unit'] == 'animal'
    secondary = [{**d, 'is_secondary': d['db_id'] == 'G2/ds2'} for d in DATASETS_ALL]
    reasons = {u['db_id']: u['not_counted_reason'] for u in reader.units(secondary, 'human_postmortem')}
    assert reasons == {'G1/ds1': None, 'G2/ds2': 'secondary dataset'}
    assert {u['db_id'] for u in reader.units(secondary, 'human_postmortem', exclude_secondary=False) if u['counted']} \
        == {'G1/ds1', 'G2/ds2'}
    # a dataset outside the cohort table and a small cohort do not count
    write_unit(variability, 'G5', 'ds5', 'G5/ds5', 25, 'human_postmortem', 'donor', {})
    extra = DATASETS_ALL + [{'db_id': 'G5/ds5', 'group_folder': 'G5', 'stem': 'ds5', 'title': 'x'}]
    by_id = {u['db_id']: u for u in reader.units(extra, 'human_postmortem')}
    assert by_id['G5/ds5']['not_counted_reason'] == 'dataset is not a counted cohort'
    write_unit(variability, 'G2', 'ds2', 'G2/ds2', 28, 'human_postmortem', 'donor', {})
    assert 'fewer than 30' in {u['db_id']: u for u in reader.units(extra, 'human_postmortem')}['G2/ds2']['not_counted_reason']


def test_second_unit_of_a_cohort_is_not_the_anchor(variability):
    manifest_path = variability / 'G1' / 'ds1.donor_variability_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['units'].append(unit_manifest(40, cell_type='Mic other', source='ant_author_cell_type'))
    manifest['units'][-1]['celltype_group'] = 'microglia'
    manifest_path.write_text(json.dumps(manifest))
    units = dvr.DonorVariability(fr.FacetResults(variability)).units(DATASETS_ALL, 'human_postmortem')
    ds1 = [u for u in units if u['db_id'] == 'G1/ds1']
    assert [u['counted'] for u in ds1] == [True, False] and 'anchor' in ds1[1]['not_counted_reason']


def test_top_genes_partial_conjunction_and_modules(reader):
    top = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'D1', limit=5)
    assert top['m_cohorts'] == 2 and not top['not_enough_cohorts'] and top['cell_type_group'] == 'microglia'
    first = top['rows'][0]
    assert first['gene'] == 'HLA-DRB5' and first['k'] == 2 and first['m'] == 2
    expected_p = float(chi2.sf(-2 * (np.log(0.001) + np.log(0.002)), 4))               # Fisher over the two cohorts
    assert first['p_rank_product'] == pytest.approx(expected_p, rel=1e-3)
    assert first['r_value'] == pytest.approx(expected_p * 32, rel=1e-3) and first['r_value'] < 0.05
    gstm1 = next(r for r in reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'D1', limit=50)['rows']
                 if r['gene'] == 'GSTM1')
    assert gstm1['k'] == 1 and gstm1['m'] == 2 and gstm1['r_value'] > 0.05           # one cohort does not replicate
    assert top['cohorts'][0]['replicate_unit'] == 'donor' and top['high_tail'] == 0.05
    assert 'not a test' in top['note']
    by_label = reader.top(DATASETS_ALL, 'human_postmortem', CELL_TYPE, 'gene', 'D1', limit=1)
    assert by_label['cell_type_group'] == 'microglia'                                # a raw label resolves to its group
    raw = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'raw', limit=1)
    assert raw['rung'] == 'raw' and raw['rows'][0]['gene'] in ('HLA-DRB5', 'GSTM1')
    modules = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'module', 'D1', min_cohorts=1)
    assert modules['rows'][0]['module'] == 'Study/Exp/modA' and modules['rows'][0]['m'] == 2
    assert modules['rows'][0]['k'] == 2 and 'median_share_design' in modules['rows'][0] and 'p_rank_product' not in modules['rows'][0]
    unavailable = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'D3')
    assert unavailable['m_cohorts'] == 0 and unavailable['not_enough_cohorts'] and unavailable['rows'] == []
    xeno = reader.top(DATASETS_ALL, 'xenograft_human_in_mouse', 'microglia', 'gene', 'D1')
    assert xeno['m_cohorts'] == 1 and xeno['not_enough_cohorts'] and xeno['rows'] == []      # a single cohort has no conjunction
    assert reader.top(DATASETS_ALL, 'xenograft_human_in_mouse', 'microglia', 'gene', 'D1', min_cohorts=1)['not_enough_cohorts'] is False
    with pytest.raises(dvr.DonorVariabilityError):
        reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'Z')
    with pytest.raises(facet_scopes_error()):
        reader.top(DATASETS_ALL, 'moon', 'microglia')


def facet_scopes_error():
    import facet_scopes  # noqa: PLC0415
    return facet_scopes.ScopeError


def test_family_cache_follows_the_files(reader, variability):
    anchors = [u for u in reader.units(DATASETS_ALL, 'human_postmortem') if u['counted']]
    first = reader.family(anchors, 'gene', 'D1')
    assert reader.family(anchors, 'gene', 'D1') is first
    write_unit(variability, 'G2', 'ds2', 'G2/ds2', 35, 'human_postmortem', 'donor',
               {'HLA-DRB5': {**HIGH, 'p_excess': 0.5}})
    second = reader.family(anchors, 'gene', 'D1')
    assert second is not first
    assert float(second.set_index('gene').loc['HLA-DRB5', 'p_rank_product']) == pytest.approx(float(chi2.sf(-2 * (np.log(0.001) + np.log(0.5)), 4)))


def test_feature_card_units_by_group_and_modules(reader):
    card = reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'hla-drb5', rung='D1')
    assert card['feature'] == 'hla-drb5' and card['scope'] == 'human_postmortem'
    assert {u['db_id'] for u in card['units']} == {'G1/ds1', 'G2/ds2'} and all(u['counted'] for u in card['units'])
    assert card['by_group'][0]['m'] == 2 and card['by_group'][0]['k'] == 2
    assert card['by_group'][0]['r_value'] == pytest.approx(float(chi2.sf(-2 * (np.log(0.001) + np.log(0.002)), 4)) * 32, rel=1e-3)
    unit = next(u for u in card['units'] if u['db_id'] == 'G1/ds1')
    assert unit['values']['reliability'] == pytest.approx(0.97) and unit['cohort_label'] == 'CohortA'
    xist = reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'XIST')
    assert xist['units'][0]['values']['lmg']['sex'] == pytest.approx(0.98)
    assert reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'NOPE')['units'] == []
    assert reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'XIST', cell_type='astrocyte')['units'] == []
    module = reader.feature_card(DATASETS_ALL, 'human_postmortem', 'module', 'Study/Exp/modA')
    assert {u['db_id'] for u in module['units']} == {'G1/ds1', 'G2/ds2'} and {u['method'] for u in module['units']} == {'pseudobulk'}
    assert module['by_group'][0]['r_value'] is None
    cell_mean = reader.feature_card(DATASETS_ALL, 'human_postmortem', 'module', 'Study/Exp/modA', method='cell_mean', source='ucell')
    assert {u['source'] for u in cell_mean['units']} == {'ucell'}
    xeno = reader.feature_card(DATASETS_ALL, 'xenograft_human_in_mouse', 'gene', 'HLA-DRB5')
    assert [u['db_id'] for u in xeno['units']] == ['M1/mus'] and xeno['units'][0]['replicate_unit'] == 'animal'


def test_partial_files_are_not_available(reader, variability):
    (variability / 'G1' / 'ds1.donor_variability.parquet').write_bytes(b'PAR1 truncated')
    assert reader.dataset_rows('G1', 'ds1', 'gene', CELL_TYPE)['unit'] is None
    assert reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'XIST')['units'] == []


def test_rank_product_calls_reproducible_ranks_and_a_partial_conjunction_could_not():
    import replication  # noqa: PLC0415
    rng = np.random.default_rng(0)
    n_genes = 3000
    ranks = np.column_stack([rng.permutation(n_genes) + 1 for _ in range(3)]) / n_genes      # independent ranks: the null
    ranks[:15] = (np.arange(1, 16) / n_genes)[:, None]                                       # the same top genes in every cohort
    r_values = replication.benjamini_hochberg(dvr.rank_product_p(ranks))
    assert (r_values[:15] < 0.05).all() and (r_values[15:] < 0.05).sum() <= 2
    pc = replication.partial_conjunction_p(ranks, u=2, method='simes')
    assert replication.benjamini_hochberg(pc).min() > 0.5       # rank p values bounded below by 1 / N: BH can never call
    two = ranks.copy()
    two[:, 2] = np.nan
    assert np.isfinite(dvr.rank_product_p(two)).all()
    one = ranks.copy()
    one[:, 1:] = np.nan
    assert np.isnan(dvr.rank_product_p(one)).all()                # a single cohort has no rank concordance


def test_concordance_of_the_excess_between_cohorts(variability):
    rng = np.random.default_rng(3)
    shared = {f'G{i:03d}': {'excess_z': float(z)} for i, z in enumerate(rng.normal(size=80))}
    for group, stem, db_id, n, noise in (('G1', 'ds1', 'G1/ds1', 40, 0.3), ('G2', 'ds2', 'G2/ds2', 35, 0.3)):
        specs = {gene: {'excess_z': spec['excess_z'] + float(rng.normal(0, noise)), 'p_excess': 0.5} for gene, spec in shared.items()}
        write_unit(variability, group, stem, db_id, n, 'human_postmortem', 'donor', specs)
    reader = dvr.DonorVariability(fr.FacetResults(variability))
    top = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'D1')
    concordance = top['concordance']
    assert concordance['n_pairs'] == 1 and concordance['pairs'][0]['cohorts'] == ['CohortA', 'CohortB']
    assert concordance['mean_spearman'] > 0.8 and concordance['pairs'][0]['n_genes'] >= 80
    modules = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'module', 'D1', min_cohorts=1)
    assert modules['concordance'] is None


def test_validators_and_helpers():
    assert dvr.check_rung(None) == 'D1' and dvr.check_rung('F') == 'F' and dvr.check_feature_type(None) == 'gene'
    with pytest.raises(dvr.DonorVariabilityError):
        dvr.check_rung('D7')
    assert dvr.fold_range(0.5) == pytest.approx(2.0) and dvr.fold_range(None) is None and dvr.fold_range(float('nan')) is None
    assert set(dvr.RUNG_LABELS) == set(dvr.RUNGS) and all('donor-variability' in dvr.HELP_TOPICS for _ in [0])


# ---- routes ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def serve(variability):
    return build_serve(variability)


def test_routes_are_registered_before_the_greedy_database_routes():
    source = SERVE_PATH.read_text()
    assert source.index('"/databases/{db_id:path}/donor-variability"') < source.index('@app.get("/databases/{db_id:path}/genes"')
    for route in ('"/genes/{gene}/donor-variability"', '"/genesets/{study}/{experiment}/{name}/donor-variability"',
                  '"/donor-variability/top"'):
        assert f'@app.get({route}' in source


def test_dataset_route_summary_and_rows(serve):
    body = serve.get('/databases/G1/ds1/donor-variability').json()
    assert body['status'] == 'analysed' and body['units'][0]['n_genes'] == len(NULL_GENES) + 3
    assert set(body['help_topics']) == set(dvr.HELP_TOPICS) and 'rows' not in body
    body = serve.get('/databases/G1/ds1/donor-variability', params={
        'feature_type': 'gene', 'cell_type': CELL_TYPE, 'feature': 'XIST'}).json()
    assert body['n_returned'] == 1 and body['rows'][0]['lmg']['sex'] == pytest.approx(0.98) and body['unit']['n_replicates'] == 40
    assert serve.get('/databases/G3/ds3/donor-variability').json()['status'] == 'not_run'
    assert serve.get('/databases/NO/such/donor-variability').status_code == 404
    assert serve.get('/databases/G1/ds1/donor-variability', params={'feature_type': 'gene'}).status_code == 400
    assert serve.get('/databases/G1/ds1/donor-variability', params={
        'feature_type': 'gene', 'cell_type': 'nothing'}).status_code == 404
    for params in ({'rung': 'D9'}, {'sort': 'x'}, {'limit': 5000}, {'min_reliability': 2}):
        assert serve.get('/databases/G1/ds1/donor-variability', params={
            'feature_type': 'gene', 'cell_type': CELL_TYPE, **params}).status_code in (400, 422)


def test_gene_route(serve):
    body = serve.get('/genes/HLA-DRB5/donor-variability').json()
    assert body['scope'] == 'human_postmortem' and body['by_group'][0]['k'] == 2
    assert 'donor-variability' in body['help_topics']
    xeno = serve.get('/genes/HLA-DRB5/donor-variability', params={'scope': 'xenograft_human_in_mouse'}).json()
    assert [u['db_id'] for u in xeno['units']] == ['M1/mus']
    assert serve.get('/genes/NOPE/donor-variability').status_code == 404
    assert serve.get('/genes/XIST/donor-variability', params={'scope': 'moon'}).status_code == 400
    assert serve.get('/genes/XIST/donor-variability', params={'rung': 'Q'}).status_code == 400
    assert serve.get('/genes/XIST/donor-variability', params={'cell_type': 'microglia', 'rung': 'raw'}).status_code == 200


def test_geneset_and_top_routes(serve):
    body = serve.get('/genesets/Study/Exp/modA/donor-variability').json()
    assert {u['db_id'] for u in body['units']} == {'G1/ds1', 'G2/ds2'} and body['feature_type'] == 'module'
    assert serve.get('/genesets/Study/Exp/none/donor-variability').status_code == 404
    top = serve.get('/donor-variability/top', params={'cell_type': 'microglia', 'limit': 3}).json()
    assert top['rows'][0]['gene'] == 'HLA-DRB5' and top['m_cohorts'] == 2 and len(top['rows']) == 3
    default = serve.get('/donor-variability/top').json()
    assert default['cell_type_group'] == 'microglia' and default['scope'] == 'human_postmortem'
    modules = serve.get('/donor-variability/top', params={'feature_type': 'module', 'min_cohorts': 1}).json()
    assert modules['rows'][0]['module'] == 'Study/Exp/modA'
    assert serve.get('/donor-variability/top', params={'feature_type': 'x'}).status_code == 400
    assert serve.get('/donor-variability/top', params={'min_cohorts': 0}).status_code == 422


def test_help_topic_exists_and_is_in_the_style(tmp_path):
    topic = help_content.get_topic('donor-variability')
    assert topic is not None and topic.related
    text = topic.body_html + topic.summary
    assert '—' not in text and '–' not in text
    for needed in ('replicate', 'reliability', 'fold range', 'D0', 'rank product', 'xenograft'):
        assert needed.lower() in text.lower()
    for related in topic.related:
        assert help_content.get_topic(related) is not None


# ---- MCP ------------------------------------------------------------------------------------------------------------

def _mcp_tool(responses: Dict[str, Any]):
    """The ``donor_variability`` tool of ant-mcp, extracted with ast and run against canned API answers."""
    tree = ast.parse(MCP_PATH.read_text())
    nested = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('donor_variability', '_fmt_number'):
            node.decorator_list = []
            nested[node.name] = node
    module = ast.Module(body=[nested['_fmt_number'], nested['donor_variability']], type_ignores=[])
    calls: List[Any] = []

    async def fake_get(path: str, **params):
        calls.append((path, params))
        return responses[path.split('?')[0]] if path in responses else responses[next(iter(responses))]

    class Status(Exception):
        response = type('Response', (), {'status_code': 404, 'text': 'nope'})()

    namespace: Dict[str, Any] = {'Optional': Optional, '_get': fake_get,
                                 'httpx': type('httpx', (), {'HTTPStatusError': Status})}
    exec(compile(ast.fix_missing_locations(module), str(MCP_PATH), 'exec'), namespace)
    return namespace['donor_variability'], calls, Status


def test_mcp_tool_is_listed_and_formats_all_three_answers(reader):
    import asyncio  # noqa: PLC0415
    source = MCP_PATH.read_text()
    assert 'async def donor_variability' in source and '/donor-variability/top' in source
    assert 'donor_variability' in source[source.index('REPLICATE-LEVEL FACET EFFECTS'):source.index('get_facet_tests  gene or module')+3000]
    top = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'gene', 'D1', limit=3)
    card = reader.feature_card(DATASETS_ALL, 'human_postmortem', 'gene', 'HLA-DRB5')
    modules = reader.top(DATASETS_ALL, 'human_postmortem', 'microglia', 'module', 'D1', min_cohorts=1)
    tool, calls, status = _mcp_tool({'/donor-variability/top': top, '/genes/HLA-DRB5/donor-variability': card})
    text = asyncio.run(tool())
    assert 'EXPLORATORY' in text and 'HLA-DRB5 | in top 5% in 2/2 cohorts' in text and calls[-1][0] == '/donor-variability/top'
    text = asyncio.run(tool(gene='HLA-DRB5'))
    assert 'CohortA' in text and 'reliability 0.97' in text and calls[-1][0] == '/genes/HLA-DRB5/donor-variability'
    assert 'r 9.04e-04' in text and '2 counted cohorts at this rung' in text
    tool, calls, _ = _mcp_tool({'/donor-variability/top': modules})
    text = asyncio.run(tool(feature_type='module'))
    assert 'Study/Exp/modA' in text and 'reliable in 2/2 cohorts' in text and calls[-1][1]['feature_type'] == 'module'
    tool, calls, _ = _mcp_tool({'/genesets/Study/Exp/modA/donor-variability': reader.feature_card(
        DATASETS_ALL, 'human_postmortem', 'module', 'Study/Exp/modA')})
    assert 'CohortA' in asyncio.run(tool(geneset='Study/Exp/modA'))
    assert calls[-1][0] == '/genesets/Study/Exp/modA/donor-variability'
