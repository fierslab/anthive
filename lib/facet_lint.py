"""Lint a dataset's ``api:`` facet mapping and its lineage keys.

Used by ``lib/md_check.py`` (``ant md-check``). Three groups of rules:

* **Namespace (decision L1).** ``ant_*`` is closed: every ``ant_*`` target
  of ``obs_rename``, ``obs_derive`` or ``obs_constant`` must be a tag of
  ``lib/obscol_tags_vocab.yaml`` with ``status: registered``. A deprecated
  tag is a warning naming its replacement. An unregistered tag is an error
  in strict mode and a warning otherwise (``NAMESPACE_DEFAULT_MODE``),
  because existing files still carry singleton facets while they migrate.
* **Mapping (decision L2).** Static rules on the ``.md`` alone: rename
  chains, a derive with the wrong dtype for the facet (a string map on a
  numerical facet), non-numeric values written into a numerical facet,
  values outside the facet's range (Braak 0 to 6, whole numbers), values
  outside a controlled vocabulary, and a declared ``ant_braak_group`` map
  that disagrees with the central rule. Data rules need the dataset's
  ``.duckdb`` (optional): a derive or rename source column that does not
  exist, non-NULL source values with neither a map entry nor a default,
  and text or out-of-range values that reach a numerical facet.
* **Lineage (decision L3).** Every ``reanalysis_of`` target exists and a
  dataset with ``reanalysis_of`` has ``is_secondary: true``.

Each rule yields a :class:`Finding` with a stable ``rule`` id, so callers can
count violations per rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from obscol_tags import facet_registry

FACET_PREFIX = 'ant_'

ERROR = 'error'
WARNING = 'warning'

#: How an unregistered ``ant_*`` target is reported when the caller does not
#: choose. ``warn`` while the store still has singleton facets (L1 transition);
#: switch to ``error`` once ``ant md-check --strict-namespace`` passes.
NAMESPACE_DEFAULT_MODE = 'warn'

#: Rule ids, with one line each (printed by ant-md-check's summary).
RULES: Dict[str, str] = {
    'namespace_unregistered': 'ant_* target is not a registered facet',
    'namespace_deprecated': 'ant_* target is a deprecated facet',
    'rename_chain': 'obs_rename target is itself renamed (A -> B, B -> C)',
    'derive_dtype': 'derive dtype does not match the facet kind (string map on a numerical facet)',
    'numeric_value': 'non-numeric value written into a numerical facet',
    'numeric_range': 'value outside the facet range or not a whole number',
    'controlled_value': 'value outside the facet vocabulary',
    'braak_group_rule': 'ant_braak_group map disagrees with 0-2 low, 3-4 medium, 5-6 high',
    'source_missing': 'derive source column is not in the dataset',
    'rename_source_missing': 'obs_rename source column is not in the dataset',
    'unmapped_value': 'non-NULL source value with no map entry and no default',
    'numeric_from_text': 'numerical facet published from a text column',
    'reanalysis_target_missing': 'reanalysis_of names a dataset that does not exist',
    'reanalysis_not_secondary': 'reanalysis_of is set but is_secondary is not true',
    'donor_group_format': 'donor_group is not a lower-case slug',
}

_ROMAN = {'0': 0, 'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7}
_DONOR_GROUP_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


@dataclass(frozen=True)
class Finding:
    """One rule violation in one file."""

    severity: str
    rule: str
    message: str


def braak_group_of(stage: Any) -> Optional[str]:
    """Central Braak grouping: 0-2 low, 3-4 medium, 5-6 high, else None."""
    number = _as_number(stage)
    if number is None or not float(number).is_integer():
        return None
    stage_int = int(number)
    if 0 <= stage_int <= 2:
        return 'low'
    if 3 <= stage_int <= 4:
        return 'medium'
    if 5 <= stage_int <= 6:
        return 'high'
    return None


def parse_braak_label(label: Any) -> Optional[int]:
    """Braak stage of an author label: ``4``, ``4.0``, ``IV``, ``Braak IV``."""
    number = _as_number(label)
    if number is not None:
        return int(number) if float(number).is_integer() else None
    text = str(label).strip()
    text = re.sub(r'^(braak)\s*(stage)?\s*', '', text, flags=re.I).strip()
    number = _as_number(text)
    if number is not None:
        return int(number) if float(number).is_integer() else None
    return _ROMAN.get(text.upper())


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _facet_of(target: Any) -> Optional[str]:
    if isinstance(target, str) and target.startswith(FACET_PREFIX) and len(target) > len(FACET_PREFIX):
        return target[len(FACET_PREFIX):]
    return None


def _written_values(api: Mapping[str, Any]) -> List[Tuple[str, str, Any]]:
    """(facet column, where, value) for every value the mapping writes into an ant_* column."""
    out: List[Tuple[str, str, Any]] = []
    rename = api.get('obs_rename') or {}
    for source, target in rename.items():
        if _facet_of(target) is None:
            continue
        for old, new in ((api.get('obs_value_rename') or {}).get(source) or {}).items():
            if new is not None:
                out.append((target, f'obs_value_rename[{source}][{old}]', new))
    for target, spec in (api.get('obs_derive') or {}).items():
        if _facet_of(target) is None or not isinstance(spec, Mapping):
            continue
        for old, new in (spec.get('map') or {}).items():
            if new is not None:
                out.append((target, f'obs_derive[{target}].map[{old}]', new))
        if spec.get('default') is not None:
            out.append((target, f'obs_derive[{target}].default', spec.get('default')))
    for target, spec in (api.get('obs_constant') or {}).items():
        if _facet_of(target) is None or not isinstance(spec, Mapping):
            continue
        if spec.get('value') is not None:
            out.append((target, f'obs_constant[{target}].value', spec.get('value')))
    return out


def _targets(api: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(ant_* column, how it is declared) for every facet target."""
    out: List[Tuple[str, str]] = []
    for source, target in (api.get('obs_rename') or {}).items():
        if _facet_of(target) is not None:
            out.append((target, f'obs_rename[{source}]'))
    for key in ('obs_derive', 'obs_constant'):
        for target in (api.get(key) or {}):
            if _facet_of(target) is not None:
                out.append((target, key))
    return out


def lint_api(api: Mapping[str, Any], namespace_mode: str = NAMESPACE_DEFAULT_MODE) -> List[Finding]:
    """Static rules on one ``api:`` section (no data needed).

    Args:
        api: The raw ``api:`` mapping of a dataset ``.md``.
        namespace_mode: ``warn`` or ``error`` for unregistered ``ant_*`` targets.

    Returns:
        Findings in a stable order.
    """
    registry = facet_registry()
    findings: List[Finding] = []
    if not isinstance(api, Mapping):
        return findings

    seen: Set[str] = set()
    for column, how in _targets(api):
        if column in seen:
            continue
        seen.add(column)
        tag = _facet_of(column)
        entry = registry.get(tag)
        if entry is None:
            severity = ERROR if namespace_mode == 'error' else WARNING
            findings.append(Finding(severity, 'namespace_unregistered',
                                    f'{column} ({how}) is not a registered facet; publish it as a plain '
                                    f'column or register the tag in lib/obscol_tags_vocab.yaml'))
        elif entry['status'] == 'deprecated':
            replacement = ', '.join(FACET_PREFIX + t for t in entry['replaced_by']) or 'a plain column'
            findings.append(Finding(WARNING, 'namespace_deprecated',
                                    f'{column} ({how}) is deprecated; use {replacement}'))

    rename = api.get('obs_rename') or {}
    for source, target in rename.items():
        if target in rename and target != source:
            findings.append(Finding(ERROR, 'rename_chain',
                                    f'obs_rename chain {source} -> {target} -> {rename[target]}: the view '
                                    f'applies one hop only, so {source} is published as {target}, not '
                                    f'{rename[target]}'))

    for target, spec in (api.get('obs_derive') or {}).items():
        entry = registry.get(_facet_of(target) or '')
        if entry is None or not isinstance(spec, Mapping):
            continue
        dtype = spec.get('dtype')
        if dtype and dtype != entry['kind']:
            findings.append(Finding(ERROR, 'derive_dtype',
                                    f'obs_derive[{target}] has dtype {dtype} but {target} is {entry["kind"]}'))

    for column, where, value in _written_values(api):
        entry = registry.get(_facet_of(column) or '')
        if entry is None:
            continue
        if entry['kind'] == 'numerical':
            number = _as_number(value)
            if number is None:
                findings.append(Finding(ERROR, 'numeric_value',
                                        f'{where} writes {value!r} into numerical {column}'))
                continue
            low_high = entry['range']
            if low_high is not None and not (low_high[0] <= number <= low_high[1]):
                findings.append(Finding(ERROR, 'numeric_range',
                                        f'{where} writes {value!r} into {column}, outside '
                                        f'{_fmt(low_high[0])} to {_fmt(low_high[1])}'))
            elif entry['integer'] and not number.is_integer():
                findings.append(Finding(ERROR, 'numeric_range',
                                        f'{where} writes {value!r} into {column}, not a whole number'))
        elif entry['controlled_values'] and str(value) not in entry['controlled_values']:
            findings.append(Finding(WARNING, 'controlled_value',
                                    f'{where} writes {value!r} into {column}; allowed: '
                                    f'{", ".join(entry["controlled_values"])}'))

    group_spec = (api.get('obs_derive') or {}).get('ant_braak_group')
    if isinstance(group_spec, Mapping):
        bad = []
        for label, group in (group_spec.get('map') or {}).items():
            stage = parse_braak_label(label)
            expected = braak_group_of(stage) if stage is not None else None
            if stage is not None and group is not None and expected != group:
                bad.append(f'{label}: {group} (rule: {expected})')
        if bad:
            findings.append(Finding(WARNING, 'braak_group_rule',
                                    'obs_derive[ant_braak_group] disagrees with the central rule: '
                                    + '; '.join(bad)))
    return findings


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


# ── data rules ───────────────────────────────────────────────────────────────

ValueLookup = Callable[[str], Optional[List[Tuple[Any, int]]]]


def lint_api_data(api: Mapping[str, Any], columns: Mapping[str, str],
                  distinct_values: ValueLookup, max_listed: int = 5) -> List[Finding]:
    """Rules that need the dataset's stored columns and values.

    Args:
        api: The raw ``api:`` mapping.
        columns: Stored obs column name -> ``categorical`` (``__obscat``) or
            ``numerical`` (``__obsnum``).
        distinct_values: Column name -> ``[(value, n_cells), ...]`` of the
            distinct non-NULL stored values, or None when unreadable.
        max_listed: Values named per finding.

    Returns:
        Findings in a stable order.
    """
    registry = facet_registry()
    findings: List[Finding] = []
    if not isinstance(api, Mapping):
        return findings
    hidden_values = api.get('obs_value_hidden') or {}
    value_rename = api.get('obs_value_rename') or {}

    for source, target in (api.get('obs_rename') or {}).items():
        if _facet_of(target) is None:
            continue
        if source not in columns:
            findings.append(Finding(WARNING, 'rename_source_missing',
                                    f'obs_rename source {source} (for {target}) is not a stored column; '
                                    f'{target} is not published'))
            continue
        entry = registry.get(_facet_of(target) or '')
        if entry is None or entry['kind'] != 'numerical':
            continue
        values = distinct_values(source) or []
        renamed = {str(k): v for k, v in (value_rename.get(source) or {}).items()}
        hidden = {str(v) for v in (hidden_values.get(source) or [])}
        published = []
        for value, n_cells in values:
            key = _value_key(value)
            if key in hidden:
                continue
            published.append((renamed.get(key, value), n_cells))
        _check_numeric_values(findings, target, entry, published, f'source {source}', max_listed)
        if columns[source] == 'categorical':
            findings.append(Finding(WARNING, 'numeric_from_text',
                                    f'{target} is numerical but is renamed from text column {source}; it '
                                    f'is published in obscat as text'))

    for target, spec in (api.get('obs_derive') or {}).items():
        if not isinstance(spec, Mapping):
            continue
        source = spec.get('source')
        if source not in columns:
            findings.append(Finding(ERROR, 'source_missing',
                                    f'obs_derive[{target}] source {source} is not a stored column; the '
                                    f'view builder drops {target}'))
            continue
        if spec.get('default') is not None:
            continue
        # Mirror the view SQL: ``col = '<key>'`` compares text for a text
        # column and numbers for a numerical one (DuckDB casts the literal).
        text_keys = {str(k) for k in (spec.get('map') or {})}
        numeric_keys = {n for n in (_as_number(k) for k in (spec.get('map') or {})) if n is not None}
        missing = []
        for value, n_cells in distinct_values(source) or []:
            if value is None:
                continue
            if columns[source] == 'numerical':
                if _as_number(value) in numeric_keys:
                    continue
            elif str(value) in text_keys:
                continue
            missing.append((value, n_cells))
        if missing:
            listed = ', '.join(f'{v!r} ({n} cells)' for v, n in missing[:max_listed])
            more = f' and {len(missing) - max_listed} more' if len(missing) > max_listed else ''
            findings.append(Finding(WARNING, 'unmapped_value',
                                    f'obs_derive[{target}] maps {len(missing)} non-NULL value(s) of {source} '
                                    f'to NULL without a map entry or default: {listed}{more}. Add the '
                                    f'values to the map (null for an intended NULL)'))
    return findings


def _value_key(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _check_numeric_values(findings: List[Finding], target: str, entry: Mapping[str, Any],
                          values: Sequence[Tuple[Any, int]], where: str, max_listed: int) -> None:
    text = [(v, n) for v, n in values if v is not None and _as_number(v) is None]
    if text:
        listed = ', '.join(f'{v!r} ({n} cells)' for v, n in text[:max_listed])
        findings.append(Finding(ERROR, 'numeric_value',
                                f'{target} ({where}) carries non-numeric values: {listed}'))
    low_high = entry.get('range')
    outside = []
    for value, n_cells in values:
        number = _as_number(value)
        if number is None:
            continue
        if low_high is not None and not (low_high[0] <= number <= low_high[1]):
            outside.append((value, n_cells))
        elif entry.get('integer') and not number.is_integer():
            outside.append((value, n_cells))
    if outside:
        listed = ', '.join(f'{v!r} ({n} cells)' for v, n in outside[:max_listed])
        findings.append(Finding(ERROR, 'numeric_range',
                                f'{target} ({where}) carries values outside its range: {listed}'))


# ── lineage ──────────────────────────────────────────────────────────────────

def lint_lineage(metadata: Mapping[str, Any], known_db_ids: Iterable[str]) -> List[Finding]:
    """``reanalysis_of`` targets exist and imply ``is_secondary: true``;
    ``donor_group`` is a slug."""
    findings: List[Finding] = []
    known = set(known_db_ids)
    targets = metadata.get('reanalysis_of')
    if targets is not None:
        if isinstance(targets, str):
            targets = [targets]
        for target in targets or []:
            if str(target) not in known:
                findings.append(Finding(ERROR, 'reanalysis_target_missing',
                                        f'reanalysis_of names {target}, which is not a dataset in the store'))
        if targets and metadata.get('is_secondary') is not True:
            findings.append(Finding(ERROR, 'reanalysis_not_secondary',
                                    f'reanalysis_of is set ({", ".join(map(str, targets))}) but is_secondary '
                                    f'is {metadata.get("is_secondary")!r}'))
    donor_group = metadata.get('donor_group')
    if donor_group is not None and not _DONOR_GROUP_RE.match(str(donor_group)):
        findings.append(Finding(WARNING, 'donor_group_format',
                                f'donor_group {donor_group!r} is not a lower-case slug'))
    return findings


# ── reading a dataset's duckdb ───────────────────────────────────────────────

def duckdb_reader(duckdb_path: Path) -> Optional[Tuple[Dict[str, str], ValueLookup, Callable[[], None]]]:
    """Open a dataset's ``.duckdb`` read-only for the data rules.

    Returns:
        ``(columns, distinct_values, close)`` or None when duckdb is not
        installed or the file cannot be opened (for example while a writer
        holds it).
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return None
    try:
        connection = duckdb.connect(str(duckdb_path), read_only=True)
    except Exception:
        return None
    columns: Dict[str, str] = {}
    try:
        for table, kind in (('__obscat', 'categorical'), ('__obsnum', 'numerical')):
            rows = connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()
            for (name,) in rows:
                if name != 'cell_id':
                    columns.setdefault(name, kind)
    except Exception:
        connection.close()
        return None

    def distinct_values(column: str) -> Optional[List[Tuple[Any, int]]]:
        table = '__obscat' if columns.get(column) == 'categorical' else '__obsnum'
        quoted = '"' + column.replace('"', '""') + '"'
        try:
            return connection.execute(
                f'SELECT {quoted}, COUNT(*) FROM {table} WHERE {quoted} IS NOT NULL '
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT 5000').fetchall()
        except Exception:
            return None

    return columns, distinct_values, connection.close


__all__ = [
    'ERROR', 'WARNING', 'NAMESPACE_DEFAULT_MODE', 'RULES', 'Finding',
    'braak_group_of', 'parse_braak_label', 'lint_api', 'lint_api_data', 'lint_lineage',
    'duckdb_reader',
]
