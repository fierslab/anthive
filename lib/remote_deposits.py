"""The central watcher's side of multi-site deposits (design section 24; formats in ``deposit_records.py``).

Each poll, before the deposits are linked to the incoming objects:

* :meth:`RemoteSites.import_records` lists ``<staging_root><site>/<deposit>/record.json`` of every known site in
  the ingestion bucket, verifies the signature (a record that fails is never imported, only logged: its ids are
  not trusted), checks the content rules, refuses a replayed or rewritten version, compares the incoming objects
  with the signed ETags and sizes, checks update rights against this store's owners, and imports the record into
  the watcher's deposit store. From there the existing route links, checks and publishes it like a local deposit.
  An authentic record that cannot be imported gets a signed ``needs_decision`` result with the reason.
* An authentic site is not trusted with another site's uploads (review S1): every incoming object an uploaded
  record names (its files, and the h5ad and sidecar it is linked by) must be listed, carry the server's
  ``x-amz-meta-deposit`` mark of that deposit (or of the deposit it continues, which must be one of the same
  site), and belong to no other deposit. When another deposit that still waits claims the same object, both are
  held (``disputed``): with shared ingestion keys the watcher cannot tell which site is right, so an admin decides.
* A site's ``is_admin`` counts only for the sites in ``trusted_admin_sites`` (review S2; default none).

After the poll:

* :meth:`RemoteSites.export_results` writes a signed ``result.json`` for every imported deposit whose outcome
  changed (state, held reasons, the redacted report, db id and version or artefact reference).

The watcher never touches a site's staging except reading its record and writing its result.
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
UPDATE_KINDS = ('dataset', 'metadata_update', 'de_only')
#: watch states after which an object a deposit claimed is its own for good
FINAL_WATCH_STATES = ('live', 'rejected', 'failed')


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
                 clock: Callable[[], float] = time.time):
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
        if not problems and record.get('state') == 'uploaded':
            problems = dr.listing_mismatches(record, listed)
        if not problems and record.get('state') == 'uploaded':
            problems = self._ownership_problems(store, state, record, site_id, deposit_id, listed, record_mtime)
        if not problems and record.get('state') == 'uploaded':
            problems = self._rights_problems(store, record, site_id)
        if not problems and record.get('state') in ('withdrawn', 'expired'):
            self._release_claims(state, f'{site_id}/{deposit_id}')
        if problems:
            self._refuse(state, site_id, deposit_id, '; '.join(problems))
            state['seen'][seen_key] = version
            return 'refused'
        if record.get('state') not in IMPORTED_STATES:
            state['seen'][seen_key] = version
            return 'ignored'
        try:
            result = dr.import_record(store, envelope, site_id, trust_admins=site_id in self.trusted_admin_sites,
                                      now=self.clock())
        except dr.RecordError as exc:
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

    def _ownership_problems(self, store: Any, state: Dict[str, Any], record: Mapping[str, Any], site_id: str,
                            deposit_id: str, listed: Mapping[str, Tuple[str, int, float]],
                            record_mtime: float) -> List[str]:
        """Review S1: the incoming objects an uploaded record names are this deposit's own.

        1. ``continues`` names a deposit of the same site already in this store (Deferred while it is not).
        2. Every file is complete; every object (the files, and the h5ad and sidecar the item is linked by) is
           listed (Deferred otherwise) with the signed ETag.
        3. An object written after this record (S3's LastModified, one clock for both) was rewritten after the
           site signed: another key holder re-marked or replaced it. The record is refused, keeps its claim on
           the objects, and a waiting deposit the object's mark now names is disputed: whoever rewrote another
           site's upload gets at most both held, never the upload.
        4. No other deposit claims one of the objects (key and ETag), outside the chain: a deposit of this store
           that is open or still waits, or an earlier authentic record kept in the state file (refused in step 3,
           or imported; until it is withdrawn or expired, or its deposit is final here).
        5. Every object carries the server's deposit mark of this deposit or of a deposit it continues; only then
           does the record claim them.
        """
        chain = [deposit_id]
        if record.get('continues'):
            continued = store.get(str(record['continues']))
            if continued is None:
                raise Deferred(f'the continued deposit {record["continues"]} is not imported yet')
            if (continued.get('site_id') or '') != site_id:
                return [f'continues {record["continues"]}, which is not a deposit of {site_id}']
            chain += [entry for entry in store.chain(str(record['continues'])) if entry not in chain]
            if any((store.get(entry) or {}).get('site_id') != site_id for entry in chain[1:]):
                return [f'continues a chain that leaves {site_id}']
        objects: Dict[str, str] = {}
        for entry in record.get('files') or []:
            if entry.get('state') != 'complete' or not entry.get('etag'):
                return [f'{entry.get("name")}: an uploaded record names a file that is not complete']
            objects[str(entry['key'])] = str(entry['etag'])
        for key_field, etag_field in (('watch_key', 'h5ad_etag'), ('mus_key', 'mus_etag')):
            key, etag = record.get(key_field), record.get(etag_field)
            if key and not str(key).startswith('artefact:'):
                if not etag:
                    continue                           # never linked: find_for_item needs both ETags
                if objects.get(str(key), etag) != etag:
                    return [f'{key_field} and its file entry name different ETags']
                objects[str(key)] = str(etag)
        for key in sorted(objects):
            if key not in listed:
                raise Deferred(f'{key} is not listed in incoming yet')
        problems = [f'{key}: the incoming object has another ETag than the signed record'
                    for key, etag in sorted(objects.items()) if listed[key][0] != etag]
        if problems:
            return problems[:20]
        claims = state.setdefault('claims', {})
        me = f'{site_id}/{deposit_id}'
        mine = {f'{site_id}/{entry}' for entry in chain}

        # 3. rewritten after signing
        rewritten = sorted(key for key in objects if float(listed[key][2] or 0) > float(record_mtime or 0))
        if rewritten:
            for key, etag in objects.items():
                claims.setdefault(f'{key}\t{etag}', me)
            for key in rewritten:
                mark = self._mark(key)
                row = store.get(mark) if mark and mark not in chain else None
                if row is not None and row.get('state') == 'uploaded' and not row.get('disputed') \
                        and row.get('watch_state') not in FINAL_WATCH_STATES:
                    reason = (f'disputed: {key} was rewritten after {deposit_id} of {site_id} signed it and now '
                              f'carries the mark of {mark}' + (f' of {row["site_id"]}' if row.get('site_id') else '')
                              + '; neither is published, an admin of the central site decides')
                    store.dispute(mark, reason)
                    self.log(f'remote deposits: {reason}')
            return [f'{key} was rewritten after this record was written (another holder of the ingestion keys '
                    f'replaced or re-marked it); an admin of the central site decides' for key in rewritten][:20]

        # 4. claimed by another deposit
        for claim in store.claims(objects, exclude=deposit_id):
            if claim['id'] in chain or claim.get('watch_state') in FINAL_WATCH_STATES:
                continue        # a final deposit's object was consumed; the same content uploaded anew has the
                #                 same ETag, so the deposit mark decides
            owner = f'{claim["id"]}' + (f' of {claim["site_id"]}' if claim.get('site_id') else '')
            what = 'locked by the open deposit' if claim.get('open') else 'claimed by the deposit'
            problems.append(f'{claim["key"]} is {what} {owner}')
        for key, etag in sorted(objects.items()):
            holder = claims.get(f'{key}\t{etag}')
            if not holder or holder in mine:
                continue
            holder_site, _, holder_id = holder.partition('/')
            row = store.get(holder_id)
            if row is not None and (row.get('site_id') or '') == holder_site and (
                    row.get('state') in ('withdrawn', 'expired') or row.get('watch_state') in FINAL_WATCH_STATES):
                del claims[f'{key}\t{etag}']                # its deposit is over: the claim is stale
                continue
            problems.append(f'{key} is claimed by the deposit {holder_id} of {holder_site}; that site withdraws it '
                            f'first, or an admin of the central site decides')
        if problems:
            return sorted(set(problems))[:20]

        # 5. the server's mark
        for key in sorted(objects):
            mark = self._mark(key)
            if mark not in chain:
                problems.append(f'{key} does not carry the mark of {deposit_id} (it is not an upload of this deposit)')
        if problems:
            return problems[:20]
        for key, etag in objects.items():
            claims.setdefault(f'{key}\t{etag}', me)
        return []

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
