"""Tests for ``bin/annot/ant-facet-test-modules`` (donor-level module facet tests).

Pure statistics:
* BH, OLS facet coefficient against an independent lstsq / scipy computation,
* permutation p value calibrated under the null (uniform, also with covariates
  and heavy-tailed noise) and powered under a planted effect, reproducible by seed,
* scanpy-style control genes (bin matched, module genes excluded).

End to end on a synthetic dataset (counts + scores sidecar + genesetstore):
* a planted module effect is recovered by variant A (cell means) and B
  (pseudobulk background-matched), with the right sign and size, and null
  modules stay mostly non-significant,
* A and B estimates correlate across modules (calibration record),
* datasets without counts run variant A only (no_counts),
* n_lt5 units are descriptive only,
* donors, strata and design rules are those of the gene tool.

Run with:
    uv run --with pytest --with duckdb --with pandas --with numpy \
           --with scipy --with pyarrow --with pyyaml --no-project \
           env PYTHONPATH=lib python -m pytest tests/test_facet_test_modules.py -q
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
import pytest
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))


def _load_tool():
    path = REPO_ROOT / 'bin' / 'annot' / 'ant-facet-test-modules'
    loader = importlib.machinery.SourceFileLoader('ant_facet_test_modules', str(path))
    spec = importlib.util.spec_from_loader('ant_facet_test_modules', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


mod = _load_tool()
aft = mod.aft


# ---------------------------------------------------------------------------
# Pure statistics
# ---------------------------------------------------------------------------

def test_benjamini_hochberg_known_values_and_nan():
    adjusted = mod.benjamini_hochberg(np.array([0.01, 0.04, 0.03, np.nan, 0.5]))
    # sorted p: .01 .03 .04 .5 -> .04, .04, .0533, .5 -> monotone
    assert np.isnan(adjusted[3])
    assert adjusted[0] == pytest.approx(0.04)
    assert adjusted[2] == pytest.approx(0.04 * 1.0, abs=1e-12) or adjusted[2] == pytest.approx(0.0533333, abs=1e-6)
    assert adjusted[4] == pytest.approx(0.5)
    assert (adjusted[~np.isnan(adjusted)] >= np.array([0.01, 0.04, 0.03, 0.5])).all()


def _toy_design(n_donors: int, rng: np.random.Generator, with_covariates: bool):
    facet = np.array([0.0] * (n_donors // 2) + [1.0] * (n_donors - n_donors // 2))
    columns = [np.ones(n_donors)]
    if with_covariates:
        columns.append(rng.integers(0, 2, n_donors).astype(float))      # sex
        columns.append(rng.normal(size=n_donors))                        # age z
    return facet, np.column_stack(columns)


def test_facet_effect_matches_lstsq_and_linregress():
    rng = np.random.default_rng(1)
    facet, nuisance = _toy_design(30, rng, with_covariates=True)
    scores = rng.normal(size=(30, 5)) + 0.7 * facet[:, None]
    result = mod.facet_effect_statistics(scores, facet, nuisance)
    full = np.column_stack([nuisance, facet])
    coefficients, _, _, _ = np.linalg.lstsq(full, scores, rcond=None)
    assert np.allclose(result['estimate'], coefficients[-1])
    residual = scores - full @ coefficients
    df = 30 - full.shape[1]
    covariance_factor = np.linalg.inv(full.T @ full)[-1, -1]
    se = np.sqrt((residual ** 2).sum(0) / df * covariance_factor)
    assert np.allclose(result['se'], se)
    assert np.allclose(result['t'], coefficients[-1] / se)
    # no covariates: identical to scipy linregress
    ones = np.ones((30, 1))
    simple = mod.facet_effect_statistics(scores, facet, ones)
    for column in range(scores.shape[1]):
        reference = stats.linregress(facet, scores[:, column])
        assert simple['estimate'][column] == pytest.approx(reference.slope)
        assert simple['p_param'][column] == pytest.approx(reference.pvalue)
    # CI is symmetric around the estimate with the t quantile
    quantile = stats.t.ppf(0.975, df)
    assert np.allclose(result['ci_high'] - result['estimate'], quantile * result['se'])


@pytest.mark.parametrize('with_covariates,heavy_tail', [(False, False), (True, False), (True, True)])
def test_permutation_p_is_calibrated_under_the_null(with_covariates, heavy_tail):
    rng = np.random.default_rng(20261004)
    n_donors, n_modules = 24, 800
    facet, nuisance = _toy_design(n_donors, rng, with_covariates)
    if heavy_tail:
        scores = rng.standard_t(3, size=(n_donors, n_modules))
    else:
        scores = rng.normal(size=(n_donors, n_modules))
    observed = mod.facet_effect_statistics(scores, facet, nuisance)
    p_perm, n_effective = mod.permutation_p_values(scores, facet, nuisance, observed['t'], 1000,
                                                    np.random.default_rng(5))
    assert n_effective == 1000
    for p_values, label in ((p_perm, 'perm'), (observed['p_param'], 'param')):
        rejection_rate = float((p_values < 0.05).mean())
        # binomial SE at 800 tests is 0.0077; allow ~4 SE (heavy tails make the t test itself anticonservative-free)
        assert 0.02 <= rejection_rate <= 0.085, (label, rejection_rate)
    ks = stats.kstest(p_perm, 'uniform')
    assert ks.pvalue > 0.001, ks
    # the permutation p tracks the parametric p for Gaussian data
    if not heavy_tail:
        assert stats.spearmanr(p_perm, observed['p_param'])[0] > 0.97


def test_permutation_p_detects_planted_effect_and_is_seed_reproducible():
    rng = np.random.default_rng(3)
    facet, nuisance = _toy_design(30, rng, with_covariates=True)
    scores = rng.normal(size=(30, 20))
    scores[:, 0] += 1.5 * facet
    observed = mod.facet_effect_statistics(scores, facet, nuisance)
    first, _ = mod.permutation_p_values(scores, facet, nuisance, observed['t'], 1000, np.random.default_rng(mod.unit_seed('a', 'b')))
    second, _ = mod.permutation_p_values(scores, facet, nuisance, observed['t'], 1000, np.random.default_rng(mod.unit_seed('a', 'b')))
    other, _ = mod.permutation_p_values(scores, facet, nuisance, observed['t'], 1000, np.random.default_rng(mod.unit_seed('a', 'c')))
    assert np.array_equal(first, second)
    assert not np.array_equal(first, other)
    assert first[0] < 0.005 and np.median(first[1:]) > 0.1 and first[0] < first[1:].min()


def test_control_genes_are_bin_matched_and_exclude_module_genes():
    rng = np.random.default_rng(0)
    gene_average = np.sort(rng.uniform(0, 10, 500))      # rank == position
    module_genes = np.array([10, 11, 250, 480])
    controls = mod.control_gene_indices(gene_average, module_genes, 25, 50, np.random.default_rng(1))
    assert not set(controls) & set(module_genes)
    n_items = int(np.round(500 / 24))
    bins = (np.arange(500) + 1) // n_items      # rank(method='min') is 1-based, as scanpy
    allowed_bins = set(bins[module_genes])
    assert set(bins[controls]) <= allowed_bins
    # deterministic for a given rng seed
    again = mod.control_gene_indices(gene_average, module_genes, 25, 50, np.random.default_rng(1))
    assert np.array_equal(controls, again)


def test_n_distinct_label_permutations():
    assert mod.n_distinct_label_permutations(np.array([0, 0, 1, 1])) == pytest.approx(6.0)
    assert mod.n_distinct_label_permutations(np.array([0.0, 1.0, 2.0])) == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# End to end on a synthetic dataset
# ---------------------------------------------------------------------------

N_GENES = 300
PLANT_GENES = list(range(10))                     # GENE0..GENE9 up in males, cell type T1
PLANT_LOG2_FC = 1.5
PLANT_SCORE_SHIFT = 0.8


def make_module_dataset(tmp_path: Path, n_f: int = 10, n_m: int = 10, with_counts: bool = True,
                        seed: int = 11):
    """Synthetic human dataset with counts, a scores sidecar and a genesetstore.

    Module PLANT (GENE0..9) is up in males in T1 (counts and scores). Modules
    NULL1..NULL5 are random gene sets without sex effect. T2 has 4 cells per donor
    (below the 10 cell minimum). Donor-level random effects are present in the scores.
    """
    rng = np.random.default_rng(seed)
    store = tmp_path / 'store'
    group_dir = store / 'Syn2026'
    group_dir.mkdir(parents=True)
    (group_dir / 'index.md').write_text('---\ngroup: Syn2026\ngroup_id: SY1\n---\n')
    stem = 'Syn_mod'
    donors = [f'D{i:02d}' for i in range(n_f + n_m)]
    sexes = ['F'] * n_f + ['M'] * n_m
    base_mean = rng.uniform(1.0, 12.0, size=N_GENES)
    base_mean[PLANT_GENES] = 3.0      # planted genes sit in mid bins, apart from the null modules' bins
    high_genes = np.flatnonzero(base_mean > 8.0)
    module_names = ['PLANT'] + [f'NULL{i}' for i in range(1, 6)]
    null_sets = {name: sorted(rng.choice(high_genes, size=15, replace=False).tolist())
                 for name in module_names[1:]}
    rows, count_triples, score_rows = [], [], []
    donor_offsets = {name: rng.normal(0, 0.3, size=len(donors)) for name in module_names}
    cell_id = 0
    for donor_index, (donor, sex) in enumerate(zip(donors, sexes)):
        donor_factor = np.exp(rng.normal(0, 0.15))
        for cell_type, n_cells in (('T1', 30), ('T2', 4)):
            for _ in range(n_cells):
                rows.append((cell_id, f'{donor}_c{cell_id}', donor, sex, cell_type))
                mean_vector = base_mean * donor_factor
                male_t1 = sex == 'M' and cell_type == 'T1'
                if male_t1:
                    mean_vector = mean_vector.copy()
                    mean_vector[PLANT_GENES] *= 2 ** PLANT_LOG2_FC
                counts = rng.poisson(mean_vector)
                for gene_id in np.nonzero(counts)[0]:
                    count_triples.append((cell_id, int(gene_id), float(counts[gene_id])))
                for name in module_names:
                    shift = PLANT_SCORE_SHIFT if (name == 'PLANT' and male_t1) else 0.0
                    score_rows.append((cell_id, 'geneset', 'SynStudy/Exp', name,
                                       float(rng.normal(0, 0.5) + donor_offsets[name][donor_index] + shift)))
                cell_id += 1
    cells = pd.DataFrame(rows, columns=['cell_id', 'cell_name', 'donor_id', 'Sex', 'celltype'])
    database_path = group_dir / f'{stem}.duckdb'
    conn = duckdb.connect(str(database_path))
    conn.execute('CREATE TABLE __cells AS SELECT cell_id, cell_name FROM cells')
    genes = pd.DataFrame({'gene_id': range(N_GENES), 'gene_name': [f'GENE{i}' for i in range(N_GENES)]})
    conn.execute('CREATE TABLE __genes AS SELECT * FROM genes')
    obscat = cells[['cell_id', 'donor_id', 'Sex', 'celltype']]
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat')
    conn.execute('CREATE TABLE __obsnum AS SELECT cell_id FROM cells')
    conn.close()
    pd.DataFrame(score_rows, columns=['cell_id', 'source', 'catalog', 'module', 'score']) \
        .to_parquet(group_dir / f'{stem}.scores.parquet', index=False)
    layer_stats = ''
    if with_counts:
        triples = pd.DataFrame(count_triples, columns=['cell_id', 'gene_id', 'value'])
        triples['cell_id'] = triples['cell_id'].astype('int32')
        triples['gene_id'] = triples['gene_id'].astype('int32')
        triples['value'] = triples['value'].astype('float32')
        triples.sort_values(['gene_id', 'cell_id']).to_parquet(
            group_dir / f'{stem}.layer.counts.by_gene.parquet', index=False)
        layer_stats = 'layer_stats:\n  counts:\n    min_nonzero: 1.0\n'
    (group_dir / f'{stem}.md').write_text(
        '---\nid: SYN\ntitle: Syn\nversion: 0.1.0\nsha256: deadbeef\nis_secondary: false\n' + layer_stats +
        'api:\n  obs_rename:\n    donor_id: ant_donor\n    Sex: ant_sex\n    celltype: ant_author_cell_type\n'
        '  obs_value_rename:\n    Sex:\n      F: f\n      M: m\n---\n')
    (group_dir / f'{stem}.h5ad.mus').write_text('organism=human\nexperimental_system=post_mortem_tissue\n')
    # genesetstore
    geneset_root = tmp_path / 'genesetstore'
    experiment_dir = geneset_root / 'SynStudy' / 'Exp'
    experiment_dir.mkdir(parents=True)
    (experiment_dir / 'index.md').write_text(
        '---\nstudy: SynStudy\nexperiment: Exp\norganism: human\nmethod: manual\ngene_id_type: symbol\n---\n')
    (experiment_dir / 'PLANT.grp').write_text('\n'.join(f'GENE{g}' for g in PLANT_GENES) + '\n')
    for name, gene_ids in null_sets.items():
        (experiment_dir / f'{name}.grp').write_text('\n'.join(f'GENE{g}' for g in gene_ids) + '\n')
    entry = aft.H5adEntry(db_id='SY1/MOD', md_path=group_dir / f'{stem}.md',
                          h5ad_path=group_dir / f'{stem}.h5ad', duckdb_path=database_path,
                          group_id='SY1', file_id='MOD', study='Syn2026', stem=stem)
    return store, geneset_root, entry


def _run(tmp_path, entry, store, geneset_root, facets=('sex',), **kwargs):
    registry = mod.GenesetRegistry(geneset_root)
    registry.scan()
    assert not registry.errors, registry.errors
    out_root = tmp_path / 'out'
    manifest = mod.process_dataset(entry, store, out_root, list(facets), registry, ['geneset'],
                                   n_permutations=kwargs.pop('n_permutations', 300), threads=1,
                                   memory_limit_gb=2, **kwargs)
    return manifest, out_root


def test_planted_module_effect_recovered_by_both_methods(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'done' and manifest['no_counts'] is False
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    sex_t1 = table[(table['facet'] == 'sex') & (table['cell_type'] == 'T1')]
    assert set(sex_t1['method']) == {'cell_mean', 'pseudobulk'}
    assert (sex_t1['contrast'] == 'm_vs_f').all() and (sex_t1['n_donors'] == 20).all()
    assert not (table['cell_type'] == 'T2').any()                      # 4 cells per donor: dropped
    plant_a = sex_t1[(sex_t1['module'] == 'PLANT') & (sex_t1['method'] == 'cell_mean')].iloc[0]
    assert plant_a['estimate'] == pytest.approx(PLANT_SCORE_SHIFT, abs=0.3)
    assert plant_a['ci_low'] < PLANT_SCORE_SHIFT < plant_a['ci_high']
    assert plant_a['q_param'] < 0.05 and plant_a['q_perm'] < 0.05
    plant_b = sex_t1[(sex_t1['module'] == 'PLANT') & (sex_t1['method'] == 'pseudobulk')].iloc[0]
    assert 0.8 < plant_b['estimate'] < PLANT_LOG2_FC + 0.2             # log2 CPM, mildly attenuated by CPM closure
    assert plant_b['q_param'] < 0.05 and plant_b['q_perm'] < 0.05
    assert plant_b['n_genes_found'] == 10 and plant_b['n_control_genes'] > 10
    for method in ('cell_mean', 'pseudobulk'):
        nulls = sex_t1[(sex_t1['module'] != 'PLANT') & (sex_t1['method'] == method)]
        assert len(nulls) == 5
        assert (nulls['q_perm'] < 0.05).sum() <= 1, nulls[['module', 'estimate', 'p_perm']]
    unit = next(u for u in manifest['units'] if u['facet'] == 'sex' and u['cell_type'] == 'T1')
    assert unit['n_sig_q05_param_cell_mean'] >= 1 and unit['n_sig_q05_param_pseudobulk'] >= 1
    assert unit['calibration']['n_modules_shared'] == 6
    assert unit['calibration']['pearson_estimate'] > 0.5
    assert unit['calibration']['sign_agreement'] >= 0.5
    # manifest on disk and reproducibility fields
    on_disk = json.loads((out_root / 'Syn2026' / 'Syn_mod.facet_module_run_manifest.json').read_text())
    assert on_disk['seed'] == mod.SEED and on_disk['parameters']['n_bins'] == 25 \
        and on_disk['parameters']['ctrl_size'] == 50 and 'numpy' in on_disk['versions']
    assert on_disk['catalogs']['geneset::SynStudy/Exp']['n_modules_sidecar'] == 6


def test_results_are_reproducible_for_a_fixed_seed(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    _, out_first = _run(tmp_path, entry, store, geneset_root)
    first = pd.read_parquet(out_first / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    shutil_out = tmp_path / 'out'
    import shutil
    shutil.rmtree(shutil_out)
    _, out_second = _run(tmp_path, entry, store, geneset_root)
    second = pd.read_parquet(out_second / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    pd.testing.assert_frame_equal(first, second)


def test_dataset_without_counts_runs_variant_a_only(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path, with_counts=False)
    registry = mod.GenesetRegistry(geneset_root)
    registry.scan()
    md_yaml, _ = aft.parse_md_file(entry.md_path)
    assert aft.dataset_eligibility(entry, md_yaml, {'organism': 'human', 'experimental_system': 'post_mortem_tissue'},
                                   aft.get_api_config(md_yaml)) == (False, 'no raw counts layer served')
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'done' and manifest['no_counts'] is True
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert set(table['method']) == {'cell_mean'} and table['no_counts'].all()
    plant = table[(table['module'] == 'PLANT') & (table['cell_type'] == 'T1')].iloc[0]
    assert plant['q_param'] < 0.05


def test_n_lt5_is_descriptive_only(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path, n_f=4, n_m=8)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert table['n_lt5'].all() and table['descriptive_only'].all()
    for column in ('p_param', 'q_param', 'p_perm', 'q_perm', 't', 'ci_low', 'ci_high'):
        assert table[column].isna().all(), column
    assert table['estimate'].notna().all() and table['se'].notna().all()


def test_unsupported_datasets_are_skipped(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    (store / 'Syn2026' / 'Syn_mod.h5ad.mus').write_text('organism=mouse\nexperimental_system=in_vivo\n')
    manifest, _ = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'skipped' and 'organism' in manifest['reason']
    (store / 'Syn2026' / 'Syn_mod.h5ad.mus').write_text('organism=human\nexperimental_system=post_mortem_tissue\n')
    (store / 'Syn2026' / 'Syn_mod.scores.parquet').unlink()
    manifest, _ = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'skipped' and 'scores' in manifest['reason']
