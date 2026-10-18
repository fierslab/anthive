"""The Caddy access log never writes the signature of a presigned S3 redirect.

ant-serve answers an h5ad download (``/databases/{id}/download/h5ad``) and a paper PDF
(``/papers/{citekey}/file``) with a 307 to a short-lived presigned S3 URL. Caddy logs
response headers under ``resp_headers``, so without a filter the full signed URL
(credential and signature) ends up in the access log. ``config/Caddyfile`` drops the
query of ``Location`` and keeps host and path.

Runs the real Caddyfile against a stub upstream that answers every request with such a
307; needs a ``caddy`` binary (``envs/caddy/caddy`` or one on PATH), skipped otherwise.
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SIGNED_PATH = 'https://s3.example/labbucket/G/x.h5ad'
SIGNED = (SIGNED_PATH + '?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDREAD%2F20261018'
          '&X-Amz-Expires=300&X-Amz-Signature=deadbeefSIGNATURE123')


def _caddy() -> Optional[str]:
    local = REPO_ROOT / 'envs' / 'caddy' / 'caddy'
    return str(local) if local.is_file() else shutil.which('caddy')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class _RedirectingApi(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(307)
        self.send_header('Location', SIGNED)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def test_caddy_access_log_drops_the_signature_of_download_redirects(tmp_path):
    caddy = _caddy()
    if caddy is None:
        pytest.skip('caddy not installed')
    upstream = http.server.HTTPServer(('127.0.0.1', 0), _RedirectingApi)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_port = str(upstream.server_address[1])
    port = _free_port()
    config = REPO_ROOT / 'config'
    env = dict(os.environ)
    env.update({
        'ANTHIVE_LISTEN': f'http://127.0.0.1:{port}', 'ANTHIVE_CADDY_BIND': '127.0.0.1',
        'ANTHIVE_TLS_SNIPPET': str(config / 'caddy-tls.prod'),
        'ANTHIVE_AUTH_SNIPPET': str(config / 'caddy-auth.none'),
        'ANTHIVE_SERVERS_SNIPPET': str(config / 'caddy-servers.dev'),
        'ANTHIVE_HTTP_REDIRECT_SNIPPET': str(config / 'caddy-http-redirect.prod'),
        'ANTHIVE_AUTO_HTTPS': 'auto_https off', 'ANTHIVE_WEBAPP_ROOT': str(tmp_path),
        'ANTHIVE_CADDY_LOG_OUTPUT': 'stderr', 'ANTHIVE_DEFAULT_SNI': '',
        'ANTHIVE_API_HOST': '127.0.0.1', 'ANTHIVE_API_PORT': upstream_port,
        'ANTHIVE_BROWSE_HOST': '127.0.0.1', 'ANTHIVE_BROWSE_PORT': upstream_port,
        'ANTHIVE_MCP_HOST': '127.0.0.1', 'ANTHIVE_MCP_PORT': upstream_port,
    })
    proc = subprocess.Popen([caddy, 'run', '--config', str(config / 'Caddyfile'), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    opener = urllib.request.build_opener(_NoRedirect)
    paths = ['/api/databases/G/x/download/h5ad', '/api/papers/Doe2024/file']
    locations = {}
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(locations) < len(paths):
            for path in paths:
                if path in locations:
                    continue
                try:
                    opener.open(f'http://127.0.0.1:{port}{path}', timeout=3)
                except urllib.error.HTTPError as error:
                    if error.code == 307:
                        locations[path] = error.headers['Location']
                except OSError:
                    if proc.poll() is not None:
                        pytest.fail(proc.stderr.read().decode()[-2000:])
                    time.sleep(0.3)
        time.sleep(0.5)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
        upstream.shutdown()
    assert locations == {path: SIGNED for path in paths}     # clients still get the whole URL
    log = (out + err).decode()
    for path in paths:
        assert path in log
    assert '"status": 307' in log
    assert 'SIGNATURE123' not in log and 'X-Amz-Signature' not in log and 'AKIDREAD' not in log
    assert log.count(SIGNED_PATH + '?REDACTED') >= len(paths)  # host and path kept
