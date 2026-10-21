"""Study design checks: lineage, replicate unit and column, replicates per group, disease basis."""
from __future__ import annotations

from typing import Dict, List, Optional

from . import Outcome, no_database, check, fail, note, ok, skip, warn
from . import thresholds

_UNITS = ('donor', 'animal', 'sample', 'none')


def _source_of(api: Dict, published: str) -> Optional[str]:
    """Obs column behind a published name (the rename or derive source), or the name itself."""
    for source, target in (api.get('obs_rename') or {}).items():
        if target == published:
            return str(source)
    spec = (api.get('obs_derive') or {}).get(published)
    if isinstance(spec, dict) and spec.get('source'):
        return str(spec['source'])
    return None


def _units_from_obs(ctx, column: Optional[str]) -> Optional[int]:
    """Distinct replicate units from the h5ad obs (the column or its source), None when not there."""
    if not column or ctx.h5ad_path is None:
        return None
    obs = ctx.adata.obs
    source = column if column in obs.columns else _source_of(ctx.api, column)
    if source not in obs.columns:
        return None
    return int(obs[source].dropna().astype(str).nunique())


@check('design.designation')
def design_designation(ctx) -> Outcome:
    """``reanalysis_of`` names a stored dataset and implies ``is_secondary``; ``donor_group`` is a slug; a QC companion
    (``qc: true``, ``qc_of``) is never primary."""
    import facet_lint  # noqa: PLC0415
    from md_check import collect_db_ids  # noqa: PLC0415

    known = collect_db_ids(ctx.store_root)
    findings = facet_lint.lint_lineage(ctx.md, known)
    errors = [f'[{item.rule}] {item.message}' for item in findings if item.severity == 'error']
    warnings = [f'[{item.rule}] {item.message}' for item in findings if item.severity != 'error']
    if errors:
        return fail('; '.join(errors), 'Fix reanalysis_of (an existing <group_id>/<id>) and set is_secondary: true '
                    'for a re-analysis. A QC companion says qc: true, qc_of: <db_id of the final dataset> (empty until '
                    'that is published) and is never is_secondary: false. See reference/designation.md.',
                    errors=errors)
    if warnings:
        return warn('; '.join(warnings), 'Use a lower-case slug for donor_group.', warnings=warnings)
    import qc_companion  # noqa: PLC0415

    if qc_companion.is_qc(ctx.md):
        target = qc_companion.qc_target(ctx.md)
        return ok(f'QC companion of {target}: secondary, not counted, not facet-tested' if target else
                  'QC companion (qc_of empty: fill it with an update once the final dataset is published): '
                  'secondary, not counted, not facet-tested', qc_of=target)
    declared = [key for key in ('is_secondary', 'reanalysis_of', 'donor_group') if ctx.md.get(key)]
    if declared:
        return ok('designation: ' + ', '.join(declared))
    if 'is_secondary' in ctx.md:
        return ok('primary dataset (is_secondary: false)')
    return ok('primary dataset: nothing declared, so a new dataset is published with is_secondary: false '
              '(a re-analysis of data already in Anthive declares is_secondary: true and reanalysis_of)')


@check('design.replicate_unit', needs=('h5ad',))
def design_replicate_unit(ctx) -> Outcome:
    """``replicate_unit`` is declared and valid; ``replicate_column`` exists and samples nest within donors."""
    import facet_lint  # noqa: PLC0415
    from replicates import ReplicateError, declared_replicate  # noqa: PLC0415

    obs_columns = [str(name) for name in ctx.adata.obs.columns]
    findings = facet_lint.lint_replicate(ctx.md, ctx.api, stored_columns=obs_columns)
    errors = [f'[{item.rule}] {item.message}' for item in findings if item.severity == 'error' or
              item.rule == 'replicate_unit_missing']
    warnings = [f'[{item.rule}] {item.message}' for item in findings if item.rule not in
                ('replicate_unit_missing',) and item.severity != 'error']
    fix = ('Declare replicate_unit: donor, animal, sample or none (what one statistical replicate is) and, unless '
           'none, replicate_column: the obs column or ant_* facet that identifies it.')
    if errors:
        return fail('; '.join(errors), fix, errors=errors)
    try:
        unit, column = declared_replicate(ctx.md)
    except ReplicateError:
        return fail('replicate_unit is invalid', fix)
    details = {'replicate_unit': unit, 'replicate_column': column}
    if unit == 'sample' and column:
        donor_source = _source_of(ctx.api, 'ant_donor')
        sample_source = column if column in obs_columns else _source_of(ctx.api, column)
        if donor_source in obs_columns and sample_source in obs_columns and donor_source != sample_source:
            frame = ctx.adata.obs[[sample_source, donor_source]].dropna()
            spread = frame.groupby(sample_source, observed=True)[donor_source].nunique()
            n_bad = int((spread > 1).sum())
            details['n_samples_in_several_donors'] = n_bad
            if n_bad:
                return fail(f'{n_bad} sample id(s) occur in more than one donor: replicate_column does not nest '
                            'within donors', 'Use a column that is unique per donor and sample (for example '
                            'donor + sample id) as replicate_column.', **details)
    if warnings:
        return warn('; '.join(warnings), 'Remove replicate_column when replicate_unit is none.', **details)
    return ok(f'replicate_unit {unit}' + (f' on {column}' if column else ''), **details)


@check('design.replicates_per_group', needs=('duckdb',))
def design_replicates_per_group(ctx) -> Outcome:
    """Replicate units in total and per level of the core facets, against the facet test thresholds."""
    from replicates import ReplicateError, declared_replicate, DEFAULT_COLUMN_BY_UNIT  # noqa: PLC0415

    try:
        unit, column = declared_replicate(ctx.md)
    except ReplicateError:
        return skip('replicate_unit is invalid (see design.replicate_unit)')
    if unit == 'none':
        return note('replicate_unit is none: results are cell-level and descriptive only; the facet tests skip this dataset',
                    replicate_unit='none')
    column = column or DEFAULT_COLUMN_BY_UNIT.get(unit or '', None)
    view = ctx.view
    if view is None:
        return no_database(ctx)
    published = view.published_columns()
    if not column or column not in published:
        n_units = _units_from_obs(ctx, column)
        if n_units is None:
            return skip(f'replicate column {column!r} is not an obs column (see design.replicate_unit)')
        details = {'replicate_unit': unit, 'replicate_column': column, 'n_units': n_units, 'per_facet': {}}
        if n_units < thresholds.MIN_UNITS_TOTAL:
            return warn(f'{n_units} {unit}s: fewer than {thresholds.MIN_UNITS_TOTAL}, the facet tests skip this dataset',
                        'The dataset is still useful for browsing.', **details)
        return ok(f'{n_units} {unit}s (the column is not published, so levels per facet are not counted)', **details)
    facets = [name for name in ('ant_disease', 'ant_sex', 'ant_braak_group') if name in published]
    frame = view.cell_frame([column] + facets)
    units = frame[column].dropna().astype(str)
    n_units = int(units.nunique())
    per_facet: Dict[str, Dict[str, int]] = {}
    thin: List[str] = []
    for facet in facets:
        counts = frame.dropna(subset=[column, facet]).groupby(facet, observed=True)[column].nunique()
        per_facet[facet] = {str(level): int(count) for level, count in counts.items()}
        thin.extend(f'{facet}={level} ({count})' for level, count in per_facet[facet].items()
                    if count < thresholds.MIN_UNITS_PER_LEVEL)
    details = {'replicate_unit': unit, 'replicate_column': column, 'n_units': n_units, 'per_facet': per_facet}
    if n_units < thresholds.MIN_UNITS_TOTAL:
        return warn(f'{n_units} {unit}s: fewer than {thresholds.MIN_UNITS_TOTAL}, the facet tests skip this dataset',
                    'The dataset is still useful for browsing. More replicates are needed for donor-level tests.',
                    **details)
    if thin:
        return warn(f'{n_units} {unit}s; levels with fewer than {thresholds.MIN_UNITS_PER_LEVEL} are dropped from '
                    'the facet tests: ' + ', '.join(thin[:8]), 'Nothing to fix; check that the levels are mapped '
                    'as intended.', **details)
    return ok(f'{n_units} {unit}s, every facet level has at least {thresholds.MIN_UNITS_PER_LEVEL}', **details)


@check('design.disease_basis')
def design_disease_basis(ctx) -> Outcome:
    """``disease_basis`` (and ``disease_derivation``) are declared when ``ant_disease`` is published."""
    import facet_lint  # noqa: PLC0415

    findings = facet_lint.lint_disease_basis(ctx.md, ctx.api)
    errors = [f'[{item.rule}] {item.message}' for item in findings]
    if errors:
        return fail('; '.join(errors), 'Declare disease_basis: nia_reagan, braak_cerad, adnc, clinical_dx or '
                    'author_dx (what the disease label is based on); braak_cerad also needs disease_derivation.',
                    errors=errors)
    basis = ctx.md.get('disease_basis')
    return ok(f'disease_basis {basis}' if basis else 'ant_disease is not published: no disease basis needed')
