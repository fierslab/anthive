"""End to end on the real object store (opt-in): artefact and collection deposits through presigned URLs,
complete, the watcher's publication, a signed download, a purge (doc/DESIGN_artefacts.md phase A2).

Runs only with ``ANTHIVE_E2E_S3=1`` and the s3 block of ``ANTHIVE_CONFIG`` (or the repository's
anthive.yaml). Only the test prefixes are touched: ``__incoming_test/``, ``__deposits_test/`` and
``__ingest_test_catalog/``. Every object the run created there is deleted at the end (keys that existed
before the run are left alone), and the test checks that none is left.
"""
from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('ANTHIVE_E2E_S3') != '1', reason='set ANTHIVE_E2E_S3=1 to run')

INCOMING = '__incoming_test/'
STAGING = '__deposits_test/'
CATALOG = '__ingest_test_catalog/'
TEST_PREFIXES = (INCOMING, STAGING, CATALOG)


def _client_and_bucket():
    import boto3
    from botocore.config import Config as BotoConfig
    import config
    s3_cfg = config.load_config().get('s3') or {}
    if not (s3_cfg.get('bucket') and s3_cfg.get('access_key_id')):
        pytest.skip('no s3 credentials')
    endpoint = s3_cfg.get('endpoint')
    if endpoint and '://' not in endpoint:
        endpoint = 'https://' + endpoint
    client = boto3.client('s3', endpoint_url=endpoint, region_name=s3_cfg.get('region') or 'us-east-1',
                          aws_access_key_id=s3_cfg['access_key_id'], aws_secret_access_key=s3_cfg['secret_access_key'],
                          config=BotoConfig(signature_version='s3v4', s3={'addressing_style': 'path' if
                                                                          s3_cfg.get('use_path_style') else 'auto'}))
    return client, s3_cfg['bucket'], s3_cfg


def _keys(client, bucket, prefix):
    keys = set()
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        keys |= {obj['Key'] for obj in page.get('Contents', []) or []}
    return keys


def _listing(client, bucket):
    objects = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=INCOMING):
        objects.extend(page.get('Contents', []) or [])
    return objects


def _http(method, url, body=None, headers=None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_artefact_route_on_the_real_bucket(tmp_path):
    import artefact_deposit
    import artefacts
    import dataset_deposit as dd
    from tests import artefact_deposit_helpers as helpers

    client, bucket, s3_cfg = _client_and_bucket()
    before = set().union(*(_keys(client, bucket, prefix) for prefix in TEST_PREFIXES))
    site = f'e2e{int(time.time())}'
    clock = [time.time()]
    store_root = tmp_path / 'store'
    helpers.make_store(store_root)
    settings = dd.DepositSettings(enabled=False, bucket=bucket, prefix=INCOMING, staging_prefix=f'{STAGING}{site}/',
                                  site_id=site, s3={'access_key_id': 'set', 'secret_access_key': 'set'})
    store = dd.DepositStore(tmp_path / 'state' / dd.DB_FILE)
    artefact_settings = {'artefacts_enabled': True, 'artefacts_max_file_gb': 100.0, 'artefacts_max_deposit_gb': 200.0,
                         'artefacts_max_files': 50, 'artefacts_max_versions': 100,
                         'artefacts_max_stored_gb_per_user': 1000.0, 'artefacts_hold_public': True,
                         'artefacts_inline_max_mb': 2.0, 'artefacts_content_check_max_gb': 2.0}
    service = artefact_deposit.ArtefactDepositService(settings, store, client, store_root, tmp_path / 'state',
                                                      artefact_settings=artefact_settings,
                                                      background=lambda work: work())
    try:
        # 1. an artefact deposit: real presigned PUTs to staging, complete with the real copy
        png = helpers.PNG + os.urandom(64)
        pdf = helpers.PDF + os.urandom(64)
        ctx = helpers.context(store_root)
        answer = service.create(helpers.ANALYST, helpers.artefact_body(
            {'volcano.png': png, 'panels/b.pdf': pdf}, scope='int',
            made_from=[{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]), ctx)
        for entry in answer['files']:
            assert entry['upload_key'].startswith(f'{STAGING}{site}/')
            body = {'volcano.png': png, 'panels/b.pdf': pdf}[entry['name']]
            status, _ = _http('PUT', entry['url'], body, entry['headers'])
            assert status == 200, status
        # a URL is bound to its size: a longer body is refused by the store
        status, _ = _http('PUT', answer['files'][0]['url'], png + b'x',
                          {**answer['files'][0]['headers'], 'Content-Length': str(len(png) + 1)})
        assert status in (400, 403)
        done = service.complete(helpers.ANALYST, answer['deposit_id'])
        assert done['state'] == 'uploaded'
        incoming = [entry['key'] for entry in done['files']]
        for key in incoming:
            head = client.head_object(Bucket=bucket, Key=key)
            assert head['Metadata']['deposit'] == answer['deposit_id']
        assert not [key for key in _keys(client, bucket, f'{STAGING}{site}/')]       # staging emptied
        # 2. the watcher publishes it
        publisher, db, git, api = helpers.make_publisher(tmp_path, client, clock)
        publisher.bucket = bucket
        publisher.prefix = INCOMING
        publisher.catalog_prefix = CATALOG
        publisher.dataset_catalog_prefix = CATALOG
        for _ in range(3):
            clock[0] += 10
            publisher.poll(_listing(client, bucket))
        row = db.get(f'artefact:{answer["deposit_id"]}')
        assert row['state'] == 'live', row['error']
        base = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v1/'
        head = client.head_object(Bucket=bucket, Key=base + 'files/volcano.png')
        assert head['Metadata']['sha256'] == hashlib.sha256(png).hexdigest() and head['ContentLength'] == len(png)
        assert client.head_object(Bucket=bucket, Key=base + 'manifest.json')['Metadata']['sha256']
        for key in incoming:                                                    # incoming deleted
            with pytest.raises(Exception):
                client.head_object(Bucket=bucket, Key=key)
        # 3. a signed download through the read side: one key, GET only
        collections, _ = artefacts.scan_collections(store_root, {'Public2021': {'PU1'}})
        index = artefacts.ArtefactIndex.build(store_root, collections)
        entry = index.get('PU1', 'int', 'volcano')
        reader = artefacts.ArtefactService(store_root, {'s3_readable': True, 'bucket': bucket, 'catalog_prefix': CATALOG,
                                                        'artefacts_url_ttl_seconds': 60,
                                                        'artefacts_download_per_minute': 100,
                                                        'artefacts_serve_local': 'fallback'}, s3_factory=lambda: client)
        delivery = reader.deliver(entry, entry.versions[1], 'volcano.png', 'user:ana')
        assert delivery.how == 'redirect'
        status, data = _http('GET', delivery.url)
        assert status == 200 and data == png
        status, _ = _http('PUT', delivery.url, b'overwrite')
        assert status == 403
        assert client.get_object(Bucket=bucket, Key=base + 'files/volcano.png')['Body'].read() == png
        other = delivery.url.replace('volcano.png', 'panels/b.pdf')
        assert _http('GET', other)[0] == 403
        # 4. purge
        service.request_purge(helpers.ADMIN, helpers.context(store_root), 'PU1', 'int', 'volcano', 'v1',
                              {'reason': 'end to end test', 'confirm': 'PU1/int/volcano@v1'})
        publisher.poll(_listing(client, bucket))
        assert not [key for key in _keys(client, bucket, base)]
        assert service.purges(helpers.ADMIN)[0]['state'] == 'done'
        # 5. a collection
        created = service.create(helpers.ANALYST, {'deposit_kind': 'collection', 'data_project': 'Fiers2026',
                                                   'collection': {'title': 'End to end collection',
                                                                  'authors': ['Mark Fiers']}},
                                 helpers.context(store_root))
        for _ in range(3):
            clock[0] += 10
            publisher.poll(_listing(client, bucket))
        assert db.get(f'artefact:{created["deposit_id"]}')['state'] == 'live'
        assert (store_root / 'Fiers2026' / 'index.md').is_file()
        # 6. a de_table, imported into the dataset with the real ant-de-ingest, then purged
        helpers.make_dataset_duckdb(store_root)
        de_payloads = helpers.de_files()
        de_answer = service.create(helpers.ANALYST, helpers.artefact_body(de_payloads, artefact_id='tvc',
                                                                          **helpers.DE_INTERNAL),
                                   helpers.context(store_root))
        for entry in de_answer['files']:
            assert _http('PUT', entry['url'], de_payloads[entry['name']], entry['headers'])[0] == 200
        assert service.complete(helpers.ANALYST, de_answer['deposit_id'])['state'] == 'uploaded'
        for _ in range(3):
            clock[0] += 10
            publisher.poll(_listing(client, bucket))
        de_row = db.get(f'artefact:{de_answer["deposit_id"]}')
        assert de_row['state'] == 'live', de_row['error']
        assert 'artefact PU1/int/tvc@v1' in (store_root / 'Public2021' / 'int.md').read_text()
        client.head_object(Bucket=bucket, Key=f'{CATALOG}Public2021/int.de.parquet')
        service.request_purge(helpers.ADMIN, helpers.context(store_root), 'PU1', 'int', 'tvc', 'v1',
                              {'reason': 'end to end test', 'confirm': 'PU1/int/tvc@v1'})
        publisher.poll(_listing(client, bucket))
        assert 'removed: purged artefact PU1/int/tvc@v1' in (store_root / 'Public2021' / 'int.md').read_text()
    finally:
        after = set().union(*(_keys(client, bucket, prefix) for prefix in TEST_PREFIXES))
        created_keys = sorted(after - before)
        for key in created_keys:
            assert key.startswith(TEST_PREFIXES), key
            client.delete_object(Bucket=bucket, Key=key)
        left = set().union(*(_keys(client, bucket, prefix) for prefix in TEST_PREFIXES)) - before
        assert not left, sorted(left)
