"""Shared, lazily built inputs of the checks.

``CheckContext`` knows the submission folder, the store and the settings,
and builds the expensive things (sha256, backed h5ad, scratch ``.duckdb``,
published views, crosswalk, overlap index) at most once and only when a
check asks. It never writes into the submission folder or the store; its
only writes go to the scratch dir (``scratch.py``).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .overlap_index import OverlapIndex
from .scratch import ConvertError, scratch_convert, sha256_cached

MODE_NEW = 'new'
MODE_EXISTING = 'existing'


@dataclass
class Settings:
    """Site settings the checks read (``config.get_dataset_ingest_settings``)."""

    max_upload_gb: float = 200.0
    auto_publish_public: bool = False
    default_access: str = 'internal'


class CheckContext:
    """Everything a check may look at.

    Args:
        group_dir: Folder with ``<basename>.h5ad``, ``.md``, ``.h5ad.mus`` and
            (new group) ``index.md``. For a dataset already in the store this
            is its group folder.
        basename: Dataset file stem.
        store_root: The h5adstore (existing datasets, crosswalk, scripts).
        scratch_root: Scratch dir (``config.get_ingest_scratch_dir()``).
        index_dir: Overlap index dir (``config.get_ingest_index_dir()``).
        settings: Site settings.
        declared_sha256: sha256 the submitter declared (``files.sha256``).
        duckdb_path: Use this ``.duckdb`` instead of a scratch convert.
        convert: Allow a scratch convert when no ``.duckdb`` exists.
        mode: ``new``, ``existing`` or ``auto`` (existing when the folder is in the store).
        convert_script: ``bin/ingest/ant-convert``.
    """

    def __init__(self, group_dir: Path, basename: str, store_root: Path, scratch_root: Path, index_dir: Path,
                 settings: Optional[Settings] = None, declared_sha256: Optional[str] = None,
                 duckdb_path: Optional[Path] = None, convert: bool = True, mode: str = 'auto',
                 convert_script: Optional[Path] = None) -> None:
        self.group_dir = Path(group_dir).resolve()
        self.basename = basename
        self.store_root = Path(store_root).resolve()
        self.scratch_root = Path(scratch_root)
        self.index_dir = Path(index_dir)
        self.settings = settings or Settings()
        self.declared_sha256 = declared_sha256
        self.explicit_duckdb = Path(duckdb_path) if duckdb_path else None
        self.convert_allowed = convert
        self.convert_script = convert_script or Path(__file__).resolve().parents[2] / 'bin' / 'ingest' / 'ant-convert'
        self.store_group_dir = self.store_root / self.group_dir.name
        in_store = self.group_dir == self.store_group_dir.resolve() if self.store_group_dir.exists() else False
        self.mode = (MODE_EXISTING if in_store else MODE_NEW) if mode == 'auto' else mode
        self.in_store = in_store
        self.convert_error: Optional[str] = None
        self._cache: Dict[str, Any] = {}
        self._closers: List[Any] = []
        self.sensitive_tokens_seen: Set[str] = set()

    # ── paths ────────────────────────────────────────────────────────────────

    @property
    def h5ad_path(self) -> Optional[Path]:
        """The submitted h5ad, or None when absent."""
        path = self.group_dir / f'{self.basename}.h5ad'
        return path if path.is_file() else None

    @property
    def md_path(self) -> Path:
        return self.group_dir / f'{self.basename}.md'

    @property
    def mus_path(self) -> Path:
        return self.group_dir / f'{self.basename}.h5ad.mus'

    @property
    def index_md_path(self) -> Path:
        return self.group_dir / 'index.md'

    @property
    def is_existing_group(self) -> bool:
        """True when the group folder already exists in the store."""
        return self.store_group_dir.is_dir()

    # ── metadata ─────────────────────────────────────────────────────────────

    def _parse(self, path: Path) -> Dict[str, Any]:
        from metadata import parse_md_file  # noqa: PLC0415

        if not path.is_file():
            return {'metadata': None, 'error': f'{path.name} not found'}
        try:
            metadata, body = parse_md_file(path)
            return {'metadata': metadata, 'body': body, 'error': None}
        except Exception as exc:  # noqa: BLE001 - any parse failure is a finding
            return {'metadata': None, 'error': f'{type(exc).__name__}: {exc}'}

    @property
    def md_parse(self) -> Dict[str, Any]:
        """``{'metadata', 'body', 'error'}`` of the dataset ``.md``."""
        if 'md' not in self._cache:
            self._cache['md'] = self._parse(self.md_path)
        return self._cache['md']

    @property
    def md(self) -> Dict[str, Any]:
        """Parsed ``.md`` front matter (empty when missing or unreadable)."""
        return dict(self.md_parse['metadata'] or {})

    @property
    def api(self) -> Dict[str, Any]:
        """The validated ``api:`` section."""
        from metadata import get_api_config  # noqa: PLC0415

        if 'api' not in self._cache:
            try:
                self._cache['api'] = get_api_config(self.md)
            except Exception:  # noqa: BLE001
                self._cache['api'] = dict(self.md.get('api') or {})
        return self._cache['api']

    @property
    def index_md(self) -> Dict[str, Any]:
        """``index.md`` of the submission, else the store group's, else empty."""
        if 'index_md' not in self._cache:
            parsed = self._parse(self.index_md_path)
            if parsed['metadata'] is None and self.is_existing_group:
                parsed = self._parse(self.store_group_dir / 'index.md')
            self._cache['index_md'] = dict(parsed['metadata'] or {})
        return self._cache['index_md']

    @property
    def submitted_index_md(self) -> Dict[str, Any]:
        """``index.md`` in the submission folder only (empty when absent)."""
        return dict(self._parse(self.index_md_path)['metadata'] or {})

    @property
    def group_id(self) -> Optional[str]:
        value = self.md.get('group_id') or self.index_md.get('group_id')
        return str(value) if value else None

    @property
    def file_id(self) -> Optional[str]:
        value = self.md.get('id')
        return str(value) if value else None

    @property
    def db_id(self) -> Optional[str]:
        return f'{self.group_id}/{self.file_id}' if self.group_id and self.file_id else None

    @property
    def annotation(self) -> Dict[str, Any]:
        """Effective ``.h5ad.mus`` annotation: store group cascade, submission cascade, sidecar."""
        import mus  # noqa: PLC0415

        if 'annotation' not in self._cache:
            layers = []
            if self.is_existing_group and not self.in_store:
                layers.append(mus.read_cascade(self.store_group_dir, root=self.store_root))
            root = self.store_root if self.in_store else self.group_dir
            layers.append(mus.read_cascade(self.group_dir, root=root))
            merged = mus.merge_cascade(layers)
            try:
                sidecar = mus.parse_mus_file(self.mus_path)
            except Exception:  # noqa: BLE001 - reported by mus.vocab
                sidecar = {}
            merged.update(sidecar)
            self._cache['annotation'] = merged
        return self._cache['annotation']

    @property
    def organism(self) -> str:
        value = self.annotation.get('organism') or ''
        if isinstance(value, list):
            value = value[0] if value else ''
        return str(value).strip().lower()

    # ── heavy inputs ─────────────────────────────────────────────────────────

    @property
    def sha256(self) -> Optional[str]:
        """SHA-256 of the h5ad (cached by size and mtime in the scratch dir)."""
        if self.h5ad_path is None:
            return None
        if 'sha256' not in self._cache:
            self.scratch_root.mkdir(parents=True, exist_ok=True)
            self._cache['sha256'] = sha256_cached(self.h5ad_path, self.scratch_root / 'sha256_cache.json')
        return self._cache['sha256']

    @property
    def adata(self) -> Any:
        """The h5ad opened backed and read-only (obs and var in memory)."""
        if 'adata' not in self._cache:
            import anndata  # noqa: PLC0415

            handle = anndata.read_h5ad(self.h5ad_path, backed='r')
            self._cache['adata'] = handle
            self._closers.append(lambda: handle.file.close())
        return self._cache['adata']

    def sample_matrix(self, name: str, n_sample: int, seed: int, rows: Optional[Any] = None) -> Any:
        """``(row indices, dense sample)`` of a matrix by display name (cached)."""
        from . import sampling  # noqa: PLC0415

        key = f'sample:{name}:{n_sample}:{seed}'
        if key not in self._cache:
            hdf5_path = sampling.matrix_paths(self.h5ad_path)[name]
            n_obs, _ = sampling.matrix_shape(self.h5ad_path, hdf5_path)
            indices = sampling.sample_indices(n_obs, n_sample, seed) if rows is None else rows
            self._cache[key] = (indices, sampling.read_rows(self.h5ad_path, hdf5_path, indices))
        return self._cache[key]

    def existing_duckdb_path(self) -> Optional[Path]:
        """An existing ``.duckdb`` that is usable without a convert, else None.

        The explicit ``--duckdb``, or the sibling of the h5ad when it is at
        least as new as the h5ad.
        """
        if self.explicit_duckdb is not None:
            return self.explicit_duckdb if self.explicit_duckdb.is_file() else None
        sibling = self.group_dir / f'{self.basename}.duckdb'
        if sibling.is_file() and self.h5ad_path is not None \
                and sibling.stat().st_mtime >= self.h5ad_path.stat().st_mtime:
            return sibling
        return None

    @property
    def duckdb(self) -> Optional[Path]:
        """The ``.duckdb`` to read: existing, else a cached scratch convert, else None."""
        if 'duckdb' in self._cache:
            return self._cache['duckdb']
        path = self.existing_duckdb_path()
        if path is None and self.convert_allowed and self.h5ad_path is not None and self.md_path.is_file():
            try:
                path = scratch_convert(
                    self.h5ad_path, self.md_path, self.index_md_path if self.index_md_path.is_file() else (
                        self.store_group_dir / 'index.md' if self.is_existing_group else None),
                    self.mus_path if self.mus_path.is_file() else None, self.md, self.sha256 or '',
                    self.scratch_root, self.convert_script, self.group_dir.name)
            except ConvertError as exc:
                self.convert_error = str(exc)
                path = None
        self._cache['duckdb'] = path
        return path

    @property
    def view(self) -> Any:
        """``DatasetView`` of the ``.duckdb`` with the submitted ``api:`` section, or None."""
        from .dataset_view import DatasetView  # noqa: PLC0415

        if 'view' not in self._cache:
            path = self.duckdb
            view = None
            if path is not None:
                view = DatasetView(path, self.api, self.group_id, db_label=self.db_id or self.basename,
                                   store_root=self.store_root, temp_dir=self.scratch_root / 'spill')
                self._closers.append(view.close)
            self._cache['view'] = view
        return self._cache['view']

    @property
    def index(self) -> OverlapIndex:
        """The overlap index (may be empty)."""
        if 'index' not in self._cache:
            self._cache['index'] = OverlapIndex(self.index_dir)
        return self._cache['index']

    def new_digests(self) -> Any:
        """Donor, cell and gene digests of the submission (needs the ``.duckdb``)."""
        from .overlap_index import digests_from_view  # noqa: PLC0415

        if 'digests' not in self._cache:
            view = self.view
            self._cache['digests'] = None if view is None else digests_from_view(
                view, self.db_id or f'new/{self.basename}', self.group_id)
        return self._cache['digests']

    # ── crosswalk (controlled access) ────────────────────────────────────────

    @property
    def crosswalk_path(self) -> Path:
        return self.store_root / 'donor_crosswalk.tsv'

    def crosswalk_for(self, spec: Dict[str, Any]) -> Any:
        """``(Crosswalk, problem)`` for a join spec. Contents never leave the checks."""
        from obs_derive_forms import crosswalk_for  # noqa: PLC0415

        return crosswalk_for(spec, self.store_root)

    def controlled_cohorts(self) -> Set[str]:
        """Cohorts the crosswalk flags in its optional ``controlled`` column (names only)."""
        if 'controlled_cohorts' not in self._cache:
            cohorts: Set[str] = set()
            if self.crosswalk_path.is_file():
                with open(self.crosswalk_path, newline='', encoding='utf-8') as handle:
                    reader = csv.DictReader(handle, delimiter='\t')
                    if 'controlled' in (reader.fieldnames or []):
                        for row in reader:
                            if str(row.get('controlled') or '').strip().lower() in {'1', 'true', 'yes', 'y', 'controlled'}:
                                cohorts.add(str(row.get('cohort') or '').strip().lower())
            self._cache['controlled_cohorts'] = cohorts
        return self._cache['controlled_cohorts']

    def sensitive_tokens(self) -> Set[str]:
        """Aliases and canonical ids of the crosswalk, for the report scrub only."""
        if 'sensitive' not in self._cache:
            tokens: Set[str] = set()
            if self.crosswalk_path.is_file():
                with open(self.crosswalk_path, newline='', encoding='utf-8') as handle:
                    for row in csv.DictReader(handle, delimiter='\t'):
                        for column in ('alias', 'canonical_uid'):
                            value = str(row.get(column) or '').strip()
                            if len(value) >= 4:
                                tokens.add(value)
            self._cache['sensitive'] = tokens
        return self._cache['sensitive']

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the h5ad and DuckDB handles."""
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001, S110 - closing must not mask a result
                pass
        self._closers.clear()
        self._cache.pop('view', None)
        self._cache.pop('adata', None)

    def __enter__(self) -> 'CheckContext':
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
