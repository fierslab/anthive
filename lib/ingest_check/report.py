"""Report model of ``ant ingest-check``: JSON (schema ``anthive.ingest_check/1``), markdown, redaction.

Two audiences. ``curator`` is what the submitter sees; ``admin`` adds
``admin_detail`` (matched dataset ids, cohort names, demographic match
tables). Neither carries donor identifiers or crosswalk content: checks put
only counts and digests into their details, and ``scrub`` replaces any
crosswalk alias or canonical id that still slipped into a text with
``[redacted]`` (and counts it in ``redactions``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Set

from . import FAIL, PASS, SKIP, WARN, CheckResult

SCHEMA = 'anthive.ingest_check/1'
AUDIENCES = ('curator', 'admin')
_TOKEN = re.compile(r'[A-Za-z0-9_.:\-]{4,}')

_SEVERITY = {FAIL: 'error', WARN: 'warning', PASS: 'none', SKIP: 'none'}


def build_report(ctx: Any, results: List[CheckResult], audience: str, tool_version: str) -> Dict[str, Any]:
    """Assemble the report dict from check results.

    Args:
        ctx: The ``CheckContext`` (dataset identity, settings).
        results: Output of ``run_checks``.
        audience: ``curator`` or ``admin``.
        tool_version: Anthive version string.

    Returns:
        The report. ``result`` is ``fail`` when any check failed, ``publish``
        is ``auto`` only without errors and held reasons.
    """
    if audience not in AUDIENCES:
        raise ValueError(f'audience must be one of {AUDIENCES}')
    counts = {'error': 0, 'warning': 0, 'info': 0}
    held: List[str] = []
    entries: List[Dict[str, Any]] = []
    admin_detail: Dict[str, Any] = {}
    for item in results:
        outcome = item.outcome
        severity = _SEVERITY[outcome.status]
        if outcome.status == PASS and outcome.info:
            counts['info'] += 1
        elif outcome.status == FAIL:
            counts['error'] += 1
        elif outcome.status == WARN:
            counts['warning'] += 1
        for reason in outcome.held:
            if reason not in held:
                held.append(reason)
        entries.append({'id': item.id, 'status': outcome.status, 'severity': 'info' if outcome.status == PASS and outcome.info else severity,
                        'summary': outcome.summary, 'details': outcome.details, 'fix': outcome.fix,
                        'docs': item.docs})
        if outcome.admin_detail:
            admin_detail[item.id] = outcome.admin_detail
    failed = counts['error'] > 0
    report: Dict[str, Any] = {
        'schema': SCHEMA, 'tool_version': tool_version,
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'audience': audience, 'mode': ctx.mode,
        'dataset': {'group': ctx.group_dir.name, 'basename': ctx.basename, 'db_id': ctx.db_id,
                    'sha256': ctx.sha256 if ctx.h5ad_path else None, 'n_obs': _n(ctx, 'n_obs'), 'n_vars': _n(ctx, 'n_vars')},
        'result': 'fail' if failed else 'pass',
        'publish': 'auto' if not failed and not held else 'held',
        'held_reasons': held,
        'counts': counts,
        'checks': entries,
    }
    if audience == 'admin':
        report['admin_detail'] = admin_detail
    return report


def _n(ctx: Any, attribute: str) -> Any:
    """``n_obs`` / ``n_vars`` from the open h5ad if a check opened it, else the ``.md``."""
    adata = ctx._cache.get('adata')  # noqa: SLF001
    if adata is not None:
        return int(adata.n_obs if attribute == 'n_obs' else adata.n_vars)
    value = ctx.md.get(attribute)
    return int(value) if isinstance(value, int) else None


def scrub(report: Dict[str, Any], tokens: Iterable[str]) -> int:
    """Replace sensitive tokens in every string of ``report`` (in place); returns how many replaced.

    A token is replaced when it occurs as a whole word (letters, digits,
    ``_ . : -``). Numbers are left alone. Meant as a last line of defence:
    the checks themselves never put crosswalk values into a report.
    """
    sensitive: Set[str] = set(tokens)
    replaced = 0
    if not sensitive:
        return 0

    def clean(text: str) -> str:
        def swap(match: re.Match) -> str:
            nonlocal replaced
            if match.group(0) in sensitive:
                replaced += 1
                return '[redacted]'
            return match.group(0)
        return _TOKEN.sub(swap, text)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return clean(node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {(clean(key) if isinstance(key, str) else key): walk(value) for key, value in node.items()}
        return node

    cleaned = walk(report)
    report.clear()
    report.update(cleaned)
    report['redactions'] = replaced
    return replaced


def _json_default(value: Any) -> Any:
    """JSON form of the odd values a check may put into its details (numpy scalars, paths, sets)."""
    if hasattr(value, 'item'):
        return value.item()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def to_json(report: Dict[str, Any]) -> str:
    """The report as indented JSON; numpy scalars, paths and sets are converted instead of raising."""
    return json.dumps(report, indent=1, default=_json_default)


def curator_view(report: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of an admin report without ``admin_detail``."""
    view = {key: value for key, value in report.items() if key != 'admin_detail'}
    view['audience'] = 'curator'
    return view


_MARK = {PASS: 'pass', WARN: 'WARN', FAIL: 'FAIL', SKIP: 'skip'}


def render_markdown(report: Dict[str, Any]) -> str:
    """Human summary: verdict, held reasons, then failed and warned checks with their fix, then the rest."""
    dataset = report['dataset']
    lines = [f'# Ingest check: {dataset["group"]}/{dataset["basename"]}', '',
             f'- Result: **{report["result"]}**, publish: **{report["publish"]}**'
             + (f' ({", ".join(report["held_reasons"])})' if report['held_reasons'] else ''),
             f'- Errors {report["counts"]["error"]}, warnings {report["counts"]["warning"]}, '
             f'notes {report["counts"]["info"]}',
             f'- Cells {dataset["n_obs"]}, genes {dataset["n_vars"]}, sha256 {dataset["sha256"]}',
             f'- Tool {report["tool_version"]}, schema {report["schema"]}, {report["created"]}', '']
    problems = [entry for entry in report['checks'] if entry['status'] in (FAIL, WARN)]
    if problems:
        lines += ['## To fix', '']
        for entry in problems:
            lines.append(f'### {_MARK[entry["status"]]} {entry["id"]}')
            lines.append(entry['summary'])
            if entry['fix']:
                lines.append(f'Fix: {entry["fix"]}')
            lines.append('')
    lines += ['## All checks', '', '| check | status | summary |', '| --- | --- | --- |']
    for entry in report['checks']:
        summary = entry['summary'].replace('|', '/').replace('\n', ' ')
        lines.append(f'| {entry["id"]} | {_MARK[entry["status"]]} | {summary[:160]} |')
    lines.append('')
    return '\n'.join(lines)
