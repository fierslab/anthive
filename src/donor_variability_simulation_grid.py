"""Simulation grid for the donor variability estimator: bias and coverage of V_donor, reliability and share_design.

What it does: simulates gamma-Poisson counts with planted donor variance (SD 0.2 log2 for every gene) and a planted facet
effect on one third of the genes (0.3 log2), runs ``lib/donor_variance.py`` and compares the estimates with the realised truth
of the simulated replicates (the variance across replicates of the true log2 proportion, which is what the split-half
covariance estimates; the realised share of the true means explained by the design for ``share_design``).

Input: none. Output: a table on stdout, one row per (replicates n, cells per replicate m) with the mean ratio estimate / truth
of V_donor (``ratio_v``), the coverage of its 95 percent bootstrap interval (``cov_v``), the mean bias of the reliability and
the coverage of its interval, and the same for ``share_design`` of rung D2 (n >= 50 only: the technical block needs room).

Usage (from the repository root)::

    uv run --with numpy --with pandas --with scipy --with pytest env PYTHONPATH=lib python src/donor_variability_simulation_grid.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT / 'tests')]

import donor_variance as dv  # noqa: E402
from test_donor_variability import simulate  # noqa: E402

N_REPLICATES = (12, 20, 50, 200)
CELLS = (40, 80, 113)
N_SEEDS = 6
N_GENES = 300


def one_cell(n_replicates: int, cells: int) -> dict:
    """Mean bias and coverage over ``N_SEEDS`` simulated datasets."""
    ratio, coverage, rel_bias, rel_coverage, share_bias, share_coverage = [], [], [], [], [], []
    for seed in range(N_SEEDS):
        rng = np.random.default_rng(seed)
        facet = (rng.random(n_replicates) < 0.5).astype(float)
        effect = np.r_[np.full(100, 0.3), np.zeros(200)]
        counts, lib, truth = simulate(n_replicates, n_genes=N_GENES, cells_per_quarter=max(cells // 4, 1), sd_donor=0.2,
                                      facet=facet, facet_effect=effect, seed=seed)
        _, _, gate = dv.gene_masks(counts.sum(axis=1))
        halves = dv.normalised_halves(counts[:, :, gate], lib, np.ones(gate.sum(), bool))
        terms = [dv.Term('technical', rng.normal(size=n_replicates)), dv.Term('disease', facet)] if n_replicates >= 35 else []
        table = dv.estimate_variability(halves.pairings, terms, n_boot=100, seed=seed).table
        true_v = truth[:, gate].var(axis=0, ddof=1)
        ratio.append(table['v_donor'].mean() / true_v.mean())
        coverage.append(((table['v_donor_lo'] <= true_v) & (true_v <= table['v_donor_hi'])).mean())
        noise = np.mean([(a - b).var(axis=0, ddof=1) / 2 for a, b in halves.pairings], axis=0)
        r_true = true_v / (true_v + noise)
        reliability_true = 2 * r_true / (1 + r_true)
        rel_bias.append(np.nanmean(table['reliability'] - reliability_true))
        rel_coverage.append(((table['reliability_lo'] <= reliability_true) & (reliability_true <= table['reliability_hi'])).mean())
        if 'share_design_D2' in table:
            design = np.column_stack([np.ones(n_replicates), terms[0].values, facet])
            beta = np.linalg.lstsq(design, truth[:, gate], rcond=None)[0]
            share_true = np.clip(1 - (truth[:, gate] - design @ beta).var(axis=0, ddof=3) / true_v, 0, 1)
            share_bias.append(np.nanmean(table['share_design_D2'] - share_true))
            share_coverage.append(np.nanmean((table['share_design_lo'] <= share_true) & (share_true <= table['share_design_hi'])))
    return {'n': n_replicates, 'cells': cells, 'ratio_v': np.mean(ratio), 'cov_v': np.mean(coverage),
            'bias_reliability': np.mean(rel_bias), 'cov_reliability': np.mean(rel_coverage),
            'bias_share': np.mean(share_bias) if share_bias else np.nan,
            'cov_share': np.mean(share_coverage) if share_coverage else np.nan}


def main() -> None:
    """Print the grid."""
    rows = [one_cell(n, cells) for n in N_REPLICATES for cells in CELLS]
    print(pd.DataFrame(rows).round(3).to_string())


if __name__ == '__main__':
    main()
