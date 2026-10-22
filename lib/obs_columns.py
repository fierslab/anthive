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


CHUNK_ROWS = 50_000
MISSING = ('', 'NA', 'NaN', 'nan')


class TableColumns:
    """The columns of a streamed table, laid out on the dataset's cells (``cell_names`` order).

    Numerical columns are float64 arrays (NaN where the table gives no value), categorical columns integer codes
    into ``categories`` (-1 where missing). Memory is about ``cells x columns x 8`` bytes plus the cell name
    index, whatever the size of the file."""

    def __init__(self, cell_names: Sequence[str], specs: Sequence[Mapping[str, Any]]):
        import numpy as np  # noqa: PLC0415
        self.cell_names = list(cell_names)
        self.specs = list(specs)
        self.numbers: Dict[str, Any] = {}
        self.codes: Dict[str, Any] = {}
        self.categories: Dict[str, Dict[str, int]] = {}
        for spec in self.specs:
            if spec['dtype'] == 'numerical':
                self.numbers[spec['name']] = np.full(len(self.cell_names), np.nan, dtype='f8')
            else:
                self.codes[spec['name']] = np.full(len(self.cell_names), -1, dtype='i4')
                self.categories[spec['name']] = {}

    def series(self, name: str) -> Any:
        """One column as a pandas Series indexed by cell name (for ``AnnotationWriter``)."""
        import numpy as np  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        index = pd.Index(self.cell_names, dtype=object)
        if name in self.numbers:
            return pd.Series(self.numbers[name], index=index, copy=False)
        labels = np.array(list(self.categories[name]) + [None], dtype=object)
        return pd.Series(labels[self.codes[name]], index=index, dtype=object)   # code -1: the trailing None

    def frame(self, cell_ids: Any) -> Any:
        """Every column with a ``cell_id`` column (``cell_ids`` in ``cell_names`` order) for
        ``AnnotationWriter.add_frame``: float64 arrays and pandas Categoricals, no per-value Python objects."""
        import pandas as pd  # noqa: PLC0415
        data: Dict[str, Any] = {'cell_id': cell_ids}
        for spec in self.specs:
            name = spec['name']
            if name in self.numbers:
                data[name] = self.numbers[name]
            else:
                data[name] = pd.Categorical.from_codes(self.codes[name], categories=list(self.categories[name]))
        return pd.DataFrame(data, copy=False)

    def distinct(self, name: str) -> List[str]:
        return list(self.categories.get(name, {}))

    def given(self, name: str) -> int:
        if name in self.numbers:
            import numpy as np  # noqa: PLC0415
            return int((~np.isnan(self.numbers[name])).sum())
        return int((self.codes[name] >= 0).sum())


def stream_table(path: Path, specs: Sequence[Mapping[str, Any]], cell_names: Sequence[str],
                 max_rows: int = MAX_ROWS, chunk_rows: int = CHUNK_ROWS) -> Tuple[Optional[TableColumns],
                                                                                  List[str], Dict[str, Any]]:
    """Read the table in chunks of ``chunk_rows`` against the dataset's cells: ``(columns, problems, coverage)``.

    Per chunk the cell names are looked up (any unknown name fails the table; the first few are named),
    duplicates found with a bitmap, numerical values parsed, categorical values coded (at most
    ``MAX_CATEGORIES`` distinct). ``coverage`` is ``{cells, given, unknown, unknown_sample, without_value}``.
    Problems stop the reading early where the answer is already known (too many rows, too many categories)."""
    import numpy as np  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    opener = gzip.open if str(path).lower().endswith('.gz') else open
    with opener(path, 'rb') as handle:
        header = handle.readline().decode('utf-8', errors='replace').rstrip('\r\n')
    cover: Dict[str, Any] = {'cells': len(cell_names), 'given': 0, 'unknown': 0, 'unknown_sample': [],
                             'without_value': {}}
    problems = header_problems(header, specs)
    if problems:
        return None, problems, cover
    position = {name: index for index, name in enumerate(cell_names)}
    columns = TableColumns(cell_names, specs)
    seen = np.zeros(len(cell_names), dtype=bool)
    rows = 0
    duplicates: List[str] = []
    bad_numbers: Dict[str, Tuple[int, str]] = {}
    unknown_sample: List[str] = []
    unknown = 0
    empty_names = 0
    try:
        reader = pd.read_csv(path, sep='\t', dtype=str, keep_default_na=False, na_values=list(MISSING),
                             compression='infer', chunksize=chunk_rows, engine='c')
        for chunk in reader:
            rows += len(chunk)
            if rows > max_rows:
                return None, [f'more than {max_rows} rows'], cover
            names = chunk['cell_name']
            empty_names += int(names.isna().sum())
            looked_up = names.map(position)
            missing = looked_up.isna() & names.notna()
            if missing.any():
                unknown += int(missing.sum())
                unknown_sample.extend(str(name) for name in names[missing].head(5 - len(unknown_sample)))
            valid = looked_up.notna().to_numpy()
            index = looked_up[valid].to_numpy(dtype='i8')
            repeated = seen[index]
            if repeated.any() or len(np.unique(index)) != len(index):
                within = pd.Series(index).duplicated().to_numpy()
                clash = repeated | within
                duplicates.extend(str(name) for name in names[valid][clash].head(5 - len(duplicates)))
            seen[index] = True
            for spec in specs:
                values = chunk[spec['name']][valid]
                if spec['dtype'] == 'numerical':
                    parsed = pd.to_numeric(values, errors='coerce')
                    bad = values.notna() & parsed.isna()
                    if bad.any():
                        count, example = bad_numbers.get(spec['name'], (0, str(values[bad].iloc[0])[:40]))
                        bad_numbers[spec['name']] = (count + int(bad.sum()), example)
                    columns.numbers[spec['name']][index] = parsed.to_numpy(dtype='f8')
                else:
                    mapping = columns.categories[spec['name']]
                    present = values.notna().to_numpy()
                    for label in pd.unique(values[present]):
                        if label not in mapping:
                            mapping[label] = len(mapping)
                    if len(mapping) > MAX_CATEGORIES:
                        return None, [f'{spec["name"]}: more than {MAX_CATEGORIES} distinct values (a column of '
                                      f'cell ids is not a category)'], cover
                    codes = values.map(mapping).fillna(-1).to_numpy(dtype='i4')
                    columns.codes[spec['name']][index] = codes
    except Exception as exc:  # noqa: BLE001
        return None, [f'the table cannot be read as TSV ({type(exc).__name__}: {str(exc)[:200]})'], cover
    cover.update(given=int(seen.sum()), unknown=unknown, unknown_sample=unknown_sample[:5])
    if rows == 0:
        problems.append('the table has no rows')
    if empty_names:
        problems.append(f'{empty_names} rows have no cell_name')
    if duplicates:
        problems.append(f'duplicate cell names: {", ".join(duplicates[:5])}')
    for name, (count, example) in bad_numbers.items():
        problems.append(f'{name}: {count} values are not numbers (for example {example!r})')
    cover['without_value'] = {spec['name']: len(cell_names) - columns.given(spec['name']) for spec in specs}
    return columns, problems, cover


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


def cells_table(duckdb_path: Path) -> Tuple[Any, List[str]]:
    """``(cell ids, cell names)`` of a served dataset (``__cells``, ordered by cell id), read-only."""
    import duckdb  # noqa: PLC0415
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        frame = connection.execute('SELECT cell_id, cell_name FROM __cells ORDER BY cell_id').fetchdf()
    finally:
        connection.close()
    return frame['cell_id'].to_numpy(), frame['cell_name'].astype(str).tolist()


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
