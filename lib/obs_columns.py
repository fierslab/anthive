"""Obs-columns deposits: new obs columns for a published dataset from one TSV (2.47.0, design section 26).

A depositor with the right to update the dataset (its owner, an admin, or ``update_policy: any``) sends one
``.tsv`` or ``.tsv.gz`` with a ``cell_name`` column and one or more value columns, and declares each column
(``name``, ``dtype`` categorical or numerical, optional ``description`` and ``source``). The rules live here so
the API (create and complete) and the watcher (publication) apply the same ones:

* names follow ant-annotate's identifier rule (``anthive_db._validate_anno_col_name``), are not reserved
  (``cell_id``, ``cell_name``, ``gene_id``, ``gene_name``), not in the ``ant_*`` facet namespace unless the
  column says ``"facet": true``, unique case-insensitively, and do not clash with an existing obs column of the
  dataset (DuckDB names are case-insensitive) unless ``replace`` is set by the owner or an admin;
* the TSV header is exactly ``cell_name`` plus the declared columns; numerical values parse as numbers, a
  categorical column has at most ``MAX_CATEGORIES`` distinct values; at most ``MAX_COLUMNS`` columns and
  ``MAX_ROWS`` rows;
* at publication every cell name must be a cell of the dataset (any unknown name fails the deposit: the TSV
  belongs to another dataset or version); cells without a value are allowed and reported (``coverage``);
* values that match the donor crosswalk hold the deposit for an admin (``donor_identifiers``), and a public
  dataset holds it (``public_access``), as other deposits.

Publication (``ArtefactPublisher.publish_obs_columns``) writes the columns with ``AnnotationWriter`` (the
ant-annotate path: ALTER TABLE, ``__pending_cols``, view rebuild, ant-serve release and restore), bumps the
dataset a minor version (additive, design section 25) with a changelog line per column and a caveat, pushes
the ``.duckdb`` to the catalog and commits the ``.md``. The h5ad is not touched: ``__pending_cols`` marks the
columns for the watcher's merge in a quiet window (section 26, ``lib/h5ad_merge.py``).
"""
from __future__ import annotations

import gzip
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

DEPOSIT_KIND = 'obs_columns'
DTYPES = ('categorical', 'numerical')
MAX_COLUMNS = 50
MAX_ROWS = 20_000_000
MAX_CATEGORIES = 10_000
DESCRIPTION_MAX_CHARS = 500
SOURCE_MAX_CHARS = 300
FILE_SUFFIXES = ('.tsv', '.tsv.gz')
RESERVED = {'cell_id', 'cell_name', 'gene_id', 'gene_name'}
FACET_PREFIX = 'ant_'
ANNOTATOR = 'obs-columns-deposit'


class ObsColumnsError(ValueError):
    """A declaration or table that cannot be taken; ``code`` is the API status word."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _clean(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 and ch not in '\t' for ch in value):
        raise ObsColumnsError('invalid_columns', f'text fields are single lines of at most {limit} characters')
    return value.strip() or None


def validate_columns(columns: Any, existing: Sequence[str], replace: bool = False,
                     may_replace: bool = False, max_columns: int = MAX_COLUMNS) -> List[Dict[str, Any]]:
    """The declared columns, checked and normalised; ObsColumnsError otherwise.

    ``existing``: the dataset's obs column names (case-insensitive clash check). ``replace``: the depositor
    asks to overwrite existing columns of the same name, allowed only when ``may_replace`` (owner or admin).
    """
    from anthive_db import _validate_anno_col_name  # noqa: PLC0415

    if not isinstance(columns, list) or not columns:
        raise ObsColumnsError('invalid_columns', 'columns: a list of {name, dtype, description?, source?}')
    if len(columns) > max_columns:
        raise ObsColumnsError('invalid_columns', f'at most {max_columns} columns per deposit')
    if replace and not may_replace:
        raise ObsColumnsError('not_owner', 'replace: only the owner of the dataset or an admin replaces columns')
    taken = {name.lower() for name in existing}
    seen: set = set()
    out = []
    for item in columns:
        if not isinstance(item, dict) or set(item) - {'name', 'dtype', 'description', 'source', 'facet'}:
            raise ObsColumnsError('invalid_columns', 'each column is {name, dtype, description?, source?, facet?}')
        name = item.get('name')
        if isinstance(name, str) and name.lower() in RESERVED:
            raise ObsColumnsError('reserved_column_name', f'{name} is reserved')
        try:
            _validate_anno_col_name(name)
        except ValueError as exc:
            raise ObsColumnsError('invalid_column_name', str(exc)) from None
        lower = name.lower()
        if lower.startswith(FACET_PREFIX) and item.get('facet') is not True:
            raise ObsColumnsError('reserved_column_name', f'{name}: the ant_* names are the facet namespace; set '
                                                          f'"facet": true to map a facet column explicitly')
        if lower in seen:
            raise ObsColumnsError('invalid_columns', f'{name} is declared twice (names are case-insensitive)')
        seen.add(lower)
        if lower in taken and not replace:
            raise ObsColumnsError('column_exists', f'{name} clashes with an existing obs column of the dataset '
                                                   f'(names are case-insensitive); choose another name, or set '
                                                   f'"replace": true (owner or admin)')
        dtype = item.get('dtype')
        if dtype not in DTYPES:
            raise ObsColumnsError('invalid_columns', f'{name}: dtype is categorical or numerical')
        out.append({'name': name, 'dtype': dtype, 'description': _clean(item.get('description'), DESCRIPTION_MAX_CHARS),
                    'source': _clean(item.get('source'), SOURCE_MAX_CHARS), 'facet': item.get('facet') is True,
                    'replaces': lower in taken})
    return out


def file_name_problem(name: Any) -> Optional[str]:
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}', name) \
            or not name.lower().endswith(FILE_SUFFIXES):
        return 'the table is one file named <name>.tsv or <name>.tsv.gz (letters, digits, ._-)'
    return None


def first_line(raw: bytes, name: str) -> str:
    """The header line of the first bytes of a table (gunzipped for ``.tsv.gz``)."""
    if name.lower().endswith('.gz'):
        import zlib  # noqa: PLC0415
        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, 256 * 1024)
    return raw.split(b'\n', 1)[0].decode('utf-8', errors='replace').rstrip('\r')


def header_problems(header: str, specs: Sequence[Mapping[str, Any]]) -> List[str]:
    """The header must be ``cell_name`` then exactly the declared columns (any order)."""
    fields = header.split('\t')
    if not fields or fields[0] != 'cell_name':
        return ['the first column of the header must be cell_name']
    declared = [spec['name'] for spec in specs]
    problems = []
    extra = [field for field in fields[1:] if field not in declared]
    missing = [name for name in declared if name not in fields[1:]]
    if extra:
        problems.append(f'columns not declared: {", ".join(extra[:10])}')
    if missing:
        problems.append(f'declared columns not in the table: {", ".join(missing[:10])}')
    if len(fields) != len(set(fields)):
        problems.append('a column name repeats in the header')
    return problems


def read_table(path: Path, specs: Sequence[Mapping[str, Any]], max_rows: int = MAX_ROWS) -> Tuple[Any, List[str]]:
    """``(DataFrame indexed by cell_name, problems)``; values typed by their declaration."""
    import pandas as pd  # noqa: PLC0415

    opener = gzip.open if str(path).lower().endswith('.gz') else open
    with opener(path, 'rb') as handle:
        header = handle.readline().decode('utf-8', errors='replace').rstrip('\r\n')
    problems = header_problems(header, specs)
    if problems:
        return None, problems
    try:
        table = pd.read_csv(path, sep='\t', dtype=str, keep_default_na=False, na_values=['', 'NA', 'NaN', 'nan'],
                            compression='infer', nrows=max_rows + 1)
    except Exception as exc:  # noqa: BLE001
        return None, [f'the table cannot be read as TSV ({type(exc).__name__}: {str(exc)[:200]})']
    if len(table) > max_rows:
        return None, [f'more than {max_rows} rows']
    if table.empty:
        return None, ['the table has no rows']
    names = table['cell_name']
    if names.isna().any():
        problems.append('empty cell_name values')
    duplicated = names[names.duplicated()].unique()[:5]
    if len(duplicated):
        problems.append(f'duplicate cell names: {", ".join(map(str, duplicated))}')
    out = pd.DataFrame(index=names.astype(str))
    for spec in specs:
        values = table[spec['name']]
        if spec['dtype'] == 'numerical':
            parsed = pd.to_numeric(values, errors='coerce')
            bad = values.notna() & parsed.isna()
            if bad.any():
                problems.append(f'{spec["name"]}: {int(bad.sum())} values are not numbers (for example '
                                f'{values[bad].iloc[0][:40]!r})')
            out[spec['name']] = parsed.to_numpy(dtype='float64')
        else:
            distinct = values.dropna().nunique()
            if distinct > MAX_CATEGORIES:
                problems.append(f'{spec["name"]}: {distinct} distinct values (at most {MAX_CATEGORIES}; a column of '
                                f'cell ids is not a category)')
            out[spec['name']] = values.to_numpy(dtype=object)
    return out, problems


def coverage(table: Any, cell_names: Sequence[str]) -> Dict[str, Any]:
    """``{cells, given, unknown, unknown_sample, without_value: {column: n}}`` of a table against the dataset."""
    cells = set(cell_names)
    given = set(table.index)
    unknown = sorted(given - cells)
    known = table[table.index.isin(cells)]
    without = {column: int(len(cells) - known[column].notna().sum()) for column in table.columns}
    return {'cells': len(cells), 'given': len(given & cells), 'unknown': len(unknown),
            'unknown_sample': unknown[:5], 'without_value': without}


def version_part(specs: Sequence[Mapping[str, Any]]) -> str:
    """``minor`` when every column is new (additive), ``major`` when one replaces existing values (design 25)."""
    return 'major' if any(spec.get('replaces') for spec in specs) else 'minor'


def changelog_lines(specs: Sequence[Mapping[str, Any]], deposit_id: str, cover: Mapping[str, Any]) -> List[str]:
    if version_part(specs) == 'major':
        replaced = ', '.join(spec['name'] for spec in specs if spec.get('replaces'))
        lines = [f'Version class major: data altered: obs values of {replaced} replaced; the h5ad gets them at the '
                 f'next merge (section 26).']
    else:
        lines = ['Version class minor: additive: obs columns added; the h5ad gets them at the next merge '
                 '(section 26).']
    for spec in specs:
        missing = (cover.get('without_value') or {}).get(spec['name'], 0)
        line = (f'obs column {spec["name"]} ({spec["dtype"]}) {"replaced" if spec.get("replaces") else "added"} from '
                f'deposit {deposit_id}' + (f': {spec["description"]}' if spec.get('description') else '')
                + (f' (source: {spec["source"]})' if spec.get('source') else '')
                + (f'; {missing} of {cover.get("cells")} cells without a value' if missing else '') + '.')
        lines.append(line)
    return lines


def spec_json(specs: Sequence[Mapping[str, Any]], replace: bool) -> str:
    """The declaration stored with the deposit (the deposit row's ``artefact`` text column)."""
    return json.dumps({'columns': list(specs), 'replace': bool(replace)}, sort_keys=True)


def spec_from_record(record: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    data = json.loads(record.get('artefact') or '{}')
    return list(data.get('columns') or []), bool(data.get('replace'))


def caveat_message(specs: Sequence[Mapping[str, Any]], deposit_id: str, date: str) -> str:
    names = ', '.join(spec['name'] for spec in specs)
    verb = 'replaced' if version_part(specs) == 'major' else 'added'
    return f'obs columns {names} {verb} from deposit {deposit_id}, {date}'


def dataset_obs_names(duckdb_path: Path, api_config: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """``{name: dtype}`` of the obs columns a dataset has or shows: the columns of ``__obsnum`` and ``__obscat``
    (hidden ones too) and the names the ``.md`` ``api:`` section publishes (renames, derived and constant
    columns). Read-only."""
    from anthive_db import _load_api_config_for, duckdb_obs_columns  # noqa: PLC0415
    out: Dict[str, str] = {}
    tables = duckdb_obs_columns(duckdb_path)
    for name in tables.get('obsnum') or []:
        out[name] = 'numerical'
    for name in tables.get('obscat') or []:
        out[name] = 'categorical'
    config = api_config if api_config is not None else _load_api_config_for(duckdb_path)
    for target in (config.get('obs_rename') or {}).values():
        if isinstance(target, str):
            out.setdefault(target, 'published')
    for block in ('obs_derive', 'obs_constant'):
        for name, spec in (config.get(block) or {}).items():
            dtype = spec.get('dtype') if isinstance(spec, dict) else None
            out.setdefault(str(name), dtype if dtype in DTYPES else 'published')
    return out


def cell_names(duckdb_path: Path) -> List[str]:
    """The cell names of a served dataset (``__cells``), read-only."""
    import duckdb  # noqa: PLC0415
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return [str(row[0]) for row in connection.execute('SELECT cell_name FROM __cells').fetchall()]
    finally:
        connection.close()


def written_by(duckdb_path: Path, deposit_id: str) -> List[str]:
    """Columns ``__pending_cols`` records for this deposit (``run_id``): the write happened before a restart."""
    import duckdb  # noqa: PLC0415
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
        if '__pending_cols' not in tables:
            return []
        return [str(row[0]) for row in connection.execute(
            'SELECT col_name FROM __pending_cols WHERE run_id = ? AND annotator = ?', [deposit_id, ANNOTATOR]).fetchall()]
    finally:
        connection.close()
