"""Two key pairs for a central watcher (lib/s3_router.py, design section 24.9).

Calls go to the client of their bucket; a copy between the ingestion bucket's client and the catalog's client is
streamed through the host (single PUT or multipart), its sha256 compared with the target metadata before the
upload is completed (aborted otherwise); with one client copies stay server-side. End to end: a deposit of site
"intern" goes live through a watcher whose ingestion keys and catalog keys are separate fake S3 services, and
neither service ever sees a call for the other's bucket.
"""
from __future__ import annotations

import hashlib

import pytest

import s3_router
from tests.artefact_deposit_helpers import ArtefactFakeS3


class Service(ArtefactFakeS3):
    """One S3 service holding one bucket; any call for another bucket is a test failure (other credentials)."""

    def __init__(self, bucket: str):
        super().__init__()
        self.bucket = bucket
        self.foreign = []
        self.server_copies = 0
        self.parts = {}

    def _mine(self, bucket):
        if bucket != self.bucket:
            self.foreign.append(bucket)

    def head_object(self, Bucket, Key):  # noqa: N803
        self._mine(Bucket)
        return super().head_object(Bucket=Bucket, Key=Key)

    def get_object(self, Bucket, Key, IfMatch=None, Range=None):  # noqa: N803
        self._mine(Bucket)
        response = super().get_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch, Range=Range)
        response['Metadata'] = dict(self.objects[Key]['metadata'])
        return response

    def put_object(self, Bucket, Key, Body, Metadata=None, ContentType=None, **kwargs):  # noqa: N803
        self._mine(Bucket)
        kwargs.pop('ContentDisposition', None)
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, Metadata=Metadata, ContentType=ContentType)

    def copy_object(self, Bucket, Key, CopySource, **kwargs):  # noqa: N803
        self._mine(Bucket)
        self._mine(CopySource['Bucket'])
        self.server_copies += 1
        return super().copy_object(Bucket=Bucket, Key=Key, CopySource=CopySource, **kwargs)

    def create_multipart_upload(self, Bucket, Key, Metadata=None, **kwargs):  # noqa: N803
        self._mine(Bucket)
        return super().create_multipart_upload(Bucket=Bucket, Key=Key, Metadata=Metadata)

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):  # noqa: N803
        self._mine(Bucket)
        self.uploads[UploadId]['parts'][PartNumber] = Body
        return {'ETag': f'"{hashlib.md5(Body).hexdigest()}"'}

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self._mine(Bucket)
        self.aborted = True
        return super().abort_multipart_upload(Bucket=Bucket, Key=Key, UploadId=UploadId)


BODY = b'staged bytes ' * 50
SHA = hashlib.sha256(BODY).hexdigest()


def services():
    ingest, data = Service('ingest'), Service('data')
    ingest.put('__incoming/G/x.tsv', BODY, metadata={'sha256': SHA, 'deposit': 'dep_1'})
    return ingest, data, s3_router.BucketRouter(data, {'ingest': ingest})


@pytest.mark.parametrize('part_bytes', [s3_router.PART_BYTES, 64])      # one PUT, or a multipart upload
def test_a_copy_between_two_key_pairs_is_streamed_and_sha256_checked(part_bytes):
    ingest, data, _router = services()
    etag = ingest.objects['__incoming/G/x.tsv']['etag']
    head = s3_router.streamed_copy(ingest, 'ingest', '__incoming/G/x.tsv', data, 'data', 'cat/G/x.tsv',
                                   metadata={'sha256': SHA}, if_match=f'"{etag}"', part_bytes=part_bytes)
    assert data.objects['cat/G/x.tsv']['body'] == BODY and head['Metadata'] == {'sha256': SHA}
    assert ingest.foreign == [] and data.foreign == []


@pytest.mark.parametrize('part_bytes', [s3_router.PART_BYTES, 64])
def test_bytes_that_are_not_the_expected_sha256_are_never_left_at_the_target(part_bytes):
    ingest, data, _router = services()
    with pytest.raises(s3_router.CopyError):
        s3_router.streamed_copy(ingest, 'ingest', '__incoming/G/x.tsv', data, 'data', 'cat/G/x.tsv',
                                metadata={'sha256': 'f' * 64}, part_bytes=part_bytes)
    assert 'cat/G/x.tsv' not in data.objects
    if part_bytes == 64:
        assert getattr(data, 'aborted', False) and data.uploads == {}


def test_a_changed_source_is_refused_by_the_pinned_etag():
    ingest, data, router = services()
    with pytest.raises(Exception):
        router.copy_object(Bucket='data', Key='cat/G/x.tsv', CopySource={'Bucket': 'ingest', 'Key': '__incoming/G/x.tsv'},
                           Metadata={'sha256': SHA}, MetadataDirective='REPLACE', CopySourceIfMatch='"0000"')
    assert 'cat/G/x.tsv' not in data.objects


def test_the_router_sends_each_call_to_its_buckets_client_and_streams_only_across_clients():
    ingest, data, router = services()
    assert router.head_object(Bucket='ingest', Key='__incoming/G/x.tsv')['ContentLength'] == len(BODY)
    router.copy_object(Bucket='data', Key='cat/G/x.tsv', CopySource={'Bucket': 'ingest', 'Key': '__incoming/G/x.tsv'},
                       Metadata={'sha256': SHA}, MetadataDirective='REPLACE')
    assert ingest.server_copies == 0 and data.server_copies == 0            # streamed
    router.copy_object(Bucket='ingest', Key='__incoming/G/y.tsv',
                       CopySource={'Bucket': 'ingest', 'Key': '__incoming/G/x.tsv'})
    assert ingest.server_copies == 1                                        # one client: server-side
    router.copy(CopySource={'Bucket': 'ingest', 'Key': '__incoming/G/x.tsv'}, Bucket='data', Key='cat/G/z.tsv',
                ExtraArgs={'Metadata': {'sha256': SHA}, 'MetadataDirective': 'REPLACE'})
    assert data.objects['cat/G/z.tsv']['body'] == BODY and data.server_copies == 0
    assert ingest.foreign == [] and data.foreign == []
    pages = router.get_paginator('list_objects_v2').paginate(Bucket='ingest', Prefix='__incoming/')
    assert [obj['Key'] for obj in pages[0]['Contents']] == ['__incoming/G/x.tsv', '__incoming/G/y.tsv']


def test_copy_mode_server_asks_the_target_for_a_server_side_copy():
    ingest, data, _router = services()
    router = s3_router.BucketRouter(data, {'ingest': ingest}, copy_mode='server')
    data.objects.update(ingest.objects)                    # a target key pair that can read the source
    router.copy_object(Bucket='data', Key='cat/G/x.tsv', CopySource={'Bucket': 'ingest', 'Key': '__incoming/G/x.tsv'})
    assert data.server_copies == 1
    with pytest.raises(ValueError):
        s3_router.BucketRouter(data, {}, copy_mode='teleport')


def test_a_central_deposit_goes_live_with_separate_ingestion_and_catalog_keys(tmp_path):
    """The watcher's catalog keys never reach the ingestion bucket and the other way round."""
    import dataset_deposit as dd
    from tests.test_deposit_central_watcher import _deposit, central
    env, service, clock = central(tmp_path)
    ingest, data = Service('ingest'), Service('data')
    ingest.clock = data.clock = lambda: env.clock_value
    router = s3_router.BucketRouter(data, {'ingest': ingest})
    for component in (env.pipeline, env.watcher, env.watcher.remote):
        component.s3 = router
    env.watcher._list_s3 = router
    env.s3 = ingest                                        # the site holds only the ingestion keys
    service.s3 = ingest
    deposit_id = _deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    assert dd.DepositStore(env.state / dd.DB_FILE).get(deposit_id)['watch_state'] == 'live'
    assert any(key.endswith("micro.h5ad") for key in data.objects)          # the catalog push, data keys only
    assert not [key for key in ingest.objects if not key.startswith(('__incoming_test/', '__deposits/'))]
    assert ingest.foreign == [] and data.foreign == []
    assert service.status(dd.Identity(user='alice'), deposit_id)['state'] == 'live'


def test_an_artefact_is_placed_with_separate_ingestion_and_catalog_keys(tmp_path):
    """The artefact route's copies from incoming to the catalog are streamed (no server-side copy can cross
    two key pairs) and keep the sha256 metadata."""
    import time as _time
    from tests.artefact_deposit_helpers import (ANALYST, CATALOG, PNG, deposit, listing, make_publisher,
                                                make_service, watch_row)
    from tests.ingest_watch_helpers import BUCKET
    clock = [_time.time()]
    ingest, data = Service(BUCKET), Service('data')
    ingest.clock = data.clock = lambda: clock[0]
    service, _ = make_service(tmp_path, clock, s3=ingest)
    publisher, db, _git, _api = make_publisher(tmp_path, ingest, clock)
    publisher.s3 = s3_router.BucketRouter(data, {BUCKET: ingest})
    publisher.bucket = 'data'
    publisher.incoming_bucket = BUCKET
    answer = deposit(service, ingest, ANALYST, {'volcano.png': PNG}, scope='int',
                     made_from=[{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}])
    for _ in range(4):
        clock[0] += 10
        publisher.poll(listing(ingest))
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    key = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/v1/files/volcano.png'
    assert data.objects[key]['body'] == PNG
    assert data.objects[key]['metadata']['sha256'] == hashlib.sha256(PNG).hexdigest()
    assert data.server_copies == 0 and ingest.foreign == [] and data.foreign == []


def test_the_ingestion_key_pair_comes_from_secrets_files_or_env_never_the_settings(tmp_path, monkeypatch):
    import config
    for name in ('ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'BUCKET', 'ENDPOINT', 'REGION'):
        monkeypatch.delenv(f'ANTHIVE_INGEST_WATCH_INCOMING_S3_{name}', raising=False)
    monkeypatch.delenv('ANTHIVE_INGEST_STATE_DIR', raising=False)
    cfg = {'ingest_state_dir': str(tmp_path / 'ingest'), 's3': {'endpoint': 's3.example', 'bucket': 'data'},
           'ingest_watch': {'incoming_bucket': 'ingest'}}
    assert config.get_incoming_s3(cfg) is None                         # one key pair
    secrets = tmp_path / 'ingest' / 'secrets'
    secrets.mkdir(parents=True)
    (secrets / 'incoming_s3_access_key_id').write_text('AKIDFILE\n')
    with pytest.raises(config.ConfigError):
        config.get_incoming_s3(cfg)                                    # half a pair
    (secrets / 'incoming_s3_secret_access_key').write_text('secretfile\n')
    pair = config.get_incoming_s3(cfg)
    assert pair == {'endpoint': 's3.example', 'region': '', 'bucket': 'ingest', 'access_key_id': 'AKIDFILE',
                    'secret_access_key': 'secretfile'}
    monkeypatch.setenv('ANTHIVE_INGEST_WATCH_INCOMING_S3_ACCESS_KEY_ID', 'AKIDENV')
    assert config.get_incoming_s3(cfg)['access_key_id'] == 'AKIDENV'
    settings = config.get_ingest_watch_settings(dict(cfg, ingest_watch={'incoming_bucket': 'ingest', 'incoming_s3': {
        'secret_access_key': 'inline'}}))
    assert 'incoming_s3' not in settings and settings['copy_mode'] == 'auto'
    with pytest.raises(config.ConfigError):
        config.get_incoming_s3(dict(cfg, ingest_watch={}))            # a pair without a bucket
    with pytest.raises(config.ConfigError):
        config.get_ingest_watch_settings(dict(cfg, ingest_watch={'copy_mode': 'teleport'}))
