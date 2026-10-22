"""Artefact and collection deposits through the API (doc/DESIGN_artefacts.md, phase A2, REST 2.47).

An artefact deposit is a dataset deposit with ``deposit_kind: "artefact"``: the same records, key locks,
rate limiter, per-user limits, staging prefix, presigned uploads, verified complete (``CopySourceIfMatch``
plus the MD5 check) and guarded deletes as ``lib/dataset_deposit.py``. Only the rules differ:

* who: members of ``artefacts_upload_groups`` (and, the design's default, of ``datasets_upload_groups``),
  admins always; a collection deposit: ``collections_create_groups``, admins always (the handler checks);
* what: an ``artefact`` block (kind, id, title, description, access, ``made_from``, declared provenance) and
  its files; target keys ``<incoming prefix><data_project>/artefacts/<deposit_id>/<name>``, a folder the
  dataset route never reads (``ingest_pipeline.plan_items`` skips it);
* a collection deposit has no files to upload: the server renders the minimal ``index.md`` (design 2.6),
  assigns the group id and writes it to ``<prefix><data_project>/artefacts/<deposit_id>/index.md`` itself.

The watcher side (QC, placement, versions, purge) is ``lib/artefact_pipeline.py``. The shared rules (kind
allowlists, magic bytes, provenance, held reasons, group id assignment, ``index.md`` rendering) live here so
the server can refuse early and the watcher checks again with the same code.

Input: the caller's identity (``dataset_deposit.Identity``), the request body and an
:class:`ArtefactContext` (the collection registry, the artefact index and the access policy of the request).
Output: JSON-able dicts; refusals raise ``dataset_deposit.DepositError``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

import artefacts
from dataset_deposit import (DEPOSIT_ID_RE, MiB, SHA256_RE, SINGLE_PUT_HARD_MAX, DepositError, DepositService,
                             Identity, _error_code, _iso, choose_part_size, clean_note, new_deposit_id, part_sizes)

ARTEFACT_DEPOSIT_KINDS = ('artefact', 'collection')
#: new obs columns for a published dataset from one TSV (REST 2.47.0, design section 26 of the ingestion design)
OBS_COLUMNS_KIND = 'obs_columns'
OBS_HEADER_BYTES = 64 * 1024
#: the folder below ``<incoming prefix><data_project>/`` that only artefact and collection deposits use
INCOMING_DIR = 'artefacts'
#: kinds not accepted yet (none since A3: ``de_table`` artefacts are imported into the dataset's DE tables)
DEFERRED_KINDS: Tuple[str, ...] = ()
#: ``deposit_kind: "de_only"`` (2.46) is accepted as an alias of a ``de_table`` artefact until this release
DE_ONLY_ALIAS_UNTIL = '2.48.0'
#: top-level file names a deposit may not use: vark and pyvark write the version's manifest beside the
#: downloaded files as manifest.json (pyvark falls back to _manifest.json)
RESERVED_FILE_NAMES = ('manifest.json', '_manifest.json')

#: allowed file endings per kind (design 2.1); ``other`` takes any of them plus plain data formats
KIND_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    'de_table': ('.tsv', '.csv', '.tsv.gz', '.csv.gz', '.mus'),
    'figure': ('.png', '.jpg', '.jpeg', '.webp', '.svg', '.pdf', '.tif', '.tiff'),
    'table': ('.tsv', '.csv', '.tsv.gz', '.csv.gz', '.xlsx', '.parquet'),
    'notebook': ('.ipynb', '.qmd', '.rmd', '.html'),
    'report': ('.pdf', '.html', '.md', '.docx'),
    'model': ('.pt', '.onnx', '.pkl', '.rds', '.h5', '.joblib', '.zip'),
}
KIND_EXTENSIONS['other'] = tuple(sorted({ext for kind, exts in KIND_EXTENSIONS.items() if kind != 'de_table'
                                         for ext in exts} | {'.txt', '.json', '.yaml', '.yml', '.gz'}))
#: endings read as text (utf-8, no NUL) by the content checks and the secret scan
TEXT_EXTENSIONS = ('.tsv', '.csv', '.svg', '.qmd', '.rmd', '.html', '.md', '.txt', '.json', '.yaml', '.yml',
                   '.ipynb')
#: table endings the donor identifier scan reads
TABLE_EXTENSIONS = ('.tsv', '.csv', '.tsv.gz', '.csv.gz')
#: content types the server records (the extension decides; the uploader's value is never used)
CONTENT_TYPES = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
    '.svg': 'image/svg+xml', '.pdf': 'application/pdf', '.tif': 'image/tiff', '.tiff': 'image/tiff',
    '.tsv': 'text/tab-separated-values', '.csv': 'text/csv', '.tsv.gz': 'application/gzip',
    '.csv.gz': 'application/gzip', '.gz': 'application/gzip',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.parquet': 'application/vnd.apache.parquet', '.ipynb': 'application/x-ipynb+json', '.qmd': 'text/markdown',
    '.rmd': 'text/markdown', '.html': 'text/html', '.md': 'text/markdown',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pt': 'application/octet-stream', '.onnx': 'application/octet-stream', '.pkl': 'application/octet-stream',
    '.rds': 'application/octet-stream', '.h5': 'application/x-hdf5', '.joblib': 'application/octet-stream',
    '.zip': 'application/zip', '.txt': 'text/plain', '.json': 'application/json', '.yaml': 'text/yaml',
    '.yml': 'text/yaml', '.mus': 'text/plain',
}
#: kinds whose provenance gaps are errors (``other``: a warning)
PROVENANCE_STRICT_KINDS = ('de_table', 'figure', 'table', 'notebook', 'report', 'model')
TITLE_MIN_CHARS = 5
DESCRIPTION_MIN_CHARS = 50
ARTEFACT_BLOCK_MAX_BYTES = 256 * 1024
FILE_ROLES = ('main', 'supporting', 'sidecar')
#: held reasons of artefact deposits (an admin decides)
HELD_REASONS = {
    'public_access': 'the scope or the artefact is public: an admin publishes it',
    'possible_secret': 'a text file looks like it holds a key, token or password',
    'donor_identifiers': 'a table holds values of the donor crosswalk (internal donor ids)',
    'made_from_internal': 'a public artefact made from an internal dataset: an admin decides',
    'not_scanned': 'a text or table file is larger than the content check limit and was not scanned',
    'de_not_checked': 'a DE table too large for one of its checks (duplicates): an admin looks at it',
}
COLLECTION_TITLE_MAX = artefacts.TITLE_MAX_CHARS
MAX_AUTHORS = 100
DOI_RE = re.compile(r'10\.\d{4,9}/\S{1,200}')
CITEKEY_RE = re.compile(r'[A-Za-z][A-Za-z0-9_:.-]{0,80}')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
COMMIT_RE = re.compile(r'[0-9a-f]{7,64}')
ORCID_RE = re.compile(r'\d{4}-\d{4}-\d{4}-\d{3}[\dX]')
VERSION_RE = re.compile(r'\d{1,4}\.\d{1,4}\.\d{1,6}')
USER_RE = re.compile(r'[A-Za-z0-9._@-]{1,80}')


# ── shared rules ──────────────────────────────────────────────────────────────

def file_extension(name: str) -> str:
    """The ending that decides the type: ``.tsv.gz`` style double endings first, lower case."""
    lower = name.lower()
    for double in ('.tsv.gz', '.csv.gz'):
        if lower.endswith(double):
            return double
    dot = lower.rfind('.')
    slash = lower.rfind('/')
    return lower[dot:] if dot > slash + 1 else ''


def extension_problem(kind: str, name: str) -> Optional[str]:
    """Why ``name`` is not allowed for ``kind`` (its ending), or None."""
    allowed = KIND_EXTENSIONS.get(kind)
    if allowed is None:
        return f'unknown kind {kind!r}'
    ending = file_extension(name)
    if kind == 'de_table' and name.lower().endswith('.mus'):
        return None
    if ending not in allowed:
        return f'{name}: a {kind} file ends in one of {", ".join(allowed)}'
    return None


def content_type_for(name: str) -> str:
    """The content type recorded for a file: by its ending, active types (html, svg, xml, ...) as
    application/octet-stream (artefacts.safe_content_type)."""
    return artefacts.safe_content_type(CONTENT_TYPES.get(file_extension(name), 'application/octet-stream'))


def magic_problem(name: str, head: bytes, tail: bytes, size: int) -> Optional[str]:
    """Why the first bytes (``head``, at least 64 kB or the whole file) and the last 8 bytes (``tail``) do not
    agree with the file's ending, or None. Formats without a reliable signature are not checked here."""
    ending = file_extension(name)
    if size == 0:
        return f'{name} is empty'

    def starts(*signatures: bytes) -> bool:
        return any(head.startswith(signature) for signature in signatures)
    if ending == '.png' and not starts(b'\x89PNG\r\n\x1a\n'):
        return f'{name} is not a PNG image'
    if ending in ('.jpg', '.jpeg') and not starts(b'\xff\xd8\xff'):
        return f'{name} is not a JPEG image'
    if ending == '.webp' and not (head[:4] == b'RIFF' and head[8:12] == b'WEBP'):
        return f'{name} is not a WebP image'
    if ending in ('.tif', '.tiff') and not starts(b'II*\x00', b'MM\x00*'):
        return f'{name} is not a TIFF image'
    if ending == '.pdf' and not starts(b'%PDF-'):
        return f'{name} is not a PDF'
    if ending in ('.zip', '.xlsx', '.docx') and not starts(b'PK\x03\x04', b'PK\x05\x06'):
        return f'{name} is not a ZIP container'
    if ending in ('.gz', '.tsv.gz', '.csv.gz') and not starts(b'\x1f\x8b'):
        return f'{name} is not gzip compressed'
    if ending == '.parquet' and not (head[:4] == b'PAR1' and tail[-4:] == b'PAR1'):
        return f'{name} is not a parquet file'
    if ending == '.h5' and not starts(b'\x89HDF\r\n\x1a\n'):
        return f'{name} is not an HDF5 file'
    if ending in TEXT_EXTENSIONS:
        sample = head[:65536]
        if b'\x00' in sample:
            return f'{name} is not text (it holds NUL bytes)'
        try:
            sample.decode('utf-8')
        except UnicodeDecodeError as exc:
            if not (len(head) >= 65536 and exc.start >= len(sample) - 4):      # a character cut at the edge
                return f'{name} is not UTF-8 text'
    if ending in ('.json', '.ipynb') and len(head) >= size:
        try:
            data = json.loads(head.decode('utf-8'))
        except ValueError:
            return f'{name} is not valid JSON'
        if ending == '.ipynb' and not (isinstance(data, dict) and 'cells' in data and 'nbformat' in data):
            return f'{name} is not a Jupyter notebook (no cells / nbformat)'
    return None


SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY'),
    re.compile(rb'(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])'),
    re.compile(rb'glpat-[0-9A-Za-z_-]{20,}'),
    re.compile(rb'gh[pousr]_[0-9A-Za-z]{36,}'),
    re.compile(rb'xox[abpors]-[0-9A-Za-z-]{10,}'),
    re.compile(rb'(?im)^[ \t"\']*(?:aws_secret_access_key|secret_access_key|password|passwd|api[_-]?key|'
               rb'secret|token)[ \t"\']*[=:][ \t"\']*[^\s"\'#]{6,}'),
)


def secret_findings(data: bytes) -> List[str]:
    """Kinds of possible secrets in ``data`` (never the secret itself)."""
    names = ('private key', 'AWS access key id', 'GitLab token', 'GitHub token', 'Slack token',
             'password or secret assignment')
    return [names[index] for index, pattern in enumerate(SECRET_PATTERNS) if pattern.search(data)]


def provenance_missing(declared: Any) -> List[str]:
    """Declared provenance keys that are missing or malformed (design 6.1)."""
    if not isinstance(declared, dict):
        return ['declared']
    missing = []
    author = declared.get('author')
    if not (isinstance(author, dict) and isinstance(author.get('name'), str) and author['name'].strip()):
        missing.append('declared.author.name')
    elif author.get('orcid') is not None and not (isinstance(author['orcid'], str)
                                                  and ORCID_RE.fullmatch(author['orcid'])):
        missing.append('declared.author.orcid (0000-0000-0000-0000)')
    created = declared.get('created')
    if not (isinstance(created, str) and DATE_RE.fullmatch(created)):
        missing.append('declared.created (YYYY-MM-DD)')
    software = declared.get('software')
    if not (isinstance(software, list) and software and all(
            isinstance(entry, dict) and isinstance(entry.get('name'), str) and entry['name'].strip()
            and isinstance(entry.get('version'), str) and entry['version'].strip() for entry in software)):
        missing.append('declared.software (at least one {name, version})')
    code = declared.get('code')
    if isinstance(code, dict) and isinstance(code.get('none'), str) and code['none'].strip():
        pass
    elif isinstance(code, dict) and isinstance(code.get('repository'), str) and code['repository'].strip() \
            and isinstance(code.get('commit'), str) and COMMIT_RE.fullmatch(code['commit']):
        pass
    else:
        missing.append('declared.code ({repository, commit} or {none: reason})')
    return missing


def _implied_first(entries: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The version before the first changelog entry when that entry is a patch bump (``1.0.1`` follows ``1.0.0``):
    a dataset published before it had a changelog keeps its first version known."""
    if not entries:
        return None
    parts = str(entries[0].get('version') or '').split('.')
    if len(parts) == 3 and all(part.isdigit() for part in parts) and int(parts[2]) > 0:
        return f'{parts[0]}.{parts[1]}.{int(parts[2]) - 1}'
    return None


def changelog_versions(meta: Mapping[str, Any]) -> List[str]:
    """Every version a dataset ``.md`` knows: the one implied before its first patch entry, its changelog
    entries and its current version."""
    entries = [entry for entry in (meta.get('changelog') or []) if isinstance(entry, dict)]
    versions = [str(entry.get('version')) for entry in entries if entry.get('version') is not None]
    implied = _implied_first(entries)
    if implied and implied not in versions:
        versions.insert(0, implied)
    if meta.get('version') is not None:
        versions.append(str(meta['version']))
    return versions


def h5ad_sha256_at(meta: Mapping[str, Any], version: str) -> Optional[str]:
    """The sha256 of the h5ad a dataset had at ``version``: the ``previous_sha256`` of the first later changelog
    entry that changed the data, else the current ``sha256``. None for a version the ``.md`` does not know."""
    entries = [entry for entry in (meta.get('changelog') or []) if isinstance(entry, dict)]
    current = str(meta.get('version')) if meta.get('version') is not None else None
    names = [str(entry.get('version')) for entry in entries]
    if version not in names and version != current:
        if version != _implied_first(entries):
            return None
        later = entries                                   # the implied first version precedes every entry
    else:
        later = entries[names.index(version) + 1:] if version in names else []
    for entry in later:
        previous = str(entry.get('previous_sha256') or '').lower()
        if SHA256_RE.fullmatch(previous):
            return previous
    sha = str(meta.get('sha256') or '').lower()
    return sha if SHA256_RE.fullmatch(sha) else None


def assign_group_id(data_project: str, taken: Iterable[str]) -> str:
    """A 3-character group id for a new collection: first letter and the two year digits (``F26``), else the
    first letter, another letter of the name and the last year digit, else the first letter, any letter and
    the last digit. Unique against ``taken`` ignoring case."""
    used = {str(value).upper() for value in taken if value}
    first = data_project[0].upper()
    year = data_project[-4:]
    candidates = [first + year[-2:]]
    letters = [char.upper() for char in data_project[1:-4] if char.isalpha()]
    candidates += [first + letter + year[-1] for letter in letters]
    candidates += [first + chr(code) + year[-1] for code in range(ord('A'), ord('Z') + 1)]
    candidates += [first + chr(code) + digit for code in range(ord('A'), ord('Z') + 1) for digit in '0123456789']
    for candidate in candidates:
        if candidate.upper() not in used and artefacts.GROUP_ID_RE.fullmatch(candidate):
            return candidate
    raise DepositError(409, 'collection_exists', 'no free group id for this name; ask an admin')


def _escape_raw_html(text: str) -> str:
    """Defence in depth for index.md: if a raw HTML span is left (create refuses them; this covers direct
    callers), write its '<' as '&lt;'. Code spans and blocks are untouched, so they render as written."""
    import safe_markdown  # noqa: PLC0415
    for part in set(safe_markdown.raw_html(text)):
        text = text.replace(part, part.replace('<', '&lt;'))
    return text


def render_analysis_index_md(data_project: str, group_id: str, collection: Mapping[str, Any], created_by: str,
                             created: str) -> str:
    """The minimal ``index.md`` of an analysis collection (design 2.6), front matter in a fixed order."""
    meta: Dict[str, Any] = {
        'group': data_project, 'group_id': group_id, 'collection_kind': 'analysis',
        'title': collection['title'], 'authors': list(collection['authors']), 'year': int(data_project[-4:]),
        'access': collection.get('access') or 'internal', 'created_by': created_by, 'created': created,
    }
    for key in ('doi', 'paper_title', 'license'):
        if collection.get(key):
            meta[key] = collection[key]
    if collection.get('papers'):
        meta['papers'] = dict(collection['papers'])
    # the description is a depositor's markdown; raw HTML is refused at create (safe_markdown.raw_html), and
    # what is left over (a bare "<" not starting a tag) is escaped only outside code, as markdown shows it
    body = _escape_raw_html(collection.get('description') or '').strip()
    text = '---\n' + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10 ** 6) + '---\n'
    return text + (f'\n{body}\n' if body else '')


def incoming_folder(prefix: str, data_project: str, deposit_id: str) -> str:
    if not DEPOSIT_ID_RE.fullmatch(deposit_id or '') or not artefacts.valid_segment(data_project):
        raise ValueError(f'invalid incoming folder {data_project!r}/{deposit_id!r}')
    return f'{prefix}{data_project}/{INCOMING_DIR}/{deposit_id}/'


def deposit_of_incoming_key(prefix: str, key: str) -> Optional[Tuple[str, str, str]]:
    """``(data_project, deposit_id, name)`` of a key below ``<prefix><data_project>/artefacts/``, else None."""
    if not key.startswith(prefix):
        return None
    parts = key[len(prefix):].split('/')
    if len(parts) < 4 or parts[1] != INCOMING_DIR:
        return None
    return parts[0], parts[2], '/'.join(parts[3:])


# ── the request context ───────────────────────────────────────────────────────

@dataclass
class ArtefactContext:
    """What the server knows when a deposit is created: the registry, the index and the caller's view."""
    store_root: Path
    collections: Mapping[str, artefacts.Collection]       # group_id -> collection
    index: artefacts.ArtefactIndex
    policy: artefacts.AccessPolicy
    viewer: artefacts.Viewer
    dataset_md: Mapping[str, Path] = field(default_factory=dict)        # served db_id -> its .md
    dataset_folder: Mapping[str, str] = field(default_factory=dict)     # served db_id -> group folder
    #: the obs column names a served dataset shows (obs-columns deposits: the case-insensitive clash check)
    dataset_obs: Optional[Callable[[str], Sequence[str]]] = None

    def collection_by_folder(self, folder: str) -> Optional[artefacts.Collection]:
        return next((c for c in self.collections.values() if c.folder == folder), None)


def read_md_meta(path: Path) -> Dict[str, Any]:
    from metadata import parse_md_file  # noqa: PLC0415
    try:
        return parse_md_file(Path(path))[0] or {}
    except Exception:  # noqa: BLE001 - an unreadable .md knows no versions
        return {}


# ── the service ───────────────────────────────────────────────────────────────

PURGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS artefact_purges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL, version INTEGER NOT NULL, group_folder TEXT NOT NULL,
    scope TEXT NOT NULL, kind TEXT NOT NULL, artefact_id TEXT NOT NULL, requested_by TEXT NOT NULL,
    by_admin INTEGER DEFAULT 0, reason TEXT, state TEXT NOT NULL, requested REAL, updated REAL, detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS artefact_purges_open ON artefact_purges(ref, version)
    WHERE state IN ('requested', 'purging');
"""
#: what a purge request asks (2.47.0 two-stage purge, design 5.3): ``soft`` marks the version purged (files kept),
#: ``unpurge`` reverses it, ``hard`` (admins) deletes a purged version. Rows written before the column existed
#: were hard purges and stay so (the column's default).
PURGE_ACTIONS = ('soft', 'unpurge', 'hard')


def ensure_purge_schema(conn: Any) -> None:
    """The purge request table with its ``action`` column (added to an older table with default ``hard``)."""
    conn.executescript(PURGE_SCHEMA)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(artefact_purges)')}
    if 'action' not in columns:
        conn.execute("ALTER TABLE artefact_purges ADD COLUMN action TEXT NOT NULL DEFAULT 'hard'")


class ArtefactDepositService(DepositService):
    """``DepositService`` plus the artefact and collection deposits. One instance per server process.

    Dataset deposits behave exactly as in the base class (they still need ``datasets_deposit_enabled``);
    artefact and collection deposits need ``artefacts_enabled`` and the deposit S3 credentials.
    """

    def __init__(self, *args: Any, artefact_settings: Optional[Mapping[str, Any]] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.artefact_settings = dict(artefact_settings or {})
        with self.store._lock:
            ensure_purge_schema(self.store.conn)

    # switches --------------------------------------------------------------
    @property
    def artefacts_active(self) -> bool:
        return bool(self.artefact_settings.get('artefacts_enabled')) and self.settings.credentials_present

    def _require_artefacts_active(self) -> None:
        if not self.artefacts_active:
            raise DepositError(503, 'disabled', 'Artefact deposits are switched off on this site '
                                                '(artefacts_enabled, or no S3 credentials for presigning).')

    def _require_active(self) -> None:
        if not (self.settings.active or self.artefacts_active):
            super()._require_active()

    def _require_active_for(self, record: Mapping[str, Any]) -> None:
        if record.get('deposit_kind') in ARTEFACT_DEPOSIT_KINDS:
            self._require_artefacts_active()
        elif not self.settings.active:
            super()._require_active()

    def _limit(self, key: str, default: float) -> float:
        try:
            return float(self.artefact_settings.get(key, default))
        except (TypeError, ValueError):
            return default

    # the generic routes, kind-aware ----------------------------------------
    def create(self, identity: Identity, body: Dict[str, Any], context: Optional[ArtefactContext] = None
               ) -> Dict[str, Any]:
        kind = body.get('deposit_kind') if isinstance(body, dict) else None
        if kind == 'artefact':
            return self.create_artefact(identity, body, context)
        if kind == 'collection':
            return self.create_collection(identity, body, context)
        if kind == OBS_COLUMNS_KIND:
            return self.create_obs_columns(identity, body, context)
        if kind not in (None, 'dataset', 'de_only'):
            raise DepositError(422, 'invalid', 'deposit_kind is dataset, artefact, collection or obs_columns')
        if (kind == 'de_only' or is_de_only_body(body)) and self.artefacts_active:
            return self.create_de_only_alias(identity, body, context)
        if not self.settings.active:
            super()._require_active()
        self._check_dataset_against_collections(identity, body)
        body = {key: value for key, value in body.items() if key != 'deposit_kind'}
        return super().create(identity, body)

    def _check_dataset_against_collections(self, identity: Identity, body: Any) -> None:
        """Review C10: a dataset deposit that would create a new group while a collection of that name is being
        created waits (409 collection_pending); a dataset deposit into an analysis collection is accepted only
        from its creator or an admin (design default: analysis collections hold artefacts)."""
        data_project = body.get('data_project') if isinstance(body, dict) else None
        if not isinstance(data_project, str) or not artefacts.valid_segment(data_project):
            return                                   # the base class refuses it with its own message
        folder = self.store_root / data_project
        index = folder / 'index.md'
        if not index.is_file():
            if self._pending_collection(data_project):
                raise DepositError(409, 'collection_pending', f'a collection named {data_project} is being created; '
                                                              f'deposit the dataset once it is live (or choose '
                                                              f'another name)')
            return
        meta = read_md_meta(index)
        if str(meta.get('collection_kind') or '').strip().lower() == 'analysis' and not identity.is_admin \
                and meta.get('created_by') != identity.user:
            raise DepositError(403, 'forbidden', f'{data_project} is an analysis collection: datasets are added to it '
                                                 f'by its creator or an admin')

    def refresh_urls(self, identity: Identity, deposit_id: str, body: Any) -> Dict[str, Any]:
        record = self._get_own(identity, deposit_id)
        self._require_active_for(record)
        if record.get('deposit_kind') in ARTEFACT_DEPOSIT_KINDS and isinstance(body, dict) \
                and ('size' in body or 'sha256' in body):
            raise DepositError(422, 'invalid', 'artefact files cannot be declared again; withdraw and deposit again')
        return super().refresh_urls(identity, deposit_id, body)

    def complete(self, identity: Identity, deposit_id: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._require_active_for(self._get_own(identity, deposit_id))
        return super().complete(identity, deposit_id, body)

    def withdraw(self, identity: Identity, deposit_id: str) -> Dict[str, Any]:
        self._require_active_for(self._get_own(identity, deposit_id))
        return super().withdraw(identity, deposit_id)

    def _verify_for_complete(self, record: Dict[str, Any], deposit_id: str):
        if record.get('deposit_kind') not in ARTEFACT_DEPOSIT_KINDS + (OBS_COLUMNS_KIND,):
            return super()._verify_for_complete(record, deposit_id)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        try:
            results = [self._finish_upload(deposit_id, row) for row in rows.values()]
        except Exception as exc:  # noqa: BLE001
            raise DepositError(502, 's3_error', f'completing the upload failed ({_error_code(exc)}); try again')
        incomplete = [entry for entry in results if not entry['ok']]
        if incomplete:
            raise DepositError(409, 'incomplete', 'not every file is uploaded completely', files=incomplete)
        rows = {row['name']: row for row in self.store.files(deposit_id)}
        if record.get('deposit_kind') == OBS_COLUMNS_KIND:
            self._check_obs_header(record, rows)
        if record.get('deposit_kind') == 'artefact' and json.loads(record.get('artefact') or '{}').get('kind') == \
                'de_table':
            from dataset_deposit import StagingChanged  # noqa: PLC0415
            try:
                errors = self._check_de_table_sidecars(record, rows)
            except StagingChanged:
                raise self._staging_changed_error()
            except DepositError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DepositError(502, 's3_error', f'reading a sidecar back failed ({_error_code(exc)})')
            if errors:
                raise DepositError(422, 'de_mismatch', '; '.join(errors) + '. Withdraw and deposit the tables again '
                                                                           'with corrected sidecars.')
        self._check_keys(Identity(user=record['user'], is_admin=bool(record['is_admin'])), list(rows.values()))
        return rows, []

    def _check_de_table_sidecars(self, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> List[str]:
        """Errors of the sidecars of a ``de_table`` deposit (read back from staging, verified ETag): the keys
        their format needs, the scope dataset named, one study id per table."""
        from ingest_pipeline import de_sidecar_names, de_sidecar_problems, de_study_id, parse_sidecar_text  # noqa: PLC0415
        block = json.loads(record.get('artefact') or '{}')
        db_id = str(record.get('update_of') or '')
        file_id = db_id.split('/')[-1]
        errors: List[str] = []
        studies: Dict[str, str] = {}
        for name, row in rows.items():
            if not name.endswith('.mus'):
                continue
            meta = parse_sidecar_text(self._read_staged(row))
            errors += [f'{name}: {problem}' for problem in de_sidecar_problems(meta)]
            if not de_sidecar_names(meta, file_id, block.get('dataset_basename') or '', db_id):
                errors.append(f'{name} names neither de_dataset_id={file_id} (or {db_id}) nor '
                              f'de_dataset_h5ad={block.get("dataset_basename")}.h5ad')
            table = name[:-len('.mus')]
            study = de_study_id(meta, PurePosixPath(table).name)
            if study in studies:
                errors.append(f'{table} and {studies[study]} have the same study id {study}')
            studies[study] = table
        return errors

    def _view(self, record: Dict[str, Any], detail: bool, identity: Identity) -> Dict[str, Any]:
        view = super()._view(record, detail, identity)
        if record.get('deposit_kind') == 'artefact':
            block = json.loads(record.get('artefact') or '{}')
            version = view.get('version')
            view['artefact'] = {'ref': record.get('artefact_ref'), 'kind': block.get('kind'),
                                'new_version_of': block.get('new_version_of'),
                                'version': version if view['state'] == 'live' else None}
            if view['state'] == 'live' and version:
                view['artefact']['ref_version'] = f'{record.get("artefact_ref")}@{version}'
        elif record.get('deposit_kind') == 'collection':
            view['collection'] = {'data_project': record['data_project'], 'group_id': record.get('group_id')}
        elif record.get('deposit_kind') == OBS_COLUMNS_KIND:
            import obs_columns  # noqa: PLC0415
            specs, replace = obs_columns.spec_from_record(record)
            view['obs_columns'] = {'db_id': record.get('update_of'), 'replace': replace,
                                   'columns': [{'name': spec['name'], 'dtype': spec['dtype'],
                                                'replaces': bool(spec.get('replaces'))} for spec in specs]}
        return view

    # create: artefact ------------------------------------------------------
    def _pending_kind_conflict(self, column: str, value: str, exclude: str = '') -> Callable[[sqlite3.Connection], None]:
        """An ``extra_check`` for ``reserve``: no other deposit with the same artefact ref (or collection name)
        is open or waiting for the watcher."""
        def check(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                f"SELECT id, user FROM deposits WHERE UPPER({column}) = UPPER(?) AND id != ? AND ("
                f"state IN ('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') "
                f"NOT IN ('live', 'rejected', 'failed')))", (value, exclude)).fetchone()
            if row is not None:
                raise DepositError(409, 'busy', 'another deposit of the same artefact or collection is open or '
                                                'being published; wait until it is live, or withdraw it')
        return check

    def _artefact_block(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        block = body.get('artefact')
        if not isinstance(block, dict):
            raise DepositError(422, 'invalid', 'an artefact deposit carries an "artefact" object')
        if len(json.dumps(block, default=str).encode('utf-8')) > ARTEFACT_BLOCK_MAX_BYTES:
            raise DepositError(413, 'too_large', f'the artefact block is at most {ARTEFACT_BLOCK_MAX_BYTES // 1024} kB')
        return block

    def create_artefact(self, identity: Identity, body: Dict[str, Any], context: Optional[ArtefactContext],
                        relaxed_provenance: bool = False) -> Dict[str, Any]:
        """``POST /datasets/deposit`` with ``deposit_kind: "artefact"`` (design 4.1)."""
        self._require_artefacts_active()
        if context is None:
            raise DepositError(503, 'disabled', 'the artefact index is not loaded yet; try again')
        block = self._artefact_block(body)
        data_project = body.get('data_project')
        scope = body.get('scope')
        if not isinstance(data_project, str) or not artefacts.valid_segment(data_project) or len(data_project) > 64:
            raise DepositError(422, 'invalid_data_project', 'data_project is the folder name of a collection')
        if self._pending_collection(data_project):
            raise DepositError(409, 'collection_pending', f'the collection {data_project} is not live yet; deposit '
                                                          f'artefacts once its creation is published')
        collection = context.collection_by_folder(data_project)
        if not isinstance(scope, str) or not artefacts.valid_scope(scope):
            raise DepositError(422, 'invalid_scope', 'scope is the dataset file id (e.g. SLM) or _group')
        # the collection must exist; a collection artefact needs a visible collection, a dataset artefact a
        # visible dataset (a public dataset of a mixed collection is a valid scope)
        if collection is None or (scope == artefacts.GROUP_SCOPE
                                  and not context.policy.collection_visible(collection.group_id, context.viewer)):
            raise DepositError(422, 'invalid_data_project', f'{data_project} is not a collection of this site')
        group_id = collection.group_id
        scope_db_id = None if scope == artefacts.GROUP_SCOPE else f'{group_id}/{scope}'
        if scope_db_id is not None and (context.dataset_folder.get(scope_db_id) != data_project
                                        or not context.policy.dataset_visible(scope_db_id, context.viewer)):
            raise DepositError(422, 'invalid_scope', f'{scope} is not a dataset of {data_project}')
        kind = block.get('kind')
        if kind not in artefacts.KINDS:
            raise DepositError(422, 'invalid_kind', f'kind is one of {", ".join(artefacts.KINDS)}')
        if kind in DEFERRED_KINDS:
            raise DepositError(422, 'invalid_kind', f'{kind} artefacts are not accepted yet')
        if kind == 'de_table' and scope_db_id is None:
            raise DepositError(422, 'invalid_scope', 'a de_table belongs to a dataset (its DE tables are imported '
                                                     'into it); scope is the dataset file id, not _group')
        if kind == 'de_table':
            self.require_dataset_update(identity, scope_db_id)
        artefact_id = block.get('artefact_id')
        if not artefacts.valid_artefact_id(artefact_id):
            raise DepositError(422, 'invalid_artefact_id', 'artefact_id: 2 to 64 lower-case letters, digits and '
                                                           'dashes, starting with a letter or digit')
        public_scope = (context.policy.dataset_public(scope_db_id) if scope_db_id is not None
                        else context.policy.collection_public(group_id))
        clean = self._clean_block(block, kind, scope_db_id, context, strict=not relaxed_provenance,
                                  public_scope=public_scope)
        if kind == 'de_table':
            clean['dataset_basename'] = Path(context.dataset_md[scope_db_id]).stem
        if relaxed_provenance:
            clean['alias_of'] = 'de_only'      # set by the server only: the watcher relaxes provenance for it
        ref = artefacts.format_ref(group_id, scope, artefact_id)
        new_version_of = block.get('new_version_of')
        existing = context.index.get(group_id, scope, artefact_id)
        if existing is not None and not context.policy.artefact_visible(existing, context.viewer,
                                                                        include_orphans=identity.is_admin):
            # an artefact the caller cannot see answers like one that does not exist, and its id stays taken
            raise DepositError(404, 'not_found', f'no artefact {ref}')
        if existing is not None and not any(v.state in ('live', 'purged', 'deleting', 'deleted')
                                            for v in existing.versions.values()):
            existing = None                 # only failed or reserved numbers: no version was ever published
        if existing is None:
            if new_version_of is not None:
                raise DepositError(409, 'version_conflict', f'{ref} does not exist; leave new_version_of out',
                                   latest=None)
        else:
            follows = version_followed(existing)
            if new_version_of is None:
                raise DepositError(409, 'artefact_exists', f'{ref} exists; a new version gives "new_version_of": '
                                                           f'{follows}', latest=existing.latest)
            if not artefacts.owned_by(existing.owner, identity.user) and not identity.is_admin:
                raise DepositError(403, 'not_owner', f'new versions of {ref} are deposited by its owner or an admin')
            if existing.path.kind != kind:
                raise DepositError(422, 'invalid_kind', f'{ref} is a {existing.path.kind}; the kind cannot change')
            if isinstance(new_version_of, bool) or not isinstance(new_version_of, int) \
                    or new_version_of != follows:
                raise DepositError(409, 'version_conflict', f'the latest version of {ref} is {follows}'
                                   + (' (purged)' if existing.latest is None else ''), latest=existing.latest)
            if len(existing.versions) >= int(self._limit('artefacts_max_versions', 100)):
                raise DepositError(413, 'quota_exceeded', f'{ref} has the maximum number of versions; purge old '
                                                          f'ones first')
        files = self._artefact_files(body.get('files'), kind)
        if kind == 'de_table':
            de_table_problem(files)
        total = sum(entry['size'] for entry in files)
        if total > self._limit('artefacts_max_deposit_gb', 200) * 1024 ** 3:
            raise DepositError(413, 'too_large', f'{total / 1024 ** 3:.1f} GB is over the '
                                                 f'{self._limit("artefacts_max_deposit_gb", 200):g} GB of one deposit')
        stored = self._stored_bytes(context.index, identity.user) + self._pending_artefact_bytes(identity.user)
        if stored + total > self._limit('artefacts_max_stored_gb_per_user', 1000) * 1024 ** 3:
            raise DepositError(413, 'quota_exceeded', f'{(stored + total) / 1024 ** 3:.1f} GB of artefacts would '
                                                      f'be stored for you (limit '
                                                      f'{self._limit("artefacts_max_stored_gb_per_user", 1000):g} GB)')
        self.limiter.check(identity.user)              # before any call to the object store
        self.expire_stale()
        now = self.clock()
        deposit_id = new_deposit_id()
        folder = incoming_folder(self.settings.prefix, data_project, deposit_id)
        expires = now + self._ttl()
        rows: List[Dict[str, Any]] = []
        for entry in files:
            key = folder + entry['name']
            multipart = entry['size'] > min(self.settings.single_put_max_mb * MiB, SINGLE_PUT_HARD_MAX)
            part_size = choose_part_size(entry['size']) if multipart else None
            rows.append({'deposit_id': deposit_id, 'name': entry['name'], 'kind': 'artefact_file', 'key': key,
                         'staging_key': self._staging_key(deposit_id, key), 'size': entry['size'],
                         'sha256': entry['sha256'], 'state': 'pending', 'etag': None, 'completed': None,
                         'method': 'multipart' if multipart else 'put', 'upload_id': None, 'part_size': part_size,
                         'part_count': len(part_sizes(entry['size'], part_size)) if multipart else None})
        clean['files'] = [{'name': entry['name'], 'role': entry['role']} for entry in files]
        clean['new_version_of'] = new_version_of
        clean.update({'kind': kind, 'artefact_id': artefact_id, 'scope': scope, 'group_id': group_id,
                      'group': data_project})
        record = {'id': deposit_id, 'user': identity.user, 'name': identity.name, 'email': identity.email,
                  'is_admin': int(identity.is_admin), 'data_project': data_project, 'basename': artefact_id,
                  'update_of': scope_db_id, 'state': 'uploading', 'created': now, 'updated': now, 'expires': expires,
                  'total_bytes': total, 'watch_key': f'artefact:{deposit_id}', 'refreshes': 0,
                  'deposit_kind': 'artefact', 'artefact_ref': ref, 'artefact': json.dumps(clean),
                  'group_id': group_id}
        self.store.reserve(record, rows, now, self.settings,
                           extra_check=self._pending_kind_conflict('artefact_ref', ref, deposit_id))
        try:
            for row in rows:
                if row['method'] == 'multipart':
                    response = self.s3.create_multipart_upload(
                        Bucket=self.bucket, Key=row['staging_key'], Metadata=self._metadata(deposit_id, row['sha256']))
                    row['upload_id'] = response['UploadId']
                    self.store.update_file(deposit_id, row['name'], upload_id=row['upload_id'])
        except Exception as exc:  # noqa: BLE001 - S3 refused: abort what was started, release the deposit
            self._abort_and_delete(self.store.get(deposit_id), actor='create')
            self.store.cas(deposit_id, ('uploading',), 'expired')
            self.store.release_keys(deposit_id)
            raise DepositError(502, 's3_error', f'the object store refused to start the upload ({_error_code(exc)}); '
                                                f'try again later')
        instructions = [self._instructions(deposit_id, row, 1, self.settings.parts_per_batch, expires) for row in rows]
        self.store.event(deposit_id, identity.user, 'created', f'artefact {ref} ({kind}), {len(rows)} file(s), '
                                                               f'{total} bytes')
        warnings = list(clean.pop('_warnings', []))
        self.store.update(deposit_id, artefact=json.dumps(clean))
        return {'deposit_id': deposit_id, 'state': 'uploading', 'deposit_kind': 'artefact',
                'data_project': data_project, 'scope': scope, 'expires': _iso(expires),
                'url_ttl_seconds': self._ttl(), 'total_bytes': total, 'files': instructions, 'warnings': warnings,
                'artefact': {'ref_without_version': ref, 'kind': kind, 'new_version_of': new_version_of},
                'next': {'urls': f'/datasets/deposit/{deposit_id}/urls',
                         'complete': f'/datasets/deposit/{deposit_id}/complete',
                         'status': f'/datasets/deposit/{deposit_id}'}}

    def _clean_block(self, block: Mapping[str, Any], kind: str, scope_db_id: Optional[str],
                     context: ArtefactContext, strict: bool = True, public_scope: bool = False) -> Dict[str, Any]:
        """The checked fields of the artefact block (``recorded`` keys of the request are dropped)."""
        warnings: List[str] = []
        title = block.get('title')
        if not isinstance(title, str) or not TITLE_MIN_CHARS <= len(title.strip()) <= artefacts.TITLE_MAX_CHARS:
            raise DepositError(422, 'invalid', f'title: {TITLE_MIN_CHARS} to {artefacts.TITLE_MAX_CHARS} characters')
        description = block.get('description')
        if not isinstance(description, str) or len(description.strip()) < DESCRIPTION_MIN_CHARS \
                or len(description.encode('utf-8')) > artefacts.DESCRIPTION_MAX_BYTES:
            raise DepositError(422, 'invalid', f'description: at least {DESCRIPTION_MIN_CHARS} characters '
                                               f'(markdown, at most 20 kB): what it shows and how it was made')
        change_note = block.get('change_note')
        if change_note is not None and (not isinstance(change_note, str)
                                        or len(change_note) > artefacts.CHANGE_NOTE_MAX_CHARS):
            raise DepositError(422, 'invalid', 'change_note: text of at most 2000 characters')
        if block.get('new_version_of') is not None and not (isinstance(change_note, str) and change_note.strip()):
            warnings.append('a new version without change_note: say what changed')
        access = block.get('access', 'inherit')
        if access not in artefacts.ACCESS_VALUES:
            raise DepositError(422, 'invalid', 'access is inherit or internal (an artefact never widens access)')
        made_from = block.get('made_from')
        if not isinstance(made_from, list) or len(made_from) > 50:
            raise DepositError(422, 'made_from_unknown', 'made_from is a list of {db_id, dataset_version}')
        clean_made_from = []
        for entry in made_from:
            db_id = entry.get('db_id') if isinstance(entry, dict) else None
            version = entry.get('dataset_version') if isinstance(entry, dict) else None
            known = isinstance(db_id, str) and artefacts.DB_ID_RE.fullmatch(db_id) \
                and db_id in context.dataset_md and context.policy.dataset_visible(db_id, context.viewer)
            if not known or not isinstance(version, str) or not VERSION_RE.fullmatch(version):
                raise DepositError(422, 'made_from_unknown', f'made_from {db_id!r}@{version!r}: no such dataset '
                                                             f'version you can see')
            meta = read_md_meta(context.dataset_md[db_id])
            if version not in changelog_versions(meta):
                raise DepositError(422, 'made_from_unknown', f'made_from {db_id!r}@{version!r}: no such dataset '
                                                             f'version you can see')
            if str(meta.get('version')) != version:
                warnings.append(f'made from {db_id} {version}; the dataset is now {meta.get("version")}')
            clean_made_from.append({'db_id': db_id, 'dataset_version': version})
        hidden = [entry['db_id'] for entry in clean_made_from if not context.policy.dataset_public(entry['db_id'])]
        if access != 'internal' and public_scope and hidden:
            warnings.append(f'the artefact will be public but is made from internal dataset(s) {", ".join(hidden)}: '
                            f'it will be held for an admin (made_from_internal); deposit it with access internal '
                            f'to avoid that')
        if scope_db_id is not None and scope_db_id not in {entry['db_id'] for entry in clean_made_from}:
            raise DepositError(422, 'made_from_unknown', f'made_from must include the scope dataset {scope_db_id}')
        declared = block.get('declared')
        missing = provenance_missing(declared)
        if not strict:
            # the de_only alias: a 2.46 request carries no software list and its client cannot add one
            missing = [item for item in missing if not item.startswith('declared.software')]
        if missing and kind in PROVENANCE_STRICT_KINDS and strict:
            raise DepositError(422, 'provenance_incomplete', 'provenance is incomplete: ' + ', '.join(missing),
                               missing=missing)
        if missing:
            warnings.append('provenance is incomplete: ' + ', '.join(missing))
        return {'title': title.strip(), 'description': description, 'change_note': change_note,
                'access': access, 'made_from': clean_made_from,
                'declared': declared if isinstance(declared, dict) else {}, '_warnings': warnings}

    def _artefact_files(self, files: Any, kind: str) -> List[Dict[str, Any]]:
        max_files = int(self._limit('artefacts_max_files', 50))
        if not isinstance(files, list) or not files:
            raise DepositError(422, 'invalid', 'files must be a non-empty list')
        if len(files) > max_files:
            raise DepositError(422, 'invalid', f'at most {max_files} files per artefact version')
        max_bytes = self._limit('artefacts_max_file_gb', 100) * 1024 ** 3
        clean = []
        for entry in files:
            if not isinstance(entry, dict):
                raise DepositError(422, 'invalid', 'each file is an object {name, size, sha256, role}')
            name, size = entry.get('name'), entry.get('size')
            sha = str(entry.get('sha256') or '').lower()
            problem = artefacts.file_name_problem(name)
            if problem:
                raise DepositError(422, 'invalid_name', problem)
            if str(name).lower() in RESERVED_FILE_NAMES:
                raise DepositError(422, 'invalid_name', f'{name} is reserved: clients write the manifest next to '
                                                        f'downloaded files under that name; rename the file or put '
                                                        f'it in a subfolder')
            if entry.get('kind') not in (None, 'artefact_file'):
                raise DepositError(422, 'invalid', f'{name}: file kind is artefact_file')
            problem = extension_problem(kind, name)
            if problem:
                raise DepositError(422, 'invalid_name', problem)
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise DepositError(422, 'invalid', f'{name}: size must be a positive integer (bytes)')
            if size > max_bytes:
                raise DepositError(413, 'too_large', f'{name}: files are at most '
                                                     f'{self._limit("artefacts_max_file_gb", 100):g} GB')
            if not SHA256_RE.fullmatch(sha):
                raise DepositError(422, 'invalid', f'{name}: sha256 must be 64 hex characters')
            role = entry.get('role', 'main' if not clean else 'supporting')
            if kind == 'de_table' and name.lower().endswith('.mus'):
                role = 'sidecar'                         # a DE table's .mus is always its sidecar
            elif kind == 'de_table' and role == 'sidecar':
                raise DepositError(422, 'invalid', f'{name}: only a .mus is a sidecar')
            if role not in FILE_ROLES:
                raise DepositError(422, 'invalid', f'{name}: role is one of {", ".join(FILE_ROLES)}')
            clean.append({'name': name, 'size': size, 'sha256': sha, 'role': role})
        clashes = artefacts.casefold_clashes(entry['name'] for entry in clean)
        if clashes:
            raise DepositError(422, 'invalid_name', f'{clashes[0]}: two files differ only in case')
        if sum(entry['role'] == 'main' for entry in clean) > 1:
            raise DepositError(422, 'invalid', 'at most one file has role main')
        return clean

    @staticmethod
    def _stored_bytes(index: artefacts.ArtefactIndex, user: str) -> int:
        """Bytes of live artefact versions this user uploaded."""
        total = 0
        for entry in index.entries.values():
            for version in entry.versions.values():
                manifest = version.manifest or {}
                if version.state == 'live' and (manifest.get('recorded') or {}).get('uploaded_by') == user:
                    total += sum(int(item.get('size') or 0) for item in manifest.get('files') or [])
        return total

    def may_update_dataset(self, identity: Identity, db_id: str) -> bool:
        """The 2.46 update rule of a published dataset (``_existing_dataset``): an admin, ``update_policy:
        any``, or the dataset's owner (assigned by an admin, else the depositor of its newest live version)."""
        return identity.is_admin or self.settings.update_policy == 'any' \
            or self.store.owner_of_dataset(db_id) == identity.user

    def require_dataset_update(self, identity: Identity, db_id: str, what: str = 'DE tables',
                               hint: str = ', or deposit the table as kind table') -> None:
        """Review N1: DE tables change the dataset (a patch version and new DE studies), so a de_table (and
        the de_only alias) needs the right to update the dataset, as the 2.46 DE-only route did. Obs-columns
        deposits need the same right."""
        if not self.may_update_dataset(identity, db_id):
            raise DepositError(403, 'not_owner', f'{what} are added to {db_id} by its owner or an admin '
                                                 f'(datasets_deposit_update_policy: own); ask an admin to record '
                                                 f'you as its owner{hint}')

    def _pending_new_group(self, data_project: str) -> bool:
        """A dataset deposit that is creating a group of this name (open or waiting for the watcher)."""
        rows = self.store._rows("SELECT id FROM deposits WHERE COALESCE(deposit_kind, 'dataset') IN ('dataset', "
                                "'metadata_update', 'de_only') AND UPPER(data_project) = UPPER(?) AND (state IN "
                                "('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') NOT "
                                "IN ('live', 'rejected', 'failed')))", (data_project,))
        return bool(rows)

    def _pending_artefact_bytes(self, user: str) -> int:
        """Bytes of this user's artefact deposits that are open or wait for the watcher."""
        rows = self.store._rows("SELECT COALESCE(SUM(total_bytes), 0) AS total FROM deposits WHERE user = ? AND "
                                "deposit_kind = 'artefact' AND (state IN ('uploading', 'completing') OR (state = "
                                "'uploaded' AND COALESCE(watch_state, '') NOT IN ('live', 'rejected', 'failed')))",
                                (user,))
        return int(rows[0]['total'] or 0) if rows else 0

    def _pending_collection(self, data_project: str) -> bool:
        rows = self.store._rows("SELECT id FROM deposits WHERE deposit_kind = 'collection' AND "
                                "UPPER(data_project) = UPPER(?) AND state IN ('uploading', 'completing', 'uploaded') "
                                "AND COALESCE(watch_state, '') NOT IN ('live', 'rejected', 'failed')", (data_project,))
        return bool(rows)

    # create: collection ----------------------------------------------------
    def _taken_group_ids(self, context: Optional[ArtefactContext]) -> Set[str]:
        taken: Set[str] = set()
        if context is not None:
            taken |= set(context.collections)
            taken |= {db_id.split('/')[0] for db_id in context.dataset_md}
        if self.store_root.is_dir():
            for entry in self.store_root.iterdir():
                index = entry / 'index.md'
                if entry.is_dir() and index.is_file():
                    group_id = read_md_meta(index).get('group_id')
                    if group_id:
                        taken.add(str(group_id))
        taken |= {row['group_id'] for row in self.store._rows(
            "SELECT group_id FROM deposits WHERE deposit_kind = 'collection' AND group_id IS NOT NULL AND "
            "state IN ('uploading', 'completing', 'uploaded') AND COALESCE(watch_state, '') NOT IN ('rejected', "
            "'failed')")}
        return taken

    def _collection_block(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        block = body.get('collection')
        if not isinstance(block, dict):
            raise DepositError(422, 'invalid_collection', 'a collection deposit carries a "collection" object')
        title = block.get('title')
        if not isinstance(title, str) or not TITLE_MIN_CHARS <= len(title.strip()) <= COLLECTION_TITLE_MAX \
                or '\n' in title:
            raise DepositError(422, 'invalid_collection', f'title: one line of {TITLE_MIN_CHARS} to '
                                                          f'{COLLECTION_TITLE_MAX} characters')
        authors = block.get('authors')
        if not isinstance(authors, list) or not authors or len(authors) > MAX_AUTHORS or not all(
                isinstance(author, str) and author.strip() and len(author) <= 200 and '\n' not in author
                for author in authors):
            raise DepositError(422, 'invalid_collection', 'authors: a list of at least one name')
        description = block.get('description')
        if description is not None and (not isinstance(description, str)
                                        or len(description.encode('utf-8')) > artefacts.DESCRIPTION_MAX_BYTES):
            raise DepositError(422, 'invalid_collection', 'description: markdown of at most 20 kB')
        if isinstance(description, str):
            import safe_markdown  # noqa: PLC0415
            html_parts = safe_markdown.raw_html(description)
            if html_parts:
                # the description goes into index.md, which other tools render (review round 4): no raw HTML
                raise DepositError(422, 'invalid_collection', f'description: no raw HTML (found '
                                                              f'{html_parts[0][:40]!r}); write markdown, or put '
                                                              f'literal tags in backticks')
        access = block.get('access') or 'internal'
        if access not in artefacts.COLLECTION_ACCESS_VALUES:
            raise DepositError(422, 'invalid_collection', 'access is internal or public')
        doi = block.get('doi')
        if doi is not None and not (isinstance(doi, str) and DOI_RE.fullmatch(doi)):
            raise DepositError(422, 'invalid_collection', 'doi looks like 10.1000/xyz')
        papers = block.get('papers') or {}
        if not isinstance(papers, dict) or len(papers) > 50 or not all(
                isinstance(key, str) and CITEKEY_RE.fullmatch(key) and isinstance(value, str)
                and re.fullmatch(r'[a-z_]{1,30}', value) for key, value in papers.items()):
            raise DepositError(422, 'invalid_collection', 'papers maps citekeys to roles ({citekey: role})')
        clean: Dict[str, Any] = {'title': title.strip(), 'authors': [author.strip() for author in authors],
                                 'access': access, 'papers': papers,
                                 'description': description.strip() if isinstance(description, str) else None}
        if doi:
            clean['doi'] = doi
        for key in ('paper_title', 'license'):
            value = block.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 300 or '\n' in value:
                    raise DepositError(422, 'invalid_collection', f'{key}: one line of text')
                clean[key] = value.strip()
        return clean

    def create_collection(self, identity: Identity, body: Dict[str, Any], context: Optional[ArtefactContext]
                          ) -> Dict[str, Any]:
        """``POST /datasets/deposit`` with ``deposit_kind: "collection"`` (design 4.2): render the analysis
        collection's ``index.md``, write it to incoming with the deposit mark; the watcher places it."""
        self._require_artefacts_active()
        data_project = body.get('data_project')
        if not isinstance(data_project, str) or not artefacts.DATA_PROJECT_RE.fullmatch(data_project) \
                or len(data_project) > 64:
            raise DepositError(422, 'invalid_data_project', 'a collection is named like Fiers2026: a capital '
                                                            'letter first, letters, digits or _, a year last')
        block = self._collection_block(body)
        existing = [entry.name for entry in (self.store_root.iterdir() if self.store_root.is_dir() else [])
                    if entry.name.lower() == data_project.lower()]
        if existing or self._pending_collection(data_project) or self._pending_new_group(data_project):
            shown = f' ({existing[0]})' if existing and identity.is_admin else ''
            raise DepositError(409, 'collection_exists', f'a collection with this name exists or is being '
                                                         f'created{shown}')
        group_id = assign_group_id(data_project, self._taken_group_ids(context))
        today = _dt.date.today().isoformat()
        text = render_analysis_index_md(data_project, group_id, block, identity.user, today)
        raw = text.encode('utf-8')
        # what the watcher will check: the rendered file is a valid analysis index.md
        meta = artefacts.parse_front_matter(text)
        problems = artefacts.analysis_collection_problems(data_project, meta)
        if problems:
            raise DepositError(422, 'invalid_collection', '; '.join(problems))
        self.limiter.check(identity.user)
        self.expire_stale()
        now = self.clock()
        deposit_id = new_deposit_id()
        key = incoming_folder(self.settings.prefix, data_project, deposit_id) + 'index.md'
        sha = hashlib.sha256(raw).hexdigest()
        rows = [{'deposit_id': deposit_id, 'name': 'index.md', 'kind': 'collection_index', 'key': key,
                 'staging_key': None, 'size': len(raw), 'sha256': sha, 'state': 'pending', 'etag': None,
                 'completed': None, 'method': 'server', 'upload_id': None, 'part_size': None, 'part_count': None}]
        record = {'id': deposit_id, 'user': identity.user, 'name': identity.name, 'email': identity.email,
                  'is_admin': int(identity.is_admin), 'data_project': data_project, 'basename': data_project,
                  'update_of': None, 'state': 'uploading', 'created': now, 'updated': now, 'expires': now + self._ttl(),
                  'total_bytes': len(raw), 'watch_key': f'artefact:{deposit_id}', 'refreshes': 0,
                  'deposit_kind': 'collection', 'artefact_ref': f'collection:{data_project.upper()}',
                  'artefact': json.dumps({'collection': block, 'group_id': group_id}), 'group_id': group_id}
        self.store.reserve(record, rows, now, self.settings,
                           extra_check=self._collection_conflict(data_project, group_id, deposit_id))
        if not self.store.cas(deposit_id, ('uploading',), 'completing', worker=self.worker_id):
            raise DepositError(409, 'busy', 'the deposit changed state meanwhile')
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType='text/markdown',
                               Metadata=self._metadata(deposit_id, sha))
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
            etag = str(head.get('ETag', '')).strip('"')
            metadata = {str(k).lower(): str(v) for k, v in (head.get('Metadata') or {}).items()}
            if int(head.get('ContentLength') or -1) != len(raw) or metadata.get('deposit') != deposit_id \
                    or etag != hashlib.md5(raw).hexdigest():
                raise RuntimeError('the written index.md could not be verified')
        except Exception as exc:  # noqa: BLE001
            self.store.cas(deposit_id, ('completing',), 'expired')
            self.store.release_keys(deposit_id)
            raise DepositError(502, 's3_error', f'writing the collection failed ({_error_code(exc)}); try again')
        if not self.store.finish_complete(deposit_id, {'index.md': etag}, self.clock(), completed=self.clock()):
            raise DepositError(409, 'not_completed', 'the deposit was ended meanwhile; start again')
        self.store.event(deposit_id, identity.user, 'created', f'collection {data_project} ({group_id})')
        self.publish_record(deposit_id)
        return {'deposit_id': deposit_id, 'state': 'uploaded', 'deposit_kind': 'collection',
                'data_project': data_project, 'group_id': group_id, 'index_md': text,
                'held': ['public_access'] if block['access'] == 'public' and self.artefact_settings.get(
                    'artefacts_hold_public', True) else [],
                'next': {'status': f'/datasets/deposit/{deposit_id}'},
                'message': 'the watcher places the collection within a few minutes; follow it with the status route'}

    def _collection_conflict(self, data_project: str, group_id: str, exclude: str) -> Callable[[sqlite3.Connection], None]:
        def check(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT id FROM deposits WHERE deposit_kind = 'collection' AND id != ? AND (UPPER(data_project) = "
                "UPPER(?) OR UPPER(group_id) = UPPER(?)) AND (state IN ('uploading', 'completing') OR (state = "
                "'uploaded' AND COALESCE(watch_state, '') NOT IN ('live', 'rejected', 'failed')))",
                (exclude, data_project, group_id)).fetchone()
            if row is not None:
                raise DepositError(409, 'collection_exists', 'a collection with this name or group id is being '
                                                             'created')
        return check

    # purge -----------------------------------------------------------------
    def request_purge(self, identity: Identity, context: ArtefactContext, group_id: str, scope: str,
                      artefact_id: str, version_label: str, body: Any) -> Dict[str, Any]:
        """``POST /artefacts/{group_id}/{scope}/{artefact_id}/v{N}/purge`` (design 5.3, stage 1): ask the watcher
        to mark the version purged (files kept). The uploader of that version, the artefact's owner or an admin;
        never the latest version."""
        return self._purge_request(identity, context, group_id, scope, artefact_id, version_label, body, 'soft')

    def request_unpurge(self, identity: Identity, context: ArtefactContext, group_id: str, scope: str,
                        artefact_id: str, version_label: str, body: Any) -> Dict[str, Any]:
        """``POST .../v{N}/unpurge``: make a purged version live again. The same people as for the purge."""
        return self._purge_request(identity, context, group_id, scope, artefact_id, version_label, body, 'unpurge')

    def request_delete(self, identity: Identity, context: ArtefactContext, group_id: str, scope: str,
                       artefact_id: str, version_label: str, body: Any) -> Dict[str, Any]:
        """``POST .../v{N}/delete`` (admins, stage 2): delete a purged version permanently (files, local copies,
        de_table studies; a tombstone stays)."""
        return self._purge_request(identity, context, group_id, scope, artefact_id, version_label, body, 'hard')

    def _purge_request(self, identity: Identity, context: ArtefactContext, group_id: str, scope: str,
                       artefact_id: str, version_label: str, body: Any, action: str) -> Dict[str, Any]:
        """Record a purge, unpurge or delete request; the watcher carries it out under the artefact lock.

        Idempotent: a version already in the requested state answers ``state: "done"`` with ``changed: false``;
        the same request still waiting answers with that request. Another action waiting is a 409 ``busy``.
        """
        self._require_artefacts_active()
        verb = {'soft': 'purge', 'unpurge': 'unpurge', 'hard': 'delete'}[action]
        if self.settings.central_watcher:
            raise DepositError(409, 'central_watcher', f'this site sends its deposits to a central watcher: ask an '
                               f'admin of the central site to {verb} the version')
        if action == 'hard' and not identity.is_admin:
            raise DepositError(403, 'forbidden', 'only admins delete a version permanently; purge it instead')
        service = artefacts.ArtefactService(context.store_root, {})
        try:
            entry = service.resolve(context.index, context.policy, context.viewer, group_id, scope, artefact_id)
        except artefacts.ArtefactError as exc:
            raise DepositError(exc.status, exc.body['status'], exc.body['message'])
        try:
            number = artefacts.parse_version_label(version_label)
        except ValueError:
            number = None
        version = entry.versions.get(number) if number is not None else None
        if version is None:
            raise DepositError(404, 'not_found', f'no version {str(version_label)[:20]} of {entry.ref}')
        ref = f'{entry.ref}@v{number}'
        uploader = ((version.manifest or {}).get('recorded') or {}).get('uploaded_by')
        if not (identity.is_admin or artefacts.owned_by(uploader, identity.user)
                or artefacts.owned_by(entry.owner, identity.user)):
            raise DepositError(403, 'forbidden', f'only the uploader of {ref}, the owner of the artefact or an admin '
                                                 f'may {verb} it')
        waiting = self.store._rows("SELECT * FROM artefact_purges WHERE UPPER(ref) = UPPER(?) AND version = ? AND "
                                   "state IN ('requested', 'purging')", (entry.ref, number))
        if waiting:
            if (waiting[0].get('action') or 'hard') == action:
                return self._purge_answer(waiting[0], entry.ref, number, action)
            raise DepositError(409, 'busy', f'a {waiting[0].get("action")} request of {ref} is waiting for the watcher')
        done = {'soft': ('purged',), 'unpurge': ('live',), 'hard': ('deleted',)}[action]
        if version.state in done:
            return {'ref': entry.ref, 'version': number, 'action': action, 'state': 'done', 'changed': False,
                    'version_state': version.state, 'message': f'{ref} is already {version.state}'}
        if version.state == 'reserved':
            raise DepositError(409, 'busy', f'{ref} is being published right now')
        if version.state in ('deleting', 'deleted'):
            raise DepositError(409, 'deleted', f'{ref} is deleted (or being deleted) permanently')
        if action == 'soft':
            if version.state == 'failed':
                raise DepositError(409, 'failed_version', f'{ref} is a failed publication: an admin deletes it '
                                                          f'(POST .../delete)')
            newer = [other for other, item in entry.versions.items() if other > number and item.is_live]
            if not newer:
                raise DepositError(409, 'latest_version', f'{ref} is the latest version of {entry.ref} and cannot be '
                                                          f'purged; deposit a newer version first')
        if action == 'unpurge' and version.state != 'purged':
            raise DepositError(409, 'not_purged', f'{ref} is {version.state}: only a purged version is unpurged')
        if action == 'hard' and version.state not in ('purged', 'failed'):
            raise DepositError(409, 'not_purged', f'{ref} is {version.state}: purge it first, then delete it')
        if not isinstance(body, dict):
            raise DepositError(422, 'invalid', 'the body is {"reason": ...}' + (', "confirm": "<ref>@v<N>"}'
                                                                               if action == 'hard' else ''))
        reason = clean_note(body.get('reason'), 500)
        if action != 'unpurge' and len(reason) < 5:
            raise DepositError(422, 'invalid', f'reason: why the version is {"deleted" if action == "hard" else "purged"} '
                                               f'(at least 5 characters)')
        if action == 'hard' and body.get('confirm') != ref:
            raise DepositError(422, 'confirm_mismatch', f'type the reference to confirm: "confirm": "{ref}"')
        busy = self.store._rows(
            "SELECT id FROM deposits WHERE deposit_kind = 'artefact' AND UPPER(artefact_ref) = UPPER(?) AND "
            "(state IN ('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') NOT IN "
            "('live', 'rejected', 'failed', 'held', 'needs_decision')))", (entry.ref,))
        if busy:
            raise DepositError(409, 'busy', 'a deposit of a new version of this artefact is being processed')
        now = self.clock()
        try:
            with self.store._lock:
                cursor = self.store.conn.execute(
                    'INSERT INTO artefact_purges (ref, version, group_folder, scope, kind, artefact_id, requested_by, '
                    'by_admin, reason, state, requested, updated, action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (entry.ref, number, entry.path.group, entry.path.scope, entry.path.kind, entry.path.artefact_id,
                     identity.user, int(identity.is_admin), reason, 'requested', now, now, action))
                request_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise DepositError(409, 'busy', f'another request of {ref} is waiting for the watcher')
        row = self.store._rows('SELECT * FROM artefact_purges WHERE id = ?', (request_id,))[0]
        return self._purge_answer(row, entry.ref, number, action)

    @staticmethod
    def _purge_answer(row: Dict[str, Any], ref: str, number: int, action: str) -> Dict[str, Any]:
        after = {'soft': 'shows as purged (files kept)', 'unpurge': 'is live again',
                 'hard': 'is deleted permanently (a tombstone stays)'}[action]
        return {'ref': ref, 'version': number, 'action': action, 'state': 'requested', 'changed': True,
                'request_id': row['id'], 'requested': _iso(row['requested']),
                'next': f'the watcher carries it out on its next poll; the version then {after}'}

    def purges(self, identity: Identity, state: Optional[str] = None,
               action: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.store._rows('SELECT * FROM artefact_purges ORDER BY id DESC LIMIT 500')
        return [{'id': row['id'], 'ref': row['ref'], 'version': row['version'], 'action': row.get('action') or 'hard',
                 'state': row['state'], 'reason': row['reason'],
                 'requested': _iso(row['requested']), 'updated': _iso(row['updated']),
                 'requested_by': row['requested_by'] if identity.is_admin or row['requested_by'] == identity.user
                 else ('an admin' if row['by_admin'] else row['requested_by']), 'detail': row['detail']}
                for row in rows if identity.is_admin or row['requested_by'] == identity.user
                if (state is None or row['state'] == state)
                and (action is None or (row.get('action') or 'hard') == action)]

    # create: obs columns ---------------------------------------------------
    def _pending_dataset_conflict(self, db_id: str, exclude: str) -> Callable[[sqlite3.Connection], None]:
        """An ``extra_check`` for ``reserve``: no other deposit that changes this dataset (an update, a DE table,
        obs columns) is open or waiting for the watcher, so two writers never race for its duckdb and ``.md``."""
        def check(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT id FROM deposits WHERE UPPER(COALESCE(update_of, '')) = UPPER(?) AND id != ? AND ("
                "state IN ('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') "
                "NOT IN ('live', 'rejected', 'failed')))", (db_id, exclude)).fetchone()
            if row is not None:
                raise DepositError(409, 'busy', f'another deposit for {db_id} is open or being published; wait '
                                                f'until it is live, or withdraw it')
        return check

    def create_obs_columns(self, identity: Identity, body: Dict[str, Any], context: Optional[ArtefactContext]
                           ) -> Dict[str, Any]:
        """``POST /datasets/deposit`` with ``deposit_kind: "obs_columns"``: new obs columns for a published
        dataset from one TSV (design section 26). ``{data_project, update_of: <db_id>, columns: [{name, dtype,
        description?, source?, facet?}], replace?: false, files: [{name, size, sha256}]}``."""
        import obs_columns  # noqa: PLC0415
        if not self.settings.active:
            super()._require_active()
        if context is None:
            raise DepositError(503, 'disabled', 'the dataset registry is not loaded yet; try again')
        unknown = sorted(set(body) - {'deposit_kind', 'data_project', 'update_of', 'columns', 'replace', 'files'})
        if unknown:
            raise DepositError(422, 'invalid', f'unknown fields: {", ".join(unknown)}')
        db_id = body.get('update_of')
        data_project = body.get('data_project')
        if not isinstance(db_id, str) or not artefacts.DB_ID_RE.fullmatch(db_id):
            raise DepositError(422, 'invalid', 'obs columns are added to a published dataset: give "update_of": '
                                               '"<db_id>"')
        if db_id not in context.dataset_md or context.dataset_folder.get(db_id) != data_project \
                or not context.policy.dataset_visible(db_id, context.viewer):
            raise DepositError(422, 'update_of_unknown', f'{data_project}/{db_id} is not a published dataset')
        self.require_dataset_update(identity, db_id, what='obs columns', hint='')
        replace = body.get('replace', False)
        if not isinstance(replace, bool):
            raise DepositError(422, 'invalid', 'replace is true or false')
        may_replace = identity.is_admin or self.store.owner_of_dataset(db_id) == identity.user
        existing = list(context.dataset_obs(db_id) or []) if context.dataset_obs is not None else []
        try:
            specs = obs_columns.validate_columns(
                body.get('columns'), existing, replace=replace, may_replace=may_replace,
                max_columns=int(self._limit('obs_columns_max_columns', obs_columns.MAX_COLUMNS)))
        except obs_columns.ObsColumnsError as exc:
            status = {'not_owner': 403, 'column_exists': 409}.get(exc.code, 422)
            raise DepositError(status, exc.code, str(exc))
        if replace and not any(spec['replaces'] for spec in specs):
            raise DepositError(422, 'invalid', 'replace is set but no declared column exists yet; leave it out')
        files = body.get('files')
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict) \
                or set(files[0]) - {'name', 'size', 'sha256', 'kind'}:
            raise DepositError(422, 'invalid', 'files is one table: [{name, size, sha256}]')
        entry = files[0]
        name, size, sha = entry.get('name'), entry.get('size'), str(entry.get('sha256') or '').lower()
        problem = obs_columns.file_name_problem(name)
        if problem:
            raise DepositError(422, 'invalid_name', problem)
        if entry.get('kind') not in (None, 'obs_table'):
            raise DepositError(422, 'invalid', f'{name}: file kind is obs_table')
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise DepositError(422, 'invalid', f'{name}: size must be a positive integer (bytes)')
        max_gb = self._limit('obs_columns_max_file_gb', 2.0)
        if size > max_gb * 1024 ** 3:
            raise DepositError(413, 'too_large', f'{name}: an obs-columns table is at most {max_gb:g} GB')
        if not SHA256_RE.fullmatch(sha):
            raise DepositError(422, 'invalid', f'{name}: sha256 must be 64 hex characters')
        self.limiter.check(identity.user)              # before any call to the object store
        self.expire_stale()
        now = self.clock()
        deposit_id = new_deposit_id()
        key = incoming_folder(self.settings.prefix, data_project, deposit_id) + name
        expires = now + self._ttl()
        multipart = size > min(self.settings.single_put_max_mb * MiB, SINGLE_PUT_HARD_MAX)
        part_size = choose_part_size(size) if multipart else None
        row = {'deposit_id': deposit_id, 'name': name, 'kind': 'obs_table', 'key': key,
               'staging_key': self._staging_key(deposit_id, key), 'size': size, 'sha256': sha, 'state': 'pending',
               'etag': None, 'completed': None, 'method': 'multipart' if multipart else 'put', 'upload_id': None,
               'part_size': part_size, 'part_count': len(part_sizes(size, part_size)) if multipart else None}
        group_id, file_id = db_id.split('/')
        record = {'id': deposit_id, 'user': identity.user, 'name': identity.name, 'email': identity.email,
                  'is_admin': int(identity.is_admin), 'data_project': data_project, 'basename': file_id,
                  'update_of': db_id, 'state': 'uploading', 'created': now, 'updated': now, 'expires': expires,
                  'total_bytes': size, 'watch_key': f'artefact:{deposit_id}', 'refreshes': 0,
                  'deposit_kind': OBS_COLUMNS_KIND, 'artefact_ref': f'obs:{db_id}',
                  'artefact': obs_columns.spec_json(specs, replace), 'group_id': group_id}
        self.store.reserve(record, [row], now, self.settings,
                           extra_check=self._pending_dataset_conflict(db_id, deposit_id))
        try:
            if multipart:
                response = self.s3.create_multipart_upload(Bucket=self.bucket, Key=row['staging_key'],
                                                           Metadata=self._metadata(deposit_id, sha))
                row['upload_id'] = response['UploadId']
                self.store.update_file(deposit_id, name, upload_id=row['upload_id'])
        except Exception as exc:  # noqa: BLE001 - S3 refused: release the deposit
            self._abort_and_delete(self.store.get(deposit_id), actor='create')
            self.store.cas(deposit_id, ('uploading',), 'expired')
            self.store.release_keys(deposit_id)
            raise DepositError(502, 's3_error', f'the object store refused to start the upload ({_error_code(exc)}); '
                                                f'try again later')
        names = ', '.join(spec['name'] for spec in specs)
        self.store.event(deposit_id, identity.user, 'created', f'obs columns {names} for {db_id}, {size} bytes')
        return {'deposit_id': deposit_id, 'state': 'uploading', 'deposit_kind': OBS_COLUMNS_KIND,
                'data_project': data_project, 'update_of': db_id, 'expires': _iso(expires),
                'url_ttl_seconds': self._ttl(), 'total_bytes': size,
                'files': [self._instructions(deposit_id, row, 1, self.settings.parts_per_batch, expires)],
                'columns': [{'name': spec['name'], 'dtype': spec['dtype'], 'replaces': spec['replaces']}
                            for spec in specs],
                'version_class': obs_columns.version_part(specs), 'warnings': [],
                'next': {'urls': f'/datasets/deposit/{deposit_id}/urls',
                         'complete': f'/datasets/deposit/{deposit_id}/complete',
                         'status': f'/datasets/deposit/{deposit_id}'}}

    def _check_obs_header(self, record: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> None:
        """At complete: the first line of the staged table (a ranged read pinned to the verified ETag) is
        ``cell_name`` and exactly the declared columns. The watcher reads the whole table again."""
        import obs_columns  # noqa: PLC0415
        specs, _replace = obs_columns.spec_from_record(record)
        for row in rows.values():
            try:
                response = self.s3.get_object(Bucket=self.bucket, Key=row['staging_key'],
                                              IfMatch=f'"{row["staging_etag"]}"',
                                              Range=f'bytes=0-{OBS_HEADER_BYTES - 1}')
                raw = response['Body'].read(OBS_HEADER_BYTES)
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) in ('PreconditionFailed', '412'):
                    raise self._staging_changed_error() from exc
                raise DepositError(502, 's3_error', f'reading the table header back failed ({_error_code(exc)})')
            try:
                header = obs_columns.first_line(raw, row['name'])
            except Exception:  # noqa: BLE001 - zlib errors
                raise DepositError(422, 'invalid_table', f'{row["name"]} is not a readable gzip file')
            problems = obs_columns.header_problems(header, specs)
            if problems:
                raise DepositError(422, 'invalid_table', f'{row["name"]}: ' + '; '.join(problems) + '. Withdraw and '
                                                         'deposit again with a matching table or declaration.')

    def obs_columns_vocab(self) -> Dict[str, Any]:
        import obs_columns  # noqa: PLC0415
        return {
            'uploads_enabled': self.settings.active,
            'deposit_kind': OBS_COLUMNS_KIND,
            'dtypes': list(obs_columns.DTYPES),
            'name_rule': _anno_name_pattern() + ' (the ant-annotate rule), unique case-insensitively',
            'reserved': sorted(obs_columns.RESERVED),
            'facet_prefix': obs_columns.FACET_PREFIX + ' (only with "facet": true)',
            'file': 'one <name>.tsv or <name>.tsv.gz: header cell_name then the declared columns, tab separated',
            'missing_values': ['', 'NA', 'NaN', 'nan'],
            'limits': {'max_file_gb': self._limit('obs_columns_max_file_gb', 2.0),
                       'max_columns': int(self._limit('obs_columns_max_columns', obs_columns.MAX_COLUMNS)),
                       'max_rows': int(self._limit('obs_columns_max_rows', obs_columns.MAX_ROWS)),
                       'max_categories': obs_columns.MAX_CATEGORIES,
                       'description_chars': obs_columns.DESCRIPTION_MAX_CHARS,
                       'source_chars': obs_columns.SOURCE_MAX_CHARS},
            'rights': 'the owner of the dataset or an admin (any uploader with datasets_deposit_update_policy: any); '
                      'replace: owner or admin only',
            'cells': 'every cell_name must be a cell of the dataset (an unknown name fails the deposit); cells '
                     'without a value are allowed and reported',
            'version': 'minor (additive); major when replace overwrites existing values',
            'held_reasons': {'public_access': 'the dataset is public: an admin publishes the columns',
                             'donor_identifiers': 'the table holds values of the donor crosswalk'},
        }

    # de_only alias ---------------------------------------------------------
    def create_de_only_alias(self, identity: Identity, body: Dict[str, Any], context: Optional[ArtefactContext]
                             ) -> Dict[str, Any]:
        """A 2.46 DE-only deposit (``update_of`` + files of kind ``de``, or ``deposit_kind: "de_only"``) taken as
        a ``de_table`` artefact of the dataset (design 13; until 2.48.0). The artefact id comes from the first
        table's name; a table that is already an artefact of the dataset becomes its next version when the
        caller owns it (or is an admin). Provenance gaps are warnings here: the 2.46 request carries none."""
        self._require_artefacts_active()
        if context is None:
            raise DepositError(503, 'disabled', 'the artefact index is not loaded yet; try again')
        db_id = body.get('update_of')
        data_project = body.get('data_project')
        if not isinstance(db_id, str) or not artefacts.DB_ID_RE.fullmatch(db_id):
            raise DepositError(422, 'invalid', 'DE tables alone are added to a published dataset: give "update_of": '
                                               '"<db_id>"')
        if db_id not in context.dataset_md or context.dataset_folder.get(db_id) != data_project \
                or not context.policy.dataset_visible(db_id, context.viewer):
            raise DepositError(422, 'update_of_unknown', f'{data_project}/{db_id} is not a published dataset')
        self.require_dataset_update(identity, db_id)
        files = body.get('files')
        if not isinstance(files, list) or not files or not all(isinstance(entry, dict) for entry in files):
            raise DepositError(422, 'invalid', 'files must be a non-empty list')
        tables = [entry.get('name') for entry in files if isinstance(entry.get('name'), str)
                  and not entry['name'].endswith('.mus')]
        if not tables:
            raise DepositError(422, 'invalid', 'a DE deposit needs at least one table with its .mus')
        stem = PurePosixPath(tables[0]).name
        for ending in ('.tsv.gz', '.csv.gz', '.tsv', '.csv'):
            if stem.lower().endswith(ending):
                stem = stem[:-len(ending)]
                break
        artefact_id = alias_artefact_id(stem)
        scope = db_id.split('/')[1]
        group_id = db_id.split('/')[0]
        meta = read_md_meta(context.dataset_md[db_id])
        new_version_of = None
        existing = context.index.get(group_id, scope, artefact_id)
        follows = version_followed(existing) if existing is not None else None
        if follows is not None:
            if not artefacts.owned_by(existing.owner, identity.user) and not identity.is_admin:
                raise DepositError(409, 'artefact_exists', f'the DE table {stem} is already an artefact of {db_id}, '
                                                           f'deposited by someone else; give your table another name '
                                                           f'or ask an admin')
            new_version_of = follows
        names = ', '.join(tables)
        artefact = {
            'kind': 'de_table', 'artefact_id': artefact_id, 'title': f'DE table {stem}'[:artefacts.TITLE_MAX_CHARS],
            'description': (f'Differential expression results ({names}) for {db_id}, deposited through the DE-only '
                            f'route (deposit_kind de_only, an alias of de_table artefacts until '
                            f'{DE_ONLY_ALIAS_UNTIL}). The table sidecars hold the method and the comparison.'),
            'access': 'inherit', 'new_version_of': new_version_of,
            'change_note': 'a new deposit through the DE-only route' if new_version_of else None,
            'made_from': [{'db_id': db_id, 'dataset_version': str(meta.get('version') or '')}],
            'declared': {'author': {'name': identity.name or identity.user}, 'created': _dt.date.today().isoformat(),
                         'code': {'none': 'deposited through the DE-only route (no provenance in the request)'}},
        }
        alias_body = {'deposit_kind': 'artefact', 'data_project': data_project, 'scope': scope,
                      'artefact': artefact,
                      'files': [{'name': entry.get('name'), 'size': entry.get('size'), 'sha256': entry.get('sha256'),
                                 'kind': 'artefact_file'} for entry in files]}
        answer = self.create_artefact(identity, alias_body, context, relaxed_provenance=True)
        answer['alias_of'] = 'de_only'
        answer.setdefault('warnings', []).append(
            f'deposit_kind de_only is an alias of a de_table artefact ({answer["artefact"]["ref_without_version"]}) '
            f'until {DE_ONLY_ALIAS_UNTIL}; deposit DE tables with deposit_kind artefact and kind de_table')
        return answer

    # vocabulary ------------------------------------------------------------
    def vocab(self, identity: Optional[Identity] = None) -> Dict[str, Any]:
        answer = super().vocab(identity)
        answer['artefacts'] = self.artefact_vocab()
        answer['analysis_index_md'] = analysis_index_md_vocab()
        answer['obs_columns'] = self.obs_columns_vocab()
        return answer

    def artefact_vocab(self) -> Dict[str, Any]:
        settings = self.artefact_settings
        return {
            'uploads_enabled': self.artefacts_active,
            'kinds': {kind: list(exts) for kind, exts in KIND_EXTENSIONS.items() if kind not in DEFERRED_KINDS},
            'deferred_kinds': list(DEFERRED_KINDS),
            'artefact_id': artefacts.ARTEFACT_ID_RE.pattern,
            'file_name': 'one or two /-separated parts, each ^[A-Za-z0-9][A-Za-z0-9._-]*$ of at most 120 characters',
            'roles': list(FILE_ROLES),
            'limits': {'max_file_gb': settings.get('artefacts_max_file_gb'),
                       'max_deposit_gb': settings.get('artefacts_max_deposit_gb'),
                       'max_files': settings.get('artefacts_max_files'),
                       'max_versions': settings.get('artefacts_max_versions'),
                       'max_stored_gb_per_user': settings.get('artefacts_max_stored_gb_per_user'),
                       'title_chars': [TITLE_MIN_CHARS, artefacts.TITLE_MAX_CHARS],
                       'description_min_chars': DESCRIPTION_MIN_CHARS},
            'provenance': {'required_for': list(PROVENANCE_STRICT_KINDS),
                           'keys': ['declared.author.name', 'declared.created (YYYY-MM-DD)',
                                    'declared.software [{name, version}]',
                                    'declared.code {repository, commit} or {none: reason}']},
            'access': list(artefacts.ACCESS_VALUES),
            'held_reasons': HELD_REASONS,
            'hold_public': bool(settings.get('artefacts_hold_public', True)),
            'incoming_key': '<prefix><data_project>/artefacts/<deposit_id>/<name>',
        }


def _anno_name_pattern() -> str:
    from anthive_db import _ANNO_COL_RE  # noqa: PLC0415
    return _ANNO_COL_RE.pattern


def is_de_only_body(body: Any) -> bool:
    """A 2.46 DE-only deposit request: ``update_of`` and every file of kind ``de``."""
    files = body.get('files') if isinstance(body, dict) else None
    return bool(isinstance(body, dict) and body.get('update_of') and isinstance(files, list) and files
                and all(isinstance(entry, dict) and entry.get('kind') == 'de' for entry in files))


def version_followed(entry: Any) -> Optional[int]:
    """The version a new version of ``entry`` must name in ``new_version_of``: its latest live version, else
    (every version purged) the highest purged one; None when nothing was ever published."""
    if entry is None:
        return None
    if entry.latest is not None:
        return entry.latest
    published = [number for number, version in entry.versions.items() if version.state in ('live', 'purged', 'deleting', 'deleted')]
    return max(published) if published else None


def alias_artefact_id(stem: str) -> str:
    """An artefact id from a table name: lower case, runs of other characters as one dash, at most 64."""
    slug = re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')[:64].strip('-')
    return slug if artefacts.valid_artefact_id(slug) else 'de-table'


def de_table_problem(files: Sequence[Dict[str, Any]]) -> None:
    """A ``de_table`` holds DE tables, each with its ``<table>.mus``, and nothing else (422 otherwise)."""
    names = {entry['name'] for entry in files}
    tables = [name for name in names if not name.lower().endswith('.mus')]
    if not tables:
        raise DepositError(422, 'invalid', 'a de_table holds at least one DE table (.tsv, .csv, gzip of those)')
    for table in tables:
        if table + '.mus' not in names:
            raise DepositError(422, 'invalid', f'every DE table needs its sidecar: {table}.mus is missing')
    for name in names:
        if name.lower().endswith('.mus') and name[:-4] not in names:
            raise DepositError(422, 'invalid', f'{name}: a sidecar without its table')


def analysis_index_md_vocab() -> Dict[str, Any]:
    """``index_md`` rules of an analysis collection (the deposit vocabulary, REST 2.47)."""
    return {'required': ['group (equals data_project)', 'group_id (assigned by the server)',
                         'collection_kind: analysis', 'title', 'authors', 'year (from the name)'],
            'required_spec': {'keys': ['group', 'group_id', 'collection_kind', 'title', 'authors', 'year'],
                              'one_of': [], 'equals': {'group': 'data_project', 'collection_kind': 'analysis'},
                              'group_id': {'length': 3, 'unique_in_store': True, 'assigned_by_server': True},
                              'not_required': ['default_dataset', 'doi', 'paper_title'],
                              'access': list(artefacts.COLLECTION_ACCESS_VALUES)},
            'naming': artefacts.DATA_PROJECT_RE.pattern,
            'written_by': 'the server, from POST /datasets/deposit {"deposit_kind": "collection", ...}'}
