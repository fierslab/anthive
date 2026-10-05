"""``ant ingest-check``: read-only checks of a dataset submission.

The package holds a registry of checks (``Check``), the context they share
(``context.CheckContext``), the report model (``report``) and the check
modules ``checks_*``. Every check returns an ``Outcome``; ``run_checks``
runs them in the frozen order of ``CHECK_IDS`` and never lets one failing
check stop the rest.

The check ids are a contract (the curator skill is written against them):
add new ones at the end of a family, never rename.

Strictly non-destructive: nothing here writes into the store or the
submission folder, and nothing calls the two legacy checkers that rewrite
``.md`` files (the h5ad checker and its umbrella wrapper) or ``md_check`` with
fixing enabled (``tests/test_ingest_check_no_destructive.py`` enforces it).
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

PASS = 'pass'
WARN = 'warn'
FAIL = 'fail'
SKIP = 'skip'
STATUSES = (PASS, WARN, FAIL, SKIP)

#: Reasons that hold a passing submission for an admin (report ``held_reasons``).
HELD_CONTROLLED = 'controlled_access'
HELD_LICENCE = 'licence_unknown'
HELD_PUBLIC = 'public_access'
HELD_DONOR_OVERLAP = 'donor_overlap'
HELD_DATA_USE = 'data_use_unknown'
HELD_REASONS = (HELD_CONTROLLED, HELD_LICENCE, HELD_PUBLIC, HELD_DONOR_OVERLAP, HELD_DATA_USE)


@dataclass
class Outcome:
    """Result of one check.

    Attributes:
        status: ``pass``, ``warn``, ``fail`` or ``skip``.
        summary: One line for the curator.
        details: Curator-safe facts (counts, digests, column names, never
            donor identifiers or crosswalk content).
        fix: How to fix it (empty on pass).
        held: Held reasons this outcome adds (``HELD_*``).
        admin_detail: Extra facts for admins only (cohort names, matched
            dataset ids, match tables), stripped from the curator view.
        info: A pass worth mentioning (counted as info, not as a pass).
    """

    status: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)
    fix: str = ''
    held: List[str] = field(default_factory=list)
    admin_detail: Dict[str, Any] = field(default_factory=dict)
    info: bool = False


def ok(summary: str, **details: Any) -> Outcome:
    """A passing outcome."""
    return Outcome(PASS, summary, details)


def note(summary: str, **details: Any) -> Outcome:
    """A pass that carries information the curator should see."""
    return Outcome(PASS, summary, details, info=True)


def warn(summary: str, fix: str = '', **details: Any) -> Outcome:
    """A warning."""
    return Outcome(WARN, summary, details, fix=fix)


def fail(summary: str, fix: str = '', **details: Any) -> Outcome:
    """An error: the submission cannot publish until it is fixed."""
    return Outcome(FAIL, summary, details, fix=fix)


def skip(summary: str, **details: Any) -> Outcome:
    """The check could not run (input absent); never counted as pass or fail."""
    return Outcome(SKIP, summary, details)


def no_database(ctx: Any) -> Outcome:
    """The skip outcome of a check that needs the ``.duckdb`` when none could be read."""
    return skip('no readable .duckdb' + (f' ({ctx.convert_error})' if ctx.convert_error else ''))


@dataclass(frozen=True)
class Check:
    """One registered check."""

    id: str
    family: str
    fn: Callable[[Any], Outcome]
    needs: frozenset
    docs: str


_REGISTRY: Dict[str, Check] = {}


def check(check_id: str, needs: Iterable[str] = ()) -> Callable:
    """Register a check function ``fn(ctx) -> Outcome`` under ``check_id``.

    ``needs`` names the heavy inputs (``h5ad``, ``duckdb``, ``index``) so
    ``--no-convert`` and ``--profile-only`` can skip a check without running it.
    """
    def decorator(fn: Callable[[Any], Outcome]) -> Callable[[Any], Outcome]:
        if check_id in _REGISTRY:
            raise ValueError(f'duplicate check id {check_id}')
        family = check_id.split('.', 1)[0]
        _REGISTRY[check_id] = Check(check_id, family, fn, frozenset(needs), f'reference/checks.md#{check_id}')
        return fn
    return decorator


def tool_version() -> str:
    """Anthive version (``lib/__init__.py``), read as text so the package need not be importable as ``lib``."""
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    match = re.search(r'__version__\s*=\s*"([^"]+)"', (Path(__file__).resolve().parents[1] / '__init__.py').read_text())
    return match.group(1) if match else 'unknown'


def _load_modules() -> None:
    """Import the check modules (registration order is the report order)."""
    from . import (checks_files, checks_meta, checks_layers, checks_facets,  # noqa: F401, PLC0415
                   checks_donors, checks_cells, checks_design, checks_access)  # registration by import


def all_checks() -> List[Check]:
    """Every registered check in report order."""
    _load_modules()
    return list(_REGISTRY.values())


def check_ids() -> List[str]:
    """The frozen check ids in report order."""
    return [item.id for item in all_checks()]


@dataclass
class CheckResult:
    """An outcome with the identity of its check."""

    id: str
    family: str
    docs: str
    outcome: Outcome
    seconds: float = 0.0


def run_checks(ctx: Any, only: Optional[Sequence[str]] = None, skip_ids: Sequence[str] = ()) -> List[CheckResult]:
    """Run the selected checks against ``ctx``.

    Args:
        ctx: A ``context.CheckContext``.
        only: Check ids (or family prefixes such as ``facets``) to run; None runs all.
        skip_ids: Ids (or family prefixes) to leave out; they appear as ``skip``.

    Returns:
        One result per registered check, in registry order. A check that
        raises becomes a ``fail`` with ``internal_error`` in its details, so a
        bug in one check never hides the others (the CLI exits 2 on those).
    """
    import time  # noqa: PLC0415

    def selected(check_id: str, names: Sequence[str]) -> bool:
        return any(check_id == name or check_id.startswith(name + '.') for name in names)

    results: List[CheckResult] = []
    for item in all_checks():
        started = time.monotonic()
        if only is not None and not selected(item.id, only):
            continue
        if selected(item.id, skip_ids):
            outcome = skip('skipped by request')
        elif 'duckdb' in item.needs and not ctx.convert_allowed and ctx.existing_duckdb_path() is None:
            outcome = skip('needs a .duckdb; none given and --no-convert is set')
        elif 'h5ad' in item.needs and ctx.h5ad_path is None:
            outcome = skip('needs the h5ad; not found')
        else:
            try:
                outcome = item.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - a check bug must not hide the other checks
                outcome = Outcome(FAIL, f'check crashed: {type(exc).__name__}',
                                  {'internal_error': True}, fix='This is a bug in ant ingest-check; report it to an admin.',
                                  admin_detail={'error': str(exc), 'traceback': traceback.format_exc(limit=6)})
        results.append(CheckResult(item.id, item.family, item.docs, outcome, time.monotonic() - started))
    return results
