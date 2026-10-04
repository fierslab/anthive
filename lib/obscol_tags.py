"""Obs-column axis-of-variation tags — anthive-local vocabulary.

See ``lib/obscol_tags_vocab.yaml`` for the tag set and the design note.
Tags live in each dataset's `.md` frontmatter under ``obs_tags``:

    obs_tags:
      donor_id: [donor]
      Braak_stage: [braak]
      leiden: [cluster]

Public API
----------
* :func:`load_vocab`        — parsed YAML, cached
* :func:`known_tags`        — set of valid tag ids
* :func:`validate_tag`      — raise on unknown tag
* :func:`validate_obs_tags` — validate a whole obs_tags dict
* :func:`get_columns_by_tag`— reverse lookup on a parsed .md
* :func:`facet_definition`  — published definition of an ``ant_<tag>`` facet
* :func:`facet_definitions_status` — audit state of those definitions
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

import yaml


VOCAB_PATH = Path(__file__).resolve().parent / 'obscol_tags_vocab.yaml'


class ObsColTagError(ValueError):
    """Raised when an obs_tags value is invalid."""


@lru_cache(maxsize=1)
def load_vocab() -> dict:
    """Parse the vocabulary YAML. Cached after first call."""
    if not VOCAB_PATH.exists():
        raise FileNotFoundError(f"obscol tag vocab not found: {VOCAB_PATH}")
    with open(VOCAB_PATH) as fh:
        return yaml.safe_load(fh) or {}


def known_tags() -> Set[str]:
    """Set of valid tag identifiers."""
    vocab = load_vocab()
    return set((vocab.get('tags') or {}).keys())


def tag_description(tag: str) -> str:
    """Human-readable description; empty if the tag doesn't exist."""
    vocab = load_vocab()
    entry = (vocab.get('tags') or {}).get(tag) or {}
    return str(entry.get('description') or '').strip()


def validate_tag(tag: str) -> None:
    """Raise :class:`ObsColTagError` if the tag is not in the vocab."""
    if tag not in known_tags():
        raise ObsColTagError(
            f"unknown obs-column tag: {tag!r}. "
            f"Valid tags: {sorted(known_tags())}"
        )


def normalize_tags(tags: Iterable[str]) -> List[str]:
    """Return a deduplicated, order-stable list of tags. Validates each."""
    seen: List[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ObsColTagError(f"tag must be a string; got {type(tag).__name__}: {tag!r}")
        tag_clean = tag.strip()
        if not tag_clean:
            continue
        validate_tag(tag_clean)
        if tag_clean not in seen:
            seen.append(tag_clean)
    return seen


def validate_obs_tags(obs_tags: Mapping[str, object]) -> Dict[str, List[str]]:
    """Validate a whole ``obs_tags`` mapping. Returns the normalized form.

    * every value is coerced to a list (a bare string is treated as a
      one-tag list, since YAML round-trips are lenient there);
    * duplicate tags per column are deduplicated;
    * every tag is checked against :func:`known_tags`;
    * empty tag lists are dropped.
    """
    if not isinstance(obs_tags, Mapping):
        raise ObsColTagError(
            f"obs_tags must be a mapping column→tags; got {type(obs_tags).__name__}"
        )
    normalized: Dict[str, List[str]] = {}
    for column, tags in obs_tags.items():
        if not isinstance(column, str) or not column.strip():
            raise ObsColTagError(f"obs_tags column key must be a non-empty string; got {column!r}")
        if isinstance(tags, str):
            tag_list = [tags]
        elif isinstance(tags, Iterable):
            tag_list = list(tags)
        else:
            raise ObsColTagError(
                f"obs_tags[{column!r}] must be a string or list; got {type(tags).__name__}"
            )
        clean = normalize_tags(tag_list)
        if clean:
            normalized[column] = clean
    return normalized


def get_columns_by_tag(md_metadata: Mapping, tag: str) -> List[str]:
    """Reverse lookup — return columns in this .md tagged with ``tag``."""
    obs_tags = md_metadata.get('obs_tags') or {}
    return [col for col, tags in obs_tags.items() if tag in (tags or [])]


FACET_PREFIX = 'ant_'


def _clean_text(value: object) -> str:
    """Collapse YAML folded-scalar whitespace into one line."""
    return ' '.join(str(value or '').split())


def facet_definition(facet: str) -> Dict[str, Any]:
    """Return the published definition of facet column ``facet``.

    ``facet`` is the view column (``ant_sex``); a bare tag (``sex``) is
    accepted too. The definition comes from the tag's ``facet:`` block in
    ``obscol_tags_vocab.yaml``. Always returns the same keys, so callers can
    render it without guards; ``defined`` is False when the vocab has no
    ``facet:`` block for the tag.

    Returns:
        Dict with ``facet``, ``tag``, ``label``, ``defined``, ``summary``,
        ``definition``, ``kind``, ``values``, ``missing``, ``hint_keywords``.
    """
    tag = facet[len(FACET_PREFIX):] if facet.startswith(FACET_PREFIX) else facet
    entry = ((load_vocab().get('tags') or {}).get(tag) or {})
    block = entry.get('facet') or {}
    keywords = [str(k).lower() for k in (block.get('hint_keywords') or []) if str(k).strip()]
    return {
        'facet': FACET_PREFIX + tag,
        'tag': tag,
        'label': _clean_text(entry.get('label')) or tag.replace('_', ' '),
        'defined': bool(block),
        'summary': _clean_text(block.get('summary')),
        'definition': _clean_text(block.get('definition')),
        'kind': _clean_text(block.get('kind')) or None,
        'values': _clean_text(block.get('values')),
        'missing': _clean_text(block.get('missing')),
        'hint_keywords': keywords,
    }


def facet_definitions_status() -> Dict[str, Optional[str]]:
    """Audit state of the facet definitions (``state`` and ``note``)."""
    status = load_vocab().get('facet_definitions_status') or {}
    return {
        'state': _clean_text(status.get('state')) or None,
        'note': _clean_text(status.get('note')) or None,
    }
