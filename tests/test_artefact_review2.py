"""Regression tests for the second review of the artefact store (A1 + A2 fixes and A3, findings N1 to N3, the
S3 and S4 residues and the concerns). Each test started as a probe that showed the faulty behaviour
(scratchpad ``test_probe_round2.py``); it now asserts the fixed behaviour.

Harness as in tests/test_artefact_de.py: the real deposit service, presigned uploads, the publisher on a fake
S3, and the real ``ant-de-ingest`` for the DE imports.
"""
from __future__ import annotations

import hashlib
import time

import duckdb
import pytest

import artefacts
import dataset_deposit as dd
from artefact_pipeline import Stop
from ingest_pipeline import TransientError
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, CATALOG, DE_INTERNAL, GENES, OTHER, PNG, context,
                                            de_files, deposit, deseq2_table, listing, make_dataset_duckdb,
                                            make_publisher, make_service, poll_until_final, upload, watch_row)

MD = 'Public2021/int.md'


@pytest.fixture
def clock():
    return [time.time()]


def _studies(tmp_path):
    connection = duckdb.connect(str(tmp_path / 'store' / 'Public2021' / 'int.duckdb'), read_only=True)
    try:
        tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
        if '__de_study' not in tables:
            return {}
        return {row[0]: row[1] for row in connection.execute(
            'SELECT s.study_id, count(d.gene_id) FROM __de_study s LEFT JOIN __de d USING (study_id) '
            'GROUP BY s.study_id').fetchall()}
    finally:
        connection.close()


def _setup(tmp_path, clock, update_policy='own', **kwargs):
    service, s3 = make_service(tmp_path, clock, update_policy=update_policy)
    make_dataset_duckdb(tmp_path / 'store')
    publisher, db, _commits, api = make_publisher(tmp_path, s3, clock, update_policy=update_policy, **kwargs)
    return service, s3, publisher, db, api


def _purge(service, identity, artefact_id, version):
    ctx = context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=identity.is_admin,
                                                       user=identity.user))
    return service.request_purge(identity, ctx, 'PU1', 'int', artefact_id, f'v{version}',
                                 {'reason': 'wrong numbers', 'confirm': f'PU1/int/{artefact_id}@v{version}'})


def _legacy_body(files):
    return {'data_project': 'Public2021', 'basename': 'int', 'update_of': 'PU1/int',
            'files': [{'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'kind': 'de'}
                      for name, data in files.items()]}


def _head(tmp_path, artefact_id):
    return artefacts.parse_head_record((tmp_path / 'store' / 'Public2021' / 'static' / 'artefacts' / 'int' /
                                        'de_table' / artefact_id / 'artefact.md').read_text())


# ── N1: a de_table changes the dataset, so it needs the right to update the dataset ─────────────────────

def test_a_de_table_of_someone_elses_dataset_is_refused_at_create(tmp_path, clock):
    service, _s3, _publisher, _db, _api = _setup(tmp_path, clock)
    with pytest.raises(dd.DepositError) as caught:
        deposit(service, _s3, OTHER, de_files(), artefact_id='tvc', **DE_INTERNAL)
    assert caught.value.status == 403 and caught.value.body['status'] == 'not_owner'
    assert 'owner or an admin' in caught.value.body['message']
    with pytest.raises(dd.DepositError) as caught:                     # the de_only alias too
        service.create(OTHER, _legacy_body(de_files()), context(service.store_root))
    assert caught.value.status == 403
    # a plain table artefact of the same dataset stays open to any member who can see it
    deposit(service, _s3, OTHER, {'t.tsv': b'gene\tvalue\nApoe\t1\n'}, kind='table', artefact_id='plain',
            scope='int', made_from=DE_INTERNAL['made_from'])


def test_the_owner_rule_is_checked_again_at_publication(tmp_path, clock):
    service, s3, publisher, db, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    service.store.set_owner('PU1/int', 'otto', 'root', 'ownership moved after the create')
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'not_owner' in row['error']
    assert _studies(tmp_path) == {}


def test_update_policy_any_lets_every_member_add_de_tables(tmp_path, clock):
    service, s3, publisher, db, _api = _setup(tmp_path, clock, update_policy='any')
    answer = deposit(service, s3, OTHER, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'


def test_the_alias_on_a_fully_purged_artefact_follows_the_purged_version(tmp_path, clock):
    service, s3, publisher, db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='treated-vs-control', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    _purge(service, ANALYST, 'treated-vs-control', 1)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done' and _studies(tmp_path) == {}
    files = de_files()
    answer = service.create(ANALYST, _legacy_body(files), context(service.store_root))
    assert answer['artefact']['new_version_of'] == 1
    # a 2.46 client cannot declare software: no provenance warning it could never fix
    assert not any('declared.software' in warning for warning in answer['warnings'])
    upload(s3, answer, files)
    assert service.complete(ANALYST, answer['deposit_id'])['state'] == 'uploaded'
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live', row.get('error')
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}


def test_a_client_cannot_relax_provenance_with_alias_of(tmp_path, clock):
    service, s3, _publisher, _db, _api = _setup(tmp_path, clock)
    with pytest.raises(dd.DepositError) as caught:
        deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL, alias_of='de_only',
                declared={'author': {'name': 'Ana'}, 'created': '2026-10-12'})
    assert caught.value.status == 422 and 'provenance' in str(caught.value.body)


# ── N2: a purge settles a study by who wrote the rows the dataset serves now ───────────────────────────────

def test_purging_the_replacing_version_restores_the_older_versions_table(tmp_path, clock):
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, de_files(table=deseq2_table(GENES[:15], scale=9)), artefact_id='tvc',
            new_version_of=1, change_note='wrong v2', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert _studies(tmp_path) == {'treated_vs_control': 15}
    _purge(service, ANALYST, 'tvc', 2)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}           # v1's rows are served again
    md = (tmp_path / 'store' / MD).read_text()
    assert 'restored from PU1/int/tvc@v1' in md and 'de_study_removed' in md


def test_purging_an_artefact_leaves_a_study_another_artefact_replaced(tmp_path, clock):
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='first', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ADMIN, de_files(table=deseq2_table(GENES[:12])), artefact_id='third', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert _studies(tmp_path) == {'treated_vs_control': 12}
    _purge(service, ANALYST, 'first', 1)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert _studies(tmp_path) == {'treated_vs_control': 12}                   # 'third' keeps its study


def test_purging_an_older_version_keeps_the_newer_versions_rows(tmp_path, clock):
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, de_files(table=deseq2_table(GENES[:15])), artefact_id='tvc', new_version_of=1,
            change_note='v2', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    _purge(service, ANALYST, 'tvc', 1)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert _studies(tmp_path) == {'treated_vs_control': 15}


def test_rows_nobody_recorded_stop_the_purge_for_an_admin(tmp_path, clock):
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    connection = duckdb.connect(str(tmp_path / 'store' / 'Public2021' / 'int.duckdb'))
    connection.execute("UPDATE __de SET log2fc = log2fc + 1 WHERE study_id = 'treated_vs_control'")  # a hand edit
    connection.close()
    _purge(service, ANALYST, 'tvc', 1)
    publisher.poll(listing(s3))
    purge = service.purges(ADMIN)[0]
    assert purge['state'] == 'needs_decision' and 'no de_table version recorded' in purge['detail']
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}


def test_a_de_table_of_an_inactive_dataset_is_purged_and_the_removal_deferred(tmp_path, clock):
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    (tmp_path / 'store' / 'Public2021' / 'int.inactive').write_text('retired for the test\n')
    _purge(service, ANALYST, 'tvc', 1)
    publisher.poll(listing(s3))
    purge = service.purges(ADMIN)[0]
    assert purge['state'] == 'done' and 'deferred' in purge['detail']
    version = [entry for entry in _head(tmp_path, 'tvc')['versions'] if entry['version'] == 1][0]
    assert version['state'] == 'purged' and version['de_removal_deferred'] == ['treated_vs_control']
    assert not [key for key in s3.keys(CATALOG) if '/tvc/v1/files/' in key]


# ── N3: a version abandoned after its DE import rolls the import back ───────────────────────────────────

def test_a_stop_after_the_import_rolls_it_back_and_the_redeposit_goes_live(tmp_path, clock, monkeypatch):
    service, s3, publisher, db, _api = _setup(tmp_path, clock)

    def broken(*args, **kwargs):
        raise Stop('failed', 'simulated failure after the import')
    monkeypatch.setattr(publisher, '_manifest', broken)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'rolled back' in row['error']
    assert _studies(tmp_path) == {}
    md = (tmp_path / 'store' / MD).read_text()
    assert 'rolled back' in md and 'de_import_rolled_back' in md
    assert [entry['state'] for entry in _head(tmp_path, 'tvc')['versions']] == ['failed']
    monkeypatch.undo()
    retry = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, retry['deposit_id'])['state'] == 'live'
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}


def test_a_rollback_restores_the_study_an_admin_replaced(tmp_path, clock, monkeypatch):
    service, s3, publisher, db, _api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='first', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)

    def broken(*args, **kwargs):
        raise Stop('failed', 'simulated failure after the import')
    monkeypatch.setattr(publisher, '_manifest', broken)
    answer = deposit(service, s3, ADMIN, de_files(table=deseq2_table(GENES[:12])), artefact_id='second',
                     **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'failed'
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}            # 'first' serves its rows again


# ── S3 residue: give-up after transient errors and unexpected exceptions abandon the version ───────────────

def _figure_head(tmp_path):
    return artefacts.parse_head_record((tmp_path / 'store' / 'Public2021' / 'static' / 'artefacts' / 'int' /
                                        'figure' / 'volcano' / 'artefact.md').read_text())


def test_a_give_up_after_transient_errors_abandons_the_reserved_version(tmp_path, clock, monkeypatch):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _commits, _api = make_publisher(tmp_path, s3, clock)
    internal = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}

    def flaky(*args, **kwargs):
        raise TransientError('simulated S3 outage after the copies')
    monkeypatch.setattr(publisher, '_local_copies', flaky)
    first = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **internal)
    for _ in range(14):
        clock[0] += 4000
        publisher.poll(listing(s3))
    assert watch_row(db, first['deposit_id'])['state'] == 'failed'
    assert [entry['state'] for entry in _figure_head(tmp_path)['versions']] == ['failed']
    assert not [key for key in s3.keys(CATALOG) if '/files/' in key]
    assert publisher.load_marker(first['deposit_id']) is None


def test_an_unexpected_exception_abandons_the_reserved_version(tmp_path, clock, monkeypatch):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _commits, _api = make_publisher(tmp_path, s3, clock)
    internal = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}

    def bug(*args, **kwargs):
        raise KeyError('a bug after the copies')
    monkeypatch.setattr(publisher, '_local_copies', bug)
    first = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **internal)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, first['deposit_id'])['state'] == 'failed'
    assert [entry['state'] for entry in _figure_head(tmp_path)['versions']] == ['failed']
    assert publisher.load_marker(first['deposit_id']) is None


# ── S4 residue: the duplicate check keeps a bounded set and holds a table beyond it ─────────────────────────

def test_the_duplicate_check_is_bounded_and_holds_beyond_the_cap(tmp_path, clock, monkeypatch):
    import artefact_de
    monkeypatch.setattr(artefact_de, 'DUPLICATE_KEY_CAP', 5)
    service, s3, publisher, db, _api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'held' and 'de_not_checked' in row['held_reasons']
    assert _studies(tmp_path) == {}
    service.approve(ADMIN, answer['deposit_id'], 'looked at the table')
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'


# ── concern: the checks are compared with the dataset again under the lock ─────────────────────────────────

def test_a_dataset_change_during_the_checks_runs_them_again(tmp_path, clock, monkeypatch):
    import md_edit
    service, s3, publisher, db, _api = _setup(tmp_path, clock)
    real_qc = publisher._qc
    calls = []

    def qc_while_the_dataset_changes(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:                    # a new dataset version lands while the first checks run
            md = tmp_path / 'store' / MD
            md.write_text(md_edit.apply_edits(md.read_text(), [md_edit.bump_version(['other'], '2026-10-19',
                                                                                    'patch')]))
        return real_qc(*args, **kwargs)
    monkeypatch.setattr(publisher, '_qc', qc_while_the_dataset_changes)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'stable' and 'changed during the checks' in row['error']
    assert not (tmp_path / 'store' / 'Public2021' / 'static' / 'artefacts' / 'int' / 'de_table' / 'tvc').exists()
    clock[0] += 4000
    publisher.poll(listing(s3))
    assert watch_row(db, answer['deposit_id'])['state'] == 'live' and len(calls) == 2


# ── concerns: the de_sources migration ──────────────────────────────────────────────────────────────────

def _migration_store(tmp_path):
    from tests.artefact_deposit_helpers import de_sidecar
    store = tmp_path / 'store'
    folder = store / 'Public2021' / 'de_sources' / 'int' / 'deseq2'
    folder.mkdir(parents=True)
    (folder / 'treated_vs_control.tsv').write_bytes(deseq2_table())
    (folder / 'treated_vs_control.tsv.mus').write_bytes(de_sidecar())
    return store


def _hand_import(tmp_path, publisher):
    """The study imported by hand (ant-de-ingest through the DE-only import), as Sierksma2020 was."""
    import ingest_pipeline
    from tests.artefact_deposit_helpers import de_sidecar
    store = tmp_path / 'store'
    table = tmp_path / 'hand' / 'treated_vs_control.tsv'
    table.parent.mkdir()
    table.write_bytes(deseq2_table())
    meta = ingest_pipeline.parse_sidecar_text(de_sidecar().decode())
    ingest_pipeline.import_de_all_or_nothing(
        store_root=store, md=store / MD, db_id='PU1/int', work=tmp_path / 'hand_work',
        tables=[(table, meta, 'treated_vs_control')], replace=[], runner=publisher.de_runner,
        api=type('NoApi', (), {'release': lambda self, db_id: None, 'restore': lambda self, db_id: None})(),
        today='2026-10-19', change=lambda _archived: 'DE treated_vs_control imported by hand.')


def test_migration_skips_tables_of_retired_datasets(tmp_path, clock):
    import artefact_migrate
    from tests.artefact_deposit_helpers import de_sidecar
    make_service(tmp_path, clock)
    store = _migration_store(tmp_path)
    make_dataset_duckdb(store)
    retired = store / 'Public2021' / '_deleted_2026-10-05'
    retired.mkdir()
    (retired / 'old.md').write_text('---\nid: AQB\ntitle: Retired cells\nversion: 1.0.0\n---\n')
    sources = store / 'Public2021' / 'de_sources' / 'aqb'
    sources.mkdir()
    (sources / 'res_TvsC.tsv').write_bytes(deseq2_table())
    (sources / 'res_TvsC.tsv.mus').write_bytes(de_sidecar('res_TvsC', de_dataset_id='AQB'))
    rows = {item.table.name: item for item in artefact_migrate.plan(store)}
    assert rows['res_TvsC.tsv'].skip.startswith('target dataset retired (Public2021/_deleted_2026-10-05/)')


def test_migration_links_a_hand_imported_study_and_its_purge_removes_it(tmp_path, clock):
    import artefact_migrate
    service, s3, publisher, _db, _api = _setup(tmp_path, clock)
    store = _migration_store(tmp_path)
    _hand_import(tmp_path, publisher)
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}
    items = artefact_migrate.plan(store)
    item = next(entry for entry in items if entry.table.name == 'treated_vs_control.tsv')
    assert item.study_in_duckdb and item.fingerprint and item.made_from_version == '1.0.1'
    assert artefact_migrate.apply(store, items, s3, 'bucket', CATALOG, log=lambda message: None) == \
        ['PU1/int/treated-vs-control']
    service.store.set_owner('PU1/int', 'ana', 'root')
    _purge(service, ADMIN, 'treated-vs-control', 1)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert _studies(tmp_path) == {}


def test_migration_matches_study_ids_exactly(tmp_path):
    import artefact_migrate
    assert artefact_migrate.names_study('DE from deposit x: added treated_vs_control.', 'treated_vs_control')
    assert not artefact_migrate.names_study('added treated_vs_control_v2', 'treated_vs_control')
    assert not artefact_migrate.names_study('added old.treated_vs_control', 'treated_vs_control')
    meta = {'version': '1.0.2', 'changelog': [
        {'version': '1.0.1', 'changes': ['DE treated_vs_control_v2 imported']},
        {'version': '1.0.2', 'changes': ['DE treated_vs_control imported']}]}
    assert artefact_migrate._made_from(meta, 'treated_vs_control')[0] == '1.0.2'


def test_migration_aborts_on_a_head_error_other_than_404(tmp_path, clock):
    import artefact_migrate
    from tests.ingest_watch_helpers import S3Error
    _service, s3 = make_service(tmp_path, clock)
    store = _migration_store(tmp_path)
    make_dataset_duckdb(store)

    def forbidden(Bucket, Key):  # noqa: N803
        raise S3Error('403')
    s3.__dict__['head_object'] = forbidden
    with pytest.raises(RuntimeError, match='403'):
        artefact_migrate.apply(store, artefact_migrate.plan(store), s3, 'bucket', CATALOG, log=lambda message: None)
    assert s3.keys(CATALOG) == []
    assert not (store / 'Public2021' / 'static').exists()


def test_migration_never_overwrites_a_manifest_already_on_s3(tmp_path, clock):
    import artefact_migrate
    _service, s3 = make_service(tmp_path, clock)
    store = _migration_store(tmp_path)
    make_dataset_duckdb(store)
    path = artefacts.ArtefactPath('Public2021', 'int', 'de_table', 'treated-vs-control')
    manifest_key = artefacts.s3_key(CATALOG, path.manifest_rel(1))
    s3.put(manifest_key, b'{"other": true}', clock[0], metadata={'sha256': 'x' * 64})
    with pytest.raises(RuntimeError, match='never overwritten'):
        artefact_migrate.apply(store, artefact_migrate.plan(store), s3, 'bucket', CATALOG, log=lambda message: None)
    assert s3.objects[manifest_key]['body'] == b'{"other": true}'
    assert not (store / str(path.head_rel)).exists()
