"""Minimal, verified text edits of a dataset ``.md`` front matter.

A YAML round trip (``metadata.write_md_file``) rewrites most dataset ``.md``
files (quoting, folding, key order), which buries a one-key change in a large
diff. These helpers change only the lines they must, then re-parse the result
and compare it with the same change applied to the parsed dictionary; any
difference raises :class:`MdEditError` and nothing is written.

Operations (all on the front matter between the two ``---`` lines):

* :func:`set_top`: set or insert a top-level key (scalar, list or mapping);
* :func:`set_path` / :func:`delete_path`: set or delete a key inside nested
  mappings (``['api', 'obs_rename', 'Braak']``);
* :func:`bump_version`: patch (or minor) bump of ``version`` plus a ``changelog``
  entry (with ``previous_sha256`` when the data changed) and the ``updated`` date;
* :func:`append_top`: append an item to a top-level list (a caveat);
* :func:`apply_edits`: several operations, one verification.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

FRONT_RE = re.compile(r'\A---\n(.*?)\n---\n', re.DOTALL)


class MdEditError(ValueError):
    """The edit could not be applied or did not reproduce the intended data."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' '))


def _is_content(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith('#')


def _block_end(lines: List[str], start: int) -> int:
    """Index after the block of the mapping key on ``lines[start]``.

    A sequence under the key may sit at the key's own indent (``key:`` then
    ``- item``), so a ``- `` line at that indent belongs to the block.
    """
    base = _indent(lines[start])
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if _is_content(line):
            indent = _indent(line)
            if indent < base or (indent == base and not line.lstrip().startswith('- ') and
                                 not line.lstrip() == '-'):
                break
        index += 1
    while index > start + 1 and not lines[index - 1].strip():
        index -= 1
    return index


def _key_line(lines: List[str], key: str, start: int, end: int, indent: int) -> Optional[int]:
    pattern = re.compile(rf'^ {{{indent}}}(?:{re.escape(key)}|\'{re.escape(key)}\'|"{re.escape(key)}"):(?:\s|$)')
    for index in range(start, end):
        if pattern.match(lines[index]):
            return index
    return None


def _inline_value(line: str) -> Optional[str]:
    """The text after ``key:`` on a key line (comment stripped), or None for a block key (``key:``)."""
    after = line.split(':', 1)[1] if ':' in line else ''
    text = after.strip()
    if not text or text.startswith('#'):
        return None
    if ' #' in text and not text.startswith(('"', "'")):
        text = text.split(' #', 1)[0].rstrip()
    return text


def _blockify(lines: List[str], index: int, indent: int) -> None:
    """Turn ``key: {}`` / ``key: []`` / ``key: null`` / a flow collection into block style in place.

    Lines appended under a key whose value sits on the key line itself (``caveats: []``, which vark init
    writes) are not valid YAML, so the value is rewritten in block style first (an empty or null value
    becomes a bare ``key:``). Scalars are left alone.
    """
    text = _inline_value(lines[index])
    if text is None:
        return
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MdEditError(f'cannot read the value on line {lines[index]!r}: {exc}') from exc
    if value is not None and not isinstance(value, (list, dict)):
        return
    key_text = lines[index].split(':', 1)[0].strip()
    block_end = _block_end(lines, index)
    if not value:
        lines[index:block_end] = [' ' * indent + key_text + ':']
    else:
        dumped = _dump('__k__', value, indent)
        dumped[0] = ' ' * indent + key_text + ':'
        lines[index:block_end] = dumped


def _dump(key: str, value: Any, indent: int) -> List[str]:
    text = yaml.dump({key: value}, default_flow_style=False, sort_keys=False, allow_unicode=True, width=10**6)
    prefix = ' ' * indent
    return [prefix + line if line else line for line in text.rstrip('\n').split('\n')]


def _split(text: str) -> Tuple[List[str], str]:
    match = FRONT_RE.match(text)
    if not match:
        raise MdEditError('no YAML front matter')
    return match.group(1).split('\n'), text[match.end():]


def _join(lines: List[str], rest: str) -> str:
    return '---\n' + '\n'.join(lines) + '\n---\n' + rest


# ── operations on the line list ──────────────────────────────────────────────

def _set_lines(lines: List[str], path: Sequence[str], value: Any) -> None:
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(path):
        found = _key_line(lines, key, start, end, indent)
        last = depth == len(path) - 1
        if found is None:
            if not last:
                raise MdEditError(f'path {list(path)}: {key!r} is not present')
            insert_at = end
            anchor = _insertion_point(lines, start, end, indent, key) if indent == 0 else insert_at
            lines[anchor:anchor] = _dump(key, value, indent)
            return
        if not last:
            _blockify(lines, found, indent)
        block_end = _block_end(lines, found)
        if last:
            lines[found:block_end] = _dump(key, value, indent)
            return
        child_indent = next((_indent(line) for line in lines[found + 1:block_end] if _is_content(line)), indent + 2)
        start, end, indent = found + 1, block_end, child_indent


def _insertion_point(lines: List[str], start: int, end: int, indent: int, key: str) -> int:
    """Where a new top-level scalar key goes: before ``api:`` / ``changelog:`` when present."""
    for anchor in ('api', 'caveats', 'facet_notes', 'changelog'):
        found = _key_line(lines, anchor, start, end, indent)
        if found is not None:
            return found
    return end


def _delete_lines(lines: List[str], path: Sequence[str]) -> bool:
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(path):
        found = _key_line(lines, key, start, end, indent)
        if found is None:
            return False
        block_end = _block_end(lines, found)
        if depth == len(path) - 1:
            del lines[found:block_end]
            return True
        child_indent = next((_indent(line) for line in lines[found + 1:block_end] if _is_content(line)), indent + 2)
        start, end, indent = found + 1, block_end, child_indent
    return False


# ── the same operations on the parsed dictionary (the verification model) ────

def _model_set(data: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    node = data
    for key in path[:-1]:
        if node.get(key) is None:
            node[key] = {}
        node = node[key]
    node[path[-1]] = copy.deepcopy(value)


def _model_delete(data: Dict[str, Any], path: Sequence[str]) -> None:
    node = data
    for key in path[:-1]:
        node = node.get(key) or {}
    node.pop(path[-1], None)


def set_top(key: str, value: Any) -> Dict[str, Any]:
    """Operation: set or insert a top-level key."""
    return {'op': 'set', 'path': [key], 'value': value}


def set_path(path: Sequence[str], value: Any) -> Dict[str, Any]:
    """Operation: set or insert a key inside nested mappings."""
    return {'op': 'set', 'path': list(path), 'value': value}


def delete_path(path: Sequence[str]) -> Dict[str, Any]:
    """Operation: delete a key (a no-op when it is absent)."""
    return {'op': 'delete', 'path': list(path)}


def bump_version(changes: Sequence[str], date: str, part: str = 'patch',
                 previous_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Operation: bump ``version``, set ``updated`` (when present) and append a changelog entry.

    Args:
        changes: Lines of the changelog entry.
        date: ISO date of the entry.
        part: ``patch`` (metadata only), ``minor`` (additive: X.Y+1.0) or ``major`` (data altered: X+1.0.0);
            design section 25 of doc/DESIGN_dataset_ingestion.md.
        previous_sha256: Recorded in the entry when the h5ad changed.
    """
    if part not in ('patch', 'minor', 'major'):
        raise MdEditError(f'unknown version part {part!r}')
    operation = {'op': 'bump', 'changes': list(changes), 'date': date, 'part': part}
    if previous_sha256:
        operation['previous_sha256'] = previous_sha256
    return operation


def append_top(key: str, item: Any) -> Dict[str, Any]:
    """Operation: append one item to a top-level list (created when absent), e.g. a caveat."""
    return {'op': 'append', 'path': [key], 'value': item}


def _append_lines(lines: List[str], data: Dict[str, Any], key: str, item: Any) -> None:
    """Append ``item`` to the top-level list ``key``; ``data`` is the verification model."""
    found = _key_line(lines, key, 0, len(lines), 0)
    current = data.get(key)
    if found is None or current is None:
        if found is not None:
            del lines[found:_block_end(lines, found)]
        anchor = _insertion_point(lines, 0, len(lines), 0, key)
        lines[anchor:anchor] = _dump(key, [item], 0)
        data[key] = [item]
        return
    if not isinstance(current, list):
        raise MdEditError(f'{key!r} is not a list')
    _blockify(lines, found, 0)
    end = _block_end(lines, found)
    item_indent = next((_indent(line) for line in lines[found + 1:end] if line.lstrip().startswith('-')), 0)
    dumped = yaml.dump([item], default_flow_style=False, sort_keys=False, allow_unicode=True, width=10**6)
    lines[end:end] = [' ' * item_indent + line if line else line for line in dumped.rstrip('\n').split('\n')]
    current.append(item)


def _bump(version: Any, part: str = 'patch') -> str:
    parts = str(version).split('.')
    if len(parts) != 3 or not all(part_text.isdigit() for part_text in parts):
        raise MdEditError(f'version {version!r} is not major.minor.patch')
    if part == 'major':
        return f'{int(parts[0]) + 1}.0.0'
    if part == 'minor':
        return f'{parts[0]}.{int(parts[1]) + 1}.0'
    return f'{parts[0]}.{parts[1]}.{int(parts[2]) + 1}'


def _apply_bump(lines: List[str], data: Dict[str, Any], operation: Dict[str, Any]) -> None:
    """Patch bump, ``updated`` date and changelog entry; ``data`` is the verification model."""
    new_version = _bump(data.get('version'), operation.get('part', 'patch'))
    version_line = _key_line(lines, 'version', 0, len(lines), 0)
    if version_line is None:
        raise MdEditError('no version key')
    lines[version_line] = f'version: {new_version}'
    updated_line = _key_line(lines, 'updated', 0, len(lines), 0)
    if updated_line is not None:
        lines[updated_line] = f"updated: '{operation['date']}'"
    entry = {'version': new_version, 'date': operation['date']}
    if operation.get('previous_sha256'):
        entry['previous_sha256'] = operation['previous_sha256']
    entry['changes'] = list(operation['changes'])
    changelog_line = _key_line(lines, 'changelog', 0, len(lines), 0)
    if changelog_line is not None and data.get('changelog') is None:
        del lines[changelog_line:_block_end(lines, changelog_line)]
        data.pop('changelog', None)
        changelog_line = None
    if changelog_line is not None:
        _blockify(lines, changelog_line, 0)
    if changelog_line is None:
        lines.extend(_dump('changelog', [entry], 0))
    else:
        end = _block_end(lines, changelog_line)
        item_indent = next((_indent(line) for line in lines[changelog_line + 1:end]
                            if line.lstrip().startswith('-')), 0)
        dumped = yaml.dump([entry], default_flow_style=False, sort_keys=False, allow_unicode=True, width=10**6)
        lines[end:end] = [' ' * item_indent + line if line else line for line in dumped.rstrip('\n').split('\n')]
    data['version'] = new_version
    if 'updated' in data:
        data['updated'] = operation['date']
    data.setdefault('changelog', []).append(entry)


def apply_edits(text: str, operations: Sequence[Dict[str, Any]]) -> str:
    """Apply operations to the front matter of ``text`` and verify the result.

    Args:
        text: The whole ``.md`` file.
        operations: Results of :func:`set_top`, :func:`set_path`,
            :func:`delete_path`, :func:`bump_version`, applied in order.

    Returns:
        The new file text (body untouched).

    Raises:
        MdEditError: the front matter is missing or malformed, a path is
            absent, or the re-parsed result differs from the intended data.
    """
    lines, rest = _split(text)
    before = yaml.safe_load('\n'.join(lines)) or {}
    expected = copy.deepcopy(before)
    for operation in operations:
        if operation['op'] == 'set':
            _set_lines(lines, operation['path'], operation['value'])
            _model_set(expected, operation['path'], operation['value'])
        elif operation['op'] == 'delete':
            _delete_lines(lines, operation['path'])
            _model_delete(expected, operation['path'])
        elif operation['op'] == 'bump':
            _apply_bump(lines, expected, operation)
        elif operation['op'] == 'append':
            _append_lines(lines, expected, operation['path'][0], operation['value'])
        else:
            raise MdEditError(f'unknown operation {operation["op"]!r}')
    after = yaml.safe_load('\n'.join(lines)) or {}
    if after != expected:
        changed = sorted(key for key in set(after) | set(expected) if after.get(key) != expected.get(key))
        raise MdEditError(f're-parsed front matter differs from the intended change in {changed}')
    return _join(lines, rest)
