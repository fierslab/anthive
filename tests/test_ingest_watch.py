"""S3 watch route: settle logic, lock, state transitions, blocking, deletes, updates, idempotency."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

import ingest_watch
from ingest_pipeline import Pipeline, PipelineConfig, delete_consumed, plan_items
from tests.ingest_watch_helpers import (BUCKET, CATALOG, INDEX_SEED, PREFIX, FakeApi, FakeRunner, FakeS3,
                                        make_h5ad, sha_of, sidecar_text)

SETTINGS = {'prefix': PREFIX, 'catalog_prefix': CATALOG, 'settle_seconds': 0, 'interval_seconds': 300,
            'delete_incoming_after_ingest': True, 'notify': {'on': []}}


class Env:
    """A store, a fake S3 holding one deposit, and a watcher wired to fakes."""

    def __init__(self, tmp_path: Path, report: dict | None = None, settings: dict | None = None):
        self.tmp = tmp_path
        self.store = tmp_path / 'store'
        self.store.mkdir(parents=True)
        self.state = tmp_path / 'state'
        self.s3 = FakeS3()
        self.runner = FakeRunner(report)
        self.api = FakeApi(self.store)
        self.settings = {**SETTINGS, **(settings or {})}
        self.clock_value = time.time()
        self.db = ingest_watch.StateDB(self.state / 'w.sqlite')
        self.pipeline = Pipeline(PipelineConfig(store_root=self.store, work_root=tmp_path / 'work',
                                                reports_root=self.state / 'reports', bucket=BUCKET,
                                                catalog_prefix=CATALOG, post_steps=['score', 'facet_test'],
                                                state_dir=self.state, min_free_gb=0),
                                 self.s3, self.runner, self.api, log=lambda message: None)
        self.watcher = ingest_watch.Watcher(self.settings, self.db, self.s3, BUCKET, self.pipeline, self.state,
                                            log=lambda message: None, clock=lambda: self.clock_value,
                                            stop_requested=lambda: self.stop)
        self.stop = False
        self.pipeline.stop_requested = lambda: self.stop

    def deposit(self, basename: str = 'micro', group: str = 'WatchTest2026', seed: int = 0, sidecar_extra=None,
                with_seeds: bool = True, mtime: float | None = None, sha_override: str | None = None,
                extra_obs: str | None = None) -> dict:
        local = make_h5ad(self.tmp / 'upload' / f'{seed}' / f'{basename}.h5ad', seed=seed, extra_obs=extra_obs)
        sha = sha_override or sha_of(local)
        when = mtime if mtime is not None else self.clock_value
        keys = {'h5ad': f'{PREFIX}{group}/{basename}.h5ad', 'mus': f'{PREFIX}{group}/{basename}.h5ad.mus'}
        self.s3.put_file(keys['h5ad'], local, when)
        self.s3.put(keys['mus'], sidecar_text(sha, **(sidecar_extra or {})).encode(), when)
        if with_seeds:
            keys['index'] = f'{PREFIX}{group}/static/index.md'
            keys['md'] = f'{PREFIX}{group}/static/{basename}.md'
            keys['pdf'] = f'{PREFIX}{group}/static/paper.pdf'
            self.s3.put(keys['index'], INDEX_SEED.encode(), when)
            self.s3.put(keys['md'], ('---\ntitle: Watch test microglia\nreplicate_unit: donor\n---\n\n'
                                     + 'Microglia from a test. ' * 20 + '\n').encode(), when)
            self.s3.put(keys['pdf'], b'%PDF-1.4 test', when)
        return keys

    def poll_twice(self) -> dict:
        self.watcher.poll()
        return self.watcher.poll()

    def row(self, key: str) -> dict:
        return self.db.get(key)


# ── settle logic ─────────────────────────────────────────────────────────────

def test_first_poll_only_records_second_unchanged_poll_processes(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    assert env.watcher.poll() == {}
    assert env.row(keys['h5ad'])['state'] == 'seen'
    assert 'ant-convert' not in env.runner.tools()
    assert env.watcher.poll() == {'live': 1}


def test_a_changed_etag_between_polls_restarts_the_wait(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    env.watcher.poll()
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'# more\n', env.clock_value)
    assert env.watcher.poll() == {}
    assert env.row(keys['h5ad'])['state'] == 'seen'
    assert env.watcher.poll() == {'live': 1}


def test_the_first_listing_never_processes_even_an_old_upload(tmp_path):
    env = Env(tmp_path)
    env.deposit(mtime=env.clock_value - 5000)
    assert env.watcher.poll() == {}
    assert env.watcher.poll() == {'live': 1}


def test_settle_seconds_is_a_minimum_age(tmp_path):
    env = Env(tmp_path, settings={'settle_seconds': 600})
    keys = env.deposit(mtime=env.clock_value)
    assert env.poll_twice() == {}
    env.clock_value += 601
    assert env.watcher.poll() == {'live': 1}
    assert env.row(keys['h5ad'])['state'] == 'live'


def test_a_changed_static_file_restarts_the_wait(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    env.watcher.poll()
    env.s3.put(keys['pdf'], b'%PDF-1.4 v2', env.clock_value)
    assert env.watcher.poll() == {}
    assert env.watcher.poll() == {'live': 1}


def test_an_h5ad_without_its_sidecar_is_never_processed(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(mtime=env.clock_value - 5000)
    del env.s3.objects[keys['mus']]
    env.poll_twice()
    row = env.row(keys['h5ad'])
    assert row['state'] == 'seen' and 'waiting for' in row['error']
    assert not env.runner.commands


def test_plan_items_layout(tmp_path):
    objects = [{'Key': f'{PREFIX}G/a.h5ad', 'ETag': '"1"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/a.h5ad.mus', 'ETag': '"2"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/sub/b.h5ad', 'ETag': '"3"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/static/b.md', 'ETag': '"4"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/de/m/x.tsv', 'ETag': '"5"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/x/y/z.h5ad', 'ETag': '"6"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}_G/c.h5ad', 'ETag': '"7"', 'Size': 1, 'LastModified': 0}]
    items, malformed = plan_items(objects, PREFIX)
    by_name = {item.basename: item for item in items}
    assert set(by_name) == {'a', 'b'}
    assert by_name['b'].subfolder == 'sub' and by_name['b'].mus is None
    assert len(malformed) == 2
    # the .md seed of item b is not consumed by item a
    assert [obj.key for obj in by_name['a'].static_for_item()] == []
    assert [obj.key for obj in by_name['b'].static_for_item()] == [f'{PREFIX}G/static/b.md']


# ── lock ─────────────────────────────────────────────────────────────────────

def test_the_lock_refuses_a_second_run(tmp_path):
    with ingest_watch.WatchLock(tmp_path):
        with pytest.raises(ingest_watch.LockBusy):
            with ingest_watch.WatchLock(tmp_path):
                pass
    with ingest_watch.WatchLock(tmp_path):         # released again
        pass


# ── a new dataset goes live ─────────────────────────────────────────────────

def test_new_dataset_goes_live_and_the_consumed_keys_are_deleted(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    row = env.row(keys['h5ad'])
    assert row['state'] == 'live' and row['db_id'] == 'WT6/MIC' and row['version'] == '1.0.0'
    states = [event['state'] for event in env.db.events(keys['h5ad'])]
    assert states[:3] == ['seen', 'stable', 'pulled'] and 'checked' in states and states[-1] == 'live'
    group = env.store / 'WatchTest2026'
    meta = yaml.safe_load((group / 'micro.md').read_text().split('---\n')[1])
    assert meta['version'] == '1.0.0' and meta['changelog'][0]['version'] == '1.0.0'
    assert meta['sha256'] == sha_of(group / 'micro.h5ad') and meta['n_obs'] == 6 and meta['id'] == 'MIC'
    assert meta['replicate_unit'] == 'donor'                   # the curator's seed key is kept
    assert set(meta['layer_stats']) == {'X', 'counts'} and meta['obscat_include'] == ['donor']
    assert (group / 'index.md').exists() and (group / 'static' / 'paper.pdf').exists()
    assert not (group / 'static' / 'index.md').exists() and not (group / 'static' / 'micro.md').exists()
    assert (group / 'micro.duckdb').exists() and (group / 'micro.h5ad.mus').exists()
    # catalog copy under the catalog prefix, with the sha256 metadata
    assert env.s3.objects[f'{CATALOG}WatchTest2026/micro.h5ad']['metadata']['sha256'] == meta['sha256']
    assert f'{CATALOG}_catalog/manifest.json' not in env.s3.objects        # manifest is not in post_steps here
    # deletes: exactly the consumed set, after verification, one DeleteObject per key
    consumed = json.loads(row['consumed'])
    assert set(consumed) == set(keys.values())
    assert sorted(env.s3.deleted) == sorted(consumed)
    assert {entry['s3_key'] for entry in env.db.deletions() if entry['outcome'] == 'deleted'} == set(consumed)
    assert all(entry['etag'] == consumed[entry['s3_key']] for entry in env.db.deletions())
    assert not any(key.startswith(PREFIX) for key in env.s3.objects)
    assert 'ant-h5ad-check' not in ' '.join(' '.join(cmd) for cmd in env.runner.commands)
    assert row['deleted'] == 1
    # deletes come after the catalog verification (head of the catalog key precedes the first delete)
    calls = env.s3.calls
    first_delete = next(index for index, call in enumerate(calls) if call[0] == 'delete')
    assert ('head', f'{CATALOG}WatchTest2026/micro.h5ad') in calls[:first_delete]
    assert env.api.calls.count('rescan') >= 2


def test_post_step_failure_is_a_warning_and_the_item_stays_live(tmp_path):
    env = Env(tmp_path)
    env.runner.failing_steps = {'score'}
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert any('post step score failed' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_access_defaults_to_internal_with_a_caveat(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': None})
    env.poll_twice()
    group = env.store / 'WatchTest2026'
    assert 'access=internal' in (group / 'micro.h5ad.mus').read_text()
    meta = yaml.safe_load((group / 'micro.md').read_text().split('---\n')[1])
    assert any(caveat['topic'] == 'access_default' for caveat in meta['caveats'])
    assert any('access=internal' in change for change in meta['changelog'][0]['changes'])
    assert env.row(keys['h5ad'])['state'] == 'live'


# ── blocking: held, errors, sha, needs_decision ─────────────────────────────

@pytest.mark.parametrize('report, state', [
    ({'result': 'pass', 'publish': 'held', 'held_reasons': ['public_access'], 'checks': []}, 'held'),
    ({'result': 'pass', 'publish': 'held', 'held_reasons': ['donor_overlap'], 'checks': []}, 'held'),
    ({'result': 'fail', 'publish': 'held', 'held_reasons': [],
      'checks': [{'id': 'meta.required_keys', 'status': 'fail'}]}, 'failed'),
])
def test_held_and_errors_block_publication(tmp_path, report, state):
    env = Env(tmp_path, report=report)
    keys = env.deposit()
    assert env.poll_twice() == {state: 1}
    row = env.row(keys['h5ad'])
    assert row['state'] == state and row['report_path']
    assert not (env.store / 'WatchTest2026').exists()
    assert not env.s3.deleted
    assert not any(key.startswith(CATALOG) for key in env.s3.objects)
    assert 'rescan' not in env.api.calls


def test_sha_mismatch_fails(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sha_override='0' * 64)
    assert env.poll_twice() == {'failed': 1}
    assert 'sha256 mismatch' in env.row(keys['h5ad'])['error']
    assert 'ant-convert' not in env.runner.tools() and not env.s3.deleted


def test_convert_failure_fails(tmp_path):
    env = Env(tmp_path)
    env.runner.convert_ok = False
    keys = env.deposit()
    assert env.poll_twice() == {'failed': 1}
    assert 'ant-ingest-check' not in env.runner.tools()


def test_h5ad_in_store_without_md_needs_a_decision(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    (env.store / 'WatchTest2026').mkdir()
    make_h5ad(env.store / 'WatchTest2026' / 'micro.h5ad', seed=5)
    assert env.poll_twice() == {'needs_decision': 1}
    assert not env.s3.deleted


def test_failed_items_are_not_retried_until_the_upload_changes(tmp_path):
    env = Env(tmp_path, report={'result': 'fail', 'publish': 'held', 'held_reasons': [], 'checks': []})
    keys = env.deposit()
    env.poll_twice()
    commands = len(env.runner.commands)
    env.watcher.poll()
    env.watcher.poll()
    assert len(env.runner.commands) == commands
    env.runner.report = {'result': 'pass', 'publish': 'auto', 'held_reasons': [], 'checks': []}
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'# fixed\n', env.clock_value)
    assert env.poll_twice() == {'live': 1}


# ── deletes ─────────────────────────────────────────────────────────────────

def test_a_changed_etag_prevents_the_delete(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    env.watcher.poll()

    def newer_upload():                    # a new sidecar lands while the item is being published
        if env.api.calls.count('rescan') == 2:
            env.s3.put(keys['mus'], b'sha256=new\n', env.clock_value)
    env.api.on_rescan = newer_upload
    assert env.watcher.poll() == {'live': 1}
    assert keys['mus'] not in env.s3.deleted and keys['mus'] in env.s3.objects
    assert keys['h5ad'] in env.s3.deleted
    outcomes = {entry['s3_key']: entry['outcome'] for entry in env.db.deletions()}
    assert outcomes[keys['mus']].startswith('skipped:etag_changed')
    assert env.row(keys['h5ad'])['deleted'] == 0


def test_no_delete_when_not_served_or_catalog_unverified(tmp_path):
    env = Env(tmp_path)
    env.api.hide = {'WT6/MIC'}
    env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert not env.s3.deleted
    env2 = Env(tmp_path / 'b')
    env2.s3.drop_metadata = True             # legacy object without sha256 metadata ...
    env2.s3.corrupt_upload = True            # ... whose bytes differ (same size, other MD5)
    env2.deposit()
    assert env2.poll_twice() == {'live': 1}
    assert not env2.s3.deleted


def test_server_down_keeps_the_item_live_and_deletes_after_a_later_rescan(tmp_path):
    env = Env(tmp_path)
    env.api.down = True
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert not env.s3.deleted and env.row(keys['h5ad'])['served'] == 0
    env.api.down = False
    env.watcher.poll()
    assert set(env.s3.deleted) == set(json.loads(env.row(keys['h5ad'])['consumed']))


def test_delete_switch_off_keeps_everything(tmp_path):
    env = Env(tmp_path, settings={'delete_incoming_after_ingest': False})
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert not env.s3.deleted
    commands = len(env.runner.commands)
    env.watcher.poll()                      # re-run: nothing changed, nothing reprocessed
    env.watcher.poll()
    assert len(env.runner.commands) == commands
    assert env.row(keys['h5ad'])['state'] == 'live'


def test_delete_consumed_refuses_keys_outside_the_prefix():
    s3 = FakeS3()
    s3.put('Other/x.h5ad', b'x')
    with pytest.raises(ValueError):
        delete_consumed(s3, BUCKET, PREFIX, {'Other/x.h5ad': s3.objects['Other/x.h5ad']['etag']})
    with pytest.raises(ValueError):
        delete_consumed(s3, BUCKET, PREFIX, {PREFIX: 'x'})
    with pytest.raises(ValueError):
        delete_consumed(s3, BUCKET, '', {'a': 'b'})
    assert not s3.deleted


# ── updates ─────────────────────────────────────────────────────────────────

def _live_v1(env: Env) -> dict:
    keys = env.deposit()
    env.poll_twice()
    return keys


def test_update_with_new_data_is_a_minor_version_and_keeps_the_previous_one(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    md = group / 'micro.md'
    text = md.read_text().replace('caveats:', 'caveats:', 1)
    meta = yaml.safe_load(text.split('---\n')[1])
    old_sha = meta['sha256']
    # a curator caveat and facet mapping added after the first version
    md.write_text(text.replace('\n---\n', '\ncaveats:\n- level: low\n  message: curator note\n'
                                         'api:\n  obs_rename:\n    donor: ant_donor\n---\n', 1))
    (group / 'micro.scores.parquet').write_bytes(b'old scores')
    keys = env.deposit(seed=1, with_seeds=False, extra_obs='batch')
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'update_minor' and row['version'] == '1.1.0'
    new_meta = yaml.safe_load(md.read_text().split('---\n')[1])
    assert new_meta['version'] == '1.1.0'
    entry = new_meta['changelog'][-1]
    assert entry['previous_sha256'] == old_sha and entry['version'] == '1.1.0'
    assert any('batch' in change for change in entry['changes'])
    assert any(caveat.get('message') == 'curator note' for caveat in new_meta['caveats'])
    assert any(caveat.get('topic') == 'dataset_update' for caveat in new_meta['caveats'])
    assert new_meta['api'] == {'obs_rename': {'donor': 'ant_donor'}}
    archived = group / '_versions' / 'micro' / '1.0.0'
    assert sha_of(archived / 'micro.h5ad') == old_sha
    assert (archived / 'micro.scores.parquet').read_bytes() == b'old scores'
    assert not (group / 'micro.scores.parquet').exists()
    assert f'{CATALOG}WatchTest2026/_versions/micro/1.0.0/micro.h5ad' in env.s3.objects
    assert f'{CATALOG}WatchTest2026/_versions/micro/1.0.0/micro.md' in env.s3.objects
    assert 'release:WT6/MIC' in env.api.calls and 'restore:WT6/MIC' in env.api.calls
    versions = env.db.versions('WT6/MIC')
    assert [(entry['action'], entry['version']) for entry in versions][-2:] == [('current', '1.1.0'),
                                                                                 ('archived', '1.0.0')]


def test_metadata_only_update_needs_an_override_to_replace_a_curated_key(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    sidecar_path = group / 'micro.h5ad.mus'
    sidecar_path.write_text(sidecar_path.read_text() + 'local_only=kept\n')
    keys = env.deposit(seed=0, with_seeds=False, sidecar_extra={'tissue': 'prefrontal cortex',
                                                                 'override_tissue': 'true'})
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'update_patch' and row['version'] == '1.0.1'
    text = sidecar_path.read_text()
    assert 'tissue=prefrontal cortex' in text and 'local_only=kept' in text and 'override_' not in text
    meta = yaml.safe_load((group / 'micro.md').read_text().split('---\n')[1])
    assert any('tissue' in change for change in meta['changelog'][-1]['changes'])
    assert 'previous_sha256' not in meta['changelog'][-1]
    assert env.runner.tools().count('ant-convert') == 1          # only the first version was converted
    assert f'{CATALOG}WatchTest2026/_versions/micro/1.0.0/micro.h5ad' not in env.s3.objects
    assert f'{CATALOG}WatchTest2026/_versions/micro/1.0.0/micro.h5ad.mus' in env.s3.objects


def test_curated_sidecar_value_wins_without_an_override(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    keys = env.deposit(seed=0, with_seeds=False, sidecar_extra={'tissue': 'prefrontal cortex', 'strain': 'x'})
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    text = (group / 'micro.h5ad.mus').read_text()
    assert 'tissue=brain' in text and 'strain=x' in text
    meta = yaml.safe_load((group / 'micro.md').read_text().split('---\n')[1])
    changes = ' '.join(meta['changelog'][-1]['changes'])
    assert 'kept the curated' in changes and 'strain' in changes


def test_identical_reupload_is_unchanged_without_a_version_bump(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    md_before = (env.store / 'WatchTest2026' / 'micro.md').read_text()
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)   # new ETag, same keys
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['kind'] == 'unchanged'
    assert (env.store / 'WatchTest2026' / 'micro.md').read_text() == md_before
    assert not (env.store / 'WatchTest2026' / '_versions').exists()


# ── status and notifications ────────────────────────────────────────────────

def test_status_file_and_table(tmp_path):
    env = Env(tmp_path)
    env.deposit()
    env.poll_twice()
    path = ingest_watch.write_status(env.state, env.db)
    data = json.loads(path.read_text())
    assert data['items'][0]['state'] == 'live'
    assert 'live' in ingest_watch.render_status_table(data['items'])


def test_notify_webhook_failure_never_raises():
    messages = []
    ingest_watch.notify({'notify': {'on': ['live'], 'webhook_url': 'http://127.0.0.1:9/x'}},
                        {'state': 'live', 'key': 'k'}, messages.append)
    assert messages and 'webhook failed' in messages[0]
    ingest_watch.notify({'notify': {'on': ['held']}}, {'state': 'live'}, messages.append)


def test_a_sidecar_without_its_h5ad_is_reported(tmp_path):
    objects = [{'Key': f'{PREFIX}G/a.h5ad.mus', 'ETag': '"1"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/de/m/x.tsv.mus', 'ETag': '"2"', 'Size': 1, 'LastModified': 0}]
    items, malformed = plan_items(objects, PREFIX)
    assert not items and len(malformed) == 1 and 'without its h5ad' in malformed[0]


def test_held_update_leaves_the_published_version_alone(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    md_before = (env.store / 'WatchTest2026' / 'micro.md').read_text()
    env.runner.report = {'result': 'pass', 'publish': 'held', 'held_reasons': ['donor_overlap'], 'checks': []}
    keys = env.deposit(seed=0, with_seeds=False, sidecar_extra={'tissue': 'cortex', 'override_tissue': 'true'})
    env.clock_value += 10
    deleted_before = list(env.s3.deleted)
    assert env.poll_twice() == {'held': 1}
    assert (env.store / 'WatchTest2026' / 'micro.md').read_text() == md_before
    assert not (env.store / 'WatchTest2026' / '_versions').exists()
    assert env.s3.deleted == deleted_before and keys['h5ad'] in env.s3.objects
    assert env.row(keys['h5ad'])['version'] is None


def test_convert_gets_the_group_id_for_a_new_dataset_in_an_existing_group(tmp_path):
    # Francken2026, 2026-10-20: the work folder of a deposit into an existing group has no index.md, so the
    # convert did not know {group_id} and ant_donor_uid was not published
    env = Env(tmp_path)
    _live_v1(env)
    group_id = yaml.safe_load((env.store / 'WatchTest2026' / 'index.md').read_text().split('---\n')[1])['group_id']
    env.deposit(basename='second', seed=3, with_seeds=False)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    converts = [cmd for cmd in env.runner.commands if Path(cmd[0]).name == 'ant-convert']
    assert converts[-1][1].endswith('second.h5ad') and converts[-1][-2:] == ['--group-id', group_id]
    assert converts[0][-2:] == ['--group-id', group_id]          # a new group: the staged index.md's id


# ── console: one line per state change and per step ─────────────────────────

def _watch_cli():
    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parent.parent / 'bin' / 'ingest' / 'ant-ingest-watch'
    loader = importlib.machinery.SourceFileLoader('ant_ingest_watch_under_test', str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_pipeline_reports_each_step_with_timings(tmp_path):
    env = Env(tmp_path)
    lines, changes = [], []
    env.pipeline.progress = lines.append
    env.db.on_transition = lambda key, state, detail, fields: changes.append((state, fields.get('db_id')))
    env.deposit()
    assert env.poll_twice() == {'live': 1}
    text = '\n'.join(lines)
    for expected in ('pulled 0.00 GB in', 'sha256 computed in', 'convert started', 'convert finished in',
                     'check started', 'check: pass, publish auto (0 warnings) in', 'placed in the store in',
                     'post step score ok in', 'post step facet_test ok in'):
        assert expected in text, expected
    states = [state for state, _ in changes]
    assert states[:2] == ['seen', 'stable'] and 'pulled' in states and states[-1] == 'live'
    assert changes[-1][1]                                        # live carries the db id


def test_check_verdict_names_failed_checks_and_held_reasons(tmp_path):
    report = {'result': 'fail', 'publish': 'held', 'held_reasons': ['licence_unknown'],
              'checks': [{'id': 'facets.mapping_data', 'status': 'fail'}, {'id': 'access.controlled', 'status': 'warn'}]}
    env = Env(tmp_path, report=report)
    lines = []
    env.pipeline.progress = lines.append
    env.deposit()
    assert env.poll_twice() == {'failed': 1}
    verdict = next(line for line in lines if line.startswith('check: '))
    assert 'check: fail, publish held (1 warnings); failed: facets.mapping_data; held: licence_unknown' in verdict


def test_cli_console_is_concise_without_a_terminal_and_complete_in_the_log_file(tmp_path, capsys):
    cli = _watch_cli()
    cli.LOG_FILE = tmp_path / 'watch.log'
    cli.VERBOSE = False
    cli.progress('convert finished in 12.0 s')
    cli.on_transition('__incoming/G/x.h5ad', 'live', 'new 1.0.0', {'db_id': 'G01/X', 'version': '1.0.0'})
    cli.log('poll done: {"live": 1}')
    printed = capsys.readouterr().out
    assert 'convert finished' not in printed and 'poll done' in printed
    logged = cli.LOG_FILE.read_text()
    assert 'convert finished in 12.0 s' in logged and 'G/x.h5ad: live as G01/X 1.0.0 (new 1.0.0)' in logged
    cli.VERBOSE = True
    cli.on_transition('__incoming/G/x.h5ad', 'failed', 'ant-ingest-check found errors', {})
    assert 'G/x.h5ad: failed (ant-ingest-check found errors)' in capsys.readouterr().out
