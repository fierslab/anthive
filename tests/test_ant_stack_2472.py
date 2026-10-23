"""ant-stack 2.47.2: a central-watcher site deposits with only the ingestion key pair (s3.enabled false), the
artefact check on such a site asks for that pair, and `check` / `pull` warn about docker disk space and list old
image tags.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ant_stack_artefacts import S3_ON, _configs, _keys, _site, _write_site, ant_stack  # noqa: F401

CENTRAL = {'ingestion_bucket': 'anthive-ingest', 'watcher_public_keys': ['ed25519:' + 'A' * 43 + '='],
           'key_id': 'k2'}
INGEST_PAIR = dict(s3_ingest_access_key_id='ING-KEY-ID', s3_ingest_secret_access_key='ING-SECRET')


def _central_site(ant_stack, tmp_path, *, s3=None, ingest_pair=True, read_pair=False, **overrides):
    """A central-watcher site; only the ingestion pair (and optionally the artefact read pair) is in secrets/."""
    site = ant_stack.load_site(_write_site(
        tmp_path, s3=s3 if s3 is not None else {'enabled': False},
        datasets={'site_id': 'intern', 'central_watcher': CENTRAL}, **overrides))
    ant_stack.ensure_secrets(site)
    if ingest_pair:
        _keys(site['_state'], **INGEST_PAIR)
    if read_pair:
        _keys(site['_state'], s3_artefacts_read_access_key_id='AR-KEY-ID',
              s3_artefacts_read_secret_access_key='AR-SECRET')
    return site


def _output(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# ── bug 1: deposits without s3.enabled on a central site ────────────────────────────────────────

def test_central_site_without_s3_renders_deposits_with_the_ingestion_pair(tmp_path, ant_stack):
    site = _central_site(ant_stack, tmp_path, services={'dataset_deposit': True}, artefacts={'enabled': True})
    stack, configs = _configs(ant_stack, site)
    api = configs['api']
    assert api['datasets_deposit_enabled'] is True and api['artefacts_enabled'] is True
    assert api['datasets_deposit_central_watcher'] is True
    assert api['datasets_deposit_s3'] == {
        'endpoint': None, 'region': 'us-east-1', 'bucket': 'anthive-ingest', 'use_path_style': False,
        'access_key_id': 'ING-KEY-ID', 'secret_access_key': 'ING-SECRET'}
    assert 's3' not in api                              # the data-bucket pair never reaches the api
    for role, config in configs.items():
        assert 's3' not in config, role
    assert '127.0.0.1:8079:8079' in yaml.safe_load((stack / 'compose.site.yaml').read_text())[
        'services']['caddy']['ports']


def test_central_site_without_s3_passes_check_with_only_the_ingestion_pair(tmp_path, ant_stack, capsys):
    site = _central_site(ant_stack, tmp_path, services={'dataset_deposit': True}, artefacts={'enabled': True})
    assert ant_stack._check_s3(site) == []
    assert ant_stack._check_datasets(site) == []
    assert ant_stack._check_artefacts(site) == []
    text = _output(capsys)
    assert 's3.enabled is false, which is fine here' in text
    assert 'neither secrets/s3_incoming' not in text and 'read-write pair' not in text


def test_central_site_without_the_ingestion_pair_is_a_problem(tmp_path, ant_stack):
    site = _central_site(ant_stack, tmp_path, ingest_pair=False, services={'dataset_deposit': True})
    problems = ant_stack._check_datasets(site)
    assert len(problems) == 1 and 's3_ingest_access_key_id' in problems[0]


def test_central_site_with_s3_enabled_keeps_its_behaviour(tmp_path, ant_stack):
    site = _central_site(ant_stack, tmp_path, s3=S3_ON, services={'dataset_deposit': True})
    _stack, configs = _configs(ant_stack, site)
    assert configs['api']['datasets_deposit_s3']['access_key_id'] == 'ING-KEY-ID'
    problems = ant_stack._check_s3(site)               # s3.enabled true still needs the read-write pair
    assert problems and all('s3_access_key_id' in problem or 's3_secret_access_key' in problem
                            for problem in problems)


def test_non_central_site_without_s3_is_unchanged(tmp_path, ant_stack):
    site = ant_stack.load_site(_write_site(tmp_path, services={'dataset_deposit': True},
                                           artefacts={'enabled': True}))
    ant_stack.ensure_secrets(site)
    _stack, configs = _configs(ant_stack, site)
    assert configs['api']['datasets_deposit_enabled'] is False and configs['api']['artefacts_enabled'] is False
    assert 'watcher' not in configs
    assert any('s3.enabled is false' in problem for problem in ant_stack._check_datasets(site))
    assert any('s3.enabled is false' in problem for problem in ant_stack._check_artefacts(site))


def test_central_site_without_s3_signs_artefact_reads_only_with_a_read_pair_and_a_bucket(tmp_path, ant_stack):
    for name in ('a', 'b', 'c'):
        (tmp_path / name).mkdir()
    no_bucket = _central_site(ant_stack, tmp_path / 'a', artefacts={'enabled': True}, read_pair=True)
    assert 'artefacts_s3' not in _configs(ant_stack, no_bucket)[1]['api']
    bucket_only = _central_site(ant_stack, tmp_path / 'b', s3={'enabled': False, 'bucket': 'data'},
                                artefacts={'enabled': True})
    assert 'artefacts_s3' not in _configs(ant_stack, bucket_only)[1]['api']
    both = _central_site(ant_stack, tmp_path / 'c', s3={'enabled': False, 'bucket': 'data'},
                         artefacts={'enabled': True}, read_pair=True)
    api = _configs(ant_stack, both)[1]['api']
    assert api['artefacts_s3']['access_key_id'] == 'AR-KEY-ID' and api['artefacts_s3']['bucket'] == 'data'
    assert api['datasets_deposit_s3']['bucket'] == 'anthive-ingest'


# ── bug 2: the artefact check on a central site ─────────────────────────────────────────────────

def test_artefact_check_on_a_central_site_uses_the_ingestion_pair(tmp_path, ant_stack, capsys):
    site = _central_site(ant_stack, tmp_path, s3=S3_ON, artefacts={'enabled': True})
    _keys(site['_state'], s3_access_key_id='RW-KEY-ID', s3_secret_access_key='RW-SECRET')
    assert ant_stack._check_artefacts(site) == []     # no s3_incoming_* needed, the read-write pair is not used
    text = _output(capsys)
    assert 's3://anthive-ingest/__deposits/intern/' in text and 'never into the data bucket' in text
    assert 'the central watcher publishes them' in text


def test_artefact_only_central_site_without_the_ingestion_pair_names_it(tmp_path, ant_stack):
    site = _central_site(ant_stack, tmp_path, ingest_pair=False, artefacts={'enabled': True})
    problems = ant_stack._check_artefacts(site)
    assert len(problems) == 1
    assert 's3_ingest_access_key_id' in problems[0] and 's3_ingest_secret_access_key' in problems[0]
    assert 'neither secrets/s3_incoming' not in problems[0]


def test_artefact_only_central_site_needs_the_watcher_keys_and_the_site_secret(tmp_path, ant_stack):
    central = {**CENTRAL, 'watcher_public_keys': []}
    site = ant_stack.load_site(_write_site(tmp_path, s3={'enabled': False}, artefacts={'enabled': True},
                                           datasets={'site_id': 'intern', 'central_watcher': central}))
    _keys(site['_state'], **INGEST_PAIR)               # ensure_secrets not run: no site secret yet
    problems = ant_stack._check_artefacts(site)
    assert any('watcher_public_keys is empty' in problem for problem in problems)
    assert any('deposit_site_secret' in problem for problem in problems)


def test_central_artefact_uploads_are_presigned_into_the_ingestion_bucket(tmp_path, ant_stack, monkeypatch):
    """The rendered api config, read the way ant-serve reads it, sends artefact uploads to the ingestion bucket
    under __deposits/<site_id>/ and never to the data bucket."""
    import config  # noqa: PLC0415

    monkeypatch.setenv('ANTHIVE_DEPOSIT_SITE_SECRET', 'x' * 64)

    site = _central_site(ant_stack, tmp_path, s3={'enabled': False, 'bucket': 'data-bucket'},
                         services={'dataset_deposit': True}, artefacts={'enabled': True})
    cfg = _configs(ant_stack, site)[1]['api']
    settings = config.get_dataset_deposit_settings(cfg)
    assert settings['bucket'] == 'anthive-ingest' and settings['s3']['bucket'] == 'anthive-ingest'
    assert settings['staging_prefix'] == '__deposits/intern/'
    assert settings['credential_source'] == 'datasets_deposit_s3'
    assert settings['central_watcher'] is True and settings['s3']['access_key_id'] == 'ING-KEY-ID'
    assert config.get_artefact_settings(cfg)['artefacts_enabled'] is True


# ── bug 4: docker disk space and old image tags ─────────────────────────────────────────────────

def _fake_docker(monkeypatch, ant_stack, *, root='/var/lib/docker', free=100 * 10 ** 9, present=()):
    monkeypatch.setattr(ant_stack, 'docker_root_dir', lambda: Path(root))
    monkeypatch.setattr(ant_stack, 'docker_free_bytes', lambda path: free)
    monkeypatch.setattr(ant_stack, '_image_id', lambda ref: 'sha256:' + 'a' * 64 if ref in present else None)


def test_image_space_warning_when_free_space_is_below_twice_the_images(tmp_path, ant_stack, monkeypatch):
    site = ant_stack.load_site(_write_site(tmp_path, s3=S3_ON))
    _fake_docker(monkeypatch, ant_stack, free=12 * 10 ** 9)
    text = ant_stack.image_space_warning(site)
    assert text and '/var/lib/docker has 12.0 GB free' in text and 'needs about 12.6 GB' in text
    assert 'app and ingest' in text


@pytest.mark.parametrize('free', [12_600_000_000, 50 * 10 ** 9])
def test_image_space_warning_is_silent_with_room(tmp_path, ant_stack, monkeypatch, free):
    site = ant_stack.load_site(_write_site(tmp_path, s3=S3_ON))
    _fake_docker(monkeypatch, ant_stack, free=free)
    assert ant_stack.image_space_warning(site) is None


def test_image_space_warning_counts_only_what_is_missing_for_check(tmp_path, ant_stack, monkeypatch):
    site = ant_stack.load_site(_write_site(tmp_path, s3=S3_ON))
    both = [ant_stack.image_ref(site, 'app'), ant_stack.image_ref(site, 'ingest')]
    _fake_docker(monkeypatch, ant_stack, free=1, present=both)
    assert ant_stack.image_space_warning(site, only_missing=True) is None   # everything is pulled already
    assert ant_stack.image_space_warning(site) is not None                  # `pull` counts all of them
    _fake_docker(monkeypatch, ant_stack, free=6 * 10 ** 9, present=both[:1])
    text = ant_stack.image_space_warning(site, only_missing=True)
    assert text and 'pulling ingest needs about 6.6 GB' in text


def test_image_space_warning_without_ingest_and_with_unknown_free_space(tmp_path, ant_stack, monkeypatch):
    site = ant_stack.load_site(_write_site(tmp_path, s3=S3_ON))
    _fake_docker(monkeypatch, ant_stack, free=7 * 10 ** 9)
    assert ant_stack.image_space_warning(site, include_ingest=False) is None     # 2 x 3.0 GB = 6.0 GB
    monkeypatch.setattr(ant_stack, 'docker_free_bytes', lambda path: None)
    assert ant_stack.image_space_warning(site) is None


def test_docker_root_dir_asks_docker_and_falls_back(ant_stack, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='/data/docker\n', stderr='')

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    assert ant_stack.docker_root_dir() == Path('/data/docker')
    assert calls[0] == ['docker', 'info', '--format', '{{.DockerRootDir}}']
    monkeypatch.setattr(ant_stack.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout='', stderr='x'))
    assert ant_stack.docker_root_dir() == Path('/var/lib/docker')

    def missing(command, **kwargs):
        raise FileNotFoundError('docker')

    monkeypatch.setattr(ant_stack.subprocess, 'run', missing)
    assert ant_stack.docker_root_dir() == Path('/var/lib/docker')


def test_docker_free_bytes_uses_the_nearest_readable_parent(ant_stack, tmp_path):
    assert ant_stack.docker_free_bytes(tmp_path / 'no' / 'such' / 'dir') > 0
    assert ant_stack.docker_free_bytes(tmp_path) > 0


def test_old_image_tags_lists_only_tags_the_site_does_not_name(tmp_path, ant_stack, monkeypatch):
    site = ant_stack.load_site(_write_site(tmp_path, s3=S3_ON, images={'tag': '2.47.2'}))
    app, ingest = ant_stack.image_ref(site, 'app'), ant_stack.image_ref(site, 'ingest')
    listing = {app.rsplit(':', 1)[0]: [app, app.replace(':2.47.2', ':2.47.1'), app.replace(':2.47.2', ':<none>')],
               ingest.rsplit(':', 1)[0]: [ingest, ingest.replace(':2.47.2', ':2.46.5')]}

    def fake_run(command, **kwargs):
        lines = [f'{ref}\t3.1GB' for ref in listing[command[3]]]
        return subprocess.CompletedProcess(command, 0, stdout='\n'.join(lines) + '\n', stderr='')

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    found = ant_stack.old_image_tags(site)
    assert [ref.rsplit(':', 1)[1] for ref, _size in found] == ['2.47.1', '2.46.5']
    assert {size for _ref, size in found} == {'3.1GB'}
    assert [ref.rsplit(':', 1)[1] for ref, _size in ant_stack.old_image_tags(site, include_ingest=False)] == ['2.47.1']


def test_pull_warns_lists_old_tags_and_removes_nothing(tmp_path, ant_stack, monkeypatch, capsys):
    site_path = _write_site(tmp_path, s3=S3_ON)
    _fake_docker(monkeypatch, ant_stack, free=5 * 10 ** 9)
    commands: list = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    monkeypatch.setattr(ant_stack, 'old_image_tags', lambda site, include_ingest=True: [('reg/app:2.46.5', '3GB')])
    monkeypatch.setattr(ant_stack, '_image_id', lambda ref: 'sha256:' + 'b' * 64)
    status = ant_stack.cmd_pull(argparse.Namespace(site=str(site_path), no_ingest=False))
    text = _output(capsys)
    assert status == 0
    assert 'has 5.0 GB free' in text
    assert 'reg/app:2.46.5  3GB' in text and 'docker rmi reg/app:2.46.5' in text
    assert not any(command[:2] == ['docker', 'rmi'] or command[:3] == ['docker', 'image', 'rm']
                   for command in commands)


def test_pull_stays_quiet_with_room_and_no_old_tags(tmp_path, ant_stack, monkeypatch, capsys):
    site_path = _write_site(tmp_path, s3=S3_ON)
    _fake_docker(monkeypatch, ant_stack, free=500 * 10 ** 9)
    monkeypatch.setattr(ant_stack.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout='', stderr=''))
    monkeypatch.setattr(ant_stack, 'old_image_tags', lambda site, include_ingest=True: [])
    monkeypatch.setattr(ant_stack, '_image_id', lambda ref: 'sha256:' + 'b' * 64)
    assert ant_stack.cmd_pull(argparse.Namespace(site=str(site_path), no_ingest=True)) == 0
    text = _output(capsys)
    assert 'free;' not in text and 'docker rmi' not in text


def test_check_warns_about_docker_space_without_blocking(tmp_path, ant_stack, monkeypatch, capsys):
    site = _site(ant_stack, tmp_path, s3=S3_ON)
    _fake_docker(monkeypatch, ant_stack, free=1 * 10 ** 9)
    monkeypatch.setattr(ant_stack.shutil, 'which', lambda name, **kwargs: '/usr/bin/' + name)
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[:2] == ['docker', 'compose']:
            return subprocess.CompletedProcess(command, 0, stdout='2.29\n', stderr='')
        return real_run(command, **kwargs)

    monkeypatch.setattr(ant_stack.subprocess, 'run', fake_run)
    problems = ant_stack.check(site)
    assert 'has 1.0 GB free' in _output(capsys)
    assert not any('docker root' in problem for problem in problems)
