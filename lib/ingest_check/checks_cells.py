"""Cell overlap: does the submission contain cells that are already in the store?"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from . import Outcome, no_database, check, fail, note, ok, skip, warn
from . import thresholds


def compare_cells(strict_new: np.ndarray, loose_new: np.ndarray, strict_old: np.ndarray,
                  loose_old: np.ndarray) -> Dict[str, float]:
    """Shares of cells found in each other, by strict (barcode + donor) and loose (barcode) hash."""
    strict_hits = int(np.intersect1d(strict_new, strict_old, assume_unique=True).size)
    loose_hits = int(np.intersect1d(loose_new, loose_old, assume_unique=True).size)
    return {'strict_of_new': strict_hits / max(len(strict_new), 1), 'strict_of_old': strict_hits / max(len(strict_old), 1),
            'loose_of_new': loose_hits / max(len(loose_new), 1), 'loose_of_old': loose_hits / max(len(loose_old), 1),
            'n_strict_hits': strict_hits}


def classify(shares: Dict[str, float]) -> str:
    """``same``, ``reanalysis``, ``relabelled`` or ``none`` for one stored dataset."""
    if shares['strict_of_new'] >= thresholds.CELLS_SAME_FRACTION and shares['strict_of_old'] >= thresholds.CELLS_SAME_FRACTION:
        return 'same'
    if shares['strict_of_new'] >= thresholds.CELLS_REANALYSIS_FRACTION:
        return 'reanalysis'
    if shares['loose_of_new'] >= thresholds.CELLS_SAME_FRACTION and shares['strict_of_new'] < thresholds.CELLS_REANALYSIS_FRACTION:
        return 'relabelled'
    return 'none'


def _targets(value: Any) -> List[str]:
    if not value:
        return []
    return [str(item) for item in (value if isinstance(value, list) else [value])]


@check('cells.overlap', needs=('duckdb', 'index'))
def cells_overlap(ctx) -> Outcome:
    """Hashed (barcode, donor) pairs against the cell index of every stored dataset."""
    index = ctx.index
    if index.is_empty():
        return skip('no overlap index; an admin runs `ant ingest-index build`')
    digests = ctx.new_digests()
    if digests is None:
        return no_database(ctx)
    if digests.n_cells < thresholds.CELLS_MIN_CELLS:
        return skip(f'{digests.n_cells} cells: too few to compare by barcode')
    found: Dict[str, Dict[str, Any]] = {}
    for db_key in sorted(index.state()):
        if db_key == digests.db_key:
            continue
        stored = index.cells(db_key)
        if stored is None:
            continue
        shares = compare_cells(digests.strict, digests.loose, *stored)
        verdict = classify(shares)
        if verdict != 'none':
            found[db_key] = {'verdict': verdict, **{key: round(value, 4) for key, value in shares.items()}}
    if not found:
        return ok('no cell of the submission is in a stored dataset', n_cells=digests.n_cells)
    reanalysis_of = _targets(ctx.md.get('reanalysis_of'))
    kinds = {key: info['verdict'] for key, info in found.items()}
    counts = {verdict: sum(1 for value in kinds.values() if value == verdict) for verdict in ('same', 'reanalysis', 'relabelled')}
    details = {'n_cells': digests.n_cells, 'n_same_cells_datasets': counts['same'],
               'n_reanalysis_datasets': counts['reanalysis'], 'n_relabelled_datasets': counts['relabelled'],
               'best_share_of_new_cells': max(info['strict_of_new'] for info in found.values())}
    admin = {'datasets': found}
    if counts['same']:
        outcome = fail('the cells are the same as in a stored dataset (barcodes and donors match both ways)',
                       'Do not add the same cells twice: submit a new version of the existing dataset instead '
                       '(ask an admin for the update route).', **details)
    else:
        unresolved = [key for key, verdict in kinds.items() if verdict == 'reanalysis'
                      and not (ctx.md.get('is_secondary') is True and key in reanalysis_of)]
        if unresolved:
            outcome = fail(f'{len(unresolved)} stored dataset(s) contain at least '
                           f'{thresholds.CELLS_REANALYSIS_FRACTION:.0%} of the cells of this submission',
                           'If this is a re-analysis (subset, re-clustering, other normalisation) of a stored '
                           'dataset, declare is_secondary: true and reanalysis_of: <db_id> in the .md. '
                           'Otherwise ask an admin.', **details)
        elif counts['reanalysis']:
            outcome = note('cells overlap with stored dataset(s) that the .md declares as re-analysis targets', **details)
        else:
            outcome = warn('the barcodes match a stored dataset but the donor ids differ',
                           'The same cells may be labelled with other donor ids; check ant_donor / ant_donor_uid '
                           'and declare reanalysis_of if it is a re-analysis.', **details)
    outcome.admin_detail = admin
    return outcome
