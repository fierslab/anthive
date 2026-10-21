"""The persistent warm-up cache (lib/warm_cache.py): stable keys, key checks on load, atomic writes.

The facet-specific keys (dataset version, code version, result files) are tested in
tests/test_facet_results.py (``disk cache`` section); the module rank keys in tests/test_module_rank.py.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import warm_cache

LIB_DIR = Path(__file__).resolve().parent.parent / 'lib'


def test_digest_is_the_same_in_every_process():
    """Keys must not depend on ``hash()`` (randomised per process for strings) or set order."""
    value = {'datasets': [{'db_id': 'G1/ds1', 'title': 'one'}], 'digests': frozenset({'a', 'b', 'c', 'd', 'e'}),
             'nan': float('nan'), 'tuple': ('x', 1, 2)}
    here = warm_cache.digest('kind', value)
    code = ('import sys; sys.path.insert(0, sys.argv[1]); import warm_cache; '
            "print(warm_cache.digest('kind', {'datasets': [{'title': 'one', 'db_id': 'G1/ds1'}], "
            "'digests': frozenset({'e', 'd', 'c', 'b', 'a'}), 'nan': float('nan'), 'tuple': ('x', 1, 2)}))")
    for seed in ('1', '2', '12345'):
        output = subprocess.run([sys.executable, '-c', code, str(LIB_DIR)], capture_output=True, text=True, check=True,
                                env={**os.environ, 'PYTHONHASHSEED': seed}).stdout.strip()
        assert output == here
    assert warm_cache.digest('kind', {**value, 'title': 'two'}) != here


def test_code_version_covers_lib_and_library_versions():
    version = warm_cache.code_version()
    assert len(version) == 64 and version == warm_cache.code_version()


def test_save_load_and_key_checks(tmp_path):
    cache = warm_cache.DiskCache(tmp_path / 'cache')
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING          # nothing yet, folder absent
    assert cache.save('tables', 'k1' * 20, {'rows': [1, 2, 3]})
    assert cache.load('tables', 'k1' * 20) == {'rows': [1, 2, 3]}
    assert cache.load('tables', 'k2' * 20) is warm_cache.MISSING          # another key
    assert cache.load('other', 'k1' * 20) is warm_cache.MISSING           # another name
    # a file whose name matches but whose stored key differs (40-character prefix collision) is a miss
    path = next((tmp_path / 'cache').glob('tables*' + warm_cache.SUFFIX))
    _write_raw(path, {'format': warm_cache.CACHE_FORMAT, 'name': 'tables', 'key': 'k1' * 20 + 'x', 'value': 'wrong',
                      'frames': []})
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING
    # an older cache format is a miss
    _write_raw(path, {'format': warm_cache.CACHE_FORMAT - 1, 'name': 'tables', 'key': 'k1' * 20, 'value': 'old',
                      'frames': []})
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING
    # a truncated file is a miss, not an error
    cache.save('tables', 'k1' * 20, pd.DataFrame({'x': range(10000)}))
    path.write_bytes(path.read_bytes()[:-100])
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING


def _write_raw(path, header):
    data = json.dumps(header).encode()
    path.write_bytes(warm_cache.MAGIC + struct.pack('>Q', len(data)) + data)


def test_a_pickle_in_the_cache_folder_is_never_loaded(tmp_path):
    """The scratch volume is shared by containers: a pickle planted under an entry's name must not run."""
    import pickle

    class Boom:
        def __reduce__(self):
            return (os.system, (f'touch {tmp_path / "pwned"}',))

    cache = warm_cache.DiskCache(tmp_path)
    cache.save('tables', 'k' * 40, {'a': 1})
    path = next(tmp_path.glob('tables*' + warm_cache.SUFFIX))
    path.write_bytes(pickle.dumps(Boom()))
    (tmp_path / path.name.replace(warm_cache.SUFFIX, '.pkl')).write_bytes(pickle.dumps(Boom()))
    assert cache.load('tables', 'k' * 40) is warm_cache.MISSING
    assert not (tmp_path / 'pwned').exists()
    assert 'import pickle' not in (Path(warm_cache.__file__)).read_text()


def test_dataframes_and_nested_values_round_trip(tmp_path):
    """What the facet cache stores: categoricals, float32, an index, object columns with dicts, sets, lists,
    None and NaN, nested in dicts with tuple keys."""
    units = pd.DataFrame({
        'unit': np.array([0, 1, 2], dtype='int32'), 'db_id': ['G1/a', 'G2/b', None],
        'summary_unit': [{'facet': 'sex', 'flags': {'n_lt5': None}, 'tau': float('nan')}, {'facet': 'age'}, None],
        'donor_digests': [frozenset({'d1', 'd2'}), None, frozenset()],
        'cohort': pd.Series(['A', float('nan'), None], dtype=object, index=[5, 7, 9]),
        'counted': pd.Series([True, False, None], dtype=object, index=[5, 7, 9]),
    }, index=pd.Index([5, 7, 9]))
    rows = pd.DataFrame({'gene': pd.Categorical(['B', 'A', 'B'], categories=['A', 'B', 'UNUSED']),
                         'logFC': np.array([0.5, -1.0, np.nan], dtype='float32')})
    frame = pd.DataFrame({'r_value': [0.01, 0.2], 'r_cohorts': [['A', 'B'], []]},
                         index=pd.Index(['APOE', 'TREM2'], name='gene_key'))
    value = {('sex', 'human_postmortem'): (rows, units), 'family': {'frame': frame, 'n': np.int64(3)},
             'plain': [1, 2.5, 'x', None, float('inf')]}
    cache = warm_cache.DiskCache(tmp_path)
    assert cache.save('mixed', 'k' * 40, value)
    loaded = cache.load('mixed', 'k' * 40)
    loaded_rows, loaded_units = loaded[('sex', 'human_postmortem')]
    pd.testing.assert_frame_equal(loaded_rows, rows)
    pd.testing.assert_frame_equal(loaded['family']['frame'], frame)
    assert list(loaded_units.index) == [5, 7, 9]
    assert loaded_units['summary_unit'][5]['flags'] == {'n_lt5': None} and np.isnan(loaded_units['summary_unit'][5]['tau'])
    assert loaded_units['donor_digests'][5] == frozenset({'d1', 'd2'}) and loaded_units['donor_digests'][7] is None
    assert np.isnan(loaded_units['cohort'][7]) and loaded_units['cohort'][9] is None
    assert loaded['family']['n'] == 3 and loaded['plain'] == [1, 2.5, 'x', None, float('inf')]


def test_unsupported_values_are_not_cached(tmp_path):
    cache = warm_cache.DiskCache(tmp_path)
    assert cache.save('odd', 'k' * 40, {'when': object()}) is False
    assert cache.load('odd', 'k' * 40) is warm_cache.MISSING
    assert list(tmp_path.glob('*')) == [] or not list(tmp_path.glob('odd*'))


def test_save_keeps_the_newest_entries_of_a_name(tmp_path):
    cache = warm_cache.DiskCache(tmp_path)
    for index in range(5):
        cache.save('families-sex', f'{index:040d}', index, keep=2)
        time.sleep(0.01)
    cache.save('families-age', 'a' * 40, 'age', keep=2)
    assert cache.load('families-sex', f'{4:040d}') == 4 and cache.load('families-sex', f'{3:040d}') == 3
    assert cache.load('families-sex', f'{2:040d}') is warm_cache.MISSING
    assert cache.load('families-age', 'a' * 40) == 'age'                  # other names are untouched
    assert len(list(tmp_path.glob('*' + warm_cache.SUFFIX))) == 3


def test_a_failed_write_keeps_the_previous_value_and_leaves_no_temporary_file(tmp_path):
    cache = warm_cache.DiskCache(tmp_path)
    assert cache.save('tables', 'k' * 40, 'good')
    assert not cache.save('tables', 'k' * 40, ['half', object()])
    assert cache.load('tables', 'k' * 40) == 'good'
    assert list(tmp_path.glob('.tmp-*')) == []


def test_unwritable_folder_is_not_an_error(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    cache = warm_cache.DiskCache(blocker / 'cache')                        # parent is a file: mkdir fails
    assert cache.save('tables', 'k' * 40, 1) is False
    assert cache.load('tables', 'k' * 40) is warm_cache.MISSING


def _writer(directory: str, worker: int, rounds: int) -> None:
    cache = warm_cache.DiskCache(Path(directory))
    for index in range(rounds):
        # every worker writes the same entry: a reader must always see one complete value
        cache.save('shared', 's' * 40, {'worker': worker, 'index': index,
                                        'payload': pd.DataFrame({'x': list(range(20000))})})


def test_concurrent_writers_never_expose_a_partial_file(tmp_path):
    """Several processes (workers) write the same entry while this process reads it: every load is either
    a miss (before the first write) or a complete value, and no temporary file is left behind."""
    context = multiprocessing.get_context('spawn')
    workers = [context.Process(target=_writer, args=(str(tmp_path), worker, 15)) for worker in range(4)]
    for process in workers:
        process.start()
    reader = warm_cache.DiskCache(tmp_path)
    seen = 0
    while any(process.is_alive() for process in workers):
        value = reader.load('shared', 's' * 40)
        if value is not warm_cache.MISSING:
            assert len(value['payload']) == 20000 and value['payload']['x'].iloc[-1] == 19999
            seen += 1
    for process in workers:
        process.join()
        assert process.exitcode == 0
    final = reader.load('shared', 's' * 40)
    assert final is not warm_cache.MISSING and final['index'] == 14
    assert list(tmp_path.glob('.tmp-*')) == []
    assert len(list(tmp_path.glob('shared*' + warm_cache.SUFFIX))) == 1
    assert seen > 0 or final is not warm_cache.MISSING


@pytest.mark.parametrize('name', ['summary-G1/ds1', 'gene_families-sex-human_postmortem-True', 'a b/c:d'])
def test_entry_names_are_file_system_safe_and_distinct(tmp_path, name):
    cache = warm_cache.DiskCache(tmp_path)
    cache.save(name, 'k' * 40, name)
    cache.save(name + '?', 'k' * 40, 'other')                             # same file-system form, other name
    assert cache.load(name, 'k' * 40) == name
    assert all(path.parent == tmp_path for path in tmp_path.glob('*' + warm_cache.SUFFIX))
