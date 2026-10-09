"""Simulation tests of the donor variability estimator (``lib/donor_variance.py``).

Planted truths: donor variance, a planted technical axis that the technical block must absorb, a planted facet share,
a latent axis the factors must absorb and the null. Counts are gamma-Poisson (cell level overdispersion summed over the
cells of a quarter); the truth of a gene is the realised variance across the simulated replicates of its true log2
proportion, which is what the covariance of the halves estimates (no sampling noise).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import donor_variance as dv


# ---- simulator -----------------------------------------------------------------------------------------------------

def simulate(n_replicates: int, n_genes: int = 300, cells_per_quarter: int = 25, umi_per_cell: float = 1500.0,
             sd_donor: np.ndarray | float = 0.3, technical: np.ndarray | None = None, technical_loading: np.ndarray | None = None,
             latent_loading: np.ndarray | None = None, latent_sd: float = 0.0, facet: np.ndarray | None = None,
             facet_effect: np.ndarray | None = None, seed: int = 1, cell_dispersion: float = 0.3):
    """Quarter counts (n, 4, genes), quarter library sizes (n, 4) and the true log2 proportion per replicate and gene."""
    rng = np.random.default_rng(seed)
    base = rng.normal(0.0, 0.7, n_genes)
    sd = np.broadcast_to(np.asarray(sd_donor, dtype=float), (n_genes,))
    effect = rng.normal(0.0, 1.0, (n_replicates, n_genes)) * sd
    if technical is not None:
        effect = effect + np.outer(technical, technical_loading)
    if latent_loading is not None:
        effect = effect + np.outer(rng.normal(0.0, latent_sd, n_replicates), latent_loading)
    if facet is not None:
        effect = effect + np.outer(facet, facet_effect)
    log_rate = base + effect
    proportion = 2.0 ** log_rate
    proportion /= proportion.sum(axis=1, keepdims=True)
    cells = cells_per_quarter * (0.8 + 0.4 * rng.random((n_replicates, 4)))
    lib = cells * umi_per_cell
    mean = lib[:, :, None] * proportion[:, None, :]
    shape = cells[:, :, None] / cell_dispersion
    rate = mean * rng.gamma(shape, 1.0 / shape, size=mean.shape)
    counts = rng.poisson(rate).astype(float)
    return counts, counts.sum(axis=2), np.log2(proportion * 1e6)


def run_unit(counts, lib, terms, n_boot=0, seed=3, rank=False):
    """Normalise the simulated quarters and estimate the variability of every gene."""
    median_half, expressed, gate = dv.gene_masks(counts.sum(axis=1))
    assert gate.sum() > 0.8 * counts.shape[2]
    halves = dv.normalised_halves(counts[:, :, gate], lib, np.ones(gate.sum(), bool))
    mean_expr = halves.full.mean(axis=0)
    result = dv.estimate_variability(halves.pairings, terms, mean_expr=mean_expr, full=halves.full,
                                     n_boot=n_boot, seed=seed, rank=rank)
    return result, halves, gate


# ---- planted donor variance ----------------------------------------------------------------------------------------

def test_v_donor_is_unbiased_and_covered():
    ratios, covered, widths = [], [], []
    for seed in range(4):
        counts, lib, truth = simulate(60, sd_donor=0.3, seed=seed)
        result, _, gate = run_unit(counts, lib, [], n_boot=100, seed=seed)
        true_v = truth[:, gate].var(axis=0, ddof=1)
        table = result.table
        ratios.append(table['v_donor'].mean() / true_v.mean())
        covered.append(((table['v_donor_lo'] <= true_v) & (true_v <= table['v_donor_hi'])).mean())
        widths.append((table['v_donor_hi'] - table['v_donor_lo']).mean() / true_v.mean())
    assert abs(np.mean(ratios) - 1.0) < 0.06
    assert np.mean(covered) > 0.85
    assert 0.2 < np.mean(widths) < 1.2           # relative SE of about 0.42 at n = 30; 60 replicates here


@pytest.mark.parametrize('n_replicates', [20, 50, 120])
def test_bias_small_across_sample_sizes(n_replicates):
    ratios = []
    for seed in range(3):
        counts, lib, truth = simulate(n_replicates, n_genes=200, sd_donor=0.25, seed=10 + seed)
        result, _, gate = run_unit(counts, lib, [])
        ratios.append(result.table['v_donor'].mean() / truth[:, gate].var(axis=0, ddof=1).mean())
    assert abs(np.mean(ratios) - 1.0) < 0.1


def test_reliability_tracks_signal_to_noise():
    sd = np.r_[np.full(150, 0.1), np.full(150, 0.5)]
    counts, lib, _ = simulate(80, sd_donor=sd, seed=5)
    result, _, gate = run_unit(counts, lib, [])
    reliability = result.table['reliability'].to_numpy()
    rows = np.flatnonzero(gate)
    low, high = reliability[rows < 150], reliability[rows >= 150]
    assert high.mean() > 0.9
    assert low.mean() < high.mean() - 0.2


def test_null_has_no_donor_variance():
    counts, lib, _ = simulate(60, sd_donor=0.0, seed=8)
    result, _, _ = run_unit(counts, lib, [], n_boot=60)
    table = result.table
    assert abs(table['v_donor'].mean()) < 0.05 * table['var_half'].mean()
    assert table['reliability'].mean() < 0.2
    assert (table['v_donor_lo'] <= 0.002).mean() > 0.9           # the interval reaches zero for nearly every gene


def test_split_half_beats_the_naive_total_variance():
    counts, lib, truth = simulate(60, sd_donor=0.1, seed=9)
    result, halves, gate = run_unit(counts, lib, [])
    naive = halves.full.var(axis=0, ddof=1).mean()
    true_v = truth[:, gate].var(axis=0, ddof=1).mean()
    assert naive > 1.5 * true_v                                  # sampling noise inflates the plain variance
    assert abs(result.table['v_donor'].mean() / true_v - 1.0) < 0.15


# ---- technical axis, facet share, latent axis ----------------------------------------------------------------------

def test_planted_technical_axis_is_absorbed_by_the_technical_block():
    n_replicates = 80
    rng = np.random.default_rng(2)
    technical = rng.normal(size=n_replicates)
    loading = np.r_[np.full(40, 0.4), np.zeros(260)]       # few loaded genes: TMM keeps their scale
    sd = np.full(300, 0.1)
    counts, lib, _ = simulate(n_replicates, sd_donor=sd, technical=technical, technical_loading=loading, seed=11)
    observed = technical + rng.normal(0.0, 0.05, n_replicates)   # the measured covariate tracks the axis
    result, _, gate = run_unit(counts, lib, [dv.Term('technical', observed)])
    table = result.table
    rows = np.flatnonzero(gate)
    loaded_rows, clean_rows = np.flatnonzero(rows < 40), np.flatnonzero(rows >= 40)
    assert table['share_design_D0'].to_numpy()[loaded_rows].mean() > 0.85
    assert np.nanmean(table['share_design_D0'].to_numpy()[clean_rows]) < 0.2
    # what is left for the loaded genes is about the random donor variance (0.01) and no longer the axis (0.16)
    assert table['v_unexplained_D0'].to_numpy()[loaded_rows].mean() < 0.03
    assert table['v_donor'].to_numpy()[loaded_rows].mean() > 0.12


def test_planted_facet_share_and_shapley_split():
    n_replicates = 90
    rng = np.random.default_rng(4)
    facet = (rng.random(n_replicates) < 0.5).astype(float)
    effect = np.r_[np.full(60, 0.4), np.zeros(240)]
    counts, lib, _ = simulate(n_replicates, sd_donor=0.2, facet=facet, facet_effect=effect, seed=12)
    technical = rng.normal(size=n_replicates)
    terms = [dv.Term('technical', technical), dv.Term('sex', (rng.random(n_replicates) < 0.5).astype(float)),
             dv.Term('disease', facet)]
    result, _, gate = run_unit(counts, lib, terms)
    table = result.table
    rows = np.flatnonzero(gate)
    planted = np.flatnonzero(rows < 60)
    others = np.flatnonzero(rows >= 60)
    # truth: facet variance 0.16 * 0.25 = 0.04 against random 0.04: share 0.5
    assert 0.35 < table['share_design_D2'].to_numpy()[planted].mean() < 0.65
    assert table['share_design_D2'].to_numpy()[others].mean() < 0.15
    assert 0.35 < table['lmg_disease'].to_numpy()[planted].mean() < 0.65
    assert abs(table['lmg_technical'].to_numpy()[planted].mean()) < 0.1
    assert abs(table['lmg_sex'].to_numpy()[planted].mean()) < 0.1
    assert result.table.attrs['lmg_rung'] == 'D2'
    lmg_sum = sum(table[f'lmg_{name}'] for name in ('technical', 'sex', 'disease'))
    explained = 1.0 - table['v_unexplained_D2'] / table['v_donor']             # all replicates are complete: same V_donor
    assert np.nanmax(np.abs(lmg_sum - explained)) < 1e-9                       # Shapley efficiency
    assert result.deepest == 'D2'
    assert table['share_facets'].to_numpy()[planted].mean() > 0.3


def test_latent_factors_absorb_an_unmeasured_axis():
    n_replicates = 90
    loading = np.r_[np.full(120, 0.5), np.zeros(180)]
    counts, lib, _ = simulate(n_replicates, sd_donor=0.1, latent_loading=loading, latent_sd=1.0, seed=13)
    rng = np.random.default_rng(1)
    terms = [dv.Term('technical', rng.normal(size=n_replicates))]
    result, _, gate = run_unit(counts, lib, terms)
    table = result.table
    rows = np.flatnonzero(gate)
    loaded = np.flatnonzero(rows < 120)
    assert 'v_unexplained_F' in table
    assert table['v_unexplained_F'].to_numpy()[loaded].mean() < 0.35 * table['v_unexplained_D0'].to_numpy()[loaded].mean()
    assert table['share_latent'].to_numpy()[loaded].mean() > 0.6
    assert result.factors is not None and result.factors.shape == (n_replicates, 4)


def test_rungs_are_nested_and_missing_ones_are_reported():
    n_replicates = 70
    rng = np.random.default_rng(6)
    counts, lib, _ = simulate(n_replicates, seed=14)
    age = rng.normal(size=n_replicates)
    age_with_gap = age.copy()
    age_with_gap[:5] = np.nan
    terms = [dv.Term('technical', rng.normal(size=n_replicates)), dv.Term('age', age_with_gap)]
    result, _, _ = run_unit(counts, lib, terms)
    by_name = {info.name: info for info in result.rungs}
    assert by_name['D0'].computed and by_name['D0'].n == n_replicates
    assert by_name['D1'].computed and by_name['D1'].n == n_replicates - 5          # listwise deletion
    assert not by_name['D2'].computed and by_name['D2'].reason == 'no new term'
    assert result.deepest == 'D1'
    assert 'share_design_D2' not in result.table


def test_small_cohorts_get_no_share():
    counts, lib, _ = simulate(20, seed=15)
    rng = np.random.default_rng(1)
    terms = [dv.Term('technical', rng.normal(size=(20, 3))), dv.Term('age', rng.normal(size=20))]
    result, _, _ = run_unit(counts, lib, terms)
    assert not result.rungs[0].computed and 'n - p' in result.rungs[0].reason
    assert 'share_design_D0' not in result.table and 'v_donor' in result.table
    assert 'lmg_technical' not in result.table


def test_per_term_shares_need_thirty_residual_df():
    counts, lib, _ = simulate(44, seed=16)
    rng = np.random.default_rng(1)
    terms = [dv.Term('technical', rng.normal(size=44)), dv.Term('sex', (rng.random(44) < 0.5).astype(float))]
    result, _, _ = run_unit(counts, lib, terms)
    assert result.rungs[1].residual_df == 41
    assert 'lmg_technical' in result.table
    counts, lib, _ = simulate(30, seed=16)
    terms = [dv.Term('technical', rng.normal(size=(30, 4))), dv.Term('sex', (rng.random(30) < 0.5).astype(float))]
    result, _, _ = run_unit(counts, lib, terms)
    assert result.rungs[1].residual_df < dv.TERM_SHARE_MIN_RESIDUAL_DF
    assert 'share_design_D1' in result.table and 'lmg_technical' not in result.table


def test_too_few_replicates_raise():
    counts, lib, _ = simulate(10, seed=17)
    with pytest.raises(ValueError, match='replicates'):
        run_unit(counts, lib, [])


# ---- ranking -------------------------------------------------------------------------------------------------------

def test_excess_z_ranks_planted_variable_genes_first():
    sd = np.full(400, 0.12)
    sd[:8] = 0.8
    counts, lib, _ = simulate(80, n_genes=400, sd_donor=sd, seed=18, umi_per_cell=3000.0)
    rng = np.random.default_rng(1)
    result, _, gate = run_unit(counts, lib, [dv.Term('technical', rng.normal(size=80))], rank=True)
    table = result.table
    rows = np.flatnonzero(gate)
    planted = [i for i, gene in enumerate(rows) if gene < 8]
    top = np.argsort(-table['excess_z_raw'].fillna(-9).to_numpy())[:len(planted) + 3]
    assert set(planted) <= set(top)
    assert table['p_excess_raw'].to_numpy()[planted].max() < 0.05


def test_excess_needs_twenty_replicates():
    counts, lib, _ = simulate(16, seed=19)
    result, _, _ = run_unit(counts, lib, [], rank=True)
    assert not [c for c in result.table.columns if c.startswith('excess_z')]


def test_robust_loess_ignores_outliers():
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, 400)
    y = 0.5 * x + np.sin(x / 2) + rng.normal(0, 0.1, 400)
    y[:10] += 8.0
    fitted = dv.robust_loess(x, y)
    truth = 0.5 * x + np.sin(x / 2)
    assert np.abs(fitted[10:] - truth[10:]).mean() < 0.12


def test_excess_scores_edge_cases():
    z, p = dv.excess_scores(np.ones(10), np.arange(10.0), np.ones(10, bool))
    assert np.isnan(z).all() and np.isnan(p).all()                # fewer than 30 usable features
    rng = np.random.default_rng(0)
    expr = rng.normal(5, 1, 100)
    z, p = dv.excess_scores(np.exp(rng.normal(size=100)), expr, np.r_[np.ones(90, bool), np.zeros(10, bool)])
    assert np.isnan(z[90:]).all() and np.isfinite(z[:90]).all()
    assert p[:90].min() >= 1 / 90 - 1e-12 and p[:90].max() == 1.0
    z, p = dv.excess_scores(np.r_[np.exp(rng.normal(size=50)), np.full(50, -1.0)], expr, np.ones(100, bool))
    assert np.isfinite(z[:50]).all() and np.isnan(z[50:]).all()    # non-positive variances are never ranked


# ---- building blocks -----------------------------------------------------------------------------------------------

def test_quarters_are_balanced_deterministic_and_order_free():
    replicate = np.repeat(np.arange(6), [41, 40, 43, 100, 57, 4])
    cell_ids = np.arange(len(replicate)) * 7 + 3
    quarters = dv.assign_quarters(replicate, cell_ids, 5)
    assert set(quarters) == {0, 1, 2, 3}
    for index in range(6):
        sizes = np.bincount(quarters[replicate == index], minlength=4)
        assert sizes.max() - sizes.min() <= 1
    assert (dv.assign_quarters(replicate, cell_ids, 5) == quarters).all()
    shuffle = np.random.default_rng(0).permutation(len(replicate))
    assert (dv.assign_quarters(replicate[shuffle], cell_ids[shuffle], 5) == quarters[shuffle]).all()
    assert (dv.assign_quarters(replicate, cell_ids, 6) != quarters).any()


def test_tmm_recovers_planted_composition_shift_and_is_neutral_otherwise():
    rng = np.random.default_rng(0)
    profile = rng.dirichlet(np.ones(500) * 2)
    scale = np.array([1e5, 2e5, 5e4, 1.5e5])
    counts = np.vstack([rng.poisson(profile * s) for s in scale]).astype(float)
    factors = dv.tmm_norm_factors(counts, counts.sum(axis=1))
    assert np.exp(np.log(factors).mean()) == pytest.approx(1.0)
    assert np.abs(np.log(factors)).max() < 0.05
    shifted = counts.copy()
    shifted[0, :10] *= 30                                       # sample 0 spends reads on 10 genes
    factors = dv.tmm_norm_factors(shifted, shifted.sum(axis=1))
    assert factors[0] < 0.97 * factors[1:].mean()                 # inflated library: factor below the others


def test_log2_cpm_matches_edger_formula():
    counts = np.array([[10.0, 90.0], [1.0, 3.0]])
    lib = np.array([100.0, 4.0])
    out = dv.log2_cpm(counts, lib, mean_lib=52.0, prior=2.0)
    prior = 2.0 * lib / 52.0
    expected = np.log2((counts + prior[:, None]) / (lib + 2 * prior)[:, None] * 1e6)
    assert np.allclose(out, expected)


def test_gene_masks():
    counts = np.array([[100.0, 10.0, 0.0, 0.0], [120.0, 12.0, 0.0, 0.0], [90.0, 8.0, 40.0, 0.0],
                       [95.0, 9.0, 60.0, 0.0], [110.0, 11.0, 0.0, 30.0]])
    half_q70, expressed, gate = dv.gene_masks(counts)
    assert half_q70.tolist() == pytest.approx([54.0, 5.4, 16.0, 0.0])
    assert expressed.tolist() == [True, True, True, False]
    assert gate.tolist() == [True, False, True, False]     # a gene present in 40 percent of the replicates passes (UTY)


def test_weighted_cross_equals_duplicated_rows():
    rng = np.random.default_rng(0)
    first, second = rng.normal(size=(15, 4)), rng.normal(size=(15, 4))
    design = rng.normal(size=(15, 2))
    draw = rng.integers(0, 15, 15)
    weights = np.bincount(draw, minlength=15).astype(float)
    cross, rank = dv._weighted_cross(first * second, first, second, design, weights)
    direct, direct_rank, _, _ = dv._cross([(first[draw], second[draw])], None, design[draw], with_variance=False)
    assert rank == direct_rank == 3
    assert np.allclose(cross, direct * 1.0)


def test_latent_factor_count_schedule():
    assert [dv.n_latent_factors(n) for n in (12, 29, 30, 59, 60, 99, 100, 500)] == [0, 0, 3, 3, 4, 4, 5, 5]


def test_module_weights_are_the_score_difference():
    rng = np.random.default_rng(0)
    expression = rng.normal(size=(6, 10))
    genes = [np.array([0, 1, 2]), np.array([5, 6, 7, 8])]
    controls = [np.array([3, 4]), np.array([0, 1, 2, 3])]
    scores = expression @ dv.module_weights(genes, controls, 10)
    assert np.allclose(scores[:, 0], expression[:, [0, 1, 2]].mean(1) - expression[:, [3, 4]].mean(1))
    assert np.allclose(scores[:, 1], expression[:, [5, 6, 7, 8]].mean(1) - expression[:, [0, 1, 2, 3]].mean(1))


def test_half_means_weighted_by_cells():
    quarter_sum = np.array([[[2.0], [4.0], [6.0], [8.0]]])
    quarter_n = np.array([[[1.0], [1.0], [2.0], [2.0]]])
    (first_a, first_b), = dv.half_means(quarter_sum, quarter_n)[:1]
    assert first_a[0, 0] == pytest.approx(3.0) and first_b[0, 0] == pytest.approx(14.0 / 4)
    empty = dv.half_means(np.zeros((1, 4, 1)), np.zeros((1, 4, 1)))
    assert np.isnan(empty[0][0]).all()


def test_covariate_terms_follow_the_coverage_rules():
    n_rows = 40
    rng = np.random.default_rng(0)
    donors = pd.DataFrame({'sex': rng.choice(['m', 'f'], n_rows), 'age': rng.normal(80, 8, n_rows),
                           'pmi': np.where(np.arange(n_rows) < 30, 5.0, np.nan),
                           'disease': rng.choice(['AD', 'control'], n_rows),
                           'apoe4_carrier': ['e4_carrier'] * 2 + ['noncarrier'] * 38,
                           'braak': rng.integers(0, 7, n_rows).astype(float),
                           'batch': rng.choice(['a', 'b', 'c'], n_rows)})
    terms, omitted = dv.covariate_terms(donors, scope_is_xenograft=False)
    names = [term.name for term in terms]
    assert names == ['sex', 'age', 'batch', 'disease', 'braak']
    assert set(omitted) == {'pmi', 'apoe4'}
    assert dict((t.name, t.matrix().shape[1]) for t in terms)['batch'] == 2
    assert abs(next(t for t in terms if t.name == 'age').values.mean()) < 1e-9
    xeno = pd.DataFrame({'host_sex': rng.choice(['m', 'f'], n_rows), 'sex': ['m'] * n_rows,
                         'app_nlgf': rng.choice(['nlgf', 'control'], n_rows)})
    terms, omitted = dv.covariate_terms(xeno, scope_is_xenograft=True)
    assert [t.name for t in terms] == ['sex', 'disease']
    many_batches = pd.DataFrame({'batch': [f'b{i}' for i in range(n_rows)]})
    assert 'batch' in dv.covariate_terms(many_batches, False)[1]


def test_technical_terms_drop_constant_columns_and_standardise():
    n_cells = np.array([50.0, 80.0, 120.0, 60.0, 90.0])
    umi = n_cells * np.array([1200.0, 1500.0, 900.0, 1300.0, 1100.0])
    nnz = n_cells * 700.0
    terms = dv.technical_terms(n_cells, umi, nnz, np.full(5, 0.01), {'neuron': np.array([1.0, 2.0, 0.5, 3.0, 1.5]), 'oligodendrocyte': np.full(5, 0.7), 'astrocyte': np.full(5, np.nan)}, None,
                               np.array([[1.0, 0.0], [0.5, 1.0], [0.2, 0.3], [0.9, 0.1], [0.0, 0.0]]))
    by_name = {term.name: term for term in terms}
    assert by_name['technical'].matrix().shape == (5, 3)           # cells, UMI per cell, neuron ambient; the rest is constant or missing
    assert by_name['technical'].names == ['log_n_cells', 'log_umi_per_cell', 'ambient_neuron']
    assert by_name['composition'].matrix().shape == (5, 2)
    assert np.abs(by_name['technical'].matrix()).max() <= 4.0


def test_composition_pcs_and_foreign_panels():
    proportions = pd.DataFrame(np.random.default_rng(0).dirichlet(np.ones(4), 30))
    assert dv.composition_pcs(proportions).shape == (30, 3)
    assert dv.composition_pcs(proportions.iloc[:, :1]) is None
    assert dv.lineage_of_group('microglia') == 'microglia'
    assert dv.lineage_of_group('excitatory neuron') == 'neuron'
    assert dv.lineage_of_group('endothelial cell') is None
    assert 'PLP1' in dv.foreign_panel('microglia') and 'CSF1R' not in dv.foreign_panel('microglia')
    assert set(dv.foreign_panels('microglia')) == {'neuron', 'oligodendrocyte', 'astrocyte'}
    assert set(dv.foreign_panels('oligodendrocyte')) == {'neuron', 'astrocyte', 'microglia'}
    assert 'SNAP25' not in dv.foreign_panel('excitatory neuron')


def test_robust_z_is_clipped_and_constant_safe():
    assert (dv.robust_z(np.full(5, 3.0)) == 0).all()
    values = np.r_[np.random.default_rng(0).normal(size=50), 1000.0]
    assert dv.robust_z(values).max() == 4.0
