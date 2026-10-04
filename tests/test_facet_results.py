"""Donor-level facet test results: lib/facet_results.py, the ant-serve
endpoints over it, the MCP tools and the config helpers.

The results are small fixture files written in the layout ant-facet-test and
ant-facet-test-modules use (``<results>/<group>/<stem>.facet_tests.parquet``
and friends). The ant-serve endpoints are extracted with ``ast`` (the server
is a single-file uv script with heavy imports) and run on a FastAPI app with
a fake registry.
"""
from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytest

import config
import facet_results as fr

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'
MCP_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-mcp'

GENES = ['XIST', 'RPS4Y1', 'DDX3Y', 'UTY', 'PTPRG', 'APOE', 'GENE7', 'GENE8']


# ── fixtures ─────────────────────────────────────────────────────────────────

def _gene_rows(db_id: str, facet: str, cell_type: str, effects: Dict[str, Tuple[float, Optional[float]]],
               descriptive: bool = False, levels=('f', 'm'), donors=(10, 8)) -> List[Dict[str, Any]]:
    rows = []
    for gene in GENES:
        logfc, q_value = effects.get(gene, (0.01, 0.9))
        rows.append({
            'db_id': db_id, 'dataset': db_id.split('/')[1], 'gene_id': GENES.index(gene),
            'logFC': logfc, 'se': 0.1,
            'ci_low': None if descriptive else logfc - 0.2, 'ci_high': None if descriptive else logfc + 0.2,
            't': None if descriptive else 2.0, 'p_value': None if descriptive else (q_value or 0.5) / 2,
            'q_value': None if descriptive else q_value, 'ave_expr': 4.0, 'df_total': 20.0,
            'n_donors_ref': None if facet == 'braak' else float(donors[0]),
            'n_donors_test': None if facet == 'braak' else float(donors[1]),
            'contrast': {'sex': 'm_vs_f', 'braak': 'braak_per_unit', 'disease': 'AD_vs_control'}[facet],
            'level_ref': None if facet == 'braak' else levels[0],
            'level_test': None if facet == 'braak' else levels[1],
            'n_donors': sum(donors), 'facet': facet, 'cell_type': cell_type,
            'celltype_source': 'constant_all_cells', 'n_lt5': descriptive, 'descriptive_only': descriptive,
            'batch_used': False, 'batch_confounded': False, 'covariates': 'age,pmi' if facet == 'sex' else 'sex,age,pmi',
            'composition_p': 0.01 if facet == 'braak' else 1.0, 'composition_flag': facet == 'braak',
            'provisional': facet == 'disease', 'gene': gene, 'multi_tissue_donors': 0,
        })
    return rows


def _module_rows(db_id: str, facet: str, cell_type: str) -> List[Dict[str, Any]]:
    rows = []
    for method, source in (('cell_mean', 'geneset'), ('pseudobulk', 'gene_list')):
        for index, module in enumerate(('modA', 'modB', 'modC')):
            q_param = 0.01 if module == 'modA' else 0.5
            q_perm = 0.2 if module == 'modA' else (0.03 if module == 'modB' else 0.6)
            rows.append({
                'db_id': db_id, 'dataset': db_id.split('/')[1], 'source': source, 'catalog': 'Study/Exp',
                'module': module, 'estimate': 0.1 * (index + 1), 'se': 0.02, 't': 3.0, 'df': 15.0,
                'p_param': q_param / 3, 'ci_low': 0.0, 'ci_high': 0.3, 'residual_sd': 0.1, 'score_sd': 0.2,
                'std_effect': 0.5 * (index + 1), 'p_perm': q_perm / 3, 'n_perm_effective': 1000,
                'q_param': q_param, 'q_perm': q_perm, 'min_score_coverage': 1.0, 'method': method,
                'facet': facet, 'cell_type': cell_type, 'celltype_source': 'constant_all_cells',
                'contrast': 'm_vs_f', 'level_ref': 'f', 'level_test': 'm', 'n_donors_ref': 10.0,
                'n_donors_test': 8.0, 'n_donors': 18, 'n_lt5': False, 'descriptive_only': False,
                'batch_used': False, 'batch_confounded': False, 'covariates': 'age,pmi',
                'composition_p': 1.0, 'composition_flag': False, 'provisional': False,
                'n_control_genes': None, 'n_genes_requested': 20, 'n_genes_found': 18,
                'no_counts': False, 'multi_tissue_donors': 0,
            })
    return rows


def _gene_manifest(db_id: str, group: str, stem: str, units: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        'tool': 'ant-facet-test/1.0', 'db_id': db_id, 'group': group, 'file': stem,
        'dataset_sha256': 'abc123', 'timestamp_utc': '2026-10-04T11:15:47+00:00', 'seed': 20261004,
        'anthive4_git': 'deadbeef', 'h5adstore_git': 'cafe',
        'parameters': {'min_cells_per_sample': 10, 'min_donors_dataset': 6, 'min_donors_per_level': 3,
                       'n_lt5_threshold': 5, 'numeric_min_donors_for_p': 10, 'min_donors_for_age_pmi': 15,
                       'min_covariate_coverage': 0.9,
                       'normalisation': 'edgeR calcNormFactors TMM + limma voom',
                       'gene_filter': 'edgeR filterByExpr(design) defaults',
                       'eBayes': 'limma eBayes defaults (trend=FALSE, robust=FALSE)'},
        'r_versions': {'R': 'R 4.4.3', 'limma': '3.62.1', 'edgeR': '4.4.0'},
        'counts_layer': 'UMIs', 'batch_column': None, 'n_cells': 1000, 'n_donors': 18,
        'units': units, 'status': 'done',
    }


def _unit(facet: str, cell_type: str, status: str = 'ok', **extra) -> Dict[str, Any]:
    record = {'facet': facet, 'cell_type': cell_type, 'celltype_source': 'constant_all_cells',
              'status': status, 'level_counts': {'f': 10, 'm': 8}, 'n_lt5': False, 'batch_used': False,
              'batch_confounded': False, 'covariates': ['age', 'pmi'], 'covariates_omitted': [],
              'n_donors_analysed': 18, 'n_donors_dropped_covariate': 0, 'composition_p': 1.0,
              'composition_flag': False, 'n_cells_in_stratum': 900, 'n_genes_tested': len(GENES),
              'n_sig_q05': 3}
    record.update(extra)
    return record


@pytest.fixture
def results(tmp_path: Path) -> Path:
    """Two tested datasets in two cohorts, one skipped by the run log, one
    without anything, plus the cohort table."""
    root = tmp_path / '_facet_tests'
    group = root / 'G1'
    group.mkdir(parents=True)
    rows = (_gene_rows('G1/ds1', 'sex', 'microglial cell',
                       {'XIST': (-9.0, 1e-20), 'RPS4Y1': (7.0, 1e-15), 'DDX3Y': (5.0, 1e-6), 'UTY': (4.0, 1e-4)})
            + _gene_rows('G1/ds1', 'braak', 'microglial cell', {'PTPRG': (0.5, 0.001), 'APOE': (-0.3, 0.02)},
                         donors=(18, 0)))
    pd.DataFrame(rows).to_parquet(group / 'ds1.facet_tests.parquet')
    pd.DataFrame(_module_rows('G1/ds1', 'sex', 'microglial cell')).to_parquet(group / 'ds1.facet_module_tests.parquet')
    braak_unit = _unit('braak', 'microglial cell', covariates=['sex', 'age', 'pmi'],
                       level_counts={'n_donors': 18, 'braak_min': 0, 'braak_max': 6, 'n_distinct': 6},
                       composition_p=0.01, composition_flag=True)
    (group / 'ds1.facet_run_manifest.json').write_text(json.dumps(_gene_manifest('G1/ds1', 'G1', 'ds1', [
        _unit('sex', 'microglial cell'),
        braak_unit,
        _unit('disease', 'microglial cell', status='level with < 3 donors (control=2, AD=16)'),
    ])))
    module_manifest = _gene_manifest('G1/ds1', 'G1', 'ds1', [
        _unit('sex', 'microglial cell', n_modules_cell_mean_geneset=3, n_sig_q05_param_cell_mean_geneset=1,
              n_sig_q05_perm_cell_mean_geneset=1)])
    module_manifest.update(tool='ant-facet-test-modules/1.0', versions={'python': '3.11'}, sources=['geneset'],
                           no_counts=False, parameters={**module_manifest['parameters'], 'n_permutations': 1000,
                                                        'permutation': 'labels shuffled',
                                                        'bh_family': '(dataset, stratum, facet, method)'})
    (group / 'ds1.facet_module_run_manifest.json').write_text(json.dumps(module_manifest))
    # second dataset, other cohort: braak, PTPRG up again, descriptive sex unit
    group2 = root / 'G2'
    group2.mkdir()
    rows2 = (_gene_rows('G2/ds2', 'braak', 'microglial cell', {'PTPRG': (0.4, 0.01), 'APOE': (0.2, 0.03)},
                        donors=(30, 0))
             + _gene_rows('G2/ds2', 'sex', 'microglial cell', {}, descriptive=True, donors=(3, 9)))
    pd.DataFrame(rows2).to_parquet(group2 / 'ds2.facet_tests.parquet')
    (group2 / 'ds2.facet_run_manifest.json').write_text(json.dumps(_gene_manifest('G2/ds2', 'G2', 'ds2', [
        _unit('braak', 'microglial cell', covariates=['sex'], n_donors_analysed=30,
              level_counts={'n_donors': 30, 'braak_min': 1, 'braak_max': 6, 'n_distinct': 5}),
        _unit('sex', 'microglial cell', n_lt5=True, level_counts={'f': 3, 'm': 9}, covariates=[],
              covariates_omitted=['pmi'], n_donors_analysed=12),
    ])))
    (root / 'run_2026-10-04.log').write_text('[skip] G3/ds3: no raw counts layer served\n[done] G1/ds1: 2/3 units\n')
    (root / fr.COHORT_TABLE_NAME).write_text(
        'status\tcohort\tcell_class\trole\tdb_id\trule_from_audit_8_3\n'
        'DRAFT\tCohortA (shared donors)\tmicroglia\tanchor\tG1/ds1\tanchor\n'
        'DRAFT\tCohortB\tmicroglia\tanchor\tG2/ds2\tanchor\n'
        'DRAFT\tCohortB\tmicroglia\tsupplement, only donors absent\tG2/ds9\tsupplement\n')
    return root


DATASETS = [{'db_id': 'G1/ds1', 'group_folder': 'G1', 'stem': 'ds1', 'title': 'Dataset one'},
            {'db_id': 'G2/ds2', 'group_folder': 'G2', 'stem': 'ds2', 'title': 'Dataset two'},
            {'db_id': 'G3/ds3', 'group_folder': 'G3', 'stem': 'ds3'}]


# ── lib: summary, rows, model ────────────────────────────────────────────────

def test_summary_units_flags_and_counts(results):
    reader = fr.FacetResults(results)
    summary = reader.summary('G1/ds1', 'G1', 'ds1', current_sha256='abc123')
    assert summary['status'] == 'tested'
    assert summary['dataset_changed'] is False
    by_facet = {u['facet']: u for u in summary['units']}
    assert set(by_facet) == {'sex', 'braak', 'disease'}
    sex = by_facet['sex']
    assert sex['tested'] and sex['contrast'] == 'm_vs_f'
    assert (sex['level_ref'], sex['level_test'], sex['n_donors_ref'], sex['n_donors_test']) == ('f', 'm', 10, 8)
    assert sex['genes'] == {'n_tested': len(GENES), 'n_q05': 4}
    methods = {(m['method'], m['source']): m for m in sex['modules']}
    assert methods[('cell_mean', 'geneset')] == {'method': 'cell_mean', 'source': 'geneset', 'n_modules': 3,
                                                 'n_q05_param': 1, 'n_q05_perm': 1}
    assert methods[('pseudobulk', 'gene_list')]['n_q05_perm'] == 1
    assert by_facet['braak']['flags']['composition_flag'] is True
    assert by_facet['braak']['level_counts']['braak_max'] == 6
    disease = by_facet['disease']
    assert not disease['tested'] and disease['reason'].startswith('level with < 3 donors')
    assert disease['flags']['provisional'] is True
    assert summary['runs']['genes']['seed'] == 20261004
    assert summary['runs']['modules']['n_permutations'] == 1000
    assert summary['cohort']['cohort_label'] == 'CohortA' and summary['cohort']['counted']
    assert reader.summary('G1/ds1', 'G1', 'ds1', current_sha256='other')['dataset_changed'] is True


def test_model_object_from_manifest(results):
    summary = fr.FacetResults(results).summary('G1/ds1', 'G1', 'ds1')
    braak = next(u for u in summary['units'] if u['facet'] == 'braak')
    model = braak['model']
    assert model['genes']['formula'] == 'expression ~ braak + sex + age_z + pmi_z'
    assert 'filterByExpr' in model['genes']['method'] and 'voom' in model['genes']['method']
    assert model['genes']['multiple_testing'].startswith('Benjamini-Hochberg')
    assert model['genes']['tool'] == 'ant-facet-test/1.0' and model['genes']['run_date'] == '2026-10-04'
    assert 'fewer than 10 cells' in model['genes']['unit_of_analysis']
    assert 'per Braak stage' in model['facet_term']
    batch = next(c for c in model['genes']['covariates'] if c['name'] == 'batch')
    assert batch['status'] == 'not used' and 'no donor-level batch column' in batch['reason']
    assert model['modules'] is None  # the module run has no braak unit
    sex = next(u for u in summary['units'] if u['facet'] == 'sex')
    assert sex['model']['genes']['formula'] == 'expression ~ sex + age_z + pmi_z'
    assert sex['model']['modules']['formula'] == 'score ~ sex + age_z + pmi_z'
    assert sex['model']['modules']['multiple_testing'] == '(dataset, stratum, facet, method)'
    assert 'pseudobulk' in sex['model']['modules']['unit_of_analysis']


def test_model_reasons_for_left_out_covariates(results):
    summary = fr.FacetResults(results).summary('G2/ds2', 'G2', 'ds2')
    sex = next(u for u in summary['units'] if u['facet'] == 'sex')
    rows = {c['name']: c for c in sex['model']['genes']['covariates']}
    # 12 donors: age is skipped silently by the tool below 15 donors
    assert rows['age']['status'] == 'not used' and 'fewer than 15 donors' in rows['age']['reason']
    assert rows['pmi']['status'] == 'dropped'
    assert sex['flags']['descriptive_only'] is True
    assert sex['model']['genes']['formula'] == 'expression ~ sex'


def test_model_reads_not_recorded_for_unknown_tool(results):
    manifest_path = results / 'G1' / 'ds1.facet_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['tool'] = 'ant-facet-test/9.9'
    del manifest['parameters']['gene_filter']
    manifest_path.write_text(json.dumps(manifest))
    summary = fr.FacetResults(results).summary('G1/ds1', 'G1', 'ds1')
    sex = next(u for u in summary['units'] if u['facet'] == 'sex')
    assert sex['model']['genes']['multiple_testing'] == fr.NOT_RECORDED
    assert 'filterByExpr' not in sex['model']['genes']['method']


def test_sex_positive_control(results):
    reader = fr.FacetResults(results)
    sex = next(u for u in reader.summary('G1/ds1', 'G1', 'ds1')['units'] if u['facet'] == 'sex')
    control = sex['sex_control']
    assert control['status'] == 'passed'
    assert control['n_y_up_q01'] == 3 and control['n_y_tested'] == 3
    sex2 = next(u for u in reader.summary('G2/ds2', 'G2', 'ds2')['units'] if u['facet'] == 'sex')
    assert sex2['sex_control']['status'] == 'not assessable'  # descriptive only: no q


def test_not_tested_reasons(results):
    reader = fr.FacetResults(results)
    skipped = reader.summary('G3/ds3', 'G3', 'ds3')
    assert skipped['status'] == 'not_tested' and skipped['reason'] == 'no raw counts layer served'
    missing = reader.summary('G9/none', 'G9', 'none')
    assert missing['status'] == 'not_run' and missing['units'] == []
    (results / 'G4').mkdir()
    manifest = _gene_manifest('G4/ds4', 'G4', 'ds4', [])
    manifest.update(status='skipped', reason='4 donors (< 6)')
    (results / 'G4' / 'ds4.facet_run_manifest.json').write_text(json.dumps(manifest))
    assert reader.summary('G4/ds4', 'G4', 'ds4')['reason'] == '4 donors (< 6)'


def test_partial_files_count_as_not_available(results):
    reader = fr.FacetResults(results)
    (results / 'G1' / 'ds1.facet_tests.parquet').write_bytes(b'PAR1 truncated')
    (results / 'G1' / 'ds1.facet_module_run_manifest.json').write_text('{"units": [')
    summary = reader.summary('G1/ds1', 'G1', 'ds1')
    assert summary['files']['genes'] is False
    assert reader.gene_rows('G1', 'ds1') is None
    # the module rows still answer
    frame, n_matching = reader.module_rows('G1', 'ds1', q=1)
    assert n_matching == 6


def test_gene_rows_filters_sort_and_limit(results):
    reader = fr.FacetResults(results)
    frame, n_matching = reader.gene_rows('G1', 'ds1', facet='sex', cell_type='microglial cell')
    assert n_matching == 4
    assert list(frame['gene']) == ['XIST', 'RPS4Y1', 'DDX3Y', 'UTY']  # q ascending
    frame, _ = reader.gene_rows('G1', 'ds1', facet='sex', q=1, sort='effect', limit=2)
    assert list(frame['gene']) == ['XIST', 'RPS4Y1'] and len(frame) == 2
    frame, n_matching = reader.gene_rows('G1', 'ds1', facet='braak', gene='ptprg', q=1)
    assert n_matching == 1 and frame.loc[0, 'logFC'] == pytest.approx(0.5)
    # q < 1 drops descriptive-only rows (q NULL); q = 1 keeps them
    assert reader.gene_rows('G2', 'ds2', facet='sex')[1] == 0
    assert reader.gene_rows('G2', 'ds2', facet='sex', q=1)[1] == len(GENES)
    with pytest.raises(fr.FacetResultsError):
        reader.gene_rows('G1', 'ds1', sort='bogus')


def test_module_rows_q_by_and_method(results):
    reader = fr.FacetResults(results)
    _, either = reader.module_rows('G1', 'ds1', facet='sex')
    _, by_param = reader.module_rows('G1', 'ds1', q_by='param')
    _, by_perm = reader.module_rows('G1', 'ds1', q_by='perm')
    assert (either, by_param, by_perm) == (4, 2, 2)
    frame, n_matching = reader.module_rows('G1', 'ds1', method='pseudobulk', q=1, sort='effect')
    assert n_matching == 3 and set(frame['method']) == {'pseudobulk'}
    assert list(frame['module']) == ['modC', 'modB', 'modA']
    with pytest.raises(fr.FacetResultsError):
        reader.module_rows('G1', 'ds1', q_by='nope')


def test_records_are_json_safe(results):
    frame, _ = fr.FacetResults(results).gene_rows('G2', 'ds2', facet='sex', q=1)
    rows = fr.records(frame)
    assert rows[0]['q_value'] is None and rows[0]['ci_low'] is None
    json.dumps(rows)


def test_volcano_png_and_descriptive_unit(results):
    reader = fr.FacetResults(results)
    png = reader.volcano_png('G1', 'ds1', 'sex', 'microglial cell')
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert reader.volcano_png('G2', 'ds2', 'sex', 'microglial cell') is None
    assert reader.volcano_png('G9', 'none', 'sex', 'x') is None


def test_cache_follows_file_changes(results):
    reader = fr.FacetResults(results)
    assert reader.gene_rows('G1', 'ds1', facet='sex')[1] == 4
    path = results / 'G1' / 'ds1.facet_tests.parquet'
    frame = pd.read_parquet(path)
    frame.loc[frame['gene'] == 'UTY', 'q_value'] = 0.9
    frame.to_parquet(path)
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    assert reader.gene_rows('G1', 'ds1', facet='sex')[1] == 3


# ── lib: cohorts, replication, recurring genes, overview ─────────────────────

def test_cohort_table_roles(results):
    cohorts = fr.FacetResults(results).cohorts()
    assert cohorts['G1/ds1']['cohort_label'] == 'CohortA' and cohorts['G1/ds1']['counted']
    assert cohorts['G2/ds9']['counted'] is False  # supplement
    assert cohorts['G2/ds2']['status'] == 'DRAFT'


def test_gene_effects_card_and_replication(results):
    card = fr.FacetResults(results).gene_effects('braak', 'PTPRG', DATASETS)
    assert card['facet']['effect_unit'] == 'log2 fold change per Braak stage'
    assert card['n_units'] == 2
    rep = card['replication'][0]
    assert (rep['cohorts_tested'], rep['cohorts_sig_same_sign'], rep['direction']) == (2, 2, 'up')
    assert rep['badge'] == 'replicates in 2 of 2 independent cohorts (up)'
    unit = card['units'][0]
    assert unit['model'].startswith('expression ~ braak') and unit['significant']
    assert card['definition'] == fr.REPLICATION_DEFINITION
    apoe = fr.FacetResults(results).gene_effects('braak', 'apoe', DATASETS)
    assert apoe['replication'][0]['sign_discordant'] is True
    assert 'sign discordant' in apoe['replication'][0]['badge']
    missing = fr.FacetResults(results).gene_effects('braak', 'NOPE', DATASETS)
    assert missing['units'] == [] and missing['replication'] == []


def test_replication_single_cohort_and_uncounted():
    rows = pd.DataFrame({'counted': [True, False], 'descriptive_only': [False, False],
                         'cohort': ['A', 'B'], 'q_value': [0.01, 0.001], 'logFC': [1.0, 1.0]})
    rep = fr.FacetResults._replication(rows)
    assert rep['cohorts_tested'] == 1
    assert rep['badge'] == 'one cohort, not replicated (q < 0.05, up)'


def test_apoe_locus_note_only_for_apoe_carrier(results):
    reader = fr.FacetResults(results)
    assert fr.APOE_LOCUS_NOTE not in reader.gene_effects('braak', 'APOE', DATASETS)['notes']
    card = reader.gene_effects('apoe_carrier', 'APOE', DATASETS)
    assert fr.APOE_LOCUS_NOTE in card['notes'] and card['apoe_locus']


def test_recurring_genes(results):
    out = fr.FacetResults(results).recurring_genes('braak', DATASETS)
    assert out['cell_type'] == 'microglial cell' and out['cohorts_in_cell_type'] == 2
    top = out['rows'][0]
    assert top['gene'] == 'PTPRG' and top['cohorts_sig_same_sign'] == 2 and top['direction'] == 'up'
    apoe = next(r for r in out['rows'] if r['gene'] == 'APOE')
    assert apoe['sign_discordant'] and apoe['apoe_locus']


def test_facet_overview_rows(results):
    overview = fr.FacetResults(results).facet_overview('sex', DATASETS)
    assert [r['db_id'] for r in overview['rows']] == ['G1/ds1', 'G2/ds2']  # most donors first
    first = overview['rows'][0]
    assert first['top_genes'][0]['gene'] == 'XIST'
    assert first['cohort_label'] == 'CohortA' and first['sex_control']['status'] == 'passed'
    assert first['model']['genes']['formula'] == 'expression ~ sex + age_z + pmi_z'
    disease = fr.FacetResults(results).facet_overview('disease', DATASETS)
    assert disease['rows'] == [] and disease['not_tested'][0]['db_id'] == 'G1/ds1'


# ── config ───────────────────────────────────────────────────────────────────

def test_facets_visible_config(monkeypatch):
    monkeypatch.delenv('ANTHIVE_FACETS_VISIBLE', raising=False)
    assert config.get_facets_visible({}) == list(config.DEFAULT_FACETS_VISIBLE)
    assert config.get_facets_visible(config.load_default_config()) == list(config.DEFAULT_FACETS_VISIBLE)
    assert config.get_facets_visible({'facets_visible': []}) == []
    assert config.get_facets_visible({'facets_visible': None}) == []
    assert config.get_facets_visible({'facets_visible': ['ant_sex']}) == ['ant_sex']
    monkeypatch.setenv('ANTHIVE_FACETS_VISIBLE', 'ant_braak, ant_sex')
    assert config.get_facets_visible({}) == ['ant_braak', 'ant_sex']
    monkeypatch.setenv('ANTHIVE_FACETS_VISIBLE', '*')
    assert config.get_facets_visible({'facets_visible': ['ant_sex']}) == []
    assert config.facet_hidden_by_config('ant_tissue', ['ant_sex']) is True
    assert config.facet_hidden_by_config('ant_tissue', []) is False


def test_facet_results_dir_config(monkeypatch, tmp_path):
    monkeypatch.delenv('ANTHIVE_FACET_RESULTS_DIR', raising=False)
    monkeypatch.setenv('ANTHIVE_H5ADSTORE', str(tmp_path / 'store'))
    assert config.get_facet_results_dir({}) == tmp_path / 'store' / '_facet_tests'
    monkeypatch.setenv('ANTHIVE_FACET_RESULTS_DIR', str(tmp_path / 'published'))
    assert config.get_facet_results_dir({}) == tmp_path / 'published'


# ── ant-serve endpoints ──────────────────────────────────────────────────────

_SERVE_WANTED = {
    '_facet_results_reader', '_facet_results_lock', '_facet_results', '_facet_results_location',
    '_facet_rows_response', '_FACET_ROWS_LIMIT_JSON', '_FACET_ROWS_LIMIT_CSV', '_facet_rows_limit',
    'get_facet_tests', 'get_facet_test_genes', 'get_facet_test_modules', 'get_facet_test_volcano',
    'get_facet_tests_across_datasets', '_tested_facet', '_facet_test_datasets',
    'get_facet_recurring_genes', 'get_facet_gene_effects', 'get_gene_facet_effects', 'get_facet_cohorts',
}


class _FakeRegistry:
    def __init__(self, store: Path):
        self.data_folder = store.resolve()
        self.databases = {'G1/ds1': 'x', 'G2/ds2': 'x', 'G3/ds3': 'x'}
        self._db_md_bases = {}
        for db_id in self.databases:
            group, stem = db_id.split('/')
            md = store / group / f'{stem}.md'
            md.parent.mkdir(parents=True, exist_ok=True)
            md.write_text('---\nid: x\n---\n')
            self._db_md_bases[db_id] = md
        self._db_extra = {}
        self.metadata_cache = {'G1/ds1': {'title': 'Dataset one', 'sha256': 'abc123'},
                               'G2/ds2': {'title': 'Dataset two'}}


@pytest.fixture
def serve(results):
    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.testclient import TestClient

    store = results.parent
    registry = _FakeRegistry(store)
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if name in _SERVE_WANTED:
            body.append(node)
    app = FastAPI()
    namespace: Dict[str, Any] = {
        'app': app, 'registry': registry, 'FacetResults': fr.FacetResults, 'facet_results_lib': fr,
        'get_config': lambda: {}, 'get_facet_results_dir': config.get_facet_results_dir, 'os': os,
        'log': logging.getLogger('test-facet-results'), 'Lock': Lock, 'Path': Path, 'pd': pd,
        'Response': Response, 'HTTPException': HTTPException, 'Query': Query,
        'Dict': Dict, 'List': List, 'Any': Any, 'Optional': Optional, 'Tuple': Tuple,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVE_PATH), 'exec', dont_inherit=True), namespace)
    return TestClient(app)


def test_serve_registers_routes_in_a_safe_order():
    source = SERVE_PATH.read_text()
    for route in ('"/databases/{db_id:path}/facet-tests"', '"/databases/{db_id:path}/facet-tests/genes"',
                  '"/databases/{db_id:path}/facet-tests/modules"', '"/databases/{db_id:path}/facet-tests/volcano.png"',
                  '"/facets/{facet_name}/tests"', '"/facets/{facet_name}/effects"',
                  '"/facets/{facet_name}/effects/top"', '"/genes/{gene}/facet-effects"', '"/facets/cohorts"'):
        assert f'@app.get({route}' in source, route
    # the greedy {db_id:path}/genes route would swallow .../facet-tests/genes
    assert source.index('"/databases/{db_id:path}/facet-tests/genes"') < source.index('@app.get("/databases/{db_id:path}/genes"')
    assert source.index('@app.get("/facets/cohorts"') < source.index('@app.get("/facets/{facet_name}",')
    assert source.index('@app.get("/facets/{facet_name}/effects/top"') < source.index('@app.get("/facets/{facet_name}/effects"')


def test_endpoint_summary(serve):
    body = serve.get('/databases/G1/ds1/facet-tests').json()
    assert body['status'] == 'tested' and body['n_units_tested'] == 2
    assert body['dataset_changed'] is False
    sex = next(u for u in body['units'] if u['facet'] == 'sex')
    assert sex['model']['genes']['formula'] == 'expression ~ sex + age_z + pmi_z'
    assert serve.get('/databases/G3/ds3/facet-tests').json()['reason'] == 'no raw counts layer served'
    assert serve.get('/databases/NO/such/facet-tests').status_code == 404


def test_endpoint_gene_rows_filters_csv_and_limits(serve):
    body = serve.get('/databases/G1/ds1/facet-tests/genes', params={'facet': 'sex'}).json()
    assert body['n_matching'] == 4 and body['rows'][0]['gene'] == 'XIST'
    body = serve.get('/databases/G1/ds1/facet-tests/genes',
                     params={'facet': 'braak', 'cell_type': 'microglial cell', 'q': 1, 'limit': 3}).json()
    assert body['n_returned'] == 3 and body['n_matching'] == len(GENES)
    csv = serve.get('/databases/G1/ds1/facet-tests/genes', params={'q': 1, 'format': 'csv', 'limit': 5000})
    assert csv.headers['content-type'].startswith('text/csv') and csv.text.startswith('facet,')
    assert 'G1_ds1_facet_genes.csv' in csv.headers['content-disposition']
    assert serve.get('/databases/G1/ds1/facet-tests/genes', params={'limit': 2001}).status_code == 400
    assert serve.get('/databases/G1/ds1/facet-tests/genes', params={'sort': 'x'}).status_code == 400
    assert serve.get('/databases/G3/ds3/facet-tests/genes').status_code == 404


def test_endpoint_module_rows(serve):
    body = serve.get('/databases/G1/ds1/facet-tests/modules', params={'method': 'cell_mean'}).json()
    assert body['n_matching'] == 2 and {r['method'] for r in body['rows']} == {'cell_mean'}
    assert serve.get('/databases/G2/ds2/facet-tests/modules').status_code == 404
    assert serve.get('/databases/G1/ds1/facet-tests/modules', params={'q_by': 'x'}).status_code == 400


def test_endpoint_volcano(serve):
    resp = serve.get('/databases/G1/ds1/facet-tests/volcano.png',
                     params={'facet': 'sex', 'cell_type': 'microglial cell'})
    assert resp.status_code == 200 and resp.headers['content-type'] == 'image/png'
    assert serve.get('/databases/G2/ds2/facet-tests/volcano.png',
                     params={'facet': 'sex', 'cell_type': 'microglial cell'}).status_code == 404


def test_endpoint_across_datasets_and_effects(serve):
    body = serve.get('/facets/ant_braak/tests').json()
    assert body['facet_column'] == 'ant_braak' and body['n_datasets_tested'] == 2
    assert body['rows'][0]['db_id'] == 'G2/ds2'  # 30 donors before 18
    assert serve.get('/facets/ant_tissue/tests').status_code == 404
    card = serve.get('/facets/braak/effects', params={'gene': 'PTPRG'}).json()
    assert card['replication'][0]['cohorts_sig_same_sign'] == 2
    top = serve.get('/facets/ant_braak/effects/top', params={'n': 5}).json()
    assert top['rows'][0]['gene'] == 'PTPRG'
    allf = serve.get('/genes/PTPRG/facet-effects').json()
    assert set(allf['facets']) == {'sex', 'disease', 'apoe_carrier', 'braak'}
    cohorts = serve.get('/facets/cohorts').json()
    assert cohorts['status'] == 'DRAFT' and cohorts['n_datasets'] == 3


# ── MCP ──────────────────────────────────────────────────────────────────────

def _mcp_tools() -> Dict[str, str]:
    tree = ast.parse(MCP_PATH.read_text())
    tools = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'tool':
                    tools[node.name] = ast.get_docstring(node) or ''
    return tools


def test_mcp_facet_tools_and_descriptions():
    tools = _mcp_tools()
    assert {'get_facet_tests', 'facet_effects', 'facet_top_genes', 'facet_units'} <= set(tools)
    doc = tools['get_facet_tests']
    for phrase in ('EXPLORATORY', 'donor', 'limma-voom', 'permutations', 'PROVISIONAL',
                   'descriptive_only', 'composition', 'state the model'):
        assert phrase in doc, phrase
    effects = ' '.join(tools['facet_effects'].split())
    assert 'replicates in k of m independent cohorts' in effects
    assert 'contributes at most once' in effects and 'EXPLORATORY' in effects
    assert 'APOE' in effects and 'PROVISIONAL' in effects
    for tool in ('facet_top_genes', 'facet_units'):
        assert 'EXPLORATORY' in tools[tool]
    for tool in ('get_facet_tests', 'facet_effects', 'facet_top_genes', 'facet_units'):
        assert '—' not in tools[tool], tool


def test_json_safe_strips_nan_and_numpy():
    import numpy as np
    payload = fr.json_safe({'a': float('nan'), 'b': [np.float32(1.5), np.int64(3), pd.NA],
                            'c': {'d': float('inf'), 'e': 'x'}})
    assert payload == {'a': None, 'b': [1.5, 3, None], 'c': {'d': None, 'e': 'x'}}
    json.dumps(payload, allow_nan=False)


def test_gene_card_is_strict_json_with_mixed_levels(results):
    # braak units carry no level_ref while sex units do: the merged unit
    # columns hold NaN, which must not reach the JSON payload
    reader = fr.FacetResults(results)
    reader.facet_gene_table('sex', DATASETS)
    for facet in ('sex', 'braak'):
        json.dumps(reader.gene_effects(facet, 'PTPRG', DATASETS), allow_nan=False)
        json.dumps(reader.recurring_genes(facet, DATASETS), allow_nan=False)
        json.dumps(reader.facet_overview(facet, DATASETS), allow_nan=False)
