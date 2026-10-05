"""Replicability statistics for the cross-cohort facet results.

Pure numpy functions (no I/O); ``lib/facet_results.py`` feeds them the
one-sided limma treat p values of the anchor unit of every cohort.

Method (Benjamini and Heller 2008, Biometrics 64:1215; Heller, Bogomolov and
Benjamini 2014, PNAS 111:16262): for one gene and one direction the partial
conjunction null "fewer than ``u`` of the ``m`` cohorts have a non-null effect"
is tested by combining the ``m - u + 1`` LARGEST of the m cohort p values. The
combination is Fisher's (default) or Simes'. The partial conjunction p values
of all genes of one (facet, cell type group) are then adjusted by
Benjamini-Hochberg; the adjusted value is the r-value, the smallest FDR at which
"replicated in at least ``u`` cohorts" can be claimed for that gene.

Fisher assumes independent cohorts, which is what the one-anchor-per-cohort rule
and the donor exclusions aim at; Simes holds under positive dependence and is the
more conservative choice for very small ``m``. Each cohort's p value is the
one-sided treat p value of the tested direction (H0: effect <= tau in that
direction; limma ``treat``, McCarthy and Smyth 2009), so a cohort whose effect
has the opposite sign contributes 1.
"""
from __future__ import annotations

import numpy as np
from scipy import stats

#: replication needs the effect in at least this many cohorts
DEFAULT_U = 2
#: smallest p value used in a log (avoids -inf for exact zeros)
P_FLOOR = 1e-300
R_THRESHOLD = 0.05
COMBINE_METHODS = ('fisher', 'simes')


def benjamini_hochberg(p_values: np.ndarray) -> np.ndarray:
    """BH adjusted p values; NaN entries are ignored (kept NaN) and do not count towards n.

    Args:
        p_values: 1-D array of p values in [0, 1] or NaN.

    Returns:
        Adjusted values, same shape, monotone and capped at 1.
    """
    p_values = np.asarray(p_values, dtype=float)
    adjusted = np.full(p_values.shape, np.nan)
    valid = ~np.isnan(p_values)
    n_valid = int(valid.sum())
    if n_valid == 0:
        return adjusted
    values = p_values[valid]
    order = np.argsort(values, kind='mergesort')
    ranked = values[order] * n_valid / np.arange(1, n_valid + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    result = np.empty(n_valid)
    result[order] = np.minimum(ranked, 1.0)
    adjusted[valid] = result
    return adjusted


def partial_conjunction_p(p_matrix: np.ndarray, u: int = DEFAULT_U, method: str = 'fisher') -> np.ndarray:
    """Partial conjunction p value per row of a genes x cohorts matrix.

    Args:
        p_matrix: ``(n_genes, m)`` one-sided p values; NaN where a cohort did
            not test the gene.
        u: Cohorts that must be non-null (default 2).
        method: ``fisher`` or ``simes``, applied to the ``m_i - u + 1``
            largest p values of row ``i`` (``m_i`` = non-NaN entries).

    Returns:
        ``(n_genes,)`` p values; NaN for rows with fewer than ``u`` p values.

    Raises:
        ValueError: unknown ``method`` or ``u`` < 1.
    """
    if method not in COMBINE_METHODS:
        raise ValueError(f'method must be one of {", ".join(COMBINE_METHODS)}')
    if u < 1:
        raise ValueError('u must be at least 1')
    matrix = np.asarray(p_matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError('p_matrix must be 2-dimensional')
    n_genes, n_cohorts = matrix.shape
    result = np.full(n_genes, np.nan)
    if n_cohorts == 0 or n_genes == 0:
        return result
    sorted_p = np.sort(np.where(np.isnan(matrix), np.inf, matrix), axis=1)  # NaN sort last
    n_valid = np.isfinite(sorted_p).sum(axis=1)
    columns = np.arange(n_cohorts)[None, :]
    selected = (columns >= u - 1) & (columns < n_valid[:, None])  # the m - u + 1 largest valid p
    n_selected = n_valid - u + 1
    clipped = np.clip(np.where(selected, sorted_p, 1.0), P_FLOOR, 1.0)
    if method == 'fisher':
        statistic = -2.0 * np.where(selected, np.log(clipped), 0.0).sum(axis=1)
        degrees = 2 * np.maximum(n_selected, 1)
        combined = stats.chi2.sf(statistic, degrees)
    else:
        ranks = np.where(selected, columns - (u - 1) + 1, 1)
        scaled = np.where(selected, n_selected[:, None] * clipped / ranks, np.inf)
        combined = np.minimum(scaled.min(axis=1), 1.0)
    usable = n_valid >= u
    result[usable] = np.minimum(combined[usable], 1.0)
    return result


def replicability(p_up: np.ndarray, p_down: np.ndarray, u: int = DEFAULT_U, method: str = 'fisher'
                  ) -> dict:
    """Partial conjunction r-values for both directions of every gene.

    Args:
        p_up: ``(n_genes, m)`` one-sided p values for an upward effect (1 where
            the observed effect is downward, NaN where untested).
        p_down: The same for a downward effect.
        u: Cohorts that must replicate.
        method: ``fisher`` or ``simes``.

    Returns:
        ``{p_pc, r_value, direction}``: per gene the better direction's
        partial conjunction p value (multiplied by 2 for the two directions,
        capped at 1), its BH adjustment over all genes with a value (the
        r-value), and ``up`` / ``down`` / None. Genes with fewer than ``u``
        cohorts are NaN / None and are not in the BH family.
    """
    pc_up = partial_conjunction_p(p_up, u, method)
    pc_down = partial_conjunction_p(p_down, u, method)
    both = np.vstack([pc_up, pc_down])
    with np.errstate(all='ignore'):
        best = np.fmin(pc_up, pc_down)
    best = np.where(np.isnan(best), np.nan, np.minimum(best * 2.0, 1.0))
    direction = np.where(np.isnan(both).all(axis=0), None,
                         np.where(np.nan_to_num(pc_up, nan=2.0) <= np.nan_to_num(pc_down, nan=2.0), 'up', 'down'))
    return {'p_pc': best, 'r_value': benjamini_hochberg(best), 'direction': direction}

