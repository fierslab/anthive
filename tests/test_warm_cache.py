"""The persistent warm-up cache (lib/warm_cache.py): stable keys, key checks on load, atomic writes.

The facet-specific keys (dataset version, code version, result files) are tested in
tests/test_facet_results.py (``disk cache`` section); the module rank keys in tests/test_module_rank.py.
"""
from __future__ import annotations

import multiprocessing
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

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
    path = next((tmp_path / 'cache').glob('tables*.pkl'))
    path.write_bytes(pickle.dumps({'format': warm_cache.CACHE_FORMAT, 'name': 'tables', 'key': 'k1' * 20 + 'x',
                                   'value': 'wrong'}))
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING
    # an older cache format is a miss
    path.write_bytes(pickle.dumps({'format': warm_cache.CACHE_FORMAT - 1, 'name': 'tables', 'key': 'k1' * 20,
                                   'value': 'old'}))
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING
    # a truncated file is a miss, not an error
    cache.save('tables', 'k1' * 20, list(range(10000)))
    path.write_bytes(path.read_bytes()[:100])
    assert cache.load('tables', 'k1' * 20) is warm_cache.MISSING


def test_save_keeps_the_newest_entries_of_a_name(tmp_path):
    cache = warm_cache.DiskCache(tmp_path)
    for index in range(5):
        cache.save('families-sex', f'{index:040d}', index, keep=2)
        time.sleep(0.01)
    cache.save('families-age', 'a' * 40, 'age', keep=2)
    assert cache.load('families-sex', f'{4:040d}') == 4 and cache.load('families-sex', f'{3:040d}') == 3
    assert cache.load('families-sex', f'{2:040d}') is warm_cache.MISSING
    assert cache.load('families-age', 'a' * 40) == 'age'                  # other names are untouched
    assert len(list(tmp_path.glob('*.pkl'))) == 3


class _Unpicklable:
    def __reduce__(self):
        raise RuntimeError('cannot pickle')


def test_a_failed_write_keeps_the_previous_value_and_leaves_no_temporary_file(tmp_path):
    cache = warm_cache.DiskCache(tmp_path)
    assert cache.save('tables', 'k' * 40, 'good')
    assert not cache.save('tables', 'k' * 40, ['half', _Unpicklable()])
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
        cache.save('shared', 's' * 40, {'worker': worker, 'index': index, 'payload': list(range(20000))})


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
            assert value['payload'][-1] == 19999 and len(value['payload']) == 20000
            seen += 1
    for process in workers:
        process.join()
        assert process.exitcode == 0
    final = reader.load('shared', 's' * 40)
    assert final is not warm_cache.MISSING and final['index'] == 14
    assert list(tmp_path.glob('.tmp-*')) == []
    assert len(list(tmp_path.glob('shared*.pkl'))) == 1
    assert seen > 0 or final is not warm_cache.MISSING


@pytest.mark.parametrize('name', ['summary-G1/ds1', 'gene_families-sex-human_postmortem-True', 'a b/c:d'])
def test_entry_names_are_file_system_safe_and_distinct(tmp_path, name):
    cache = warm_cache.DiskCache(tmp_path)
    cache.save(name, 'k' * 40, name)
    cache.save(name + '?', 'k' * 40, 'other')                             # same file-system form, other name
    assert cache.load(name, 'k' * 40) == name
    assert all(path.parent == tmp_path for path in tmp_path.glob('*.pkl'))
