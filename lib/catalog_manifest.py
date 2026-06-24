"""Catalog manifest builder.

Writes ``s3://<bucket>/_catalog/manifest.json`` on every ingest / build-all
run so that mus (and any other tooling) can validate sidecar fields
against the canonical list of data_projects / file_ids / basenames /
titles before a deposit reaches anthive's ingest gate.

The manifest reflects the LOCAL catalog state at time of write — what's
in ``data/h5adstore/``. Anthive's ingest tools always produce a fresh
manifest; there's no merge-with-S3 step, so the file is authoritative
per-run.

## Schema (version 1)

```json
{
  "version": 1,
  "updated_at": "2026-06-24T18:55:00Z",
  "data_projects": {
    "Sun2026": {
      "group_id": "S26",
      "datasets": {
        "AQR": {
          "basename": "adata_qc_removed_clusters.h5ad",
          "title":    "adata_qc_removed_clusters",
          "inactive": false
        }
      }
    }
  }
}
```

## What's included / excluded

- ``index.md`` and ``README.md`` are NOT datasets (group-level docs).
- Folders prefixed with ``_`` or ``.`` are skipped (staging / archive).
- ``.h5ad`` and ``.duckdb`` need NOT exist locally for an entry to land —
  the ``.md`` is the source of truth; presence-of-data is the ingester's
  problem at the next step.
- **Inactive datasets ARE included** with ``inactive: true``. This is the
  whole point: mus surfaces a warning when the user references an inactive
  dataset rather than silently failing the lookup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from md_check import generate_file_id, generate_group_id
from metadata import parse_md_file

MANIFEST_VERSION = 1
DEFAULT_S3_KEY = "_catalog/manifest.json"


def _iso_z(dt: datetime) -> str:
    """ISO 8601 with seconds precision and Zulu suffix (no '+00:00')."""
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _read_group_id(item: Path, data_folder: Path, data_project: str) -> str:
    """Resolve group_id for an entry's data_project.

    Looks at the sibling ``index.md`` first; falls back to the
    data_project-level ``index.md``; finally falls back to the same
    generator ant-serve uses (``generate_group_id``) so the manifest's
    key matches whatever the live catalog assigns.
    """
    # sibling
    sibling_index = item.parent / 'index.md'
    if sibling_index.exists():
        try:
            meta, _ = parse_md_file(sibling_index)
            gid = str(meta.get('group_id') or '').strip()
            if gid:
                return gid
        except Exception:
            pass
    # top-level index
    top_index = data_folder / data_project / 'index.md'
    if top_index.exists() and top_index != sibling_index:
        try:
            meta, _ = parse_md_file(top_index)
            gid = str(meta.get('group_id') or '').strip()
            if gid:
                return gid
        except Exception:
            pass
    return generate_group_id(data_project)


def build_manifest(data_folder: Path) -> Dict[str, Any]:
    """Walk ``data_folder`` and produce the manifest dict.

    Mirrors ant-serve's ``_scan_databases`` discovery rules:
      - ``.md`` files (excluding ``index.md`` / ``README.md``) become entries
      - folders starting with ``_`` or ``.`` are skipped
      - file_id comes from frontmatter ``id:`` or falls back to
        ``generate_file_id`` (same as ant-serve)
      - group_id comes from the data_project-level ``index.md``
      - presence of a ``<basename>.inactive`` sidecar marks the dataset
        ``inactive: true`` (NOT excluded — mus needs to surface the warning)
    """
    data_folder = data_folder.resolve()
    data_projects: Dict[str, Dict[str, Any]] = {}

    def walk(d: Path) -> None:
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, OSError):
            return

        for item in entries:
            if item.is_dir():
                name = item.name
                if name.startswith('_') or name.startswith('.'):
                    continue
                walk(item)
                continue

            if not item.is_file():
                continue
            if item.suffix != '.md':
                continue
            if item.name in ('index.md', 'README.md'):
                continue

            try:
                rel = item.relative_to(data_folder)
            except ValueError:
                continue
            if len(rel.parts) < 2:
                # bare .md at the top of data_folder isn't a dataset.
                continue
            data_project = rel.parts[0]

            try:
                meta, _ = parse_md_file(item)
            except Exception:
                continue

            file_id = str(meta.get('id') or '').strip() or generate_file_id(rel.stem)
            group_id = _read_group_id(item, data_folder, data_project)
            basename = item.with_suffix('.h5ad').name
            title = str(meta.get('title') or '').strip() or item.stem
            inactive = item.with_suffix('.inactive').exists()

            project_entry = data_projects.setdefault(
                data_project,
                {'group_id': group_id, 'datasets': {}},
            )
            # If a later entry resolves a non-empty group_id and the
            # current one is empty, upgrade it. (Shouldn't happen in
            # practice since data_project-level index.md is consulted
            # uniformly, but defensive.)
            if not project_entry.get('group_id') and group_id:
                project_entry['group_id'] = group_id

            if file_id in project_entry['datasets']:
                # ID collision within a data_project — append the stem to
                # disambiguate, matching ant-serve's path-fallback behaviour.
                file_id = str(rel.with_suffix(''))

            project_entry['datasets'][file_id] = {
                'basename': basename,
                'title': title,
                'inactive': inactive,
            }

    walk(data_folder)
    return {
        'version': MANIFEST_VERSION,
        'updated_at': _iso_z(datetime.now(timezone.utc)),
        'data_projects': data_projects,
    }


def write_manifest_to_s3(
    manifest: Dict[str, Any],
    s3_client,
    bucket: str,
    key: str = DEFAULT_S3_KEY,
) -> None:
    """Upload the manifest JSON to ``s3://<bucket>/<key>`` atomically.

    Uses ``put_object`` so the write is atomic at the S3 layer: clients
    won't ever see half a JSON document during the upload. ``CacheControl``
    is set so a CDN / browser-cache layer doesn't re-serve a stale copy
    (mus does its own TTL via the cache file under XDG_CACHE_HOME).
    """
    body = json.dumps(manifest, indent=2, sort_keys=True).encode('utf-8')
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType='application/json',
        CacheControl='no-cache',
    )


def write_manifest(
    data_folder: Path,
    s3_client,
    bucket: str,
    key: str = DEFAULT_S3_KEY,
) -> Dict[str, Any]:
    """Convenience: build + upload in one call. Returns the manifest dict."""
    manifest = build_manifest(data_folder)
    write_manifest_to_s3(manifest, s3_client, bucket, key=key)
    return manifest
