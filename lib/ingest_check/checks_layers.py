"""Expression layer checks: raw counts, log-normalised X, their consistency, gene ids."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

import numpy as np

from . import Outcome, check, fail, note, ok, skip, warn
from . import sampling, thresholds

_ENSEMBL = re.compile(r'^ENS[A-Z]{0,6}[GTP]\d{6,}(\.\d+)?$')


def _is_counts(sample: np.ndarray) -> bool:
    """Non-negative whole numbers with at least one non-zero value."""
    if sample.size == 0 or not np.any(sample):
        return False
    return bool(np.all(sample >= 0) and np.all(sample == np.round(sample)))


def find_counts_layer(ctx) -> Tuple[Optional[str], List[str]]:
    """``(name of the first raw-counts matrix other than X, names examined)``; cached on the context."""
    if 'counts_layer' not in ctx._cache:  # noqa: SLF001 - shared cache of the checks
        names = [name for name in sampling.matrix_paths(ctx.h5ad_path) if name != 'X']
        found = None
        for name in sampling.counts_candidates(names):
            _, sample = ctx.sample_matrix(name, thresholds.SAMPLE_CELLS, thresholds.SAMPLE_SEED)
            if _is_counts(sample):
                found = name
                break
        ctx._cache['counts_layer'] = (found, names)  # noqa: SLF001
    return ctx._cache['counts_layer']  # noqa: SLF001


@check('layers.counts', needs=('h5ad',))
def layers_counts(ctx) -> Outcome:
    """A raw-counts layer exists (sampled: 2,000 cells, seed 0), or ``counts_unavailable`` gives the reason."""
    found, names = find_counts_layer(ctx)
    reason = str(ctx.md.get('counts_unavailable') or '').strip()
    if found:
        return ok(f'layer {found!r} holds raw counts', layer=found, layers=names)
    if reason:
        return warn('no raw counts layer; allowed by counts_unavailable', 'The reason is shown on the dataset page.',
                    counts_unavailable=reason, layers=names)
    return fail('no layer with raw integer counts' + (f' (looked at: {", ".join(names)})' if names else ''),
                'Store raw counts as adata.layers["counts"] (non-negative integers). If the authors only share '
                'normalised data, say so with a front matter key counts_unavailable: <reason>.', layers=names)


@check('layers.x_lognorm', needs=('h5ad',))
def layers_x_lognorm(ctx) -> Outcome:
    """X is log-normalised: not integers, not scaled (no negatives), not on a raw scale."""
    _, sample = ctx.sample_matrix('X', thresholds.SAMPLE_CELLS, thresholds.SAMPLE_SEED)
    if not np.any(sample):
        return fail('X sample is all zeros', 'Check the matrix that was written to X.')
    low, high = float(sample.min()), float(sample.max())
    details = {'min': round(low, 4), 'max': round(high, 4), 'n_sampled_cells': int(sample.shape[0])}
    if low < 0:
        return fail('X has negative values: it looks scaled (z-scores or residuals)',
                    'Put log-normalised expression in X (log1p of counts per 10,000, not scaled).', **details)
    if _is_counts(sample):
        return fail('X holds whole numbers: it looks like raw counts',
                    'Move the counts to adata.layers["counts"] and put log1p(counts per 10,000) in X.', **details)
    if high >= thresholds.LOGNORM_MAX_VALUE:
        return fail(f'X maximum is {high:.1f}: not on a log scale',
                    'X must be log-normalised (values rarely above 15).', **details)
    row_sums = np.expm1(sample).sum(axis=1)
    row_sums = row_sums[row_sums > 0]
    cv = float(row_sums.std() / row_sums.mean()) if len(row_sums) > 1 and row_sums.mean() > 0 else float('nan')
    details['expm1_rowsum_cv'] = None if np.isnan(cv) else round(cv, 4)
    declared = str(ctx.md.get('normalisation') or '').strip()
    if not np.isnan(cv) and cv <= thresholds.LOGNORM_ROWSUM_CV_MAX:
        return ok('X is a log1p library-size normalisation', **details)
    if declared:
        return note(f'X is not a plain library-size normalisation; declared: {declared}', normalisation=declared, **details)
    return warn('X is on a log scale, but expm1(X) row sums vary '
                f'(CV {cv:.2f}); the normalisation cannot be confirmed',
                'If this is intended (SCTransform, vst, another method), declare it with normalisation: <method> '
                'in the .md.', **details)


@check('layers.consistency', needs=('h5ad',))
def layers_consistency(ctx) -> Outcome:
    """X matches log1p(counts per 10,000) on 1,000 cells (median Pearson r)."""
    found, _ = find_counts_layer(ctx)
    if not found:
        return skip('no counts layer to compare with X')
    declared = str(ctx.md.get('normalisation') or '').strip()
    if declared:
        return skip(f'normalisation declared ({declared}): X is not expected to follow log1p(CP10k(counts))')
    indices, counts = ctx.sample_matrix(found, thresholds.CONSISTENCY_CELLS, thresholds.SAMPLE_SEED)
    _, expression = ctx.sample_matrix('X', thresholds.CONSISTENCY_CELLS, thresholds.SAMPLE_SEED)
    if counts.shape != expression.shape:
        return skip('counts and X differ in shape')
    sums = counts.sum(axis=1)
    keep = sums > 0
    normalised = np.log1p(counts[keep] / sums[keep, None] * thresholds.CONSISTENCY_TARGET_SUM)
    expression = expression[keep]
    correlations = []
    for expected, observed in zip(normalised, expression):
        if expected.std() > 0 and observed.std() > 0:
            correlations.append(float(np.corrcoef(expected, observed)[0, 1]))
    if not correlations:
        return skip('no cell with variation to compare')
    median = float(np.median(correlations))
    details = {'median_r': round(median, 4), 'n_cells': len(correlations), 'counts_layer': found}
    if median >= thresholds.CONSISTENCY_PASS:
        return ok(f'median r {median:.3f} between X and log1p(CP10k(counts))', **details)
    fix = ('X and the counts layer must come from the same cells and genes: normalise the counts with '
           'log1p(counts / total * 10,000) and write both. If X deliberately uses another normalisation (vst, '
           'SCTransform, ...), declare it with normalisation: <method> in the .md.')
    if median >= thresholds.CONSISTENCY_WARN:
        return warn(f'median r {median:.3f}: X deviates from log1p(CP10k(counts))', fix, **details)
    return fail(f'median r {median:.3f}: X does not follow from the counts layer', fix, **details)


@check('genes.ids', needs=('h5ad',))
def genes_ids(ctx) -> Outcome:
    """Gene symbols (not Ensembl ids) as var_names, unique, and known for the organism."""
    names = [str(name) for name in ctx.adata.var_names]
    n_genes = len(names)
    n_duplicated = n_genes - len(set(names))
    n_ensembl = sum(1 for name in names if _ENSEMBL.match(name))
    details = {'n_genes': n_genes, 'n_duplicated': n_duplicated, 'n_ensembl_like': n_ensembl}
    if n_duplicated:
        return fail(f'{n_duplicated} duplicated gene names', 'Make var_names unique (adata.var_names_make_unique() '
                    'or drop the duplicates).', **details)
    if n_genes and n_ensembl / n_genes > 0.5:
        return fail('var_names are Ensembl ids; gene symbols are required', 'Use symbols as var_names and keep '
                    'the Ensembl ids in a var column (ensg_id).', **details)
    universe, n_datasets = ctx.index.gene_universe(ctx.organism) if ctx.organism else (set(), 0)
    if n_datasets < thresholds.GENES_UNIVERSE_MIN_DATASETS:
        return ok('no duplicates, not Ensembl; the share of known genes is not judged (no gene universe for '
                  f'organism {ctx.organism or "unknown"!r}; run ant ingest-index)', **details)
    upper = {gene.upper() for gene in universe}
    exact = sum(1 for name in names if name in universe) / max(n_genes, 1)
    folded = sum(1 for name in names if name.upper() in upper) / max(n_genes, 1)
    share = max(exact, folded)
    details.update(known_share=round(share, 4), universe_datasets=n_datasets)
    fix = ('var_names must be official gene symbols of the organism. Check the organism in the .h5ad.mus and '
           'that var_names are not probe or transcript ids.')
    if share < thresholds.GENES_KNOWN_WARN:
        return fail(f'only {share:.0%} of the genes are known for {ctx.organism}', fix, **details)
    if share < thresholds.GENES_KNOWN_PASS:
        return warn(f'{share:.0%} of the genes are known for {ctx.organism}', fix, **details)
    return ok(f'{share:.0%} of the genes are known for {ctx.organism}', **details)
