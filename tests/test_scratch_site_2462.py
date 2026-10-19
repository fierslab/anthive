"""2.46.2: findings from the 2.46.1 scratch site.

A. ``ant-stack check`` refuses unknown keys in the security-relevant sections of site.yaml and warns
   about the rest, with "did you mean".
B. The MCP instructions never name the container listener (``http://0.0.0.0:8090/mcp``); container sites
   render ``mcp.mcp_url`` = public_url + /mcp.
C. ``/me/mcp`` without an identity no longer talks about the chat endpoint.
D. An invalid rendered config is one PROBLEM line, no traceback; ant-sync reports an unreachable endpoint
   in one line.
E. An empty store gives one "no datasets yet" line.
F. The deposit watcher lists with few, logged retries, and a second SIGTERM exits at once.
G. ``ant-stack watch`` prints the command it runs.
"""
from __future__ import annotations

import argparse
import ast
import importlib.machinery
import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def ant_stack():
    pytest.importorskip('bcrypt')
    loader = importlib.machinery.SourceFileLoader('ant_stack_2462', str(REPO / 'bin/ops/ant-stack'))
    spec = importlib.util.spec_from_loader('ant_stack_2462', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_site(tmp_path: Path, extra: dict | None = None) -> Path:
    data = tmp_path / 'store'
    data.mkdir(exist_ok=True)
    doc = {
        'site': {'name': 'unit', 'public_url': 'https://anthive.example.org'},
        'data': {'h5adstore': str(data)},
        'state_dir': str(tmp_path / 'state'),
        'auth': {'mode': 'authelia', 'admins': ['boss'], 'users': [{'name': 'boss', 'groups': ['admins']}]},
    }
    for key, value in (extra or {}).items():
        doc[key] = {**doc.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / 'site.yaml'
    path.write_text(yaml.safe_dump(doc))
    return path


def _lift(path: Path, names: set, namespace: dict) -> dict:
    """Module-level definitions of a uv script, without running its imports."""
    tree = ast.parse(path.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), namespace)
    assert names <= set(namespace), names - set(namespace)
    return namespace


# ── A. unknown site.yaml keys ────────────────────────────────────────────────

def test_typos_in_strict_sections_are_problems(tmp_path, ant_stack):
    path = _write_site(tmp_path, {'auth': {'internal_requires_auht': False, 'mdoe': 'none'},
                                  'tls': {'acme_emial': 'a@b.org'}, 'services': {'mpc': True},
                                  'data': {'h5adstroe': '/x'}})
    site = ant_stack.load_site(path)
    problems = site['_key_problems']
    assert "site.yaml: unknown key auth.internal_requires_auht (did you mean 'internal_requires_auth'?)" in problems
    assert "site.yaml: unknown key auth.mdoe (did you mean 'mode'?)" in problems
    assert any(p.startswith('site.yaml: unknown key tls.acme_emial') and "'acme_email'" in p for p in problems)
    assert any(p.startswith('site.yaml: unknown key services.mpc') and "'mcp'" in p for p in problems)
    assert any(p.startswith('site.yaml: unknown key data.h5adstroe') for p in problems)
    assert site['_key_warnings'] == []


def test_typos_elsewhere_are_warnings(tmp_path, ant_stack):
    path = _write_site(tmp_path, {'servcies': {'mcp': True}, 'resources': {'server_thread': 4},
                                  'library': {'upload_group': ['x']}})
    site = ant_stack.load_site(path)
    assert site['_key_problems'] == []
    warnings = site['_key_warnings']
    assert "site.yaml: unknown key servcies (did you mean 'services'?)" in warnings
    assert any(w.startswith('site.yaml: unknown key resources.server_thread') and "'server_threads'" in w
               for w in warnings)
    assert any(w.startswith('site.yaml: unknown key library.upload_group') for w in warnings)


def test_known_and_free_form_keys_pass(tmp_path, ant_stack):
    path = _write_site(tmp_path, {'site': {'docs_base_url': 'https://docs.example.org'},
                                  'auth': {'smtp': {'address': 'submission://smtp.example.org:587',
                                                    'username': 'u', 'sender': 's'}},
                                  'anthive': {'warmup': {'module_ranks': 'off'}},
                                  'ports': {'bind': '127.0.0.1', 'https': 8443}})
    site = ant_stack.load_site(path)
    assert site['_key_problems'] == [] and site['_key_warnings'] == []


def test_the_example_itself_is_clean(ant_stack):
    example = yaml.safe_load((REPO / 'site.example.yaml').read_text())
    assert ant_stack.site_key_findings(example, example) == ([], [])


def test_check_blocks_on_a_strict_typo(tmp_path, ant_stack, monkeypatch, capsys):
    path = _write_site(tmp_path, {'auth': {'mdoe': 'none'}, 'servcies': {}})
    site = ant_stack.load_site(path)
    monkeypatch.setattr(ant_stack.shutil, 'which', lambda name: None)
    problems = ant_stack.check(site, running=True)
    assert any('auth.mdoe' in p for p in problems)
    assert 'servcies' in capsys.readouterr().err


# ── B. MCP address in the instructions ───────────────────────────────────────

def test_container_config_names_the_public_mcp_url(tmp_path, ant_stack):
    site = ant_stack.load_site(_write_site(tmp_path))
    cfg = ant_stack.render_anthive_yaml(site, 'serve')
    assert cfg['mcp']['mcp_url'] == 'https://anthive.example.org/mcp'


def test_mcp_public_url_never_falls_back_to_the_listener():
    from typing import Optional
    namespace = _lift(REPO / 'bin/api/ant-mcp', {'_mcp_public_url'}, {'Optional': Optional})
    pick = namespace['_mcp_public_url']
    assert pick('https://a.example.org/mcp/', None) == 'https://a.example.org/mcp'
    assert pick(None, 'https://a.example.org/') == 'https://a.example.org/mcp'
    assert pick(None, None) is None and pick('', '  ') is None
    source = (REPO / 'bin/api/ant-mcp').read_text()
    assert "f'http://{args.host}:{args.port}/mcp').rstrip" not in source
    assert "if mcp_public_url else ''" in source


# ── C. /me/mcp wording ───────────────────────────────────────────────────────

def test_missing_identity_message_is_not_about_chat():
    source = (REPO / 'bin/api/ant-serve').read_text()
    assert 'chat endpoint requires authentication' not in source
    assert 'sign-in required: this request carries no user identity' in source


# ── D. rendered config errors, ant-sync without an endpoint ──────────────────

def test_invalid_render_is_one_problem_line(tmp_path, ant_stack, capsys):
    path = _write_site(tmp_path, {'anthive': {'no_such_key_anywhere': 1}})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    with pytest.raises(ant_stack.SiteError) as caught:
        ant_stack.render(site, quiet=True, apply=False)
    message = str(caught.value)
    assert 'no_such_key_anywhere' in message and 'Traceback' not in message and '\n' not in message
    capsys.readouterr()
    assert ant_stack.cmd_check(argparse.Namespace(site=str(path), running=True)) == 1
    out = capsys.readouterr()
    assert '  PROBLEM  rendered ' in out.out and 'Traceback' not in out.out + out.err


def test_ant_sync_unreachable_endpoint_is_one_line(tmp_path):
    pytest.importorskip('boto3')
    store = tmp_path / 'store'
    store.mkdir()
    config_path = tmp_path / 'anthive.yaml'
    config_path.write_text(yaml.safe_dump({'h5adstore': str(store), 's3': {
        'bucket': 'b', 'endpoint': '127.0.0.1:9', 'access_key_id': 'x', 'secret_access_key': 'y'}}))
    env = {**os.environ, 'ANTHIVE_CONFIG': str(config_path), 'AWS_MAX_ATTEMPTS': '1'}
    result = subprocess.run([sys.executable, str(REPO / 'bin/ops/ant-sync'), 'pull', '--dry-run', '--no-library'],
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 3, result.stderr
    assert 'Traceback' not in result.stderr
    assert 'Error: cannot reach the S3 endpoint 127.0.0.1:9' in result.stderr


# ── E. empty store ───────────────────────────────────────────────────────────

def test_empty_store_is_one_line(tmp_path, ant_stack, monkeypatch, capsys):
    site = ant_stack.load_site(_write_site(tmp_path))
    monkeypatch.setattr(ant_stack.shutil, 'which', lambda name: None)
    ant_stack.check(site, running=True)
    ant_stack._check_internal_access(site)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert text.count('no datasets yet') == 1 and 'has no dataset .md files' in text


# ── F. watcher retries and SIGTERM ───────────────────────────────────────────

def _watch_namespace(logged: list, exits: list) -> dict:
    class FakeOs:
        @staticmethod
        def _exit(code):
            exits.append(code)
    from typing import Optional
    namespace = {'log': logged.append, 'signal': signal, 'os': FakeOs, 'Optional': Optional, '_stop': False}
    return _lift(REPO / 'bin/ingest/ant-ingest-watch',
                 {'_handle_stop', '_log_s3_retry', 's3_client', 'LIST_RETRIES', 'LIST_TIMEOUTS', 'TRANSFER_RETRIES',
                  'TRANSFER_TIMEOUTS'}, namespace)


def test_second_sigterm_exits_at_once():
    logged, exits = [], []
    namespace = _watch_namespace(logged, exits)
    namespace['_handle_stop'](signal.SIGTERM, None)
    assert namespace['_stop'] is True and exits == []
    assert 'send it again to exit now' in logged[-1]
    namespace['_handle_stop'](signal.SIGTERM, None)
    assert exits == [128 + signal.SIGTERM] and 'second SIGTERM: exiting now' in logged[1]


def test_listing_client_retries_little_and_logs_each_attempt():
    pytest.importorskip('boto3')
    logged, exits = [], []
    namespace = _watch_namespace(logged, exits)
    s3_cfg = {'endpoint': '127.0.0.1:9', 'bucket': 'b', 'access_key_id': 'x', 'secret_access_key': 'y'}
    lister = namespace['s3_client'](s3_cfg, namespace['LIST_RETRIES'], namespace['LIST_TIMEOUTS'])
    assert lister.meta.config.retries['total_max_attempts'] == 4 and lister.meta.config.connect_timeout == 10
    transfer = namespace['s3_client'](s3_cfg)
    assert transfer.meta.config.retries['total_max_attempts'] == 21
    with pytest.raises(Exception):
        lister.list_objects_v2(Bucket='b', Prefix='__incoming/')
    attempts = [line for line in logged if line.startswith('S3 ListObjectsV2 attempt')]
    assert len(attempts) >= 2, logged
    assert 'EndpointConnectionError' in attempts[0]


def test_watcher_lists_with_the_listing_client(tmp_path):
    import ingest_watch

    class Lister:
        def get_paginator(self, name):
            assert name == 'list_objects_v2'
            return self

        def paginate(self, **kwargs):
            return [{'Contents': [{'Key': '__incoming/x'}]}]

    class NoListing:
        def get_paginator(self, name):
            raise AssertionError('the transfer client must not list')

    watcher = ingest_watch.Watcher({'prefix': '__incoming/'}, None, NoListing(), 'b', None, tmp_path,
                                   list_s3=Lister())
    assert watcher.list_prefix() == [{'Key': '__incoming/x'}]


# ── G. ant-stack watch prints its command ────────────────────────────────────

def test_watch_prints_the_command_before_exec(tmp_path, ant_stack, monkeypatch, capsys):
    path = _write_site(tmp_path, {'services': {'dataset_deposit': True},
                                  's3': {'enabled': True, 'endpoint': 's3.example.org', 'bucket': 'b'}})
    site = ant_stack.load_site(path)
    ant_stack.ensure_secrets(site)
    for name, value in (('s3_access_key_id', 'K'), ('s3_secret_access_key', 'S')):
        (site['_state'] / 'secrets' / name).write_text(value)
        os.chmod(site['_state'] / 'secrets' / name, 0o600)
    calls = []
    monkeypatch.setattr(ant_stack.os, 'execvpe', lambda tool, argv, env: calls.append(argv))
    ant_stack.cmd_watch(argparse.Namespace(site=str(path), cmd=['--', '--once']))
    err = capsys.readouterr().err
    assert calls and calls[0][-1] == '--once'
    assert '[ant-stack] exec ' in err and 'bin/ingest/ant-ingest-watch --once' in err
    assert 'ANTHIVE_CONFIG=' in err
