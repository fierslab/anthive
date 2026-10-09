"""Donor variability of gene expression and module scores: split-half covariance estimator (pure numpy).

Question answered: how much do replicates (donors, or animals for xenografts) differ from each other in one cell
type, beyond what cell sampling noise produces, and how much of that is explained by what we know about them.

Method (design: ``doc/DESIGN_donor_variability.md``; second opinion ``fable_variability.md``)
----------------------------------------------------------------------------------------------
Per replicate and stratum the cells are dealt into four random quarters (hash of the cell id and a seed, balanced to one
cell). Raw counts are summed per quarter; three balanced pairings of quarters give two independent halves ``A`` and ``B``
per replicate. Library size normalisation is TMM computed once on the full replicate and applied to both halves
(``tmm_norm_factors``), the scale is log2 CPM with a library scaled prior count of 2 (edgeR ``cpm``).

For a feature (gene or module score) the halves are ``x_A = mu_d + e_A`` and ``x_B = mu_d + e_B`` with independent
cell sampling errors, so the covariance of ``x_A`` and ``x_B`` over replicates is an unbiased estimate of the donor
variance ``V_donor`` (the variance of the replicates' true half-library log2 expression) without the noise. It is
averaged over the three pairings (they are three correlated looks at the same cells, not three replicates).
``reliability`` is the Spearman-Brown corrected split-half correlation. Removing a design ``X`` from both halves
gives ``V_unexplained = r_A' r_B / (n - p)`` and ``share_design = 1 - V_unexplained / V_donor`` (a disattenuated R squared
of the true replicate means on ``X``; it can fall outside [0, 1] by chance, the clipped value is reported).

``X`` is built in nested rungs: D0 technical block (log cells, log UMI per cell, genes per cell, mitochondrial fraction,
ambient score of foreign lineage markers, long gene index, composition PCs), D1 + sex, age, PMI, batch, D2 + disease,
D3 + APOE e4, D4 + Braak, F + latent factors (PCs of the residual replicate matrix, 3 to 5 when n >= 30). Everything
the halves share by construction (library prep, ambient RNA, depth) is part of ``V_donor``; the technical block and the
factors exist to keep it out of "unexplained". Per-term shares are Shapley (LMG) values of the explained fraction over the
terms of the deepest rung.

Gates (all constants below): compute at >= 12 replicates, rank within a cohort at >= 20, ``share_design`` at
``n - p >= 20``, per-term shares at ``n - p >= 30``, a gene needs an expected count of 10 per half in at least 30 percent of the replicates.
No p value of ``V_donor = 0`` is produced (nearly every expressed gene rejects it); ``excess_z`` is a within-unit
ranking statistic (robust loess trend of log V against mean expression, MAD scaled), ``p_excess`` its rank based upper
tail inside the unit (relative, exploratory).
"""
from __future__ import annotations

import math
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TOOL_METHOD = 'split-half covariance, TMM on the full replicate, nested design rungs'

# ---- gates and parameters (written to every manifest) -------------------------------------------------------------
MIN_REPLICATES = 12                # compute V_donor and reliability
MIN_CELLS_PER_REPLICATE = 40       # 20 per half; the sensitivity run uses 20
RANK_MIN_REPLICATES = 20           # excess_z within the unit
SHARE_MIN_RESIDUAL_DF = 20         # share_design, V_unexplained of a rung
TERM_SHARE_MIN_RESIDUAL_DF = 30    # per-term LMG shares
CROSS_COHORT_MIN_REPLICATES = 30   # a cohort takes part in the cross-cohort ranking
FACTOR_MIN_REPLICATES = 30         # latent factors
COUNT_GATE_HALF = 10.0             # expected count per half a reported gene reaches in at least 30 percent of the replicates
EXPRESSED_GATE_HALF = 1.0          # the same for the genes kept in the module and normalisation matrix
COUNT_GATE_QUANTILE = 0.7          # 70th percentile over replicates, not the median: a gene of one sex (UTY, XIST) must pass
RELIABILITY_FLOOR = 0.3            # genes ranked by excess_z need at least this reliability
PRIOR_COUNT = 2.0
N_QUARTERS = 4
PAIRINGS: Tuple[Tuple[Tuple[int, int], Tuple[int, int]], ...] = (
    ((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)))
N_BOOT = 200
BOOT_BUDGET = 2.0e10               # n * genes * 48 * n_boot above this reduces n_boot (floor 50)
MIN_BOOT = 50
RUNGS = ('D0', 'D1', 'D2', 'D3', 'D4')
FACTOR_RUNG = 'F'
EXCESS_RUNGS = ('raw',) + RUNGS + (FACTOR_RUNG,)
#: Term names in the order of the LMG columns; ``block`` says which share group a term belongs to.
TERM_RUNG = {'technical': 'D0', 'composition': 'D0', 'sex': 'D1', 'age': 'D1', 'pmi': 'D1', 'batch': 'D1',
             'disease': 'D2', 'apoe4': 'D3', 'braak': 'D4'}
TERM_BLOCK = {'technical': 'technical', 'composition': 'technical', 'sex': 'covariate', 'age': 'covariate',
              'pmi': 'covariate', 'batch': 'covariate', 'disease': 'facet', 'apoe4': 'facet', 'braak': 'facet'}
TERM_ORDER = tuple(TERM_RUNG)

#: Foreign lineage marker panels for the ambient score (the panel of the stratum's own lineage is left out).
AMBIENT_PANELS: Dict[str, Tuple[str, ...]] = {
    'neuron': ('SNAP25', 'SYT1', 'RBFOX3', 'STMN2', 'SLC17A7', 'GAD1', 'GAD2'),
    'oligodendrocyte': ('PLP1', 'MBP', 'ST18', 'MOBP', 'MOG'),
    'astrocyte': ('AQP4', 'GFAP', 'SLC1A2', 'ALDH1L1'),
    'microglia': ('CSF1R', 'P2RY12', 'CX3CR1', 'TMEM119'),
}
_LINEAGE_OF_GROUP = (('microglia', 'microglia'), ('macrophage', 'microglia'), ('myeloid', 'microglia'),
                     ('oligodendrocyte', 'oligodendrocyte'), ('astrocyte', 'astrocyte'), ('neuron', 'neuron'),
                     ('neural progenitor', 'neuron'))


@contextmanager
def _quiet():
    """Silence the RuntimeWarnings of nan-aware reductions over all-NaN slices (a NaN result is the intended answer)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        yield


# ---- deterministic quarters ----------------------------------------------------------------------------------------

def hash_cells(cell_ids: np.ndarray, seed: int) -> np.ndarray:
    """splitmix64 of (cell id, seed): a deterministic pseudo random 64 bit value per cell.

    Args:
        cell_ids: Integer cell ids.
        seed: Run seed; changing it reshuffles every quarter.

    Returns:
        uint64 array, same length.
    """
    with np.errstate(over='ignore'):
        value = cell_ids.astype(np.uint64) + (np.uint64(seed) + np.uint64(1)) * np.uint64(0x9E3779B97F4A7C15)
        value = (value ^ (value >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        value = (value ^ (value >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return value ^ (value >> np.uint64(31))


def assign_quarters(replicate_index: np.ndarray, cell_ids: np.ndarray, seed: int) -> np.ndarray:
    """Quarter (0 to 3) of every cell: random within its replicate, balanced to one cell, independent of row order.

    Args:
        replicate_index: Integer replicate (group) of each cell.
        cell_ids: Integer cell ids (the hash input).
        seed: Run seed.

    Returns:
        int8 array of quarters.
    """
    hashed = hash_cells(np.asarray(cell_ids), seed)
    order = np.lexsort((hashed, np.asarray(replicate_index)))
    sorted_group = np.asarray(replicate_index)[order]
    starts = np.r_[0, np.flatnonzero(np.diff(sorted_group)) + 1]
    first_of_row = np.repeat(starts, np.diff(np.r_[starts, len(order)]))
    position = np.arange(len(order)) - first_of_row
    quarters = np.empty(len(order), dtype=np.int8)
    quarters[order] = (position % N_QUARTERS).astype(np.int8)
    return quarters


# ---- normalisation -------------------------------------------------------------------------------------------------

def _rank(values: np.ndarray) -> np.ndarray:
    ranks = np.empty(len(values), dtype=np.int64)
    ranks[np.argsort(values, kind='mergesort')] = np.arange(1, len(values) + 1)
    return ranks


def _tmm_pair(observed: np.ndarray, reference: np.ndarray, observed_lib: float, reference_lib: float,
              logratio_trim: float, sum_trim: float) -> float:
    both = (observed > 0) & (reference > 0)
    if both.sum() < 2:
        return 1.0
    obs, ref = observed[both].astype(float), reference[both].astype(float)
    log_ratio = np.log2((obs / observed_lib) / (ref / reference_lib))
    abs_expr = (np.log2(obs / observed_lib) + np.log2(ref / reference_lib)) / 2.0
    variance = (observed_lib - obs) / observed_lib / obs + (reference_lib - ref) / reference_lib / ref
    n_genes = len(log_ratio)
    if np.max(np.abs(log_ratio)) < 1e-6:
        return 1.0
    low_l = math.floor(n_genes * logratio_trim) + 1
    high_l = n_genes + 1 - low_l
    low_s = math.floor(n_genes * sum_trim) + 1
    high_s = n_genes + 1 - low_s
    rank_l, rank_s = _rank(log_ratio), _rank(abs_expr)
    keep = (rank_l >= low_l) & (rank_l <= high_l) & (rank_s >= low_s) & (rank_s <= high_s)
    if not keep.any():
        return 1.0
    weights = 1.0 / variance[keep]
    return float(2.0 ** (np.sum(log_ratio[keep] * weights) / np.sum(weights)))


def tmm_norm_factors(counts: np.ndarray, lib_sizes: np.ndarray, logratio_trim: float = 0.3,
                     sum_trim: float = 0.05) -> np.ndarray:
    """edgeR TMM normalisation factors (reference sample by upper quartile, weighted trimmed mean of M values).

    Args:
        counts: (samples, genes) raw counts of the genes used for the estimate.
        lib_sizes: Library size of each sample (all genes).
        logratio_trim: Fraction trimmed from each end of the log ratios (edgeR default 0.3).
        sum_trim: Fraction trimmed from each end of the mean log expression (edgeR default 0.05).

    Returns:
        (samples,) factors with geometric mean 1.
    """
    lib_sizes = np.asarray(lib_sizes, dtype=float)
    upper = np.quantile(counts / lib_sizes[:, None], 0.75, axis=1)
    reference = int(np.argmin(np.abs(upper - upper.mean())))
    factors = np.array([_tmm_pair(counts[i], counts[reference], lib_sizes[i], lib_sizes[reference],
                                  logratio_trim, sum_trim) for i in range(len(lib_sizes))])
    return factors / np.exp(np.mean(np.log(factors)))


def log2_cpm(counts: np.ndarray, effective_lib: np.ndarray, mean_lib: float, prior: float = PRIOR_COUNT) -> np.ndarray:
    """edgeR ``cpm(log = TRUE)``: ``log2((count + prior') / (lib + 2 prior') * 1e6)`` with the prior scaled by library size.

    Args:
        counts: (..., genes) counts.
        effective_lib: (...,) library size times TMM factor.
        mean_lib: Library size the prior count is expressed at.
        prior: Prior count at the mean library size.
    """
    scaled = prior * effective_lib / mean_lib
    return np.log2((counts + scaled[..., None]) / (effective_lib + 2.0 * scaled)[..., None] * 1e6)


def gene_masks(full_counts: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Expression gates from replicate totals.

    The expected count per half (replicate count / 2) must reach the threshold in at least 30 percent of the
    replicates (the 70th percentile), not in the median replicate: a gene expressed in one sex (UTY, RPS4Y1) would
    otherwise be lost in a cohort with fewer than half males.

    Args:
        full_counts: (replicates, genes) summed counts of the whole replicate.

    Returns:
        ``(half_count_q70, expressed, gate)``: the 70th percentile over replicates of count / 2, genes with
        ``half_count_q70 >= EXPRESSED_GATE_HALF`` and genes with ``>= COUNT_GATE_HALF``.
    """
    half_q70 = np.quantile(full_counts, COUNT_GATE_QUANTILE, axis=0) / 2.0
    return half_q70, half_q70 >= EXPRESSED_GATE_HALF, half_q70 >= COUNT_GATE_HALF


@dataclass
class Halves:
    """Normalised halves of one stratum.

    Attributes:
        pairings: Three ``(x_A, x_B)`` pairs of (replicates, features) log2 CPM matrices.
        full: (replicates, features) log2 CPM of the whole replicate (latent factors, trend, composition check).
        norm_factors: TMM factor per replicate.
    """
    pairings: List[Tuple[np.ndarray, np.ndarray]]
    full: np.ndarray
    norm_factors: np.ndarray


def normalised_halves(quarter_counts: np.ndarray, quarter_lib: np.ndarray, norm_mask: np.ndarray) -> Halves:
    """Log2 CPM halves for the three pairings with the TMM factors of the full replicate.

    Args:
        quarter_counts: (replicates, 4, genes) counts of the expressed genes.
        quarter_lib: (replicates, 4) total counts of all genes per quarter.
        norm_mask: (genes,) genes the TMM factors are estimated on (the gate genes).

    Returns:
        Halves (features = all given genes).
    """
    full_counts = quarter_counts.sum(axis=1)
    full_lib = quarter_lib.sum(axis=1)
    factors = tmm_norm_factors(full_counts[:, norm_mask], full_lib)
    mean_lib = float(np.mean(full_lib * factors) / 2.0)
    pairings = []
    for (first_a, second_a), (first_b, second_b) in PAIRINGS:
        counts_a = quarter_counts[:, first_a] + quarter_counts[:, second_a]
        counts_b = quarter_counts[:, first_b] + quarter_counts[:, second_b]
        lib_a = (quarter_lib[:, first_a] + quarter_lib[:, second_a]) * factors
        lib_b = (quarter_lib[:, first_b] + quarter_lib[:, second_b]) * factors
        pairings.append((log2_cpm(counts_a, lib_a, mean_lib), log2_cpm(counts_b, lib_b, mean_lib)))
    return Halves(pairings, log2_cpm(full_counts, full_lib * factors, mean_lib * 2.0), factors)


def half_means(quarter_sum: np.ndarray, quarter_n: np.ndarray) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Halves of per-cell scores (cell mean variant): the mean over the cells of two quarters.

    Args:
        quarter_sum: (replicates, 4, features) sum of the cell scores per quarter.
        quarter_n: (replicates, 4, features) number of scored cells per quarter.

    Returns:
        Three ``(x_A, x_B)`` pairs; NaN where a half has no scored cell.
    """
    pairings = []
    for (first_a, second_a), (first_b, second_b) in PAIRINGS:
        with np.errstate(invalid='ignore', divide='ignore'):
            half_a = (quarter_sum[:, first_a] + quarter_sum[:, second_a]) / (quarter_n[:, first_a] + quarter_n[:, second_a])
            half_b = (quarter_sum[:, first_b] + quarter_sum[:, second_b]) / (quarter_n[:, first_b] + quarter_n[:, second_b])
        pairings.append((half_a, half_b))
    return pairings


# ---- the estimator -------------------------------------------------------------------------------------------------

def _basis(design: Optional[np.ndarray], n_rows: int) -> Tuple[np.ndarray, int]:
    """Orthonormal basis of ``[1, design]`` (rank revealing) and its rank."""
    columns = [np.ones((n_rows, 1))]
    if design is not None and design.size:
        columns.append(np.asarray(design, dtype=float).reshape(n_rows, -1))
    matrix = np.hstack(columns)
    left, singular, _ = np.linalg.svd(matrix, full_matrices=False)
    rank = int((singular > singular[0] * 1e-8).sum())
    return left[:, :rank], rank


def _cross(pairings: Sequence[Tuple[np.ndarray, np.ndarray]], rows: Optional[np.ndarray],
           design: Optional[np.ndarray], with_variance: bool = True) -> Tuple[np.ndarray, int, float, float]:
    """Mean over the pairings of the residual cross-product of the halves, the rank, and the half variances.

    Returns:
        ``(cross, rank, mean variance of A, mean variance of B)``: ``cross`` is ``sum_i r_A,i r_B,i`` per feature
        (divide by ``n - rank``); the variances are of the intercept-only residuals per feature (arrays).
    """
    cross_total = 0.0
    var_a = var_b = 0.0
    for first, second in pairings:
        if rows is not None:
            first, second = first[rows], second[rows]
        n_rows = first.shape[0]
        basis, rank = _basis(design, n_rows)
        cross_total = cross_total + (first * second).sum(axis=0) - ((basis.T @ first) * (basis.T @ second)).sum(axis=0)
        if with_variance:
            mean_a, mean_b = first.mean(axis=0), second.mean(axis=0)
            var_a = var_a + ((first - mean_a) ** 2).sum(axis=0)
            var_b = var_b + ((second - mean_b) ** 2).sum(axis=0)
    k = len(pairings)
    return cross_total / k, rank, var_a / k, var_b / k


def donor_variance(pairings: Sequence[Tuple[np.ndarray, np.ndarray]], rows: Optional[np.ndarray] = None
                   ) -> Dict[str, np.ndarray]:
    """``V_donor``, split-half correlation and reliability per feature (intercept-only design).

    Args:
        pairings: Three ``(x_A, x_B)`` pairs, (replicates, features).
        rows: Replicate rows to use (default all).

    Returns:
        ``v_donor`` (covariance of the halves), ``var_half`` (mean variance of one half), ``r_half`` (correlation of the
        halves; NaN for a feature without variance) and ``reliability`` (Spearman-Brown ``2r / (1 + r)`` of ``r_half``,
        clipped to [0, 1]).
    """
    n_rows = pairings[0][0].shape[0] if rows is None else len(rows)
    cross, _, var_a, var_b = _cross(pairings, rows, None)
    denominator = n_rows - 1
    v_donor = cross / denominator
    var_half = (var_a + var_b) / 2.0 / denominator
    with np.errstate(invalid='ignore', divide='ignore'):
        r_half = v_donor / np.sqrt(var_a / denominator * var_b / denominator)
        reliability = np.clip(2.0 * r_half / (1.0 + r_half), 0.0, 1.0)
    reliability = np.where(np.isfinite(r_half), reliability, np.nan)
    return {'v_donor': v_donor, 'var_half': var_half, 'r_half': r_half, 'reliability': reliability}


def residual_variance(pairings: Sequence[Tuple[np.ndarray, np.ndarray]], design: np.ndarray,
                      rows: Optional[np.ndarray] = None) -> Tuple[np.ndarray, int]:
    """``V_unexplained = r_A' r_B / (n - p)`` per feature after regressing both halves on ``[1, design]``.

    Args:
        pairings: Three ``(x_A, x_B)`` pairs.
        design: (rows, columns) design without intercept (rows aligned with ``rows``).
        rows: Replicate rows used.

    Returns:
        ``(v_unexplained, rank)`` where rank counts the intercept.
    """
    n_rows = pairings[0][0].shape[0] if rows is None else len(rows)
    cross, rank, _, _ = _cross(pairings, rows, design, with_variance=False)
    return cross / (n_rows - rank), rank


@dataclass
class Term:
    """One design term (a group of columns): ``values`` is (n,) or (n, k) with NaN for a missing replicate."""
    name: str
    values: np.ndarray
    names: List[str] = field(default_factory=list)   # column names (technical block), for the manifest

    @property
    def rung(self) -> str:
        return TERM_RUNG[self.name]

    def matrix(self) -> np.ndarray:
        """Columns of the term as (n, k)."""
        return np.asarray(self.values, dtype=float).reshape(len(self.values), -1)


@dataclass
class RungInfo:
    """What one rung of the ladder used (written to the manifest)."""
    name: str
    terms: List[str]
    n: int
    p: int
    residual_df: int
    computed: bool
    reason: Optional[str] = None


def _term_design(terms: Sequence[Term]) -> Tuple[np.ndarray, np.ndarray]:
    """Rows without a missing value and the stacked design of ``terms`` on those rows."""
    matrix = np.hstack([term.matrix() for term in terms])
    complete = ~np.isnan(matrix).any(axis=1)
    return np.flatnonzero(complete), matrix[complete]


def n_latent_factors(n_replicates: int) -> int:
    """Number of latent factors: 0 below ``FACTOR_MIN_REPLICATES`` replicates, then 3 (n < 60), 4 (n < 100), 5."""
    if n_replicates < FACTOR_MIN_REPLICATES:
        return 0
    return 3 if n_replicates < 60 else (4 if n_replicates < 100 else 5)


def latent_factors(full: np.ndarray, design: Optional[np.ndarray], n_factors: int) -> np.ndarray:
    """First PCs of the replicate matrix after removing ``[1, design]`` (columns scaled to unit variance).

    Args:
        full: (replicates, genes) log2 CPM of the whole replicates (use well expressed genes).
        design: Design of the deepest rung (rows aligned with ``full``) or None.
        n_factors: Number of PCs.

    Returns:
        (replicates, n_factors) PC scores, unit variance.
    """
    n_rows = full.shape[0]
    basis, _ = _basis(design, n_rows)
    residual = full - basis @ (basis.T @ full)
    scale = residual.std(axis=0)
    keep = scale > 1e-9
    scaled = residual[:, keep] / scale[keep]
    gram = scaled @ scaled.T
    values, vectors = np.linalg.eigh(gram)
    order = np.argsort(values)[::-1][:n_factors]
    scores = vectors[:, order] * np.sqrt(np.maximum(values[order], 0.0))
    spread = scores.std(axis=0)
    return scores / np.where(spread > 0, spread, 1.0)


def _weighted_cross(product: np.ndarray, first: np.ndarray, second: np.ndarray, design: Optional[np.ndarray],
                    weights: np.ndarray) -> Tuple[np.ndarray, int]:
    """Residual cross-product with integer bootstrap weights (rows drawn ``weights`` times) and its rank."""
    columns = [np.ones((len(weights), 1))]
    if design is not None and design.size:
        columns.append(design)
    matrix = np.hstack(columns)
    weighted = matrix * weights[:, None]
    gram = matrix.T @ weighted
    pinv = np.linalg.pinv(gram, rcond=1e-10)
    rank = int(np.linalg.matrix_rank(gram, tol=1e-8 * max(np.linalg.norm(gram, 2), 1e-300)))
    right_a = weighted.T @ first
    right_b = matrix.T @ (second * weights[:, None])
    return weights @ product - (right_a * (pinv @ right_b)).sum(axis=0), rank


def _boot_stats(pairings, products, squares, rows_all: np.ndarray, rung_rows: Optional[np.ndarray],
                rung_design: Optional[np.ndarray], rng: np.random.Generator) -> Dict[str, np.ndarray]:
    """One bootstrap replicate over replicates (rows resampled with replacement, integer weights)."""
    weights_all = np.bincount(rng.integers(0, len(rows_all), len(rows_all)), minlength=len(rows_all)).astype(float)
    out: Dict[str, np.ndarray] = {}
    cross = var_a = var_b = 0.0
    for (first, second), product, (square_a, square_b) in zip(pairings, products, squares):
        n_rows = weights_all.sum()
        mean_a, mean_b = weights_all @ first / n_rows, weights_all @ second / n_rows
        cross = cross + weights_all @ product - n_rows * mean_a * mean_b
        var_a = var_a + weights_all @ square_a - n_rows * mean_a ** 2
        var_b = var_b + weights_all @ square_b - n_rows * mean_b ** 2
    k = len(pairings)
    denominator = n_rows - 1
    v_donor = cross / k / denominator
    with np.errstate(invalid='ignore', divide='ignore'):
        r_half = cross / k / np.sqrt(var_a / k * var_b / k)
        reliability = np.clip(2.0 * r_half / (1.0 + r_half), 0.0, 1.0)
    out['v_donor'], out['reliability'] = v_donor, reliability
    if rung_rows is not None:
        n_sub = len(rung_rows)
        weights = np.bincount(rng.integers(0, n_sub, n_sub), minlength=n_sub).astype(float)
        cross_rung = cross_plain = 0.0
        rank = 1
        for (first, second), product in zip(pairings, products):
            first, second, product = first[rung_rows], second[rung_rows], product[rung_rows]
            with_design, rank = _weighted_cross(product, first, second, rung_design, weights)
            cross_rung = cross_rung + with_design
            cross_plain = cross_plain + _weighted_cross(product, first, second, None, weights)[0]
        v_unexp = cross_rung / k / (n_sub - rank)
        v_plain = cross_plain / k / (n_sub - 1)
        out['v_unexplained'] = v_unexp
        with np.errstate(invalid='ignore', divide='ignore'):
            out['share_design'] = np.where(v_plain > 0, 1.0 - v_unexp / v_plain, np.nan)
    return out


def robust_loess(x: np.ndarray, y: np.ndarray, fraction: float = 0.3, n_grid: int = 80, n_iter: int = 3) -> np.ndarray:
    """Robust local linear (tricube kernel, bisquare reweighting) trend of ``y`` on ``x``, evaluated at ``x``.

    Evaluated at ``n_grid`` quantiles of ``x`` and interpolated (the usual loess shortcut). Used for the
    mean-variance trend of ``excess_z``.
    """
    grid = np.unique(np.quantile(x, np.linspace(0.0, 1.0, n_grid)))
    n_neighbours = int(min(len(x), max(math.ceil(fraction * len(x)), 10)))
    robust = np.ones(len(x))
    fitted = np.full(len(x), np.nan)
    for _ in range(n_iter + 1):
        values = np.empty(len(grid))
        for position, centre in enumerate(grid):
            distance = np.abs(x - centre)
            bandwidth = max(np.partition(distance, n_neighbours - 1)[n_neighbours - 1], 1e-12)
            weight = np.clip(1.0 - (distance / bandwidth) ** 3, 0.0, None) ** 3 * robust
            total = weight.sum()
            if total <= 0:
                values[position] = np.average(y)
                continue
            mean_x = weight @ x / total
            centred = x - mean_x
            spread = weight @ centred ** 2
            slope = (weight @ (centred * y) / spread) if spread > 1e-12 else 0.0
            values[position] = weight @ y / total + slope * (centre - mean_x)
        fitted = np.interp(x, grid, values)
        residual = y - fitted
        scale = np.median(np.abs(residual))
        if scale <= 0:
            break
        u = np.clip(residual / (6.0 * scale), -1.0, 1.0)
        robust = (1.0 - u ** 2) ** 2
    return fitted


def excess_scores(v_values: np.ndarray, mean_expr: np.ndarray, usable: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Within-unit ranking of a variance: ``excess_z`` and its relative upper tail ``p_excess``.

    A robust loess of ``log(V)`` on the mean expression is fitted on the usable features with ``V > 0``; the residual
    divided by 1.4826 times its MAD is ``excess_z``; ``p_excess = #features with residual >= this / N`` (the feature itself included).
    These are ranks of one unit, not tests (the trend includes genuinely variable genes).

    Args:
        v_values: Variance per feature.
        mean_expr: Mean log2 CPM per feature.
        usable: Features allowed into the trend and the ranking (gate and reliability floor).

    Returns:
        ``(excess_z, p_excess)`` with NaN outside the usable positive features.
    """
    z = np.full(len(v_values), np.nan)
    p_values = np.full(len(v_values), np.nan)
    mask = usable & np.isfinite(v_values) & (v_values > 0) & np.isfinite(mean_expr)
    if mask.sum() < 30:
        return z, p_values
    log_v = np.log(v_values[mask])
    residual = log_v - robust_loess(mean_expr[mask], log_v)
    mad = np.median(np.abs(residual - np.median(residual))) * 1.4826
    if mad <= 0:
        return z, p_values
    z[mask] = residual / mad
    sorted_residual = np.sort(residual)
    n_used = len(residual)
    p_values[mask] = (n_used - np.searchsorted(sorted_residual, residual, side='left')) / n_used
    return z, p_values


def shapley_shares(pairings, rows: np.ndarray, designs: Dict[str, np.ndarray], v_donor_rows: np.ndarray
                   ) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """LMG (Shapley) shares of the explained fraction ``1 - V_unexplained(S) / V_donor`` over design terms.

    Args:
        pairings: Three ``(x_A, x_B)`` pairs (all replicates).
        rows: Replicates used (complete for every term).
        designs: Term name -> (len(rows), k) columns.
        v_donor_rows: ``V_donor`` of the rows per feature.

    Returns:
        ``(shares, total)``: share per term name and the explained fraction of the full model; the shares add up to
        ``total`` (unclipped; sampling noise can make a small share negative).
    """
    names = list(designs)
    k = len(names)
    subset_value = np.zeros((1 << k, len(v_donor_rows)))
    sub_pairings = [(first[rows], second[rows]) for first, second in pairings]
    with np.errstate(invalid='ignore', divide='ignore'):
        for mask in range(1, 1 << k):
            members = [designs[names[bit]] for bit in range(k) if mask >> bit & 1]
            v_unexp, _ = residual_variance(sub_pairings, np.hstack(members))
            subset_value[mask] = 1.0 - v_unexp / v_donor_rows
    factorial = [math.factorial(i) for i in range(k + 1)]
    shares: Dict[str, np.ndarray] = {}
    for bit, name in enumerate(names):
        total = np.zeros(len(v_donor_rows))
        for mask in range(1 << k):
            if mask >> bit & 1:
                continue
            size = bin(mask).count('1')
            total += factorial[size] * factorial[k - size - 1] / factorial[k] * (
                subset_value[mask | (1 << bit)] - subset_value[mask])
        shares[name] = total
    return shares, subset_value[(1 << k) - 1]


@dataclass
class VariabilityResult:
    """Output of ``estimate_variability``.

    Attributes:
        table: Feature-indexed columns (see ``estimate_variability``).
        rungs: One ``RungInfo`` per rung of the ladder (computed or not).
        factors: (n, K) latent factors on the deepest rung's replicates (NaN elsewhere); None without factors.
        deepest: Name of the deepest rung with ``share_design``, None when none was computed.
        n_boot: Bootstrap replicates actually used.
    """
    table: pd.DataFrame
    rungs: List[RungInfo]
    factors: Optional[np.ndarray]
    deepest: Optional[str]
    n_boot: int


def estimate_variability(pairings: Sequence[Tuple[np.ndarray, np.ndarray]], terms: Sequence[Term],
                         mean_expr: Optional[np.ndarray] = None, gate: Optional[np.ndarray] = None,
                         full: Optional[np.ndarray] = None, factors: Optional[np.ndarray] = None,
                         n_boot: int = N_BOOT, seed: int = 1, rank: bool = True) -> VariabilityResult:
    """Donor variability of every feature of one stratum.

    Args:
        pairings: Three ``(x_A, x_B)`` pairs (replicates x features), see ``Halves``.
        terms: Design terms of the replicates (NaN rows are dropped from the rungs that need them). Rung of a term:
            ``TERM_RUNG``.
        mean_expr: (features,) mean log2 CPM, needed for the ``excess_*`` columns of genes.
        gate: (features,) boolean, features that may enter the ranking (default all).
        full: (replicates, genes) log2 CPM of whole replicates, source of the latent factors (genes only).
        factors: Precomputed latent factors (modules reuse the genes' factors); wins over ``full``.
        n_boot: Bootstrap replicates for the confidence intervals (reduced for big problems).
        seed: Bootstrap seed.
        rank: Compute ``excess_z`` / ``p_excess`` (genes; modules have no mean-variance trend).

    Returns:
        VariabilityResult. Columns of ``table``: ``v_donor``, ``v_donor_lo/hi``, ``sd_donor``, ``var_half``, ``r_half``,
        ``reliability``, ``reliability_lo/hi``; per computed rung ``R``: ``v_unexplained_R``, ``share_design_R``
        (clipped to [0, 1]); ``v_unexplained_ci_rung`` / ``share_design_lo/hi`` at the deepest rung before factors;
        ``lmg_<term>`` (when allowed); ``share_<block>`` increments; ``excess_z_*`` and ``p_excess_*``.

    Raises:
        ValueError: fewer than ``MIN_REPLICATES`` replicates.
    """
    n_rows, n_features = pairings[0][0].shape
    if n_rows < MIN_REPLICATES:
        raise ValueError(f'{n_rows} replicates (< {MIN_REPLICATES})')
    rng = np.random.default_rng(seed)
    base = donor_variance(pairings)
    products = [first * second for first, second in pairings]
    table = pd.DataFrame({'v_donor': base['v_donor'], 'var_half': base['var_half'], 'r_half': base['r_half'],
                          'reliability': base['reliability']})
    table['sd_donor'] = np.sqrt(np.maximum(table['v_donor'], 0.0))
    rungs: List[RungInfo] = []
    cumulative: List[Term] = []
    previous_n_terms = 0
    deepest: Optional[str] = None
    deepest_rows = deepest_design = None
    shares_by_rung: Dict[str, np.ndarray] = {}
    deepest_terms: List[Term] = []
    for rung in RUNGS:
        cumulative.extend(term for term in terms if term.rung == rung)
        names = [term.name for term in cumulative]
        if len(cumulative) == previous_n_terms:
            rungs.append(RungInfo(rung, names, 0, 0, 0, False, 'no new term'))
            continue
        previous_n_terms = len(cumulative)
        rows, design = _term_design(cumulative)
        n_used = len(rows)
        rank_x = _basis(design, n_used)[1] if n_used else 0
        residual_df = n_used - rank_x
        if residual_df < SHARE_MIN_RESIDUAL_DF:
            rungs.append(RungInfo(rung, names, n_used, rank_x, residual_df, False,
                                  f'n - p = {residual_df} (< {SHARE_MIN_RESIDUAL_DF})'))
            continue
        all_rows = len(rows) == n_rows
        v_unexp, _ = residual_variance(pairings, design, None if all_rows else rows)
        v_rung = base['v_donor'] if all_rows else donor_variance(pairings, rows)['v_donor']
        with np.errstate(invalid='ignore', divide='ignore'):
            share_raw = np.where(v_rung > 0, 1.0 - v_unexp / v_rung, np.nan)
        table[f'v_unexplained_{rung}'] = v_unexp
        table[f'share_design_{rung}'] = np.clip(share_raw, 0.0, 1.0)
        shares_by_rung[rung] = share_raw
        rungs.append(RungInfo(rung, names, n_used, rank_x, residual_df, True))
        deepest, deepest_rows, deepest_design, deepest_terms = rung, rows, design, list(cumulative)
    # latent factors on the deepest rung
    n_factor = n_latent_factors(len(deepest_rows) if deepest_rows is not None else n_rows)
    factor_matrix: Optional[np.ndarray] = None
    if deepest is not None and n_factor > 0 and (factors is not None or full is not None):
        if factors is None:
            fitted = latent_factors(full[deepest_rows], deepest_design, n_factor)
            factors = np.full((n_rows, n_factor), np.nan)
            factors[deepest_rows] = fitted
        factor_matrix = factors
        factor_rows = factors[deepest_rows]
        if np.isnan(factor_rows).any():
            rungs.append(RungInfo(FACTOR_RUNG, [], len(deepest_rows), 0, 0, False, 'factors missing for some replicates'))
        else:
            design_f = np.hstack([deepest_design, factor_rows])
            rank_f = _basis(design_f, len(deepest_rows))[1]
            df_f = len(deepest_rows) - rank_f
            if df_f >= SHARE_MIN_RESIDUAL_DF:
                all_rows = len(deepest_rows) == n_rows
                v_f, _ = residual_variance(pairings, design_f, None if all_rows else deepest_rows)
                v_rung = base['v_donor'] if all_rows else donor_variance(pairings, deepest_rows)['v_donor']
                with np.errstate(invalid='ignore', divide='ignore'):
                    share_raw = np.where(v_rung > 0, 1.0 - v_f / v_rung, np.nan)
                table[f'v_unexplained_{FACTOR_RUNG}'] = v_f
                table[f'share_design_{FACTOR_RUNG}'] = np.clip(share_raw, 0.0, 1.0)
                shares_by_rung[FACTOR_RUNG] = share_raw
                rungs.append(RungInfo(FACTOR_RUNG, [t.name for t in deepest_terms] + ['latent'],
                                      len(deepest_rows), rank_f, df_f, True))
            else:
                rungs.append(RungInfo(FACTOR_RUNG, [], len(deepest_rows), rank_f, df_f, False, f'n - p = {df_f}'))
    elif n_factor == 0 and deepest is not None:
        rungs.append(RungInfo(FACTOR_RUNG, [], len(deepest_rows), 0, 0, False, f'n < {FACTOR_MIN_REPLICATES}'))
    # block increments
    def _raw(rung: str) -> Optional[np.ndarray]:
        return shares_by_rung.get(rung)
    if _raw('D0') is not None:
        table['share_technical'] = np.clip(_raw('D0'), 0.0, 1.0)
    computed = [rung for rung in RUNGS if rung in shares_by_rung]
    if _raw('D1') is not None and _raw('D0') is not None:
        table['share_covariates'] = np.clip(_raw('D1') - _raw('D0'), 0.0, 1.0)
    if deepest is not None and deepest not in ('D0', 'D1') and _raw('D1') is not None:
        table['share_facets'] = np.clip(_raw(deepest) - _raw('D1'), 0.0, 1.0)
    if FACTOR_RUNG in shares_by_rung and deepest is not None:
        table['share_latent'] = np.clip(shares_by_rung[FACTOR_RUNG] - shares_by_rung[deepest], 0.0, 1.0)
    # per-term shares
    lmg_rung = None
    if deepest is not None:
        rung_info = next(info for info in rungs if info.name == deepest)
        if rung_info.residual_df >= TERM_SHARE_MIN_RESIDUAL_DF:
            designs: Dict[str, np.ndarray] = {}
            for term in deepest_terms:
                designs[term.name] = term.matrix()[deepest_rows]
            all_rows = len(deepest_rows) == n_rows
            v_rows = base['v_donor'] if all_rows else donor_variance(pairings, deepest_rows)['v_donor']
            shares, _ = shapley_shares(pairings, deepest_rows, designs, v_rows)
            for name, values in shares.items():
                table[f'lmg_{name}'] = values
            lmg_rung = deepest
    # bootstrap confidence intervals
    n_boot_used = 0
    if n_boot > 0:
        work = n_rows * n_features * 48.0
        n_boot_used = int(np.clip(BOOT_BUDGET / max(work, 1.0), MIN_BOOT, n_boot))
        boot_design = deepest_design
        boot_rows = deepest_rows
        replicates: Dict[str, List[np.ndarray]] = {}
        squares = [(first * first, second * second) for first, second in pairings]
        for _ in range(n_boot_used):
            stats = _boot_stats(pairings, products, squares, np.arange(n_rows), boot_rows, boot_design, rng)
            for key, value in stats.items():
                replicates.setdefault(key, []).append(value)
        with _quiet():
            for key, column in (('v_donor', 'v_donor'), ('reliability', 'reliability')):
                stacked = np.vstack(replicates[key])
                table[f'{column}_lo'] = np.nanquantile(stacked, 0.025, axis=0)
                table[f'{column}_hi'] = np.nanquantile(stacked, 0.975, axis=0)
            if 'share_design' in replicates:
                stacked = np.vstack(replicates['share_design'])
                table['share_design_lo'] = np.clip(np.nanquantile(stacked, 0.025, axis=0), 0.0, 1.0)
                table['share_design_hi'] = np.clip(np.nanquantile(stacked, 0.975, axis=0), 0.0, 1.0)
                stacked = np.vstack(replicates['v_unexplained'])
                table['v_unexplained_lo'] = np.nanquantile(stacked, 0.025, axis=0)
                table['v_unexplained_hi'] = np.nanquantile(stacked, 0.975, axis=0)
    # excess ranking
    if rank and mean_expr is not None and n_rows >= RANK_MIN_REPLICATES:
        usable = (np.ones(n_features, bool) if gate is None else gate) & (table['reliability'].to_numpy() >= RELIABILITY_FLOOR)
        for rung in ('raw',) + tuple(computed) + ((FACTOR_RUNG,) if FACTOR_RUNG in shares_by_rung else ()):
            column = 'v_donor' if rung == 'raw' else f'v_unexplained_{rung}'
            z_values, p_values = excess_scores(table[column].to_numpy(), mean_expr, usable)
            table[f'excess_z_{rung}'] = z_values
            table[f'p_excess_{rung}'] = p_values
    table.attrs['lmg_rung'] = lmg_rung
    return VariabilityResult(table, rungs, factor_matrix, deepest, n_boot_used)


# ---- design terms and technical covariates -------------------------------------------------------------------------

def robust_z(values: np.ndarray, clip: float = 4.0) -> np.ndarray:
    """Median / MAD z-score clipped to ``+-clip`` (falls back to the SD when the MAD is 0); constant input gives 0."""
    values = np.asarray(values, dtype=float)
    with _quiet():
        centre = np.nanmedian(values)
        spread = np.nanmedian(np.abs(values - centre)) * 1.4826
        if not spread > 0:
            spread = np.nanstd(values)
    if not spread > 0:
        return np.zeros_like(values)
    return np.clip((values - centre) / spread, -clip, clip)


def technical_terms(n_cells: np.ndarray, total_umi: np.ndarray, total_nnz: np.ndarray, mito_fraction: np.ndarray,
                    ambient: Dict[str, np.ndarray], long_gene_index: Optional[np.ndarray],
                    composition: Optional[np.ndarray]) -> List[Term]:
    """The technical block of one stratum as D0 terms.

    Args:
        n_cells: Cells per replicate.
        total_umi: Counts per replicate (all genes).
        total_nnz: Detected (cell, gene) pairs per replicate.
        mito_fraction: Mitochondrial counts / all counts.
        ambient: Per foreign lineage (``AMBIENT_PANELS``) the mean log2(CPM + 1) of its marker panel: one column each.
        long_gene_index: Mean log2 CPM of the longest minus the shortest expressed genes (None without annotation).
        composition: (replicates, k) composition PCs or None.

    Returns:
        ``technical`` (log cells, log UMI per cell, genes per cell, mito fraction, one ambient score per foreign lineage,
        long gene index; constant columns dropped) and ``composition`` terms, robust z-scored.
    """
    columns = {
        'log_n_cells': np.log(np.maximum(n_cells, 1)),
        'log_umi_per_cell': np.log(np.maximum(total_umi, 1) / np.maximum(n_cells, 1)),
        'log_genes_per_cell': np.log(np.maximum(total_nnz, 1) / np.maximum(n_cells, 1)),
        'mito_fraction': np.sqrt(np.maximum(mito_fraction, 0.0)),
        **{f'ambient_{lineage}': score for lineage, score in ambient.items()},
    }
    if long_gene_index is not None:
        columns['long_gene_index'] = long_gene_index
    with _quiet():
        used = [name for name, values in columns.items() if np.nanstd(values) > 1e-9]
    out: List[Term] = []
    if used:
        out.append(Term('technical', np.column_stack([robust_z(columns[name]) for name in used]), used))
    if composition is not None and composition.size:
        out.append(Term('composition', np.column_stack([robust_z(col) for col in composition.T]),
                        [f'composition_pc{i + 1}' for i in range(composition.shape[1])]))
    return out


def composition_pcs(proportions: pd.DataFrame, max_pcs: int = 3) -> Optional[np.ndarray]:
    """First PCs of the replicate x author cluster proportion matrix (centred log ratio), None when there is no structure."""
    if proportions.shape[1] < 2:
        return None
    clr = np.log(proportions.to_numpy() + 0.5 / np.maximum(1.0, proportions.shape[0]))
    clr = clr - clr.mean(axis=1, keepdims=True)
    centred = clr - clr.mean(axis=0)
    n_pcs = min(max_pcs, proportions.shape[1] - 1, proportions.shape[0] - 2)
    if n_pcs < 1:
        return None
    left, singular, _ = np.linalg.svd(centred, full_matrices=False)
    return left[:, :n_pcs] * singular[:n_pcs]


def lineage_of_group(group: str) -> Optional[str]:
    """Lineage (key of ``AMBIENT_PANELS``) of a cell type group name, None when it is not one of the four."""
    lowered = str(group).lower()
    for token, lineage in _LINEAGE_OF_GROUP:
        if token in lowered:
            return lineage
    return None


def foreign_panels(group: str) -> Dict[str, List[str]]:
    """Marker panels of every lineage except the stratum's own (lineage name -> genes): one ambient score each."""
    own = lineage_of_group(group)
    return {lineage: list(genes) for lineage, genes in AMBIENT_PANELS.items() if lineage != own}


def foreign_panel(group: str) -> List[str]:
    """Marker genes of every lineage except the stratum's own, as one list."""
    return [gene for genes in foreign_panels(group).values() for gene in genes]


def covariate_terms(donors: pd.DataFrame, scope_is_xenograft: bool,
                    min_coverage: float = 0.9) -> Tuple[List[Term], Dict[str, str]]:
    """Terms D1 to D4 of the replicates of one stratum from the donor table.

    Args:
        donors: One row per replicate in stratum order, with the columns of ``ant-facet-test`` ``build_donor_table``
            (``sex``, ``host_sex``, ``age``, ``pmi``, ``batch``, ``disease``, ``app_nlgf``, ``apoe4_carrier``, ``braak``).
        scope_is_xenograft: Host sex is the sex covariate and the host genotype (``app_nlgf``) is the ``disease`` term.
        min_coverage: A term needs a value for at least this fraction of the replicates (the facet tests' rule).

    Returns:
        ``(terms, omitted)``: terms with at least two distinct values and enough coverage, and a reason per omitted one.
    """
    terms: List[Term] = []
    omitted: Dict[str, str] = {}
    n_rows = len(donors)

    def add(name: str, values: np.ndarray, kind: str) -> None:
        known = ~np.isnan(values)
        if known.mean() < min_coverage:
            omitted[name] = f'known for {known.mean():.0%} (< {min_coverage:.0%})'
            return
        distinct = np.unique(values[known])
        if len(distinct) < 2 or (kind == 'binary' and min(np.sum(values[known] == v) for v in distinct) < 3):
            omitted[name] = 'fewer than 2 values (or a level with < 3 replicates)'
            return
        if kind == 'numeric':
            values = np.where(known, (values - np.nanmean(values)) / (np.nanstd(values) or 1.0), np.nan)
        terms.append(Term(name, values))

    def get(column: str) -> np.ndarray:
        return donors[column].to_numpy() if column in donors else np.full(n_rows, None, dtype=object)

    sex = get('host_sex' if scope_is_xenograft and 'host_sex' in donors else 'sex')
    add('sex', np.array([{'m': 1.0, 'f': 0.0}.get(v, np.nan) for v in sex]), 'binary')
    add('age', pd.to_numeric(pd.Series(get('age')), errors='coerce').to_numpy(dtype=float), 'numeric')
    add('pmi', pd.to_numeric(pd.Series(get('pmi')), errors='coerce').to_numpy(dtype=float), 'numeric')
    batch = pd.Series(get('batch')).astype('object')
    if batch.notna().all() and batch.nunique() >= 2:
        n_levels = batch.nunique()
        if n_levels <= max(2, n_rows // 4):
            dummies = pd.get_dummies(batch.astype(str), drop_first=True).to_numpy(dtype=float)
            terms.append(Term('batch', dummies))
        else:
            omitted['batch'] = f'{n_levels} levels for {n_rows} replicates'
    if scope_is_xenograft:
        status = get('app_nlgf')
        add('disease', np.array([{'nlgf': 1.0, 'control': 0.0}.get(v, np.nan) for v in status]), 'binary')
    else:
        status = get('disease')
        add('disease', np.array([{'AD': 1.0, 'control': 0.0}.get(v, np.nan) for v in status]), 'binary')
        carrier = get('apoe4_carrier')
        add('apoe4', np.array([{'e4_carrier': 1.0, 'noncarrier': 0.0}.get(v, np.nan) for v in carrier]), 'binary')
        add('braak', pd.to_numeric(pd.Series(get('braak')), errors='coerce').to_numpy(dtype=float), 'numeric')
    return terms, omitted


def module_weights(module_genes: Sequence[np.ndarray], controls: Sequence[np.ndarray], n_genes: int) -> np.ndarray:
    """Weight matrix (genes x modules) of the pseudobulk module score: mean of the module genes minus mean of controls.

    The score is linear in log2 CPM, so the score of a half is ``x_half @ W``.
    """
    weights = np.zeros((n_genes, len(module_genes)))
    for column, (genes, control) in enumerate(zip(module_genes, controls)):
        weights[genes, column] += 1.0 / len(genes)
        weights[control, column] -= 1.0 / len(control)
    return weights
