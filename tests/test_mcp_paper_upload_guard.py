"""ant-mcp's paper tools through ant-serve's cross-site guard (security review round 4, finding 1).

check_paper (always without a PDF) and upload_paper without pdf_base64 used to post a urlencoded form (httpx
``data=`` without ``files=``), which _CrossSiteGuard answers with 415. The tools are cut out of ``main()`` with
ast and run with the real httpx against an in-process app that carries the real guard and a /papers/upload route
like ant-serve's (multipart form fields)."""
from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, File, Form, UploadFile

from tests.test_cross_site_guard import _guard_namespace

MCP_PATH = Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-mcp'
NESTED = ('_post_upload', '_upload_report', 'check_paper', 'upload_paper')


def _app(seen: List[Dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post('/papers/upload')
    async def upload(metadata: str = Form(...), file: Optional[UploadFile] = File(None),
                     folder: Optional[str] = Form(None), dry_run: bool = Form(False), update: bool = Form(False)):
        pdf = await file.read() if file is not None else None
        seen.append({'metadata': metadata, 'pdf': pdf, 'dry_run': dry_run, 'folder': folder})
        return {'status': 'dry_run' if dry_run else 'created', 'citekey': 'doe2026x', 'folder': folder or 'reading',
                'warnings': []}
    app.add_middleware(_guard_namespace()['_CrossSiteGuard'])
    return app


def _tools(seen: List[Dict[str, Any]]) -> Dict[str, Any]:
    tree = ast.parse(MCP_PATH.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    body = []
    for node in ast.walk(main):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NESTED:
            node.decorator_list = []
            body.append(node)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(seen)))
    namespace: Dict[str, Any] = {
        'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional, 'Tuple': Tuple, 'json': json, 'httpx': httpx,
        'yaml': __import__('yaml'),
        'base64': base64, 'binascii': binascii, '_http_client': client, 'api_base_url': 'http://api',
        '_identity_headers': lambda: {'Remote-User': 'lib', 'Remote-Groups': 'librarians'},
        'UPLOADS_DISABLED_MESSAGE': 'off', '_get_anthive_config': lambda: {'mcp': {'max_upload_mb': 25}},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MCP_PATH), 'exec'), namespace)
    assert set(NESTED) <= set(namespace)
    return namespace


def test_check_paper_passes_the_guard_as_multipart():
    seen: List[Dict[str, Any]] = []
    tools = _tools(seen)
    answer = asyncio.run(tools['check_paper']({'doi': '10.1/x', 'title': 'A paper'}))
    assert 'Dry run' in answer, answer
    assert json.loads(seen[0]['metadata'])['doi'] == '10.1/x' and seen[0]['dry_run'] is True and seen[0]['pdf'] is None


def test_upload_paper_without_a_pdf_passes_the_guard():
    seen: List[Dict[str, Any]] = []
    tools = _tools(seen)
    answer = asyncio.run(tools['upload_paper']({'doi': '10.1/x'}, folder='lab'))
    assert 'Added doe2026x' in answer, answer
    assert seen[0]['pdf'] is None and seen[0]['folder'] == 'lab' and seen[0]['dry_run'] is False


def test_upload_paper_with_a_pdf_still_works():
    seen: List[Dict[str, Any]] = []
    tools = _tools(seen)
    pdf = b'%PDF-1.7 tiny'
    asyncio.run(tools['upload_paper']('---\ndoi: 10.1/x\n---\n', pdf_base64=base64.b64encode(pdf).decode()))
    assert seen[0]['pdf'] == pdf


def test_a_urlencoded_form_is_still_refused():
    """The guard stays strict: what the old code sent is a 415."""
    seen: List[Dict[str, Any]] = []

    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(seen))) as client:
            return await client.post('http://api/papers/upload', data={'metadata': '{}'})
    assert asyncio.run(post()).status_code == 415 and seen == []
