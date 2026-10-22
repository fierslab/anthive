"""Obs-columns deposits (deposit_kind obs_columns, design section 26 of doc/DESIGN_dataset_ingestion.md).

API: rights (owner, admin, update_policy any; replace only owner or admin), names (identifier rule, reserved,
ant_* only with facet, case-insensitive clash), the one-table rule and the header check at complete. Watcher:
unknown cells fail, partial coverage is reported, the columns land in the duckdb with __pending_cols, the .md
gets a minor (or major) version, a changelog line per column and a caveat, the duckdb is pushed to the catalog,
the .md committed, the h5ad untouched; public datasets are held.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path

import duckdb
import pytest
import yaml

import artefacts
import dataset_deposit as dd
import obs_columns
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, CATALOG, OTHER, PREFIX, context, make_publisher,
                                            make_service, poll_until_final, upload, watch_row)

CELLS = [f'c{index}' for index in range(10)]


@pytest.fixture
def clock():
    return [time.time()]


def make_duckdb(store_root: Path, stem: str = 'int') -> Path:
    """A served duckdb with ten cells, one numerical (n_counts) and one categorical (cell_type) obs column."""
    path = store_root / 'Public2021' / f'{stem}.duckdb'
    path.unlink(missing_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute('CREATE TABLE __cells (cell_id INTEGER PRIMARY KEY, cell_name VARCHAR)')
        connection.executemany('INSERT INTO __cells VALUES (?, ?)', list(enumerate(CELLS)))
        connection.execute('CREATE TABLE __genes (gene_id INTEGER, gene_name VARCHAR)')
        connection.execute("INSERT INTO __genes VALUES (0, 'Apoe')")
        connection.execute('CREATE TABLE __obsnum (cell_id INTEGER PRIMARY KEY, n_counts DOUBLE)')
        connection.executemany('INSERT INTO __obsnum VALUES (?, ?)', [(index, 100.0 + index) for index in range(10)])
        connection.execute('CREATE TABLE __obscat (cell_id INTEGER PRIMARY KEY, cell_type VARCHAR)')
        connection.executemany('INSERT INTO __obscat VALUES (?, ?)', [(index, 'MG') for index in range(10)])
        for view in ('obsnum', 'obscat'):
            connection.execute(f'CREATE VIEW {view} AS SELECT c.cell_name, o.* FROM __{view} o '
                               f'JOIN __cells c USING (cell_id)')
    finally:
        connection.close()
    return path


def obs_context(store_root: Path, viewer=None):
    ctx = context(store_root, viewer)
    ctx.dataset_obs = lambda db_id: list(obs_columns.dataset_obs_names(
        store_root / 'Public2021' / f'{db_id.split("/")[1]}.duckdb'))
    return ctx


def table_bytes(rows, header=('cell_name', 'state', 'score'), gz=False) -> bytes:
    text = '\t'.join(header) + '\n' + ''.join('\t'.join(str(value) for value in row) + '\n' for row in rows)
    raw = text.encode()
    return gzip.compress(raw, mtime=0) if gz else raw


FULL = [(cell, 'A' if index % 2 else 'B', index / 10) for index, cell in enumerate(CELLS)]
COLUMNS = [{'name': 'state', 'dtype': 'categorical', 'description': 'activation state', 'source': 'lab call'},
           {'name': 'score', 'dtype': 'numerical'}]


def body(payload: bytes, name: str = 'calls.tsv', columns=None, db_id: str = 'PU1/int', **extra):
    return {'deposit_kind': 'obs_columns', 'data_project': 'Public2021', 'update_of': db_id,
            'columns': COLUMNS if columns is None else columns,
            'files': [{'name': name, 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}], **extra}


def setup(tmp_path, clock, **overrides):
    service, s3 = make_service(tmp_path, clock, datasets_enabled=True, **overrides)
    store_root = tmp_path / 'store'
    make_duckdb(store_root, 'int')
    make_duckdb(store_root, 'pub')
    return service, s3, store_root


def deposit(service, s3, identity, payload, name='calls.tsv', **kwargs):
    ctx = obs_context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=identity.is_admin,
                                                           user=identity.user))
    answer = service.create(identity, body(payload, name, **kwargs), ctx)
    upload(s3, answer, {name: payload})
    done = service.complete(identity, answer['deposit_id'])
    assert done['state'] == 'uploaded', done
    return answer


def create_error(service, identity, payload, **kwargs) -> dd.DepositError:
    ctx = obs_context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=identity.is_admin,
                                                           user=identity.user))
    with pytest.raises(dd.DepositError) as caught:
        service.create(identity, body(payload, **kwargs), ctx)
    return caught.value


def read_obs(path: Path):
    connection = duckdb.connect(str(path), read_only=True)
    try:
        rows = connection.execute('SELECT c.cell_name, n.score, k.state FROM __cells c JOIN __obsnum n USING '
                                  '(cell_id) JOIN __obscat k USING (cell_id) ORDER BY c.cell_id').fetchall()
        pending = connection.execute('SELECT col_name, kind, annotator, run_id FROM __pending_cols '
                                     'ORDER BY col_name').fetchall()
        return rows, pending
    finally:
        connection.close()


def md_meta(store_root: Path, stem: str = 'int'):
    return yaml.safe_load((store_root / 'Public2021' / f'{stem}.md').read_text().split('---\n')[1])


# ── API ──────────────────────────────────────────────────────────────────────

def test_create_answers_upload_instructions_and_the_vocab_lists_the_kind(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    payload = table_bytes(FULL)
    ctx = obs_context(store_root)
    answer = service.create(ANALYST, body(payload), ctx)
    assert answer['deposit_kind'] == 'obs_columns' and answer['update_of'] == 'PU1/int'
    assert answer['columns'] == [{'name': 'state', 'dtype': 'categorical', 'replaces': False},
                                 {'name': 'score', 'dtype': 'numerical', 'replaces': False}]
    assert answer['version_class'] == 'minor'
    assert answer['files'][0]['method'] == 'put'
    record = service.store.get(answer['deposit_id'])
    assert record['watch_key'] == f'artefact:{answer["deposit_id"]}'
    assert service.store.files(answer['deposit_id'])[0]['key'] == \
        f'{PREFIX}Public2021/artefacts/{answer["deposit_id"]}/calls.tsv'
    vocab = service.vocab(ANALYST)['obs_columns']
    assert vocab['limits']['max_file_gb'] == 2.0 and vocab['limits']['max_columns'] == 50
    assert vocab['deposit_kind'] == 'obs_columns' and 'cell_name' in vocab['reserved']
    status = service.status(ANALYST, answer['deposit_id'])
    assert status['obs_columns']['db_id'] == 'PU1/int' and status['obs_columns']['columns'][0]['name'] == 'state'


def test_non_owner_is_refused(tmp_path, clock):
    service, _s3, _root = setup(tmp_path, clock)
    error = create_error(service, OTHER, table_bytes(FULL))
    assert error.status == 403 and error.body['status'] == 'not_owner'


def test_update_policy_any_lets_another_uploader_add_but_not_replace(tmp_path, clock):
    service, _s3, _root = setup(tmp_path, clock, update_policy='any')
    ctx = obs_context(service.store_root, artefacts.Viewer(sees_internal=True, user='otto'))
    assert service.create(OTHER, body(table_bytes(FULL)), ctx)['state'] == 'uploading'
    payload = table_bytes([(cell, 1.0) for cell in CELLS], header=('cell_name', 'n_counts'))
    error = create_error(service, OTHER, payload, columns=[{'name': 'n_counts', 'dtype': 'numerical'}], replace=True,
                         db_id='PU1/pub')
    assert error.status == 403 and error.body['status'] == 'not_owner'


@pytest.mark.parametrize('name, word', [('cell_name', 'reserved_column_name'), ('Gene_ID', 'reserved_column_name'),
                                        ('ant_braak', 'reserved_column_name'), ('1bad', 'invalid_column_name'),
                                        ('has space', 'invalid_column_name')])
def test_reserved_and_invalid_names(tmp_path, clock, name, word):
    service, _s3, _root = setup(tmp_path, clock)
    error = create_error(service, ANALYST, table_bytes(FULL), columns=[{'name': name, 'dtype': 'numerical'}])
    assert error.status == 422 and error.body['status'] == word


def test_ant_names_need_an_explicit_facet_mapping(tmp_path, clock):
    service, _s3, store_root = setup(tmp_path, clock)
    payload = table_bytes([(cell, 'x') for cell in CELLS], header=('cell_name', 'ant_custom'))
    answer = service.create(ANALYST, body(payload, columns=[{'name': 'ant_custom', 'dtype': 'categorical',
                                                             'facet': True}]), obs_context(store_root))
    assert answer['columns'][0]['name'] == 'ant_custom'


def test_a_case_insensitive_clash_with_an_existing_column_is_refused(tmp_path, clock):
    service, _s3, _root = setup(tmp_path, clock)
    payload = table_bytes([(cell, 1.0) for cell in CELLS], header=('cell_name', 'N_Counts'))
    error = create_error(service, ANALYST, payload, columns=[{'name': 'N_Counts', 'dtype': 'numerical'}])
    assert error.status == 409 and error.body['status'] == 'column_exists'
    twice = create_error(service, ANALYST, table_bytes(FULL),
                         columns=[{'name': 'state', 'dtype': 'numerical'}, {'name': 'STATE', 'dtype': 'numerical'}])
    assert twice.status == 422


def test_one_table_with_a_safe_name_and_a_size_limit(tmp_path, clock):
    service, _s3, _root = setup(tmp_path, clock)
    assert create_error(service, ANALYST, table_bytes(FULL), name='calls.csv').body['status'] == 'invalid_name'
    big = body(table_bytes(FULL))
    big['files'][0]['size'] = 3 * 1024 ** 3
    ctx = obs_context(service.store_root)
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, big, ctx)
    assert caught.value.status == 413
    two = body(table_bytes(FULL))
    two['files'] = two['files'] * 2
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, two, ctx)
    assert caught.value.status == 422


def test_a_second_deposit_for_the_same_dataset_waits(tmp_path, clock):
    service, _s3, store_root = setup(tmp_path, clock)
    service.create(ANALYST, body(table_bytes(FULL)), obs_context(store_root))
    error = create_error(service, ANALYST, table_bytes(FULL))
    assert error.status == 409 and error.body['status'] == 'busy'


def test_complete_checks_the_header(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    payload = table_bytes(FULL, header=('cell_name', 'state', 'other'))
    answer = service.create(ANALYST, body(payload), obs_context(store_root))
    upload(s3, answer, {'calls.tsv': payload})
    with pytest.raises(dd.DepositError) as caught:
        service.complete(ANALYST, answer['deposit_id'])
    assert caught.value.status == 422 and 'not declared: other' in caught.value.body['message']
    assert s3.keys(PREFIX) == []                       # nothing reached incoming


# ── watcher ──────────────────────────────────────────────────────────────────

def test_happy_path_categorical_and_numerical(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, git, api = make_publisher(tmp_path, s3, clock)
    h5ad_before = (store_root / 'Public2021' / 'int.h5ad').read_bytes()
    answer = deposit(service, s3, ANALYST, table_bytes(FULL, gz=True), name='calls.tsv.gz')
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live', row['error']
    assert row['version'] == '1.1.0' and row['db_id'] == 'obs:PU1/int'
    duck = store_root / 'Public2021' / 'int.duckdb'
    rows, pending = read_obs(duck)
    assert rows[1] == ('c1', 0.1, 'A') and rows[0] == ('c0', 0.0, 'B')
    assert pending == [('score', 'num', 'obs-columns-deposit', answer['deposit_id']),
                       ('state', 'cat', 'obs-columns-deposit', answer['deposit_id'])]
    meta = md_meta(store_root)
    assert meta['version'] == '1.1.0'
    entry = meta['changelog'][-1]
    assert entry['changes'][0].startswith('Version class minor: additive')
    assert any('obs column state (categorical) added from deposit' in line and 'activation state' in line
               and 'source: lab call' in line for line in entry['changes'])
    assert any(line.startswith('obs column score (numerical) added') for line in entry['changes'])
    assert 'previous_sha256' not in entry                                   # the h5ad did not change
    caveat = meta['caveats'][-1]
    assert caveat['message'].startswith(f'obs columns state, score added from deposit {answer["deposit_id"]}, ')
    assert caveat['topic'] == 'obs_columns'
    key = f'{CATALOG}Public2021/int.duckdb'
    assert s3.objects[key]['body'] == duck.read_bytes()
    assert s3.objects[key]['metadata']['sha256'] == hashlib.sha256(duck.read_bytes()).hexdigest()
    committed = [cmd for cmd in git.commands if 'commit' in cmd]
    assert len(committed) == 1 and committed[0][-1] == 'Public2021/int.md'
    assert ('POST', '/admin/rescan') in api.calls and ('GET', '/databases/PU1/int/info') in api.calls
    assert (store_root / 'Public2021' / 'int.h5ad').read_bytes() == h5ad_before          # h5ad untouched
    assert s3.keys(PREFIX) == []
    status = service.status(ANALYST, answer['deposit_id'])
    assert status['state'] == 'live'
    report = service.report(ANALYST, answer['deposit_id'])
    assert report['coverage']['given'] == 10 and report['version_class'] == 'minor'


def test_any_unknown_cell_name_fails(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes(FULL + [('zz9', 'A', 1.0)]))
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and '1 cell names of the table are not cells of PU1/int' in row['error']
    assert 'zz9' in row['error']
    assert md_meta(store_root)['version'] == '1.0.0'
    connection = duckdb.connect(str(store_root / 'Public2021' / 'int.duckdb'), read_only=True)
    try:
        columns = [r[0] for r in connection.execute("SELECT column_name FROM information_schema.columns WHERE "
                                                     "table_name = '__obsnum'").fetchall()]
    finally:
        connection.close()
    assert 'score' not in columns


def test_partial_coverage_is_reported(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes(FULL[:6] + [('c6', 'NA', '')]))
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    rows, _pending = read_obs(store_root / 'Public2021' / 'int.duckdb')
    assert rows[6] == ('c6', None, None) and rows[9] == ('c9', None, None)
    report = service.report(ANALYST, answer['deposit_id'])
    assert report['coverage']['without_value'] == {'state': 4, 'score': 4}
    changes = md_meta(store_root)['changelog'][-1]['changes']
    assert any('4 of 10 cells without a value' in line for line in changes)


def test_numbers_that_are_not_numbers_fail(tmp_path, clock):
    service, s3, _root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes([('c0', 'A', 'high')] + FULL[1:]))
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'score: 1 values are not numbers' in row['error']


def test_replace_needs_owner_rights_and_is_a_major_version(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    payload = table_bytes([(cell, 5.0) for cell in CELLS], header=('cell_name', 'n_counts'))
    columns = [{'name': 'n_counts', 'dtype': 'numerical'}]
    without = create_error(service, ANALYST, payload, columns=columns)
    assert without.body['status'] == 'column_exists'
    answer = deposit(service, s3, ANALYST, payload, columns=columns, replace=True)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live', row['error']
    meta = md_meta(store_root)
    assert meta['version'] == '2.0.0'
    assert meta['changelog'][-1]['changes'][0].startswith('Version class major: data altered: obs values of n_counts')
    assert 'replaced from deposit' in meta['caveats'][-1]['message']
    connection = duckdb.connect(str(store_root / 'Public2021' / 'int.duckdb'), read_only=True)
    try:
        assert connection.execute('SELECT DISTINCT n_counts FROM __obsnum').fetchall() == [(5.0,)]
    finally:
        connection.close()


def test_replace_keeps_the_dtype(tmp_path, clock):
    service, s3, _root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    payload = table_bytes([(cell, 'x') for cell in CELLS], header=('cell_name', 'n_counts'))
    answer = deposit(service, s3, ADMIN, payload, columns=[{'name': 'n_counts', 'dtype': 'categorical'}],
                     replace=True)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'keeps its exact name and dtype' in row['error']


def test_a_column_added_meanwhile_is_a_clash_at_publication(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes(FULL))
    connection = duckdb.connect(str(store_root / 'Public2021' / 'int.duckdb'))
    connection.execute('ALTER TABLE __obscat ADD COLUMN "State" VARCHAR')
    connection.close()
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'column_exists' in row['error']


def test_a_public_dataset_is_held_then_approved(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes(FULL), db_id='PU1/pub')
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'held' and json.loads(row['held_reasons']) == ['public_access']
    assert md_meta(store_root, 'pub')['version'] == '1.1.0'
    service.approve(ADMIN, answer['deposit_id'], 'fine')
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    assert md_meta(store_root, 'pub')['version'] == '1.2.0'


def test_donor_identifiers_hold_the_deposit(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    (store_root / 'donor_crosswalk.tsv').write_text(
        'cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\nleuven\tdonor_id\tR1234567\t'
        'leuven:R1234567\tx\t1\n')
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    rows = [(cell, 'R1234567', 1.0) for cell in CELLS]
    answer = deposit(service, s3, ANALYST, table_bytes(rows))
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'held' and 'donor_identifiers' in json.loads(row['held_reasons'])


def test_a_restart_after_the_write_does_not_write_twice(tmp_path, clock):
    service, s3, store_root = setup(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, table_bytes(FULL))
    original = publisher._push_duckdb
    calls = []

    def die_once(md, warnings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('killed after the .md edit')
        return original(md, warnings)
    publisher._push_duckdb = die_once
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed'                    # a bug-shaped error: recorded
    # an admin retries; the columns and the version bump are not repeated
    service.retry(ADMIN, answer['deposit_id'])
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    meta = md_meta(store_root)
    assert meta['version'] == '1.1.0' and len(meta['caveats']) == 1
    _rows, pending = read_obs(store_root / 'Public2021' / 'int.duckdb')
    assert len(pending) == 2


def test_records_of_another_site_may_carry_the_kind():
    import deposit_records as dr
    assert 'obs_columns' in dr.DEPOSIT_KINDS
    import remote_deposits
    assert 'obs_columns' in remote_deposits.UPDATE_KINDS
