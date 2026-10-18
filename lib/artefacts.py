"""Artefact store, read side (doc/DESIGN_artefacts.md, phase A1).

An artefact is a versioned, immutable set of analysis files (a DE table, a figure, a table, a notebook, a
report, a model, other) that belongs to one dataset or to one collection. This module holds everything the
server needs to read and serve them; the deposit and the watcher (phase A2) write them with the same layout
and validators.

Layout (design 2.3), relative to the store root and, on S3, to the catalog prefix::

    <Group>/static/artefacts/<FILEID|_group>/<kind>/<artefact_id>/
        artefact.md               head record (YAML front matter, written only by the pipeline)
        v<N>/manifest.json        immutable version manifest (schema ``anthive.artefact/1``)
        v<N>/files/<name>         the files, byte for byte (S3; locally only inline copies)
        v<N>/preview/<name>.webp  server-generated previews (``.txt`` for the first rows of a table)

What is here:

* names and identifiers: :func:`file_name_problem`, :class:`ArtefactPath`, :func:`parse_ref`,
  :func:`parse_version_label`;
* the head record (:func:`parse_head_record`, :func:`render_head_record`, :func:`validate_head_record`) and
  the manifest (:func:`validate_manifest`);
* the collection registry built from every ``<Group>/index.md`` (:func:`scan_collections`), so collections
  without datasets (``collection_kind: analysis``) exist for the API;
* the artefact index built at the catalog scan (:class:`ArtefactIndex`): only records whose every field
  agrees with their place in the tree and whose manifests match the sha256 in the head record are served;
* access (:class:`AccessPolicy`, design 3.1): an artefact is visible exactly when its scope is (dataset:
  served, not hidden from the caller; collection: the most restrictive of its datasets, or the ``access`` of
  an analysis collection), and ``access: internal`` artefacts need a caller who sees internal data;
* delivery (:class:`ArtefactService`): a 307 to a presigned GET for one key, or a verified local copy, or
  ``not_on_this_site``; previews from local files only.

Input: the store tree and the S3 bucket (HEAD and presign only). Output: plain dicts for the REST routes in
``bin/api/ant-serve``; refusals raise :class:`ArtefactError` with the deposit API's JSON shape.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

import dataset_access

log = logging.getLogger('anthive.artefacts')

# ── identifiers and names ─────────────────────────────────────────────────────

SCHEMA = 'anthive.artefact/1'
KINDS = ('de_table', 'figure', 'table', 'notebook', 'report', 'model', 'other')
GROUP_SCOPE = '_group'
ARTEFACTS_PARTS = ('static', 'artefacts')
HEAD_RECORD_NAME = 'artefact.md'
MANIFEST_NAME = 'manifest.json'
FILES_DIR = 'files'
PREVIEW_DIR = 'preview'
#: preview suffix -> the Content-Type the server sends (never taken from the file or the manifest)
PREVIEW_TYPES = {'.webp': 'image/webp', '.txt': 'text/plain; charset=utf-8'}

#: one key segment: the same rule as ``ingest_pipeline.SEGMENT_RE`` (tested to stay equal)
SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
ARTEFACT_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,63}$')
GROUP_ID_RE = re.compile(r'^[A-Za-z0-9]{3}$')
#: the ``data_project`` naming rule of new collections (``dataset_deposit.NEW_GROUP_RE``)
DATA_PROJECT_RE = re.compile(r'^[A-Z][A-Za-z0-9_]*\d{4}$')
VERSION_LABEL_RE = re.compile(r'^v([1-9][0-9]{0,5})$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DB_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,32}/[A-Za-z0-9._-]{1,64}$')
CONTENT_TYPE_RE = re.compile(r'^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$')
REF_RE = re.compile(r'^(?P<group_id>[^/@]+)/(?P<scope>[^/@]+)/(?P<artefact_id>[^/@]+)(?:@(?P<version>[^/@]+))?$')

SCOPE_MAX_CHARS = 64
FILE_SEGMENT_MAX_CHARS = 120
MAX_SUBFOLDERS = 1
TITLE_MAX_CHARS = 200
DESCRIPTION_MAX_BYTES = 20 * 1024
CHANGE_NOTE_MAX_CHARS = 2000
MAX_VERSION = 999999
ACCESS_VALUES = ('inherit', 'internal')
VERSION_STATES = ('live', 'purging', 'purged')
FILE_ROLES = ('main', 'supporting', 'sidecar')
COLLECTION_KINDS = ('datasets', 'analysis')
COLLECTION_ACCESS_VALUES = ('internal', 'public')
#: largest head record / manifest the index reads
RECORD_MAX_BYTES = 1024 * 1024


class ArtefactError(Exception):
    """A refusal with an HTTP status and a JSON body ``{'status': <word>, 'message': ...}`` (deposit API shape)."""

    def __init__(self, status: int, word: str, message: str, headers: Optional[Dict[str, str]] = None,
                 **extra: Any):
        super().__init__(message)
        self.status = status
        self.body: Dict[str, Any] = {'status': word, 'message': message, **extra}
        self.headers: Dict[str, str] = dict(headers or {})


def valid_segment(segment: Any) -> bool:
    """A safe key segment: ASCII letters, digits, ``._-``, not starting with ``.`` or ``_``, NFC."""
    return (isinstance(segment, str) and bool(segment) and segment not in ('.', '..')
            and unicodedata.normalize('NFC', segment) == segment and bool(SEGMENT_RE.fullmatch(segment)))


def valid_scope(scope: Any) -> bool:
    """``_group`` or a dataset file id (a safe segment of at most 64 characters)."""
    return scope == GROUP_SCOPE or (valid_segment(scope) and len(scope) <= SCOPE_MAX_CHARS)


def valid_artefact_id(artefact_id: Any) -> bool:
    return isinstance(artefact_id, str) and bool(ARTEFACT_ID_RE.fullmatch(artefact_id))


def file_name_problem(name: Any) -> Optional[str]:
    """Why ``name`` is not an acceptable artefact file name, or None.

    A name is one or two ``/``-separated segments (at most one subfolder), each a safe segment (ASCII
    letters, digits, ``._-``; not starting with ``.`` or ``_``; so never ``..``) of at most 120 characters.
    """
    if not isinstance(name, str) or not name:
        return 'the file name is empty'
    if not name.isascii():
        return f'{name!r}: only ASCII letters, digits, ".", "_" and "-" are allowed'
    if '\\' in name or name.startswith('/') or name.endswith('/') or '//' in name:
        return f'{name!r}: not a relative name'
    parts = name.split('/')
    if len(parts) > MAX_SUBFOLDERS + 1:
        return f'{name!r}: at most one subfolder'
    for part in parts:
        if len(part) > FILE_SEGMENT_MAX_CHARS:
            return f'{name!r}: a name part is longer than {FILE_SEGMENT_MAX_CHARS} characters'
        if not valid_segment(part):
            return (f'{name!r}: each part must start with a letter or digit and hold only letters, digits, '
                    f'".", "_" and "-"')
    return None


def casefold_clashes(names: Iterable[str]) -> List[str]:
    """Names that equal an earlier one when case is ignored."""
    seen: Set[str] = set()
    clashes = []
    for name in names:
        key = name.casefold()
        if key in seen:
            clashes.append(name)
        seen.add(key)
    return clashes


def safe_content_type(value: Any) -> str:
    """``value`` when it is a plain ``type/subtype``, else ``application/octet-stream``."""
    if isinstance(value, str) and CONTENT_TYPE_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return 'application/octet-stream'


@dataclass(frozen=True)
class ArtefactPath:
    """Where one artefact lives, relative to the store root (and to the catalog prefix on S3).

    Every part is validated at construction, so a path built from it never leaves the artefact's folder.
    """
    group: str
    scope: str
    kind: str
    artefact_id: str

    def __post_init__(self) -> None:
        if not valid_segment(self.group):
            raise ValueError(f'invalid group folder {self.group!r}')
        if not valid_scope(self.scope):
            raise ValueError(f'invalid scope {self.scope!r}')
        if self.kind not in KINDS:
            raise ValueError(f'invalid kind {self.kind!r}')
        if not valid_artefact_id(self.artefact_id):
            raise ValueError(f'invalid artefact id {self.artefact_id!r}')

    @property
    def rel(self) -> PurePosixPath:
        return PurePosixPath(self.group, *ARTEFACTS_PARTS, self.scope, self.kind, self.artefact_id)

    @property
    def head_rel(self) -> PurePosixPath:
        return self.rel / HEAD_RECORD_NAME

    def version_rel(self, version: int) -> PurePosixPath:
        return self.rel / f'v{_check_version(version)}'

    def manifest_rel(self, version: int) -> PurePosixPath:
        return self.version_rel(version) / MANIFEST_NAME

    def file_rel(self, version: int, name: str) -> PurePosixPath:
        problem = file_name_problem(name)
        if problem:
            raise ValueError(problem)
        return self.version_rel(version) / FILES_DIR / name

    def preview_rel(self, version: int, name: str) -> PurePosixPath:
        problem = file_name_problem(name)
        if problem:
            raise ValueError(problem)
        return self.version_rel(version) / PREVIEW_DIR / name


def _check_version(version: Any) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        raise ValueError(f'invalid version {version!r}')
    return version


def s3_key(catalog_prefix: str, rel: PurePosixPath) -> str:
    """The object key of a store-relative path (``catalog_prefix`` is empty or ends in ``/``)."""
    prefix = (catalog_prefix or '').strip().strip('/')
    return f'{prefix}/{rel}' if prefix else str(rel)


@dataclass(frozen=True)
class Ref:
    """``<group_id>/<scope>/<artefact_id>[@vN]``; ``version`` None means the latest live version."""
    group_id: str
    scope: str
    artefact_id: str
    version: Optional[int] = None

    def __str__(self) -> str:
        return format_ref(self.group_id, self.scope, self.artefact_id, self.version)


def format_ref(group_id: str, scope: str, artefact_id: str, version: Optional[int] = None) -> str:
    base = f'{group_id}/{scope}/{artefact_id}'
    return f'{base}@v{version}' if version is not None else base


def parse_version_label(label: Any) -> Optional[int]:
    """``v3`` -> 3, ``latest`` -> None; anything else raises ValueError."""
    if label == 'latest':
        return None
    match = VERSION_LABEL_RE.fullmatch(label) if isinstance(label, str) else None
    if not match:
        raise ValueError(f'invalid version {label!r} (v1, v2, ... or latest)')
    return int(match.group(1))


def parse_ref(text: Any) -> Ref:
    """Parse a reference; raises ValueError for anything malformed."""
    match = REF_RE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise ValueError(f'invalid artefact reference {text!r}')
    group_id, scope, artefact_id = match.group('group_id'), match.group('scope'), match.group('artefact_id')
    if not GROUP_ID_RE.fullmatch(group_id) or not valid_scope(scope) or not valid_artefact_id(artefact_id):
        raise ValueError(f'invalid artefact reference {text!r}')
    version = parse_version_label(match.group('version')) if match.group('version') else None
    return Ref(group_id, scope, artefact_id, version)


# ── head record and manifest ──────────────────────────────────────────────────

def _plain(value: Any) -> Any:
    """YAML dates and datetimes as ISO strings, recursively (the API answers JSON)."""
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


_FRONT_MATTER_RE = re.compile(r'\A---\n(.*?\n)?---[ \t]*(?:\n|\Z)', re.DOTALL)


def split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """(front matter as a plain dict, body) of a ``---``-delimited document; ValueError when absent."""
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError('no front matter (a document starting with a --- line and closed by another)')
    data = yaml.safe_load(match.group(1) or '')
    if not isinstance(data, dict):
        raise ValueError('front matter is not a mapping')
    return _plain(data), text[match.end():]


def parse_front_matter(text: str) -> Dict[str, Any]:
    """The YAML front matter of a ``---``-delimited document as a plain dict; ValueError when absent."""
    return split_front_matter(text)[0]


def parse_head_record(text: str) -> Dict[str, Any]:
    return parse_front_matter(text)


def render_head_record(head: Mapping[str, Any]) -> str:
    """The text of ``artefact.md`` for ``head`` (front matter only, keys in the given order)."""
    return '---\n' + yaml.safe_dump(dict(head), sort_keys=False, allow_unicode=False, default_flow_style=None) \
        + '---\n'


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any, max_chars: int = 1000) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_chars


def validate_head_record(head: Mapping[str, Any], expected: ArtefactPath) -> List[str]:
    """Problems of a head record read from ``expected``'s folder (empty list: fine)."""
    problems: List[str] = []
    for key, want in (('artefact_id', expected.artefact_id), ('kind', expected.kind), ('scope', expected.scope)):
        if head.get(key) != want:
            problems.append(f'{key} is {head.get(key)!r}, the folder says {want!r}')
    if not _nonempty_str(head.get('owner'), 200):
        problems.append('owner missing')
    if not _nonempty_str(head.get('created'), 40):
        problems.append('created missing')
    if head.get('title') is not None and not isinstance(head.get('title'), str):
        problems.append('title is not text')
    versions = head.get('versions')
    if not isinstance(versions, list) or not versions:
        problems.append('versions missing')
        return problems
    numbers: Set[int] = set()
    live: List[int] = []
    for entry in versions:
        if not isinstance(entry, dict):
            problems.append('a versions entry is not a mapping')
            continue
        number = entry.get('version')
        if not _is_int(number) or not 1 <= number <= MAX_VERSION:
            problems.append(f'invalid version number {number!r}')
            continue
        if number in numbers:
            problems.append(f'version {number} is listed twice')
        numbers.add(number)
        state = entry.get('state')
        if state not in VERSION_STATES:
            problems.append(f'v{number}: invalid state {state!r}')
        if not (isinstance(entry.get('manifest_sha256'), str) and SHA256_RE.fullmatch(entry['manifest_sha256'])):
            problems.append(f'v{number}: manifest_sha256 missing or malformed')
        if not _nonempty_str(entry.get('published'), 40):
            problems.append(f'v{number}: published missing')
        if state == 'purged' and not _nonempty_str(entry.get('purged'), 40):
            problems.append(f'v{number}: purged without a date')
        if state == 'live':
            live.append(number)
    latest = head.get('latest')
    want_latest = max(live) if live else None
    if latest != want_latest:
        problems.append(f'latest is {latest!r}, the highest live version is {want_latest!r}')
    return problems


def validate_manifest(manifest: Any, path: ArtefactPath, group_id: str, version: int) -> List[str]:
    """Problems of a version manifest read from ``path``/v``version`` (empty list: fine).

    Checks the schema, that every identifying field agrees with the place the manifest was read from, and
    the shape of the files, provenance and access fields. The kind-specific QC (design section 6) is the
    pipeline's job (phase A2).
    """
    if not isinstance(manifest, dict):
        return ['the manifest is not a JSON object']
    problems: List[str] = []
    if manifest.get('schema') != SCHEMA:
        problems.append(f'schema is {manifest.get("schema")!r}, expected {SCHEMA!r}')
    expected = {
        'ref': format_ref(group_id, path.scope, path.artefact_id, version),
        'artefact_id': path.artefact_id, 'version': version, 'kind': path.kind, 'group': path.group,
        'group_id': group_id, 'scope': path.scope,
        'db_id': None if path.scope == GROUP_SCOPE else f'{group_id}/{path.scope}',
    }
    for key, want in expected.items():
        value = manifest.get(key)
        if value != want or (key == 'version' and not _is_int(value)):
            problems.append(f'{key} is {value!r}, expected {want!r}')
    if not _nonempty_str(manifest.get('title'), TITLE_MAX_CHARS):
        problems.append(f'title missing or longer than {TITLE_MAX_CHARS} characters')
    description = manifest.get('description')
    if description is not None and (not isinstance(description, str)
                                    or len(description.encode('utf-8')) > DESCRIPTION_MAX_BYTES):
        problems.append('description is not text of at most 20 kB')
    change_note = manifest.get('change_note')
    if change_note is not None and (not isinstance(change_note, str) or len(change_note) > CHANGE_NOTE_MAX_CHARS):
        problems.append('change_note is not text of at most 2000 characters')
    supersedes = manifest.get('supersedes')
    if supersedes is not None and (not _is_int(supersedes) or not 1 <= supersedes < version):
        problems.append(f'supersedes {supersedes!r} is not an earlier version')
    if manifest.get('access') not in ACCESS_VALUES:
        problems.append(f'access is {manifest.get("access")!r}, expected inherit or internal')
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        problems.append('files missing')
    else:
        names = []
        mains = 0
        for entry in files:
            if not isinstance(entry, dict):
                problems.append('a files entry is not a mapping')
                continue
            name = entry.get('name')
            problem = file_name_problem(name)
            if problem:
                problems.append(problem)
                continue
            names.append(name)
            if not _is_int(entry.get('size')) or entry['size'] < 0:
                problems.append(f'{name}: size missing')
            if not (isinstance(entry.get('sha256'), str) and SHA256_RE.fullmatch(entry['sha256'])):
                problems.append(f'{name}: sha256 missing or malformed')
            if not (isinstance(entry.get('content_type'), str) and CONTENT_TYPE_RE.fullmatch(entry['content_type'])):
                problems.append(f'{name}: content_type missing or malformed')
            if entry.get('role') not in FILE_ROLES:
                problems.append(f'{name}: role {entry.get("role")!r} is not one of {", ".join(FILE_ROLES)}')
            mains += entry.get('role') == 'main'
        for clash in casefold_clashes(names):
            problems.append(f'{clash}: two files differ only in case')
        if mains > 1:
            problems.append('more than one main file')
    made_from = manifest.get('made_from')
    if not isinstance(made_from, list):
        problems.append('made_from missing')
    else:
        db_ids = []
        for entry in made_from:
            if not isinstance(entry, dict) or not isinstance(entry.get('db_id'), str) \
                    or not DB_ID_RE.fullmatch(entry['db_id']):
                problems.append('a made_from entry has no valid db_id')
                continue
            db_ids.append(entry['db_id'])
            if not _nonempty_str(entry.get('dataset_version'), 40):
                problems.append(f'made_from {entry["db_id"]}: dataset_version missing')
            sha = entry.get('h5ad_sha256')
            if sha is not None and not (isinstance(sha, str) and SHA256_RE.fullmatch(sha)):
                problems.append(f'made_from {entry["db_id"]}: h5ad_sha256 malformed')
        if path.scope != GROUP_SCOPE and expected['db_id'] not in db_ids:
            problems.append(f'made_from must include the scope dataset {expected["db_id"]}')
    if not isinstance(manifest.get('declared'), dict):
        problems.append('declared missing')
    recorded = manifest.get('recorded')
    if not isinstance(recorded, dict):
        problems.append('recorded missing')
    else:
        if not _nonempty_str(recorded.get('uploaded_by'), 200):
            problems.append('recorded.uploaded_by missing')
        if not _nonempty_str(recorded.get('published'), 40):
            problems.append('recorded.published missing')
    if manifest.get('de') is not None and not isinstance(manifest.get('de'), dict):
        problems.append('de is not a mapping')
    if manifest.get('de') is not None and path.kind != 'de_table':
        problems.append('de is only allowed for kind de_table')
    return problems


# ── the collection registry ───────────────────────────────────────────────────

@dataclass
class Collection:
    """One collection (``<Group>/`` with its ``index.md``) as the API sees it."""
    folder: str
    group_id: str
    kind: str                       # 'datasets' or 'analysis'
    meta: Dict[str, Any]            # the index.md front matter (plain values)
    description: str = ''           # the markdown body of index.md
    access: Optional[str] = None    # the index.md access key, lower case, when set

    @property
    def title(self) -> Optional[str]:
        value = self.meta.get('title')
        return value if isinstance(value, str) else None


def _read_index_md(path: Path) -> Tuple[Dict[str, Any], str]:
    text = path.read_text(encoding='utf-8')
    if len(text) > RECORD_MAX_BYTES:
        raise ValueError('index.md is larger than 1 MB')
    meta, body = split_front_matter(text)
    return meta, body.lstrip('\n')


def analysis_collection_problems(folder: str, meta: Mapping[str, Any]) -> List[str]:
    """What an analysis collection's ``index.md`` lacks (design 2.6): the name rule, group equal to the
    folder, a 3-character group_id, a title, at least one author, access internal or public."""
    problems: List[str] = []
    if not DATA_PROJECT_RE.fullmatch(folder):
        problems.append(f'folder {folder!r} does not follow the collection naming rule (e.g. Fiers2026)')
    if meta.get('group') != folder:
        problems.append(f'group is {meta.get("group")!r}, expected the folder name {folder!r}')
    group_id = meta.get('group_id')
    if not (isinstance(group_id, str) and GROUP_ID_RE.fullmatch(group_id)):
        problems.append('group_id missing or not 3 letters or digits')
    if not _nonempty_str(meta.get('title'), TITLE_MAX_CHARS):
        problems.append('title missing')
    authors = meta.get('authors')
    if not (isinstance(authors, list) and any(isinstance(a, str) and a.strip() for a in authors)):
        problems.append('authors missing (a list with at least one name)')
    access = meta.get('access')
    if access is not None and str(access).strip().lower() not in COLLECTION_ACCESS_VALUES:
        problems.append(f'access is {access!r}, expected internal or public')
    return problems


def scan_collections(store_root: Path, dataset_group_ids: Optional[Mapping[str, Iterable[str]]] = None
                     ) -> Tuple[Dict[str, Collection], List[str]]:
    """The collection registry: every top-level ``<Group>/`` of the store with an ``index.md`` or with
    served datasets, keyed by group_id.

    ``dataset_group_ids`` maps a group folder to the group ids of its served datasets (from the catalog
    scan); it supplies the group id of a collection whose ``index.md`` has none, and makes a collection
    without ``index.md`` known. Folders starting with ``_`` or ``.`` are never collections. An analysis
    collection with an incomplete ``index.md`` is left out (fail closed). Two folders claiming one group id
    are both left out. Returns (registry, problems).
    """
    store_root = Path(store_root)
    dataset_group_ids = {folder: set(ids) for folder, ids in (dataset_group_ids or {}).items()}
    problems: List[str] = []
    found: Dict[str, List[Collection]] = {}
    try:
        children = sorted(store_root.iterdir())
    except OSError as exc:
        return {}, [f'cannot list the store: {exc}']
    folders = {child.name for child in children if child.is_dir()} | set(dataset_group_ids)
    for folder in sorted(folders):
        if not valid_segment(folder):
            continue
        index_path = store_root / folder / 'index.md'
        meta: Dict[str, Any] = {}
        body = ''
        if index_path.is_file():
            try:
                meta, body = _read_index_md(index_path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                problems.append(f'{folder}/index.md: {exc}')
                meta = {}
        elif folder not in dataset_group_ids:
            continue
        kind = str(meta.get('collection_kind') or 'datasets').strip().lower()
        if kind not in COLLECTION_KINDS:
            problems.append(f'{folder}/index.md: collection_kind {meta.get("collection_kind")!r} is not '
                            f'datasets or analysis; left out')
            continue
        if kind == 'analysis':
            issues = analysis_collection_problems(folder, meta)
            if issues:
                problems.append(f'{folder}/index.md (analysis collection) left out: ' + '; '.join(issues))
                continue
            group_id = meta['group_id']
        else:
            group_id = meta.get('group_id')
            group_id = str(group_id).strip() if group_id is not None else ''
            if not group_id:
                ids = dataset_group_ids.get(folder) or set()
                if len(ids) != 1:
                    if ids:
                        problems.append(f'{folder}: no group_id in index.md and its datasets use {sorted(ids)}')
                    continue
                group_id = next(iter(ids))
            if not GROUP_ID_RE.fullmatch(group_id):
                problems.append(f'{folder}: group_id {group_id!r} is not 3 letters or digits; left out')
                continue
        access = meta.get('access')
        access = str(access).strip().lower() if access is not None else None
        found.setdefault(group_id, []).append(Collection(folder, group_id, kind, meta, body, access))
    registry: Dict[str, Collection] = {}
    for group_id, entries in found.items():
        if len(entries) > 1:
            problems.append(f'group_id {group_id} is claimed by {", ".join(e.folder for e in entries)}; '
                            f'none of them is used for artefacts')
            continue
        registry[group_id] = entries[0]
    return registry, problems


# ── the artefact index ────────────────────────────────────────────────────────

@dataclass
class VersionEntry:
    """One version as the head record lists it, with its manifest when live and valid."""
    number: int
    state: str
    manifest_sha256: str
    published: Optional[str]
    purged: Optional[str] = None
    purged_by: Optional[str] = None
    purged_by_admin: bool = False
    purge_reason: Optional[str] = None
    manifest: Optional[Dict[str, Any]] = None
    problems: List[str] = field(default_factory=list)

    @property
    def servable(self) -> bool:
        return self.state == 'live' and self.manifest is not None and not self.problems


@dataclass
class ArtefactEntry:
    """One artefact of the index."""
    path: ArtefactPath
    group_id: str
    head: Dict[str, Any]
    versions: Dict[int, VersionEntry]
    folder: Path                    # resolved local folder of the artefact

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.group_id, self.path.scope, self.path.artefact_id)

    @property
    def ref(self) -> str:
        return format_ref(self.group_id, self.path.scope, self.path.artefact_id)

    @property
    def db_id(self) -> Optional[str]:
        return None if self.path.scope == GROUP_SCOPE else f'{self.group_id}/{self.path.scope}'

    @property
    def latest(self) -> Optional[int]:
        """The highest live version with a valid manifest (None: none)."""
        servable = [number for number, version in self.versions.items() if version.servable]
        return max(servable) if servable else None

    @property
    def live_versions(self) -> List[int]:
        return sorted(number for number, version in self.versions.items() if version.servable)

    @property
    def access_internal(self) -> bool:
        """True when any version's manifest narrows access to internal (the most restrictive wins)."""
        return any((version.manifest or {}).get('access') == 'internal' for version in self.versions.values())

    @property
    def owner(self) -> Optional[str]:
        owner = self.head.get('owner')
        return owner if isinstance(owner, str) else None


def _read_limited(path: Path) -> bytes:
    with path.open('rb') as handle:
        data = handle.read(RECORD_MAX_BYTES + 1)
    if len(data) > RECORD_MAX_BYTES:
        raise ValueError(f'{path.name} is larger than 1 MB')
    return data


def _inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside the resolved ``root``."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _list_dirs(folder: Path) -> List[Path]:
    try:
        return sorted(child for child in folder.iterdir() if child.is_dir())
    except OSError:
        return []


class ArtefactIndex:
    """Every servable artefact of the store, built once per catalog scan (immutable afterwards)."""

    def __init__(self, entries: Dict[Tuple[str, str, str], ArtefactEntry], problems: List[str],
                 built_at: float):
        self.entries = entries
        self.problems = problems
        self.built_at = built_at

    @classmethod
    def empty(cls) -> 'ArtefactIndex':
        return cls({}, [], time.time())

    @classmethod
    def build(cls, store_root: Path, collections: Mapping[str, Collection]) -> 'ArtefactIndex':
        """Read the head records and the live manifests under every collection's ``static/artefacts/``.

        A record is indexed only when its folder names are valid, its head record agrees with them, and each
        live version's manifest has the sha256 the head record lists and agrees with its place. Problems are
        logged and kept in ``problems``; nothing invalid is served. An artefact id used twice in one scope
        (two kinds) is left out entirely.
        """
        store_root = Path(store_root)
        entries: Dict[Tuple[str, str, str], ArtefactEntry] = {}
        problems: List[str] = []
        clashes: Set[Tuple[str, str, str]] = set()
        for group_id, collection in sorted(collections.items()):
            base = store_root / collection.folder / ARTEFACTS_PARTS[0] / ARTEFACTS_PARTS[1]
            if not base.is_dir():
                continue
            try:
                root = base.resolve()
            except (OSError, RuntimeError) as exc:
                problems.append(f'{collection.folder}: {exc}')
                continue
            for scope_dir in _list_dirs(base):
                scope = scope_dir.name
                if not valid_scope(scope):
                    problems.append(f'{collection.folder}/static/artefacts/{scope}: not a scope; ignored')
                    continue
                for kind_dir in _list_dirs(scope_dir):
                    if kind_dir.name not in KINDS:
                        problems.append(f'{collection.folder}/static/artefacts/{scope}/{kind_dir.name}: '
                                        f'not a kind; ignored')
                        continue
                    for artefact_dir in _list_dirs(kind_dir):
                        try:
                            path = ArtefactPath(collection.folder, scope, kind_dir.name, artefact_dir.name)
                        except ValueError as exc:
                            problems.append(f'{collection.folder}/static/artefacts/{scope}/{kind_dir.name}/'
                                            f'{artefact_dir.name}: {exc}; ignored')
                            continue
                        entry = cls._load_entry(store_root, root, path, group_id, problems)
                        if entry is None:
                            continue
                        if entry.key in entries or entry.key in clashes:
                            clashes.add(entry.key)
                            problems.append(f'{entry.ref}: the artefact id is used by more than one kind; '
                                            f'left out')
                            entries.pop(entry.key, None)
                            continue
                        entries[entry.key] = entry
        for problem in problems:
            log.warning('artefact index: %s', problem)
        return cls(entries, problems, time.time())

    @staticmethod
    def _load_entry(store_root: Path, root: Path, path: ArtefactPath, group_id: str,
                    problems: List[str]) -> Optional[ArtefactEntry]:
        folder = store_root / str(path.rel)
        head_path = store_root / str(path.head_rel)
        where = str(path.rel)
        if not head_path.is_file() or not _inside(head_path, root) or not _inside(folder, root):
            problems.append(f'{where}: no head record inside the artefacts folder; left out')
            return None
        try:
            head = parse_head_record(_read_limited(head_path).decode('utf-8'))
        except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
            problems.append(f'{where}/{HEAD_RECORD_NAME}: {exc}; left out')
            return None
        head_problems = validate_head_record(head, path)
        if head_problems:
            problems.append(f'{where}/{HEAD_RECORD_NAME}: ' + '; '.join(head_problems) + '; left out')
            return None
        versions: Dict[int, VersionEntry] = {}
        for item in head['versions']:
            number = item['version']
            version = VersionEntry(
                number=number, state=item['state'], manifest_sha256=item['manifest_sha256'],
                published=item.get('published'), purged=item.get('purged'),
                purged_by=item.get('purged_by') if isinstance(item.get('purged_by'), str) else None,
                purged_by_admin=item.get('purged_by_admin') is True,
                purge_reason=item.get('purge_reason') if isinstance(item.get('purge_reason'), str) else None)
            versions[number] = version
            if version.state != 'live':
                continue
            manifest_path = store_root / str(path.manifest_rel(number))
            if not manifest_path.is_file() or not _inside(manifest_path, root):
                version.problems.append('manifest missing')
            else:
                try:
                    raw = _read_limited(manifest_path)
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest != version.manifest_sha256:
                        version.problems.append(f'manifest sha256 {digest} differs from the head record')
                    else:
                        manifest = json.loads(raw.decode('utf-8'))
                        version.problems.extend(validate_manifest(manifest, path, group_id, number))
                        if not version.problems:
                            version.manifest = manifest
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    version.problems.append(str(exc))
            if version.problems:
                problems.append(f'{where}/v{number}: ' + '; '.join(version.problems) + '; not served')
        return ArtefactEntry(path=path, group_id=group_id, head=head, versions=versions, folder=folder)

    def get(self, group_id: str, scope: str, artefact_id: str) -> Optional[ArtefactEntry]:
        return self.entries.get((group_id, scope, artefact_id))

    def __len__(self) -> int:
        return len(self.entries)


# ── access ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Viewer:
    """Who asks: may they see internal data, are they an admin, which datasets are hidden from them."""
    sees_internal: bool
    is_admin: bool = False
    hidden: frozenset = frozenset()
    user: str = ''


class AccessPolicy:
    """Who sees which artefact (design 3.1). Built per request from the registry's full maps.

    ``served`` maps every served db_id (all of them, not only the caller's) to its metadata;
    ``folder_db_ids`` maps a group folder to the served db_ids inside it.
    """

    def __init__(self, collections: Mapping[str, Collection], served: Mapping[str, Mapping[str, Any]],
                 folder_db_ids: Mapping[str, Sequence[str]]):
        self.collections = collections
        self.served = served
        self.folder_db_ids = folder_db_ids

    def dataset_public(self, db_id: str) -> bool:
        return dataset_access.is_public(self.served.get(db_id))

    def dataset_visible(self, db_id: str, viewer: Viewer) -> bool:
        if db_id not in self.served or db_id in viewer.hidden:
            return False
        return viewer.sees_internal or self.dataset_public(db_id)

    def collection_public(self, group_id: str) -> bool:
        """An analysis collection: its ``access`` (internal unless ``public``) and every dataset in it.
        A dataset collection: public only when it has served datasets, every one is public, and its
        ``index.md`` does not narrow access to internal. Unknown: never public."""
        collection = self.collections.get(group_id)
        if collection is None:
            return False
        db_ids = list(self.folder_db_ids.get(collection.folder) or ())
        if collection.kind == 'analysis':
            if collection.access != 'public':
                return False
            return all(self.dataset_public(db_id) for db_id in db_ids)
        if not db_ids:
            return False
        if collection.access is not None and collection.access != 'public':
            return False
        return all(self.dataset_public(db_id) for db_id in db_ids)

    def collection_visible(self, group_id: str, viewer: Viewer) -> bool:
        if group_id not in self.collections:
            return False
        return viewer.sees_internal or self.collection_public(group_id)

    def is_orphan(self, entry: ArtefactEntry) -> bool:
        """The scope is gone: the dataset is not served (inactive, removed) or the collection is unknown."""
        if entry.group_id not in self.collections:
            return True
        return entry.db_id is not None and entry.db_id not in self.served

    def artefact_visible(self, entry: ArtefactEntry, viewer: Viewer, include_orphans: bool = False) -> bool:
        """Visible exactly when the scope is visible to ``viewer`` and, for an internal artefact, the viewer
        sees internal data. Admins also see artefacts whose scope is gone when ``include_orphans``."""
        if self.is_orphan(entry):
            return viewer.is_admin and include_orphans
        if entry.access_internal and not viewer.sees_internal:
            return False
        if entry.db_id is not None:
            return self.dataset_visible(entry.db_id, viewer)
        return self.collection_visible(entry.group_id, viewer)


# ── delivery helpers ──────────────────────────────────────────────────────────

class LocalVerifier:
    """sha256 of local copies, computed once per (inode, size, mtime) and cached (bounded)."""

    def __init__(self, max_entries: int = 4096):
        self.max_entries = max_entries
        self._cache: 'OrderedDict[str, Tuple[Tuple[int, int, int, int], str]]' = OrderedDict()
        self._lock = threading.Lock()
        self._busy: Dict[str, threading.Lock] = {}

    def digest(self, path: Path) -> Optional[str]:
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        key = str(path)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == signature:
                self._cache.move_to_end(key)
                return cached[1]
            busy = self._busy.setdefault(key, threading.Lock())
        with busy:
            with self._lock:
                cached = self._cache.get(key)
                if cached and cached[0] == signature:
                    return cached[1]
            hasher = hashlib.sha256()
            try:
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
                        hasher.update(chunk)
            except OSError:
                return None
            value = hasher.hexdigest()
            with self._lock:
                self._cache[key] = (signature, value)
                self._cache.move_to_end(key)
                while len(self._cache) > self.max_entries:
                    self._cache.popitem(last=False)
                self._busy.pop(key, None)
            return value

    def verify(self, path: Path, sha256: str, size: int) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
        except OSError:
            return False
        return self.digest(path) == sha256


class DownloadLimiter:
    """Sliding one-minute window per caller key; raises a 429 ArtefactError with Retry-After."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time, max_keys: int = 10000):
        self.per_minute = max(1, int(per_minute))
        self.clock = clock
        self.max_keys = max_keys
        self.calls: Dict[str, List[float]] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = self.clock()
        with self.lock:
            recent = [stamp for stamp in self.calls.get(key, []) if now - stamp < 60]
            if len(recent) >= self.per_minute:
                self.calls[key] = recent
                retry = str(max(1, int(60 - (now - recent[0])) + 1))
                raise ArtefactError(429, 'rate_limited',
                                    f'at most {self.per_minute} artefact downloads per minute; wait a moment',
                                    headers={'Retry-After': retry})
            recent.append(now)
            self.calls[key] = recent
            if len(self.calls) > self.max_keys:
                for stale in [k for k, stamps in self.calls.items() if not stamps or now - stamps[-1] >= 60]:
                    self.calls.pop(stale, None)


@dataclass
class Delivery:
    """How one file is delivered: ``redirect`` (``url``) or ``local`` (``path``)."""
    how: str
    filename: str
    content_type: str
    size: int
    url: Optional[str] = None
    path: Optional[Path] = None


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, 'response', None) or {}
    code = str((response.get('Error') or {}).get('Code') or '') if isinstance(response, dict) else ''
    status = (response.get('ResponseMetadata') or {}).get('HTTPStatusCode') if isinstance(response, dict) else None
    return code in ('404', 'NoSuchKey', 'NotFound') or status == 404


def api_path(entry: ArtefactEntry, version: Optional[int] = None) -> str:
    """The artefact's path relative to the API root (``/artefacts/...``); MCP prints these, never URLs."""
    base = f'/artefacts/{entry.group_id}/{entry.path.scope}/{entry.path.artefact_id}'
    return f'{base}/v{version}' if version is not None else base


class ArtefactService:
    """The read routes' logic. One per server process; the index and collections are swapped at each scan.

    ``s3_factory`` returns a boto3-like client (``head_object``, ``generate_presigned_url``) or None when the
    site cannot read S3; it is called lazily.
    """

    def __init__(self, store_root: Path, settings: Mapping[str, Any],
                 s3_factory: Optional[Callable[[], Any]] = None, clock: Callable[[], float] = time.time):
        self.store_root = Path(store_root)
        self.settings = dict(settings)
        self.s3_factory = s3_factory
        self._s3 = None
        self._s3_lock = threading.Lock()
        self.verifier = LocalVerifier()
        self.limiter = DownloadLimiter(int(self.settings.get('artefacts_download_per_minute', 60)), clock)
        self.downloads: Dict[str, int] = {}
        self.downloads_lock = threading.Lock()

    # S3 --------------------------------------------------------------------
    def s3(self) -> Any:
        if not self.settings.get('s3_readable') or self.s3_factory is None:
            return None
        with self._s3_lock:
            if self._s3 is None:
                self._s3 = self.s3_factory()
            return self._s3

    def object_key(self, rel: PurePosixPath) -> str:
        return s3_key(str(self.settings.get('catalog_prefix') or ''), rel)

    # lookups ---------------------------------------------------------------
    @staticmethod
    def not_found(group_id: str, scope: str, artefact_id: str, version: Optional[str] = None) -> ArtefactError:
        ref = f'{group_id}/{scope}/{artefact_id}'[:200]
        if version:
            ref += f'@{str(version)[:20]}'
        return ArtefactError(404, 'not_found', f'no artefact {ref}', ref=ref)

    def resolve(self, index: ArtefactIndex, policy: AccessPolicy, viewer: Viewer, group_id: str, scope: str,
                artefact_id: str) -> ArtefactEntry:
        """The artefact if the viewer may see it; else the same 404 as for one that does not exist."""
        entry = None
        if GROUP_ID_RE.fullmatch(group_id or '') and valid_scope(scope) and valid_artefact_id(artefact_id):
            entry = index.get(group_id, scope, artefact_id)
        if entry is None or not policy.artefact_visible(entry, viewer, include_orphans=viewer.is_admin):
            raise self.not_found(group_id, scope, artefact_id)
        return entry

    @staticmethod
    def version_of(entry: ArtefactEntry, label: str, viewer: Viewer) -> VersionEntry:
        """The version a ``v<N>`` / ``latest`` label names. 404 unknown, 410 purged (with the tombstone)."""
        try:
            number = parse_version_label(label)
        except ValueError:
            raise ArtefactService.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, label)
        if number is None:
            number = entry.latest
            if number is None:
                if entry.versions and all(v.state == 'purged' for v in entry.versions.values()):
                    raise ArtefactError(410, 'purged', f'every version of {entry.ref} was purged', ref=entry.ref)
                raise ArtefactService.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, label)
        version = entry.versions.get(number)
        if version is None:
            raise ArtefactService.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, label)
        if version.state == 'purged':
            raise ArtefactError(410, 'purged', f'{entry.ref}@v{number} was purged', ref=f'{entry.ref}@v{number}',
                                tombstone=tombstone(version, viewer))
        if version.state == 'purging':
            raise ArtefactError(409, 'purging', f'{entry.ref}@v{number} is being purged',
                                ref=f'{entry.ref}@v{number}')
        if not version.servable:
            raise ArtefactService.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, label)
        return version

    # payloads --------------------------------------------------------------
    def summary(self, entry: ArtefactEntry) -> Dict[str, Any]:
        latest = entry.latest
        manifest = entry.versions[latest].manifest if latest is not None else None
        files = (manifest or {}).get('files') or []
        main = next((f['name'] for f in files if f.get('role') == 'main'), files[0]['name'] if files else None)
        preview = None
        if latest is not None and main is not None:
            preview = self._preview_name(entry, latest, main)
            if preview is not None:
                preview = f'{api_path(entry, latest)}/preview/{preview}'
        published = [v.published for v in entry.versions.values() if v.published]
        purged = [v.purged for v in entry.versions.values() if v.purged]
        return {
            'ref': entry.ref, 'artefact_id': entry.path.artefact_id, 'kind': entry.path.kind,
            'group_id': entry.group_id, 'scope': entry.path.scope, 'db_id': entry.db_id,
            'title': (manifest or {}).get('title') or entry.head.get('title'),
            'latest': latest, 'versions_live': len(entry.live_versions), 'owner': entry.owner,
            'updated': max(published + purged) if (published or purged) else None,
            'size_bytes': sum(int(f.get('size') or 0) for f in files) if manifest else None,
            'main_file': main, 'preview': preview,
            'access': 'internal' if entry.access_internal else 'inherit',
            'path': api_path(entry),
        }

    def files_payload(self, entry: ArtefactEntry, number: int) -> List[Dict[str, Any]]:
        manifest = entry.versions[number].manifest or {}
        out = []
        for item in manifest.get('files') or []:
            preview = self._preview_name(entry, number, item['name'])
            out.append({
                'name': item['name'], 'size': item.get('size'), 'sha256': item.get('sha256'),
                'content_type': item.get('content_type'), 'role': item.get('role'),
                'download': f'{api_path(entry, number)}/files/{item["name"]}',
                'preview': f'{api_path(entry, number)}/preview/{preview}' if preview else None,
            })
        return out

    def detail(self, entry: ArtefactEntry, viewer: Viewer) -> Dict[str, Any]:
        latest = entry.latest
        payload = self.summary(entry)
        payload.update({
            'created': entry.head.get('created'),
            'versions': [version_summary(v) for _, v in sorted(entry.versions.items())],
            'manifest': entry.versions[latest].manifest if latest is not None else None,
            'files': self.files_payload(entry, latest) if latest is not None else [],
        })
        return payload

    def versions_payload(self, entry: ArtefactEntry, viewer: Viewer) -> Dict[str, Any]:
        out = []
        for _, version in sorted(entry.versions.items()):
            if version.state == 'purged':
                out.append(tombstone(version, viewer))
            else:
                out.append(version_summary(version))
        return {'ref': entry.ref, 'latest': entry.latest, 'versions': out}

    def version_payload(self, entry: ArtefactEntry, version: VersionEntry) -> Dict[str, Any]:
        return {'ref': format_ref(entry.group_id, entry.path.scope, entry.path.artefact_id, version.number),
                'version': version.number, 'state': version.state, 'latest': entry.latest,
                'manifest': version.manifest, 'files': self.files_payload(entry, version.number)}

    def listing(self, index: ArtefactIndex, policy: AccessPolicy, viewer: Viewer, *,
                group_id: Optional[str] = None, db_id: Optional[str] = None, kind: Optional[str] = None,
                query: Optional[str] = None, owner: Optional[str] = None, include_purged: bool = False,
                limit: int = 50, cursor: Optional[str] = None, include_orphans: bool = False) -> Dict[str, Any]:
        """``GET /artefacts``: summaries ordered by reference, ``next_cursor`` for the next page."""
        if kind is not None and kind not in KINDS:
            raise ArtefactError(422, 'invalid_kind', f'kind must be one of {", ".join(KINDS)}')
        if db_id is not None and not DB_ID_RE.fullmatch(db_id):
            raise ArtefactError(422, 'invalid_db_id', 'db_id must look like G25/ABC')
        if not 1 <= int(limit) <= 200:
            raise ArtefactError(422, 'invalid_limit', 'limit must be between 1 and 200')
        after = decode_cursor(cursor) if cursor else None
        needle = query.casefold().strip() if query else None
        include_orphans = include_orphans and viewer.is_admin
        rows = []
        for key in sorted(index.entries):
            if after is not None and key <= after:
                continue
            entry = index.entries[key]
            if group_id is not None and entry.group_id != group_id:
                continue
            if db_id is not None and entry.db_id != db_id:
                continue
            if kind is not None and entry.path.kind != kind:
                continue
            if owner is not None and entry.owner != owner:
                continue
            if entry.latest is None and not include_purged:
                continue
            if not policy.artefact_visible(entry, viewer, include_orphans=include_orphans):
                continue
            if needle:
                manifest = entry.versions[entry.latest].manifest if entry.latest is not None else {}
                haystack = ' '.join(str(part) for part in (
                    entry.path.artefact_id, (manifest or {}).get('title') or entry.head.get('title') or '',
                    (manifest or {}).get('description') or '', entry.path.kind)).casefold()
                if needle not in haystack:
                    continue
            rows.append(entry)
            if len(rows) > limit:
                break
        next_cursor = encode_cursor(rows[limit - 1].key) if len(rows) > limit else None
        return {'artefacts': [self.summary(entry) for entry in rows[:limit]], 'next_cursor': next_cursor}

    def count(self, index: ArtefactIndex, policy: AccessPolicy, viewer: Viewer, *, group_id: Optional[str] = None,
              db_id: Optional[str] = None) -> int:
        """Artefacts with a live version the viewer sees, of one collection (all scopes) or one dataset."""
        total = 0
        for entry in index.entries.values():
            if group_id is not None and entry.group_id != group_id:
                continue
            if db_id is not None and entry.db_id != db_id:
                continue
            if entry.latest is not None and policy.artefact_visible(entry, viewer):
                total += 1
        return total

    # files -----------------------------------------------------------------
    @staticmethod
    def manifest_file(version: VersionEntry, name: str) -> Optional[Dict[str, Any]]:
        """The manifest entry named exactly ``name`` (names are looked up, never joined from the request)."""
        if file_name_problem(name):
            return None
        for item in (version.manifest or {}).get('files') or []:
            if item.get('name') == name:
                return item
        return None

    def _local(self, entry: ArtefactEntry, rel: PurePosixPath) -> Optional[Path]:
        """The local path of a store-relative ``rel`` when it is a regular file inside the artefact's folder."""
        candidate = self.store_root / str(rel)
        try:
            folder = entry.folder.resolve()
        except (OSError, RuntimeError):
            return None
        if not candidate.is_file() or not _inside(candidate, folder):
            return None
        return candidate

    def deliver(self, entry: ArtefactEntry, version: VersionEntry, name: str, caller_key: str) -> Delivery:
        """Design 3.2: a presigned GET for exactly one key, else a verified local copy, else
        ``not_on_this_site``. The caller already checked access (``resolve``) and the version."""
        item = self.manifest_file(version, name)
        if item is None:
            raise self.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, f'v{version.number}')
        self.limiter.check(caller_key)
        rel = entry.path.file_rel(version.number, item['name'])
        filename = PurePosixPath(item['name']).name
        content_type = safe_content_type(item.get('content_type'))
        size, sha256 = int(item['size']), item['sha256']
        mode = self.settings.get('artefacts_serve_local', 'fallback')

        def local_copy() -> Optional[Delivery]:
            path = self._local(entry, rel)
            if path is not None and self.verifier.verify(path, sha256, size):
                return Delivery('local', filename, content_type, size, path=path)
            if path is not None:
                log.error('artefact %s@v%s/%s: the local copy does not match the manifest; not served',
                          entry.ref, version.number, name)
            return None

        if mode == 'prefer':
            found = local_copy()
            if found is not None:
                return self._counted(found, caller_key)
        s3_problem = None
        client = None
        try:
            client = self.s3()
        except Exception as exc:  # noqa: BLE001 - a broken client config: fall back to local copies
            log.error('artefacts: cannot build the S3 client: %s', exc)
            s3_problem = 'unavailable'
        if client is not None:
            key = self.object_key(rel)
            bucket = self.settings['bucket']
            try:
                head = client.head_object(Bucket=bucket, Key=key)
                stored_sha = ((head.get('Metadata') or {}).get('sha256') or '').lower()
                if int(head.get('ContentLength', -1)) != size or (stored_sha and stored_sha != sha256):
                    log.error('artefact %s@v%s/%s: the S3 object does not match the manifest (size %s, sha256 %s)',
                              entry.ref, version.number, name, head.get('ContentLength'), stored_sha or '-')
                    s3_problem = 'integrity'
                else:
                    url = presign_get(client, bucket, key, filename, content_type,
                                      int(self.settings.get('artefacts_url_ttl_seconds', 300)))
                    return self._counted(Delivery('redirect', filename, content_type, size, url=url), caller_key)
            except Exception as exc:  # noqa: BLE001 - boto3 raises many types
                if _is_not_found(exc):
                    s3_problem = 'missing'
                else:
                    log.error('artefact %s@v%s/%s: S3 HEAD failed: %s', entry.ref, version.number, name, exc)
                    s3_problem = 'unavailable'
        if mode != 'prefer':
            found = local_copy()
            if found is not None:
                return self._counted(found, caller_key)
        ref = f'{entry.ref}@v{version.number}'
        if s3_problem == 'integrity':
            raise ArtefactError(502, 'integrity_error', f'the stored copy of {name} does not match {ref}; '
                                                        f'an admin has been told', ref=ref, file=name)
        if s3_problem == 'unavailable':
            raise ArtefactError(503, 'storage_unavailable', 'the object store did not answer; try again later',
                                ref=ref, file=name, headers={'Retry-After': '30'})
        raise ArtefactError(404, 'not_on_this_site', f'{name} of {ref} is not available on this site', ref=ref,
                            file=name)

    def _counted(self, delivery: Delivery, caller_key: str) -> Delivery:
        with self.downloads_lock:
            self.downloads[caller_key] = self.downloads.get(caller_key, 0) + 1
        return delivery

    def download_stats(self) -> Dict[str, Any]:
        with self.downloads_lock:
            return {'downloads': sum(self.downloads.values()), 'callers': len(self.downloads)}

    def _preview_name(self, entry: ArtefactEntry, number: int, file_name: str) -> Optional[str]:
        for suffix in PREVIEW_TYPES:
            candidate = f'{file_name}{suffix}'
            if file_name_problem(candidate):
                continue
            if self._local(entry, entry.path.preview_rel(number, candidate)) is not None:
                return candidate
        return None

    def preview(self, entry: ArtefactEntry, version: VersionEntry, name: str) -> Tuple[Path, str]:
        """A local preview: ``name`` must be ``<manifest file name>.webp`` or ``.txt``; the type is fixed."""
        for item in (version.manifest or {}).get('files') or []:
            for suffix, content_type in PREVIEW_TYPES.items():
                if name == f'{item["name"]}{suffix}' and not file_name_problem(name):
                    path = self._local(entry, entry.path.preview_rel(version.number, name))
                    if path is not None:
                        return path, content_type
        raise self.not_found(entry.group_id, entry.path.scope, entry.path.artefact_id, f'v{version.number}')


def presign_get(client: Any, bucket: str, key: str, filename: str, content_type: str, ttl: int) -> str:
    """A presigned GET for exactly ``key``: attachment disposition and content type fixed in the signature,
    so the bucket never decides how a browser renders the bytes. No ACL parameter is ever sent."""
    if '"' in filename or '\\' in filename or not filename.isascii():
        raise ValueError(f'unsafe file name {filename!r}')
    return client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key,
                'ResponseContentDisposition': f'attachment; filename="{filename}"',
                'ResponseContentType': safe_content_type(content_type)},
        ExpiresIn=int(ttl), HttpMethod='GET')


def version_summary(version: VersionEntry) -> Dict[str, Any]:
    out: Dict[str, Any] = {'version': version.number, 'state': version.state, 'published': version.published}
    if version.state == 'live' and not version.servable:
        out['state'] = 'unavailable'
    if version.purged:
        out['purged'] = version.purged
    return out


def tombstone(version: VersionEntry, viewer: Viewer) -> Dict[str, Any]:
    """A purged version as anyone may see it; who purged is "an admin" for non-admins when an admin did."""
    purged_by = version.purged_by
    if version.purged_by_admin and not viewer.is_admin:
        purged_by = 'an admin'
    return {'version': version.number, 'state': 'purged', 'published': version.published,
            'purged': version.purged, 'purged_by': purged_by, 'purge_reason': version.purge_reason}


def encode_cursor(key: Tuple[str, str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode('utf-8')).decode('ascii').rstrip('=')


def decode_cursor(cursor: str) -> Tuple[str, str, str]:
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
        if isinstance(value, list) and len(value) == 3 and all(isinstance(part, str) for part in value):
            return (value[0], value[1], value[2])
    except (ValueError, binascii.Error, UnicodeError):
        pass
    raise ArtefactError(422, 'invalid_cursor', 'the cursor is not one this server handed out')


def caller_key(user: str, headers: Mapping[str, str], peer: str) -> str:
    """Rate-limit key: the signed-in user, else the address Caddy saw (last X-Forwarded-For entry)."""
    if user:
        return f'user:{user}'
    forwarded = (headers.get('x-forwarded-for') or '').split(',')
    address = forwarded[-1].strip() if forwarded and forwarded[-1].strip() else peer
    return f'anon:{address}'


def folder_db_ids(db_extra: Mapping[str, Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Group folder -> served db_ids, from the catalog scan's ``rel_path`` of each dataset."""
    out: Dict[str, List[str]] = {}
    for db_id, extra in db_extra.items():
        rel = str((extra or {}).get('rel_path') or '')
        parts = PurePosixPath(rel.replace(os.sep, '/')).parts
        if len(parts) > 1:
            out.setdefault(parts[0], []).append(db_id)
    return out


def dataset_group_ids(db_extra: Mapping[str, Mapping[str, Any]]) -> Dict[str, Set[str]]:
    """Group folder -> the group ids its served datasets use."""
    out: Dict[str, Set[str]] = {}
    for _db_id, extra in db_extra.items():
        rel = str((extra or {}).get('rel_path') or '')
        parts = PurePosixPath(rel.replace(os.sep, '/')).parts
        if len(parts) > 1 and (extra or {}).get('group_id'):
            out.setdefault(parts[0], set()).add(str(extra['group_id']))
    return out
