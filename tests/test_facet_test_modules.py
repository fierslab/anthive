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
* n_lt5 units are tested and flagged small_sample (v3),
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


def test_default_number_of_permutations_is_ten_thousand():
    import inspect
    assert mod.N_PERMUTATIONS == 10000
    assert inspect.signature(mod.fit_modules).parameters['n_permutations'].default == 10000


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
                        seed: int = 11, extra_md: str = '', experiment_scope: str = 'celltypes:\n- all\n',
                        celltype_names=('T1', 'T2')):
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
        for cell_type, n_cells in ((celltype_names[0], 30), (celltype_names[1], 4)):
            for _ in range(n_cells):
                rows.append((cell_id, f'{donor}_c{cell_id}', donor, sex, cell_type))
                mean_vector = base_mean * donor_factor
                male_t1 = sex == 'M' and cell_type == celltype_names[0]
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
    cells['bank'] = np.where(cells['donor_id'].isin(['D00', 'D01', 'D12']), 'R', 'M')
    obscat = cells[['cell_id', 'donor_id', 'Sex', 'celltype', 'bank']]
    conn.execute('CREATE TABLE __obscat AS SELECT * FROM obscat')
    conn.execute('CREATE TABLE __obsnum AS SELECT cell_id FROM cells')
    conn.close()
    score_frame = pd.DataFrame(score_rows, columns=['cell_id', 'source', 'catalog', 'module', 'score'])
    ucell_frame = score_frame.assign(source='ucell', score=lambda f: 1 / (1 + np.exp(-f['score'])))
    pd.concat([score_frame, ucell_frame]).to_parquet(group_dir / f'{stem}.scores.parquet', index=False)
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
        '  obs_value_rename:\n    Sex:\n      F: f\n      M: m\n' + extra_md + '---\n')
    (group_dir / f'{stem}.h5ad.mus').write_text('organism=human\nexperimental_system=post_mortem_tissue\n')
    # genesetstore
    geneset_root = tmp_path / 'genesetstore'
    experiment_dir = geneset_root / 'SynStudy' / 'Exp'
    experiment_dir.mkdir(parents=True)
    (experiment_dir / 'index.md').write_text(
        '---\nstudy: SynStudy\nexperiment: Exp\norganism: human\nmethod: manual\ngene_id_type: symbol\n'
        + experiment_scope + '---\n')
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
    manifest = mod.process_dataset(entry, store, out_root, list(facets), registry,
                                   kwargs.pop('sources', ['geneset']),
                                   n_permutations=kwargs.pop('n_permutations', 300), threads=1,
                                   memory_limit_gb=2, **kwargs)
    return manifest, out_root


def test_planted_module_effect_recovered_by_both_methods(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'done' and manifest['no_counts'] is False
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    sex_t1 = table[(table['facet'] == 'sex') & (table['cell_type'] == 'T1')]
    assert set(sex_t1['method']) == {'cell_mean', 'pseudobulk'} and set(sex_t1['source']) == {'geneset', 'gene_list'}
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
    assert unit['n_sig_q05_param_cell_mean_geneset'] >= 1 and unit['n_sig_q05_param_pseudobulk_gene_list'] >= 1
    # donor keys: no ant_donor_uid here, so every donor is keyed SY1:<donor> and stored as a digest
    assert manifest['donor_keys']['n_donors_uid'] == 0 and manifest['donor_keys']['n_donors'] == 20
    assert len(unit['donor_key_digests']) == 20 and all(len(d) == 16 for d in unit['donor_key_digests'])
    calibration = unit['calibration']['geneset']
    assert calibration['n_modules_shared'] == 6
    assert calibration['pearson_estimate'] > 0.5
    assert calibration['sign_agreement'] >= 0.5
    # manifest on disk and reproducibility fields
    on_disk = json.loads((out_root / 'Syn2026' / 'Syn_mod.facet_module_run_manifest.json').read_text())
    assert on_disk['seed'] == mod.SEED and on_disk['parameters']['n_bins'] == 25 \
        and on_disk['parameters']['ctrl_size'] == 50 and 'numpy' in on_disk['versions']
    assert on_disk['catalogs']['geneset::SynStudy/Exp']['n_modules_sidecar'] == 6


def test_sample_sidecars_hold_the_scores_the_tests_fitted_and_no_donor_ids(tmp_path):
    """The stored replicate scores reproduce the stored estimate (the module test is an unweighted OLS) and
    ``--samples-only`` rewrites them without touching the results."""
    from facet_donor_values import DonorValues, _covariate_matrix
    store, geneset_root, entry = make_module_dataset(tmp_path)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root, sources=['geneset', 'ucell'])
    folder = out_root / 'Syn2026'
    samples_path = folder / 'Syn_mod.facet_module_samples.parquet'
    scores_path = folder / 'Syn_mod.facet_module_sample_scores.parquet'
    assert manifest['sample_sidecar_rows'] == 20
    samples, scores = pd.read_parquet(samples_path), pd.read_parquet(scores_path)
    assert not {'donor', 'ant_donor'} & set(samples.columns)
    for path in (samples_path, scores_path):
        assert b'D00' not in path.read_bytes()
    table = pd.read_parquet(folder / 'Syn_mod.facet_module_tests.parquet')
    unit = samples[(samples['facet'] == 'sex') & (samples['cell_type'] == 'T1')]
    covariates, _ = _covariate_matrix(unit)
    design = np.column_stack([np.ones(len(unit)), unit['facet_value'].to_numpy(), covariates])
    for method, source in (('cell_mean', 'geneset'), ('cell_mean', 'ucell'), ('pseudobulk', 'gene_list')):
        stored = table[(table['facet'] == 'sex') & (table['cell_type'] == 'T1') & (table['method'] == method)
                       & (table['source'] == source) & (table['module'] == 'PLANT')].iloc[0]
        answer = DonorValues(out_root).module_unit('Syn2026', 'Syn_mod', 'sex', 'T1', stored['celltype_source'],
                                                   'SynStudy/Exp', 'PLANT', method, source, ('f', 'm'))
        assert answer['available'] and answer['n_donors'] == 20
        coefficient = np.linalg.lstsq(design, scores[(scores['method'] == method) & (scores['source'] == source)
                                                     & (scores['module'] == 'PLANT')]
                                      .set_index('sample_idx')['score'].reindex(unit['sample_idx']).to_numpy(),
                                      rcond=None)[0][1]
        assert coefficient == pytest.approx(stored['estimate'], abs=1e-9)
    # backfill: same sidecars, results untouched; a changed .md is refused
    results_stamp = (folder / 'Syn_mod.facet_module_tests.parquet').stat().st_mtime_ns
    samples_path.unlink(), scores_path.unlink()
    registry = mod.GenesetRegistry(geneset_root)
    registry.scan()
    backfill = mod.process_dataset(entry, store, out_root, ['sex'], registry, ['geneset', 'ucell'], n_permutations=300,
                                   threads=1, memory_limit_gb=2, samples_only=True)
    assert backfill['status'] == 'samples_only'
    pd.testing.assert_frame_equal(pd.read_parquet(samples_path), samples)
    pd.testing.assert_frame_equal(pd.read_parquet(scores_path), scores)
    assert (folder / 'Syn_mod.facet_module_tests.parquet').stat().st_mtime_ns == results_stamp
    samples_path.unlink(), scores_path.unlink()
    entry.md_path.write_text(entry.md_path.read_text() + '\n')
    refused = mod.process_dataset(entry, store, out_root, ['sex'], registry, ['geneset', 'ucell'], n_permutations=300,
                                  threads=1, memory_limit_gb=2, samples_only=True)
    assert refused['status'] == 'skipped' and 'another .md' in refused['reason']
    assert not samples_path.exists()


def test_both_sidecar_sources_are_separate_bh_families_and_b_is_not_duplicated(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root, sources=['geneset', 'ucell'])
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    sex_t1 = table[(table['facet'] == 'sex') & (table['cell_type'] == 'T1')]
    counts = sex_t1.groupby(['method', 'source']).size().to_dict()
    assert counts == {('cell_mean', 'geneset'): 6, ('cell_mean', 'ucell'): 6, ('pseudobulk', 'gene_list'): 6}
    unit = next(u for u in manifest['units'] if u['facet'] == 'sex' and u['cell_type'] == 'T1')
    assert set(unit['calibration']) == {'geneset', 'ucell'}
    ucell_plant = sex_t1[(sex_t1['source'] == 'ucell') & (sex_t1['module'] == 'PLANT')].iloc[0]
    assert ucell_plant['estimate'] > 0 and ucell_plant['q_param'] < 0.05


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


def test_donor_exclusion_rule_applies_to_module_tests(tmp_path):
    rule = 'facet_donor_exclusions:\n  reason: shared persons\n  obs:\n    bank: [R]\n'
    store, geneset_root, entry = make_module_dataset(tmp_path, extra_md=rule)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'done'
    exclusion = manifest['donor_exclusion']
    assert exclusion['counted'] is True
    assert (exclusion['n_donors_before'], exclusion['n_donors_excluded'], exclusion['n_donors_after']) == (20, 3, 17)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert set(table['n_donors']) == {17}
    full, out_full = _run(tmp_path, entry, store, geneset_root, all_donors=True)
    assert full['donor_exclusion']['counted'] is False and full['n_donors'] == 20
    supplementary = out_root / 'Syn2026' / 'supplementary_all_donors'
    assert set(pd.read_parquet(supplementary / 'Syn_mod.facet_module_tests.parquet')['n_donors']) == {20}
    assert set(pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')['n_donors']) == {17}


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
    assert set(table['source']) == {'geneset'}
    plant = table[(table['module'] == 'PLANT') & (table['cell_type'] == 'T1')].iloc[0]
    assert plant['q_param'] < 0.05


def test_units_below_the_donor_p_floor_are_descriptive(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path, n_f=4, n_m=8)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert table['n_lt5'].all() and table['small_sample'].all() and table['descriptive_only'].all()
    assert (table['facet_scope'] == 'human_postmortem').all() and manifest['facet_scope'] == 'human_postmortem'
    for column in ('p_param', 'q_param', 'p_perm', 'q_perm', 't', 'ci_low', 'ci_high'):
        assert table[column].isna().all(), column
    assert table['estimate'].notna().all() and table['se'].notna().all()


def test_units_at_the_donor_p_floor_report_p_values(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path, n_f=5, n_m=8)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert not table['descriptive_only'].any() and table['p_param'].notna().all() and table['p_perm'].notna().all()


def test_unsupported_datasets_are_skipped(tmp_path):
    store, geneset_root, entry = make_module_dataset(tmp_path)
    (store / 'Syn2026' / 'Syn_mod.h5ad.mus').write_text('organism=human\nexperimental_system=in_vitro_2d\n')
    manifest, _ = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'skipped' and 'experimental_system' in manifest['reason']
    (store / 'Syn2026' / 'Syn_mod.h5ad.mus').write_text('organism=human\nexperimental_system=post_mortem_tissue\n')
    (store / 'Syn2026' / 'Syn_mod.scores.parquet').unlink()
    manifest, _ = _run(tmp_path, entry, store, geneset_root)
    assert manifest['status'] == 'skipped' and 'scores' in manifest['reason']


# ---------------------------------------------------------------------------
# Cell type scope of modules (2026-10-08)
# ---------------------------------------------------------------------------

def _scoped_run(tmp_path, experiment_scope, overrides=None, label='Microglia'):
    """Run the synthetic dataset whose strata are called ``label`` (a microglia label) and ``Other``."""
    store, geneset_root, entry = make_module_dataset(tmp_path, experiment_scope=experiment_scope,
                                                     celltype_names=(label, 'Other'))
    for name, text in (overrides or {}).items():
        (geneset_root / 'SynStudy' / 'Exp' / f'{name}.md').write_text(text)
    manifest, out_root = _run(tmp_path, entry, store, geneset_root)
    result = out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet'
    if not result.exists():                       # no module of the dataset is in scope of any stratum
        return manifest, pd.DataFrame(columns=['module', 'method'])
    table = pd.read_parquet(result)
    return manifest, table[(table['facet'] == 'sex') & (table['cell_type'] == label)]


@pytest.mark.parametrize('experiment_scope', ['celltypes:\n- microglial cell\n', 'celltypes:\n- all\n'])
def test_module_is_tested_in_a_stratum_of_its_cell_type_group_and_all_everywhere(tmp_path, experiment_scope):
    manifest, sex_stratum = _scoped_run(tmp_path, experiment_scope)
    assert len(sex_stratum[sex_stratum['method'] == 'cell_mean']) == 6
    assert len(sex_stratum[sex_stratum['method'] == 'pseudobulk']) == 6


def test_module_of_another_cell_type_group_is_not_tested_and_leaves_the_bh_family(tmp_path):
    manifest, sex_stratum = _scoped_run(tmp_path, 'celltypes:\n- astrocyte\n')
    assert sex_stratum.empty                                   # no module of the catalog is in scope
    manifest, sex_stratum = _scoped_run(
        tmp_path / 'override', 'celltypes:\n- microglial cell\n',
        overrides={'PLANT': '---\ncelltypes:\n- astrocyte\n---\n'})
    assert set(sex_stratum['module']) == {f'NULL{i}' for i in range(1, 6)}
    assert (sex_stratum.groupby('method').size() == 5).all()     # BH family of 5, PLANT is out of scope
    unit = next(u for u in manifest['units'] if u['facet'] == 'sex' and u['cell_type'] == 'Microglia')
    assert unit['n_modules_in_scope'] == 5 and unit['n_modules_out_of_scope'] == 1


def test_markers_role_module_is_never_tested(tmp_path):
    manifest, sex_stratum = _scoped_run(
        tmp_path, 'celltypes:\n- all\n', overrides={'PLANT': '---\nrole: markers\n---\n'})
    assert 'PLANT' not in set(sex_stratum['module'])
    assert (sex_stratum.groupby('method').size() == 5).all()


def test_unmapped_stratum_label_gets_only_all_scope_modules(tmp_path):
    _, sex_stratum = _scoped_run(tmp_path, 'celltypes:\n- microglial cell\n', label='T1')
    assert sex_stratum.empty
    _, sex_stratum = _scoped_run(tmp_path / 'all', 'celltypes:\n- all\n', label='T1')
    assert len(sex_stratum[sex_stratum['method'] == 'cell_mean']) == 6


def test_module_in_stratum_rules(tmp_path):
    geneset_root = tmp_path / 'genesetstore'
    for experiment, scope in (('Micro', 'celltypes:\n- microglial cell\n'), ('Everywhere', 'celltypes:\n- all\n'),
                              ('Panel', 'celltypes:\n- all\nrole: markers\n')):
        folder = geneset_root / 'S' / experiment
        folder.mkdir(parents=True)
        (folder / 'index.md').write_text(f'---\norganism: human\nmethod: manual\n{scope}---\n')
        (folder / 'm.grp').write_text('A\nB\nC\n')
    registry = mod.GenesetRegistry(geneset_root)
    registry.scan()
    assert not registry.errors, registry.errors
    assert mod.module_in_stratum(registry, 'S/Micro', 'm', 'microglial cell')
    assert mod.module_in_stratum(registry, 'S/Micro', 'm', 'Mic P2RY12')           # prefix of the microglia group
    assert mod.module_in_stratum(registry, 'S/Micro', 'm', 'microglia')            # the group_pool label
    assert not mod.module_in_stratum(registry, 'S/Micro', 'm', 'astrocyte')
    assert not mod.module_in_stratum(registry, 'S/Micro', 'm', 'all')              # the single "none" stratum
    assert mod.module_in_stratum(registry, 'S/Everywhere', 'm', 'all')
    assert mod.module_in_stratum(registry, 'S/Everywhere', 'm', 'anything at all')
    assert not mod.module_in_stratum(registry, 'S/Panel', 'm', 'microglial cell')  # markers: never
    assert mod.module_in_stratum(registry, 'Unknown/Catalog', 'm', 'astrocyte')    # no declared scope


# ---------------------------------------------------------------------------
# app_nlgf in the module tool (2026-10-09): same design as the gene tool
# ---------------------------------------------------------------------------

NLGF_DERIVE = ('  obs_derive:\n    ant_app_nlgf:\n      source: dx\n      dtype: categorical\n'
               '      map:\n        nlgf: nlgf\n        control: control\n')
ANIMALS = 'replicate_unit: animal\nreplicate_column: ant_donor\n'


def make_xenograft_dataset(tmp_path: Path, design: str, timepoint_aligned: bool = False):
    """The synthetic dataset as a human xenograft: males are ``nlgf`` (the planted module effect), females ``control``.

    ``tp`` is an animal-level column with two timepoints, each holding 5 animals per genotype; with
    ``timepoint_aligned`` it equals the genotype instead (a fully confounded design column).
    """
    store, geneset_root, entry = make_module_dataset(
        tmp_path, extra_md=NLGF_DERIVE + ANIMALS + design, celltype_names=('T1', 'T2'))
    (store / 'Syn2026' / 'Syn_mod.h5ad.mus').write_text('organism=human\nexperimental_system=xenograft\n')
    conn = duckdb.connect(str(entry.duckdb_path))
    conn.execute("ALTER TABLE __obscat ADD COLUMN dx VARCHAR")
    conn.execute("ALTER TABLE __obscat ADD COLUMN tp VARCHAR")
    conn.execute("UPDATE __obscat SET dx = CASE WHEN Sex = 'M' THEN 'nlgf' ELSE 'control' END")
    if timepoint_aligned:
        conn.execute("UPDATE __obscat SET tp = CASE WHEN dx = 'nlgf' THEN '6m' ELSE '12m' END")
    else:
        conn.execute("UPDATE __obscat SET tp = CASE WHEN CAST(substr(donor_id, 2) AS INTEGER) % 2 = 0 "
                     "THEN '6m' ELSE '12m' END")
    conn.close()
    return store, geneset_root, entry


def test_app_nlgf_modules_are_tested_per_timepoint_with_the_design_recorded(tmp_path):
    store, geneset_root, entry = make_xenograft_dataset(
        tmp_path, 'control_basis: humanised_app_host\napp_nlgf_design:\n  timepoint_column: tp\n')
    manifest, out_root = _run(tmp_path, entry, store, geneset_root, facets=('app_nlgf',))
    assert manifest['status'] == 'done' and 'app_nlgf' in manifest['facets_tested_in_scope']
    assert manifest['control_basis'] == 'humanised_app_host' and manifest['app_nlgf_design'] == {'timepoint': 'tp'}
    units = [u for u in manifest['units'] if u['facet'] == 'app_nlgf' and u['status'] == 'ok']
    assert {u['celltype_source'] for u in units} == {'ant_author_cell_type@timepoint=12m',
                                                     'ant_author_cell_type@timepoint=6m'}
    assert {u['design_subset'] for u in units} == {'timepoint=12m', 'timepoint=6m'}
    assert all(u['control_basis'] == 'humanised_app_host' and u['level_counts'] == {'control': 5, 'nlgf': 5}
               for u in units)
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert set(table['celltype_source']) == {u['celltype_source'] for u in units}
    assert set(table['design_subset']) == {'timepoint=12m', 'timepoint=6m'}
    assert set(table['contrast']) == {'nlgf_vs_control'} and set(table['level_ref']) == {'control'}
    plant = table[(table['module'] == 'PLANT') & (table['method'] == 'cell_mean')]
    assert len(plant) == 2 and (plant['estimate'] > 0.3).all() and (plant['p_param'] < 0.05).all()
    # the permutation seeds of the two strata differ: the unit key carries the subset
    assert mod.unit_seed('d', 's', 'app_nlgf', 'timepoint=6m') != mod.unit_seed('d', 's', 'app_nlgf', 'timepoint=12m')
    assert mod.unit_seed('d', 's', 'app_nlgf') != mod.unit_seed('d', 's', 'app_nlgf', 'timepoint=6m')
    # the stratified units are the ones the sample sidecar and its staleness check key on
    samples = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_samples.parquet')
    assert set(samples['celltype_source']) == {u['celltype_source'] for u in units}


def test_app_nlgf_modules_without_a_design_run_one_unit(tmp_path):
    store, geneset_root, entry = make_xenograft_dataset(tmp_path, '')
    manifest, out_root = _run(tmp_path, entry, store, geneset_root, facets=('app_nlgf',))
    units = [u for u in manifest['units'] if u['facet'] == 'app_nlgf' and u['status'] == 'ok']
    assert manifest['status'] == 'done' and [u['celltype_source'] for u in units] == ['ant_author_cell_type']
    assert units[0]['design_subset'] is None and units[0]['level_counts'] == {'control': 10, 'nlgf': 10}
    table = pd.read_parquet(out_root / 'Syn2026' / 'Syn_mod.facet_module_tests.parquet')
    assert table['design_subset'].isna().all()


def test_app_nlgf_modules_refuse_a_design_covariate_confounded_with_the_genotype(tmp_path):
    store, geneset_root, entry = make_xenograft_dataset(
        tmp_path, 'control_basis: other\napp_nlgf_design:\n  covariate_columns: [tp]\n', timepoint_aligned=True)
    manifest, _ = _run(tmp_path, entry, store, geneset_root, facets=('app_nlgf',))
    statuses = {u['status'] for u in manifest['units'] if u['facet'] == 'app_nlgf'}
    assert statuses and all('confounded with tp' in status for status in statuses)


def test_app_nlgf_modules_skip_the_dataset_when_a_design_column_is_missing_or_the_design_malformed(tmp_path):
    store, geneset_root, entry = make_xenograft_dataset(tmp_path, 'app_nlgf_design:\n  timepoint_column: nope\n')
    manifest, _ = _run(tmp_path, entry, store, geneset_root, facets=('app_nlgf',))
    assert manifest['status'] == 'skipped' and 'nope' in manifest['reason']
    store, geneset_root, entry = make_xenograft_dataset(tmp_path / 'bad', 'control_basis: sideways\n')
    manifest, _ = _run(tmp_path / 'bad', entry, store, geneset_root, facets=('app_nlgf',))
    assert manifest['status'] == 'skipped' and 'sideways' in manifest['reason']


def test_app_nlgf_is_in_the_default_facets_and_sensitivity_facets_are_not():
    default = mod.build_parser().parse_args(['run', '--all']).facets.split(',')
    assert 'app_nlgf' in default and not set(default) & set(aft.SENSITIVITY_FACETS)


def test_design_subset_of_reads_the_celltype_source_suffix():
    import facet_module_results
    assert facet_module_results.design_subset_of('ant_cell_type@timepoint=6m|line=A') == 'timepoint=6m|line=A'
    assert facet_module_results.design_subset_of('ant_cell_type') is None


@pytest.mark.parametrize('n_cohorts, n_family, expected', [
    (0, 0, 'no counted cohort'), (1, 0, 'only 1 cohort'), (3, 0, 'no feature is tested in at least 2'), (3, 40, None)])
def test_not_tested_reason(n_cohorts, n_family, expected):
    import facet_results
    reason = facet_results.not_tested_reason('app_nlgf', 'xenograft_human_in_mouse', n_cohorts, n_family)
    if expected is None:
        assert reason is None
    else:
        assert expected in reason and 'app_nlgf' in reason and 'xenograft_human_in_mouse' in reason


def _write_scores_sidecar(path, n_cells=40, n_modules=7):
    rng = np.random.default_rng(3)
    rows = [{'cell_id': f'c{i}', 'source': source, 'catalog': 'cat', 'module': f'm{j}',
             'score': float(rng.normal())}
            for source in ('geneset', 'ucell') for j in range(n_modules) for i in range(n_cells)]
    pd.DataFrame(rows).to_parquet(path, index=False)
    return pd.DataFrame({'cell_id': [f'c{i}' for i in range(n_cells)], 'sample_idx': [i % 4 for i in range(n_cells)]})


def test_cell_mean_scores_batches_give_identical_means(tmp_path, monkeypatch):
    sample_map = _write_scores_sidecar(tmp_path / 's.scores.parquet')
    sizes = sample_map.groupby('sample_idx').size()
    results = []
    for batch_values in (10 ** 9, 1):      # one query, then one module per query
        monkeypatch.setattr(mod, 'SCORE_BATCH_VALUES', batch_values)
        conn = duckdb.connect(':memory:')
        results.append(mod.cell_mean_scores(conn, tmp_path / 's.scores.parquet', sample_map, sizes,
                                            ['geneset', 'ucell']))
        conn.close()
    pd.testing.assert_frame_equal(results[0][0], results[1][0], check_exact=True)
    pd.testing.assert_frame_equal(results[0][1], results[1][1], check_exact=True)
    assert results[0][0].shape == (4, 14)


def test_failed_manifest_is_recorded_and_hides_stale_results(tmp_path):
    entry = type('Entry', (), {'db_id': 'X/Y', 'study': 'X', 'stem': 'y'})()
    manifest_path = tmp_path / 'X' / 'y.facet_module_run_manifest.json'
    result_file = tmp_path / 'X' / 'y.facet_module_tests.parquet'
    result_file.parent.mkdir()
    assert mod._run_is_done(result_file)
    mod.write_failed_manifest(entry, manifest_path, MemoryError('out of memory'))
    stored = json.loads(manifest_path.read_text())
    assert stored['status'] == 'failed' and 'MemoryError: out of memory' in stored['reason']
    assert not mod._run_is_done(result_file)


def test_spill_base_dir_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ANT_FACET_TMP', str(tmp_path))
    assert mod.spill_base_dir() == tmp_path
    monkeypatch.delenv('ANT_FACET_TMP')
    monkeypatch.setenv('ANTHIVE_DUCKDB_TEMP_DIR', str(tmp_path / 'spill'))
    assert mod.spill_base_dir() == tmp_path / 'spill' and (tmp_path / 'spill').is_dir()
