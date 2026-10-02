"""Adding papers to the library through the API (``POST /papers/upload``).

One call takes a metadata record (a JSON object or the full ``.md`` text with
YAML front matter) and optionally a PDF, and:

1. normalises and validates the record (the upload requirements below);
2. fills missing fields from CrossRef/OpenAlex by DOI or PMID when asked,
   never overwriting what the caller gave, and lists filled fields in
   ``anthive_draft``;
3. refuses duplicates (same DOI, PMID, PDF checksum or citekey) unless the
   caller asked to update the existing record;
4. picks the folder (``lab`` for a lab author, ``datasets`` for source and
   reanalysis papers, ``methods`` for method papers, ``reading`` otherwise)
   unless the caller named one;
5. writes ``papers/<folder>/<citekey>.md`` (+ ``.pdf``) atomically under a
   lock and commits the record in the library git repository with the
   uploader as author.

Dataset links proposed by the uploader (``papers_for``) are stored as
``anthive_proposed_links`` in the record and never written into the
h5adstore: linking a dataset to a paper stays a curated step.

Nothing here talks HTTP; ant-serve and the tests call ``upload_paper``.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import yaml

from library import (ACCESS, DEFAULT_LAB_AUTHORS, ENTRY_TYPES, LIST_FIELDS, LLM_POLICY,
                     ROLES, Paper, first_author_last, folder_for, load_library,
                     make_citekey, normalize_doi, parse_front_matter, render_sidecar,
                     validate_paper)

FOLDERS = ('lab', 'datasets', 'methods', 'reading')
NOTE_ONLY_TYPES = {'unpublished', 'misc'}
IDENTIFIER_FIELDS = ('doi', 'pmid', 'eprint', 'url')
REQUIRED = ['entrytype', 'title', 'author', 'date or year', 'identifier']
RECOMMENDED = ['journaltitle', 'abstract', 'keywords', 'anthive_role', 'anthive_celltypes',
               'anthive_tissues', 'anthive_diseases', 'anthive_methods', 'anthive_cohorts',
               'anthive_summary', 'anthive_access', 'anthive_llm', 'papers_for']
PDF_MAGIC = b'%PDF-'
LOCK_NAME = '.anthive-upload.lock'
DEFAULT_COMMITTER = 'anthive <noreply@anthive.local>'

FIELD_HELP: Dict[str, Dict[str, Any]] = {
    'entrytype': {'description': 'BibLaTeX entry type', 'example': 'article', 'type': 'string',
                  'vocab': sorted(ENTRY_TYPES)},
    'title': {'description': 'full title', 'example': 'Plasticity of human microglia ...', 'type': 'string'},
    'author': {'description': "authors as a list of 'Last, First'", 'example': ['Lee, Donghoon', 'Roussos, Panos'],
               'type': 'list'},
    'date': {'description': 'publication date YYYY, YYYY-MM or YYYY-MM-DD', 'example': '2026-03-14', 'type': 'string'},
    'year': {'description': 'publication year (when no full date)', 'example': 2026, 'type': 'integer'},
    'doi': {'description': 'DOI without https://doi.org/', 'example': '10.1038/s41588-026-02716-6', 'type': 'string'},
    'pmid': {'description': 'PubMed id', 'example': '41234567', 'type': 'string'},
    'eprint': {'description': 'preprint id (with eprinttype)', 'example': '2024.01.02.573911', 'type': 'string'},
    'eprinttype': {'description': 'preprint server', 'example': 'biorxiv', 'type': 'string'},
    'url': {'description': 'stable URL when there is no DOI/PMID', 'example': 'https://...', 'type': 'string'},
    'note': {'description': 'free note; required for unpublished/misc entries without an identifier',
             'example': 'Manuscript in preparation', 'type': 'string'},
    'journaltitle': {'description': 'journal name', 'example': 'Nature Genetics', 'type': 'string'},
    'abstract': {'description': 'abstract text', 'example': '...', 'type': 'string'},
    'keywords': {'description': 'free keywords', 'example': ['microglia', 'aging'], 'type': 'list'},
    'anthive_role': {'description': 'why the paper is in the library', 'example': 'source', 'type': 'string',
                     'vocab': sorted(ROLES)},
    'anthive_celltypes': {'description': 'cell types studied (mus vocabulary labels)',
                          'example': ['microglial cell'], 'type': 'list'},
    'anthive_tissues': {'description': 'tissues or regions (mus vocabulary labels)',
                        'example': ['prefrontal cortex'], 'type': 'list'},
    'anthive_diseases': {'description': 'diseases or conditions (mus vocabulary labels)',
                         'example': ['Alzheimer disease'], 'type': 'list'},
    'anthive_methods': {'description': 'main methods', 'example': ['snRNA-seq', '10x Chromium'], 'type': 'list'},
    'anthive_cohorts': {'description': 'donor cohorts', 'example': ['ROSMAP'], 'type': 'list'},
    'anthive_summary': {'description': '2-3 sentences on what the paper shows', 'example': '...', 'type': 'string'},
    'anthive_access': {'description': 'who may see it', 'example': 'internal', 'type': 'string',
                       'vocab': sorted(ACCESS)},
    'anthive_llm': {'description': 'may passages go to external LLMs', 'example': 'allowed', 'type': 'string',
                    'vocab': sorted(LLM_POLICY)},
    'papers_for': {'description': "datasets or collections this paper is the source/reanalysis of, "
                                  "e.g. {'L26/FMG': 'source'} (stored as a proposal for an admin to link)",
                   'example': {'L26/FMG': 'source'}, 'type': 'mapping'},
}


class UploadError(Exception):
    """Carries an HTTP status and a JSON body for ant-serve."""

    def __init__(self, status: int, body: Dict[str, Any]):
        super().__init__(body.get('status', 'error'))
        self.status = status
        self.body = body


@dataclass
class UploadSettings:
    library_dir: Path
    lab_authors: Tuple[str, ...] = DEFAULT_LAB_AUTHORS
    max_pdf_mb: int = 25
    committer: str = DEFAULT_COMMITTER
    git_push: bool = False


@dataclass
class UploadResult:
    status: str
    http_status: int
    body: Dict[str, Any]
    pdf_path: Optional[Path] = None


def requirements(max_pdf_mb: int = 25) -> Dict[str, Any]:
    return {
        'required': REQUIRED,
        'required_detail': {
            'identifier': "at least one of doi, pmid, eprint (+eprinttype) or url; entrytype "
                          "unpublished or misc may instead give a note",
            'author': "a list of 'Last, First' names, at least one",
            'date or year': 'date (YYYY[-MM[-DD]]) or year',
        },
        'recommended': RECOMMENDED,
        'fields': FIELD_HELP,
        'roles': sorted(ROLES),
        'folders': list(FOLDERS),
        'entrytypes': sorted(ENTRY_TYPES),
        'max_pdf_mb': max_pdf_mb,
    }


# ── input normalisation ─────────────────────────────────────────────────

def parse_metadata(metadata: Any) -> Tuple[Dict[str, Any], str]:
    """Fields and body from a dict, a JSON string, or a .md text with front matter."""
    if isinstance(metadata, dict):
        fields = dict(metadata)
        body = str(fields.pop('notes', '') or fields.pop('body', '') or '')
        return fields, body
    if not isinstance(metadata, str) or not metadata.strip():
        raise UploadError(422, {'status': 'invalid', 'missing_required': REQUIRED,
                                'errors': [{'field': 'metadata', 'message': 'metadata is empty'}]})
    text = metadata.strip()
    if text.startswith('{'):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                    'errors': [{'field': 'metadata', 'message': f'invalid JSON: {exc}'}]})
        if not isinstance(data, dict):
            raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                    'errors': [{'field': 'metadata', 'message': 'JSON must be an object'}]})
        return parse_metadata(data)
    try:
        fields, body = parse_front_matter(text + '\n')
    except (ValueError, yaml.YAMLError) as exc:
        raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                'errors': [{'field': 'metadata', 'message': f'not a JSON object or .md with front matter: {exc}'}]})
    return dict(fields), body.strip()


def _normalise(fields: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in fields.items() if v not in (None, '', [], {})}
    if 'entrytype' in out:
        out['entrytype'] = str(out['entrytype']).strip().lower().lstrip('@')
    if 'journal' in out and 'journaltitle' not in out:
        out['journaltitle'] = out.pop('journal')
    if isinstance(out.get('author'), str):
        out['author'] = [a.strip() for a in re.split(r'\s+and\s+|;', out['author']) if a.strip()]
    for key in LIST_FIELDS:
        if key in out and isinstance(out[key], str):
            out[key] = [v.strip() for v in re.split(r'[;,]', out[key]) if v.strip()]
    if out.get('doi'):
        out['doi'] = normalize_doi(out['doi'])
    if out.get('pmid') is not None:
        out['pmid'] = re.sub(r'\D', '', str(out['pmid'])) or None
        if not out['pmid']:
            out.pop('pmid')
    if 'year' in out and str(out['year']).isdigit():
        out['year'] = int(out['year'])
    return out


def normalise_links(value: Any, default_role: str = 'source') -> Dict[str, str]:
    """papers_for in any of the accepted shapes -> {db_id_or_collection: role}."""
    if value in (None, '', [], {}):
        return {}
    out: Dict[str, str] = {}
    items: Iterable[Any]
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, str):
        items = [(v.strip(), None) for v in value.split(',') if v.strip()]
    elif isinstance(value, list):
        items = []
        for entry in value:
            if isinstance(entry, dict):
                target = entry.get('id') or entry.get('db_id') or entry.get('collection') or entry.get('target')
                items.append((target, entry.get('role')))
            else:
                items.append((entry, None))
    else:
        raise ValueError('papers_for must be a mapping, a list or a comma-separated string')
    for target, role in items:
        if not target:
            continue
        target = str(target).strip()
        if role is None and ':' in target and '/' not in target.split(':', 1)[1]:
            target, role = target.rsplit(':', 1)
        role = str(role or default_role).strip()
        if role not in ROLES:
            raise ValueError(f"papers_for: role {role!r} for {target} not in {sorted(ROLES)}")
        out[target] = role
    return out


# ── validation ──────────────────────────────────────────────────────────

def check_required(fields: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[str]]:
    errors: List[Dict[str, str]] = []
    missing: List[str] = []
    entrytype = fields.get('entrytype')
    if not entrytype:
        missing.append('entrytype')
        errors.append({'field': 'entrytype', 'message': 'entrytype is required (e.g. article, online)'})
    elif entrytype not in ENTRY_TYPES:
        errors.append({'field': 'entrytype', 'message': f"{entrytype!r} is not one of {sorted(ENTRY_TYPES)}"})
    if not fields.get('title'):
        missing.append('title')
        errors.append({'field': 'title', 'message': 'title is required'})
    authors = fields.get('author')
    if not authors:
        missing.append('author')
        errors.append({'field': 'author', 'message': "at least one author ('Last, First') is required"})
    elif not isinstance(authors, list) or not all(isinstance(a, str) and a.strip() for a in authors):
        errors.append({'field': 'author', 'message': "author must be a list of 'Last, First' strings"})
    if not (fields.get('date') or fields.get('year')):
        missing.append('date or year')
        errors.append({'field': 'date', 'message': 'date or year is required'})
    has_id = any(fields.get(k) for k in ('doi', 'pmid', 'url')) or (
        fields.get('eprint') and fields.get('eprinttype'))
    if fields.get('eprint') and not fields.get('eprinttype') and not any(fields.get(k) for k in ('doi', 'pmid', 'url')):
        errors.append({'field': 'eprinttype', 'message': 'eprint needs eprinttype (e.g. biorxiv, arxiv)'})
    if not has_id:
        if entrytype in NOTE_ONLY_TYPES:
            if not fields.get('note'):
                missing.append('identifier')
                errors.append({'field': 'note', 'message': f'{entrytype} entries without an identifier need a note'})
        else:
            missing.append('identifier')
            errors.append({'field': 'doi', 'message': 'an identifier is required: doi, pmid, eprint+eprinttype or url'})
    return errors, missing


def missing_recommended(fields: Dict[str, Any]) -> List[str]:
    out = []
    for key in RECOMMENDED:
        if key == 'papers_for':
            if not fields.get('anthive_proposed_links'):
                out.append(key)
        elif not fields.get(key):
            out.append(key)
    return out


# ── enrichment ──────────────────────────────────────────────────────────

def default_fetch(fields: Dict[str, Any]) -> Dict[str, Any]:
    """CrossRef/OpenAlex metadata for the record's DOI (or PMID -> DOI)."""
    from library import doi_from_pmid, metadata_from_doi  # noqa: PLC0415
    doi = fields.get('doi')
    if not doi and fields.get('pmid'):
        doi = doi_from_pmid(str(fields['pmid']))
    if not doi:
        return {}
    return metadata_from_doi(doi)


def enrich(fields: Dict[str, Any], fetch: Callable[[Dict[str, Any]], Dict[str, Any]] = default_fetch
           ) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """(fields, filled field names, warnings). Never overwrites a given value."""
    warnings: List[str] = []
    if not (fields.get('doi') or fields.get('pmid')):
        return fields, [], warnings
    try:
        fetched = fetch(fields) or {}
    except Exception as exc:  # noqa: BLE001 - network or lookup failure is a warning
        warnings.append(f"enrichment failed ({type(exc).__name__}: {exc}); record kept as given")
        return fields, [], warnings
    out = dict(fields)
    filled = []
    has_date = bool(out.get('date') or out.get('year'))
    for key, value in fetched.items():
        if value in (None, '', [], {}):
            continue
        if key in ('date', 'year') and has_date:
            continue
        if key == 'entrytype' and out.get('entrytype'):
            continue
        if out.get(key) in (None, '', [], {}):
            out[key] = value
            filled.append(key)
    return out, filled, warnings


# ── duplicates and PDFs ─────────────────────────────────────────────────

_sha_cache: Dict[str, Tuple[float, int, str]] = {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    stat = path.stat()
    key = str(path)
    cached = _sha_cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    _sha_cache[key] = (stat.st_mtime, stat.st_size, digest.hexdigest())
    return _sha_cache[key][2]


def find_duplicate(fields: Dict[str, Any], pdf_sha: Optional[str],
                   papers: Dict[str, Paper]) -> Optional[Tuple[Paper, str]]:
    doi = fields.get('doi')
    pmid = str(fields.get('pmid') or '')
    for paper in papers.values():
        if doi and paper.doi == doi:
            return paper, 'doi'
        if pmid and str(paper.fields.get('pmid') or '') == pmid:
            return paper, 'pmid'
    if pdf_sha:
        for paper in papers.values():
            name = paper.fields.get('file')
            if name and paper.path and (paper.path.parent / name).is_file():
                if sha256_path(paper.path.parent / name) == pdf_sha:
                    return paper, 'sha256'
    citekey = fields.get('citekey')
    if citekey and citekey in papers:
        return papers[citekey], 'citekey'
    if not (citekey or doi or pmid) and fields.get('title') and fields.get('author'):
        # no DOI/PMID to go on (preprints, url-only records): the same base
        # citekey (first author + year + title word) with the same title is
        # the same paper, not a reason to mint "<key>a"
        year = fields.get('year') or str(fields.get('date') or '')[:4]
        base = make_citekey(first_author_last(fields), year, fields['title'], {})
        same = papers.get(base)
        if same is not None and _norm_title(same.fields.get('title')) == _norm_title(fields['title']):
            return same, 'citekey'
    return None


def _norm_title(title: Any) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(title or '').lower()).strip()


def check_pdf(data: Optional[bytes], max_pdf_mb: int) -> None:
    if data is None:
        return
    if len(data) > max_pdf_mb * 1024 * 1024:
        raise UploadError(413, {'status': 'too_large', 'max_pdf_mb': max_pdf_mb,
                                'detail': f'PDF is {len(data) / 1e6:.1f} MB, the limit is {max_pdf_mb} MB'})
    if not data[:1024].lstrip().startswith(PDF_MAGIC):
        raise UploadError(415, {'status': 'not_pdf', 'detail': 'the file is not a PDF (no %PDF- header)'})


# ── writing and git ─────────────────────────────────────────────────────

def private_path(library_dir: Path, name: str) -> Path:
    """A per-checkout file that must never be committed: inside the git
    metadata folder when the library is a plain checkout, else a dotfile in
    the library root (doc/LIBRARY.md asks to gitignore those)."""
    meta_dir = Path(library_dir) / '.git'
    return meta_dir / name.lstrip('.') if meta_dir.is_dir() else Path(library_dir) / name


@contextlib.contextmanager
def library_lock(library_dir: Path):
    path = private_path(library_dir, LOCK_NAME)
    with open(path, 'a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.uploading")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _git(library_dir: Path, *args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(library_dir), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=120)


def is_git_repo(library_dir: Path) -> bool:
    try:
        return _git(library_dir, 'rev-parse', '--is-inside-work-tree').stdout.strip() == 'true'
    except (OSError, subprocess.SubprocessError):
        return False


def _split_ident(value: str) -> Tuple[str, str]:
    match = re.match(r'^\s*(.*?)\s*<([^>]*)>\s*$', value or '')
    if match:
        return match.group(1) or 'anthive', match.group(2) or 'noreply@anthive.local'
    return (value or 'anthive').strip(), 'noreply@anthive.local'


def git_commit(library_dir: Path, paths: List[Path], message: str, author: Tuple[str, str],
               committer: str = DEFAULT_COMMITTER, push: bool = False) -> Tuple[Optional[str], List[str]]:
    """Commit ``paths`` (records only; PDFs are gitignored). Returns (sha, warnings)."""
    warnings: List[str] = []
    if not is_git_repo(library_dir):
        return None, [f"{library_dir} is not a git repository: the record was written but not committed"]
    rels = [str(Path(p).relative_to(library_dir)) for p in paths]
    added = _git(library_dir, 'add', '--', *rels)
    if added.returncode != 0:
        return None, [f"git add failed: {added.stderr.strip()}"]
    c_name, c_email = _split_ident(committer)
    env = {'GIT_COMMITTER_NAME': c_name, 'GIT_COMMITTER_EMAIL': c_email,
           'GIT_AUTHOR_NAME': author[0], 'GIT_AUTHOR_EMAIL': author[1]}
    commit = _git(library_dir, '-c', 'commit.gpgsign=false', 'commit', '-m', message, '--', *rels, env=env)
    if commit.returncode != 0:
        if 'nothing to commit' in (commit.stdout + commit.stderr):
            warnings.append('record unchanged: nothing to commit')
            return None, warnings
        return None, [f"git commit failed: {(commit.stderr or commit.stdout).strip()}"]
    sha = _git(library_dir, 'rev-parse', 'HEAD').stdout.strip()
    if push:
        pushed = _git(library_dir, 'push')
        if pushed.returncode != 0:
            warnings.append(f"committed {sha[:8]} but git push failed: {(pushed.stderr or pushed.stdout).strip()}")
    return sha, warnings


def uploader_identity(user: str, name: Optional[str], email: Optional[str]) -> Tuple[str, str]:
    return (name or user or 'anthive').strip(), (email or f"{user or 'anthive'}@anthive.local").strip()


# ── the upload ──────────────────────────────────────────────────────────

def upload_paper(settings: UploadSettings, metadata: Any, pdf: Optional[bytes] = None, *,
                 folder: Optional[str] = None, enrich_fields: bool = True, dry_run: bool = False,
                 update: bool = False, user: str = 'anthive', user_name: Optional[str] = None,
                 user_email: Optional[str] = None,
                 fetch: Callable[[Dict[str, Any]], Dict[str, Any]] = default_fetch) -> UploadResult:
    """Validate, enrich, deduplicate, place and commit one paper (see module doc)."""
    library_dir = Path(settings.library_dir)
    check_pdf(pdf, settings.max_pdf_mb)
    if folder is not None and folder not in FOLDERS:
        raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                'errors': [{'field': 'folder', 'message': f'folder must be one of {list(FOLDERS)}'}]})
    raw, body = parse_metadata(metadata)
    try:
        links = normalise_links(raw.pop('papers_for', None), str(raw.get('anthive_role') or 'source'))
    except ValueError as exc:
        raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                'errors': [{'field': 'papers_for', 'message': str(exc)}]})
    fields = _normalise(raw)
    if links:
        fields['anthive_proposed_links'] = {**(fields.get('anthive_proposed_links') or {}), **links}
    filled: List[str] = []
    warnings: List[str] = []
    if enrich_fields:
        fields, filled, enrich_warnings = enrich(fields, fetch)
        warnings += enrich_warnings
        fields = _normalise(fields)
    if filled:
        draft = list(fields.get('anthive_draft') or [])
        fields['anthive_draft'] = draft + [f for f in filled if f not in draft]
    errors, missing = check_required(fields)
    pdf_sha = sha256_bytes(pdf) if pdf is not None else None

    if not library_dir.is_dir():
        raise UploadError(503, {'status': 'unavailable', 'detail': f'library folder {library_dir} does not exist'})
    lock = library_lock(library_dir) if not dry_run else contextlib.nullcontext()
    with lock:
        papers, _issues = load_library(library_dir)
        duplicate = find_duplicate(fields, pdf_sha, papers)
        if duplicate and not update:
            existing, matched_on = duplicate
            raise UploadError(409, {'status': 'duplicate', 'existing_citekey': existing.citekey,
                                    'matched_on': matched_on})
        existing = duplicate[0] if duplicate else None
        if update and existing is not None:
            merged = dict(existing.fields)
            merged.update({k: v for k, v in fields.items() if k != 'citekey'})
            if fields.get('anthive_draft') is not None:
                merged['anthive_draft'] = sorted(set(existing.fields.get('anthive_draft') or [])
                                                 | set(fields.get('anthive_draft') or []))
            fields = merged
            citekey = existing.citekey
            target_dir = existing.path.parent
            body = body or existing.body
            errors, missing = check_required(fields)
        else:
            citekey = str(fields.get('citekey') or '')
            if citekey and citekey in papers:
                raise UploadError(409, {'status': 'duplicate', 'existing_citekey': citekey,
                                        'matched_on': 'citekey'})
            if not citekey:
                year = fields.get('year') or str(fields.get('date') or '')[:4]
                citekey = make_citekey(first_author_last(fields), year, fields.get('title', ''), papers)
            role = str(fields.get('anthive_role') or '')
            try:
                target_dir = folder_for(library_dir, role, folder, fields.get('author') or [],
                                        settings.lab_authors)
            except ValueError as exc:
                raise UploadError(422, {'status': 'invalid', 'missing_required': [],
                                        'errors': [{'field': 'folder', 'message': str(exc)}]})
        fields['citekey'] = citekey
        if pdf is not None:
            fields['file'] = f"{citekey}.pdf"
        record_path = target_dir / f"{citekey}.md"
        paper = Paper(citekey=citekey, fields=fields, body=body, path=record_path)
        issues = validate_paper(paper)
        errors += [{'field': _field_of(msg), 'message': msg.split(': ', 1)[-1]} for msg in issues.errors
                   if 'file name must be' not in msg]
        warnings += [msg.split(': ', 1)[-1] for msg in issues.warnings]
        if errors:
            # check_required and validate_paper overlap; report each problem once per field
            unique, seen_fields = [], set()
            for error in errors:
                if error['field'] not in seen_fields:
                    seen_fields.add(error['field'])
                    unique.append(error)
            raise UploadError(422, {'status': 'invalid', 'errors': unique, 'missing_required': missing})
        if links:
            pairs = ', '.join(f"{k} ({v})" for k, v in links.items())
            warnings.append(f"proposed dataset links are stored in the record but not applied: an admin "
                            f"must add `papers: {{{citekey}: <role>}}` to {pairs}")
        try:
            rel_folder = target_dir.relative_to(library_dir / 'papers').as_posix()
        except ValueError:
            rel_folder = target_dir.relative_to(library_dir).as_posix()
        status = 'dry_run' if dry_run else ('updated' if existing is not None else 'created')
        result_body = {
            'status': status, 'citekey': citekey, 'folder': rel_folder,
            'path': str(record_path.relative_to(library_dir)),
            'pdf_sha256': pdf_sha, 'record': fields, 'filled_fields': filled,
            'warnings': warnings, 'missing_recommended': missing_recommended(fields),
        }
        if dry_run:
            return UploadResult(status, 200, result_body)
        pdf_path = None
        if pdf is not None:
            pdf_path = target_dir / f"{citekey}.pdf"
            _atomic_write(pdf_path, pdf)
        _atomic_write(record_path, render_sidecar(fields, body).encode('utf-8'))
        verb = 'Update' if existing is not None else 'Add'
        sha, git_warnings = git_commit(
            library_dir, [record_path], f"{verb} {citekey}: {str(fields.get('title', ''))[:60]}",
            uploader_identity(user, user_name, user_email), settings.committer, settings.git_push)
        result_body['warnings'] = warnings + git_warnings
        result_body['commit'] = sha
        return UploadResult(status, 200 if existing is not None else 201, result_body, pdf_path)


def _field_of(message: str) -> str:
    for key in ('doi', 'entrytype', 'title', 'author', 'anthive_role', 'anthive_access', 'anthive_llm',
                'date', 'citekey'):
        if key in message:
            return key
    return 'record'
