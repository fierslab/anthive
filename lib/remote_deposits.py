"""The central watcher's side of multi-site deposits (design section 24; formats in ``deposit_records.py``).

Each poll, before the deposits are linked to the incoming objects:

* :meth:`RemoteSites.import_records` lists ``<staging_root><site>/<deposit>/record.json`` of every known site in
  the ingestion bucket, verifies the signature (a record that fails is never imported, only logged: its ids are
  not trusted), checks the content rules, refuses a replayed or rewritten version, compares the incoming objects
  with the signed ETags and sizes, checks update rights against this store's owners, and imports the record into
  the watcher's deposit store. From there the existing route links, checks and publishes it like a local deposit.
  An authentic record that cannot be imported gets a signed ``needs_decision`` result with the reason.
* Sites never write incoming (section 24.9): an uploaded record names its files in the site's own staging
  folder with their staging ETags and sha256. The watcher HEADs each staged upload (the signed ETag and size,
  the server's ``deposit`` and ``sha256`` marks), checks that no other deposit claims the incoming keys, copies
  the uploads into incoming itself (marks set by the watcher, pinned to the staging ETag; streamed and
  sha256-checked between two key pairs) and imports the record with the ETags of its own copies. A withdrawn or
  expired record has those copies removed.
* A site's ``is_admin`` counts only for the sites in ``trusted_admin_sites`` (review S2; default none).

After the poll:

* :meth:`RemoteSites.export_results` writes a signed ``result.json`` for every imported deposit whose outcome
  changed (state, held reasons, the redacted report, db id and version or artefact reference).

The watcher never writes a site's staging except its result; it reads the record and the staged uploads.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import deposit_records as dr

STATE_FILE = 'remote_deposits.json'
#: record states the watcher imports (an open deposit is the site's business until it is uploaded)
IMPORTED_STATES = ('uploaded', 'withdrawn', 'expired')
#: deposit kinds whose import needs the right to update the dataset (``update_of``); a plain artefact on a
#: visible dataset needs none, as on a single host (a de_table artefact does)
UPDATE_KINDS = ('dataset', 'metadata_update', 'de_only', 'obs_columns')
#: watch states after which an object a deposit claimed is its own for good
FINAL_WATCH_STATES = ('live', 'rejected', 'failed')
#: copies up to this size are one CopyObject; larger ones a managed (multipart) copy
COPY_SINGLE_MAX = 5 * 1024 ** 3


def epoch(value: Any) -> float:
    """A listing's LastModified (datetime, or a number in tests) as epoch seconds; 0.0 when missing."""
    if hasattr(value, 'timestamp'):
        return float(value.timestamp())
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class Deferred(Exception):
    """The record cannot be judged yet (an object not listed, a continued deposit not imported): next poll."""


class RemoteSites:
    """Known sites, the result key and the bookkeeping of one central watcher."""

    def __init__(self, *, s3: Any, bucket: str, state_dir: Path, sites: Mapping[str, Mapping[str, bytes]],
                 result_key: Any, staging_root: str, incoming_prefix: str, local_site_id: str = '',
                 trusted_admin_sites: Optional[List[str]] = None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, catalog_bucket: Optional[str] = None,
                 catalog_prefix: str = ''):
        self.s3 = s3
        self.bucket = bucket
        self.state_dir = Path(state_dir)
        self.sites = dict(sites)
        self.result_key = result_key
        self.staging_root = dr.staging_root(staging_root)
        self.incoming_prefix = incoming_prefix
        self.local_site_id = local_site_id
        self.trusted_admin_sites = set(trusted_admin_sites or [])
        self.log = log
        self.clock = clock
        #: where the published files of a metadata-only update are reused from (the data bucket)
        self.catalog_bucket = catalog_bucket or bucket
        self.catalog_prefix = catalog_prefix
        self._logged: set = set()

    # bookkeeping (what was seen and answered) --------------------------------
    def _state_path(self) -> Path:
        return self.state_dir / STATE_FILE

    def _load(self) -> Dict[str, Any]:
        try:
            state = json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            state = {}
        state.setdefault('seen', {})
        state.setdefault('results', {})
        return state

    def _save(self, state: Mapping[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix('.json.partial')
        partial.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(partial, path)

    def _log_once(self, key: str, message: str) -> None:
        if (key, message) not in self._logged:
            self._logged.add((key, message))
            self.log(f'remote deposits: {message}')

    # import -------------------------------------------------------------------
    def _list_records(self) -> List[Dict[str, Any]]:
        objects: List[Dict[str, Any]] = []
        paginator = self.s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.staging_root):
            objects.extend(page.get('Contents', []) or [])
        return [obj for obj in objects if str(obj.get('Key', '')).endswith('/' + dr.RECORD_NAME)]

    def import_records(self, store: Any, listed: Mapping[str, Tuple[Any, ...]]) -> Dict[str, int]:
        """Verify and import every new record version of the known sites. ``listed``: incoming key -> (etag,
        size, last modified as epoch seconds; 0 when unknown)."""
        listed = {key: (tuple(value) + (0.0,))[:3] for key, value in listed.items()}
        counts = {'imported': 0, 'refused': 0, 'ignored': 0}
        state = self._load()
        try:
            objects = self._list_records()
        except Exception as exc:  # noqa: BLE001 - listing trouble: the next poll tries again
            self._log_once('list', f'listing {self.staging_root} failed ({type(exc).__name__})')
            return counts
        verified: List[Tuple[str, str, str, Dict[str, Any], float]] = []
        for obj in objects:
            key = str(obj['Key'])
            parsed = dr.parse_object_key(key, self.staging_root)
            if parsed is None:
                self._log_once(key, f'{key}: not a deposit record path; ignored')
                counts['ignored'] += 1
                continue
            site_id, deposit_id = parsed
            if site_id == self.local_site_id:
                continue                                   # this host's own API shares the store directly
            if site_id not in self.sites:
                self._log_once(key, f'{key}: site {site_id!r} is not known to this watcher; ignored')
                counts['ignored'] += 1
                continue
            if int(obj.get('Size') or 0) > dr.MAX_RECORD_BYTES:
                self._log_once(key, f'{key}: larger than {dr.MAX_RECORD_BYTES} bytes; ignored')
                counts['ignored'] += 1
                continue
            try:
                envelope = self._read_verified(key, site_id, deposit_id)
            except Exception as exc:  # noqa: BLE001 - one bad record never stops the others
                self._log_once(key, f'{key}: not read ({type(exc).__name__}); retried next poll')
                envelope = 'ignored'
            if isinstance(envelope, str):
                counts[envelope] = counts.get(envelope, 0) + 1
                continue
            verified.append((key, site_id, deposit_id, envelope, epoch(obj.get('LastModified'))))
        # withdrawn and expired records first: they release their claims before the uploads of this poll
        verified.sort(key=lambda entry: entry[3]['record'].get('state') not in ('withdrawn', 'expired'))
        for key, site_id, deposit_id, envelope, record_mtime in verified:
            try:
                outcome = self._import_one(store, state, key, site_id, deposit_id, envelope, listed, record_mtime)
            except Deferred as reason:
                self._log_once(key, f'{key}: waits, {reason}')
                outcome = 'waiting'
            except Exception as exc:  # noqa: BLE001 - one bad record never stops the others
                self._log_once(key, f'{key}: not imported ({type(exc).__name__}); retried next poll')
                outcome = 'ignored'
            counts[outcome] = counts.get(outcome, 0) + 1
        self._save(state)
        return counts

    def _read_verified(self, key: str, site_id: str, deposit_id: str) -> Any:
        """The verified envelope of a record, or the outcome word when it is unreadable or not authentic."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            raw = response['Body'].read(dr.MAX_RECORD_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            self._log_once(key, f'{key}: unreadable ({type(exc).__name__}); retried next poll')
            return 'ignored'
        try:
            return dr.verify_record(raw, site_id, deposit_id, self.sites)
        except dr.RecordError as exc:                      # not authentic: nothing in it is trusted, nothing answered
            self._log_once(key, f'{key}: refused, {exc}')
            return 'refused'

    def _import_one(self, store: Any, state: Dict[str, Any], key: str, site_id: str, deposit_id: str,
                    envelope: Dict[str, Any], listed: Mapping[str, Tuple[str, int, float]],
                    record_mtime: float = 0.0) -> str:
        seen_key = f'{site_id}/{deposit_id}'
        version = envelope['version']
        seen = int(state['seen'].get(seen_key) or 0)
        if version < seen:
            self._log_once(key, f'{key}: refused, replay of version {version} (seen {seen})')
            return 'refused'
        record = envelope['record']
        existing = store.get(deposit_id)
        if existing is not None and (existing.get('site_id') or '') == site_id \
                and int(existing.get('record_version') or 0) <= version \
                and existing.get('record_digest') == dr.digest(record):
            # imported before with this content (its objects may be consumed by now): only the counter moves
            if int(existing.get('record_version') or 0) < version:
                store.update(deposit_id, record_version=version)
            state['seen'][seen_key] = version
            return 'ignored'
        problems = dr.record_problems(record, site_id, deposit_id, self.incoming_prefix, self.staging_root)
        sources: Dict[str, Tuple[str, str]] = {}
        if not problems and record.get('state') == 'uploaded':
            problems = self._rights_problems(store, record, site_id)
        if not problems and record.get('state') == 'uploaded':
            problems, sources = self._staged_problems(store, state, record, site_id, deposit_id)
        if not problems and record.get('state') in ('withdrawn', 'expired'):
            self._remove_copies(store, state, record, site_id, deposit_id)
            self._release_claims(state, f'{site_id}/{deposit_id}')
        if problems:
            self._refuse(state, site_id, deposit_id, '; '.join(problems))
            state['seen'][seen_key] = version
            return 'refused'
        if record.get('state') not in IMPORTED_STATES:
            state['seen'][seen_key] = version
            return 'ignored'
        copied: Dict[str, str] = {}
        if sources:
            try:
                copied = self._stage_in(state, record, site_id, deposit_id, sources)
            except Exception as exc:  # noqa: BLE001 - a failed copy: the record is looked at again next poll
                self._log_once(key, f'{key}: copy into incoming failed ({type(exc).__name__}); retried next poll')
                return 'waiting'
        try:
            result = dr.import_record(store, envelope, site_id, trust_admins=site_id in self.trusted_admin_sites,
                                      now=self.clock(), copied=copied)
        except dr.RecordError as exc:
            self._delete_copies(copied)
            self._refuse(state, site_id, deposit_id, str(exc))
            return 'refused'
        state['seen'][seen_key] = version
        if result == 'imported':
            self.log(f'remote deposits: imported {deposit_id} of {site_id} (version {version}, {record.get("state")})')
            return 'imported'
        return 'ignored'

    def _is_admin(self, record: Mapping[str, Any], site_id: str) -> bool:
        return bool(record.get('is_admin')) and site_id in self.trusted_admin_sites

    def _rights_problems(self, store: Any, record: Mapping[str, Any], site_id: str) -> List[str]:
        """Update rights against this store's owners: for dataset, metadata_update and de_only deposits with
        ``update_of``, and for de_table artefacts (their scope dataset). A plain artefact or a collection needs
        none (the site checked visibility; the artefact route checks owners of artefact versions)."""
        kind = record.get('deposit_kind')
        needs_owner = kind in UPDATE_KINDS
        if kind == 'artefact':
            try:
                block = json.loads(record.get('artefact') or '{}')
            except (TypeError, ValueError):
                return ['the artefact block is not JSON']
            needs_owner = isinstance(block, dict) and block.get('kind') == 'de_table'
        if not needs_owner or not record.get('update_of') or self._is_admin(record, site_id):
            return []
        owner = store.owner_of_dataset(str(record['update_of']))
        if owner != dr.qualified_user(str(record['user']), site_id):
            return [f'update of {record["update_of"]} refused: the central store does not record '
                    f'{record["user"]}@{site_id} as its owner (an admin of the central site assigns owners with '
                    f'ant-ingest-watch owner set)']
        return []

    @staticmethod
    def _release_claims(state: Dict[str, Any], holder: str) -> None:
        claims = state.setdefault('claims', {})
        for token in [token for token, owner in claims.items() if owner == holder]:
            del claims[token]

    def _staged_problems(self, store: Any, state: Dict[str, Any], record: Mapping[str, Any], site_id: str,
                         deposit_id: str) -> Tuple[List[str], Dict[str, Tuple[str, str]]]:
        """Section 24.9: the uploads of an authentic, uploaded record are still in the site's staging folder.

        Per file: the source is its staging key, which ``record_problems`` already confined to
        ``<staging_root><site_id>/<deposit_id>/`` (a reused published file of a metadata-only update is the
        catalog's own object of the dataset instead); a HEAD must show the signed staging ETag and size, and a
        staged upload the server's marks (``deposit`` = this deposit, ``sha256`` = the signed sha256). Then the
        incoming target keys must be free: no open or waiting deposit of this store and no earlier authentic
        record claims one (the key alone, whatever the ETag), and an object that waits there must be one this
        watcher wrote for this deposit (it is overwritten by the copy). Returns ``(problems, sources)`` with
        ``sources``: incoming key -> (source bucket, source key)."""
        problems: List[str] = []
        sources: Dict[str, Tuple[str, str]] = {}
        files = list(record.get('files') or [])
        for entry in files:
            name, key = str(entry.get('name')), str(entry.get('key'))
            staging_key = str(entry.get('staging_key') or '')
            if entry.get('method') == 'reuse':
                bucket = self.catalog_bucket
                expected = f'{self.catalog_prefix}{record.get("data_project")}/{name}'
                if record.get('deposit_kind') != 'metadata_update' or not record.get('update_of') \
                        or staging_key != expected or name not in (f'{record.get("basename")}.h5ad',
                                                                   f'{record.get("basename")}.h5ad.mus'):
                    problems.append(f'{name}: a reused file must be the published file {expected} of a metadata '
                                    f'update')
                    continue
            else:
                bucket = self.bucket
            try:
                head = self.s3.head_object(Bucket=bucket, Key=staging_key)
            except Exception:  # noqa: BLE001 - absent (or unreadable): the record cannot be staged in
                problems.append(f'{name}: the staged upload is gone (withdrawn, swept or never complete)')
                continue
            metadata = {str(k).lower(): str(v) for k, v in (head.get('Metadata') or {}).items()}
            if str(head.get('ETag', '')).strip('"') != str(entry.get('staging_etag')) \
                    or int(head.get('ContentLength') or -1) != int(entry.get('size') or -2):
                problems.append(f'{name}: the staged upload is not the signed one (ETag or size changed after signing)')
                continue
            if entry.get('method') != 'reuse' and (metadata.get('deposit') != deposit_id
                                                   or metadata.get('sha256') != entry.get('sha256')):
                problems.append(f'{name}: the staged upload does not carry the marks of {deposit_id} and its sha256')
                continue
            if entry.get('method') == 'reuse' and name.endswith('.h5ad') \
                    and metadata.get('sha256') != entry.get('sha256'):
                problems.append(f'{name}: the published file is not the one the site verified (sha256)')
                continue
            sources[key] = (bucket, staging_key)
        if problems:
            return problems[:20], {}
        # the incoming targets: claimed by nobody else, and empty or written by this watcher for this deposit
        claims = state.setdefault('claims', {})
        me = f'{site_id}/{deposit_id}'
        for claim in store.claims(dict.fromkeys(sources, ''), exclude=deposit_id):
            if claim.get('watch_state') in FINAL_WATCH_STATES:
                continue        # a final deposit's object was consumed or is never used again: its key is free
            owner = f'{claim["id"]}' + (f' of {claim["site_id"]}' if claim.get('site_id') else '')
            what = 'locked by the open deposit' if claim.get('open') else 'claimed by the deposit'
            problems.append(f'{claim["key"]} is {what} {owner}')
        for key in sorted(sources):
            holder = claims.get(key)
            if not holder or holder == me:
                continue
            holder_site, _, holder_id = holder.partition('/')
            row = store.get(holder_id)
            if row is not None and (row.get('site_id') or '') == holder_site and (
                    row.get('state') in ('withdrawn', 'expired') or row.get('watch_state') in FINAL_WATCH_STATES):
                del claims[key]                # its deposit is over: the claim is stale
                continue
            problems.append(f'{key} is claimed by the deposit {holder_id} of {holder_site}; that site withdraws it '
                            f'first, or an admin of the central site decides')
        for key in sorted(sources):
            mark = self._mark_or_none(key)
            if mark is not None and mark != deposit_id:
                problems.append(f'{key}: another upload waits at this incoming key (mark {mark or "none"}); it is '
                                f'removed first (an admin of the central site decides)')
        if problems:
            return sorted(set(problems))[:20], {}
        return [], sources

    def _stage_in(self, state: Dict[str, Any], record: Mapping[str, Any], site_id: str, deposit_id: str,
                  sources: Mapping[str, Tuple[str, str]]) -> Dict[str, str]:
        """Copy each verified staged upload to its incoming key (pinned to the signed staging ETag, the deposit
        and sha256 marks set by the watcher) and HEAD it; returns incoming key -> the copy's ETag. The copy
        between two key pairs is streamed and checked against the sha256 (``s3_router``)."""
        by_key = {str(entry.get('key')): entry for entry in record.get('files') or []}
        claims = state.setdefault('claims', {})
        copies = state.setdefault('copies', {})
        me = f'{site_id}/{deposit_id}'
        copied: Dict[str, str] = {}
        try:
            for key, (bucket, source_key) in sorted(sources.items()):
                self._copy_one(by_key[key], key, bucket, source_key, deposit_id, copied)
        except BaseException:
            self._delete_copies(copied)        # never leave marked copies of a deposit that is not imported
            raise
        for key, etag in copied.items():
            claims[key] = me
            copies[key] = [me, etag]
        return copied

    def _delete_copies(self, copied: Mapping[str, str]) -> None:
        from ingest_pipeline import delete_consumed  # noqa: PLC0415
        for key, etag in copied.items():
            try:
                delete_consumed(self.s3, self.bucket, self.incoming_prefix, {key: etag})
            except Exception:  # noqa: BLE001 - left for the unlinked-object check and an admin
                pass

    def _copy_one(self, entry: Mapping[str, Any], key: str, bucket: str, source_key: str, deposit_id: str,
                  copied: Dict[str, str]) -> None:
        metadata = {'deposit': deposit_id, 'sha256': str(entry['sha256'])}
        condition = f'"{entry["staging_etag"]}"'
        source = {'Bucket': bucket, 'Key': source_key}
        if int(entry['size']) <= COPY_SINGLE_MAX:
            self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource=source, Metadata=metadata,
                                MetadataDirective='REPLACE', CopySourceIfMatch=condition)
        else:
            self.s3.copy(source, self.bucket, key, ExtraArgs={'Metadata': metadata, 'MetadataDirective': 'REPLACE',
                                                              'CopySourceIfMatch': condition})
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        marks = {str(k).lower(): str(v) for k, v in (head.get('Metadata') or {}).items()}
        if int(head.get('ContentLength') or -1) != int(entry['size']) or marks.get('deposit') != deposit_id:
            raise RuntimeError(f'the copy of {entry.get("name")} into incoming could not be verified')
        copied[key] = str(head.get('ETag', '')).strip('"')

    def _remove_copies(self, store: Any, state: Dict[str, Any], record: Mapping[str, Any], site_id: str,
                       deposit_id: str) -> None:
        """A withdrawn or expired record: delete the incoming copies this watcher made for it (only objects that
        still carry its mark and the ETag of the copy), unless the deposit was published meanwhile."""
        from ingest_pipeline import delete_consumed  # noqa: PLC0415
        row = store.get(deposit_id)
        if row is not None and row.get('watch_state') == 'live':
            return
        copies = state.setdefault('copies', {})
        me = f'{site_id}/{deposit_id}'
        for key in [key for key, value in copies.items() if value and value[0] == me]:
            try:
                delete_consumed(self.s3, self.bucket, self.incoming_prefix, {key: copies[key][1]})
            except Exception as exc:  # noqa: BLE001 - the next record version or an admin cleans up
                self._log_once(f'{me}:{key}', f'{key}: copy of withdrawn {deposit_id} not deleted ({type(exc).__name__})')
                continue
            del copies[key]

    def _mark_or_none(self, key: str) -> Optional[str]:
        """``x-amz-meta-deposit`` of an incoming object ('' when unmarked), None when there is no object."""
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            code = str(((getattr(exc, 'response', None) or {}).get('Error') or {}).get('Code') or '')
            if isinstance(exc, KeyError) or code in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise
        metadata = {str(name).lower(): str(value) for name, value in (head.get('Metadata') or {}).items()}
        return metadata.get('deposit', '')

    def _mark(self, key: str) -> str:
        """``x-amz-meta-deposit`` of an incoming object ('' when unmarked)."""
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        metadata = {str(name).lower(): str(value) for name, value in (head.get('Metadata') or {}).items()}
        return metadata.get('deposit', '')


    # results ------------------------------------------------------------------
    def _write_result(self, state: Dict[str, Any], site_id: str, deposit_id: str, result: Dict[str, Any],
                      floor: Optional[int] = None) -> bool:
        result = dr.ensure_no_floats(result)
        content = dr.digest(result)
        entry = state['results'].get(f'{site_id}/{deposit_id}') or [0, '']
        if entry[1] == content:
            return False
        # review N2: milliseconds of the watcher's clock, so a lost state file never makes a site ignore new
        # results (a site takes only a version above the one it has)
        version = max(int(entry[0]) + 1, int(floor or 0) + 1, int(self.clock() * 1000))
        raw = dr.sign_result(result, site_id, deposit_id, version, self.result_key)
        if len(raw) > dr.MAX_RESULT_BYTES:                 # a huge report: the outcome without it
            result = {**result, 'report': None}
            raw = dr.sign_result(result, site_id, deposit_id, version, self.result_key)
        key = dr.object_key(self.staging_root, site_id, deposit_id, dr.RESULT_NAME)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType='application/json')
        state['results'][f'{site_id}/{deposit_id}'] = [version, content]
        return True

    def _refuse(self, state: Dict[str, Any], site_id: str, deposit_id: str, reason: str) -> None:
        self._log_once(f'{site_id}/{deposit_id}', f'{site_id}/{deposit_id}: refused, {reason}')
        try:
            self._write_result(state, site_id, deposit_id, dr.result_from_row({}, None, refused=reason[:2000]))
        except Exception as exc:  # noqa: BLE001
            self._log_once(f'{site_id}/{deposit_id}:result', f'{site_id}/{deposit_id}: result not written '
                                                              f'({type(exc).__name__})')

    def export_results(self, store: Any) -> int:
        """Write a signed result for every imported deposit whose outcome changed. Returns how many were written."""
        state = self._load()
        written = 0
        for row in store.remote_rows(self.local_site_id):
            if not row.get('watch_state') or row.get('site_id') not in self.sites:
                continue
            report = None
            if row.get('report_file'):
                try:
                    report = json.loads(Path(row['report_file']).read_text())
                except (OSError, ValueError):
                    report = None
            try:
                if self._write_result(state, row['site_id'], row['id'], dr.result_from_row(row, report),
                                      floor=row.get('result_version')):
                    written += 1
                    store.update(row['id'], result_version=state['results'][f'{row["site_id"]}/{row["id"]}'][0])
            except Exception as exc:  # noqa: BLE001 - the next poll writes it again
                self._log_once(f'{row["id"]}:export', f'{row["id"]}: result not written ({type(exc).__name__})')
        self._save(state)
        return written
