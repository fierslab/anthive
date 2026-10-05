"""bin/ingest/ant-ingest-index and the overlap index files: incremental build, digests only, permissions."""
import importlib.machinery
import importlib.util
import json
import os
import stat
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ingest_check.overlap_index import (DONOR_COLUMNS, OverlapIndex, cell_donor_digests, donor_frame, safe_key)
from tests.ingest_helpers import donor_obs, make_duckdb, write_index_md, write_md

SCRIPT = Path(__file__).parent.parent / 'bin' / 'ingest' / 'ant-ingest-index'
API = {'obs_rename': {'donor': 'ant_donor', 'age': 'ant_age', 'braak': 'ant_braak', 'pmi': 'ant_pmi',
                      'sex_raw': 'ant_sex'},
       'obs_value_rename': {'sex_raw': {'F': 'f', 'M': 'm'}},
       'obs_derive': {'ant_donor_uid': {'source': 'donor', 'template': 'shared:{value}'}}}


@pytest.fixture(scope='module')
def tool():
    loader = importlib.machinery.SourceFileLoader('ant_ingest_index', str(SCRIPT))
    spec = importlib.util.spec_from_loader('ant_ingest_index', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def add_dataset(store, folder, stem, group_id, dataset_id, n_cells=40, n_donors=8, organism='human', **front):
    directory = store / folder
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / 'index.md').exists():
        write_index_md(directory, group_id=group_id)
    obs = donor_obs(n_cells, n_donors)
    write_md(directory / f'{stem}.md', id=dataset_id, title=stem, api=API, **front)
    (directory / f'{stem}.h5ad').write_bytes(b'')
    (directory / f'{stem}.h5ad.mus').write_text(f'organism={organism}\n')
    make_duckdb(directory / f'{stem}.duckdb', obs, genes=['GFAP', 'AIF1', 'TREM2'])
    return directory


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'store'
    add_dataset(root, 'GroupA', 'one', 'AAA', 'ONE')
    add_dataset(root, 'GroupA', 'two', 'AAA', 'TWO')
    add_dataset(root, 'GroupB', 'mouse', 'BBB', 'MOU', organism='mouse')
    return root


@pytest.fixture
def index(tmp_path):
    return OverlapIndex(tmp_path / 'state' / 'index')


def build(tool, store, index, tmp_path, only=(), force=False):
    return tool.build(store, index, list(only), force, tmp_path / 'scratch')


def test_active_entries_skip_inactive_underscore_and_database_less_datasets(tool, store):
    add_dataset(store, 'GroupA', 'dormant', 'AAA', 'DOR')
    (store / 'GroupA' / 'dormant.inactive').write_text('')
    add_dataset(store / 'GroupA' / '_backup_2026', '.', 'old', 'AAA', 'OLD')
    (store / 'GroupA' / 'nodb.md').write_text('---\nid: NDB\n---\n')
    (store / 'GroupA' / 'nodb.h5ad').write_bytes(b'')
    assert sorted(entry.db_id for entry in tool.active_entries(store)) == ['AAA/ONE', 'AAA/TWO', 'BBB/MOU']


def test_build_indexes_donors_cells_and_genes(tool, store, index, tmp_path):
    assert build(tool, store, index, tmp_path) == 0
    state = index.state()
    assert set(state) == {'AAA/ONE', 'AAA/TWO', 'BBB/MOU'}
    assert state['AAA/ONE']['organism'] == 'human' and state['BBB/MOU']['organism'] == 'mouse'
    assert state['AAA/ONE']['n_cells'] == 40 and state['AAA/ONE']['n_donors'] == 8
    donors = index.donors()
    assert list(donors.columns) == DONOR_COLUMNS and len(donors) == 24
    assert set(donors['key_kind']) == {'uid'}
    first = donors[donors['db_key'] == 'AAA/ONE'].sort_values('age').iloc[0]
    assert first['sex'] == 'f' and first['age'] == 60.0 and first['braak'] == 0.0 and first['pmi'] == 4.0
    strict, loose = index.cells('AAA/ONE')
    assert strict.dtype == np.uint64 and len(strict) == len(loose) == 40 and list(strict) == sorted(strict)
    assert index.cells('NO/SUCH') is None
    universe, n_datasets = index.gene_universe('human')
    assert universe == {'GFAP', 'AIF1', 'TREM2'} and n_datasets == 2
    assert index.gene_universe('mouse')[1] == 1 and index.gene_universe('zebrafish') == (set(), 0)


def test_the_index_holds_digests_not_donor_identifiers_or_barcodes(tool, store, index, tmp_path):
    build(tool, store, index, tmp_path)
    labels = {'D00', 'D01', 'D07', 'shared:D00'}
    donors = index.donors()
    assert not labels & set(donors['key_digest'])
    assert all(len(digest) == 16 and set(digest) <= set('0123456789abcdef') for digest in donors['key_digest'])
    barcode_head = 'ACGTACGT'
    for path in index.dir.rglob('*'):
        if path.is_file() and path.suffix in ('.parquet', '.npy', '.json'):
            blob = path.read_bytes()
            assert b'D00' not in blob and barcode_head.encode() not in blob, path


def test_files_are_private(tool, store, index, tmp_path):
    build(tool, store, index, tmp_path)
    assert stat.S_IMODE(index.dir.stat().st_mode) == 0o700
    for path in index.dir.rglob('*.parquet'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in index.dir.rglob('*.npy'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_build_is_incremental(tool, store, index, tmp_path, capsys):
    build(tool, store, index, tmp_path)
    capsys.readouterr()
    assert build(tool, store, index, tmp_path) == 0
    assert 'indexed' not in capsys.readouterr().out
    database = store / 'GroupA' / 'two.duckdb'
    os.utime(database, (database.stat().st_atime, database.stat().st_mtime + 5))
    build(tool, store, index, tmp_path)
    assert capsys.readouterr().out.count('indexed') == 1
    build(tool, store, index, tmp_path, force=True)
    assert capsys.readouterr().out.count('indexed') == 3


def test_changed_md_triggers_a_rebuild_of_that_dataset(tool, store, index, tmp_path, capsys):
    build(tool, store, index, tmp_path)
    capsys.readouterr()
    md = store / 'GroupB' / 'mouse.md'
    md.write_text(md.read_text() + '\n# touched\n')
    build(tool, store, index, tmp_path)
    assert 'indexed BBB/MOU' in capsys.readouterr().out


def test_vanished_datasets_are_dropped(tool, store, index, tmp_path):
    build(tool, store, index, tmp_path)
    (store / 'GroupA' / 'two.inactive').write_text('')
    build(tool, store, index, tmp_path)
    assert set(index.state()) == {'AAA/ONE', 'BBB/MOU'}
    assert 'AAA/TWO' not in set(index.donors()['db_key']) and index.cells('AAA/TWO') is None


def test_only_limits_the_build_and_keeps_the_rest(tool, store, index, tmp_path):
    build(tool, store, index, tmp_path, only=['BBB/MOU'])
    assert set(index.state()) == {'BBB/MOU'}
    build(tool, store, index, tmp_path, only=['AAA/ONE'])
    assert set(index.state()) == {'BBB/MOU', 'AAA/ONE'}


def test_a_broken_database_is_counted_and_the_rest_is_indexed(tool, store, index, tmp_path, capsys):
    (store / 'GroupA' / 'two.duckdb').write_bytes(b'garbage')
    assert build(tool, store, index, tmp_path) == 1
    assert set(index.state()) == {'AAA/ONE', 'BBB/MOU'}
    assert 'FAILED AAA/TWO' in capsys.readouterr().err


def test_datasets_without_donors_get_empty_donor_tables(tool, tmp_path, index):
    root = tmp_path / 'bare'
    directory = root / 'G'
    directory.mkdir(parents=True)
    write_index_md(directory, group_id='GGG')
    write_md(directory / 'x.md', id='XXX', title='x')
    (directory / 'x.h5ad').write_bytes(b'')
    make_duckdb(directory / 'x.duckdb', donor_obs(20, 4))
    assert build(tool, root, index, tmp_path) == 0
    assert index.state()['GGG/XXX']['n_donors'] == 0 and len(index.donors()) == 0


def test_main_refuses_an_index_inside_the_store(tool, store, capsys):
    assert tool.main(['build', '--store', str(store), '--index-dir', str(store / '_index')]) == 2
    assert 'inside the store' in capsys.readouterr().err


def test_main_build_and_status(tool, store, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ANTHIVE_TMP_DIR', str(tmp_path / 'tmp'))
    assert tool.main(['status', '--store', str(store)]) == 0
    assert '0 datasets indexed, 3 active in the store, 3 missing or stale' in capsys.readouterr().out
    assert tool.main(['build', '--store', str(store)]) == 0
    assert 'Done: 3 datasets in the index, 0 failure(s)' in capsys.readouterr().out
    assert (tmp_path / 'state' / 'ingest' / 'index' / 'donors.parquet').exists()
    assert tool.main(['status', '--store', str(store)]) == 0
    assert '3 datasets indexed, 3 active in the store, 0 missing or stale' in capsys.readouterr().out
    assert tool.main(['build', '--store', str(store), '--only', 'AAA/ONE', '--force']) == 0
    assert tool.main(['build', '--store', str(store), '--index-dir', str(tmp_path / 'alt')]) == 0
    assert (tmp_path / 'alt' / 'state.json').exists()


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_donor_frame_normalises_demographics_and_nulls_conflicts():
    cells = pd.DataFrame({'ant_donor': ['a', 'a', 'b', 'b', 'c'], 'ant_donor_uid': ['u:a', 'u:a', None, None, 'u:c'],
                          'ant_sex': ['Female', 'female', 'm', 'f', 'Male'], 'ant_age': ['90+', '90+', 70.5, 70.5, 'x'],
                          'ant_apoe': ['E3/E4', 'E3/E4', '33', '33', 'E4'], 'ant_braak': [3, 3, 2, 2, np.nan],
                          'ant_pmi': [5.0, 5.0, 6.0, 6.0, np.nan]})
    frame = donor_frame(cells, 'GRP', 'GRP/DS').set_index('n_cells')
    assert len(frame) == 3
    a = frame[frame['key_kind'] == 'uid'].iloc[0:2]
    donor_a = a[a['age_censored']].iloc[0]
    assert (donor_a['sex'], donor_a['age'], donor_a['apoe'], donor_a['braak']) == ('f', 90.0, '3/4', 3.0)
    donor_b = frame[frame['key_kind'] == 'fallback'].iloc[0]
    assert pd.isna(donor_b['sex']) and donor_b['age'] == 70.5 and donor_b['apoe'] == '3/3'     # sex conflicts -> NULL
    donor_c = frame.loc[1]
    assert pd.isna(donor_c['apoe']) and pd.isna(donor_c['age']) and pd.isna(donor_c['braak'])


def test_donor_frame_without_a_donor_column_is_empty():
    assert len(donor_frame(pd.DataFrame({'ant_sex': ['f']}), 'G', 'G/X')) == 0
    assert len(donor_frame(pd.DataFrame({'ant_donor': [None]}), 'G', 'G/X')) == 0


def test_cell_donor_digests():
    cells = pd.DataFrame({'ant_donor': ['a', None, 'a'], 'ant_donor_uid': [None, None, None]})
    digests = cell_donor_digests(cells, 'G')
    assert digests[0] == digests[2] and len(digests[0]) == 16 and digests[1] == ''
    assert cell_donor_digests(pd.DataFrame({'x': [1, 2]}), 'G') == ['', '']


def test_safe_key():
    assert safe_key('AB1/CD2') == 'AB1__CD2'
    assert safe_key('../x') == '__.__x' or '/' not in safe_key('../x')


def test_unreadable_state_file_means_an_empty_index(tmp_path):
    index = OverlapIndex(tmp_path)
    (tmp_path / 'state.json').write_text('{broken')
    assert index.state() == {} and index.is_empty()
    assert len(index.donors()) == 0


def test_state_round_trip(tmp_path):
    index = OverlapIndex(tmp_path / 'i')
    index._write_state({'A/B': {'n': 1}})
    assert json.loads((tmp_path / 'i' / 'state.json').read_text()) == {'A/B': {'n': 1}}
    index.drop_dataset('A/B')
    assert index.state() == {}
