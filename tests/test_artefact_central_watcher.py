"""An artefact deposit of a site with a central watcher (design section 24, checked for 2.47.2): the site stages
the verified uploads and a signed record, the central watcher imports the record, copies the uploads into
incoming and publishes the artefact with the artefact route. One fake S3, both halves real.
"""
from __future__ import annotations

import hashlib
import time

import pytest

import artefact_pipeline
import dataset_deposit as dd
import deposit_records as dr
import ingest_watch
import remote_deposits
from tests.artefact_deposit_helpers import (ANALYST, BUCKET, CATALOG, PNG, PREFIX, SITE, STAGING, ArtefactFakeS3,
                                            FakeApi, FakeGit, deposit, listing, make_service, make_store, watch_row)

SECRET = 'testsite-secret-' + 'x' * 40
INTERNAL = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}
ARTEFACT_SETTINGS = {'artefacts_enabled': True, 'artefacts_max_file_gb': 100.0, 'artefacts_max_deposit_gb': 200.0,
                     'artefacts_max_files': 50, 'artefacts_max_versions': 100, 'artefacts_hold_public': True,
                     'artefacts_inline_max_mb': 2.0, 'artefacts_content_check_max_gb': 2.0}


@pytest.fixture
def clock():
    return [time.time()]


def _central(tmp_path, s3, clock):
    """(RemoteSites, StateDB, publisher, public key, state dir) of the central watcher with its own store."""
    root = tmp_path / 'central'
    store_root = make_store(root / 'store')
    (store_root / '.git').mkdir()
    state = root / 'state'
    key = dr.load_or_create_result_key(state / 'secrets' / 'deposit_result_ed25519.pem')
    remote = remote_deposits.RemoteSites(
        s3=s3, bucket=BUCKET, state_dir=state, sites={SITE: {'k1': SECRET.encode()}}, result_key=key,
        staging_root=STAGING, incoming_prefix=PREFIX, local_site_id='central', log=lambda message: None,
        catalog_bucket=BUCKET, catalog_prefix=CATALOG, clock=lambda: clock[0])
    db = ingest_watch.StateDB(state / ingest_watch.DB_FILE)
    publisher = artefact_pipeline.ArtefactPublisher(
        store_root=store_root, state_dir=state, work_root=root / 'work', s3=s3, bucket=BUCKET,
        incoming_prefix=PREFIX, catalog_prefix=CATALOG, artefact_settings=ARTEFACT_SETTINGS, db=db, api=FakeApi(),
        settle_seconds=0, log=lambda message: None, clock=lambda: clock[0], run=FakeGit(), site_id='central',
        server_version='2.47.2')
    return remote, db, publisher, dr.public_key_text(key), state


def test_an_artefact_deposited_at_a_central_site_is_published_by_the_central_watcher(tmp_path, clock):
    s3 = ArtefactFakeS3(lambda: clock[0])
    remote, db, publisher, public_key, central_state = _central(tmp_path, s3, clock)
    service, _ = make_service(tmp_path / 'site', clock, s3=s3, central_watcher=True, site_key_id='k1',
                              site_secret=SECRET, watcher_public_keys=[public_key], catalog_bucket=BUCKET)
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    deposit_id = answer['deposit_id']
    assert s3.keys(PREFIX) == []                                    # the site never writes incoming
    assert f'{STAGING}{SITE}/{deposit_id}/{dr.RECORD_NAME}' in s3.objects
    assert s3.keys(CATALOG) == []

    central_store = dd.DepositStore(central_state / dd.DB_FILE)
    for _ in range(4):
        clock[0] += 10
        remote.import_records(central_store, {})
        publisher.poll(listing(s3))
    row = watch_row(db, deposit_id)
    assert row['state'] == 'live', row
    base = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/'
    assert s3.keys(CATALOG) == sorted([base + 'artefact.md', base + 'v1/manifest.json', base + 'v1/files/volcano.png'])
    assert s3.objects[base + 'v1/files/volcano.png']['metadata']['sha256'] == hashlib.sha256(PNG).hexdigest()
    assert s3.keys(PREFIX) == []                                    # incoming removed after placement
