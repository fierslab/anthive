"""The central watcher's side of multi-site deposits (design section 24; formats in ``deposit_records.py``).

Each poll, before the deposits are linked to the incoming objects:

* :meth:`RemoteSites.import_records` lists ``<staging_root><site>/<deposit>/record.json`` of every known site in
  the ingestion bucket, verifies the signature (a record that fails is never imported, only logged: its ids are
  not trusted), checks the content rules, refuses a replayed or rewritten version, compares the incoming objects
  with the signed ETags and sizes, checks update rights against this store's owners, and imports the record into
  the watcher's deposit store. From there the existing route links, checks and publishes it like a local deposit.
  An authentic record that cannot be imported gets a signed ``needs_decision`` result with the reason.

After the poll:

* :meth:`RemoteSites.export_results` writes a signed ``result.json`` for every imported deposit whose outcome
  changed (state, held reasons, the redacted report, db id and version or artefact reference).

The watcher never touches a site's staging except reading its record and writing its result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import deposit_records as dr

STATE_FILE = 'remote_deposits.json'
#: record states the watcher imports (an open deposit is the site's business until it is uploaded)
IMPORTED_STATES = ('uploaded', 'withdrawn', 'expired')


class RemoteSites:
    """Known sites, the result key and the bookkeeping of one central watcher."""

    def __init__(self, *, s3: Any, bucket: str, state_dir: Path, sites: Mapping[str, Mapping[str, bytes]],
                 result_key: Any, staging_root: str, incoming_prefix: str, local_site_id: str = '',
                 log: Callable[[str], None] = print):
        self.s3 = s3
        self.bucket = bucket
        self.state_dir = Path(state_dir)
        self.sites = dict(sites)
        self.result_key = result_key
        self.staging_root = dr.staging_root(staging_root)
        self.incoming_prefix = incoming_prefix
        self.local_site_id = local_site_id
        self.log = log
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

    def import_records(self, store: Any, listed: Mapping[str, Tuple[str, int]]) -> Dict[str, int]:
        """Verify and import every new record version of the known sites. ``listed``: incoming key -> (etag, size)."""
        counts = {'imported': 0, 'refused': 0, 'ignored': 0}
        state = self._load()
        try:
            objects = self._list_records()
        except Exception as exc:  # noqa: BLE001 - listing trouble: the next poll tries again
            self._log_once('list', f'listing {self.staging_root} failed ({type(exc).__name__})')
            return counts
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
            outcome = self._import_one(store, state, key, site_id, deposit_id, listed)
            counts[outcome] = counts.get(outcome, 0) + 1
        self._save(state)
        return counts

    def _import_one(self, store: Any, state: Dict[str, Any], key: str, site_id: str, deposit_id: str,
                    listed: Mapping[str, Tuple[str, int]]) -> str:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            raw = response['Body'].read(dr.MAX_RECORD_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            self._log_once(key, f'{key}: unreadable ({type(exc).__name__}); retried next poll')
            return 'ignored'
        try:
            envelope = dr.verify_record(raw, site_id, deposit_id, self.sites)
        except dr.RecordError as exc:                      # not authentic: nothing in it is trusted, nothing answered
            self._log_once(key, f'{key}: refused, {exc}')
            return 'refused'
        seen_key = f'{site_id}/{deposit_id}'
        version = envelope['version']
        seen = int(state['seen'].get(seen_key) or 0)
        if version < seen:
            self._log_once(key, f'{key}: refused, replay of version {version} (seen {seen})')
            return 'refused'
        record = envelope['record']
        problems = dr.record_problems(record, site_id, deposit_id, self.incoming_prefix, self.staging_root)
        if not problems and record.get('state') == 'uploaded':
            problems = dr.listing_mismatches(record, listed)
        if not problems and record.get('state') == 'uploaded' and record.get('update_of') and not record.get('is_admin'):
            owner = store.owner_of_dataset(str(record['update_of']))
            if owner != dr.qualified_user(str(record['user']), site_id):
                problems = [f'update of {record["update_of"]} refused: the central store does not record '
                            f'{record["user"]}@{site_id} as its owner (an admin of the central site assigns '
                            f'owners with ant-ingest-watch owner set)']
        if problems:
            self._refuse(state, site_id, deposit_id, '; '.join(problems))
            state['seen'][seen_key] = version
            return 'refused'
        if record.get('state') not in IMPORTED_STATES:
            state['seen'][seen_key] = version
            return 'ignored'
        try:
            result = dr.import_record(store, envelope, site_id)
        except dr.RecordError as exc:
            self._refuse(state, site_id, deposit_id, str(exc))
            return 'refused'
        state['seen'][seen_key] = version
        if result == 'imported':
            self.log(f'remote deposits: imported {deposit_id} of {site_id} (version {version}, {record.get("state")})')
            return 'imported'
        return 'ignored'

    # results ------------------------------------------------------------------
    def _write_result(self, state: Dict[str, Any], site_id: str, deposit_id: str, result: Dict[str, Any]) -> bool:
        result = dr.ensure_no_floats(result)
        content = dr.digest(result)
        entry = state['results'].get(f'{site_id}/{deposit_id}') or [0, '']
        if entry[1] == content:
            return False
        version = int(entry[0]) + 1
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
                if self._write_result(state, row['site_id'], row['id'], dr.result_from_row(row, report)):
                    written += 1
                    store.update(row['id'], result_version=state['results'][f'{row["site_id"]}/{row["id"]}'][0])
            except Exception as exc:  # noqa: BLE001 - the next poll writes it again
                self._log_once(f'{row["id"]}:export', f'{row["id"]}: result not written ({type(exc).__name__})')
        self._save(state)
        return written
