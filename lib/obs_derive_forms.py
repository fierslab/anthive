"""The template and join forms of ``api.obs_derive`` (used for ``ant_donor_uid``).

A dataset ``.md`` derives a column in one of three forms:

* **map** (the original form): ``{source, dtype, map, default}``, a value
  map over a source column. Handled in ``metadata`` / ``anthive_db``.
* **template**: ``{source, template: '{group_id}:{value}'}``. Every non-NULL
  source value is rendered through the template. Placeholders: ``{value}``
  (required) and ``{group_id}`` (the dataset's 3-character group id).
* **join**: ``{source, join: donor_crosswalk.tsv, cohort: rosmap,
  fallback_template: '{group_id}:{value}'}``. The source value is looked up
  as an ``alias`` of the named ``cohort`` in a crosswalk table that lives in
  the store root; the match gives its ``canonical_uid``. Values without a
  match get the ``fallback_template`` result, or NULL without one.

Template and join derives are categorical (``dtype`` defaults to
``categorical``; anything else is refused).

**Value normalisation** (same in Python and SQL): the source value is cast
to text and trimmed; an integer written as a float (``1234.0``, also as
text) loses its ``.0``; an empty string and a numerical NaN count as NULL.
A NULL source value always gives NULL, also in the join form with a
fallback.

**View SQL.** The served view must not depend on any external file at query
time, so the join form embeds the cohort's alias table into the view as a
scalar subquery over a ``VALUES`` list. The table is read when the view is
built (``ant-convert``, ``/admin/refresh-views``, ``ant-rebuild-obs-views``,
``AnnotationWriter`` view refresh); rebuilding re-reads it (the parsed
table is cached by path, mtime and size).

The crosswalk table (``donor_crosswalk.tsv``) has the columns ``cohort``,
``alias_type``, ``alias``, ``canonical_uid`` (plus ``source`` and
``n_datasets``, unused here). Cohort names compare case-insensitively. An
alias that maps to two different uids within one cohort is a conflict: it
is left out of the lookup (treated as not found) and reported by the
linter. Crosswalk content can be controlled-access (ROSMAP); nothing in
this module logs or returns more than the aliases a caller asks about.
"""
from __future__ import annotations

import csv
import math
import re
import string
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

FORM_MAP = 'map'
FORM_TEMPLATE = 'template'
FORM_JOIN = 'join'

PLACEHOLDERS = ('group_id', 'value')
CROSSWALK_COLUMNS = ('cohort', 'alias', 'canonical_uid')

# Rules for one translated value (facet_mapping value tables).
RULE_TEMPLATE = 'template'      # template form: rendered
RULE_CROSSWALK = 'crosswalk'    # join form: alias found in the crosswalk
RULE_FALLBACK = 'fallback'      # join form: not found, fallback_template rendered
RULE_UNMATCHED = 'unmatched'    # join form: not found, no fallback: NULL
RULE_NULL = 'null'              # NULL (or empty) source value: NULL in both forms

_FLOAT_INT_RE = re.compile(r'^(-?[0-9]+)\.0+$')


class DeriveFormError(ValueError):
    """An invalid template or join derive spec."""


# ── spec validation ──────────────────────────────────────────────────────────

def derive_form(spec: Any) -> Optional[str]:
    """``map``, ``template``, ``join`` or None (not a mapping / no form key)."""
    if not isinstance(spec, Mapping):
        return None
    if spec.get('join') is not None:
        return FORM_JOIN
    if spec.get('template') is not None:
        return FORM_TEMPLATE
    if spec.get('map') is not None:
        return FORM_MAP
    return None


def template_fields(template: Any) -> List[str]:
    """Placeholder names in ``template``, in order.

    Raises:
        DeriveFormError: not a string, malformed braces, an unknown
            placeholder, a format spec or conversion (``{value:>8}``,
            ``{value!r}``), or no ``{value}``.
    """
    if not isinstance(template, str) or not template:
        raise DeriveFormError('template must be a non-empty string')
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise DeriveFormError(f'template {template!r} is malformed: {exc}') from None
    names: List[str] = []
    for _literal, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS:
            raise DeriveFormError(f'template {template!r} uses unknown placeholder {{{name}}}; '
                                  f'allowed: {{group_id}}, {{value}}')
        if spec or conversion:
            raise DeriveFormError(f'template {template!r}: placeholders take no format spec or conversion')
        names.append(name)
    if 'value' not in names:
        raise DeriveFormError(f'template {template!r} has no {{value}} placeholder')
    return names


def validate_spec(target: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalised copy of a template or join derive spec.

    Returns:
        ``{source, dtype: 'categorical', form, template}`` or ``{source,
        dtype, form, join, cohort, fallback_template}``.

    Raises:
        DeriveFormError: with a message naming ``target``.
    """
    where = f'api.obs_derive[{target!r}]'
    form = derive_form(spec)
    if form not in (FORM_TEMPLATE, FORM_JOIN):
        raise DeriveFormError(f'{where} is not a template or join derive')
    present = [key for key in ('map', 'template', 'join') if spec.get(key) is not None]
    if len(present) > 1:
        raise DeriveFormError(f'{where} mixes forms ({", ".join(present)}); use one of map, template, join')
    if spec.get('default') is not None:
        raise DeriveFormError(f'{where}: default applies to the map form only'
                              + ('; use fallback_template' if form == FORM_JOIN else ''))
    source = spec.get('source')
    if not isinstance(source, str) or not source:
        raise DeriveFormError(f'{where}.source must be a non-empty string')
    dtype = spec.get('dtype') or 'categorical'
    if dtype != 'categorical':
        raise DeriveFormError(f'{where}: the {form} form publishes text; dtype must be categorical (got {dtype!r})')
    try:
        if form == FORM_TEMPLATE:
            template_fields(spec['template'])
            return {'source': source, 'dtype': dtype, 'form': form, 'template': spec['template']}
        join = spec.get('join')
        if not isinstance(join, str) or not join.strip():
            raise DeriveFormError('join must name a table in the store root, e.g. donor_crosswalk.tsv')
        join_path = Path(join.strip())
        if join_path.is_absolute() or '..' in join_path.parts:
            raise DeriveFormError(f'join {join!r} must be a relative path inside the store')
        cohort = spec.get('cohort')
        if not isinstance(cohort, str) or not cohort.strip():
            raise DeriveFormError('join needs a cohort (a cohort of the crosswalk table)')
        fallback = spec.get('fallback_template')
        if fallback is not None:
            template_fields(fallback)
        return {'source': source, 'dtype': dtype, 'form': form, 'join': join.strip(),
                'cohort': cohort.strip(), 'fallback_template': fallback}
    except DeriveFormError as exc:
        message = str(exc)
        if message.startswith(where):
            raise
        raise DeriveFormError(f'{where}: {message}') from None


def needs_group_id(spec: Mapping[str, Any]) -> bool:
    """True when the spec's template or fallback uses ``{group_id}``."""
    for key in ('template', 'fallback_template'):
        text = spec.get(key)
        if isinstance(text, str):
            try:
                if 'group_id' in template_fields(text):
                    return True
            except DeriveFormError:
                continue
    return False


# ── values ───────────────────────────────────────────────────────────────────

def normalize_value(value: Any) -> Optional[str]:
    """Text form of a source value for templates and crosswalk lookups.

    None, a float NaN and an empty (after trim) string give None. A float
    holding an integer and a text ``1234.0`` give ``'1234'``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        text = str(value)
    elif isinstance(value, float):
        if math.isnan(value):
            return None
        text = str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    elif isinstance(value, int):
        text = str(value)
    else:
        try:
            if value != value:  # pandas NA / numpy NaN
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(value, 'item') and not isinstance(value, str):  # numpy scalar
            return normalize_value(value.item())
        text = str(value)
    text = text.strip()
    text = _FLOAT_INT_RE.sub(r'\1', text)
    return text or None


def render_template(template: str, value: Any, group_id: Optional[str]) -> Optional[str]:
    """``template`` with ``{value}`` / ``{group_id}`` filled; None for a NULL value."""
    text = normalize_value(value)
    if text is None:
        return None
    return template.format(value=text, group_id='' if group_id is None else str(group_id))


# ── crosswalk ────────────────────────────────────────────────────────────────

class CrosswalkError(ValueError):
    """The crosswalk table cannot be read or lacks a required column."""


@dataclass(frozen=True)
class Crosswalk:
    """Parsed alias -> canonical uid table, per cohort (cohort keys lower case)."""

    path: Path
    by_cohort: Mapping[str, Mapping[str, str]]
    conflicts: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    def cohorts(self) -> List[str]:
        return sorted(self.by_cohort)

    def has_cohort(self, cohort: str) -> bool:
        return str(cohort).strip().lower() in self.by_cohort

    def table(self, cohort: str) -> Mapping[str, str]:
        """Alias -> uid of one cohort (empty for an unknown cohort)."""
        return self.by_cohort.get(str(cohort).strip().lower(), {})

    def lookup(self, cohort: str, value: Any) -> Optional[str]:
        key = normalize_value(value)
        if key is None:
            return None
        return self.table(cohort).get(key)


_CROSSWALK_CACHE: Dict[Tuple[str, int, int], Crosswalk] = {}
_CROSSWALK_LOCK = threading.Lock()


def load_crosswalk(path: Path) -> Crosswalk:
    """Read a crosswalk TSV, cached by (path, mtime, size).

    Raises:
        FileNotFoundError: no file at ``path``.
        CrosswalkError: unreadable, or a required column is missing.
    """
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _CROSSWALK_LOCK:
        cached = _CROSSWALK_CACHE.get(key)
    if cached is not None:
        return cached
    seen: Dict[Tuple[str, str], str] = {}
    conflicts: set = set()
    try:
        with open(path, newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle, delimiter='\t')
            missing = [c for c in CROSSWALK_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise CrosswalkError(f'{path.name} lacks column(s) {", ".join(missing)}')
            for row in reader:
                cohort = str(row.get('cohort') or '').strip().lower()
                alias = normalize_value(row.get('alias'))
                uid = str(row.get('canonical_uid') or '').strip()
                if not cohort or alias is None or not uid:
                    continue
                previous = seen.get((cohort, alias))
                if previous is not None and previous != uid:
                    conflicts.add((cohort, alias))
                seen[(cohort, alias)] = uid
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise CrosswalkError(f'cannot read {path.name}: {exc}') from None
    by_cohort: Dict[str, Dict[str, str]] = {}
    for (cohort, alias), uid in seen.items():
        if (cohort, alias) in conflicts:
            continue
        by_cohort.setdefault(cohort, {})[alias] = uid
    result = Crosswalk(path=path, by_cohort=by_cohort, conflicts=tuple(sorted(conflicts)))
    with _CROSSWALK_LOCK:
        stale = [k for k in _CROSSWALK_CACHE if k[0] == key[0]]
        for old in stale:
            del _CROSSWALK_CACHE[old]
        _CROSSWALK_CACHE[key] = result
    return result


def resolve_join_file(name: str, store_root: Optional[Path] = None) -> Path:
    """Path of a join table: under ``store_root`` when given, else the
    configured store root (``config.get_store_file``).

    Raises:
        ValueError: an absolute path or one with ``..``.
    """
    candidate = Path(str(name).strip())
    if not str(name).strip() or candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError(f'join table must be a relative path inside the store: {name!r}')
    if store_root is not None:
        return Path(store_root) / candidate
    try:
        from config import get_store_file  # noqa: PLC0415  (lib/ on sys.path)
    except ImportError:
        from lib.config import get_store_file  # type: ignore[no-redef]  # noqa: PLC0415
    return get_store_file(str(candidate))


def crosswalk_for(spec: Mapping[str, Any], store_root: Optional[Path] = None
                  ) -> Tuple[Optional[Crosswalk], Optional[str]]:
    """``(crosswalk, problem)`` for a join spec; ``problem`` is a sentence
    when the table is missing, unreadable or lacks the cohort."""
    try:
        path = resolve_join_file(spec.get('join') or '', store_root)
    except ValueError as exc:
        return None, str(exc)
    try:
        crosswalk = load_crosswalk(path)
    except FileNotFoundError:
        return None, f'join table {spec.get("join")} not found in the store root ({path.parent})'
    except CrosswalkError as exc:
        return None, str(exc)
    if not crosswalk.has_cohort(spec.get('cohort') or ''):
        return crosswalk, (f'cohort {spec.get("cohort")!r} is not in {path.name} '
                           f'(cohorts: {", ".join(crosswalk.cohorts()) or "none"})')
    return crosswalk, None


def translate_value(spec: Mapping[str, Any], value: Any, group_id: Optional[str],
                    crosswalk: Optional[Crosswalk] = None) -> Tuple[Optional[str], str]:
    """``(published value, rule)`` of one source value under a template or join spec."""
    form = derive_form(spec)
    if normalize_value(value) is None:
        # NULL in, NULL out, in both forms (no fallback for NULL).
        return None, RULE_NULL
    if form == FORM_TEMPLATE:
        return render_template(spec['template'], value, group_id), RULE_TEMPLATE
    uid = crosswalk.lookup(spec.get('cohort') or '', value) if crosswalk is not None else None
    if uid is not None:
        return uid, RULE_CROSSWALK
    fallback = spec.get('fallback_template')
    if fallback:
        return render_template(fallback, value, group_id), RULE_FALLBACK
    return None, RULE_UNMATCHED


# ── SQL ──────────────────────────────────────────────────────────────────────

def _sql_str(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def value_text_sql(column_sql: str, numeric: bool) -> str:
    """SQL mirror of ``normalize_value`` over one source column."""
    text = (f"NULLIF(regexp_replace(trim(CAST({column_sql} AS VARCHAR)), "
            f"'^(-?[0-9]+)\\.0+$', '\\1'), '')")
    if numeric:
        return f"CASE WHEN isnan(TRY_CAST({column_sql} AS DOUBLE)) THEN NULL ELSE {text} END"
    return text


def template_sql(template: str, text_sql: str, group_id: Optional[str]) -> str:
    """SQL rendering ``template`` over the normalised text ``text_sql``;
    NULL when the text is NULL."""
    parts: List[str] = []
    for literal, name, _spec, _conversion in string.Formatter().parse(template):
        if literal:
            parts.append(_sql_str(literal))
        if name == 'value':
            parts.append(text_sql)
        elif name == 'group_id':
            parts.append(_sql_str('' if group_id is None else group_id))
    body = ' || '.join(parts) if parts else "''"
    return f"CASE WHEN {text_sql} IS NULL THEN NULL ELSE ({body}) END"


def join_sql(table: Mapping[str, str], text_sql: str) -> str:
    """Scalar subquery: the uid of ``text_sql`` in the embedded alias table."""
    if not table:
        return 'CAST(NULL AS VARCHAR)'
    rows = ', '.join(f'({_sql_str(alias)}, {_sql_str(uid)})' for alias, uid in sorted(table.items()))
    return (f"(SELECT xw.canonical_uid FROM (VALUES {rows}) AS xw(alias, canonical_uid) "
            f"WHERE xw.alias = {text_sql})")


def derive_sql(spec: Mapping[str, Any], column_sql: str, numeric: bool, group_id: Optional[str],
               store_root: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(expression, problem)`` for a template or join derive.

    ``problem`` (and no expression) when the column cannot be built: a
    template needs ``{group_id}`` but none is known, the join table is
    missing or lacks the cohort. The caller leaves the column out and logs
    the problem, as for a missing source column.
    """
    if needs_group_id(spec) and not group_id:
        return None, 'the template uses {group_id} but the dataset group id is unknown'
    text = value_text_sql(column_sql, numeric)
    if derive_form(spec) == FORM_TEMPLATE:
        return template_sql(spec['template'], text, group_id), None
    crosswalk, problem = crosswalk_for(spec, store_root)
    if problem is not None or crosswalk is None:
        return None, problem or 'join table unavailable'
    expression = join_sql(crosswalk.table(spec['cohort']), text)
    fallback = spec.get('fallback_template')
    if fallback:
        expression = f"COALESCE({expression}, {template_sql(fallback, text, group_id)})"
    return expression, None


# ── group id of a dataset ────────────────────────────────────────────────────

def api_source_columns(api_config: Optional[Mapping[str, Any]]) -> List[str]:
    """Author obs columns the ``api:`` section reads, in .md order, without duplicates.

    ``obs_rename`` keys, the ``source`` of every ``obs_derive`` (all forms),
    ``obs_value_rename`` and ``obs_value_hidden`` keys. ``ant-convert`` stores
    them whatever ``obscat_include`` / ``obsnum_include`` say: a mapping whose
    source is not stored publishes nothing (Francken2026, 2026-10-20).
    """
    api = api_config or {}
    columns: List[str] = []

    def add(name: Any) -> None:
        text = str(name or '').strip()
        if text and text not in columns:
            columns.append(text)

    for key in (api.get('obs_rename') or {}):
        add(key)
    for spec in (api.get('obs_derive') or {}).values():
        if isinstance(spec, Mapping):
            add(spec.get('source'))
    for section in ('obs_value_rename', 'obs_value_hidden'):
        for key in (api.get(section) or {}):
            add(key)
    return columns


def group_id_for_md(md_path: Path, max_levels: int = 4) -> Optional[str]:
    """The 3-character group id of the dataset described by ``md_path``.

    The dataset ``.md``'s own ``group_id`` wins; otherwise the nearest
    ``index.md`` upwards (same folder first) that has one. None when none
    is found or a file cannot be parsed.
    """
    try:
        from metadata import parse_md_file  # noqa: PLC0415
    except ImportError:
        try:
            from lib.metadata import parse_md_file  # type: ignore[no-redef]  # noqa: PLC0415
        except ImportError:
            return None
    md_path = Path(md_path)
    try:
        own, _ = parse_md_file(md_path)
        if own and str(own.get('group_id') or '').strip():
            return str(own['group_id']).strip()
    except Exception:
        pass
    folder = md_path.parent
    for _ in range(max_levels):
        index = folder / 'index.md'
        if index.exists():
            try:
                meta, _ = parse_md_file(index)
            except Exception:
                meta = None
            if meta and str(meta.get('group_id') or '').strip():
                return str(meta['group_id']).strip()
        if folder.parent == folder:
            break
        folder = folder.parent
    return None


__all__ = [
    'FORM_MAP', 'FORM_TEMPLATE', 'FORM_JOIN', 'PLACEHOLDERS',
    'RULE_TEMPLATE', 'RULE_CROSSWALK', 'RULE_FALLBACK', 'RULE_UNMATCHED', 'RULE_NULL',
    'DeriveFormError', 'CrosswalkError', 'Crosswalk',
    'derive_form', 'template_fields', 'validate_spec', 'needs_group_id',
    'normalize_value', 'render_template', 'load_crosswalk', 'resolve_join_file', 'crosswalk_for',
    'translate_value', 'value_text_sql', 'template_sql', 'join_sql', 'derive_sql', 'group_id_for_md',
]
