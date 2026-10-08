"""Module score checks: a facet-eligible dataset carries scores for every catalog that applies to it."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from . import Outcome, check, ok, skip, warn

#: Sidecar sources every facet-eligible dataset must carry for each applicable catalog
#: (scanpy ``score_genes`` and the one rank-based scorer, UCell).
REQUIRED_SOURCES = ('geneset', 'ucell')
_MAX_LISTED = 10


def missing_scores(applicable: Dict[str, Dict[str, int]], present: Set[Tuple[str, str]],
                   sources: Tuple[str, ...] = REQUIRED_SOURCES) -> List[str]:
    """``source::catalog`` pairs that apply to the dataset but are absent from its sidecar.

    Args:
        applicable: ``celltype_scope.applicable_experiments`` of the dataset.
        present: ``(source, catalog)`` pairs of the scores sidecar.
        sources: sources that must exist for every applicable catalog.
    """
    return [f'{source}::{catalog}' for catalog in sorted(applicable) for source in sources
            if (source, catalog) not in present]


def facet_eligible(ctx: Any) -> Tuple[bool, str]:
    """Metadata part of the facet-test gate (``ant-facet-test`` ``dataset_eligibility``), without data."""
    import facet_scopes  # noqa: PLC0415

    if ctx.md.get('is_secondary') is True:
        return False, 'secondary dataset'
    exclusion = facet_scopes.facet_exclusion_reason(ctx.annotation, ctx.md)
    if exclusion:
        return False, f'excluded from the facet layer: {exclusion}'
    published = set((ctx.api.get('obs_rename') or {}).values()) | set((ctx.api.get('obs_derive') or {}).keys()) \
        | set((ctx.api.get('obs_constant') or {}).keys())
    scope = facet_scopes.scope_of(ctx.annotation, published, db_id=ctx.db_id)
    if scope not in facet_scopes.TESTED_SCOPES:
        return False, f'no tested facet scope ({scope or "none"})'
    return True, 'ok'


@check('scores.modules_complete', needs=('duckdb',))
def scores_modules_complete(ctx) -> Outcome:
    """A facet-eligible dataset has scanpy and UCell scores for every module catalog that applies to its cell types."""
    import celltype_scope  # noqa: PLC0415
    from scores_parquet import scores_path, summarize  # noqa: PLC0415
    from h5adstore import H5adEntry  # noqa: PLC0415

    eligible, reason = facet_eligible(ctx)
    if not eligible:
        return skip(f'not facet-eligible: {reason}')
    database = ctx.duckdb
    if database is None:
        return skip('no readable .duckdb')
    registry = celltype_scope.default_registry()
    if registry.errors:
        return skip('the geneset store has errors (ant geneset validate)', errors=len(registry.errors))
    entry = H5adEntry(db_id=ctx.db_id or '', md_path=ctx.md_path, h5ad_path=ctx.h5ad_path or ctx.group_dir / 'x.h5ad',
                      duckdb_path=database, group_id=ctx.group_id or '', file_id=ctx.file_id or '',
                      study=ctx.group_dir.name, stem=ctx.basename)
    cells = celltype_scope.cell_type_groups(entry, ctx.store_root)
    organisms = ctx.annotation.get('organism')
    organisms = organisms if isinstance(organisms, list) else [organisms] if organisms else ['human']
    applicable = celltype_scope.applicable_experiments(registry, cells['group'], organisms)
    sidecar = scores_path(database)
    if not applicable:
        return skip('no module catalog applies to the cell types of this dataset')
    if not sidecar.exists():
        return warn(f'no scores sidecar yet; {len(applicable)} catalogs apply',
                    'Scoring runs after the conversion (ant-score-fleet run --only <db_id>); score before the '
                    'dataset enters the facet tests.', applicable=sorted(applicable))
    summary = summarize(database)
    present = set(zip(summary['source'], summary['catalog']))
    missing = missing_scores(applicable, present)
    if missing:
        return warn(f'{len(missing)} score sets missing: ' + ', '.join(missing[:3]) + (' ...' if len(missing) > 3 else ''),
                    'Run ant-score-fleet run --only <db_id> (scanpy score_genes and UCell for every applicable '
                    'catalog), then re-run the check.', missing=missing[:_MAX_LISTED], n_missing=len(missing))
    return ok(f'scores present for all {len(applicable)} applicable catalogs', n_catalogs=len(applicable))
