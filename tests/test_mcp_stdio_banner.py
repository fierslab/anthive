"""ant-mcp with ``--transport stdio`` writes nothing but JSON-RPC to stdout.

The startup banner went to stdout, which carries the protocol stream; with unbuffered
output (``PYTHONUNBUFFERED=1``, common in client configs and containers) the client read
five non-JSON lines before the ``initialize`` answer. The banner belongs on stderr.
Runs the real script (``uv run --script``); skipped when uv is not installed.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MCP_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-mcp'


@pytest.mark.skipif(shutil.which('uv') is None, reason='uv is needed to run the ant-mcp script')
def test_stdio_stdout_carries_only_json_rpc():
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
               'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                          'clientInfo': {'name': 'test', 'version': '0'}}}
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    proc = subprocess.run(
        [str(MCP_PATH), '--transport', 'stdio', '--api-url', 'http://127.0.0.1:9'],
        input=json.dumps(request) + '\n', capture_output=True, text=True, timeout=300, env=env)
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert stdout_lines, f'no answer on stdout; stderr: {proc.stderr[-2000:]}'
    answers = [json.loads(line) for line in stdout_lines]  # raises on any non-JSON line
    assert answers[0]['id'] == 1 and answers[0]['result']['serverInfo']['name'] == 'anthive'
    assert 'Anthive MCP server' in proc.stderr
