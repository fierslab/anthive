"""bin/ingest/ant-restore-obs: obs columns from a tiny h5ad into an obs-less duckdb."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import duckdb
import h5py
import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

from test_annotation_writer import CELL_NAMES, build_synthetic_duckdb  # noqa: E402


def _load():
    path = REPO_ROOT / 'bin' / 'ingest' / 'ant-restore-obs'
    spec = importlib.util.spec_from_loader('ant_restore_obs', importlib.machinery.SourceFileLoader('ant_restore_obs', str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules['ant_restore_obs'] = module
    spec.loader.exec_module(module)
    return module


restore = _load()
STRING = h5py.string_dtype(encoding='utf-8')


def _build_h5ad(path: Path) -> None:
    n_cells = len(CELL_NAMES)
    with h5py.File(path, 'w') as handle:
        obs = handle.create_group('obs')
        obs.attrs['_index'] = '_index'
        obs.attrs['column-order'] = np.array(['Donor ID', 'Age at Death', 'flag', 'note-x', 'count_int', 'skip_me'], dtype=object)
        obs.create_dataset('_index', data=np.array(CELL_NAMES, dtype=object), dtype=STRING)
        donor = obs.create_group('Donor ID')
        donor.attrs['encoding-type'] = 'categorical'
        donor.create_dataset('categories', data=np.array(['D1', 'D2'], dtype=object), dtype=STRING)
        codes = np.array([i % 2 for i in range(n_cells)], dtype='int8')
        codes[3] = -1
        donor.create_dataset('codes', data=codes)
        obs.create_dataset('Age at Death', data=np.arange(n_cells, dtype='f8') + 60.0)
        obs.create_dataset('flag', data=np.array([i % 2 == 0 for i in range(n_cells)]))
        obs.create_dataset('note-x', data=np.array([f'n{i % 3}' for i in range(n_cells)], dtype=object), dtype=STRING)
        integer = obs.create_group('count_int')
        integer.attrs['encoding-type'] = 'nullable-integer'
        integer.create_dataset('values', data=np.arange(n_cells, dtype='int64'))
        mask = np.zeros(n_cells, dtype=bool)
        mask[0] = True
        integer.create_dataset('mask', data=mask)
        obs.create_dataset('skip_me', data=np.zeros(n_cells))


def _setup(tmp_path: Path, obsnum, obscat):
    h5ad = tmp_path / 'toy.h5ad'
    _build_h5ad(h5ad)
    build_synthetic_duckdb(tmp_path / 'toy.duckdb', with_obs_tables=False)
    front = {'title': 'toy', 'id': 'TOY', 'obsnum_include': obsnum, 'obscat_include': obscat}
    (tmp_path / 'toy.md').write_text('---\n' + yaml.safe_dump(front) + '---\n')
    return h5ad


def _args(h5ad: Path, command: str = 'apply') -> argparse.Namespace:
    return argparse.Namespace(command=command, file=str(h5ad), db_id=None, api_url=None, no_coordinate=True)


def test_restore_writes_columns_without_pending(tmp_path):
    h5ad = _setup(tmp_path, ['Age at Death', 'count_int', 'absent'], ['Donor ID', 'flag', 'note-x'])
    assert restore.run(_args(h5ad)) == 0
    connection = duckdb.connect(str(tmp_path / 'toy.duckdb'), read_only=True)
    assert connection.execute('SELECT count("Age at Death"), min("Age at Death"), max("Age at Death") FROM __obsnum').fetchone() == (20, 60.0, 79.0)
    assert connection.execute('SELECT count(count_int) FROM __obsnum').fetchone()[0] == 19
    assert connection.execute('SELECT count("Donor ID"), count(DISTINCT "Donor ID") FROM __obscat').fetchone() == (19, 2)
    assert {row[0] for row in connection.execute('SELECT DISTINCT flag FROM __obscat').fetchall()} == {'True', 'False'}
    assert connection.execute('SELECT count(*) FROM __pending_cols').fetchone()[0] == 0
    view_columns = {row[0] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'obscat'").fetchall()}
    assert {'Donor ID', 'flag', 'note-x'} <= view_columns
    connection.close()


def test_second_run_is_idempotent_and_plan_writes_nothing(tmp_path):
    h5ad = _setup(tmp_path, ['Age at Death'], ['Donor ID'])
    assert restore.run(_args(h5ad, 'plan')) == 0
    connection = duckdb.connect(str(tmp_path / 'toy.duckdb'), read_only=True)
    tables = {row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    connection.close()
    assert '__obsnum' not in tables
    assert restore.run(_args(h5ad)) == 0
    assert restore.run(_args(h5ad)) == 0   # nothing left to write


def test_plan_columns_reasons():
    to_write, skipped = restore.plan_columns(['a', 'b'], ['c', 'cell_name', 'A2', 'a2'], ['a', 'c', 'cell_name', 'A2', 'a2'], {'b'})
    assert to_write == [('a', 'num'), ('c', 'cat'), ('A2', 'cat')]
    assert dict(skipped) == {'b': 'not in h5ad obs', 'cell_name': 'reserved name', 'a2': 'case-insensitive duplicate'}


def test_cell_count_mismatch_refused(tmp_path):
    h5ad = _setup(tmp_path, ['Age at Death'], [])
    connection = duckdb.connect(str(tmp_path / 'toy.duckdb'))
    connection.execute("DELETE FROM __cells WHERE cell_id = 0")
    connection.close()
    assert restore.run(_args(h5ad)) == 1
