"""Signed deposit records and results for one central watcher serving several sites (2.47.0).

Design: doc/DESIGN_dataset_ingestion.md section 24. Every object in the ingestion bucket is reachable by anyone
holding its keys, so nothing read from it is trusted until verified here.

* A site (an anthive API that accepts deposits) writes ``<staging_root><site_id>/<deposit_id>/record.json``:
  its local deposit record (login, files with sizes, sha256 and ETags, update_of, deposit_kind, artefact
  fields, state; no names or e-mail addresses) signed with HMAC-SHA256 under a per-site secret the watcher also
  holds. The file is one signature line (schema, key id, HMAC) and the canonical JSON body (sorted keys, no
  floats, ASCII) with the site id, the deposit id, a version counter and the key id inside. The HMAC covers the
  exact body bytes, so nothing is parsed before the signature holds; the body is then parsed with a depth limit.
* The watcher verifies (known site, key id, signature, site and deposit id equal to the object's path, schema,
  size limit, monotonic version, field rules) and imports the record into its own deposit store, where the
  existing route links it to the incoming objects.
* The watcher answers with ``.../result.json``: the outcome (state, held reasons, the redacted report, db id
  and version or artefact reference) signed with its own Ed25519 key, in the same layout (signature line, then
  body). Sites verify it with the public key(s) in their configuration before parsing, so a site cannot forge
  another site's or its own results.

Secrets never appear in logs or exceptions: errors name the rule, not the material.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RECORD_SCHEMA = 'anthive.deposit_record/1'
RESULT_SCHEMA = 'anthive.deposit_result/1'
RECORD_NAME = 'record.json'
RESULT_NAME = 'result.json'
MAX_RECORD_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_FILES = 500
SITE_ID_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
KEY_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,32}')
DEPOSIT_ID_RE = re.compile(r'dep_[0-9a-f]{20}')
USER_RE = re.compile(r'[A-Za-z0-9._@-]{1,80}')
SEGMENT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
ETAG_RE = re.compile(r'[0-9a-f]{32}(-[0-9]{1,5})?')
SHA256_RE = re.compile(r'[0-9a-f]{64}')
DB_ID_RE = re.compile(r'[A-Za-z0-9]{1,16}/[A-Za-z0-9]{1,16}')
ED25519_PREFIX = 'ed25519:'
#: the signature line of record.json: ``anthive.deposit_record/1 <key_id> <hmac hex>``
RECORD_HEADER_RE = re.compile(rb'anthive\.deposit_record/1 ([A-Za-z0-9._-]{1,32}) ([0-9a-f]{64})')
#: the signature line of result.json: ``anthive.deposit_result/1 <ed25519 signature, base64>``
RESULT_HEADER_RE = re.compile(rb'anthive\.deposit_result/1 ([A-Za-z0-9+/]{86}==)')
MAX_HEADER_BYTES = 200
#: nesting depth of a signed body (records and results are a few levels deep; reports somewhat more)
MAX_DEPTH = 32

#: Deposit columns a site exports (the watcher's own columns are never taken from a site). No name or e-mail
#: address: the watcher never e-mails another site's depositors, and every holder of the ingestion keys can read
#: the records.
RECORD_FIELDS = ('id', 'user', 'is_admin', 'data_project', 'basename', 'update_of', 'state',
                 'created', 'expires', 'completed', 'total_bytes', 'watch_key', 'h5ad_etag', 'mus_key',
                 'mus_etag', 'continues', 'h5ad_sha256', 'deposit_kind', 'artefact_ref', 'artefact', 'group_id')
FILE_FIELDS = ('name', 'kind', 'key', 'size', 'sha256', 'method', 'state', 'etag', 'staging_key', 'staging_etag',
               'completed')
INT_FIELDS = {'is_admin', 'created', 'expires', 'completed', 'total_bytes', 'size'}
DEPOSIT_KINDS = ('dataset', 'metadata_update', 'de_only', 'artefact', 'collection', 'obs_columns')
#: kinds whose watch key is ``artefact:<deposit_id>`` and whose files lie under ``artefacts/<deposit_id>/``
ARTEFACT_ROUTE_KINDS = ('artefact', 'collection', 'obs_columns')
RECORD_STATES = ('uploading', 'completing', 'uploaded', 'withdrawing', 'withdrawn', 'expiring', 'expired')


class RecordError(ValueError):
    """A record or result that must not be trusted; the message names the rule (never secret material)."""


# ── canonical form ───────────────────────────────────────────────────────────

def _check_value(value: Any, path: str = '') -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) >= 2 ** 63:
            raise RecordError(f'integer out of range at {path or "top"}')
        return
    if isinstance(value, float):
        raise RecordError(f'floats are not allowed in a signed document ({path or "top"})')
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_value(item, f'{path}[{index}]')
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecordError(f'non-string key at {path or "top"}')
            _check_value(item, f'{path}.{key}')
        return
    raise RecordError(f'unsupported value type {type(value).__name__} at {path or "top"}')


def canonical(document: Mapping[str, Any]) -> bytes:
    """The bytes that are signed: sorted keys, no whitespace, ASCII escapes, no floats."""
    _check_value(dict(document))
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(document)).hexdigest()


_STRING_RE = re.compile(rb'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKET_RE = re.compile(rb'[\[\]{}]')


def _check_depth(body: bytes, what: str) -> None:
    """RecordError when ``body`` nests deeper than MAX_DEPTH (strings skipped), before json.loads sees it."""
    depth = 0
    for match in _BRACKET_RE.finditer(_STRING_RE.sub(b'""', body)):
        if match.group() in (b'[', b'{'):
            depth += 1
            if depth > MAX_DEPTH:
                raise RecordError(f'{what} nests deeper than {MAX_DEPTH} levels')
        else:
            depth -= 1


def _split(raw: Any, limit: int, what: str) -> Tuple[bytes, bytes]:
    """(signature line, body) of a signed file; nothing of it is parsed here."""
    if not isinstance(raw, (bytes, bytearray)):
        raise RecordError(f'{what} is not bytes')
    raw = bytes(raw)
    if len(raw) > limit:
        raise RecordError(f'{what} is larger than {limit} bytes')
    newline = raw.find(b'\n', 0, MAX_HEADER_BYTES)
    if newline < 0:
        raise RecordError(f'{what}: no signature line')
    return raw[:newline], raw[newline + 1:]


def _parse(body: bytes, what: str) -> Dict[str, Any]:
    """The JSON object of a body whose signature was verified (depth limited, no floats)."""
    _check_depth(body, what)
    try:
        document = json.loads(body.decode('utf-8'), parse_float=_no_float, parse_constant=_no_float)
    except RecordError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecordError(f'{what} is not JSON ({type(exc).__name__})') from None
    if not isinstance(document, dict):
        raise RecordError(f'{what} is not a JSON object')
    return document


def _no_float(text: str) -> Any:
    raise RecordError('floats are not allowed in a signed document')


# ── paths ────────────────────────────────────────────────────────────────────

def staging_root(prefix: str) -> str:
    """``__deposits/`` (the staging prefix without the site folder)."""
    return str(prefix or '__deposits/').strip().strip('/') + '/'


def object_key(root: str, site_id: str, deposit_id: str, name: str) -> str:
    if not SITE_ID_RE.fullmatch(site_id) or not DEPOSIT_ID_RE.fullmatch(deposit_id) or name not in (RECORD_NAME,
                                                                                                     RESULT_NAME):
        raise RecordError('not a deposit record path')
    return f'{staging_root(root)}{site_id}/{deposit_id}/{name}'


def parse_object_key(key: str, root: str, name: str = RECORD_NAME) -> Optional[Tuple[str, str]]:
    """``(site_id, deposit_id)`` of ``<root><site>/<deposit>/<name>``, or None for any other key."""
    base = staging_root(root)
    if not isinstance(key, str) or not key.startswith(base):
        return None
    parts = key[len(base):].split('/')
    if len(parts) != 3 or parts[2] != name:
        return None
    site_id, deposit_id = parts[0], parts[1]
    if not SITE_ID_RE.fullmatch(site_id) or not DEPOSIT_ID_RE.fullmatch(deposit_id):
        return None
    return site_id, deposit_id


# ── site secrets (HMAC) ──────────────────────────────────────────────────────

def _secret_bytes(secret: str) -> bytes:
    value = str(secret or '').strip()
    if len(value) < 32:
        raise RecordError('a site secret must have at least 32 characters')
    return value.encode('utf-8')


def load_site_keys(directory: Path) -> Dict[str, Dict[str, bytes]]:
    """The watcher's table of known sites: ``<directory>/<site_id>.keys`` with lines ``<key_id> <secret>``.

    Several lines rotate a secret (old and new accepted during the window). A file readable by group or others,
    a bad site id or key id, or a short secret is refused (RecordError naming the file, never the secret).
    """
    sites: Dict[str, Dict[str, bytes]] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return sites
    uid = os.getuid()
    folder = directory.stat()
    if folder.st_uid != uid or folder.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RecordError(f'{directory}: must belong to the watcher\'s user and not be writable by group or '
                          f'others; chown it and chmod 700')
    for path in sorted(directory.glob('*.keys')):
        site_id = path.name[:-len('.keys')]
        if not SITE_ID_RE.fullmatch(site_id):
            raise RecordError(f'{path.name}: not a site id')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RecordError(f'{path}: not a regular file (a symbolic link is not accepted)')
        if info.st_uid != uid:
            raise RecordError(f'{path}: does not belong to the watcher\'s user; chown it')
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RecordError(f'{path}: readable by group or others; chmod 600')
        keys: Dict[str, bytes] = {}
        for number, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) != 2 or not KEY_ID_RE.fullmatch(parts[0]):
                raise RecordError(f'{path.name} line {number}: expected "<key_id> <secret>"')
            try:
                keys[parts[0]] = _secret_bytes(parts[1])
            except RecordError as exc:
                raise RecordError(f'{path.name} line {number}: {exc}') from None
        if keys:
            sites[site_id] = keys
    return sites


def sign_record(record: Mapping[str, Any], site_id: str, deposit_id: str, version: int, key_id: str,
                secret: str) -> bytes:
    """The ``record.json`` bytes of a site: the signature line (schema, key id, HMAC-SHA256 of the body), then
    the canonical body (the envelope with the record)."""
    if not SITE_ID_RE.fullmatch(site_id) or not DEPOSIT_ID_RE.fullmatch(deposit_id) or not KEY_ID_RE.fullmatch(key_id):
        raise RecordError('bad site id, deposit id or key id')
    if not isinstance(version, int) or version < 1:
        raise RecordError('version must be a positive integer')
    envelope = {'schema': RECORD_SCHEMA, 'site_id': site_id, 'deposit_id': deposit_id, 'version': version,
                'key_id': key_id, 'record': dict(record)}
    body = canonical(envelope)
    signature = hmac.new(_secret_bytes(secret), body, hashlib.sha256).hexdigest()
    return f'{RECORD_SCHEMA} {key_id} {signature}\n'.encode('ascii') + body


def verify_record(raw: bytes, site_id: str, deposit_id: str, sites: Mapping[str, Mapping[str, bytes]]) -> Dict[str, Any]:
    """The verified envelope of ``record.json`` found at ``<root><site_id>/<deposit_id>/``; RecordError otherwise.

    Checks the size limit, that the site and the key id of the signature line are known and the HMAC over the
    exact body bytes (constant-time compare), all before anything is parsed; then the body (depth limited), the
    schema, and that the site id, deposit id and key id inside equal the path and the signature line. Field
    rules are :func:`record_problems`.
    """
    keys = sites.get(site_id)
    if not keys:
        raise RecordError(f'unknown site {site_id!r}')
    header, body = _split(raw, MAX_RECORD_BYTES, 'record.json')
    match = RECORD_HEADER_RE.fullmatch(header)
    if match is None:
        raise RecordError('record.json: no signature line')
    key_id = match.group(1).decode('ascii')
    secret = keys.get(key_id)
    if secret is None:
        raise RecordError(f'record.json: key id {key_id!r} is not known for site {site_id!r}')
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode('ascii'), match.group(2)):
        raise RecordError('record.json: bad signature')
    envelope = _parse(body, 'record.json')
    if envelope.get('schema') != RECORD_SCHEMA:
        raise RecordError('record.json: unknown schema')
    if envelope.get('site_id') != site_id:
        raise RecordError('record.json: the site id inside does not match its path')
    if envelope.get('deposit_id') != deposit_id:
        raise RecordError('record.json: the deposit id inside does not match its path')
    version = envelope.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RecordError('record.json: version is not a positive integer')
    if envelope.get('key_id') != key_id:
        raise RecordError('record.json: the key id inside does not match the signature line')
    if set(envelope) - {'schema', 'site_id', 'deposit_id', 'version', 'key_id', 'record'}:
        raise RecordError('record.json: unknown envelope fields')
    if not isinstance(envelope.get('record'), dict):
        raise RecordError('record.json: no record')
    return envelope


# ── record content rules ─────────────────────────────────────────────────────

def _key_ok(key: Any) -> bool:
    return isinstance(key, str) and 0 < len(key) <= 1024 and all(SEGMENT_RE.fullmatch(part) or
                                                                 (index == 0 and part.startswith('__')
                                                                  and SEGMENT_RE.fullmatch(part[2:] or 'x'))
                                                                 for index, part in enumerate(key.split('/')))


def record_problems(record: Mapping[str, Any], site_id: str, deposit_id: str, incoming_prefix: str,
                    root: str) -> List[str]:
    """Why a verified record cannot be imported (empty list: it can). Path traversal, foreign prefixes, types."""
    problems: List[str] = []
    incoming = str(incoming_prefix or '__incoming/').strip().strip('/') + '/'
    staging = f'{staging_root(root)}{site_id}/{deposit_id}/'
    unknown = set(record) - set(RECORD_FIELDS) - {'files'}
    if unknown:
        problems.append(f'unknown record fields: {", ".join(sorted(unknown))[:200]}')
    if record.get('id') != deposit_id:
        problems.append('record id differs from the deposit id')
    user = record.get('user')
    if not isinstance(user, str) or not USER_RE.fullmatch(user) or '@' in user:
        problems.append('user is not a plain login (letters, digits, ._-)')
    for field in ('data_project', 'basename'):
        if not isinstance(record.get(field), str) or not SEGMENT_RE.fullmatch(record[field]):
            problems.append(f'{field} is not a safe name')
    if record.get('deposit_kind') not in DEPOSIT_KINDS:
        problems.append('deposit_kind is unknown')
    if record.get('state') not in RECORD_STATES:
        problems.append('state is unknown')
    if record.get('update_of') is not None and (not isinstance(record['update_of'], str)
                                                or not DB_ID_RE.fullmatch(record['update_of'])):
        problems.append('update_of is not a dataset id')
    for field in sorted(INT_FIELDS & set(record)):
        if record[field] is not None and (not isinstance(record[field], int) or isinstance(record[field], bool)):
            problems.append(f'{field} is not an integer')
    for field in ('h5ad_etag', 'mus_etag'):
        if record.get(field) is not None and (not isinstance(record[field], str) or not ETAG_RE.fullmatch(record[field])):
            problems.append(f'{field} is not an ETag')
    if record.get('h5ad_sha256') is not None and (not isinstance(record['h5ad_sha256'], str)
                                                  or not SHA256_RE.fullmatch(record['h5ad_sha256'])):
        problems.append('h5ad_sha256 is not a sha256')
    project_prefix = f'{incoming}{record.get("data_project")}/'
    for field in ('watch_key', 'mus_key', 'continues'):
        value = record.get(field)
        if value is None:
            continue
        if field == 'continues':
            if not isinstance(value, str) or not DEPOSIT_ID_RE.fullmatch(value):
                problems.append('continues is not a deposit id')
            continue
        if field == 'watch_key' and record.get('deposit_kind') in ARTEFACT_ROUTE_KINDS:
            if value != f'artefact:{deposit_id}':
                problems.append('the watch key of an artefact, collection or obs-columns deposit is '
                                'artefact:<deposit_id>')
            continue
        if not _key_ok(value) or not value.startswith(project_prefix):
            problems.append(f'{field} is not a key under {project_prefix}')
    files = record.get('files')
    if not isinstance(files, list) or len(files) > MAX_FILES:
        problems.append(f'files is not a list of at most {MAX_FILES}')
        return problems
    names = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) - set(FILE_FIELDS):
            problems.append('a file entry has unknown fields')
            continue
        name = entry.get('name')
        if not isinstance(name, str) or not 0 < len(name) <= 255 or name in names or '\\' in name \
                or any(not SEGMENT_RE.fullmatch(part) for part in name.split('/')):
            problems.append(f'file name {str(name)[:60]!r} is not safe or not unique')
            continue
        names.add(name)
        key = entry.get('key')
        if not _key_ok(key) or not key.startswith(project_prefix):
            problems.append(f'{name}: key is not under {project_prefix}')
        if entry.get('kind') == 'artefact' or record.get('deposit_kind') in ARTEFACT_ROUTE_KINDS:
            if isinstance(key, str) and record.get('deposit_kind') in ('artefact', 'obs_columns') \
                    and not key.startswith(f'{project_prefix}artefacts/{deposit_id}/'):
                problems.append(f'{name}: an artefact file must lie under artefacts/{deposit_id}/')
        staging_key = entry.get('staging_key')
        if staging_key is not None and (not _key_ok(staging_key) or not staging_key.startswith(staging)) \
                and entry.get('method') != 'reuse':
            problems.append(f'{name}: staging key is not under {staging}')
        if not isinstance(entry.get('size'), int) or isinstance(entry.get('size'), bool) or entry['size'] < 0:
            problems.append(f'{name}: size is not a non-negative integer')
        for field in ('etag', 'staging_etag'):
            if entry.get(field) is not None and (not isinstance(entry[field], str) or not ETAG_RE.fullmatch(entry[field])):
                problems.append(f'{name}: {field} is not an ETag')
        if entry.get('sha256') is not None and (not isinstance(entry['sha256'], str)
                                                or not SHA256_RE.fullmatch(entry['sha256'])):
            problems.append(f'{name}: sha256 is not a sha256')
    if record.get('state') == 'uploaded':
        problems += staged_record_problems(record)
    return problems[:20]


def staged_record_problems(record: Mapping[str, Any]) -> List[str]:
    """Section 24.9: a site leaves its uploads in its staging folder and the watcher copies them into incoming.
    An uploaded record names every file complete with its staging key, staging ETag and sha256, and no incoming
    ETag (a site that copied into incoming itself runs the protocol before 2.47.0); nothing continues a
    deposit (the h5ad is deposited again)."""
    problems: List[str] = []
    if record.get('continues'):
        problems.append('continues: a site with a central watcher deposits the h5ad again instead of continuing one')
    if record.get('h5ad_etag') or record.get('mus_etag'):
        problems.append('incoming ETags in the record: the site copied into incoming itself (an older version); '
                        'the watcher copies from staging')
    for entry in record.get('files') or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('name'))[:60]
        if entry.get('state') != 'complete':
            problems.append(f'{name}: an uploaded record names a file that is not complete')
        if entry.get('etag'):
            problems.append(f'{name}: an incoming ETag in the record (the site copied it into incoming)')
        if not entry.get('staging_key') or not entry.get('staging_etag') or not entry.get('sha256'):
            problems.append(f'{name}: no staged copy (staging key, staging ETag and sha256) in the record')
    return problems


# ── export (site) and import (watcher) ───────────────────────────────────────

def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == '':
        return None
    return int(value)


def export_record(store: Any, deposit_id: str) -> Dict[str, Any]:
    """A site's local deposit record as the signed ``record`` (no floats, no watcher columns, no upload ids)."""
    row = store.get(deposit_id)
    if row is None:
        raise RecordError('no such deposit')
    record: Dict[str, Any] = {}
    for field in RECORD_FIELDS:
        value = row.get(field)
        record[field] = _int_or_none(value) if field in INT_FIELDS else value
    record['is_admin'] = 1 if row.get('is_admin') else 0
    files = []
    for entry in store.files(deposit_id):
        item = {}
        for field in FILE_FIELDS:
            value = entry.get(field)
            item[field] = _int_or_none(value) if field in INT_FIELDS else value
        files.append(item)
    record['files'] = files
    return record


def qualified_user(user: str, site_id: str) -> str:
    """How the watcher names a remote site's depositor: ``<login>@<site_id>`` (logins are per site)."""
    return f'{user}@{site_id}'


def import_record(store: Any, envelope: Mapping[str, Any], site_id: str, trust_admins: bool = False,
                  now: Optional[float] = None, copied: Optional[Mapping[str, str]] = None) -> str:
    """Write a verified, checked record into the watcher's deposit store. Returns ``imported``, ``unchanged``.

    A site's admin flag is kept only when the watcher trusts that site's admins (``trust_admins``, off by
    default): otherwise a site admin would be an admin of the whole central store. ``imported`` is the
    watcher's clock (``now``), which orders remote deposits; the site's own times are kept but never used for
    that. Raises RecordError for a replay (a version older than the stored one), a different record under an
    already stored version, or a deposit id that belongs to another site.
    """
    record = dict(envelope['record'])
    deposit_id = envelope['deposit_id']
    version = int(envelope['version'])
    record_digest = digest(record)
    existing = store.get(deposit_id)
    if existing is not None:
        if (existing.get('site_id') or '') != site_id:
            raise RecordError(f'{deposit_id} is a deposit of another site')
        stored = int(existing.get('record_version') or 0)
        if version < stored:
            raise RecordError(f'replay: version {version} is older than the stored version {stored}')
        if version == stored:
            if existing.get('record_digest') == record_digest:
                return 'unchanged'
            raise RecordError(f'version {version} was already imported with other content')
    values = {field: record.get(field) for field in RECORD_FIELDS}
    values['user'] = qualified_user(str(record['user']), site_id)
    values['is_admin'] = 1 if trust_admins and record.get('is_admin') else 0
    values.update(site_id=site_id, record_version=version, record_digest=record_digest,
                  imported=float(now if now is not None else time.time()))
    files = [dict(entry) for entry in record['files']]
    if copied:                  # section 24.9: the watcher's own copies in incoming (key -> ETag)
        for entry in files:
            if entry.get('key') in copied:
                entry['etag'] = copied[entry['key']]
        values['h5ad_etag'] = copied.get(str(record.get('watch_key') or ''))
        values['mus_etag'] = copied.get(str(record.get('mus_key') or ''))
    store.import_remote(deposit_id, values, files)
    return 'imported'


def listing_mismatches(record: Mapping[str, Any], listed: Mapping[str, Sequence[Any]]) -> List[str]:
    """Files of an uploaded record whose incoming object is listed with another ETag or size (swapped after signing).

    ``listed``: key -> (etag, size[, last modified]) of the incoming listing. A file not listed (yet, or consumed) is not a
    mismatch here: the route links only what matches the record.
    """
    problems = []
    for entry in record.get('files') or []:
        key = entry.get('key')
        if key in listed and entry.get('state') == 'complete':
            etag, size = listed[key][0], listed[key][1]
            if entry.get('etag') and etag != entry['etag']:
                problems.append(f'{entry.get("name")}: the incoming object has another ETag than the signed record')
            elif entry.get('size') is not None and size != entry['size']:
                problems.append(f'{entry.get("name")}: the incoming object has another size than the signed record')
    return problems


# ── watcher results (Ed25519) ────────────────────────────────────────────────

def load_or_create_result_key(path: Path) -> Any:
    """The watcher's Ed25519 private key (PEM, created once with mode 0600)."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415

    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(f'{path}.{os.getpid()}.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        try:
            os.link(f'{path}.{os.getpid()}.tmp', path)
        except FileExistsError:
            pass
        finally:
            Path(f'{path}.{os.getpid()}.tmp').unlink(missing_ok=True)
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RecordError(f'{path}: readable by group or others; chmod 600')
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def public_key_text(private_key: Any) -> str:
    """``ed25519:<base64>`` of the public half (what sites put in datasets_deposit_watcher_public_keys)."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return ED25519_PREFIX + base64.b64encode(raw).decode('ascii')


def _public_key(text: str) -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415

    if not isinstance(text, str) or not text.startswith(ED25519_PREFIX):
        raise RecordError('a watcher public key reads ed25519:<base64>')
    try:
        raw = base64.b64decode(text[len(ED25519_PREFIX):], validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:  # noqa: BLE001
        raise RecordError('a watcher public key is not a valid Ed25519 key') from None


def sign_result(result: Mapping[str, Any], site_id: str, deposit_id: str, version: int, private_key: Any) -> bytes:
    """The ``result.json`` bytes: the signature line (schema, Ed25519 signature of the body), then the canonical
    body (the envelope with the watcher's answer to one deposit and the signing key's public half)."""
    envelope = {'schema': RESULT_SCHEMA, 'site_id': site_id, 'deposit_id': deposit_id, 'version': int(version),
                'key': public_key_text(private_key), 'result': dict(result)}
    body = canonical(envelope)
    signature = base64.b64encode(private_key.sign(body)).decode('ascii')
    return f'{RESULT_SCHEMA} {signature}\n'.encode('ascii') + body


def verify_result(raw: bytes, site_id: str, deposit_id: str, public_keys: Sequence[str]) -> Dict[str, Any]:
    """The verified envelope of a ``result.json`` for this site's deposit; RecordError otherwise.

    The Ed25519 signature over the exact body bytes is checked against every trusted key before anything is
    parsed; the body is then parsed with a depth limit and must name the key that verified it.
    """
    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415

    trusted = [text for text in public_keys or [] if isinstance(text, str)]
    if not trusted:
        raise RecordError('no watcher public key is configured (datasets_deposit_watcher_public_keys)')
    header, body = _split(raw, MAX_RESULT_BYTES, 'result.json')
    match = RESULT_HEADER_RE.fullmatch(header)
    if match is None:
        raise RecordError('result.json: no signature line')
    try:
        signature = base64.b64decode(match.group(1), validate=True)
    except Exception:  # noqa: BLE001
        raise RecordError('result.json: no signature') from None
    verified_by = None
    for key_text in trusted:
        try:
            _public_key(key_text).verify(signature, body)
        except InvalidSignature:
            continue
        verified_by = key_text
        break
    if verified_by is None:
        raise RecordError('result.json: bad signature, or signed with a key this site does not trust')
    envelope = _parse(body, 'result.json')
    if envelope.get('schema') != RESULT_SCHEMA:
        raise RecordError('result.json: unknown schema')
    if envelope.get('key') != verified_by:
        raise RecordError('result.json: the key inside is not the one that signed it')
    if envelope.get('site_id') != site_id or envelope.get('deposit_id') != deposit_id:
        raise RecordError('result.json: it belongs to another site or deposit')
    version = envelope.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RecordError('result.json: version is not a positive integer')
    if not isinstance(envelope.get('result'), dict):
        raise RecordError('result.json: no result')
    return envelope


def result_from_row(row: Mapping[str, Any], report: Optional[Mapping[str, Any]], refused: Optional[str] = None) -> Dict[str, Any]:
    """The ``result`` the watcher signs for a deposit row of its store (watcher columns plus the redacted report)."""
    def loads(value: Any, default: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return default
        return value if value is not None else default

    return {'state': 'needs_decision' if refused else row.get('watch_state'),
            'db_id': row.get('db_id'), 'version': row.get('version'), 'kind': row.get('kind'),
            'artefact_ref': row.get('artefact_ref'),
            'held_reasons': [str(item) for item in loads(row.get('held_reasons'), [])],
            'error': refused or row.get('error'),
            'warnings': [str(item) for item in loads(row.get('warnings'), [])],
            'report': json.loads(canonical(report).decode()) if isinstance(report, Mapping) else None}


def ensure_no_floats(document: Any) -> Any:
    """``document`` with floats turned into strings (reports carry floats; the signed form has none)."""
    if isinstance(document, float):
        return repr(document)
    if isinstance(document, dict):
        return {str(key): ensure_no_floats(value) for key, value in document.items()}
    if isinstance(document, (list, tuple)):
        return [ensure_no_floats(item) for item in document]
    return document
