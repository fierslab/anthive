"""Facet mapping checks: namespace, static rules, data rules, unmapped values, Braak, core facets."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from . import Outcome, no_database, check, fail, ok, skip, warn

#: facet-lint rules that concern the crosswalk join; the donor checks report them with counts only.
CROSSWALK_RULES = frozenset({'join_table_missing', 'join_cohort_unknown', 'crosswalk_conflict', 'join_unmatched'})
NAMESPACE_RULES = frozenset({'namespace_unregistered', 'namespace_deprecated'})
DONOR_FACETS = ('ant_donor', 'ant_donor_uid')
#: a mapping source column the stored data lack: an error here (facet_lint warns for obs_rename, which suits
#: ant md-check on the store), and the message names the column: a column name is not a donor value.
SOURCE_MISSING_RULES = frozenset({'source_missing', 'rename_source_missing'})
_MAX_LISTED = 10


def lint_findings(ctx) -> List[Any]:
    """Static ``facet_lint.lint_api`` findings (strict namespace), cached on the context."""
    import facet_lint  # noqa: PLC0415

    if 'lint_api' not in ctx._cache:  # noqa: SLF001
        ctx._cache['lint_api'] = facet_lint.lint_api(  # noqa: SLF001
            ctx.api, namespace_mode='error', store_root=ctx.store_root, group_id=ctx.group_id or '')
    return ctx._cache['lint_api']  # noqa: SLF001


def public_message(finding) -> str:
    """Finding text without donor values or crosswalk content."""
    if finding.rule in CROSSWALK_RULES:
        return f'[{finding.rule}] the donor id join cannot be resolved (see donor.uid_resolves)'
    if finding.rule in SOURCE_MISSING_RULES:     # column and facet names only, never values
        return f'[{finding.rule}] {finding.message}'
    if any(name in finding.message for name in DONOR_FACETS):
        return f'[{finding.rule}] concerns the donor facets; values are withheld from the report'
    return f'[{finding.rule}] {finding.message}'


def _verdict(findings: List[Any], ok_text: str, fix: str) -> Outcome:
    errors = [public_message(item) for item in findings if item.severity == 'error']
    warnings = [public_message(item) for item in findings if item.severity != 'error']
    details = {'errors': errors[:_MAX_LISTED], 'warnings': warnings[:_MAX_LISTED]}
    if errors:
        return fail('; '.join(errors[:3]) + (f' (+{len(errors) - 3} more)' if len(errors) > 3 else ''), fix, **details)
    if warnings:
        return warn('; '.join(warnings[:3]) + (f' (+{len(warnings) - 3} more)' if len(warnings) > 3 else ''), fix,
                    **details)
    return ok(ok_text)


@check('facets.namespace')
def facets_namespace(ctx) -> Outcome:
    """Every ``ant_*`` target of the ``api:`` section is a registered facet (strict namespace)."""
    findings = [item for item in lint_findings(ctx) if item.rule in NAMESPACE_RULES]
    return _verdict(findings, 'all ant_* targets are registered facets',
                    'Publish unregistered names as plain columns (no ant_ prefix); use the replacement named for a '
                    'deprecated facet. The registry is `facets.registry` in the requirements.')


@check('facets.mapping_static')
def facets_mapping_static(ctx) -> Outcome:
    """Rename chains, dtypes, value vocabularies, Braak group rule, derive forms (no data needed)."""
    findings = [item for item in lint_findings(ctx) if item.rule not in NAMESPACE_RULES]
    return _verdict([item for item in findings if item.rule not in CROSSWALK_RULES],
                    'the api: section is consistent',
                    'Fix the api: section of the .md: see the rule named in the message and '
                    'reference/facet-mapping.md in the skill.')


def _reader(ctx):
    """``facet_lint.duckdb_reader`` of the (scratch) duckdb, or None."""
    import facet_lint  # noqa: PLC0415

    path = ctx.duckdb
    return facet_lint.duckdb_reader(path) if path is not None else None


@check('facets.mapping_data', needs=('duckdb',))
def facets_mapping_data(ctx) -> Outcome:
    """The columns and values the ``api:`` section names exist in the converted database."""
    import facet_lint  # noqa: PLC0415

    reader = _reader(ctx)
    if reader is None:
        return no_database(ctx)
    columns, distinct_values, close = reader
    try:
        findings = facet_lint.lint_api_data(ctx.api, columns, distinct_values, store_root=ctx.store_root,
                                          metadata=ctx.md)
        findings += [item for item in facet_lint.lint_replicate(ctx.md, ctx.api, stored_columns=columns)
                     if item.rule == 'replicate_column_missing']
    finally:
        close()
    findings = [item for item in findings if item.rule not in CROSSWALK_RULES and item.rule != 'unmapped_value']
    findings = [replace(item, severity='error') if item.rule in SOURCE_MISSING_RULES else item for item in findings]
    return _verdict(findings, 'all source columns exist and numeric facets hold numbers in range',
                    'Correct the source column names in obs_rename / obs_derive (they must be obs columns of the '
                    'h5ad) and the values written into numeric facets.')


def unmapped_by_facet(ctx) -> Optional[Dict[str, Dict[str, Any]]]:
    """``{facet: {'n_values', 'n_cells', 'values': [...]}}`` of author values the mapping leaves unresolved.

    A value is unresolved when it ends up outside the facet's controlled values,
    or a derived facet without default turns it into NULL. Donor facets are
    left out (their values are identifiers). None when the database cannot be read.
    """
    import facet_mapping  # noqa: PLC0415
    from obscol_tags import facet_registry  # noqa: PLC0415

    reader = _reader(ctx)
    if reader is None:
        return None
    columns, distinct_values, close = reader
    registry = facet_registry()
    result: Dict[str, Dict[str, Any]] = {}
    try:
        entries = facet_mapping.project_dataset(
            ctx.api, [name for name, kind in columns.items() if kind == 'categorical'],
            [name for name, kind in columns.items() if kind == 'numerical'], ctx.group_id, ctx.store_root)
        for entry in entries:
            facet = entry['facet']
            if entry['mapping_kind'] not in (facet_mapping.RENAMED, facet_mapping.DERIVED) or facet in DONOR_FACETS:
                continue
            controlled = (registry.get(facet[len('ant_'):]) or {}).get('controlled_values') or []
            sources = entry.get('source_columns') or []
            if len(sources) != 1 or sources[0] not in columns:
                continue
            observed = distinct_values(sources[0]) or []
            translated = facet_mapping.translate_observed(dict(entry), observed, store_root=ctx.store_root)
            unresolved = []
            for row in translated['value_map']:
                if not row.get('n_cells') or row['from'] is None:
                    continue
                target = row['to']
                if row['rule'] == facet_mapping.RULE_HIDDEN:
                    continue
                if target is None and entry['mapping_kind'] == facet_mapping.DERIVED \
                        and row['rule'] == facet_mapping.RULE_DEFAULT:
                    unresolved.append(row)
                elif target is not None and controlled and str(target) not in controlled:
                    unresolved.append(row)
            if unresolved:
                result[facet] = {'n_values': len(unresolved), 'n_cells': sum(int(row['n_cells']) for row in unresolved),
                                 'values': [str(row['from']) for row in unresolved[:_MAX_LISTED]],
                                 'allowed': controlled or None}
    finally:
        close()
    return result


@check('facets.unmapped_values', needs=('duckdb',))
def facets_unmapped_values(ctx) -> Outcome:
    """Every value of every mapped source column is mapped, renamed, hidden or covered by a default."""
    found = unmapped_by_facet(ctx)
    if found is None:
        return no_database(ctx)
    if not found:
        return ok('all values of the mapped source columns are resolved')
    parts = [f'{facet}: {info["n_values"]} value(s) ({info["n_cells"]} cells), e.g. ' + ', '.join(repr(v) for v in info['values'][:4])
             for facet, info in found.items()]
    return fail('; '.join(parts),
                'Add every listed author value to obs_value_rename / the obs_derive map (to a registered value), '
                'hide it with obs_value_hidden, or give the derive a default. See reference/facet-mapping.md.',
                facets=found)


@check('facets.braak_range', needs=('duckdb',))
def facets_braak_range(ctx) -> Outcome:
    """Published ``ant_braak`` values are whole numbers from 0 to 6."""
    from facet_lint import parse_braak_label  # noqa: PLC0415

    view = ctx.view
    if view is None:
        return no_database(ctx)
    if 'ant_braak' not in view.published_columns():
        return ok('ant_braak is not published')
    values = view.distinct_values('ant_braak') or []
    bad: List[Tuple[Any, int]] = []
    for value, n_cells in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            stage = parse_braak_label(value)
            number = float(stage) if stage is not None else float('nan')
        if not (number == number and 0 <= number <= 6 and number == int(number)):
            bad.append((value, n_cells))
    if bad:
        return fail('ant_braak has values that are not whole numbers 0 to 6: '
                    + ', '.join(f'{value!r} ({n} cells)' for value, n in bad[:_MAX_LISTED]),
                    'Map Braak stages to integers 0 to 6 (roman numerals: 0, I=1 ... VI=6); write NULL for '
                    'unknown stages. Intermediate values like 2.5 are not allowed.',
                    n_bad_values=len(bad), values=[str(value) for value, _n in bad[:_MAX_LISTED]])
    return ok(f'{len(values)} Braak levels, all whole numbers 0 to 6', n_levels=len(values))


@check('facets.core_present', needs=('h5ad',))
def facets_core_present(ctx) -> Outcome:
    """Donor, sex and disease are published for human and mouse data; Braak for Alzheimer datasets."""
    import facet_mapping  # noqa: PLC0415
    from replicates import ReplicateError, declared_replicate  # noqa: PLC0415

    if ctx.organism not in ('human', 'mouse'):
        return skip(f'organism {ctx.organism or "unknown"!r}: core facets are required for human and mouse only')
    obs_columns = [str(name) for name in ctx.adata.obs.columns]
    entries = facet_mapping.project_dataset(ctx.api, obs_columns, [], ctx.group_id, ctx.store_root)
    published = {entry['facet'] for entry in entries if entry['mapping_kind'] is not None}
    published |= {name for name in obs_columns if name.startswith('ant_')}
    try:
        unit = declared_replicate(ctx.md)[0]
    except ReplicateError:
        unit = None
    missing_donor = not ({'ant_donor', 'ant_sample'} & published)
    missing = [facet for facet in ('ant_sex', 'ant_disease') if facet not in published]
    diseases = ctx.annotation.get('disease') or ''
    diseases = ' '.join(diseases) if isinstance(diseases, list) else str(diseases)
    if 'alzheimer' in diseases.lower() and 'ant_braak' not in published and not ctx.md.get('braak_unavailable'):
        missing.append('ant_braak (or braak_unavailable: <reason>)')
    if missing_donor:
        missing.insert(0, 'ant_donor (or ant_sample)')
    fix = ('Map the obs columns to the core facets with obs_rename / obs_derive / obs_constant (constants for a '
           'single-condition dataset). Without them the dataset cannot enter the donor-level facet tests.')
    if missing_donor and unit == 'donor':
        return fail('replicate_unit is donor but no ant_donor facet is published; also missing: '
                    + (', '.join(missing[1:]) or 'nothing else'), fix, missing=missing)
    if missing:
        return warn('core facets not published: ' + ', '.join(missing), fix, missing=missing)
    return ok('donor, sex and disease are published', published=sorted(published)[:40])
