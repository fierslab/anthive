"""
mus annotation reader — reads `.h5ad.mus` sidecars and the folder `.env`
cascade introduced by the 2026-06 h5adstore migration.

Replaces the old `annotation:` block model that lived inside `.md` YAML
frontmatter. The new model splits responsibilities cleanly:

- The dataset's `<basename>.h5ad.mus` carries experimental annotation
  (organism, tissue, cell_type, technology, suspension_type, …).
- `.env` files in each parent folder carry cascade-able defaults.
- Anthive-catalog fields (`id`, `sha256`, `n_obs`, `layer_stats`,
  `obscat_include`, `default_layer`, …) stay in `.md`.

Format spec (per data/h5adstore/CONVENTIONS.md):
  - Flat `KEY=VALUE`, one per line.
  - List values are comma-separated. Items containing literal commas
    must be double-quoted.
  - Lines starting with `#` are comments; empty lines are ignored.
  - In cascade list-merge, `-foo` removes a previously-added `foo`.
  - The sidecar **REPLACES** (does not merge with) the cascade value
    for any key it specifies.

Vocabulary: the joint vocabulary lives upstream at
`https://codeberg.org/mfiers/mus/raw/branch/main/vocabulary/main.yaml`.
A snapshot (`_mus_vocab.yaml`) is bundled here; use `refresh_vocabulary()`
to update from the canonical source. Matching is case-insensitive with
`_` ≡ ` ` ≡ `-`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────

VocValue = Union[str, List[str]]
Annotation = Dict[str, VocValue]

#: Vocabulary fields whose values are inherently lists. A value with no
#: comma still parses to a list of length 1 for these. Single-valued fields
#: keep `str` unless multi-comma input forces a list.
_LIST_FIELDS = {
    'tissue', 'disease', 'condition', 'doi', 'pmid', 'accession',
    'contributor', 'link', 'age', 'strain', 'cell_type',
}

#: Bundled vocabulary snapshot — refresh from upstream when needed.
_BUNDLED_VOCAB = Path(__file__).with_name('_mus_vocab.yaml')

_VOCAB_CACHE: Optional[dict] = None

#: Source URL for the canonical vocabulary.
VOCAB_URL = 'https://codeberg.org/mfiers/mus/raw/branch/main/vocabulary/main.yaml'


# ─────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────

def _split_csv_quoted(raw: str) -> List[str]:
    """Split a comma-separated string with optional double-quoted items
    (items containing literal commas must be quoted). Whitespace around
    items is stripped; surrounding double-quotes are removed."""
    items: List[str] = []
    buf: List[str] = []
    in_quote = False
    for ch in raw:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == ',' and not in_quote:
            items.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append(''.join(buf).strip())
    # Strip surrounding quotes on each item
    out: List[str] = []
    for it in items:
        if len(it) >= 2 and it[0] == '"' and it[-1] == '"':
            it = it[1:-1]
        if it:
            out.append(it)
    return out


def _parse_value(field: str, raw: str) -> VocValue:
    """Parse a raw KEY=VALUE right-hand side into a string or list."""
    raw = raw.strip()
    if not raw:
        return ''

    # Migration quirk: some sidecars used Python-list literal form
    # (e.g. `age=['6m', '12m']`). Accept it gracefully.
    if raw.startswith('[') and raw.endswith(']'):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = _split_csv_quoted(inner)
        items = [it.strip("'\"") for it in items]
        return items if len(items) != 1 else items[0]

    if field in _LIST_FIELDS or (',' in raw and not (raw[0] == '"' and raw.count('"') == 2)):
        items = _split_csv_quoted(raw)
        # Keep as list if known list field OR if multiple items parsed
        if field in _LIST_FIELDS:
            return items if len(items) != 1 else items[0]
        if len(items) > 1:
            return items
        return items[0] if items else ''

    # Single string — strip surrounding quotes if any
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def parse_mus_file(path: Path) -> Annotation:
    """Parse one `.env` or `.h5ad.mus` file. Returns empty dict if missing."""
    out: Annotation = {}
    path = Path(path)
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue
        if '=' not in line:
            continue
        key, _, raw = line.partition('=')
        key = key.strip()
        if not key:
            continue
        out[key] = _parse_value(key, raw)
    return out


def _serialize_value(value: VocValue) -> str:
    """Render a Python value back to the `.h5ad.mus` KEY=VALUE form.

    - List → comma-separated. Items containing commas / quotes get
      double-quoted.
    - String → as-is. Empty string OK (round-trips an explicit empty).
    - None  → empty string.
    Other types are stringified via str().
    """
    if value is None:
        return ''
    if isinstance(value, list):
        items = []
        for entry in value:
            entry_str = str(entry)
            needs_quote = (',' in entry_str) or ('"' in entry_str)
            if needs_quote:
                escaped = entry_str.replace('"', '\\"')
                items.append(f'"{escaped}"')
            else:
                items.append(entry_str)
        return ','.join(items)
    return str(value)


def write_mus_file(path: Path, annotation: Annotation) -> None:
    """Write a per-dataset ``.h5ad.mus`` sidecar (KEY=VALUE format).

    Behaviour:
      - Keys sorted alphabetically for stable diffs.
      - Existing file content is REPLACED (not merged). The caller is
        responsible for round-tripping any pre-existing keys it wants
        to preserve — typical pattern: call ``parse_mus_file(path)``,
        update the dict in place, pass to ``write_mus_file()``.
      - Empty values are written as ``key=`` (explicit). Drop the key
        from ``annotation`` instead if you want to remove it.
      - Atomic via write-tmp-then-rename so a concurrent reader never
        sees a partial file.

    Validation is the CALLER'S job. ``lib.mus.validate_value()`` is
    the right pre-write gate; this helper only handles serialization
    + atomic write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in sorted(annotation.keys()):
        value = annotation[key]
        lines.append(f"{key}={_serialize_value(value)}")
    body = '\n'.join(lines) + '\n'
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(body, encoding='utf-8')
    tmp_path.replace(path)


# ─────────────────────────────────────────────────────────────────────────
# Cascade resolution
# ─────────────────────────────────────────────────────────────────────────

def _as_list(v: VocValue) -> List[str]:
    if isinstance(v, list):
        return v
    if not v:
        return []
    return [v]


def merge_cascade(layers_top_down: List[Annotation]) -> Annotation:
    """Merge cascade layers in order (root-most first, deepest last).

    For list-typed fields, accumulate items across layers; an item prefixed
    with `-` removes a previously-added item with the same name. For
    scalar fields, deeper layers replace shallower ones.
    """
    out: Annotation = {}
    for layer in layers_top_down:
        for key, val in layer.items():
            if key in _LIST_FIELDS:
                current = _as_list(out.get(key, []))
                for v in _as_list(val):
                    if isinstance(v, str) and v.startswith('-'):
                        target = v[1:]
                        current = [x for x in current if x != target]
                    elif v not in current:
                        current.append(v)
                out[key] = current if len(current) != 1 else current[0]
            else:
                out[key] = val
    return out


def read_cascade(start_dir: Path, root: Optional[Path] = None) -> Annotation:
    """Collect `.env` files walking upward from `start_dir` and merge them
    top-down (root-most first). Stops at `root` (if provided) or filesystem
    root. Missing `.env` at any level is fine — only existing files are used.
    """
    start_dir = Path(start_dir).resolve()
    root = root.resolve() if root is not None else None

    found: List[Path] = []
    cur = start_dir
    while True:
        env = cur / '.env'
        if env.is_file():
            found.append(env)
        if root is not None and cur == root:
            break
        if cur.parent == cur:
            break
        cur = cur.parent

    # Reverse: we collected bottom-up, need top-down for cascade
    found.reverse()
    return merge_cascade([parse_mus_file(p) for p in found])


def sidecar_path(h5ad_path: Path) -> Path:
    """Return the `.h5ad.mus` sidecar path for an .h5ad file."""
    h5ad_path = Path(h5ad_path)
    return h5ad_path.parent / (h5ad_path.name + '.mus')


def resolve_annotation(
    h5ad_path: Path,
    root: Optional[Path] = None,
) -> Annotation:
    """Resolve the effective annotation for an h5ad: cascade then sidecar.

    The sidecar **replaces** cascade values per key (not merge); this is
    the per-file authority semantic from CONVENTIONS.md.
    """
    h5ad_path = Path(h5ad_path)
    cascade = read_cascade(h5ad_path.parent, root=root)
    sidecar = parse_mus_file(sidecar_path(h5ad_path))
    out = dict(cascade)
    for key, val in sidecar.items():
        out[key] = val
    return out


# ─────────────────────────────────────────────────────────────────────────
# Vocabulary validation
# ─────────────────────────────────────────────────────────────────────────

def _normalize(value: str) -> str:
    """Vocabulary matching key: lowercase, then `_` ≡ ` ` ≡ `-`."""
    return re.sub(r'[\s_\-]+', ' ', value.lower()).strip()


def load_vocabulary(path: Optional[Path] = None) -> dict:
    """Load the vocabulary YAML (cached after first call). Default is the
    bundled snapshot; pass `path` to use a refreshed copy."""
    global _VOCAB_CACHE
    if _VOCAB_CACHE is not None and path is None:
        return _VOCAB_CACHE
    import yaml
    target = path or _BUNDLED_VOCAB
    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    if path is None:
        _VOCAB_CACHE = data
    return data


def refresh_vocabulary(
    dest: Optional[Path] = None,
    timeout: float = 10.0,
) -> Path:
    """Fetch the canonical vocabulary from upstream and write it to `dest`
    (default: the bundled snapshot path). Invalidates the in-memory cache."""
    global _VOCAB_CACHE
    import urllib.request
    dest = dest or _BUNDLED_VOCAB
    req = urllib.request.Request(VOCAB_URL, headers={'User-Agent': 'anthive4'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    dest.write_bytes(body)
    _VOCAB_CACHE = None
    return dest


def required_fields() -> List[str]:
    """Fields that MUST be present in an effective annotation (HARD)."""
    return list(load_vocabulary().get('required', []))


def soft_fields() -> List[str]:
    """Fields that SHOULD be present (warning if missing)."""
    return list(load_vocabulary().get('soft', []))


#: The expected form of each soft (recommended) field, for clients that help fill a sidecar.
#: ``organisms``: the organisms the field applies to (None: every organism). Controlled fields add
#: their vocabulary values in :func:`field_info`.
FIELD_FORMS: Dict[str, Dict[str, Any]] = {
    'disease': {'form': 'a vocabulary value (MONDO label), or normal for controls; comma separated for several'},
    'condition': {'form': 'free text: the compared conditions or treatments, comma separated'},
    'sex': {'form': 'a vocabulary value; mixed for a dataset with donors or animals of both sexes'},
    'age': {'form': 'a value or a range with a unit, e.g. "75 years", "33-62 years" for several donors, '
                    '"8 weeks" or "P30" for animals; state what it is (age at death, at sampling) when known'},
    'strain': {'form': 'the strain or line of the animals, e.g. C57BL/6J, 5xFAD on C57BL/6J; not for human data',
               'organisms': ['mouse', 'rat', 'macaque', 'zebrafish', 'drosophila']},
    'doi': {'form': 'the DOI of the paper without a URL prefix, e.g. 10.1126/science.adh1938'},
    'pmid': {'form': 'the PubMed id, digits only'},
    'accession': {'form': 'repository accession(s) of the data, e.g. GSE123456, EGAS00001000001; comma separated'},
    'year': {'form': 'the publication year, four digits'},
    'contributor': {'form': 'the people or lab who generated the data, comma separated'},
    'title': {'form': 'a short title of the dataset (one line)'},
    'link': {'form': 'a URL to the paper or the data'},
    'license': {'form': 'an SPDX id (CC-BY-4.0, CC0-1.0, ...) or in-house; never guessed'},
    'source_description': {'form': 'one sentence: where the data come from and what was done to them'},
}


def field_applies(field: str, organism: Any) -> bool:
    """The field applies to a dataset of this organism (fields like ``strain`` are for animals only)."""
    organisms = (FIELD_FORMS.get(field) or {}).get('organisms')
    if not organisms or organism in (None, '', []):
        return True
    values = [str(item) for item in _as_list(organism)]
    return any(_normalize(value) in {_normalize(name) for name in organisms} for value in values)


def field_info() -> Dict[str, Dict[str, Any]]:
    """``{field: {form, organisms, values?}}`` for every soft field (``organisms`` None: all)."""
    info: Dict[str, Dict[str, Any]] = {}
    for field in soft_fields():
        entry = FIELD_FORMS.get(field) or {'form': 'free text'}
        info[field] = {'form': entry['form'], 'organisms': entry.get('organisms')}
        values = allowed_values(field)
        if values:
            info[field]['values'] = values
    return info


def is_controlled(field: str) -> bool:
    """True if `field` has a controlled-value list in the vocabulary."""
    return field in load_vocabulary().get('vocabulary', {})


def allowed_values(field: str) -> Optional[List[str]]:
    """Return the allowed values for a controlled field, or None if free."""
    vocab = load_vocabulary().get('vocabulary', {})
    if field not in vocab:
        return None
    return list(vocab[field])


def validate_value(field: str, value: str) -> bool:
    """True if `value` is acceptable for `field` (free-form fields always OK)."""
    allowed = allowed_values(field)
    if allowed is None:
        return True
    target = _normalize(value)
    return any(target == _normalize(v) for v in allowed)


# ─────────────────────────────────────────────────────────────────────────
# Whole-deposit validation
# ─────────────────────────────────────────────────────────────────────────

def validate_annotation(ann: Annotation) -> Dict[str, List[str]]:
    """Validate an annotation dict against the vocabulary.

    Returns a dict with three keys, each holding human-readable messages:
      - 'missing_required'   — required vocabulary keys absent or empty
      - 'invalid_controlled' — controlled fields with non-vocabulary values
      - 'missing_soft'       — soft keys absent (warnings)
    Empty lists mean a clean pass on that severity.
    """
    out: Dict[str, List[str]] = {
        'missing_required': [], 'invalid_controlled': [], 'missing_soft': [],
    }
    for key in required_fields():
        v = ann.get(key)
        if v is None or v == '' or v == []:
            out['missing_required'].append(key)
    for key in soft_fields():
        v = ann.get(key)
        if (v is None or v == '' or v == []) and field_applies(key, ann.get('organism')):
            out['missing_soft'].append(key)
    for key, val in ann.items():
        if not is_controlled(key):
            continue
        for item in _as_list(val):
            if not isinstance(item, str):
                item = str(item)
            if not validate_value(key, item):
                out['invalid_controlled'].append(f"{key}={item!r}")
    return out
