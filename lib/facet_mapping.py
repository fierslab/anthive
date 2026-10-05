"""How a dataset's author columns project onto ``ant_*`` facets.

A facet is a published ``ant_<tag>`` obs column. Each dataset's ``.md``
declares it in the ``api:`` section (validated by
``metadata.get_api_config``):

* ``obs_rename``  ``<author col>: ant_x`` publishes an author column under
  the facet name. ``obs_value_rename`` rewrites single values on the way
  (``M: m``), ``obs_value_hidden`` turns values into NULL. Values that are
  not listed pass through unchanged.
* ``obs_derive``  ``ant_x: {source, dtype, map, default}`` builds the facet
  from a value map over a source column. Unlisted values become ``default``
  (NULL when absent). The raw source column is read, so value renames and
  hides on that source do not apply.
* ``obs_derive`` template form ``ant_x: {source, template}`` renders every
  non-NULL source value through the template (``{group_id}``, ``{value}``);
  kind ``template``.
* ``obs_derive`` join form ``ant_x: {source, join, cohort,
  fallback_template}`` looks the source value up in a crosswalk table of
  the store root (alias -> canonical uid of the cohort); values without a
  match get the fallback template or NULL; kind ``join``. Both forms are
  described in ``lib/obs_derive_forms.py``.
* ``obs_constant`` ``ant_x: {value, dtype}`` broadcasts one value to every
  cell.
* computed: ``ant_braak_group`` and ``ant_apoe4_carrier`` are computed
  centrally from the published ``ant_braak`` / ``ant_apoe`` when the
  ``.md`` declares neither (``COMPUTED_FACETS``; the SQL is
  ``anthive_db._computed_facet_sql``).

The semantics mirror ``anthive_db.build_obs_view_sql``, which builds the
served ``obscat``/``obsnum`` views from the same config. This module only
describes that projection: ``project_dataset`` turns an api config into one
entry per facet, ``translate_observed`` adds per-value cell counts, and
``redact_hidden_sources`` strips columns listed in ``obs_hidden`` for
callers who may not see them. ``candidate_columns`` names author columns
that look like they carry a facet the dataset lacks.

Consumers: ant-serve (``GET /facets/{name}``,
``GET /databases/{id}/facets``) and, through the API, the web UI.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from facet_aliases import is_deprecated_facet_column
from obs_derive_forms import (FORM_JOIN, FORM_TEMPLATE, RULE_CROSSWALK, RULE_FALLBACK, RULE_TEMPLATE,
                              RULE_NULL, RULE_UNMATCHED, Crosswalk, crosswalk_for, derive_form, translate_value)

FACET_PREFIX = 'ant_'

RENAMED = 'renamed'
DERIVED = 'derived'
CONSTANT = 'constant'
NATIVE = 'native'
COMPUTED = 'computed'
TEMPLATE = 'template'
JOIN = 'join'

#: Facet computed centrally -> the facet it is computed from, with the rule
#: as published text. Mirrors ``anthive_db.COMPUTED_FACETS``.
COMPUTED_FACETS: Dict[str, Tuple[str, str]] = {
    'ant_braak_group': ('ant_braak', 'Braak 0 to 2 low, 3 to 4 medium, 5 to 6 high; anything else NULL'),
    'ant_apoe4_carrier': ('ant_apoe', 'E4+ when ant_apoe (E<a>/E<b>) holds an E4 allele, E4- when it holds '
                                     'none; values of another form NULL'),
}

# Translation rules for one author value.
RULE_MAPPED = 'mapped'          # listed in obs_value_rename / obs_derive map
RULE_HIDDEN = 'hidden'          # listed in obs_value_hidden: becomes NULL
RULE_PASSTHROUGH = 'passthrough'  # renamed column, value not listed: unchanged
RULE_DEFAULT = 'default'        # derived column, value not in map: default

# Author columns that never carry donor-level facets (score columns etc.).
_NON_AUTHOR_PREFIXES = ('ant_', 'ucell__', 'starcat__', 'aucell__')


def is_facet(name: Any) -> bool:
    """True for an ``ant_*`` facet column name."""
    return isinstance(name, str) and name.startswith(FACET_PREFIX) and len(name) > len(FACET_PREFIX)


def _value_key(value: Any) -> str:
    """String form used to compare author values with config keys."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _same_value(observed: Any, configured: Any) -> bool:
    """Mirror the view SQL's ``col = '<key>'`` comparison.

    VARCHAR columns compare as strings. A numerical source compares
    numerically (DuckDB casts the string literal), so ``0.0`` matches ``0``.
    """
    if _value_key(observed) == _value_key(configured):
        return True
    try:
        return float(observed) == float(configured)
    except (TypeError, ValueError):
        return False


def _json_value(value: Any) -> Any:
    """Values for JSON: keep numbers, strings and None, stringify the rest."""
    if isinstance(value, bool):
        return str(value)
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def project_dataset(
    api_config: Mapping[str, Any],
    live_categorical: Iterable[str] = (),
    live_numerical: Iterable[str] = (),
    group_id: Optional[str] = None,
    store_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Describe how this dataset publishes each of its facets.

    Args:
        api_config: The ``api:`` section as returned by
            ``metadata.get_api_config`` (missing keys are fine).
        live_categorical: Public column names of the ``obscat`` view.
        live_numerical: Public column names of the ``obsnum`` view.
        group_id: The dataset's group id (fills ``{group_id}`` in templates).
        store_root: Where join tables live (default: the configured store).

    Returns:
        One entry per facet, sorted by name, with keys ``facet``,
        ``mapping_kind`` (renamed / derived / template / join / constant /
        native / computed), ``live``
        (the served view exposes it), ``dtype``, ``source_columns``,
        ``sources`` (``{column, published_as, hidden}``), ``source_hidden``,
        ``value_map`` (``{from, to, rule}`` in .md order), ``unmapped``
        (passthrough / default / None), ``default``, ``constant_value`` and
        ``redacted`` (always False here). Hidden source columns are
        included; callers decide who may see them. Template and join
        entries add ``template`` / ``join_table``, ``cohort``,
        ``fallback_template``, ``group_id``, ``n_crosswalk_aliases`` (size of
        the cohort's alias table, a count only) and ``join_problem`` (why
        the view cannot build the column, else None).
    """
    api = api_config or {}
    rename: Dict[str, str] = dict(api.get('obs_rename') or {})
    hidden = set(api.get('obs_hidden') or [])
    value_rename: Mapping[str, Mapping[Any, Any]] = api.get('obs_value_rename') or {}
    value_hidden: Mapping[str, Sequence[Any]] = api.get('obs_value_hidden') or {}
    derive: Mapping[str, Mapping[str, Any]] = api.get('obs_derive') or {}
    constant: Mapping[str, Mapping[str, Any]] = api.get('obs_constant') or {}

    live_cat = set(live_categorical or ())
    live_num = set(live_numerical or ())

    def _live_dtype(facet: str) -> Optional[str]:
        if facet in live_cat:
            return 'categorical'
        if facet in live_num:
            return 'numerical'
        return None

    def _source(column: str) -> Dict[str, Any]:
        is_hidden = column in hidden
        if is_hidden:
            published_as = None
        else:
            published_as = rename.get(column, column)
        return {'column': column, 'published_as': published_as, 'hidden': is_hidden}

    entries: Dict[str, Dict[str, Any]] = {}

    def _base(facet: str, kind: str, spec_dtype: Optional[str] = None) -> Dict[str, Any]:
        live_dtype = _live_dtype(facet)
        return {
            'facet': facet,
            'mapping_kind': kind,
            'live': live_dtype is not None,
            'dtype': live_dtype or spec_dtype,
            'source_columns': [],
            'sources': [],
            'source_hidden': False,
            'value_map': [],
            'unmapped': None,
            'default': None,
            'constant_value': None,
            'redacted': False,
        }

    for author_col, target in rename.items():
        if not is_facet(target):
            continue
        entry = _base(target, RENAMED)
        source = _source(author_col)
        source['published_as'] = None if source['hidden'] else target
        entry['sources'] = [source]
        entry['source_columns'] = [author_col]
        entry['source_hidden'] = source['hidden']
        if source['hidden']:
            # build_obs_view_sql drops a hidden column before renaming it,
            # so the facet never reaches the view.
            entry['live'] = False
        rows: List[Dict[str, Any]] = []
        for value in value_hidden.get(author_col) or []:
            rows.append({'from': _json_value(value), 'to': None, 'rule': RULE_HIDDEN})
        hidden_keys = {_value_key(r['from']) for r in rows}
        for old_value, new_value in (value_rename.get(author_col) or {}).items():
            if _value_key(old_value) in hidden_keys:
                continue  # hide wins over rename in the view's CASE
            rows.append({
                'from': _json_value(old_value),
                'to': _json_value(new_value),
                'rule': RULE_MAPPED if new_value is not None else RULE_HIDDEN,
            })
        entry['value_map'] = rows
        entry['unmapped'] = RULE_PASSTHROUGH
        entries[target] = entry

    for target, spec in derive.items():
        if not is_facet(target) or not isinstance(spec, Mapping):
            continue
        form = derive_form(spec)
        if form in (FORM_TEMPLATE, FORM_JOIN):
            entries[target] = _form_entry(target, spec, form, _base, _source, group_id, store_root)
            continue
        entry = _base(target, DERIVED, spec.get('dtype'))
        source_col = str(spec.get('source') or '')
        source = _source(source_col)
        entry['sources'] = [source]
        entry['source_columns'] = [source_col]
        entry['source_hidden'] = source['hidden']
        entry['value_map'] = [
            {'from': _json_value(old_value), 'to': _json_value(new_value), 'rule': RULE_MAPPED}
            for old_value, new_value in (spec.get('map') or {}).items()
        ]
        entry['unmapped'] = RULE_DEFAULT
        entry['default'] = _json_value(spec.get('default'))
        entries[target] = entry

    for target, spec in constant.items():
        if not is_facet(target) or not isinstance(spec, Mapping):
            continue
        entry = _base(target, CONSTANT, spec.get('dtype'))
        entry['constant_value'] = _json_value(spec.get('value'))
        entries[target] = entry

    # Centrally computed facets: the dataset publishes the source facet but
    # declares nothing for the target (anthive_db.build_obs_view_sql).
    for target, (source_facet, rule) in COMPUTED_FACETS.items():
        if target in entries or source_facet not in entries:
            continue
        entry = _base(target, COMPUTED, 'categorical')
        entry['source_columns'] = [source_facet]
        entry['sources'] = [{'column': source_facet, 'published_as': source_facet, 'hidden': False}]
        entry['computed_rule'] = rule
        entry['unmapped'] = None
        entries[target] = entry

    # A facet the view serves without any declaration came from the h5ad
    # itself under its ant_* name (the deprecated alias columns are not facets).
    for facet in sorted(live_cat | live_num):
        if is_facet(facet) and facet not in entries and not is_deprecated_facet_column(facet):
            entry = _base(facet, NATIVE)
            entry['source_columns'] = [facet]
            entry['sources'] = [{'column': facet, 'published_as': facet, 'hidden': False}]
            entry['unmapped'] = RULE_PASSTHROUGH
            entries[facet] = entry

    return [entries[name] for name in sorted(entries)]


def _form_entry(target: str, spec: Mapping[str, Any], form: str, base, source_of,
                group_id: Optional[str], store_root: Optional[Path]) -> Dict[str, Any]:
    """Projection entry of a template or join derive."""
    entry = base(target, TEMPLATE if form == FORM_TEMPLATE else JOIN, 'categorical')
    source_col = str(spec.get('source') or '')
    source = source_of(source_col)
    entry['sources'] = [source]
    entry['source_columns'] = [source_col]
    entry['source_hidden'] = source['hidden']
    entry['group_id'] = group_id
    if form == FORM_TEMPLATE:
        entry['template'] = spec.get('template')
        entry['unmapped'] = None   # every non-NULL value is rendered
        return entry
    entry['join_table'] = spec.get('join')
    entry['cohort'] = spec.get('cohort')
    entry['fallback_template'] = spec.get('fallback_template')
    entry['unmapped'] = RULE_FALLBACK if spec.get('fallback_template') else RULE_UNMATCHED
    crosswalk, problem = crosswalk_for(spec, store_root)
    entry['join_problem'] = problem
    entry['n_crosswalk_aliases'] = (len(crosswalk.table(spec.get('cohort') or ''))
                                    if crosswalk is not None and problem is None else None)
    return entry


def _form_spec(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """The derive spec a template / join entry describes."""
    if entry.get('mapping_kind') == TEMPLATE:
        return {'template': entry.get('template')}
    return {'join': entry.get('join_table'), 'cohort': entry.get('cohort'),
            'fallback_template': entry.get('fallback_template')}


def translate(entry: Mapping[str, Any], value: Any,
              crosswalk: Optional[Crosswalk] = None) -> Tuple[Any, str]:
    """Return ``(facet value, rule)`` for one author value under ``entry``.

    ``crosswalk`` is needed for a join entry (without it every value counts
    as not found).
    """
    kind = entry.get('mapping_kind')
    if kind in (TEMPLATE, JOIN):
        return translate_value(_form_spec(entry), value, entry.get('group_id'), crosswalk)
    rows = entry.get('value_map') or []
    if value is None:
        # SQL: NULL never equals a key, so a renamed column keeps NULL and a
        # derived one falls through to its default.
        if kind == DERIVED:
            return entry.get('default'), RULE_DEFAULT
        return None, RULE_PASSTHROUGH
    if kind == RENAMED:
        for row in rows:
            if row['rule'] == RULE_HIDDEN and row['to'] is None and _same_value(value, row['from']):
                return None, RULE_HIDDEN
        for row in rows:
            if row['rule'] == RULE_MAPPED and _same_value(value, row['from']):
                return row['to'], RULE_MAPPED
        return _json_value(value), RULE_PASSTHROUGH
    if kind == DERIVED:
        for row in rows:
            if _same_value(value, row['from']):
                return row['to'], RULE_MAPPED
        return entry.get('default'), RULE_DEFAULT
    return _json_value(value), RULE_PASSTHROUGH


def translate_observed(
    entry: Dict[str, Any],
    observed: Sequence[Tuple[Any, int]],
    n_distinct: Optional[int] = None,
    store_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Rebuild ``entry['value_map']`` from the author values actually present.

    Args:
        entry: One ``project_dataset`` entry (renamed or derived).
        observed: ``(author value, n_cells)`` pairs, most frequent first,
            possibly truncated. A NULL author value gets a row with
            ``from: None``: it stays NULL in a renamed facet but takes the
            default of a derived one, exactly as the view SQL does.
        n_distinct: Number of distinct non-NULL author values, when known.
        store_root: Where a join entry's table lives (default: the store).

    Returns:
        The same entry, with ``value_map`` rows ``{from, to, rule, n_cells}``
        (observed values first by cell count, then configured values that
        never occur, with ``n_cells`` 0) and ``n_distinct_source_values`` /
        ``value_map_truncated`` set. Template and join entries also get
        ``rule_counts`` (``{rule: n_cells}`` over the observed rows).
    """
    crosswalk = None
    if entry.get('mapping_kind') == JOIN:
        crosswalk, _problem = crosswalk_for(_form_spec(entry), store_root)
    configured = list(entry.get('value_map') or [])
    rows: List[Dict[str, Any]] = []
    matched_config: set = set()
    for value, n_cells in observed:
        to_value, rule = translate(entry, value, crosswalk)
        rows.append({'from': _json_value(value), 'to': to_value, 'rule': rule,
                     'n_cells': int(n_cells)})
        for index, row in enumerate(configured):
            if value is not None and _same_value(value, row['from']):
                matched_config.add(index)
    n_observed = len([v for v, _ in observed if v is not None])
    truncated = n_distinct is not None and n_distinct > n_observed
    for index, row in enumerate(configured):
        if index in matched_config:
            continue
        # A configured value missing from a truncated list may simply be rare.
        rows.append({**row, 'n_cells': None if truncated else 0})
    entry['value_map'] = rows
    if entry.get('mapping_kind') in (TEMPLATE, JOIN):
        rule_counts: Dict[str, int] = {}
        for row in rows:
            rule_counts[row['rule']] = rule_counts.get(row['rule'], 0) + int(row['n_cells'] or 0)
        entry['rule_counts'] = rule_counts
    entry['n_distinct_source_values'] = n_distinct if n_distinct is not None else n_observed
    entry['value_map_truncated'] = bool(truncated)
    return entry


def redact_hidden_sources(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``entry`` without names or values of ``obs_hidden`` columns.

    Columns in ``obs_hidden`` are kept out of the public views on purpose.
    For a facet built from one, the author column name and the author side
    of the translation are removed; the facet values (already public in the
    view) stay, with cell counts summed per facet value.
    """
    if not entry.get('source_hidden'):
        return copy.deepcopy(entry)
    clean = copy.deepcopy(entry)
    clean['source_columns'] = []
    clean['sources'] = []
    clean['redacted'] = True
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in clean.get('value_map') or []:
        key = (_value_key(row.get('to')), row.get('rule'))
        slot = merged.setdefault(key, {'from': None, 'to': row.get('to'),
                                       'rule': row.get('rule'), 'n_cells': None})
        if row.get('n_cells') is not None:
            slot['n_cells'] = (slot['n_cells'] or 0) + int(row['n_cells'])
    clean['value_map'] = list(merged.values())
    clean.pop('n_distinct_source_values', None)
    return clean


_CAMEL_BOUNDARY = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
_TOKEN_SPLIT = re.compile(r'[^a-z0-9]+')


def _name_tokens(name: str) -> List[str]:
    spaced = _CAMEL_BOUNDARY.sub('_', name)
    return [t for t in _TOKEN_SPLIT.split(spaced.lower()) if t]


def candidate_columns(columns: Iterable[str], keywords: Iterable[str]) -> List[str]:
    """Author columns whose names suggest they carry a facet.

    A keyword of four letters or more matches anywhere in the lower-cased
    name (``braak`` in ``BraakStage``); shorter ones (``age``, ``sex``,
    ``dx``) must equal a whole name token, split on punctuation and
    camelCase, so ``passage`` or ``Sussex`` do not match. Facet columns and
    module-score columns are skipped.
    """
    keyword_list = [k.lower() for k in keywords if k]
    if not keyword_list:
        return []
    hits: List[str] = []
    for column in columns:
        if not isinstance(column, str) or not column:
            continue
        if column.startswith(_NON_AUTHOR_PREFIXES) or '::' in column:
            continue
        lowered = column.lower()
        tokens = set(_name_tokens(column))
        for keyword in keyword_list:
            if len(keyword) >= 4:
                if keyword in lowered or keyword in tokens:
                    hits.append(column)
                    break
            elif keyword in tokens:
                hits.append(column)
                break
    return hits


__all__ = [
    'FACET_PREFIX', 'RENAMED', 'DERIVED', 'TEMPLATE', 'JOIN', 'CONSTANT', 'NATIVE', 'COMPUTED',
    'COMPUTED_FACETS', 'RULE_MAPPED', 'RULE_HIDDEN', 'RULE_PASSTHROUGH', 'RULE_DEFAULT',
    'RULE_TEMPLATE', 'RULE_CROSSWALK', 'RULE_FALLBACK', 'RULE_UNMATCHED', 'RULE_NULL',
    'is_facet', 'project_dataset', 'translate', 'translate_observed',
    'redact_hidden_sources', 'candidate_columns',
]
