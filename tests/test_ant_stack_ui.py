"""site.yaml ``ui:`` switches in bin/ops/ant-stack (a public mirror without facets, caveats, API pages or MCP).

Render: the block reaches every rendered anthive*.yaml; ``ui.mcp: false`` drops the
mcp compose profile, switches personal links off and renders Caddy's /mcp snippet
(stack/caddy/mcp.caddy) from config/caddy-mcp.off. Validation: strict section, booleans only. Check: what is off
and the caveats warning. The Caddy test runs the real Caddyfile (skipped without a
``caddy`` binary) and proves /mcp answers a JSON 404 when MCP is off.
"""
from __future__ import annotations

import http.server
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from tests.test_ant_stack_hardening import (REPO, _caddy, _caddy_env, _free_port, _host_snippets,  # noqa: F401
                                            _site, ant_stack)

ALL_OFF = {'facets': False, 'caveats': False, 'api': False, 'mcp': False, 'jupyter': False}


def _rendered(stack, name='anthive.yaml'):
    return yaml.safe_load((stack / name).read_text())


def test_defaults_render_every_part_on(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path)
    assert ant_stack.ui_settings(site) == {'facets': True, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True}
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    for name in ('anthive.yaml', 'anthive-api.yaml'):
        rendered = _rendered(stack, name)
        assert rendered['ui'] == {'facets': True, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True}
        assert rendered['mcp']['enabled'] is True
    assert 'COMPOSE_PROFILES="mcp,authelia"' in (stack / '.env').read_text()
    compose = _rendered(stack, 'compose.site.yaml')
    assert compose['services']['caddy']['environment']['ANTHIVE_MCP_SNIPPET'] == f"{ant_stack.C_CADDY_SNIPPETS}/mcp.caddy"
    assert 'reverse_proxy' in (stack / 'caddy' / 'mcp.caddy').read_text()


def test_all_off_renders_a_public_mirror(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, auth={'mode': 'none'}, ui=ALL_OFF)
    assert site['_key_problems'] == []
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    for name in ('anthive.yaml', 'anthive-api.yaml', 'anthive-ingest.yaml'):
        rendered = _rendered(stack, name)
        assert rendered['ui'] == ALL_OFF, name
        assert rendered['mcp']['enabled'] is False, name
    env = (stack / '.env').read_text()
    assert 'COMPOSE_PROFILES=""' in env                    # neither mcp nor authelia
    mcp_snippet = (stack / 'caddy' / 'mcp.caddy').read_text()
    assert 'respond' in mcp_snippet and '404' in mcp_snippet and 'reverse_proxy' not in mcp_snippet
    assert not ant_stack.mcp_enabled(site)


def test_ui_mcp_off_wins_over_services_mcp(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, services={'mcp': True}, ui={'mcp': False})
    assert not ant_stack.mcp_enabled(site)
    ant_stack._check_ui(site)
    err = capsys.readouterr().err
    assert 'ui.mcp is false' in err and 'not started' in err


def test_services_mcp_off_still_routes_404(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, services={'mcp': False})
    assert 'respond' in ant_stack.render_caddy_mcp(site)
    assert 'reverse_proxy' in ant_stack.render_caddy_mcp(_site(ant_stack, tmp_path))


def test_check_warns_that_caveats_carry_data_quality_warnings(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, ui={'caveats': False, 'api': False})
    ant_stack._check_ui(site)
    out = capsys.readouterr()
    assert 'ui: switched off: caveats, api' in out.out
    assert 'data-quality warnings' in out.err
    assert '/api/docs' in out.out
    quiet = _site(ant_stack, tmp_path)
    ant_stack._check_ui(quiet)
    assert capsys.readouterr().out == ''


def test_plan_lists_the_parts_that_are_off(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, ui={'facets': False, 'mcp': False})
    ant_stack.print_plan(site)
    out = capsys.readouterr().out
    assert 'ui off         facets, mcp' in out
    assert ' mcp' not in out.split('services')[-1]


def test_plan_shows_no_admins_on_auth_mode_none(tmp_path, ant_stack, capsys):
    """2.46.5 scratch-site check: nobody signs in on auth.mode none, so the plan names no admins."""
    ant_stack.print_plan(_site(ant_stack, tmp_path, auth={'mode': 'none'}))
    out = capsys.readouterr().out
    assert 'admins:' not in out and 'no admins (nobody signs in on auth.mode none)' in out


@pytest.mark.parametrize('ui,message', [
    ({'facets': 'no'}, 'ui.facets must be true or false'),
    ({'mcp': 0}, 'ui.mcp must be true or false'),
])
def test_ui_values_must_be_booleans(tmp_path, ant_stack, ui, message):
    with pytest.raises(ant_stack.SiteError, match=message):
        _site(ant_stack, tmp_path, ui=ui)


def test_ui_is_a_strict_section(tmp_path, ant_stack):
    site = _site(ant_stack, tmp_path, ui={'facet': False})
    assert any('unknown key ui.facet' in problem and "did you mean 'facets'" in problem
               for problem in site['_key_problems'])


def test_rendered_ui_block_is_validated(tmp_path, ant_stack):
    """The escape hatch cannot smuggle a misspelt ui key past the strict render check."""
    site = _site(ant_stack, tmp_path, anthive={'ui': {'facet': False}})
    ant_stack.ensure_secrets(site)
    with pytest.raises(ant_stack.SiteError, match='invalid'):
        ant_stack.render(site, quiet=True)


def test_site_example_documents_every_ui_key():
    example = yaml.safe_load((REPO / 'site.example.yaml').read_text())
    assert example['ui'] == {'facets': True, 'caveats': True, 'api': True, 'mcp': True, 'jupyter': True}
    text = (REPO / 'site.example.yaml').read_text()
    assert 'data-quality warnings' in text
    default = yaml.safe_load((REPO / 'anthive.default.yaml').read_text())
    assert default['ui'] == example['ui']


class _Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header('Content-Length', '8')
        self.end_headers()
        self.wfile.write(b'upstream')

    def log_message(self, *args):
        pass


@pytest.mark.parametrize('mcp_on', [True, False])
def test_caddy_routes_mcp_only_when_on(tmp_path, ant_stack, mcp_on):
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    site = _site(ant_stack, tmp_path, auth={'mode': 'none'}, tls={'mode': 'off'}, ports={'bind': '127.0.0.1'},
                 ui={'mcp': mcp_on})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    _host_snippets(ant_stack, stack)
    server = http.server.HTTPServer(('127.0.0.1', 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = _free_port()
    env = _caddy_env(ant_stack, site, stack, port, server.server_port, server.server_port)
    snippet = _rendered(stack, 'compose.site.yaml')['services']['caddy']['environment']['ANTHIVE_MCP_SNIPPET']
    env['ANTHIVE_MCP_SNIPPET'] = snippet.replace(ant_stack.C_CADDY_SNIPPETS, str(stack / 'caddy'))
    proc = subprocess.Popen([caddy, 'run', '--config', str(REPO / 'config' / 'Caddyfile'), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        status = body = None
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp/u/amcp_test",
                                                 headers={'Host': 'anthive.example.org'})
                with urllib.request.urlopen(request, timeout=3) as response:
                    status, body = response.status, response.read()
                break
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-2000:])
                time.sleep(0.3)
        if mcp_on:
            assert (status, body) == (200, b'upstream')
        else:
            assert status == 404
            assert json.loads(body)['error'] == 'not_found'
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


@pytest.mark.parametrize('jupyter_on', [True, False])
def test_caddy_serves_jupyter_only_when_on(tmp_path, ant_stack, jupyter_on, capsys):
    """ui.jupyter false: /jupyter answers 404 (no redirect into the build), check says so, nothing links to it."""
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    site = _site(ant_stack, tmp_path, auth={'mode': 'none'}, tls={'mode': 'off'}, ports={'bind': '127.0.0.1'},
                 ui={'jupyter': jupyter_on})
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)
    assert _rendered(stack)['ui']['jupyter'] is jupyter_on
    _host_snippets(ant_stack, stack)
    server = http.server.HTTPServer(('127.0.0.1', 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = _free_port()
    env = _caddy_env(ant_stack, site, stack, port, server.server_port, server.server_port)
    environment = _rendered(stack, 'compose.site.yaml')['services']['caddy']['environment']
    for name in ('ANTHIVE_MCP_SNIPPET', 'ANTHIVE_JUPYTER_SNIPPET'):
        env[name] = environment[name].replace(ant_stack.C_CADDY_SNIPPETS, str(stack / 'caddy'))
    proc = subprocess.Popen([caddy, 'run', '--config', str(REPO / 'config' / 'Caddyfile'), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        deadline = time.monotonic() + 15
        status = None
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/jupyter",
                                                 headers={'Host': 'anthive.example.org'})
                with opener.open(request, timeout=3) as response:
                    status = response.status
                break
            except urllib.error.HTTPError as error:
                status = error.code
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-2000:])
                time.sleep(0.3)
        assert status == (301 if jupyter_on else 404)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.shutdown()
    capsys.readouterr()
    ant_stack._check_ui(site)
    assert ('ui.jupyter is false' in capsys.readouterr().out) is (not jupyter_on)


def test_ui_caveats_serious_is_rendered_and_checked(tmp_path, ant_stack, capsys):
    site = _site(ant_stack, tmp_path, auth={'mode': 'none'}, ui={'caveats': 'serious'})
    assert site['_key_problems'] == [] and ant_stack.caveats_mode(site) == 'serious'
    ant_stack.ensure_secrets(site)
    stack = ant_stack.render(site, quiet=True)                      # the strict render check accepts it
    assert _rendered(stack)['ui']['caveats'] == 'serious'
    capsys.readouterr()
    ant_stack._check_ui(site)
    assert 'ui.caveats is serious' in capsys.readouterr().out
    with pytest.raises(ant_stack.SiteError, match='true or false or serious'):
        _site(ant_stack, tmp_path, ui={'caveats': 'some'})
