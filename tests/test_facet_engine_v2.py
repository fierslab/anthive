"""Facet engine v2: replication statistics, replicate unit and analysis level,
limma treat columns, donor-pooled units, group-pool strata.

* ``lib/replication.py``: BH, partial conjunction (Fisher, Simes) and r-values
  against closed forms.
* ``lib/replicates.py``: declared / inferred replicate unit, analysis level rule.
* ``lib/facet_lint.py``: ``replicate_unit`` / ``replicate_column`` rules.
* ``bin/annot/ant-facet-test``: treat p / q, tau per facet, manifest fields,
  a non-donor replicate column, donor pooling over member files (R needed).
* ``lib/facet_results.py``: pooled units are served as datasets and win the anchor.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facet_lint  # noqa: E402
import facet_results as fr  # noqa: E402
import replicates  # noqa: E402
import replication  # noqa: E402
from test_facet_test import PLANTED_GENE, RSCRIPT, aft, make_dataset, needs_r  # noqa: E402

NAN = float('nan')


# ── lib/replication.py ───────────────────────────────────────────────────────

def test_benjamini_hochberg_matches_definition_and_ignores_nan():
    p_values = np.array([0.01, 0.04, 0.03, NAN, 0.5])
    adjusted = replication.benjamini_hochberg(p_values)
    assert adjusted[:3].tolist() == pytest.approx([0.04, 0.16 / 3, 0.16 / 3])   # n = 4 valid values
    assert math.isnan(adjusted[3])
    assert adjusted[4] == pytest.approx(0.5)
    assert math.isnan(replication.benjamini_hochberg(np.array([NAN]))[0])      # all NaN stays NaN
    assert replication.benjamini_hochberg(np.array([])).size == 0


def test_partial_conjunction_fisher_closed_form():
    p_matrix = np.array([[1e-6, 1e-4, 0.3, 0.8], [0.2, 0.9, 0.5, 0.4]])
    combined = replication.partial_conjunction_p(p_matrix, u=2, method='fisher')
    largest = [1e-4, 0.3, 0.8]                 # the m - u + 1 = 3 largest of the first row
    expected = stats.chi2.sf(-2 * sum(math.log(p) for p in largest), 2 * 3)
    assert combined[0] == pytest.approx(expected)
    assert 0 < combined[1] <= 1


def test_partial_conjunction_simes_closed_form_and_u_equal_m_is_max_p():
    row = np.array([[0.001, 0.01, 0.2, 0.6]])
    simes = replication.partial_conjunction_p(row, u=2, method='simes')
    largest = [0.01, 0.2, 0.6]
    expected = min(len(largest) * p / rank for rank, p in enumerate(largest, start=1))
    assert simes[0] == pytest.approx(expected)
    for method in replication.COMBINE_METHODS:
        both = replication.partial_conjunction_p(np.array([[0.001, 0.02]]), u=2, method=method)
        assert both[0] == pytest.approx(0.02)   # one p left: the larger of the two (max-p rule)


def test_partial_conjunction_handles_nan_rows_and_validates_arguments():
    matrix = np.array([[0.01, NAN, NAN], [0.01, 0.02, NAN], [0.01, 0.02, 0.03]])
    combined = replication.partial_conjunction_p(matrix, u=2)
    assert math.isnan(combined[0])                 # fewer than u cohorts
    assert combined[1] == pytest.approx(0.02)
    assert combined[2] > 0
    with pytest.raises(ValueError):
        replication.partial_conjunction_p(matrix, method='stouffer')
    with pytest.raises(ValueError):
        replication.partial_conjunction_p(matrix, u=0)
    with pytest.raises(ValueError):
        replication.partial_conjunction_p(np.array([0.1, 0.2]))
    assert replication.partial_conjunction_p(np.empty((0, 3))).size == 0


def test_replicability_direction_and_bh():
    # gene 0 replicates up, gene 1 is up in one cohort only, gene 2 is discordant, gene 3 untested twice
    p_up = np.array([[1e-8, 1e-7, 0.4], [1e-9, 1.0, 1.0], [1e-9, 1.0, 1.0], [1e-3, NAN, NAN]])
    p_down = np.array([[1.0, 1.0, 0.6], [1.0, 1e-9, 1.0], [1.0, 1e-8, 1.0], [1.0, NAN, NAN]])
    out = replication.replicability(p_up, p_down)
    assert out['direction'][0] == 'up' and out['direction'][3] is None
    assert out['r_value'][0] < 0.05
    assert out['r_value'][1] > 0.5 and out['r_value'][2] > 0.5
    assert math.isnan(out['r_value'][3])


# ── lib/replicates.py ────────────────────────────────────────────────────────

@pytest.mark.parametrize('metadata, annotation, columns, expected', [
    ({}, {'organism': 'human'}, ['ant_donor', 'x'], ('donor', 'ant_donor', 'inferred')),
    ({}, {}, ['ant_donor'], ('donor', 'ant_donor', 'inferred')),
    ({}, {'organism': 'human'}, ['x'], ('none', None, 'inferred')),
    ({}, {'organism': 'mouse'}, ['hash.ID', 'ant_donor_uid'], ('animal', 'hash.ID', 'inferred')),
    ({}, {'organism': 'mouse'}, ['ant_donor', 'Mouse_ID'], ('animal', 'Mouse_ID', 'inferred')),
    ({}, {'organism': 'mouse'}, ['ant_donor'], ('none', None, 'inferred')),   # ant_donor is not an animal id
    ({}, {'organism': 'human', 'experimental_system': 'xenograft'}, ['ant_donor', 'mouse.id'],
     ('animal', 'mouse.id', 'inferred')),
    ({}, {'organism': 'human', 'experimental_system': 'xenograft'}, ['ant_donor'], ('none', None, 'inferred')),
    ({}, {'organism': 'mouse'}, ['sample'], ('none', None, 'inferred')),
    ({'replicate_unit': 'sample', 'replicate_column': 'library'}, {}, [], ('sample', 'library', 'declared')),
    ({'replicate_unit': 'donor'}, {}, [], ('donor', 'ant_donor', 'declared')),
    ({'replicate_unit': 'none', 'replicate_column': 'x'}, {}, [], ('none', None, 'declared')),
    ({'replicate_unit': 'animal'}, {}, [], ('animal', None, 'declared')),
])
def test_resolve_replicate(metadata, annotation, columns, expected):
    resolved = replicates.resolve_replicate(metadata, annotation, columns)
    assert (resolved['replicate_unit'], resolved['replicate_column'], resolved['basis']) == expected


@pytest.mark.parametrize('metadata', [{'replicate_unit': 'person'}, {'replicate_unit': 3},
                                      {'replicate_unit': 'donor', 'replicate_column': ''},
                                      {'replicate_column': 5}])
def test_declared_replicate_rejects_bad_values(metadata):
    with pytest.raises(replicates.ReplicateError):
        replicates.declared_replicate(metadata)


def test_analysis_level_rule():
    ok = replicates.analysis_level('donor', False, n_per_level={'f': 3, 'm': 3})
    assert ok == {'analysis_level': 'replicate', 'label': 'replicate level', 'reason': None}
    few = replicates.analysis_level('animal', False, n_per_level={'ctrl': 2, 'treated': 6})
    assert few['analysis_level'] == 'cell' and few['label'] == 'cell-level, descriptive only'
    assert 'fewer than 3 animals' in few['reason']
    one_level = replicates.analysis_level('donor', False, n_per_level={'f': 9})
    assert one_level['analysis_level'] == 'cell'
    none = replicates.analysis_level('none', False, n_per_level={'f': 9, 'm': 9})
    assert none['analysis_level'] == 'cell' and 'replicate_unit is none' in none['reason']
    assert replicates.analysis_level(None, True, n_replicates=50, n_distinct=7)['analysis_level'] == 'cell'
    numeric = replicates.analysis_level('donor', True, n_replicates=6, n_distinct=3)
    assert numeric['analysis_level'] == 'replicate'
    assert replicates.analysis_level('donor', True, n_replicates=5, n_distinct=5)['analysis_level'] == 'cell'
    assert replicates.analysis_level('donor', True, n_replicates=9, n_distinct=1)['analysis_level'] == 'cell'
    custom = replicates.analysis_level('donor', False, n_per_level={'a': 2, 'b': 2}, min_per_level=2)
    assert custom['analysis_level'] == 'replicate'


# ── lib/facet_lint.py ────────────────────────────────────────────────────────

def test_lint_replicate_rules():
    missing = facet_lint.lint_replicate({}, {})
    assert [f.rule for f in missing] == ['replicate_unit_missing'] and missing[0].severity == 'warning'
    assert facet_lint.lint_replicate({'replicate_unit': 'donor', 'replicate_column': 'ant_donor'}, {}) == []
    invalid = facet_lint.lint_replicate({'replicate_unit': 'person'}, {})
    assert [f.rule for f in invalid] == ['replicate_unit_invalid'] and invalid[0].severity == 'error'
    unused = facet_lint.lint_replicate({'replicate_unit': 'none', 'replicate_column': 'x'}, {})
    assert [f.rule for f in unused] == ['replicate_column_unused']
    donor = {'replicate_unit': 'donor', 'replicate_column': 'ant_donor'}
    api = {'obs_rename': {'Donor': 'ant_donor'}}
    assert facet_lint.lint_replicate(donor, api, stored_columns=['Donor']) == []
    assert facet_lint.lint_replicate(donor, {}, stored_columns=['ant_donor']) == []
    absent = facet_lint.lint_replicate({'replicate_unit': 'animal', 'replicate_column': 'hash.ID'}, {},
                                       stored_columns=['sample'])
    assert [f.rule for f in absent] == ['replicate_column_missing'] and absent[0].severity == 'error'


# ── tool: treat, tau, replicate fields (R needed) ────────────────────────────

def test_tau_constants_are_ten_percent():
    assert aft.TAU_TWO_GROUP == pytest.approx(math.log2(1.1))
    assert aft.TAU_BRAAK_PER_STAGE * 3 == pytest.approx(math.log2(1.1))


@needs_r
def test_treat_columns_tau_and_manifest(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10)
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done'
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    t1 = table[table['cell_type'] == 'T1']
    assert {'p_treat', 'q_treat', 'tau_log2'} <= set(t1.columns)
    assert (t1['tau_log2'] == aft.TAU_TWO_GROUP).all()
    planted = t1[t1['gene'] == PLANTED_GENE].iloc[0]
    assert planted['q_treat'] < 0.01 and planted['logFC'] > aft.TAU_TWO_GROUP
    # treat tests |logFC| > tau: never smaller than the one-sided p of the plain test in the observed direction
    one_sided = t1['p_value'] / 2
    assert (t1['p_treat'] >= one_sided - 1e-12).all()
    assert (t1['q_treat'] >= t1['q_value'] - 1e-9).mean() > 0.95
    assert manifest['parameters']['tau_two_group_log2'] == pytest.approx(aft.TAU_TWO_GROUP)
    assert (manifest['replicate_unit'], manifest['replicate_column'], manifest['replicate_basis']) == (
        'donor', 'ant_donor', 'inferred')
    unit = next(u for u in manifest['units'] if u['cell_type'] == 'T1')
    assert unit['analysis_level'] == 'replicate' and unit['analysis_level_reason'] is None
    assert unit['replicate_unit'] == 'donor' and unit['tau_log2'] == pytest.approx(aft.TAU_TWO_GROUP)
    assert manifest['disease_basis'] is None


@needs_r
def test_tau_is_configurable_and_braak_uses_per_stage_tau(tmp_path):
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, extra_api='')
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1,
                                   tau_two_group=0.5, tau_braak_per_stage=0.1)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    assert (table['tau_log2'] == 0.5).all()
    assert manifest['parameters']['tau_two_group_log2'] == 0.5
    wide = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    strict = wide[wide['gene'] == PLANTED_GENE].iloc[0]
    assert strict['p_treat'] > 0  # a larger tau lowers the evidence


@needs_r
def test_declared_replicate_none_skips_the_dataset(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='replicate_unit: none\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'skipped' and 'replicate_unit is none' in manifest['reason']


@needs_r
def test_replicate_column_other_than_ant_donor_is_the_pseudobulk_unit(tmp_path):
    """The author column ``sample_id`` (two per donor here) as the replicate column:
    twice the replicates, all of them analysed."""
    store, entry = make_dataset(tmp_path, n_f=10, n_m=10, samples_per_donor=2,
                                extra_md='replicate_unit: sample\nreplicate_column: sample_id\n')
    out_root = tmp_path / 'out'
    manifest = aft.process_dataset(entry, store, out_root, ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'done' and manifest['n_donors'] == 40
    assert (manifest['replicate_unit'], manifest['replicate_column'], manifest['replicate_basis']) == (
        'sample', 'sample_id', 'declared')
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_ds.facet_tests.parquet')
    assert table[table['cell_type'] == 'T1']['n_donors'].iloc[0] == 40


@needs_r
def test_replicate_column_missing_skips(tmp_path):
    store, entry = make_dataset(tmp_path, extra_md='replicate_unit: animal\nreplicate_column: nope\n')
    manifest = aft.process_dataset(entry, store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'skipped' and 'nope' in manifest['reason']


def test_disease_basis_of_agreement():
    assert aft.disease_basis_of([{}]) is None
    assert aft.disease_basis_of([{'disease_basis': 'nia_reagan'}, {'disease_basis': 'nia_reagan'}]) == 'nia_reagan'
    assert aft.disease_basis_of([{'disease_basis': 'nia_reagan'}, {'disease_basis': 'adnc'}]) == 'mixed'
    assert aft.disease_basis_of([{'disease_basis': 'adnc'}, {}]) == 'adnc'


# ── group pool strata ────────────────────────────────────────────────────────

def test_group_pool_strata_pool_labels_of_one_group_only():
    cells = pd.DataFrame({'cell_type_high_resolution': ['Mic P2RY12', 'Mic TPT1', 'Mic MKI67', 'CAMs', 'T cells',
                                                        'weird label', 'weird two']})
    membership, note = aft.choose_strata(cells, {'cell_type_high_resolution': 'VARCHAR'})
    pools = membership[membership['source'] == 'group_pool']
    assert set(pools['label']) == {'microglia'} and sorted(pools['pos']) == [0, 1, 2]
    assert '1 derived cell type group pool' in note
    assert aft.group_of('microglia') == 'microglia'          # the pool label maps to its own group
    single = pd.DataFrame({'ant_cell_type': ['microglial cell', 'astrocyte', 'astrocyte']})
    assert 'group_pool' not in set(aft.choose_strata(single, {})[0]['source'])


def test_group_pool_not_built_for_unmapped_labels():
    cells = pd.DataFrame({'ant_cell_type': ['xyz1', 'xyz2', 'xyz1', 'xyz2']})
    assert 'group_pool' not in set(aft.choose_strata(cells, {})[0]['source'])


# ── donor pooling over member files (R needed) ───────────────────────────────

def _second_member(first_store: Path, first_entry, tmp_path: Path, seed: int = 11):
    """A second file of the same collection with the same donors and different counts."""
    other_store, other_entry = make_dataset(tmp_path / 'second', seed=seed)
    folder = first_store / 'Syn2026'
    stem = 'Syn_ds2'
    for suffix in ('.duckdb', '.layer.counts.by_gene.parquet', '.h5ad.mus'):
        shutil.copy(other_entry.md_path.parent / f'Syn_ds{suffix}', folder / f'{stem}{suffix}')
    md_text = other_entry.md_path.read_text().replace('id: SYN', 'id: SY2')
    (folder / f'{stem}.md').write_text(md_text)
    return aft.H5adEntry(db_id='SY1/SY2', md_path=folder / f'{stem}.md', h5ad_path=folder / f'{stem}.h5ad',
                         duckdb_path=folder / f'{stem}.duckdb', group_id='SY1', file_id='SY2',
                         study='Syn2026', stem=stem)


def test_pooled_pseudobulk_is_the_sum_over_member_files(tmp_path):
    store, first = make_dataset(tmp_path)
    second = _second_member(store, first, tmp_path)
    members = [first, second]
    conns, layers, cells_parts = {}, {}, []
    for index, entry in enumerate(members):
        md, _ = aft.parse_md_file(entry.md_path)
        conns[entry.db_id] = aft.open_dataset(entry, aft.get_api_config(md), threads=1, memory_limit_gb=1)
        layers[entry.db_id] = aft.counts_layer_for(entry, md)
        part = aft.cell_table(conns[entry.db_id], ['ant_donor', 'ant_author_cell_type'])
        part = part[part['ant_author_cell_type'] == 'T1'].assign(file_index=index)
        cells_parts.append(part)
    cells = pd.concat(cells_parts, ignore_index=True)
    donors = sorted(cells['ant_donor'].unique())
    cells['sample_idx'] = cells['ant_donor'].map({d: i for i, d in enumerate(donors)})
    long_counts, genes = aft.pooled_pseudobulk(members, conns, layers, cells)
    assert len(donors) == 20 and long_counts['sample_idx'].nunique() == 20
    assert genes['symbol'].tolist() == sorted(genes['symbol'])
    expected = 0.0
    for entry, part in zip(members, cells_parts):
        raw = pd.read_parquet(layers[entry.db_id][1])
        expected += raw[raw['cell_id'].isin(part['cell_id']) & (raw['gene_id'] == 0)]['value'].sum()
    gene0 = genes.loc[genes['symbol'] == 'GENE0', 'gene_id'].iloc[0]
    assert long_counts[long_counts['gene_id'] == gene0]['count'].sum() == pytest.approx(expected)
    for conn in conns.values():
        conn.close()


@needs_r
def test_process_pool_pools_donors_and_writes_pooled_unit(tmp_path):
    store, first = make_dataset(tmp_path, n_f=10, n_m=10)
    second = _second_member(store, first, tmp_path)
    out_root = tmp_path / 'out'
    manifest = aft.process_pool('syn_pool', [first, second], store, out_root, ['sex'], RSCRIPT, threads=1,
                                memory_limit_gb=1)
    assert manifest['status'] == 'done' and manifest['db_id'] == 'pool:syn_pool'
    assert manifest['n_donors'] == 20 and manifest['n_cells'] == 2 * first_n_cells(first)
    assert manifest['pool']['members'] == ['SY1/SYN', 'SY1/SY2']
    assert 'ignored' in manifest['pool']['region_handling']
    assert manifest['batch_column'] is None
    table = pd.read_parquet(out_root / '_pooled' / 'syn_pool.facet_tests.parquet')
    assert set(table['db_id']) == {'pool:syn_pool'}
    t1 = table[table['cell_type'] == 'T1']
    assert t1['n_donors'].iloc[0] == 20                                  # one sample per donor, not per file
    planted = t1[t1['gene'] == PLANTED_GENE].iloc[0]
    assert planted['q_treat'] < 0.01
    assert (out_root / '_pooled' / 'syn_pool.facet_run_manifest.json').exists()
    assert not (out_root / 'Syn2026').exists()                           # members write nothing
    assert aft.pool_manifest_current(out_root / '_pooled' / 'syn_pool.facet_run_manifest.json', [first, second])
    second.md_path.write_text(second.md_path.read_text() + '\n')
    assert not aft.pool_manifest_current(out_root / '_pooled' / 'syn_pool.facet_run_manifest.json',
                                         [first, second])


def first_n_cells(entry) -> int:
    import duckdb
    conn = duckdb.connect(str(entry.duckdb_path), read_only=True)
    try:
        return conn.execute('SELECT count(*) FROM __cells').fetchone()[0]
    finally:
        conn.close()


@needs_r
def test_process_pool_skips_when_no_member_is_eligible(tmp_path):
    store, first = make_dataset(tmp_path, organism='mouse')
    manifest = aft.process_pool('p', [first], store, tmp_path / 'out', ['sex'], RSCRIPT, threads=1, memory_limit_gb=1)
    assert manifest['status'] == 'skipped' and manifest['reason'] == 'no eligible member file'


# ── facet_results: pooled units ──────────────────────────────────────────────

def _write_unit(folder: Path, db_id: str, stem: str, effect: float, q_value: float, tool: str = 'ant-facet-test/2.0'):
    folder.mkdir(parents=True, exist_ok=True)
    genes = [f'G{i}' for i in range(12)]
    rows = [{'facet': 'sex', 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells', 'gene': gene,
             'logFC': effect if gene == 'G0' else 0.01, 'ci_low': 0.0, 'ci_high': 1.0,
             'q_value': q_value if gene == 'G0' else 0.9, 'p_treat': q_value ** 2 if gene == 'G0' else 0.8,
             'q_treat': q_value if gene == 'G0' else 0.9, 'tau_log2': 0.1375, 'n_donors': 20,
             'descriptive_only': False, 'n_lt5': False} for gene in genes]
    pd.DataFrame(rows).to_parquet(folder / f'{stem}.facet_tests.parquet')
    unit = {'facet': 'sex', 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells', 'status': 'ok',
            'n_donors_analysed': 20, 'n_genes_tested': 12, 'level_counts': {'f': 10, 'm': 10},
            'covariates': [], 'covariates_omitted': [], 'analysis_level': 'replicate',
            'analysis_level_label': 'replicate level', 'replicate_unit': 'donor', 'replicate_column': 'ant_donor',
            'tau_log2': 0.1375}
    (folder / f'{stem}.facet_run_manifest.json').write_text(json.dumps({
        'tool': tool, 'db_id': db_id, 'file': stem, 'units': [unit], 'parameters': {}, 'status': 'done',
        'replicate_unit': 'donor', 'replicate_column': 'ant_donor', 'replicate_basis': 'inferred',
        'disease_basis': None}))


def test_pooled_units_are_served_and_win_the_anchor(tmp_path):
    root = tmp_path / '_facet_tests'
    _write_unit(root / 'WA4', 'WA4/MIT', 'mit', 0.5, 0.01)          # a regional file of MADRC
    _write_unit(root / 'WA4', 'WA4/MPF', 'mpf', 0.5, 0.01)
    _write_unit(root / '_pooled', 'pool:madrc', 'madrc', 0.6, 1e-4)
    _write_unit(root / 'G1', 'G1/ds', 'ds', 0.4, 1e-4)
    (root / fr.COHORT_TABLE_NAME).write_text(
        'status\tcohort\tcell_class\trole\tdb_id\tpool_members\tindependence\n'
        'DRAFT\tMADRC\tmyeloid\tanchor\tWA4/MIT\t\tchecked\n'
        'DRAFT\tMADRC\tmyeloid\tanchor\tWA4/MPF\t\tchecked\n'
        'DRAFT\tMADRC\tmyeloid\tanchor (donor-pooled)\tpool:madrc\tWA4/MIT,WA4/MPF\tchecked\n'
        'DRAFT\tOther (x)\tmicroglia\tanchor\tG1/ds\t\tunknown\n')
    reader = fr.FacetResults(root)
    assert fr.pool_definitions(root) == {'madrc': ['WA4/MIT', 'WA4/MPF']}
    assert [d['db_id'] for d in reader.pooled_datasets()] == ['pool:madrc']
    assert reader.cohorts()['G1/ds']['overlap_unknown'] is True
    datasets = [{'db_id': 'WA4/MIT', 'group_folder': 'WA4', 'stem': 'mit'},
                {'db_id': 'WA4/MPF', 'group_folder': 'WA4', 'stem': 'mpf'},
                {'db_id': 'G1/ds', 'group_folder': 'G1', 'stem': 'ds'}]
    card = reader.gene_effects('sex', 'G0', datasets)
    rep = card['replication'][0]
    assert rep['m'] == 2 and rep['replicated'] is True and rep['r_value'] < 0.05
    assert {a['db_id'] for a in rep['anchors']} == {'pool:madrc', 'G1/ds'}
    assert rep['overlap_unknown'] is True and rep['cohorts_overlap_unknown'] == ['Other']
    assert 'donor overlap unknown for Other' in rep['badge']
    by_db = {u['db_id']: u for u in card['units']}
    assert by_db['pool:madrc']['anchor'] and by_db['pool:madrc']['pooled']
    assert by_db['WA4/MIT']['counted'] is False
    assert by_db['WA4/MIT']['not_counted_reason'] == 'supplementary: pool:madrc is the anchor of this cohort for this cell type group'
    assert by_db['G1/ds']['analysis_level'] == 'replicate'
    top = reader.recurring_genes('sex', datasets)
    assert [r['gene'] for r in top['rows']] == ['G0'] and top['rows'][0]['overlap_unknown'] is True
    overview = reader.facet_overview('sex', datasets)
    assert 'pool:madrc' in {r['db_id'] for r in overview['rows']}


def test_summary_units_report_analysis_level_for_untested_and_old_runs(tmp_path):
    root = tmp_path / '_facet_tests'
    _write_unit(root / 'G1', 'G1/ds', 'ds', 0.4, 1e-4)
    manifest_path = root / 'G1' / 'ds.facet_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['units'].append({'facet': 'braak', 'cell_type': 'microglial cell', 'celltype_source': 'constant_all_cells',
                              'status': 'braak: 3 donors / 2 distinct values (need 6 / 3)',
                              'analysis_level': 'cell', 'analysis_level_label': 'cell-level, descriptive only',
                              'analysis_level_reason': 'braak: 3 donors / 2 distinct values (need 6 / 3)'})
    manifest_path.write_text(json.dumps(manifest))
    summary = fr.FacetResults(root).summary('G1/ds', 'G1', 'ds')
    assert (summary['replicate_unit'], summary['replicate_column']) == ('donor', 'ant_donor')
    sex = next(u for u in summary['units'] if u['facet'] == 'sex')
    braak = next(u for u in summary['units'] if u['facet'] == 'braak')
    assert sex['analysis_level'] == 'replicate' and sex['replicate_unit'] == 'donor'
    assert braak['analysis_level'] == 'cell' and 'need 6 / 3' in braak['analysis_level_reason']
    assert braak['analysis_level_label'] == 'cell-level, descriptive only'
    # a run before the rule: no fields in the manifest, derived from tested / untested
    manifest['units'] = [{k: v for k, v in u.items() if not k.startswith(('analysis_level', 'replicate'))}
                         for u in manifest['units']]
    manifest_path.write_text(json.dumps(manifest))
    old = fr.FacetResults(root).summary('G1/ds', 'G1', 'ds')
    assert next(u for u in old['units'] if u['facet'] == 'sex')['analysis_level'] == 'replicate'
    assert next(u for u in old['units'] if u['facet'] == 'braak')['analysis_level'] == 'cell'


def test_basis_class():
    assert [fr.basis_class(b) for b in ('nia_reagan', 'braak_cerad', 'adnc', 'clinical_dx', 'author_dx', None, 'x')] == [
        'pathology', 'pathology', 'pathology', 'clinical', 'clinical', 'unknown', 'unknown']


# ── per-cell tools: replicate_unit column and API marker ─────────────────────

def test_per_cell_basis_is_descriptive_whatever_the_replicate_unit():
    donor = replicates.per_cell_basis('donor')
    assert donor == {'basis': 'per-cell (descriptive)', 'replicate_unit': 'donor',
                     'replicate_level_analysis': 'possible'}
    assert replicates.per_cell_basis('none')['replicate_level_analysis'] == 'impossible'
    assert replicates.per_cell_basis(None)['replicate_level_analysis'] == 'impossible'
    assert replicates.PER_CELL_LABEL == 'per-cell (descriptive)'


def test_dataset_replicate_unit_from_disk(tmp_path):
    store, entry = make_dataset(tmp_path)
    assert replicates.dataset_replicate_unit(entry.md_path, entry.duckdb_path, store) == 'donor'
    declared_store, declared = make_dataset(tmp_path / 'b', extra_md='replicate_unit: sample\n'
                                                                    'replicate_column: sample_id\n')
    assert replicates.dataset_replicate_unit(declared.md_path, declared.duckdb_path, declared_store) == 'sample'
    mouse_store, mouse = make_dataset(tmp_path / 'c', organism='mouse')
    assert replicates.dataset_replicate_unit(mouse.md_path, mouse.duckdb_path, mouse_store) == 'none'
    bad_store, bad = make_dataset(tmp_path / 'd', extra_md='replicate_unit: person\n')
    assert replicates.dataset_replicate_unit(bad.md_path, bad.duckdb_path, bad_store) == 'unresolved'
    assert replicates.dataset_replicate_unit(tmp_path / 'missing.md', None) == 'unresolved'


def _load_script(name: str):
    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parent.parent / 'bin' / 'annot' / name
    spec = importlib.util.spec_from_loader(name.replace('-', '_'),
                                           importlib.machinery.SourceFileLoader(name.replace('-', '_'), str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_module_variability_aggregate_carries_replicate_unit(tmp_path):
    amv = _load_script('ant-module-variability')
    cache = tmp_path / 'c.parquet'
    pd.DataFrame({'dataset': ['A/x', 'B/y'], 'source': 's', 'catalog': 'c', 'module': 'm', 'tag_column': 't',
                  'tag': 'sex', 'statistic': 'eta2', 'value': [0.1, 0.2], 'n_cells': [5, 6],
                  'n_categories': [2, 2]}).to_parquet(cache)
    out = tmp_path / 'out.parquet'
    amv._concat_and_write(tmp_path, out, [cache], None, {'A/x': 'donor'})
    table = pd.read_parquet(out)
    assert list(table.columns) == [*amv.OUTPUT_COLUMNS, 'replicate_unit']
    assert dict(zip(table['dataset'], table['replicate_unit'])) == {'A/x': 'donor', 'B/y': 'unresolved'}
    empty = tmp_path / 'empty.parquet'
    amv._concat_and_write(tmp_path, empty, [], None)
    assert 'replicate_unit' in pd.read_parquet(empty).columns


def test_gene_variance_aggregate_carries_replicate_unit(tmp_path):
    gvp = _load_script('ant-gene-variance-precompute')
    cache = tmp_path / 'c.parquet'
    frame = pd.DataFrame({column: [] for column in gvp.OUTPUT_COLUMNS}).astype(gvp.OUTPUT_DTYPES)
    frame.loc[0] = ['A/x', 'GENE', 'sex', 'categorical', 'X', 10, 2, 0.1, 1.0, 0.2, 0.3, '{}']
    frame.to_parquet(cache)
    out = tmp_path / 'out.parquet'
    n_rows, n_datasets, _, _ = gvp._concat_and_write(out, [(cache, 'A/x', 'fp')], {'A/x': 'animal'})
    table = pd.read_parquet(out)
    assert (n_rows, n_datasets) == (1, 1)
    assert table['replicate_unit'].tolist() == ['animal'] and table['src_fingerprint'].tolist() == ['fp']
    gvp._concat_and_write(out, [(cache, 'A/x', 'fp')], None)
    assert pd.read_parquet(out)['replicate_unit'].tolist() == ['unresolved']


# ── lint: disease_basis / disease_derivation ─────────────────────────────────

def _derivation():
    return {'rule': 'braak_cerad', 'column': 'disease_braak_cerad', 'braak': {'column': 'Braak'},
            'cerad': {'column': 'CERAD', 'map': {1: 'definite'}}}


def test_lint_disease_basis_rules():
    publishes = {'obs_rename': {'dx': 'ant_disease'}}
    assert [f.rule for f in facet_lint.lint_disease_basis({}, publishes)] == ['disease_basis_missing']
    assert facet_lint.lint_disease_basis({}, {}) == []                      # no ant_disease: nothing to declare
    assert facet_lint.lint_disease_basis({'disease_basis': 'author_dx'}, publishes) == []
    bad = facet_lint.lint_disease_basis({'disease_basis': 'vibes'}, publishes)
    assert [f.rule for f in bad] == ['disease_basis_invalid'] and bad[0].severity == 'error'
    good = {'disease_basis': 'braak_cerad', 'disease_derivation': _derivation()}
    assert facet_lint.lint_disease_basis(good, publishes) == []
    assert [f.rule for f in facet_lint.lint_disease_basis({'disease_basis': 'braak_cerad'}, publishes)] == [
        'disease_derivation_invalid']
    mismatch = facet_lint.lint_disease_basis({'disease_basis': 'adnc', 'disease_derivation': _derivation()}, publishes)
    assert [f.rule for f in mismatch] == ['disease_derivation_invalid']
    broken = {'disease_basis': 'braak_cerad', 'disease_derivation': {**_derivation(), 'rule': 'x'}}
    assert [f.rule for f in facet_lint.lint_disease_basis(broken, publishes)] == ['disease_derivation_invalid']


def test_summarize_replication_tables(tmp_path):
    store, _ = make_dataset(tmp_path)               # a store root with one (unrelated) dataset
    root = tmp_path / 'results'
    _write_unit(root / 'WA4', 'WA4/MIT', 'mit', 0.5, 0.01)
    _write_unit(root / '_pooled', 'pool:madrc', 'madrc', 0.6, 1e-4)
    _write_unit(root / 'G1', 'G1/ds', 'ds', 0.4, 1e-4)
    (root / fr.COHORT_TABLE_NAME).write_text(
        'status\tcohort\tcell_class\trole\tdb_id\tpool_members\tindependence\n'
        'DRAFT\tMADRC\tmyeloid\tanchor\tWA4/MIT\t\tchecked\n'
        'DRAFT\tMADRC\tmyeloid\tanchor (donor-pooled)\tpool:madrc\tWA4/MIT\tchecked\n'
        'DRAFT\tOther (x)\tmicroglia\tanchor\tG1/ds\t\tunknown\n')
    assert [d['db_id'] for d in aft.datasets_from_results(root, store)] == ['G1/ds', 'WA4/MIT']
    summary = aft.summarize_replication(root, store, tmp_path / 'out', ['g0', 'nope'], facets=['sex'])
    row = summary.iloc[0]
    assert (row['facet'], row['celltype_group'], row['n_cohorts'], row['replicated_r05']) == ('sex', 'microglia', 2, 1)
    assert row['replicated_up'] == 1 and row['genes_in_family'] == 12
    candidates = pd.read_csv(tmp_path / 'out' / 'candidates.tsv', sep='\t')
    assert candidates['gene'].tolist() == ['G0'] and bool(candidates['replicated'].iloc[0]) and candidates['k'].iloc[0] == 2
    units = pd.read_csv(tmp_path / 'out' / 'units.tsv', sep='\t')
    by_db = units.set_index('db_id')
    assert bool(by_db.loc['pool:madrc', 'anchor']) and not bool(by_db.loc['WA4/MIT', 'anchor'])
    assert (tmp_path / 'out' / 'replicated_sex_microglia.tsv').exists()
