"""Regression tests for the second review of the S3 watch route (N1 to N12 and nits, 2026-10-14)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import ingest_pipeline
import ingest_watch
from ingest_pipeline import (SwapJournal, delete_consumed, etag_matches_file, if_match_selftest, multipart_etag,
                             transient_output)
from tests.ingest_watch_helpers import BUCKET, CATALOG, PREFIX, FakeS3, S3Error, make_h5ad, sha_of
from tests.test_ingest_watch import Env, _live_v1

ROOT = Path(__file__).parent.parent


def _meta(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split('---\n')[1])


class Killed(BaseException):
    """A SIGKILL stand-in: not an Exception, nothing in the pipeline catches it."""


# ── N1: a swap finished by recovery still gets push, post steps, versions ───

def test_n1_recovery_runs_the_post_steps_push_and_versions(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    (group / 'micro.scores.parquet').write_bytes(b'old scores')
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    real_replace = ingest_pipeline.os.replace
    count = {'n': 0}

    def crashing_replace(src, dst, *args, **kwargs):
        if str(src).endswith('.ingest-new'):
            count['n'] += 1
            if count['n'] == 2:
                raise Killed('SIGKILL mid-commit')
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(ingest_pipeline.os, 'replace', crashing_replace)
    with pytest.raises(Killed):
        env.poll_twice()
    monkeypatch.setattr(ingest_pipeline.os, 'replace', real_replace)
    assert env.row(keys['h5ad'])['state'] == 'pulled'
    before = len(env.runner.commands)
    env.clock_value += 3600
    assert env.watcher.poll() == {'live': 1}                 # recovery, then the item is finished at once
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'update_major' and row['version'] == '2.0.0'
    tools = [Path(cmd[0]).name for cmd in env.runner.commands[before:]]
    assert 'ant-score-fleet' in tools and 'ant-facet-test' in tools               # the post steps ran
    assert not (env.state / 'swaps' / 'resume').exists() or not list((env.state / 'swaps' / 'resume').iterdir())
    assert [(entry['action'], entry['version']) for entry in env.db.versions('WT6/MIC')][-2:] == \
        [('current', '2.0.0'), ('archived', '1.0.0')]
    catalog = env.s3.objects[f'{CATALOG}WatchTest2026/micro.h5ad']
    assert catalog['metadata']['sha256'] == sha_of(group / 'micro.h5ad')          # the new version was pushed
    assert keys['h5ad'] in env.s3.deleted
    assert 'restore:WT6/MIC' in env.api.calls


# ── N2: IfMatch is never trusted alone; the self-test ───────────────────────

class IgnoringIfMatchS3(FakeS3):
    """A backend that accepts the IfMatch header and ignores it."""

    def delete_object(self, Bucket, Key, IfMatch=None):  # noqa: N803
        self.calls.append(('delete', Key))
        self.objects.pop(Key, None)


@pytest.mark.parametrize('use_if_match', [True, False])
def test_n2_a_newer_upload_survives_a_backend_that_ignores_ifmatch(use_if_match):
    s3 = IgnoringIfMatchS3()
    key = f'{PREFIX}G/a.h5ad'
    old = s3.put(key, b'old upload')
    s3.put(key, b'NEW upload by the depositor')
    deleted, skipped = delete_consumed(s3, BUCKET, PREFIX, {key: old}, if_match=use_if_match)
    assert not deleted and key in s3.objects


@pytest.mark.parametrize('backend, expected', [(FakeS3, 'enforced'), (IgnoringIfMatchS3, 'ignored')])
def test_n2_selftest_result_and_cleanup(backend, expected):
    s3 = backend()
    assert if_match_selftest(s3, BUCKET, PREFIX) == expected
    assert not [key for key in s3.objects if '_selftest' in key]


def test_n2_selftest_unsupported_and_cached(tmp_path):
    s3 = FakeS3()
    s3.if_match_supported = False
    assert if_match_selftest(s3, BUCKET, PREFIX) == 'unsupported:NotImplemented'
    env = Env(tmp_path)
    env.s3.if_match_supported = False
    assert env.watcher.check_if_match() is False
    cache = json.loads((env.state / 'ifmatch_selftest.json').read_text())
    assert cache['result'] == 'unsupported:NotImplemented'
    env.s3.if_match_supported = True
    fresh = ingest_watch.Watcher(env.settings, env.db, env.s3, BUCKET, env.pipeline, env.state,
                                 log=lambda message: None, clock=lambda: env.clock_value)
    assert fresh.check_if_match() is False                                   # cached for a week
    assert any(event['state'] == 'selftest' for event in env.db.events('-'))


def test_n2_selftest_delete_is_restricted_to_its_scratch_key():
    s3 = FakeS3()
    s3.put(f'{PREFIX}G/a.h5ad', b'x')
    for key in (f'{PREFIX}G/a.h5ad', f'{PREFIX}_selftest/../G/a.h5ad', f'{PREFIX}_selftest/a/b', f'{PREFIX}_selftest/'):
        with pytest.raises(ValueError):
            ingest_pipeline._delete_selftest_object(s3, BUCKET, PREFIX, key)
    assert f'{PREFIX}G/a.h5ad' in s3.objects


# ── N3: legacy catalog objects ──────────────────────────────────────────────

def test_n3_multipart_etag_matches_a_manual_computation(tmp_path):
    path = tmp_path / 'blob'
    data = bytes(range(256)) * 40000                                         # about 10 MB
    path.write_bytes(data)
    part = 8 * 1024 * 1024
    digests = [hashlib.md5(data[start:start + part]).digest() for start in range(0, len(data), part)]
    expected = hashlib.md5(b''.join(digests)).hexdigest() + f'-{len(digests)}'
    assert multipart_etag(path, part) == expected
    assert etag_matches_file(expected, path) and not etag_matches_file('0' * 32 + '-2', path)
    assert etag_matches_file(hashlib.md5(data).hexdigest(), path)


def _legacy_env(tmp_path, body_transform=lambda body: body, etag=None):
    env = Env(tmp_path)
    _live_v1(env)
    store_h5ad = env.store / 'WatchTest2026' / 'micro.h5ad'
    key = f'{CATALOG}WatchTest2026/micro.h5ad'
    env.s3.put(key, body_transform(store_h5ad.read_bytes()))
    if etag:
        env.s3.objects[key]['etag'] = etag
    item = type('I', (), {'rel_dir': 'WatchTest2026'})()
    return env, store_h5ad, key, item


def test_n3_multipart_object_with_other_content_is_not_verified(tmp_path):
    env, store_h5ad, key, item = _legacy_env(tmp_path, lambda body: b'X' * len(body), etag='deadbeef' * 4 + '-3')
    outcome = ingest_pipeline.Outcome()
    assert env.pipeline._verify_catalog(item, store_h5ad, sha_of(store_h5ad), outcome) is False
    assert 'sha256' not in env.s3.objects[key]['metadata']                   # nothing backfilled


def test_n3_multipart_object_with_the_same_content_is_verified_and_backfilled(tmp_path):
    env, store_h5ad, key, item = _legacy_env(tmp_path)
    etag = multipart_etag(store_h5ad, 8 * 1024 * 1024)                       # a one-part multipart upload: md5(md5)-1
    assert etag == hashlib.md5(hashlib.md5(store_h5ad.read_bytes()).digest()).hexdigest() + '-1'
    env.s3.objects[key]['etag'] = etag
    outcome = ingest_pipeline.Outcome()
    sha = sha_of(store_h5ad)
    assert env.pipeline._verify_catalog(item, store_h5ad, sha, outcome) is True
    assert env.s3.objects[key]['metadata']['sha256'] == sha


def test_n3_failed_backfill_keeps_the_true_verdict(tmp_path):
    env, store_h5ad, key, item = _legacy_env(tmp_path)

    def too_large(**kwargs):
        raise S3Error('InvalidRequest')                                     # e.g. self-copy of an object > 5 GB

    env.s3.copy_object = too_large
    outcome = ingest_pipeline.Outcome()
    assert env.pipeline._verify_catalog(item, store_h5ad, sha_of(store_h5ad), outcome) is True   # MD5 ETag matched
    assert any('not backfilled' in warning for warning in outcome.warnings)


# ── N4: repush only for the published file ──────────────────────────────────

def test_n4_repush_of_a_changed_store_file_needs_a_decision(tmp_path):
    env = Env(tmp_path)
    env.s3.drop_metadata = True
    env.s3.corrupt_upload = True                                             # first push cannot be verified
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['catalog_verified'] == 0
    store_h5ad = env.store / 'WatchTest2026' / 'micro.h5ad'
    store_h5ad.write_bytes(store_h5ad.read_bytes() + b'tampered')
    env.watcher.poll()
    row = env.row(keys['h5ad'])
    assert row['state'] == 'needs_decision' and 'no longer the published file' in row['error']
    assert not env.s3.deleted


# ── N5: s3_* stamps of the deposit are dropped ──────────────────────────────

def test_n5_deposit_s3_stamps_never_reach_the_store(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'s3_url': 'https://evil.example/new', 's3_key': f'{PREFIX}x'})
    env.poll_twice()
    mus = (env.store / 'WatchTest2026' / 'micro.h5ad.mus').read_text()
    assert 'evil.example' not in mus and f's3_key={CATALOG}WatchTest2026/micro.h5ad' in mus
    keys = env.deposit(seed=0, with_seeds=False, sidecar_extra={'brand_new_key': 'x',
                                                                 's3_url': 'https://evil.example/x'})
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    mus = (env.store / 'WatchTest2026' / 'micro.h5ad.mus').read_text()
    assert 'evil.example' not in mus and 'brand_new_key=x' in mus


# ── N6: stage before release, rescan right after restore ───────────────────

def test_n6_release_comes_after_staging_and_rescan_after_restore(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    seen = {}
    original_release = env.api.release

    def release(db_id):
        seen['staged_at_release'] = sorted(path.name for path in group.glob('.micro.*.ingest-new'))
        return original_release(db_id)

    env.api.release = release
    env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert '.micro.md.ingest-new' in seen['staged_at_release'] and '.micro.h5ad.ingest-new' in seen['staged_at_release']
    calls = env.api.calls
    assert calls[calls.index('restore:WT6/MIC') + 1] == 'rescan'


# ── N7, N8: approvals ───────────────────────────────────────────────────────

def test_n7_a_changed_static_file_voids_the_approval(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': 'public'})
    env.poll_twice()
    ingest_watch.approve(env.db, keys['h5ad'], 'Mark')
    env.s3.put(keys['pdf'], b'%PDF-1.4 swapped after review', env.clock_value)
    env.watcher.poll()
    assert env.row(keys['h5ad'])['approval'] is None
    assert env.watcher.poll() == {'held': 1}


def test_n8_approval_records_the_os_user_and_the_cli_requires_primary(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': 'public'})
    env.poll_twice()
    approval = ingest_watch.approve(env.db, keys['h5ad'], 'Mark', 'licence checked')
    assert approval['os_user'] and approval['by'] == 'Mark'
    assert approval['os_user'] in env.db.events(keys['h5ad'])[-1]['detail']
    text = (ROOT / 'bin' / 'ingest' / 'ant-ingest-watch').read_text()
    review = text[text.index('def cmd_review'):text.index('def cmd_status')] if 'def cmd_status' in \
        text[text.index('def cmd_review'):] else text[text.index('def cmd_review'):]
    assert 'require_primary' in review


# ── N9: the store commit ────────────────────────────────────────────────────

def _git_env(tmp_path):
    env = Env(tmp_path)
    (env.store / '.git').mkdir()
    env.pipeline.config.store_git_commit = True
    return env


def test_n9_unchanged_deposit_never_commits(tmp_path):
    env = _git_env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    env.clock_value += 10
    commits = sum('commit' in cmd for cmd in env.runner.commands)
    assert env.poll_twice() == {'live': 1}
    assert sum('commit' in cmd for cmd in env.runner.commands) == commits


def test_n9_a_file_changed_after_the_route_wrote_it_is_not_committed(tmp_path):
    env = _git_env(tmp_path)
    original = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        if label == 'post:score':                     # a curator edits the .md while the post steps run
            md = env.store / 'WatchTest2026' / 'micro.md'
            md.write_text(md.read_text() + '\nEdited by hand.\n')
        return original(label, cmd, cwd, timeout)

    env.runner.run = run
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert not any(cmd[3:4] == ['add'] or 'commit' in cmd for cmd in env.runner.commands if cmd[0] == 'git')
    assert any('changed after the route wrote them' in warning
               for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_n9_staged_changes_of_the_curator_are_left_alone(tmp_path):
    env = _git_env(tmp_path)
    original = env.runner.run
    env.runner.run = lambda label, cmd, cwd=None, timeout=None: (0, 'WatchTest2026/micro.md') \
        if label == 'git diff --cached' else original(label, cmd, cwd, timeout)
    keys = env.deposit()
    env.poll_twice()
    git = [cmd for cmd in env.runner.commands if cmd[0] == 'git']
    assert not any('add' in cmd or 'reset' in cmd or 'commit' in cmd for cmd in git)
    assert any('already staged' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))


# ── N11, nits: hidden and partial files ─────────────────────────────────────

def test_n11_partial_files_are_removed_by_rollback_and_block_the_item(tmp_path):
    store = tmp_path / 'store' / 'G'
    store.mkdir(parents=True)
    journal = SwapJournal(tmp_path / 'swaps', 'x', store)
    source = tmp_path / 'f'
    source.write_text('new')
    journal.stage(source, store / 'd.md')
    partial = Path(journal.data['entries'][0]['partial'])
    partial.write_text('half a copy')                            # what a crash during a cross-device copy leaves
    journal.rollback()
    assert not partial.exists() and not list(store.iterdir())
    env = Env(tmp_path / 'e')
    group = env.store / 'WatchTest2026'
    group.mkdir()
    (group / '.micro.h5ad.ingest-partial').write_text('x')
    env.deposit()
    assert env.poll_twice() == {'needs_decision': 1}


def test_nit_hidden_check_matches_the_exact_basename(tmp_path):
    env = Env(tmp_path)
    group = env.store / 'WatchTest2026'
    group.mkdir()
    (group / '.micro2.h5ad.ingest-new').write_text('another dataset')
    env.deposit()
    assert env.poll_twice() == {'live': 1}


def test_nit_a_conflicting_seed_applies_its_caveats_but_not_the_curated_key(tmp_path):
    # 2.47.0: a curated key in conflict is not applied and the seed stays in incoming, but its new caveats are
    # applied (before, a conflict applied nothing at all)
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=1, with_seeds=False)
    seed = f'{PREFIX}WatchTest2026/static/micro.md'
    env.s3.put(seed, b'---\nid: XYZ\ncaveats:\n- message: SEED CAVEAT\n---\nbody\n', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    text = (env.store / 'WatchTest2026' / 'micro.md').read_text()
    assert 'SEED CAVEAT' in text and 'id: XYZ' not in text
    assert seed in env.s3.objects


# ── N12: tool failures that are not about the dataset ───────────────────────

@pytest.mark.parametrize('output', ['error: Failed to resolve dependencies for `anndata`',
                                    'Connection refused', 'error: Failed to download `numpy==2.0`',
                                    'Temporary failure in name resolution'])
def test_n12_dependency_and_network_failures_retry(tmp_path, output):
    assert transient_output(output)
    env = Env(tmp_path)
    original = env.runner.run
    env.runner.run = lambda label, cmd, cwd=None, timeout=None: (1, output) if label == 'ant-convert' \
        else original(label, cmd, cwd, timeout)
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    assert env.row(keys['h5ad'])['state'] == 'stable'


def test_n12_a_real_conversion_error_still_fails(tmp_path):
    assert not transient_output('ValueError: X contains raw counts')
    env = Env(tmp_path)
    env.runner.convert_ok = False
    env.deposit()
    assert env.poll_twice() == {'failed': 1}
