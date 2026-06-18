"""
sidecar_merge — merge logic for .h5ad.mus / mus.cfg KEY=VALUE files.

Used by `bin/ingest/ant-ingest` when promoting a freshly-deposited sidecar
from S3 `__incoming/` alongside an existing local sidecar. The merge is
pure (no I/O) and reports conflicts explicitly so the caller can decide
whether to abort or warn-and-continue.

Conflict rules
--------------
* Same key, same value          → silent.
* Incoming-only key             → adopt silently (`adopted`).
* Same key, different value, and
    - key is in MACHINE_KEYS    → adopt silently (`overwritten`); these
                                   are deposit-state fields (s3_*, irods_*,
                                   mtime, hashed, …) that mus owns.
    - otherwise                 → **CONFLICT** — keep local value, emit
                                   a `Conflict` record.
* Local-only key                → keep.

Comments and blank lines from the LOCAL file are preserved verbatim;
incoming comments are discarded (mus regenerates sidecars from scratch
on each upload, so its comments are not authoritative).

The companion `restamp_s3_block` is used by ant-ingest after pushing the
local h5ad to its catalog location — it rewrites the `s3_*` keys so the
local sidecar reflects the catalog copy, not the (about-to-be-deleted)
__incoming/ copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

# Keys mus / anthive treat as machine-managed deposit state. On a
# value-conflict these get overwritten silently (incoming wins).
S3_KEYS = frozenset({
    's3_bucket', 's3_key', 's3_url', 's3_etag', 's3_uploaded_at',
})
IRODS_KEYS = frozenset({
    'irods_path', 'irods_url', 'irods_purl', 'irods_status',
    'irods_uploaded_at', 'irods_message',
})
INFO_KEYS = frozenset({
    'abspath', 'mtime', 'hashed', 'updated', 'created',
    'hashed_at', 'size',
    'h5ad_inspected_at', 'h5ad_inspected_sha256',
    'h5ad_inspected_vocab_sha256', 'h5ad_inspected_check_version',
})
MACHINE_KEYS = S3_KEYS | IRODS_KEYS | INFO_KEYS


@dataclass(frozen=True)
class Conflict:
    """A key whose value differs between local and incoming and is NOT
    in MACHINE_KEYS. Caller decides whether to abort the ingest."""
    key: str
    local_value: str
    incoming_value: str


@dataclass
class MergeResult:
    merged_text: str
    conflicts: List[Conflict] = field(default_factory=list)
    adopted: List[str] = field(default_factory=list)
    overwritten: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────

# Row kinds preserved during merge. 'pair' lines hold a KEY=VALUE; others
# are kept verbatim so comments survive.
_ROW_PAIR = 'pair'
_ROW_COMMENT = 'comment'
_ROW_BLANK = 'blank'


def parse_lines(text: str) -> List[Tuple[str, str, str]]:
    """Tokenise a sidecar into rows of (kind, key, raw_value).

    For `pair` rows, `key` is stripped and `raw_value` is the right-hand
    side AS-IS (no whitespace stripping — the merge compares stripped
    values but writes them back verbatim to minimise churn).

    For `comment` / `blank` rows, key is empty and raw_value holds the
    original line.
    """
    rows: List[Tuple[str, str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            rows.append((_ROW_BLANK, '', line))
        elif stripped.startswith('#'):
            rows.append((_ROW_COMMENT, '', line))
        elif '=' in line:
            key, _, value = line.partition('=')
            rows.append((_ROW_PAIR, key.strip(), value))
        else:
            # Malformed (no '=' and not a comment) — preserve verbatim.
            rows.append((_ROW_COMMENT, '', line))
    return rows


def _index_pairs(rows: List[Tuple[str, str, str]]) -> Dict[str, str]:
    """Return {key: raw_value} for pair rows. On duplicate keys (which
    a sidecar shouldn't have), the LAST occurrence wins, matching how
    `lib.mus.parse_mus_file` reads them."""
    out: Dict[str, str] = {}
    for kind, key, value in rows:
        if kind == _ROW_PAIR:
            out[key] = value
    return out


# ─────────────────────────────────────────────────────────────────────────
# Merge
# ─────────────────────────────────────────────────────────────────────────

def merge_sidecars(local_text: str, incoming_text: str) -> MergeResult:
    """Merge `incoming_text` into `local_text` per the conflict rules.

    The merged text preserves the local file's order, comments, and blank
    lines. New incoming-only keys are appended at the end in alphabetical
    order (one block, no separator — keep diffs minimal).
    """
    local_rows = parse_lines(local_text)
    incoming_rows = parse_lines(incoming_text)

    local_pairs = _index_pairs(local_rows)
    incoming_pairs = _index_pairs(incoming_rows)

    result_pairs: Dict[str, str] = dict(local_pairs)
    conflicts: List[Conflict] = []
    adopted: List[str] = []
    overwritten: List[str] = []

    for key, incoming_value in incoming_pairs.items():
        if key not in local_pairs:
            result_pairs[key] = incoming_value
            adopted.append(key)
            continue

        # Compare stripped values to avoid spurious whitespace conflicts.
        if local_pairs[key].strip() == incoming_value.strip():
            continue

        if key in MACHINE_KEYS:
            result_pairs[key] = incoming_value
            overwritten.append(key)
            continue

        # Genuine semantic conflict — keep local, record it.
        conflicts.append(Conflict(
            key=key,
            local_value=local_pairs[key].strip(),
            incoming_value=incoming_value.strip(),
        ))
        # result_pairs[key] is already the local value (from the dict copy).

    # Reassemble in the local file's original order.
    out_lines: List[str] = []
    written: set[str] = set()
    for kind, key, raw in local_rows:
        if kind == _ROW_PAIR:
            if key in written:
                continue
            written.add(key)
            out_lines.append(f'{key}={result_pairs[key]}')
        else:
            out_lines.append(raw)

    # Append adopted (incoming-only) keys alphabetically at the end.
    new_keys = sorted(k for k in adopted if k not in written)
    if new_keys and out_lines and out_lines[-1].strip():
        # Separate adopted block from existing content with a blank line.
        out_lines.append('')
    for key in new_keys:
        out_lines.append(f'{key}={result_pairs[key]}')

    merged_text = '\n'.join(out_lines)
    if not merged_text.endswith('\n'):
        merged_text += '\n'

    return MergeResult(
        merged_text=merged_text,
        conflicts=conflicts,
        adopted=adopted,
        overwritten=overwritten,
    )


# ─────────────────────────────────────────────────────────────────────────
# s3_* re-stamp (used by ant-ingest after catalog push)
# ─────────────────────────────────────────────────────────────────────────

def restamp_s3_block(
    text: str,
    *,
    bucket: str,
    key: str,
    url: str,
    etag: str,
    uploaded_at: str,
) -> str:
    """Update the s3_* keys in a sidecar, preserving everything else.

    If any s3_* key is absent from `text`, it is inserted at the end of
    the file (as a block) so the sidecar always reflects the latest
    catalog-side push location.
    """
    new_values = {
        's3_bucket': bucket,
        's3_key': key,
        's3_url': url,
        's3_etag': etag,
        's3_uploaded_at': uploaded_at,
    }
    rows = parse_lines(text)
    out_lines: List[str] = []
    seen: set[str] = set()
    for kind, k, raw in rows:
        if kind == _ROW_PAIR:
            if k in seen:
                continue
            seen.add(k)
            if k in new_values:
                out_lines.append(f'{k}={new_values[k]}')
            else:
                out_lines.append(f'{k}={raw}')
        else:
            out_lines.append(raw)

    missing = [k for k in new_values if k not in seen]
    if missing and out_lines and out_lines[-1].strip():
        out_lines.append('')
    for k in missing:
        out_lines.append(f'{k}={new_values[k]}')

    body = '\n'.join(out_lines)
    if not body.endswith('\n'):
        body += '\n'
    return body


# ─────────────────────────────────────────────────────────────────────────
# File I/O helpers
# ─────────────────────────────────────────────────────────────────────────

def read_sidecar(path: Path) -> str:
    """Read a sidecar file. Empty string if missing."""
    path = Path(path)
    if not path.exists():
        return ''
    return path.read_text(encoding='utf-8', errors='replace')


def write_sidecar_atomic(path: Path, text: str) -> None:
    """Write a sidecar via a temp-file + rename so a crash mid-write
    can't leave a truncated file behind."""
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)
