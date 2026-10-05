"""Metadata checks: ``.md``, group ``index.md``, ``.h5ad.mus`` annotation, initial version."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import Outcome, check, fail, ok, skip, warn
from .context import MODE_EXISTING, MODE_NEW
from .thresholds import MIN_DESCRIPTION_CHARS

_FINDING_RULE = re.compile(r'^[^:]+: \[[a-z_]+\] ')
_INITIAL_VERSION = '1.0.0'


def _description_length(ctx) -> int:
    """Characters of the dataset description: the ``description`` key, else the markdown body."""
    text = ctx.md.get('description')
    if not text:
        text = (ctx.md_parse.get('body') or '')
    return len(str(text).strip())


def store_group_ids(store_root: Path, exclude_folder: str) -> Dict[str, str]:
    """``{GROUP_ID upper-case: folder}`` of every group ``index.md`` in the store except one folder."""
    from metadata import parse_md_file  # noqa: PLC0415

    found: Dict[str, str] = {}
    for index_path in sorted(store_root.glob('*/index.md')):
        folder = index_path.parent.name
        if folder.startswith('_') or folder == exclude_folder:
            continue
        try:
            group_id = parse_md_file(index_path)[0].get('group_id')
        except Exception:  # noqa: BLE001 - an unreadable neighbour is not this submission's problem
            continue
        if group_id:
            found[str(group_id).upper()] = folder
    return found


@check('meta.md_parse')
def meta_md_parse(ctx) -> Outcome:
    """The dataset ``.md`` exists and its YAML front matter parses."""
    error = ctx.md_parse['error']
    if error:
        return fail(f'cannot use {ctx.basename}.md: {error}',
                    'Provide a catalogue .md next to the h5ad: YAML front matter between two --- lines. '
                    'See reference/md-template.md in the skill.')
    return ok(f'{len(ctx.md)} front matter keys')


@check('meta.catalog')
def meta_catalog(ctx) -> Outcome:
    """Catalogue ids: valid 3-character id, no duplicate in the group or the store (md_check, fix off)."""
    from md_check import MdCheckReport, check_md_folder, collect_db_ids, generate_file_id  # noqa: PLC0415

    if ctx.md_parse['error']:
        return skip('the .md could not be parsed (see meta.md_parse)')
    report = MdCheckReport()
    # The group folder itself is the scan root: md_check skips any folder below
    # the root whose name starts with "_", so scanning _incoming from above would check nothing.
    check_md_folder(ctx.group_dir, report, fix=False, namespace_mode='warn',
                    known_db_ids=collect_db_ids(ctx.store_root), store_root=ctx.store_root)
    mine = (f'{ctx.basename}.md', 'index.md')
    errors: List[str] = [
        message for message in report.errors
        if not _FINDING_RULE.match(message) and (message.startswith(mine) or ctx.basename + '.md' in message)]
    lines: List[str] = list(errors)
    script = ctx.store_root / 'scripts' / 'validate_md.py'
    if script.is_file():
        result = subprocess.run([sys.executable, str(script), str(ctx.group_dir)], capture_output=True, text=True,
                                cwd=ctx.scratch_root if ctx.scratch_root.is_dir() else None, check=False)
        if result.returncode != 0:
            lines.extend(line.strip() for line in (result.stdout + result.stderr).splitlines()
                         if ctx.basename in line and line.strip())
    file_id = ctx.file_id
    proposed = None
    if not file_id:
        proposed = generate_file_id(ctx.basename)
        file_id = proposed
    if ctx.mode == MODE_NEW and ctx.group_id and file_id:
        taken = collect_db_ids(ctx.store_root)
        if f'{ctx.group_id}/{file_id}' in taken:
            lines.append(f'{ctx.group_id}/{file_id} already exists in the store')
    if lines:
        return fail('; '.join(dict.fromkeys(lines)),
                    'The dataset id is exactly 3 characters and unique in its group; the group id is exactly '
                    '3 characters and unique in the store. Change the id in the .md.', findings=lines[:10])
    if proposed:
        return warn(f'the .md has no id; the server will generate {proposed!r}',
                    f'Add "id: {proposed}" (3 characters) to the .md to choose it yourself.', proposed_id=proposed)
    return ok(f'dataset id {file_id}' + (f' (db id {ctx.db_id})' if ctx.db_id else ''), db_id=ctx.db_id)


@check('meta.required_keys')
def meta_required_keys(ctx) -> Outcome:
    """title, description (>= 200 characters), version and changelog are present."""
    if ctx.md_parse['error']:
        return skip('the .md could not be parsed (see meta.md_parse)')
    md = ctx.md
    missing = [key for key in ('title', 'version') if not md.get(key)]
    changelog = md.get('changelog')
    if not isinstance(changelog, list) or not changelog:
        missing.append('changelog')
    length = _description_length(ctx)
    problems = [f'missing {key}' for key in missing]
    legacy = ctx.mode == MODE_EXISTING
    if length < MIN_DESCRIPTION_CHARS:
        text = f'description is {length} characters (at least {MIN_DESCRIPTION_CHARS} needed)'
        if legacy and not problems:
            return warn(text + '; existing dataset', 'Add a description key or write the markdown body of the .md.',
                        description_chars=length)
        problems.append(text)
    if problems:
        return fail('; '.join(problems),
                    'Add title, version, a changelog list and a description (the description key, or the markdown '
                    f'text below the front matter) of at least {MIN_DESCRIPTION_CHARS} characters taken from the paper.',
                    description_chars=length, missing=missing)
    return ok('title, description, version and changelog present', description_chars=length)


@check('meta.index_md')
def meta_index_md(ctx) -> Outcome:
    """Group ``index.md``: a new group needs a complete one; a dataset in an existing group must match it."""
    strict = ctx.mode == MODE_NEW and not ctx.is_existing_group
    submitted = ctx.submitted_index_md
    index = ctx.index_md
    if not index:
        if strict:
            return fail('a new group needs an index.md next to the dataset',
                        'Add index.md with group, group_id (3 characters), doi or paper_title, year, authors '
                        'and default_dataset. See reference/md-template.md.')
        return warn('no index.md found for the group', 'Add the group index.md.')
    problems: List[str] = []
    group_ids = store_group_ids(ctx.store_root, ctx.group_dir.name)
    group_id = str(index.get('group_id') or '')
    if not group_id:
        problems.append('group_id missing')
    elif len(group_id) != 3:
        problems.append(f'group_id {group_id!r} is not exactly 3 characters')
    elif group_id.upper() in group_ids:
        problems.append(f'group_id {group_id} is already used by another group')
    if index.get('group') and str(index['group']) != ctx.group_dir.name:
        problems.append(f'group {index["group"]!r} does not match the folder name {ctx.group_dir.name!r}')
    if ctx.is_existing_group and not ctx.in_store and submitted:
        from metadata import parse_md_file  # noqa: PLC0415

        stored = parse_md_file(ctx.store_group_dir / 'index.md')[0] if (ctx.store_group_dir / 'index.md').is_file() else {}
        if stored.get('group_id') and submitted.get('group_id') and str(stored['group_id']) != str(submitted['group_id']):
            problems.append(f'group_id {submitted["group_id"]} differs from the group in the store ({stored["group_id"]})')
    soft: List[str] = []
    if not (index.get('doi') or index.get('paper_title')):
        soft.append('doi or paper_title')
    if not index.get('year'):
        soft.append('year')
    if not (index.get('authors') or index.get('first_author')):
        soft.append('authors')
    if not index.get('default_dataset'):
        soft.append('default_dataset')
    if strict and soft:
        problems.append('missing ' + ', '.join(soft))
    if problems:
        return fail('; '.join(problems), 'Fix index.md: group equals the folder name, group_id is 3 characters and '
                    'unique, and the paper fields are filled.', problems=problems)
    if soft:
        return warn('index.md lacks ' + ', '.join(soft), 'Fill these in index.md.', missing=soft)
    return ok(f'group {index.get("group") or ctx.group_dir.name} ({group_id})', group_id=group_id)


@check('mus.vocab')
def mus_vocab(ctx) -> Outcome:
    """``.h5ad.mus`` annotation (with the ``.env`` cascade) against the mus vocabulary."""
    import mus  # noqa: PLC0415

    if not ctx.mus_path.is_file() and not ctx.annotation:
        return fail('no .h5ad.mus sidecar (and no .env) found', 'Write <basename>.h5ad.mus with the required '
                    'fields. See reference/mus-template.md.')
    verdict = mus.validate_annotation(ctx.annotation)
    details = {'missing_required': verdict['missing_required'], 'invalid_controlled': verdict['invalid_controlled'],
               'missing_soft': verdict['missing_soft']}
    if verdict['missing_required'] or verdict['invalid_controlled']:
        parts = []
        if verdict['missing_required']:
            parts.append('missing required: ' + ', '.join(verdict['missing_required']))
        if verdict['invalid_controlled']:
            parts.append('not in the vocabulary: ' + ', '.join(verdict['invalid_controlled']))
        return fail('; '.join(parts), 'Add the required fields and use vocabulary values (`mus vocab` lists them).',
                    **details)
    if verdict['missing_soft']:
        return warn('recommended fields missing: ' + ', '.join(verdict['missing_soft']),
                    'Fill them in when the paper says (disease, sex, age, doi, year, license...).', **details)
    return ok('annotation is complete', **details)


@check('version.initial', needs=('h5ad',))
def version_initial(ctx) -> Outcome:
    """A new dataset starts at 1.0.0 with one changelog entry carrying the h5ad hash."""
    if ctx.mode == MODE_EXISTING:
        return skip('existing dataset: versions of live datasets are handled by the update route')
    if ctx.md_parse['error']:
        return skip('the .md could not be parsed (see meta.md_parse)')
    md = ctx.md
    problems: List[str] = []
    if str(md.get('version') or '') != _INITIAL_VERSION:
        problems.append(f'version is {md.get("version")!r}, a new dataset starts at {_INITIAL_VERSION}')
    changelog = md.get('changelog')
    entries: List[Any] = changelog if isinstance(changelog, list) else []
    if len(entries) != 1:
        problems.append(f'changelog has {len(entries)} entries, a new dataset has exactly one')
    else:
        entry = entries[0] if isinstance(entries[0], dict) else {}
        for key in ('version', 'date', 'changes'):
            if not entry.get(key):
                problems.append(f'changelog entry lacks {key}')
        if str(entry.get('version') or '') != _INITIAL_VERSION:
            problems.append(f'changelog entry version is {entry.get("version")!r}')
        if str(entry.get('sha256') or '').lower() != (ctx.sha256 or ''):
            problems.append('changelog entry sha256 is not the sha256 of the h5ad')
    if problems:
        return fail('; '.join(problems),
                    f'Set version: {_INITIAL_VERSION} and a single changelog entry: version {_INITIAL_VERSION}, '
                    'date, sha256 of the h5ad (the files.sha256 check reports it) and changes.',
                    sha256=ctx.sha256)
    return ok('version 1.0.0 with a matching changelog entry')
