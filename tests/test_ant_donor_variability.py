"""Tests for ``bin/annot/ant-donor-variability`` on a small synthetic dataset built like ant-convert output.

Planted truths: genes 0 to 9 differ between donors (SD 0.8 log2), genes 10 to 19 differ by sex (1.5 log2) and a gene set
holds donor offsets in its scores. Everything else has no donor variance beyond counting noise.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy --with scipy --with pyarrow --with pyyaml \
           --no-project env PYTHONPATH=lib python -m pytest tests/test_ant_donor_variability.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))


def _load_tool():
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-donor-variability'
    loader = importlib.machinery.SourceFileLoader('ant_donor_variability', str(path))
    spec = importlib.util.spec_from_loader('ant_donor_variability', loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules['ant_donor_variability'] = module
    loader.exec_module(module)
    return module


tool = _load_tool()
aft = tool.aft
dv = tool.dv

N_GENES = 300
DONOR_GENES = list(range(10))
SEX_GENES = list(range(10, 20))
DONOR_SD = 0.8
SEX_LOG2_FC = 1.5


def make_dataset(tmp_path: Path, n_donors: int = 40, cells_per_donor: int = 60, seed: int = 5, with_counts: bool = True,
                 extra_md: str = '', systems: str = 'post_mortem_tissue', organism: str = 'human'):
    """Synthetic dataset with counts, scores and a genesetstore; returns (store, geneset_root, entry)."""
    rng = np.random.default_rng(seed)
    store = tmp_path / 'store'
    group_dir = store / 'Syn2026'
    group_dir.mkdir(parents=True)
    (group_dir / 'index.md').write_text('---\ngroup: Syn2026\ngroup_id: SY1\n---\n')
    stem = 'Syn_var'
    donors = [f'D{i:02d}' for i in range(n_donors)]
    sexes = np.array(['F', 'M'] * (n_donors // 2) + ['F'] * (n_donors % 2))
    base_mean = rng.uniform(1.0, 8.0, size=N_GENES)
    donor_effect = np.zeros((n_donors, N_GENES))
    donor_effect[:, DONOR_GENES] = rng.normal(0, DONOR_SD, (n_donors, len(DONOR_GENES)))
    score_offsets = rng.normal(0, 0.5, n_donors)
    cell_rows, triples, score_rows = [], [], []
    cell_id = 0
    for index, (donor, sex) in enumerate(zip(donors, sexes)):
        mean_vector = base_mean * 2.0 ** donor_effect[index]
        if sex == 'M':
            mean_vector = mean_vector.copy()
            mean_vector[SEX_GENES] *= 2 ** SEX_LOG2_FC
        for _ in range(cells_per_donor):
            cell_rows.append((cell_id, f'{donor}_c{cell_id}', donor, sex, 'Microglia'))
            counts = rng.poisson(mean_vector * rng.gamma(4.0, 1 / 4.0))
            for gene_id in np.nonzero(counts)[0]:
                triples.append((cell_id, int(gene_id), float(counts[gene_id])))
            score_rows.append((cell_id, 'geneset', 'SynStudy/Exp', 'DONORSET', float(rng.normal(0, 0.5) + score_offsets[index])))
            score_rows.append((cell_id, 'geneset', 'SynStudy/Exp', 'NOISESET', float(rng.normal(0, 0.5))))
            cell_id += 1
    cells = pd.DataFrame(cell_rows, columns=['cell_id', 'cell_name', 'donor_id', 'Sex', 'celltype'])
    database_path = group_dir / f'{stem}.duckdb'
    conn = duckdb.connect(str(database_path))
    conn.execute('CREATE TABLE __cells AS SELECT cell_id, cell_name FROM cells')
    genes = pd.DataFrame({'gene_id': range(N_GENES), 'gene_name': [f'GENE{i}' for i in range(N_GENES)]})
    conn.execute('CREATE TABLE __genes AS SELECT * FROM genes')
    obscat = cells[['cell_id', 'donor_id', 'Sex', 'celltype']]
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat')
    conn.execute('CREATE TABLE __obsnum AS SELECT cell_id FROM cells')
    conn.close()
    pd.DataFrame(score_rows, columns=['cell_id', 'source', 'catalog', 'module', 'score']).to_parquet(
        group_dir / f'{stem}.scores.parquet', index=False)
    layer_stats = ''
    if with_counts:
        frame = pd.DataFrame(triples, columns=['cell_id', 'gene_id', 'value'])
        frame['cell_id'], frame['gene_id'] = frame['cell_id'].astype('int32'), frame['gene_id'].astype('int32')
        frame['value'] = frame['value'].astype('float32')
        frame.sort_values(['gene_id', 'cell_id']).to_parquet(group_dir / f'{stem}.layer.counts.by_gene.parquet', index=False)
        layer_stats = 'layer_stats:\n  counts:\n    min_nonzero: 1.0\n'
    (group_dir / f'{stem}.md').write_text(
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\nis_secondary: false\n' + layer_stats +
        'api:\n  obs_rename:\n    donor_id: ant_donor\n    Sex: ant_sex\n    celltype: ant_cell_type\n'
        '  obs_value_rename:\n    Sex:\n      F: f\n      M: m\n' + extra_md + '---\n')
    (group_dir / f'{stem}.h5ad.mus').write_text(f'organism={organism}\nexperimental_system={systems}\n')
    geneset_root = tmp_path / 'genesetstore'
    experiment_dir = geneset_root / 'SynStudy' / 'Exp'
    experiment_dir.mkdir(parents=True)
    (experiment_dir / 'index.md').write_text(
        '---\nstudy: SynStudy\nexperiment: Exp\norganism: human\nmethod: manual\ngene_id_type: symbol\ncelltypes:\n- all\n---\n')
    (experiment_dir / 'DONORSET.grp').write_text('\n'.join(f'GENE{g}' for g in range(20, 50)) + '\n')
    (experiment_dir / 'NOISESET.grp').write_text('\n'.join(f'GENE{g}' for g in range(60, 90)) + '\n')
    entry = aft.H5adEntry(db_id='SY1/VAR', md_path=group_dir / f'{stem}.md', h5ad_path=group_dir / f'{stem}.h5ad',
                          duckdb_path=database_path, group_id='SY1', file_id='VAR', study='Syn2026', stem=stem)
    return store, geneset_root, entry


def run(tmp_path, entry, store, geneset_root, modules=True, **kwargs):
    registry = None
    if modules:
        registry = tool.GenesetRegistry(geneset_root)
        registry.scan()
        assert not registry.errors, registry.errors
    out_root = tmp_path / 'out'
    manifest = tool.process_dataset(entry, store, out_root, registry, n_boot=kwargs.pop('n_boot', 40), threads=1,
                                    memory_limit_gb=2, run_modules=modules, **kwargs)
    return manifest, out_root


@pytest.fixture(scope='module')
def planted(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp('planted')
    store, geneset_root, entry = make_dataset(tmp_path)
    manifest, out_root = run(tmp_path, entry, store, geneset_root)
    return manifest, out_root, (tmp_path, store, geneset_root, entry)


def gene_table(out_root) -> pd.DataFrame:
    return pd.read_parquet(out_root / 'Syn2026' / 'Syn_var.donor_variability.parquet').set_index('gene')


def test_planted_donor_variance_and_sex_effect_are_recovered(planted):
    manifest, out_root, _ = planted
    assert manifest['status'] == 'done' and manifest['n_strata_analysed'] == 1
    table = gene_table(out_root)
    donor_genes = table.loc[[f'GENE{g}' for g in DONOR_GENES]]
    others = table.drop([f'GENE{g}' for g in DONOR_GENES + SEX_GENES])
    assert (donor_genes['reliability'] > 0.85).all()
    assert donor_genes['sd_donor'].mean() == pytest.approx(DONOR_SD, abs=0.2)
    assert others['reliability'].median() < 0.4
    assert others['v_donor'].median() < 0.02
    sex_genes = table.loc[[f'GENE{g}' for g in SEX_GENES]]
    assert sex_genes['reliability'].median() > 0.9
    assert sex_genes['share_design_D1'].median() > 0.8                 # sex is the donor variance of these genes
    assert sex_genes['v_unexplained_D1'].median() < 0.1 * sex_genes['v_donor'].median()
    assert donor_genes['share_design_D1'].median() < 0.3               # nothing known explains the planted donor effect
    assert (table['n_replicates'] == 40).all() and (table['rung_deepest'] == 'D1').all()
    assert (table['facet_scope'] == 'human_postmortem').all() and (table['replicate_unit'] == 'donor').all()


def test_donor_effect_genes_top_the_excess_ranking(planted):
    _, out_root, _ = planted
    table = gene_table(out_root).sort_values('excess_z_D1', ascending=False)
    assert len(set(table.index[:15]) & {f'GENE{g}' for g in DONOR_GENES}) >= 8
    assert table.loc[[f'GENE{g}' for g in DONOR_GENES], 'p_excess_D1'].median() < 0.15


def test_manifest_records_method_gates_terms_and_versions(planted):
    manifest, out_root, (_, _, _, entry) = planted
    assert manifest['tool'] == tool.TOOL_VERSION and manifest['seed'] == tool.SEED
    assert manifest['md_sha256'] == aft.md_file_sha256(entry.md_path)
    parameters = manifest['parameters']
    assert parameters['min_replicates'] == 12 and parameters['min_cells_per_replicate'] == 40
    assert parameters['share_min_residual_df'] == 20 and parameters['term_share_min_residual_df'] == 30
    assert parameters['pairings'] == [[[0, 1], [2, 3]], [[0, 2], [1, 3]], [[0, 3], [1, 2]]]
    assert 'numpy' in manifest['versions'] and manifest['replicate_unit'] == 'donor'
    unit = manifest['units'][0]
    assert unit['terms'] == ['technical', 'sex'] and unit['technical_covariates'][0] == 'log_umi_per_cell'
    assert 'log_n_cells' not in unit['technical_covariates']            # 60 cells for every donor: constant, dropped
    assert 'long_gene_index' not in unit['technical_covariates']       # no GTF annotation for GENE0..: index is left out
    assert unit['terms_omitted'].keys() >= {'age', 'pmi'}
    rungs = {info['name']: info for info in unit['rungs']}
    assert rungs['D0']['computed'] and rungs['D1']['computed'] and not rungs['D2']['computed']
    assert rungs['F']['computed'] and unit['n_factors'] == 3
    on_disk = json.loads((out_root / 'Syn2026' / 'Syn_var.donor_variability_manifest.json').read_text())
    assert on_disk['units'][0]['n_replicates'] == 40


def test_genes_file_is_sorted_with_small_row_groups_and_float32(planted):
    _, out_root, _ = planted
    path = out_root / 'Syn2026' / 'Syn_var.donor_variability.parquet'
    schema = pq.read_schema(path)
    assert str(schema.field('v_donor').type) == 'float'
    genes = pd.read_parquet(path, columns=['gene'])['gene']
    assert genes.is_monotonic_increasing


def test_modules_both_variants_and_scope(planted):
    manifest, out_root, _ = planted
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_var.donor_variability_modules.parquet')
    assert set(table['method']) == {'pseudobulk', 'cell_mean'}
    cell_mean = table[table['method'] == 'cell_mean'].set_index('module')
    # the planted offsets (SD 0.5 per donor, noise SD 0.5 per cell over 15 cells per quarter)
    assert cell_mean.loc['DONORSET', 'reliability'] > 0.8
    assert cell_mean.loc['DONORSET', 'sd_donor'] == pytest.approx(0.5, abs=0.15)
    assert cell_mean.loc['NOISESET', 'reliability'] < 0.5
    pseudobulk = table[table['method'] == 'pseudobulk']
    assert set(pseudobulk['source']) == {'gene_list'} and (pseudobulk['n_genes_found'] == 30).all()
    assert (pseudobulk['n_control_genes'] > 30).all()
    module_info = manifest['units'][0]['modules']
    assert module_info['pseudobulk']['n_modules'] == 2 and module_info['cell_mean']['n_modules'] == 2


def test_results_are_reproducible_and_seed_changes_the_quarters(planted, tmp_path):
    _, out_root, (_, store, geneset_root, entry) = planted
    first = gene_table(out_root)
    again_root = tmp_path / 'again'
    tool.process_dataset(entry, store, again_root, None, n_boot=40, threads=1, memory_limit_gb=2, run_modules=False)
    second = pd.read_parquet(again_root / 'Syn2026' / 'Syn_var.donor_variability.parquet').set_index('gene')
    pd.testing.assert_frame_equal(first[['v_donor', 'reliability', 'v_donor_lo', 'excess_z_D1']],
                                  second[['v_donor', 'reliability', 'v_donor_lo', 'excess_z_D1']])
    other_root = tmp_path / 'other'
    tool.process_dataset(entry, store, other_root, None, seed=77, n_boot=40, threads=1, memory_limit_gb=2, run_modules=False)
    third = pd.read_parquet(other_root / 'Syn2026' / 'Syn_var.donor_variability.parquet').set_index('gene')
    assert not np.allclose(first['v_donor'], third['v_donor'])
    assert np.corrcoef(first['v_donor'], third['v_donor'])[0, 1] > 0.9


def test_sensitivity_run_marks_its_files_and_keeps_more_replicates(tmp_path):
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=20, cells_per_donor=30)
    manifest, _ = run(tmp_path, entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'done' and manifest['n_strata_analysed'] == 0      # 30 cells < 40
    manifest, out_root = run(tmp_path, entry, store, geneset_root, modules=False, min_cells=20)
    assert manifest['n_strata_analysed'] == 1
    assert (out_root / 'Syn2026' / 'Syn_var.min20.donor_variability.parquet').exists()
    assert tool.result_stem('Syn_var', 20) == 'Syn_var.min20' and tool.result_stem('Syn_var', 40) == 'Syn_var'


@pytest.mark.parametrize('kwargs, reason', [
    ({'systems': 'in_vitro_2d'}, 'experimental_system'),
    ({'organism': 'mouse'}, 'mouse-only'),
    ({'extra_md': 'facet_exclude: true\nfacet_exclude_reason: metacells\n'}, 'metacells'),
])
def test_unsupported_datasets_are_skipped(tmp_path, kwargs, reason):
    extra = kwargs.get('extra_md', '')
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=14, **{k: v for k, v in kwargs.items() if k != 'extra_md'})
    if extra:                       # top-level keys go before the api block
        text = entry.md_path.read_text().replace('api:', extra + 'api:', 1)
        entry.md_path.write_text(text)
    manifest, out_root = run(tmp_path, entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'skipped' and reason in manifest['reason']
    assert not list((out_root / 'Syn2026').glob('*.parquet')) if (out_root / 'Syn2026').exists() else True


def test_dataset_without_counts_is_skipped_and_few_replicates_too(tmp_path):
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=14, with_counts=False)
    manifest, _ = run(tmp_path, entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'skipped' and 'counts' in manifest['reason']
    store, geneset_root, entry = make_dataset(tmp_path / 'few', n_donors=10)
    manifest, _ = run(tmp_path / 'few', entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'skipped' and '10 replicates' in manifest['reason']


def test_donor_exclusion_rule_of_the_md_is_applied(tmp_path):
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=30)
    rule = 'facet_donor_exclusions:\n  reason: test overlap\n  obs:\n    ant_donor: [D00, D01]\n'
    entry.md_path.write_text(entry.md_path.read_text().replace('api:', rule + 'api:', 1))
    manifest, out_root = run(tmp_path, entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'done' and 'donor_exclusion' in manifest
    assert gene_table(out_root)['n_replicates'].iloc[0] == 28


def test_a_failing_stratum_is_recorded_and_does_not_stop_the_dataset(tmp_path, monkeypatch):
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=14)

    def boom(*args, **kwargs):
        raise RuntimeError('planted failure')

    monkeypatch.setattr(tool, 'analyse_stratum', boom)
    manifest, out_root = run(tmp_path, entry, store, geneset_root, modules=False)
    assert manifest['status'] == 'done' and manifest['n_gene_rows'] == 0
    assert manifest['units'][0]['status'] == 'error' and 'planted failure' in manifest['units'][0]['reason']
    assert not (out_root / 'Syn2026' / 'Syn_var.donor_variability.parquet').exists()


def test_command_line_run_scans_the_genesetstore_and_skips_current_results(tmp_path, capsys):
    store, geneset_root, entry = make_dataset(tmp_path, n_donors=16)
    out_root = tmp_path / 'cli_out'
    arguments = ['run', '--h5adstore-root', str(store), '--out-root', str(out_root), '--genesetstore', str(geneset_root),
                 '--db-id', 'SY1/SYN', '--n-boot', '20', '--threads', '1', '--memory-gb', '2', '--max-load', '1000']
    tool.cmd_run(tool.build_parser().parse_args(arguments))
    assert '[done] SY1/SYN: 1 strata' in capsys.readouterr().out
    modules = pd.read_parquet(out_root / 'Syn2026' / 'Syn_var.donor_variability_modules.parquet')
    assert set(modules['method']) == {'pseudobulk', 'cell_mean'}        # the registry was scanned: variant B exists
    tool.cmd_run(tool.build_parser().parse_args(['run', '--h5adstore-root', str(store), '--out-root', str(out_root),
                                                  '--genesetstore', str(geneset_root), '--all', '--no-modules',
                                                  '--max-load', '1000']))
    assert '[skip existing] SY1/SYN' in capsys.readouterr().out
    tool.cmd_run(tool.build_parser().parse_args(['run', '--h5adstore-root', str(store), '--db-id', 'NO/SUCH',
                                                  '--out-root', str(out_root), '--no-modules']))
    assert 'unknown db_id NO/SUCH' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        tool.cmd_run(tool.build_parser().parse_args(['run', '--h5adstore-root', str(store), '--out-root', str(out_root)]))


# ---- pure helpers ---------------------------------------------------------------------------------------------------

def test_unit_seed_is_deterministic_and_key_dependent():
    assert tool.unit_seed('a', 'b') == tool.unit_seed('a', 'b')
    assert tool.unit_seed('a', 'b') != tool.unit_seed('a', 'c')
    assert tool.unit_seed('a', base=1) != tool.unit_seed('a', base=2)


def test_long_gene_index_needs_annotation_and_orders_by_span():
    expression = np.tile(np.arange(1200, dtype=float), (3, 1))
    spans = np.arange(1200, dtype=float) * 1000
    index = tool.long_gene_index(expression, spans, n_each=100)
    assert index == pytest.approx(np.full(3, expression[0, -100:].mean() - expression[0, :100].mean()))
    assert tool.long_gene_index(expression, np.full(1200, np.nan)) is None
    assert tool.long_gene_index(expression[:, :150], spans[:150], n_each=100) is None


def test_ambient_score_and_empty_panel():
    counts = np.array([[10.0, 0.0], [0.0, 0.0]])
    score = tool.ambient_score(counts, np.array([1000.0, 1000.0]))
    assert score[0] == pytest.approx((np.log2(10001.0) + 0.0) / 2) and score[1] == 0.0
    assert np.isnan(tool.ambient_score(np.zeros((2, 0)), np.array([1.0, 1.0]))).all()


def test_composition_matrix_rows_sum_to_one_and_needs_clusters():
    cells = pd.DataFrame({'donor': ['a', 'a', 'a', 'b', 'b', 'c'], 'cluster': ['x', 'y', 'x', 'y', 'y', 'x']})
    table = tool.composition_matrix(cells, ['a', 'b', 'c'], 'cluster')
    assert table.sum(axis=1).tolist() == [1.0, 1.0, 1.0] and table.loc['a', 'x'] == pytest.approx(2 / 3)
    assert tool.composition_matrix(cells.assign(cluster='x'), ['a', 'b'], 'cluster') is None


def test_float32_frame_only_narrows_floats():
    frame = pd.DataFrame({'a': [1.5], 'b': [2], 'c': ['x']})
    out = tool.float32_frame(frame)
    assert out['a'].dtype == np.float32 and out['b'].dtype == frame['b'].dtype and out['c'][0] == 'x'


def test_manifest_is_current_checks_md_hash_and_tool_version(tmp_path):
    md = tmp_path / 'x.md'
    md.write_text('---\nid: x\n---\n')
    manifest = tmp_path / 'm.json'
    manifest.write_text(json.dumps({'md_sha256': aft.md_file_sha256(md), 'tool': tool.TOOL_VERSION}))
    assert tool.manifest_is_current(manifest, md)
    md.write_text('---\nid: y\n---\n')
    assert not tool.manifest_is_current(manifest, md)
    assert not tool.manifest_is_current(tmp_path / 'missing.json', md)
    manifest.write_text(json.dumps({'md_sha256': aft.md_file_sha256(md), 'tool': 'old'}))
    assert not tool.manifest_is_current(manifest, md)
