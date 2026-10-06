"""Tests for ``bin/annot/ant-facet-test`` (replicate-level facet testing, v3).

Covers, on a tiny synthetic dataset built like ant-convert output
(``__cells``, ``__genes``, ``__obscat``, ``__obsnum`` + a counts by_gene
sidecar + ``.md`` / ``.h5ad.mus``):

* a planted sex effect is recovered with the right sign and size, and null
  genes stay mostly non-significant (needs R with limma + edgeR; skipped
  otherwise),
* small units (a level below 5 replicates) are tested, counted and flagged ``small_sample``,
* in vitro, bulk and secondary datasets are excluded; xenograft and mouse datasets are tested in
  their own facet scope,
* donors with several samples collapse into one pseudobulk sample per
  (donor, cell type) and the summed counts match,
* the current .md mapping (not the stored view) defines the facets,
* level, batch-confounding and normalisation rules.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy \
           --with scipy --with pyarrow --with pyyaml --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_facet_test.py -q
Set ANT_FACET_RSCRIPT to an Rscript that has limma and edgeR (default:
envs/facet-test-r of the checkout).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))


def _load_tool():
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-facet-test'
    loader = importlib.machinery.SourceFileLoader('ant_facet_test', str(path))
    spec = importlib.util.spec_from_loader('ant_facet_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


aft = _load_tool()


def _rscript_with_limma() -> str | None:
    candidate = os.environ.get('ANT_FACET_RSCRIPT', aft.DEFAULT_RSCRIPT)
    if not candidate or not Path(candidate).exists():
        return None
    result = subprocess.run([candidate, '-e', 'library(limma); library(edgeR)'],
                            capture_output=True, text=True)
    return candidate if result.returncode == 0 else None


RSCRIPT = _rscript_with_limma()
needs_r = pytest.mark.skipif(RSCRIPT is None, reason='no Rscript with limma + edgeR')

N_GENES = 300
PLANTED_GENE = 'GENE0'
PLANTED_LOG2_FC = 2.0


def make_dataset(tmp_path: Path, n_f: int = 10, n_m: int = 10, organism: str = 'human',
                 system: str = 'post_mortem_tissue', extra_api: str = '',
                 is_secondary: bool = False, samples_per_donor: int = 1,
                 disease_pattern: str | None = None, seed: int = 7, extra_md: str = '',
                 extra_rename: str = ''):
    """Write a synthetic dataset; returns (store_root, H5adEntry).

    Two cell types (T1 with 30 cells per donor, T2 with 4 cells per donor so
    it falls under the 10 cell minimum). GENE0 is up in males by 2 log2 units
    in T1. The author sex column is ``Sex`` with values F / M and the .md
    renames it to ant_sex (the current mapping), so the physical table never
    carries an ant_ name.
    """
    rng = np.random.default_rng(seed)
    store = tmp_path / 'store'
    group_dir = store / 'Syn2026'
    group_dir.mkdir(parents=True)
    (group_dir / 'index.md').write_text('---\ngroup: Syn2026\ngroup_id: SY1\n---\n')
    stem = 'Syn_ds'
    donors = [f'D{i:02d}' for i in range(n_f + n_m)]
    sexes = ['F'] * n_f + ['M'] * n_m
    disease = disease_pattern and disease_pattern.split(',')
    rows = []
    cell_id = 0
    base_mean = rng.uniform(2.0, 12.0, size=N_GENES)
    count_triples = []
    for donor_index, (donor, sex) in enumerate(zip(donors, sexes)):
        donor_factor = np.exp(rng.normal(0, 0.15))
        for cell_type, n_cells in (('T1', 30), ('T2', 4)):
            for cell_index in range(n_cells):
                sample = f'{donor}_s{cell_index % samples_per_donor}'
                rows.append((cell_id, f'{sample}_c{cell_id}', donor, sample, sex, cell_type,
                             disease[donor_index] if disease else 'control'))
                mean_vector = base_mean * donor_factor
                if sex == 'M' and cell_type == 'T1':
                    mean_vector = mean_vector.copy()
                    mean_vector[0] = mean_vector[0] * 2 ** PLANTED_LOG2_FC
                counts = rng.poisson(mean_vector)
                for gene_id in np.nonzero(counts)[0]:
                    count_triples.append((cell_id, int(gene_id), float(counts[gene_id])))
                cell_id += 1
    cells = pd.DataFrame(rows, columns=['cell_id', 'cell_name', 'donor_id', 'sample_id', 'Sex',
                                        'celltype', 'dx'])
    database_path = group_dir / f'{stem}.duckdb'
    conn = duckdb.connect(str(database_path))
    conn.execute('CREATE TABLE __cells AS SELECT cell_id, cell_name FROM cells')
    genes = pd.DataFrame({'gene_id': range(N_GENES),
                          'gene_name': [f'GENE{i}' for i in range(N_GENES)]})
    conn.execute('CREATE TABLE __genes AS SELECT * FROM genes')
    donor_rank = cells['donor_id'].map({d: i for i, d in enumerate(donors)})
    cells['bank'] = np.where(donor_rank % 2 == 0, 'BA', 'BB')      # donor level, crossed with sex
    cells['age'] = 50.0 + 3.0 * donor_rank                          # D00 50 ... D19 107
    obscat = cells[['cell_id', 'donor_id', 'sample_id', 'Sex', 'celltype', 'dx', 'bank']]
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat')
    obsnum = cells[['cell_id', 'age']]
    conn.execute('CREATE TABLE __obsnum AS SELECT * FROM obsnum')
    conn.close()
    triples = pd.DataFrame(count_triples, columns=['cell_id', 'gene_id', 'value'])
    triples['cell_id'] = triples['cell_id'].astype('int32')
    triples['gene_id'] = triples['gene_id'].astype('int32')
    triples['value'] = triples['value'].astype('float32')
    triples.sort_values(['gene_id', 'cell_id']).to_parquet(
        group_dir / f'{stem}.layer.counts.by_gene.parquet', index=False)
    md_text = (
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\n'
        f'is_secondary: {"true" if is_secondary else "false"}\n'
        'layer_stats:\n  counts:\n    min_nonzero: 1.0\n'
        'api:\n  obs_rename:\n    donor_id: ant_donor\n    Sex: ant_sex\n'
        '    celltype: ant_author_cell_type\n    dx: ant_disease\n' + extra_rename + extra_api +
        '  obs_value_rename:\n    Sex:\n      F: f\n      M: m\n' + extra_md + '---\n')
    (group_dir / f'{stem}.md').write_text(md_text)
    (group_dir / f'{stem}.h5ad.mus').write_text(
        f'organism={organism}\nexperimental_system={system}\n')
    entry = aft.H5adEntry(db_id='SY1/SYN', md_path=group_dir / f'{stem}.md',
                          h5ad_path=group_dir / f'{stem}.h5ad',
                          duckdb_path=database_path, group_id='SY1', file_id='SYN',
                          study='Syn2026', stem=stem)
    return store, entry


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def test_facet_notes_from_md_normalises_names_and_whitespace():
    assert aft.facet_notes_from_md({}) == {}
    assert aft.facet_notes_from_md({'facet_notes': 'nonsense'}) == {}
    notes = aft.facet_notes_from_md({'facet_notes': {'ant_disease': 'a  proxy\n note', 'sex': '', 'x': 5}})
    assert notes == {'disease': 'a proxy note'}


def test_normalisers():
    assert aft.normalize_sex('F') == 'f' and aft.normalize_sex('male') == 'm'
    assert aft.normalize_sex('unknown') is None and aft.normalize_sex(None) is None
    assert aft.normalize_disease('AD') == 'AD' and aft.normalize_disease('control') == 'control'
    for dropped in ('AD_DLBD', 'AD_vascular', 'MCI', 'NCI', 'PD', 'other', None):
        assert aft.normalize_disease(dropped) is None
    assert aft.normalize_apoe4_carrier('E4+') == 'e4_carrier'
    assert aft.normalize_apoe4_carrier('E4-') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('E3/E4') == 'e4_carrier'
    assert aft.derive_apoe4_carrier_from_genotype('E4/E4') == 'e4_carrier'
    assert aft.derive_apoe4_carrier_from_genotype('E2/E3') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('33') == 'noncarrier'
    assert aft.derive_apoe4_carrier_from_genotype('unknown') is None


@pytest.mark.parametrize('column', ['ant_apoe4_carrier', 'ant_apoe_carrier'])
def test_donor_table_finds_the_apoe_facet_under_the_new_and_the_old_column(column):
    cells = pd.DataFrame({'ant_donor': ['D1', 'D1', 'D2'], column: ['E4+', 'E4+', 'E4-']})
    donors, conflicts = aft.build_donor_table(cells)
    assert dict(zip(donors['donor'], donors['apoe4_carrier'])) == {'D1': 'e4_carrier', 'D2': 'noncarrier'}
    assert conflicts == {}


def test_donor_table_prefers_the_new_column_and_derives_from_the_genotype_otherwise():
    both = pd.DataFrame({'ant_donor': ['D1'], 'ant_apoe4_carrier': ['E4-'], 'ant_apoe_carrier': ['E4+']})
    assert aft.build_donor_table(both)[0]['apoe4_carrier'].tolist() == ['noncarrier']
    genotype = pd.DataFrame({'ant_donor': ['D1'], 'ant_apoe': ['E3/E4']})
    assert aft.build_donor_table(genotype)[0]['apoe4_carrier'].tolist() == ['e4_carrier']


def _donor_table(levels: list[str], batches: list[str] | None = None, facet: str = 'disease',
                 sexes: list[str] | None = None):
    frame = pd.DataFrame({'donor': [f'D{i}' for i in range(len(levels))], facet: levels})
    frame['sex'] = sexes if sexes is not None else ['f', 'm'] * (len(levels) // 2) + ['f'] * (len(levels) % 2)
    frame['host_sex'] = None
    frame['app_nlgf'] = None
    frame['age'] = np.nan
    frame['pmi'] = np.nan
    frame['braak'] = np.nan
    frame['batch'] = batches if batches is not None else None
    return frame


def test_plan_job_drops_level_with_fewer_than_three_donors():
    table = _donor_table(['AD'] * 2 + ['control'] * 8)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['status'] != 'ok' and 'level' in plan['status']


def test_plan_job_n_lt5_is_an_informational_flag_that_never_blocks():
    table = _donor_table(['AD'] * 4 + ['control'] * 10)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['status'] == 'ok' and plan['n_lt5'] is True
    table = _donor_table(['AD'] * 5 + ['control'] * 10)
    assert aft.plan_job(table, 'disease', table['donor'])['n_lt5'] is False
    smallest = _donor_table(['AD'] * 3 + ['control'] * 3)
    plan = aft.plan_job(smallest, 'disease', smallest['donor'])
    assert plan['status'] == 'ok' and plan['n_lt5'] is True and plan['n_donors_analysed'] == 6


def test_plan_job_batch_confounded_is_flagged_and_dropped():
    levels = ['AD'] * 6 + ['control'] * 6
    batches = ['b1'] * 6 + ['b2'] * 6          # batch == disease
    table = _donor_table(levels, batches)
    plan = aft.plan_job(table, 'disease', table['donor'])
    assert plan['batch_confounded'] is True and plan['batch_used'] is False
    mixed = ['b1', 'b2'] * 6                   # batch crossed with disease
    plan = aft.plan_job(_donor_table(levels, mixed), 'disease', table['donor'])
    assert plan['batch_confounded'] is False and plan['batch_used'] is True


def test_plan_job_age_pmi_only_with_15_donors():
    small = _donor_table(['AD'] * 7 + ['control'] * 7)
    small['age'] = np.arange(14.0)
    assert 'age' not in aft.plan_job(small, 'disease', small['donor'])['covariates']
    large = _donor_table(['AD'] * 8 + ['control'] * 8)
    large['age'] = np.arange(16.0) % 7
    plan = aft.plan_job(large, 'disease', large['donor'])
    assert 'age' in plan['covariates'] and 'sex' in plan['covariates']
    sex_plan = aft.plan_job(_donor_table(['f', 'm'] * 6, facet='sex'), 'sex', [f'D{i}' for i in range(12)])
    assert 'sex' not in sex_plan['covariates']


def test_braak_plan_uses_numeric_value():
    table = _donor_table(['control'] * 12)
    table['braak'] = [0, 1, 2, 3, 4, 5, 6, 2, 3, 4, 5, 6]
    plan = aft.plan_job(table, 'braak', table['donor'])
    assert plan['status'] == 'ok' and plan['n_lt5'] is False
    assert plan['design']['facet_value'].max() == 6


def test_numeric_facets_need_four_replicates_with_three_distinct_values():
    table = _donor_table(['control'] * 4)
    table['braak'] = [1.0, 3.0, 5.0, 3.0]
    assert aft.plan_job(table, 'braak', table['donor'])['status'] == 'ok'
    table['braak'] = [1.0, 3.0, 3.0, 1.0]            # two distinct values
    assert 'distinct' in aft.plan_job(table, 'braak', table['donor'])['status']
    three = _donor_table(['control'] * 3)
    three['braak'] = [1.0, 3.0, 5.0]                 # three replicates
    assert aft.plan_job(three, 'braak', three['donor'])['status'] != 'ok'


def test_age_facet_is_per_decade_without_an_age_covariate():
    table = _donor_table(['control'] * 20, sexes=['f', 'm'] * 10)
    table['age'] = np.linspace(60.0, 95.0, 20)
    table['pmi'] = np.linspace(2.0, 30.0, 20) % 7
    plan = aft.plan_job(table, 'age', table['donor'])
    assert plan['status'] == 'ok'
    design = plan['design']
    assert design['facet_value'].max() == pytest.approx(9.5) and design['facet_value'].min() == pytest.approx(6.0)
    assert 'age' not in plan['covariates'] and {'sex', 'pmi'} <= set(plan['covariates'])
    assert plan['level_counts']['value_min'] == pytest.approx(60.0) and plan['level_counts']['n_distinct'] == 20
    assert aft.tau_for_facet('age') == pytest.approx(np.log2(1.1))
    assert aft.tau_for_facet('braak') == pytest.approx(np.log2(1.1) / 3)
    assert aft.tau_for_facet('sex') == pytest.approx(np.log2(1.1))


def test_covariates_are_dropped_before_the_test_is():
    # 4 replicates: the slope leaves 2 residual df, a sex covariate would leave 1, so the covariate goes
    table = _donor_table(['control'] * 4, sexes=['f', 'm', 'f', 'm'])
    table['braak'] = [1.0, 3.0, 5.0, 3.0]
    plan = aft.plan_job(table, 'braak', table['donor'])
    assert plan['status'] == 'ok' and plan['covariates'] == [] and 'sex(df)' in plan['covariates_omitted']
    # 3 vs 3: sex would leave 3 residual df, below the floor of 4: the covariate goes, the test stays
    six = _donor_table(['AD'] * 3 + ['control'] * 3, batches=['b1', 'b2'] * 3, sexes=['f', 'm', 'f', 'm', 'f', 'm'])
    plan = aft.plan_job(six, 'disease', six['donor'])
    assert plan['status'] == 'ok' and plan['covariates'] == [] and 'sex(df)' in plan['covariates_omitted']
    # 5 vs 5: sex (7 left) and a two-level batch (6 left) fit; a batch with one level per donor does not
    ten = _donor_table(['AD'] * 5 + ['control'] * 5, batches=['b1', 'b2'] * 5, sexes=['f', 'm'] * 5)
    plan = aft.plan_job(ten, 'disease', ten['donor'])
    assert plan['status'] == 'ok' and plan['covariates'] == ['sex', 'batch']
    assert len(plan['design']) - (2 + len(plan['covariates'])) >= facet_rules_min_df()
    many = _donor_table(['AD'] * 5 + ['control'] * 5, batches=list('abcdefghij'), sexes=['f', 'm'] * 5)
    plan = aft.plan_job(many, 'disease', many['donor'])
    assert plan['status'] == 'ok' and 'batch' not in plan['covariates']


def facet_rules_min_df():
    return aft.MIN_RESIDUAL_DF


def test_age_adjusted_plan_uses_braak_else_disease_and_never_age():
    table = _donor_table(['AD'] * 10 + ['control'] * 10, sexes=['f', 'm'] * 10)
    table['age'] = np.linspace(61.0, 95.0, 20)
    table['braak'] = np.linspace(0, 6, 20).round()
    plan = aft.plan_job(table, 'age_adjusted', table['donor'])
    assert plan['status'] == 'ok' and 'pathology:braak' in plan['covariates'] and 'age' not in plan['covariates']
    assert plan['design']['cov_path'].notna().all()
    no_braak = table.assign(braak=np.nan)
    plan = aft.plan_job(no_braak, 'age_adjusted', no_braak['donor'])
    assert plan['status'] == 'ok' and 'pathology:disease' in plan['covariates']
    assert set(plan['design']['cov_path']) == {0.0, 1.0}
    nothing = no_braak.assign(disease=None)
    refused = aft.plan_job(nothing, 'age_adjusted', nothing['donor'])
    assert refused['status'].startswith('age_adjusted: no pathology covariate')
    unadjusted = aft.plan_job(table, 'age', table['donor'])
    assert unadjusted['status'] == 'ok' and not any(c.startswith('pathology') for c in unadjusted['covariates'])


def test_app_nlgf_is_refused_when_the_genotype_is_confounded_with_a_design_covariate():
    table = _donor_table(['control'] * 8)
    table['app_nlgf'] = ['nlgf'] * 4 + ['control'] * 4
    table['batch'] = ['inj1'] * 4 + ['inj2'] * 4                    # injection arm = genotype
    refused = aft.plan_job(table, 'app_nlgf', table['donor'], refuse_confounded='injection')
    assert refused['status'] == 'app_nlgf is confounded with injection: not tested'
    crossed = table.assign(batch=['inj1', 'inj2'] * 4)
    ok = aft.plan_job(crossed, 'app_nlgf', crossed['donor'], refuse_confounded='injection')
    assert ok['status'] == 'ok' and ok['batch_used'] is True
    dropped = aft.plan_job(table, 'app_nlgf', table['donor'])          # without the rule the old behaviour: batch dropped
    assert dropped['status'] == 'ok' and dropped['batch_confounded'] is True


def test_split_by_design_makes_one_group_per_timepoint_and_graft_line():
    table = _donor_table(['control'] * 8)
    table['timepoint'] = ['3m'] * 4 + ['6m'] * 4
    table['graft_line'] = ['WT', 'WT', 'KO', 'KO'] * 2
    groups = dict(aft.split_by_design(table, table['donor'], {'timepoint': 'age', 'graft_line': 'line'}))
    assert set(groups) == {'timepoint=3m|line=KO', 'timepoint=3m|line=WT', 'timepoint=6m|line=KO', 'timepoint=6m|line=WT'}
    assert all(len(v) == 2 for v in groups.values())
    only_time = dict(aft.split_by_design(table, table['donor'], {'timepoint': 'age'}))
    assert set(only_time) == {'timepoint=3m', 'timepoint=6m'}
    assert aft.split_by_design(table, table['donor'], {}) == [(None, list(table['donor']))]
    table.loc[0, 'timepoint'] = None
    assert sum(len(v) for v in dict(aft.split_by_design(table, table['donor'], {'timepoint': 'age'})).values()) == 7


def test_app_nlgf_design_from_the_md():
    design = aft.app_nlgf_design_from_md({'control_basis': 'wt_host', 'app_nlgf_design': {
        'timepoint_column': 'age', 'graft_line_column': 'ant_genotype', 'covariate_columns': 'injection'}})
    assert design == {'control_basis': 'wt_host', 'timepoint': 'age', 'graft_line': 'ant_genotype',
                      'covariates': ['injection']}
    assert aft.design_columns_of(design) == ['age', 'ant_genotype', 'injection']
    assert aft.app_nlgf_design_from_md({})['control_basis'] is None
    for bad in ({'control_basis': 'mystery'}, {'app_nlgf_design': ['x']}, {'app_nlgf_design': {'nope': 1}}):
        with pytest.raises(ValueError):
            aft.app_nlgf_design_from_md(bad)


def test_donor_table_carries_the_design_columns():
    cells = pd.DataFrame({'ant_donor': ['A', 'A', 'B', 'B'], 'age': ['3m', '3m', '6m', '6m'],
                          'line': ['x', 'x', 'y', 'z'], 'inj': ['i1', 'i1', 'i2', 'i2']})
    table, _ = aft.build_donor_table(cells, {'timepoint': 'age', 'graft_line': 'line', 'covariates': ['inj']})
    row = table.set_index('donor')
    assert row.loc['A', 'timepoint'] == '3m' and row.loc['A', 'graft_line'] == 'x' and row.loc['A', 'design_cov'] == 'i1'
    assert pd.isna(row.loc['B', 'graft_line'])                      # two lines in one animal: no value, never a pick


def test_genomic_inflation_and_sex_discordance_and_top_coding():
    rng = np.random.default_rng(1)
    null_p = rng.uniform(size=4000)
    assert 0.9 < aft.genomic_inflation(null_p) < 1.1
    assert aft.genomic_inflation(null_p ** 3) > 1.3 and aft.genomic_inflation([0.5] * 10) is None
    symbols = pd.DataFrame({'gene_id': [0, 1, 2], 'symbol': ['XIST', 'DDX3Y', 'ACTB']})
    rows, sample_donor, labels = [], {}, {}
    for index in range(10):
        male = index >= 5
        sample_donor[index] = f'D{index}'
        labels[f'D{index}'] = 'm' if male else 'f'
        rows += [(index, 0, 5 if male else 400), (index, 1, 300 if male else 2), (index, 2, 10000)]
    counts = pd.DataFrame(rows, columns=['sample_idx', 'gene_id', 'count'])
    assert aft.sex_discordance(counts, symbols, sample_donor, labels) == {'n_checked': 10, 'n_discordant': 0}
    labels['D2'] = 'm'                                             # a female sample labelled male
    labels['D7'] = 'f'
    assert aft.sex_discordance(counts, symbols, sample_donor, labels) == {'n_checked': 10, 'n_discordant': 2}
    assert aft.sex_discordance(counts, symbols.assign(symbol=['A', 'B', 'C']), sample_donor, labels)['n_checked'] == 0
    ages = pd.DataFrame({'age': [90.0] * 5 + list(np.linspace(60, 89, 20))})
    assert aft.age_top_coding(ages) == 90.0
    assert aft.age_top_coding(pd.DataFrame({'age': np.linspace(60, 89, 30)})) is None


def test_persisting_age_genes_are_marked_from_the_adjusted_twin():
    def unit(facet, effects, q_values):
        return pd.DataFrame({'facet': facet, 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells',
                             'gene_id': [0, 1, 2], 'logFC': effects, 'q_value': q_values,
                             'persists_after_pathology_adjustment': np.nan})
    age = unit('age', [0.2, 0.2, -0.2], [0.01, 0.01, 0.01])
    adjusted = unit('age_adjusted', [0.2, -0.2, -0.2], [0.01, 0.01, 0.5])
    aft.mark_persisting_age_genes([age, adjusted])
    assert age['persists_after_pathology_adjustment'].tolist() == [1.0, 0.0, 0.0]
    assert adjusted['persists_after_pathology_adjustment'].isna().all()


def test_app_nlgf_plan_uses_host_sex_in_a_xenograft():
    table = _donor_table(['control'] * 8)
    table['app_nlgf'] = ['nlgf'] * 4 + ['control'] * 4
    table['sex'] = ['f', 'm'] * 4                       # donor sex (varies within an animal in a village)
    table['host_sex'] = ['f', 'f', 'm', 'm'] * 2
    plan = aft.plan_job(table, 'app_nlgf', table['donor'], scope='xenograft_human_in_mouse')
    assert plan['status'] == 'ok' and plan['covariates'] == ['sex']
    design = plan['design']
    expected = (table.set_index('donor')['host_sex'] == 'm').astype(float)
    assert (design.set_index('donor')['cov_sex'] == expected.reindex(design['donor']).to_numpy()).all()
    assert plan['level_counts'] == {'control': 4, 'nlgf': 4}
    mouse = aft.plan_job(table, 'app_nlgf', table['donor'], scope='mouse_in_vivo')
    donor_sex = (table.set_index('donor')['sex'] == 'm').astype(float)
    assert (mouse['design'].set_index('donor')['cov_sex'] == donor_sex.reindex(mouse['design']['donor']).to_numpy()).all()


def test_facets_for_scope_follow_the_registry():
    allowed = aft.facets_for_scope('mouse_in_vivo', aft.FACETS)
    assert allowed == ['sex', 'app_nlgf']
    assert aft.facets_for_scope('xenograft_human_in_mouse', aft.FACETS) == ['sex', 'app_nlgf']
    assert aft.facets_for_scope('human_postmortem', aft.FACETS) == [
        'sex', 'disease', 'apoe4_carrier', 'braak', 'age', 'age_adjusted']
    assert aft.normalize_app_nlgf('NLGF') == 'nlgf' and aft.normalize_app_nlgf('wt') is None


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _eligibility(tmp_path, **kwargs):
    store, entry = make_dataset(tmp_path, **kwargs)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    return aft.dataset_eligibility(entry, md_yaml, annotation, aft.get_api_config(md_yaml))


def test_eligible_baseline(tmp_path):
    assert _eligibility(tmp_path) == (True, 'ok')


def test_xenograft_is_a_tested_scope(tmp_path):
    store, entry = make_dataset(tmp_path, organism='human', system='xenograft')
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    api = aft.get_api_config(md_yaml)
    assert aft.dataset_eligibility(entry, md_yaml, annotation, api) == (True, 'ok')
    assert aft.dataset_scope(entry, annotation, api) == 'xenograft_human_in_mouse'


def test_in_vitro_and_bulk_excluded(tmp_path):
    assert not _eligibility(tmp_path / 'a', system='in_vitro_2d')[0]
    assert not _eligibility(tmp_path / 'b', system='bulk_tissue')[0]


def test_mouse_is_a_tested_scope_mixed_organism_and_secondary_are_not(tmp_path):
    assert _eligibility(tmp_path / 'a', organism='mouse') == (True, 'ok')
    assert not _eligibility(tmp_path / 'b', organism='human,mouse')[0]
    assert not _eligibility(tmp_path / 'c', is_secondary=True)[0]


def test_host_sex_facet_makes_a_human_dataset_a_xenograft(tmp_path):
    store, entry = make_dataset(tmp_path, extra_api='    sample_id: ant_host_sex\n')
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    annotation = aft.mus.resolve_annotation(entry.h5ad_path, root=store)
    api = aft.get_api_config(md_yaml)
    assert aft.dataset_scope(entry, annotation, api) == 'xenograft_human_in_mouse'
    assert aft.dataset_eligibility(entry, md_yaml, annotation, api)[0]


# ---------------------------------------------------------------------------
# Current mapping, pseudobulk collapse
# ---------------------------------------------------------------------------

def test_views_use_current_md_mapping(tmp_path):
    store, entry = make_dataset(tmp_path)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    conn = aft.open_dataset(entry, aft.get_api_config(md_yaml), threads=1, memory_limit_gb=1)
    columns = aft.view_columns(conn, 'obscat')
    assert {'ant_donor', 'ant_sex', 'ant_author_cell_type', 'ant_disease'} <= set(columns)
    cells = aft.cell_table(conn, ['ant_donor', 'ant_sex'])
    assert set(cells['ant_sex'].dropna()) == {'f', 'm'}
    conn.close()


def test_multi_sample_donors_collapse_to_one_pseudobulk_sample(tmp_path):
    store, entry = make_dataset(tmp_path, samples_per_donor=3)
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    conn = aft.open_dataset(entry, aft.get_api_config(md_yaml), threads=1, memory_limit_gb=1)
    cells = aft.cell_table(conn, ['ant_donor', 'ant_author_cell_type'])
    t1 = cells[cells['ant_author_cell_type'] == 'T1'].copy()
    t1['donor'] = t1['ant_donor'].astype(str)
    donors = sorted(t1['donor'].unique())
    t1['sample_idx'] = t1['donor'].map({d: i for i, d in enumerate(donors)})
    layer, parquet = aft.counts_layer_for(entry, md_yaml)
    pseudobulk = aft.pseudobulk(conn, parquet, t1[['cell_id', 'sample_idx']])
    assert pseudobulk['sample_idx'].nunique() == len(donors) == 20
    raw = pd.read_parquet(parquet)
    expected = raw[raw['cell_id'].isin(t1['cell_id'])].merge(
        t1[['cell_id', 'sample_idx']], on='cell_id').groupby(['sample_idx', 'gene_id'])['value'].sum()
    got = pseudobulk.set_index(['sample_idx', 'gene_id'])['count']
    assert np.allclose(got.sort_index().to_numpy(), expected.sort_index().to_numpy())
    conn.close()


# ---------------------------------------------------------------------------
# End to end (needs R)
# ---------------------------------------------------------------------------

@needs_r
def test_planted_sex_effect_recovered(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, samples_per_donor=2)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1,
                                   memory_limit_gb=1)
    assert manifest['status'] == 'done'
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    t1 = table[(table['cell_type'] == 'T1') & (table['facet'] == 'sex')]
    planted = t1[t1['gene'] == PLANTED_GENE].iloc[0]
    assert planted['logFC'] == pytest.approx(PLANTED_LOG2_FC, abs=0.5)
    assert planted['q_value'] < 0.01
    assert planted['ci_low'] < planted['logFC'] < planted['ci_high']
    assert planted['n_donors_ref'] == 10 and planted['n_donors_test'] == 10
    assert planted['contrast'] == 'm_vs_f'
    null_genes = t1[t1['gene'] != PLANTED_GENE]
    assert (null_genes['q_value'] < 0.05).mean() < 0.05
    # T2 has only 4 cells per donor: below the 10 cell minimum, never tested
    assert 'T2' not in set(table['cell_type'])
    assert manifest['pseudobulk']['n_dropped_lt_min_cells'] >= 20
    # manifest carries the reproducibility fields
    for key in ('dataset_sha256', 'seed', 'r_versions', 'parameters', 'anthive4_git'):
        assert key in manifest
    assert json.loads((out_root / 'Syn2026' / 'Syn_ds.facet_run_manifest.json').read_text())['seed']


EXCLUSION_MD = ('facet_donor_exclusions:\n  reason: shared persons\n  obs:\n'
                '    sample_id: [D00_s0, D01_s0, D12_s0]\n')


@needs_r
def test_donor_exclusion_rule_counted_run_and_all_donors_run(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_md=EXCLUSION_MD)
    out_root = tmp_path / 'out'
    counted = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert counted['status'] == 'done'
    exclusion = counted['donor_exclusion']
    assert exclusion['counted'] is True and exclusion['variant'] == 'counted'
    assert (exclusion['n_donors_before'], exclusion['n_donors_excluded'], exclusion['n_donors_after']) == (20, 3, 17)
    assert counted['n_donors'] == 17
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    planted = table[(table['cell_type'] == 'T1') & (table['gene'] == PLANTED_GENE)].iloc[0]
    assert planted['n_donors'] == 17 and planted['n_donors_ref'] == 8 and planted['n_donors_test'] == 9
    # the manifest holds counts, never donor labels
    # (the rule itself is echoed in the manifest; here its values are sample labels, in practice a bank code)
    text = json.dumps({'units': counted['units'], 'keys': counted['donor_keys'],
                       'counts': {k: v for k, v in exclusion.items() if k not in ('rule', 'n_excluded_by_criterion')}})
    assert 'D00' not in text and 'D12' not in text
    digests = [u['donor_key_digests'] for u in counted['units'] if u['status'] == 'ok'][0]
    assert len(digests) == 17
    # full run: every donor, supplementary folder, not counted; the counted files are untouched
    before = (out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet').read_bytes()
    full = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1,
                               all_donors=True)
    assert full['status'] == 'done' and full['donor_exclusion']['counted'] is False
    assert full['n_donors'] == 20 and full['donor_exclusion']['n_donors_excluded'] == 3
    supplementary = out_root / 'Syn2026' / 'supplementary_all_donors'
    assert (supplementary / 'Syn_ds.facet_tests.parquet').exists()
    assert (out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet').read_bytes() == before
    all_table = pd.read_parquet(supplementary / 'Syn_ds.facet_tests.parquet')
    assert all_table[all_table['gene'] == PLANTED_GENE].iloc[0]['n_donors'] == 20


@needs_r
def test_age_floor_applies_to_the_declared_facets_only(tmp_path):
    pattern = ','.join(['AD', 'control'] * 10)
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, disease_pattern=pattern,
                                extra_rename='    age: ant_age\n', extra_md='facet_min_age:\n  disease: 80\n')
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex', 'disease'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done'
    report = manifest['facet_min_age']['disease']
    assert report['floor_years'] == 80.0 and report['n_donors_below_floor'] == 10 and report['n_donors'] == 20
    by_facet = {u['facet']: u for u in manifest['units'] if u['status'] == 'ok'}
    assert by_facet['sex']['n_donors_analysed'] == 20                  # sex keeps every donor
    assert by_facet['disease']['n_donors_analysed'] == 10              # D10..D19 are 80 or older


@needs_r
def test_declared_batch_column_replaces_the_name_guess(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_md='facet_batch_column: bank\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['batch_column'] == 'bank'
    unit = [u for u in manifest['units'] if u['status'] == 'ok'][0]
    assert unit['batch_used'] is True and unit['batch_confounded'] is False


def test_declared_batch_column_must_exist(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_batch_column: nope\n')
    with pytest.raises(ValueError, match='not an obs column'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


@needs_r
def test_all_donors_run_needs_a_rule_and_plain_datasets_are_unchanged(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=8, n_m=8)
    out_root = tmp_path / 'out'
    skipped = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1,
                                  all_donors=True)
    assert skipped['status'] == 'skipped' and 'no facet_donor_exclusions' in skipped['reason']
    plain = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert plain['status'] == 'done' and 'donor_exclusion' not in plain and plain['n_donors'] == 16


def test_malformed_exclusion_rule_is_an_error_not_a_silent_skip(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_donor_exclusions: nonsense\n')
    with pytest.raises(ValueError, match='must be a mapping'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


def test_unknown_exclusion_column_fails_loudly(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='facet_donor_exclusions:\n  obs:\n    no_such_col: [x]\n')
    with pytest.raises(ValueError, match='not an obs column'):
        aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT or 'Rscript', threads=1,
                            memory_limit_gb=1)


@needs_r
def test_small_units_are_tested_counted_and_flagged(tmp_path):
    # 3 AD vs 12 control donors: tested (>= 3 per level), flagged small_sample, p and q present, not descriptive only
    pattern = ','.join(['AD'] * 3 + ['control'] * 12)
    store, entry = make_dataset(tmp_path, n_f=8, n_m=7, disease_pattern=pattern)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['disease'], RSCRIPT, threads=1,
                                   memory_limit_gb=1)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    disease = table[table['facet'] == 'disease']
    assert len(disease) > 0
    assert disease['n_lt5'].all() and disease['small_sample'].all() and not disease['descriptive_only'].any()
    for column in ('p_value', 'q_value', 'ci_low', 'ci_high', 't', 'p_treat', 'q_treat'):
        assert disease[column].notna().any()
    assert (disease['facet_scope'] == 'human_postmortem').all()
    assert disease['provisional'].all()
    unit = [u for u in manifest['units'] if u['facet'] == 'disease'][0]
    assert unit['n_lt5'] is True and unit['small_sample'] is True and unit['descriptive_only'] is False
    assert 'n_sig_q05' in unit and manifest['facet_scope'] == 'human_postmortem'


def test_choose_strata_rules():
    cells = pd.DataFrame({
        'ant_cell_type': ['astrocyte'] * 6,
        'ant_author_cell_type': ['A1', 'A1', 'A2', 'A2', 'A1', None],
        'ant_author_cluster': ['c1', 'c2', 'c1', 'c2', 'c1', 'c2'],
    })
    membership, _ = aft.choose_strata(cells, {})
    assert set(membership['source']) == {'constant_all_cells', 'ant_author_cell_type'}
    assert (membership[membership['source'] == 'constant_all_cells']['label'] == 'astrocyte').all()
    assert len(membership[membership['source'] == 'ant_author_cell_type']) == 5   # NULL label dropped
    varying = cells.assign(ant_cell_type=['a', 'a', 'b', 'b', 'a', 'b'])
    membership, _ = aft.choose_strata(varying, {})
    assert set(membership['source']) == {'ant_cell_type'}
    absent = cells.drop(columns=['ant_cell_type', 'ant_author_cell_type'])
    membership, _ = aft.choose_strata(absent, {})
    assert set(membership['source']) == {'ant_author_cluster'}
    # no ant_cell_type and no ant_author_cell_type: an author column such as M23/IMM's
    # cell_type_high_resolution must be used (it used to fall through to a single 'all' stratum)
    high_res = pd.DataFrame({
        'cell_type_high_resolution': ['Mic P2RY12', 'Mic P2RY12', 'T cells', 'CAMs', 'Mic TPT1', None],
        'ant_author_cluster': ['c1', 'c2', 'c1', 'c2', 'c1', 'c2'],
    })
    membership, note = aft.choose_strata(high_res, {'cell_type_high_resolution': 'VARCHAR'})
    # the two Mic labels are one group: a derived whole-microglia pool stratum is added
    assert set(membership['source']) == {'cell_type_high_resolution', 'group_pool'}
    own = membership[membership['source'] == 'cell_type_high_resolution']
    assert set(own['label']) == {'Mic P2RY12', 'T cells', 'CAMs', 'Mic TPT1'}     # NULL label dropped
    pooled = membership[membership['source'] == 'group_pool']
    assert set(pooled['label']) == {'microglia'} and sorted(pooled['pos']) == [0, 1, 4]
    assert 'cell_type_high_resolution' in note and 'group pool' in note
    for name in ('cell_type_high_resolution', 'author_cell_type_low_resolution', 'cell_type_fine', 'celltype_major'):
        assert aft.CELLTYPE_CANDIDATE_RE.match(name), name
    for name in ('cell_type_ontology_term_id', 'seurat_idents', 'sample'):
        assert not aft.CELLTYPE_CANDIDATE_RE.match(name), name
    nothing = pd.DataFrame({'x': [1, 2, 3]})
    membership, _ = aft.choose_strata(nothing, {})
    assert set(membership['source']) == {'none'} and set(membership['label']) == {'all'}
