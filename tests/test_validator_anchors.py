"""Validation regexes reject a trailing newline, carriage return, NUL and path traversal.

Python's ``$`` also matches just before a trailing ``\\n``, so ``re.match(r'^abc$', 'abc\\n')``
succeeds. Every validator that guards untrusted input (S3 key segments, file and group names,
db ids, deposit ids, citekeys, tokens, usernames, layer keys, SQL identifiers) must be applied
with ``fullmatch``. Each validator is exercised through the function that uses it where one
exists; a source scan keeps ``<VALIDATOR>.match(`` from coming back.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import anthive_db
import dataset_deposit as dd
import facet_lint
import ingest_md_fill
import ingest_pipeline
import ingest_versions
import library
from mcp_tokens import McpLinkStore, looks_like_token

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Suffixes that a ``$``-anchored ``re.match`` lets through or that must never be part of a name.
BAD_SUFFIXES = ['\n', '\r', '\r\n', '\x00', '\n\n']


def _variants(good: str):
    return [good + suffix for suffix in BAD_SUFFIXES]


def _lift(path: Path, names: set) -> dict:
    """Module-level ``NAME = <re>.compile(...)`` assignments of a script that cannot be imported."""
    tree = ast.parse(path.read_text())
    body = [node for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)]
    namespace = {'re': re, '_re_notebook_filename': re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), namespace)
    assert names <= set(namespace), names - set(namespace)
    return namespace


# ── S3 key segments (ingest) ─────────────────────────────────────────────────

@pytest.mark.parametrize('bad', _variants('Smith2024') + ['..', '.', '../x', 'a/b', '_x', '.hidden', '', 'a b'])
def test_valid_segment_rejects(bad):
    assert ingest_pipeline.valid_segment('Smith2024')
    assert not ingest_pipeline.valid_segment(bad)


# ── dataset deposits ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('regex, good', [
    (dd.NEW_GROUP_RE, 'Smith2024'),
    (dd.BASENAME_RE, 'microglia_v1'),
    (dd.SHA256_RE, 'a' * 64),
    (dd.DEPOSIT_ID_RE, 'dep_' + '0' * 20),
    (dd.DB_ID_RE, 'AL1/CBL'),
    (dd.USER_RE, 'alice'),
])
def test_deposit_validators_reject_trailing_bytes(regex, good):
    assert regex.fullmatch(good)
    for bad in _variants(good):
        assert regex.fullmatch(bad) is None, repr(bad)


def test_deposit_group_rejects_unicode_digits():
    assert dd.NEW_GROUP_RE.fullmatch('Smith٢٠٢٤') is None   # Arabic-Indic digits


@pytest.mark.parametrize('bad', ['../x', 'AL1/..', '../..', 'AL1/../CBL', './x', 'AL1/CBL/x', 'AL1\\CBL', '/AL1/CBL',
                                 'AL1/', '/CBL', 'AL1/C.L', 'AL1/C_L', 'AL1/C-L', 'A' * 17 + '/CBL', 'AL1/CBL\n'])
def test_db_id_is_group_id_slash_dataset_id(bad):
    assert dd.DB_ID_RE.fullmatch(bad) is None
    for good in ('AL1/CBL', 'Si5/MIC', 'OL2/mic', 'NW6/cells', 'WT6/MIC'):
        assert dd.DB_ID_RE.fullmatch(good)


@pytest.mark.parametrize('name', ['index', 'INDEX', 'x.index', 'a.b.index'])
def test_reserved_basenames(name):
    assert dd.RESERVED_BASENAME_RE.fullmatch(name)
    assert dd.RESERVED_BASENAME_RE.fullmatch(name + 'x') is None


# ── personal MCP link tokens ─────────────────────────────────────────────────

def test_mcp_token_with_trailing_bytes_does_not_verify(tmp_path):
    store = McpLinkStore(tmp_path / 'links.db', 'server-secret', ttl_days=90)
    try:
        token = store.token_for(store.rotate('alice', ['users']))
        assert looks_like_token(token) and store.verify(token) is not None
        for bad in _variants(token):
            assert not looks_like_token(bad), repr(bad)
            assert store.verify(bad) is None, repr(bad)
    finally:
        store.close()


# ── paper library citekeys ───────────────────────────────────────────────────

@pytest.mark.parametrize('bad', _variants('smith2024') + ['../smith2024', 'smith/2024', 'Smith2024'])
def test_citekey_validation(bad):
    paper = library.Paper(citekey=bad, fields={'entrytype': 'article', 'title': 't', 'year': 2024}, body='',
                          path=None)
    errors = library.validate_paper(paper).errors
    assert any('citekey' in error for error in errors), (bad, errors)
    good = library.Paper(citekey='smith2024', fields={'entrytype': 'article', 'title': 't', 'year': 2024}, body='',
                         path=None)
    assert not any('citekey' in error for error in library.validate_paper(good).errors)


# ── depositor identity for git commits ───────────────────────────────────────

@pytest.mark.parametrize('field, good', [('depositor_email', 'a@b.org'), ('depositor', 'Ann Lee')])
def test_depositor_identity_rejects_trailing_bytes(field, good):
    sidecar = {'depositor': 'Ann Lee', 'depositor_email': 'a@b.org'}
    assert ingest_md_fill.depositor_of(sidecar) == 'Ann Lee <a@b.org>'
    for bad in ['\x00', '\n', '\r']:                     # str.strip() removes surrounding \n and \r, NUL stays
        assert ingest_md_fill.depositor_of({**sidecar, field: good + bad + 'x'}) is None
        assert ingest_md_fill._EMAIL_RE.fullmatch('a@b.org' + bad) is None
        assert ingest_md_fill._NAME_RE.fullmatch('Ann Lee' + bad) is None


# ── names, versions, layer keys, SQL identifiers ─────────────────────────────

@pytest.mark.parametrize('bad', _variants('cells.h5ad'))
def test_h5ad_filename_rejects(bad):
    h5ad_utils = pytest.importorskip('h5ad_utils', exc_type=ImportError)
    assert h5ad_utils.validate_filename('cells.h5ad')[0]
    assert not h5ad_utils.validate_filename(bad)[0]


@pytest.mark.parametrize('bad', _variants('1.2.3') + ['1.2.٣'])
def test_version_key(bad):
    assert ingest_versions.version_key('1.2.3') == (1, 2, 3)
    assert ingest_versions.version_key(bad) == (-1, -1, -1)


@pytest.mark.parametrize('bad', _variants('counts') + ['../counts', 'a/b'])
def test_layer_key(bad, tmp_path):
    assert anthive_db._LAYER_KEY_RE.fullmatch('counts')
    assert anthive_db._LAYER_KEY_RE.fullmatch(bad) is None
    assert anthive_db.layer_sidecar_path(tmp_path / 'x.duckdb', bad, 'by_gene') is None


@pytest.mark.parametrize('bad', _variants('cell_type'))
def test_safe_ident(bad):
    assert anthive_db.safe_ident('cell_type') == '"cell_type"'
    with pytest.raises(ValueError):
        anthive_db.safe_ident(bad)
    assert anthive_db._SAFE_IDENT_RE.fullmatch(bad) is None


@pytest.mark.parametrize('bad', _variants('x.parquet') + ['../x.parquet', "x'.parquet"])
def test_sidecar_basename(bad):
    assert anthive_db._SIDECAR_BASENAME_RE.fullmatch('x.parquet')
    assert anthive_db._SIDECAR_BASENAME_RE.fullmatch(bad) is None


@pytest.mark.parametrize('bad', _variants('cohort-a'))
def test_donor_group_slug(bad):
    assert facet_lint._DONOR_GROUP_RE.fullmatch('cohort-a')
    assert facet_lint._DONOR_GROUP_RE.fullmatch(bad) is None


@pytest.mark.parametrize('regex_name, good', [('_SPDX_LIKE', 'CC-BY-4.0'), ('_NAME_PART', 'Smith2024')])
def test_ingest_check_names(regex_name, good):
    import ingest_check.checks_access as access
    import ingest_check.checks_files as files
    regex = getattr(access, regex_name, None) or getattr(files, regex_name)
    assert regex.fullmatch(good)
    for bad in _variants(good):
        assert regex.fullmatch(bad) is None


# ── scripts (not importable): ant-serve, ant-adduser, ant_s3 ─────────────────

@pytest.mark.parametrize('script, name, good', [
    ('bin/api/ant-serve', '_NOTEBOOK_FILENAME_RE', 'TREM2_violin'),
    ('bin/ops/ant-adduser', 'LOGIN_RE', 'alice'),
    ('bin/ops/ant-adduser', 'EMAIL_RE', 'a@b.org'),
    ('bin/ops/ant_s3', 'GROUP_NAME_RE', 'Smith2024'),
])
def test_script_validators(script, name, good):
    regex = _lift(REPO_ROOT / script, {name})[name]
    assert regex.fullmatch(good)
    for bad in _variants(good):
        assert regex.fullmatch(bad) is None, repr(bad)


# ── guard: validators are never applied with .match / .search ────────────────

VALIDATORS = ('SEGMENT_RE', 'NEW_GROUP_RE', 'BASENAME_RE', 'RESERVED_BASENAME_RE', 'SHA256_RE', 'DEPOSIT_ID_RE',
              'DB_ID_RE', 'USER_RE', '_TOKEN_RE', 'CITEKEY_RE', '_EMAIL_RE', '_NAME_RE', '_SPDX_LIKE', '_NAME_PART',
              '_SAFE_IDENT_RE', '_LAYER_KEY_RE', '_SIDECAR_BASENAME_RE', '_SIDECAR_NAME_RE', '_ANNO_COL_RE',
              '_VERSION_RE', '_DONOR_GROUP_RE', '_SESSION_ID_RE', 'EMAIL_RE', 'LOGIN_RE', 'GROUP_NAME_RE',
              '_NOTEBOOK_FILENAME_RE')


def _sources():
    for folder in ('lib', 'bin', 'web'):
        for path in (REPO_ROOT / folder).rglob('*'):
            if path.is_file() and (path.suffix == '.py' or (not path.suffix and path.read_bytes()[:2] == b'#!')):
                yield path


def test_no_validator_is_applied_with_match_or_search():
    pattern = re.compile(r'(?<![A-Za-z0-9_])(?:\w+\.)?(' + '|'.join(VALIDATORS) + r')\.(match|search)\(')
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
            if pattern.search(line):
                offenders.append(f'{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}')
    assert not offenders, '\n'.join(offenders)
