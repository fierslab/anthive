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
    assert caught.value.status == 403 and 'owner' in str(caught.value.body)
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
