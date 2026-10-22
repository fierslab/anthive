"""Versioned merge of pending obs columns into the h5ad, run by the watcher in a quiet window (design 26.3).

Obs columns written to a served duckdb (``AnnotationWriter``: obs-columns deposits, ant-score, ant-annotate)
are listed in ``__pending_cols`` until the h5ad, the long-term source of truth, has them. ``ant-merge-h5ad``
writes them into the h5ad in place; the watcher instead makes a new dataset version:

1. Gates: inside ``ingest_watch.merge_window`` (default ``01:00-06:00`` local time) unless ``--now``; no
   deposit for the dataset open or waiting; the host's load and free memory under the fleet's limits
   (``host_load``); free disk for one more copy of the h5ad. One dataset at a time (the watcher's lock), and
   the run stops between datasets when a stop is requested.
2. Checks: the pending state is consistent (``pending_state_diagnosis``), the duckdb and h5ad hold the same
   cells, the store h5ad is the published one (its sha256 is the ``.md``'s).
3. A copy of the h5ad (``.<name>.h5ad.merge-work`` next to it) gets every pending column with the duckdb's
   values (an existing column of that name is replaced: the duckdb is the truth for pending columns); the
   copy is compared with the published h5ad (``h5ad_compare.classify``): additive is a minor version, a
   replaced value a major version (the archived h5ad holds values the new one lacks, design 25).
4. Placement through a ``SwapJournal`` in ``<ingest_state_dir>/merge_swaps/``: the current h5ad, ``.md``,
   sidecar and scores are hard-linked into ``_versions/<basename>/<version>/`` and copied to the matching S3
   prefix (sha256 metadata kept), then the new h5ad and the new ``.md`` (version bump, ``sha256``, changelog
   with ``previous_sha256``) are renamed into place, the ``.md`` last.
5. After the swap (a resume marker carries it across a crash): ``__pending_cols`` rows of the merged columns
   are deleted (ant-serve release and restore), the new h5ad and the duckdb are pushed to the catalog with
   their sha256 and verified by HEAD, the ``.md`` is committed (and pushed with ``store_git_push``), ant-serve
   rescans, a history line is written (``<ingest_state_dir>/h5ad_merges.json``). No depositor e-mail.

Input: the store, the deposit store (pending deposits), S3. Output: a new h5ad version in the store and the
catalog, the archived previous version, a cleared ``__pending_cols``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_WINDOW = '01:00-06:00'
WINDOW_RE = re.compile(r'([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)')
JOURNAL_DIR = 'merge_swaps'
HISTORY_FILE = 'h5ad_merges.json'
HISTORY_KEEP = 200
WORK_SUFFIX = '.merge-work'
ANNOTATOR_EVENT = 'h5ad-merge'


class MergeError(RuntimeError):
    """A dataset that cannot be merged now (recorded, the others go on)."""


# ── the window ───────────────────────────────────────────────────────────────

def parse_window(text: str) -> Tuple[int, int]:
    """``HH:MM-HH:MM`` as minutes after midnight; the end may be before the start (past midnight)."""
    match = WINDOW_RE.fullmatch(str(text or '').strip())
    if not match:
        raise ValueError(f'merge_window must be HH:MM-HH:MM (24 h, local time), not {text!r}')
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if start == end:
        raise ValueError('merge_window must not be empty (start equals end)')
    return start, end


def in_window(moment: _dt.datetime, window: str) -> bool:
    start, end = parse_window(window)
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start < end else (minute >= start or minute < end)


# ── h5ad writing (the encodings ant-merge-h5ad writes) ───────────────────────

def h5_cell_names(h5_obs: Any) -> List[str]:
    import h5py  # noqa: PLC0415
    node = h5_obs[str(h5_obs.attrs.get('_index', '_index'))]
    raw = node['values'][:] if isinstance(node, h5py.Group) else node[:]
    return [value.decode() if isinstance(value, (bytes, bytearray)) else str(value) for value in raw]


def h5_column_order(h5_obs: Any) -> List[str]:
    raw = h5_obs.attrs.get('column-order', None)
    if raw is None:
        return []
    return [value.decode() if isinstance(value, (bytes, bytearray)) else str(value) for value in list(raw)]


def pick_int_dtype(n_categories: int) -> Any:
    """The smallest signed integer type for ``n_categories`` codes plus the -1 of a missing value."""
    import numpy as np  # noqa: PLC0415
    if n_categories <= 127:
        return np.dtype('int8')
    if n_categories <= 32767:
        return np.dtype('int16')
    return np.dtype('int32')


def write_numerical(h5_obs: Any, name: str, values: Any) -> None:
    dataset = h5_obs.create_dataset(name, data=values)
    dataset.attrs['encoding-type'] = 'array'
    dataset.attrs['encoding-version'] = '0.2.0'


def write_categorical(h5_obs: Any, name: str, categories: Sequence[str], codes: Any) -> None:
    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    group = h5_obs.create_group(name)
    labels = group.create_dataset('categories', data=np.array(list(categories), dtype=object),
                                  dtype=h5py.string_dtype(encoding='utf-8'))
    labels.attrs['encoding-type'] = 'string-array'          # anndata >= 0.8 reads the members' encodings
    labels.attrs['encoding-version'] = '0.2.0'
    numbers = group.create_dataset('codes', data=codes)
    numbers.attrs['encoding-type'] = 'array'
    numbers.attrs['encoding-version'] = '0.2.0'
    group.attrs['encoding-type'] = 'categorical'
    group.attrs['encoding-version'] = '0.2.0'
    group.attrs['ordered'] = False


def column_values(duckdb_path: Path, column: str, target: str, kind: str, order: Dict[str, int],
                  n_cells: int) -> Tuple[str, Any]:
    """One pending column, read from the duckdb and laid out in the h5ad's cell order:
    ``('num', float64 array)`` or ``('cat', (categories, codes))``."""
    import duckdb  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from anthive_db import safe_ident  # noqa: PLC0415
    if target not in ('obsnum', 'obscat'):
        raise MergeError(f'{column}: unknown target table {target!r}')
    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        rows = connection.execute(f'SELECT c.cell_name, t.{safe_ident(column)} FROM __cells c LEFT JOIN '
                                  f'{safe_ident("__" + target)} t ON c.cell_id = t.cell_id').fetchall()
    finally:
        connection.close()
    if kind == 'num':
        values = np.full(n_cells, np.nan, dtype='f8')
        for name, value in rows:
            if value is not None:
                values[order[name]] = float(value)
        return 'num', values
    categories = sorted({str(value) for _name, value in rows if value is not None})
    index = {value: position for position, value in enumerate(categories)}
    codes = np.full(n_cells, -1, dtype=pick_int_dtype(len(categories)))
    for name, value in rows:
        if value is not None:
            codes[order[name]] = index[str(value)]
    return 'cat', (categories, codes)


def write_pending_into(h5ad_path: Path, duckdb_path: Path, pending: Sequence[Dict[str, Any]]) -> List[str]:
    """Write every pending column into ``h5ad_path`` (a copy), replacing a column of the same name. Returns
    the replaced names."""
    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    replaced: List[str] = []
    with h5py.File(h5ad_path, 'r+') as handle:
        obs = handle['obs']
        names = h5_cell_names(obs)
        order = {name: position for position, name in enumerate(names)}
        column_order = h5_column_order(obs)
        for row in pending:
            column = str(row['col_name'])
            kind, values = column_values(duckdb_path, column, str(row['target_table']), str(row['kind']), order,
                                         len(names))
            if column in obs:
                del obs[column]
                replaced.append(column)
            if kind == 'num':
                write_numerical(obs, column, values)
            else:
                write_categorical(obs, column, values[0], values[1])
            if column not in column_order:
                column_order.append(column)
        obs.attrs['column-order'] = np.array(column_order, dtype=object)
    return replaced


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(db_id: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', db_id)


# ── the merger ───────────────────────────────────────────────────────────────

@dataclass
class MergeResult:
    db_id: str
    state: str                       # merged, skipped, failed, finished (post steps after a crash)
    detail: str = ''
    version: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    sha256: Optional[str] = None
    previous_sha256: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


class H5adMerger:
    """Merges the pending obs columns of the store's datasets into new h5ad versions (module docstring)."""

    def __init__(self, *, store_root: Path, state_dir: Path, s3: Any, bucket: str, catalog_prefix: str,
                 api: Any = None, log: Callable[[str], None] = print, window: str = DEFAULT_WINDOW,
                 max_load: float = 8.0, min_free_gb: float = 40.0, git_commit: bool = True, git_push: bool = False,
                 git_identity: str = 'anthive ingest-watch <noreply@anthive.local>',
                 run: Optional[Callable[..., Any]] = None, stop_requested: Callable[[], bool] = lambda: False,
                 clock: Callable[[], _dt.datetime] = _dt.datetime.now, keep_local_versions: bool = True,
                 busy: Optional[Callable[[], Optional[str]]] = None, s3_endpoint: Optional[str] = None):
        self.store_root = Path(store_root)
        self.state_dir = Path(state_dir)
        self.journal_dir = self.state_dir / JOURNAL_DIR
        self.s3 = s3
        self.bucket = bucket
        self.catalog_prefix = catalog_prefix
        self.api = api
        self.log = log
        #: empty: never in the poll loop (only ``merge-pending --now``); a bad window fails at the start
        self.window = str(window or '').strip() or None
        if self.window:
            parse_window(self.window)
        self.max_load = float(max_load)
        self.min_free_gb = float(min_free_gb)
        self.git_commit = git_commit
        self.git_push = git_push
        self.git_identity = git_identity
        self.run = run or (lambda cmd: subprocess.run([str(part) for part in cmd], capture_output=True, text=True))
        self.stop_requested = stop_requested
        self.clock = clock
        self.keep_local_versions = keep_local_versions
        self.s3_endpoint = s3_endpoint
        if busy is None:
            from host_load import busy_reason  # noqa: PLC0415
            busy = lambda: busy_reason(self.max_load, self.min_free_gb)  # noqa: E731
        self.busy = busy

    def in_window(self) -> bool:
        return bool(self.window) and in_window(self.clock(), self.window)

    # candidates ---------------------------------------------------------------
    def datasets(self) -> List[Tuple[str, Path]]:
        """``(db_id, .md)`` of served datasets in the store (catalog scan rules)."""
        import dataset_deposit  # noqa: PLC0415
        from h5adstore import catalog_dataset_mds  # noqa: PLC0415
        out = []
        for _item, resolved, rel in catalog_dataset_mds(self.store_root):
            if len(rel.parts) != 2:
                continue
            db_id = dataset_deposit.dataset_db_id(self.store_root, rel.parts[0], resolved.stem)
            if db_id:
                out.append((db_id, resolved))
        return sorted(out)

    @staticmethod
    def pending_columns(md: Path) -> List[Dict[str, Any]]:
        from anthive_db import read_pending_cols  # noqa: PLC0415
        duckdb_path = md.with_suffix('.duckdb')
        if not duckdb_path.is_file() or not md.with_suffix('.h5ad').is_file():
            return []
        try:
            frame = read_pending_cols(duckdb_path)
        except Exception:  # noqa: BLE001 - a duckdb being written: not now
            return []
        return frame.to_dict('records')

    def pending(self) -> List[Dict[str, Any]]:
        """Datasets with pending columns: ``{db_id, columns, since}`` (for ``status``)."""
        out = []
        for db_id, md in self.datasets():
            rows = self.pending_columns(md)
            if rows:
                out.append({'db_id': db_id, 'columns': sorted(str(row['col_name']) for row in rows),
                            'since': str(min(str(row['added_at']) for row in rows))})
        return out

    def history(self) -> List[Dict[str, Any]]:
        path = self.state_dir / HISTORY_FILE
        try:
            return list(json.loads(path.read_text()))
        except (OSError, ValueError):
            return []

    def _record(self, result: MergeResult) -> None:
        entries = self.history()
        entries.append({'db_id': result.db_id, 'state': result.state, 'detail': result.detail,
                        'version': result.version, 'columns': result.columns, 'sha256': result.sha256,
                        'previous_sha256': result.previous_sha256, 'warnings': result.warnings,
                        'date': self.clock().isoformat(timespec='seconds')})
        path = self.state_dir / HISTORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix('.json.partial')
        partial.write_text(json.dumps(entries[-HISTORY_KEEP:], indent=1))
        os.replace(partial, path)

    # the run ------------------------------------------------------------------
    def run_pending(self, now: bool = False, only: Optional[str] = None,
                    deposit_pending: Callable[[str], bool] = lambda db_id: False) -> List[MergeResult]:
        """Merge every dataset with pending columns (or ``only``), one at a time, while the gates allow."""
        results = self.recover()
        if not now and not self.in_window():
            return results
        for db_id, md in self.datasets():
            if only is not None and db_id.lower() != only.lower():
                continue
            if self.stop_requested():
                self.log('h5ad merge: stop requested; the other datasets wait for the next window')
                break
            pending = self.pending_columns(md)
            if not pending:
                continue
            if not now and not self.in_window():
                break
            if deposit_pending(db_id):
                self.log(f'h5ad merge: {db_id} waits: a deposit for it is open or being published')
                continue
            reason = self.busy()
            if reason:
                self.log(f'h5ad merge: paused ({reason}); the other datasets wait')
                break
            try:
                result = self.merge_one(db_id, md, pending)
            except MergeError as exc:
                result = MergeResult(db_id, 'failed', str(exc), columns=[str(row['col_name']) for row in pending])
            except Exception as exc:  # noqa: BLE001 - never stop the watcher for one dataset
                result = MergeResult(db_id, 'failed', f'{type(exc).__name__}: {exc}',
                                     columns=[str(row['col_name']) for row in pending])
            self._record(result)
            self.log(f'h5ad merge: {db_id}: {result.state}' + (f' {result.version}' if result.version else '')
                     + (f': {result.detail}' if result.detail else ''))
            results.append(result)
        return results

    # one dataset --------------------------------------------------------------
    def merge_one(self, db_id: str, md: Path, pending: Sequence[Dict[str, Any]]) -> MergeResult:
        import h5py  # noqa: PLC0415
        import ingest_versions  # noqa: PLC0415
        import md_edit  # noqa: PLC0415
        from anthive_db import pending_state_diagnosis  # noqa: PLC0415
        from artefact_deposit import read_md_meta  # noqa: PLC0415
        from h5ad_compare import classify  # noqa: PLC0415
        from host_load import disk_free_bytes  # noqa: PLC0415
        from ingest_md_fill import today_iso  # noqa: PLC0415
        from ingest_pipeline import SwapJournal  # noqa: PLC0415

        h5ad, duckdb_path = md.with_suffix('.h5ad'), md.with_suffix('.duckdb')
        basename, dataset_dir = md.stem, md.parent
        rel_dir = dataset_dir.relative_to(self.store_root).as_posix()
        columns = sorted(str(row['col_name']) for row in pending)
        name = f'merge-{_safe_name(db_id)}'
        if (self.journal_dir / f'{name}.json').exists() or \
                (self.journal_dir / 'resume' / f'{name}.json').exists():
            raise MergeError('an earlier merge of this dataset is not finished (recovered at the next run)')
        with h5py.File(h5ad, 'r') as handle:
            h5_keys = set(handle['obs'].keys())
            h5_names = h5_cell_names(handle['obs'])
        diagnosis = pending_state_diagnosis(duckdb_path, h5_keys)
        if diagnosis['unrecorded'] or diagnosis['missing_from_duckdb']:
            raise MergeError(f'divergent pending state (columns not recorded: {diagnosis["unrecorded"][:5]}; '
                             f'recorded but missing: {diagnosis["missing_from_duckdb"][:5]}): an admin looks with '
                             f'ant-merge-h5ad --dry-run')
        import duckdb  # noqa: PLC0415
        connection = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            duck_names = [row[0] for row in connection.execute('SELECT cell_name FROM __cells').fetchall()]
        finally:
            connection.close()
        if len(duck_names) != len(h5_names) or set(duck_names) != set(h5_names):
            raise MergeError('the duckdb and the h5ad hold different cells (an ant-convert rebuild is due)')
        meta = read_md_meta(md)
        sha_before = sha256_file(h5ad)
        if meta.get('sha256') and str(meta['sha256']) != sha_before:
            raise MergeError(f'the store h5ad (sha256 {sha_before[:12]}) is not the published one '
                             f'({str(meta["sha256"])[:12]}); nothing merged')
        size = h5ad.stat().st_size
        if disk_free_bytes(dataset_dir) < size * 1.1 + 1024 ** 3:
            raise MergeError(f'not enough free disk next to the h5ad for a copy of {size / 1024 ** 3:.1f} GB')
        work = dataset_dir / f'.{h5ad.name}{WORK_SUFFIX}'
        work.unlink(missing_ok=True)
        new_md = dataset_dir / f'.{md.name}{WORK_SUFFIX}'
        try:
            shutil.copyfile(h5ad, work)
            replaced = write_pending_into(work, duckdb_path, pending)
            classification = classify(h5ad, work)
            if classification.part == 'major' and not replaced:
                raise MergeError(f'the merged copy is not additive ({classification.reason}); nothing merged')
            sha_after = sha256_file(work)
            old_version = str(meta.get('version') or '')
            today = today_iso()
            verb = 'merged obs columns ' + ', '.join(columns)
            class_line = (f'Version class {classification.part}: '
                          + ('additive: the h5ad catches up with the served obs columns'
                             if classification.part == 'minor'
                             else f'data altered: replaced obs values of {", ".join(replaced)}') + '.')
            lines = [class_line, f'h5ad: {verb} (no data change for the served dataset; they were served from the '
                                 f'duckdb since their deposit or annotation).']
            text = md.read_text(encoding='utf-8')
            edited = md_edit.apply_edits(text, [md_edit.set_top('sha256', sha_after),
                                                md_edit.bump_version(lines, today, classification.part,
                                                                     previous_sha256=sha_before)])
            new_md.write_text(edited, encoding='utf-8')
            new_version = str(read_md_meta(new_md).get('version') or '')
            journal = SwapJournal(self.journal_dir, name, dataset_dir, meta={
                'post_pending': True, 'kind': 'h5ad_merge', 'db_id': db_id, 'md': str(md), 'columns': columns,
                'sha_before': sha_before, 'sha_after': sha_after, 'old_version': old_version,
                'new_version': new_version, 'rel_dir': rel_dir, 'basename': basename,
                'part': classification.part})
            links, _remove = ingest_versions.archive_plan(dataset_dir, basename, old_version, data_changed=False)
            for source, destination in links:
                journal.link(source, destination)
            try:
                ingest_versions.archive_s3(self.s3, self.bucket, self.catalog_prefix, rel_dir, basename, old_version,
                                           [destination for _source, destination in links])
            except Exception as exc:  # noqa: BLE001 - the S3 archive comes first: nothing swapped
                journal.rollback()
                raise MergeError(f'the previous version could not be archived on S3 ({exc}); nothing swapped')
            journal.stage(work, h5ad)
            journal.stage(new_md, md)
            journal.commit()                           # renames; the resume marker carries the post steps
        finally:
            work.unlink(missing_ok=True)
            new_md.unlink(missing_ok=True)
        result = self.finish(json.loads((self.journal_dir / 'resume' / f'{name}.json').read_text()))
        result.state = 'merged'
        return result

    # after the swap -----------------------------------------------------------
    def finish(self, meta: Dict[str, Any]) -> MergeResult:
        """The post steps of a swapped merge (idempotent): clear the pending rows, push and verify the h5ad
        and the duckdb, commit the ``.md``, rescan, drop the resume marker."""
        import ingest_versions  # noqa: PLC0415
        md = Path(meta['md'])
        db_id = meta['db_id']
        result = MergeResult(db_id, 'finished', version=meta.get('new_version'), columns=list(meta['columns']),
                             sha256=meta.get('sha_after'), previous_sha256=meta.get('sha_before'))
        self._clear_pending(md.with_suffix('.duckdb'), db_id, result.columns)
        h5ad_ok = self._push(md.with_suffix('.h5ad'), meta['sha_after'], result.warnings)
        self._push(md.with_suffix('.duckdb'), None, result.warnings)
        if not h5ad_ok:
            raise MergeError('the new h5ad could not be pushed to the catalog and verified; retried at the next run '
                             '(the store already holds the new version)')
        commit_paths = [md]
        if self._restamp_sidecar(md, result.warnings):
            commit_paths.append(md.with_suffix('.h5ad.mus'))
        self._git(commit_paths, f'{db_id}: h5ad merged obs columns {", ".join(result.columns)} ({meta.get("part")}, '
                      f'{meta.get("new_version")})\n\nThe watcher merged pending obs columns into the h5ad; the '
                      f'previous h5ad (sha256 {meta["sha_before"]}) is archived as {meta.get("old_version")}.\n',
                  result.warnings)
        if self.api is not None:
            try:
                self.api.post_with_retry('/admin/rescan')
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f'rescan failed ({exc})')
        if not self.keep_local_versions:
            removed, why = ingest_versions.drop_local_if_verified(md.parent, meta['basename'], meta['old_version'],
                                                                  self.s3, self.bucket, self.catalog_prefix,
                                                                  meta['rel_dir'])
            if not removed:
                result.warnings.append(f'local copy of {meta["old_version"]} kept: {why}')
        (self.journal_dir / 'resume' / f'merge-{_safe_name(db_id)}.json').unlink(missing_ok=True)
        result.detail = f'{len(result.columns)} column(s) merged; previous h5ad archived as {meta.get("old_version")}'
        return result

    def recover(self) -> List[MergeResult]:
        """Interrupted merges: a journal before its commit is rolled back (the merge runs again later), one in
        its commit is rolled forward; swapped merges get their post steps."""
        from ingest_pipeline import recover_swaps  # noqa: PLC0415
        results: List[MergeResult] = []
        for message in recover_swaps(self.journal_dir, self.log):
            self.log(f'h5ad merge: {message}')
        for leftover in self.store_root.glob(f'*/.*{WORK_SUFFIX}'):
            leftover.unlink(missing_ok=True)       # a copy whose merge died before its journal
        resume = self.journal_dir / 'resume'
        for path in sorted(resume.glob('merge-*.json')) if resume.is_dir() else []:
            try:
                result = self.finish(json.loads(path.read_text()))
            except Exception as exc:  # noqa: BLE001
                result = MergeResult(path.stem[len('merge-'):], 'failed', f'post steps not finished: {exc}')
            self._record(result)
            results.append(result)
        return results

    def _clear_pending(self, duckdb_path: Path, db_id: str, columns: Sequence[str]) -> None:
        import duckdb  # noqa: PLC0415
        from ingest_pipeline import TransientError, writer_lock  # noqa: PLC0415
        if not columns:
            return
        released = False
        with writer_lock(duckdb_path, timeout=600):
            try:
                connection = duckdb.connect(str(duckdb_path), read_only=False)
            except duckdb.IOException as exc:
                if 'Conflicting lock' not in str(exc) or self.api is None:
                    raise TransientError(f'the duckdb of {db_id} is locked ({exc})') from exc
                self.api.post_with_retry(f'/admin/release/{db_id}')
                released = True
                connection = duckdb.connect(str(duckdb_path), read_only=False)
            try:
                placeholders = ', '.join('?' * len(columns))
                connection.execute(f'DELETE FROM __pending_cols WHERE col_name IN ({placeholders})', list(columns))
            finally:
                connection.close()
                if released:
                    try:
                        self.api.post_with_retry(f'/admin/restore/{db_id}')
                    except Exception as exc:  # noqa: BLE001 - it reopens on the next rescan or start
                        self.log(f'h5ad merge: restore of {db_id} failed ({exc})')

    def _restamp_sidecar(self, md: Path, warnings: List[str]) -> bool:
        """The sidecar's ``s3_*`` keys name the new catalog object (as the dataset route re-stamps them after a
        push); the sidecar is pushed too. The archived version keeps its own sidecar (a hard link to the old
        file: the rewrite is a new file)."""
        from sidecar_merge import restamp_s3_block, write_sidecar_atomic  # noqa: PLC0415
        sidecar = md.with_suffix('.h5ad.mus')
        if not sidecar.is_file():
            return False
        key = f'{self.catalog_prefix}{md.parent.relative_to(self.store_root).as_posix()}/{md.stem}.h5ad'
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
            url = f'https://{self.s3_endpoint}/{self.bucket}/{key}' if self.s3_endpoint else f's3://{self.bucket}/{key}'
            write_sidecar_atomic(sidecar, restamp_s3_block(
                sidecar.read_text(encoding='utf-8'), bucket=self.bucket, key=key, url=url,
                etag=str(head.get('ETag', '')).strip('"'),
                uploaded_at=_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'the sidecar s3_* keys were not re-stamped ({exc})')
            return False
        self._push(sidecar, None, warnings)
        return True

    def _push(self, path: Path, sha: Optional[str], warnings: List[str]) -> bool:
        """Upload one store file to the catalog with its sha256; verified by HEAD (size and sha256)."""
        from ingest_pipeline import _transfer_config, writer_lock  # noqa: PLC0415
        key = f'{self.catalog_prefix}{path.parent.relative_to(self.store_root).as_posix()}/{path.name}'
        try:
            if path.suffix == '.duckdb':
                with writer_lock(path, timeout=600):
                    sha = sha256_file(path)
                    size = path.stat().st_size
                    self._upload(path, key, sha, _transfer_config())
            else:
                size = path.stat().st_size
                self._upload(path, key, sha or sha256_file(path), _transfer_config())
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'push of {path.name} failed ({exc})')
            return False
        if int(head.get('ContentLength', -1)) != size or (head.get('Metadata') or {}).get('sha256') != sha:
            warnings.append(f'the catalog copy of {path.name} could not be verified')
            return False
        return True

    def _upload(self, path: Path, key: str, sha: str, transfer: Any) -> None:
        kwargs: Dict[str, Any] = {'ExtraArgs': {'Metadata': {'sha256': sha,
                                                             'original-mtime': str(int(path.stat().st_mtime))}}}
        if transfer is not None:
            kwargs['Config'] = transfer
        self.s3.upload_file(str(path), self.bucket, key, **kwargs)

    def _git(self, paths: Sequence[Path], message: str, warnings: List[str]) -> None:
        if not self.git_commit:
            return
        if not (self.store_root / '.git').exists():
            warnings.append('the store is not a git repository: not committed')
            return
        rel = [str(PurePosixPath(path.relative_to(self.store_root))) for path in paths]
        staged = self.run(['git', '-C', self.store_root, 'diff', '--cached', '--name-only', '--', *rel])
        if staged.returncode != 0 or staged.stdout.strip():
            warnings.append('not committed: staged changes of someone else on the .md or the sidecar')
            return
        name, _, email = self.git_identity.partition('<')
        committer = ['-c', f'user.name={name.strip()}', '-c', f'user.email={email.rstrip(">").strip()}']
        add = self.run(['git', '-C', self.store_root, 'add', '--', *rel])
        result = self.run(['git', '-C', self.store_root, *committer, 'commit', '--only', '-m', message, '--', *rel]) \
            if add.returncode == 0 else add
        if result.returncode != 0:
            output = ((result.stdout or '') + (result.stderr or '')).strip()
            if 'nothing to commit' not in output and 'no changes added' not in output:
                warnings.append(f'git commit failed: {output[:300]}')
            self.run(['git', '-C', self.store_root, 'reset', '-q', '--', *rel])
            return
        if self.git_push:
            ahead = self.run(['git', '-C', self.store_root, 'rev-list', '--count', '@{u}..HEAD'])
            if ahead.returncode != 0 or (ahead.stdout or '').strip().splitlines()[-1:] != ['1']:
                warnings.append('store_git_push: not pushed, the store branch has other unpushed commits or no '
                                'upstream')
                return
            pushed = self.run(['git', '-C', self.store_root, 'push'])
            if pushed.returncode != 0:
                warnings.append(f'git push failed: {((pushed.stdout or "") + (pushed.stderr or "")).strip()[:300]}')


#: watcher rows in these states no longer hold a dataset (anything else, held included, waits for a person or
#: the next poll and may still change the dataset)
SETTLED_STATES = ('live', 'rejected', 'failed')


def deposit_pending_check(state_dir: Path, db: Any = None) -> Callable[[str], bool]:
    """``db_id -> True`` while a deposit for that dataset is open or waiting (the API deposit store), or a
    watcher item for it (``db``: the watcher's StateDB; obs-columns items are ``obs:<db_id>``) is not settled."""
    def check(db_id: str) -> bool:
        import dataset_deposit  # noqa: PLC0415
        if db is not None:
            names = {db_id.lower(), f'obs:{db_id}'.lower()}
            for row in db.items():
                if str(row['db_id'] or '').lower() in names and row['state'] not in SETTLED_STATES:
                    return True
        try:
            store = dataset_deposit.DepositStore.open_if_exists(state_dir)
        except Exception:  # noqa: BLE001 - unreadable: be careful, wait
            return True
        if store is None:
            return False
        try:
            rows = store._rows(
                "SELECT id FROM deposits WHERE UPPER(COALESCE(update_of, '')) = UPPER(?) AND (state IN "
                "('uploading', 'completing') OR (state = 'uploaded' AND COALESCE(watch_state, '') NOT IN "
                "('live', 'rejected', 'failed')))", (db_id,))
            return bool(rows)
        finally:
            store.close()
    return check


def time_label() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')
