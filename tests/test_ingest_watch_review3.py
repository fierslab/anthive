"""Regression tests for the third review of the S3 watch route (2026-10-14): interrupted post steps,
per-key delete errors, multipart part sizes, resume markers across later passes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import ingest_pipeline
from ingest_pipeline import candidate_part_sizes, delete_consumed, etag_matches_file, slug
from tests.ingest_watch_helpers import BUCKET, PREFIX, FakeS3, S3Error
from tests.test_ingest_watch import Env, _live_v1


def _meta(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split('---\n')[1])


def _stop_during(env: Env, step: str, code: int = -15):
    """Make the runner behave as if SIGTERM arrived while ``post:<step>`` ran."""
    original = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        if label == f'post:{step}' and not env.stop_hit:
            env.stop_hit = True
            env.stop = True
            env.runner.commands.append([str(part) for part in cmd])
            return code, 'Terminated'
        return original(label, cmd, cwd, timeout)

    env.stop_hit = False
    env.runner.run = run


def _marker(env: Env, key: str):
    path = env.state / 'swaps' / 'resume' / f'{slug(key)}.json'
    return json.loads(path.read_text()) if path.exists() else None


# ── 1: SIGTERM / SIGKILL during the post steps ──────────────────────────────

def test_sigterm_during_a_post_step_keeps_incoming_and_the_restart_finishes(tmp_path):
    env = Env(tmp_path)
    _stop_during(env, 'facet_test')
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    row = env.row(keys['h5ad'])
    assert row['state'] == 'stable' and 'post step facet_test stopped' in row['error']
    assert not env.s3.deleted and keys['h5ad'] in env.s3.objects                # nothing deleted
    marker = _marker(env, keys['h5ad'])
    assert marker['pushed'] is True and marker['done_steps'] == ['score']        # score ran, facet_test did not
    assert (env.store / 'WatchTest2026' / 'micro.md').exists()                  # published already
    env.stop = False
    before = len(env.runner.commands)
    env.clock_value += 120
    assert env.watcher.poll() == {'live': 1}
    tools = [Path(cmd[0]).name for cmd in env.runner.commands[before:]]
    assert 'ant-facet-test' in tools and 'ant-score-fleet' not in tools         # only the remaining step
    assert _marker(env, keys['h5ad']) is None
    assert keys['h5ad'] in env.s3.deleted
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'new' and row['version'] == '1.0.0'


def test_after_a_stop_in_the_post_steps_the_restart_still_commits(tmp_path):
    env = Env(tmp_path)
    (env.store / '.git').mkdir()
    env.pipeline.config.store_git_commit = True
    _stop_during(env, 'facet_test')
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    env.stop = False
    env.clock_value += 120
    before = len(env.runner.commands)
    assert env.watcher.poll() == {'live': 1}
    assert any('commit' in cmd for cmd in env.runner.commands[before:] if cmd[0] == 'git')
    assert not any('changed after the route wrote them' in warning
                   for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_sigkill_during_a_post_step_is_finished_on_restart(tmp_path):
    env = Env(tmp_path)

    class Killed(BaseException):
        pass

    original = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        if label == 'post:facet_test' and not getattr(env, 'killed', False):
            env.killed = True
            raise Killed()
        return original(label, cmd, cwd, timeout)

    env.runner.run = run
    keys = env.deposit()
    with pytest.raises(Killed):
        env.poll_twice()
    assert env.row(keys['h5ad'])['state'] == 'pulled' and _marker(env, keys['h5ad'])['done_steps'] == ['score']
    env.clock_value += 120
    assert env.watcher.poll() == {'live': 1}
    assert _marker(env, keys['h5ad']) is None and keys['h5ad'] in env.s3.deleted


def test_no_delete_while_a_resume_is_pending(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    env.poll_twice()
    item = ingest_pipeline.plan_items([{'Key': keys['h5ad'], 'ETag': '"x"', 'Size': 1, 'LastModified': 0}],
                                      PREFIX)[0][0]
    ingest_pipeline.write_resume(env.state / 'swaps', slug(keys['h5ad']), {'to': '1.0.0'})
    assert env.pipeline.has_pending(item)
    outcome = ingest_pipeline.Outcome(state='live', served=True, catalog_verified=True, consumed={'x': 'y'})
    env.watcher.delete_if_verified(item, outcome)
    assert any('still pending' in event['detail'] for event in env.db.events(keys['h5ad']))


@pytest.mark.parametrize('accept, deleted', [(True, True), (False, False)])
def test_post_step_warnings_and_the_delete(tmp_path, accept, deleted):
    env = Env(tmp_path, settings={'delete_despite_post_warnings': accept})
    env.runner.failing_steps = {'score'}
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert (keys['h5ad'] in env.s3.deleted) is deleted
    events = ' '.join(event['detail'] for event in env.db.events(keys['h5ad']))
    assert ('accepted for the delete' in events) is accept
    assert json.loads(env.row(keys['h5ad'])['post_failed']) == ['score']


# ── 5: a pending marker is honoured whatever the next pass is ───────────────

def test_pending_post_steps_run_before_a_later_patch_update(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    _stop_during(env, 'facet_test')
    keys = env.deposit(seed=1, with_seeds=False)                                # data update 2.0.0 (major), stopped in post
    env.clock_value += 10
    assert env.poll_twice() == {'retry': 1}
    assert _marker(env, keys['h5ad'])['to'] == '2.0.0'
    env.stop = False
    keys = env.deposit(seed=1, with_seeds=False, sidecar_extra={'tissue': 'cortex', 'override_tissue': 'true'})
    env.clock_value += 120
    before = len(env.runner.commands)
    assert env.poll_twice() == {'live': 1}
    tools = [Path(cmd[0]).name for cmd in env.runner.commands[before:]]
    assert tools.count('ant-facet-test') == 1                                   # the pending step of 2.0.0
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'update_patch' and row['version'] == '2.0.1'
    versions = [(entry['action'], entry['version']) for entry in env.db.versions('WT6/MIC')]
    assert ('current', '2.0.0') in versions and ('current', '2.0.1') in versions
    assert _marker(env, keys['h5ad']) is None and keys['h5ad'] in env.s3.deleted


def test_a_stale_marker_is_removed_with_a_warning(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    md = env.store / 'WatchTest2026' / 'micro.md'
    keys = env.deposit(seed=0, with_seeds=False)
    ingest_pipeline.write_resume(env.state / 'swaps', slug(keys['h5ad']),
                                 {'to': '0.9.0', 'sha256': 'x', 'kind': 'update_minor', 'done_steps': []})
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert _marker(env, keys['h5ad']) is None
    assert any('stale resume marker' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))
    assert _meta(md)['version'] == '1.0.0'


# ── 2: a failing delete never crashes the poll and is retried ───────────────

@pytest.mark.parametrize('if_match', [True, False])
def test_a_delete_error_is_recorded_per_key(if_match):
    s3 = FakeS3()
    good, bad = f'{PREFIX}G/a.h5ad', f'{PREFIX}G/b.h5ad'
    etags = {good: s3.put(good, b'a'), bad: s3.put(bad, b'b')}
    original = s3.delete_object

    def flaky(Bucket, Key, IfMatch=None):  # noqa: N803
        if Key == bad:
            raise S3Error('InternalError')
        return original(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    s3.delete_object = flaky
    results = {}
    deleted, skipped = delete_consumed(s3, BUCKET, PREFIX, etags, lambda key, etag, outcome: results.update({key: outcome}),
                                       if_match=if_match)
    assert deleted == [good] and skipped == [bad] and results[bad] == 'skipped:error:InternalError'


def test_a_failed_delete_is_retried_on_the_next_poll(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    original = env.s3.delete_object
    state = {'fail': True}

    def flaky(Bucket, Key, IfMatch=None):  # noqa: N803
        if Key == keys['h5ad'] and state['fail']:
            raise S3Error('ServiceUnavailable')
        return original(Bucket=Bucket, Key=Key, IfMatch=IfMatch)

    env.s3.delete_object = flaky
    assert env.poll_twice() == {'live': 1}
    assert keys['h5ad'] in env.s3.objects and env.row(keys['h5ad'])['deleted'] == 0
    state['fail'] = False
    env.watcher.poll()
    assert keys['h5ad'] not in env.s3.objects and env.row(keys['h5ad'])['deleted'] == 1


# ── 4: part sizes ───────────────────────────────────────────────────────────

def test_part_sizes_include_5_mib_and_the_boto3_enlargement():
    mib = 1024 * 1024
    assert 5 * mib in candidate_part_sizes(100 * mib)
    huge = 200 * 1024 ** 3                       # 200 GiB: 8 MiB would need 25,600 parts
    sizes = candidate_part_sizes(huge)
    assert 32 * mib in sizes                     # boto3: 8 -> 16 -> 32 MiB (doubling until <= 10,000 parts)
    assert -(-huge // 10000 // mib) * mib in sizes or (-(-huge // 10000) + mib - 1) // mib * mib in sizes
    assert all(-(-huge // size) <= 10000 for size in sizes)


def test_etag_of_5_mib_parts_is_recognised(tmp_path):
    path = tmp_path / 'blob'
    data = bytes(range(256)) * 50000                                         # about 12 MB, 3 parts of 5 MiB
    path.write_bytes(data)
    part = 5 * 1024 * 1024
    digests = [hashlib.md5(data[start:start + part]).digest() for start in range(0, len(data), part)]
    etag = hashlib.md5(b''.join(digests)).hexdigest() + f'-{len(digests)}'
    assert etag_matches_file(etag, path)
