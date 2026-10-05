"""Access checks: level, controlled data and licence. These decide whether a pass can publish by itself."""
from __future__ import annotations

import re

from . import (HELD_CONTROLLED, HELD_DATA_USE, HELD_LICENCE, HELD_PUBLIC, Outcome, check, fail, ok, warn)
from . import thresholds

_SPDX_LIKE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.+-]*$')
ACCESS_VALUES = ('internal', 'public')
DATA_USE_VALUES = ('open', 'controlled')


def _first(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ''
    return str(value or '').strip()


@check('access.level')
def access_level(ctx) -> Outcome:
    """``access`` is internal or public; public waits for an admin unless the site allows it."""
    access = _first(ctx.annotation.get('access')).lower()
    if access not in ACCESS_VALUES:
        return fail(f'access is {access!r}; it must be internal or public' if access else 'access is not set',
                    'Set access=internal (everyone who can log in) in the .h5ad.mus. Choose public only after '
                    'confirming licence and consent.')
    if access == 'public' and not ctx.settings.auto_publish_public:
        outcome = warn('access is public: an admin approves before publication',
                       'Nothing to fix; use access=internal to publish without waiting.', access=access)
        outcome.held.append(HELD_PUBLIC)
        return outcome
    return ok(f'access {access}', access=access)


@check('access.controlled')
def access_controlled(ctx) -> Outcome:
    """``data_use`` is open or controlled; controlled data, or a controlled cohort in the donor join, waits for an admin."""
    from obs_derive_forms import FORM_JOIN, derive_form  # noqa: PLC0415

    data_use = _first(ctx.annotation.get('data_use') or ctx.md.get('data_use')).lower()
    spec = (ctx.api.get('obs_derive') or {}).get('ant_donor_uid')
    cohort = str(spec.get('cohort') or '').strip().lower() if isinstance(spec, dict) and derive_form(spec) == FORM_JOIN else ''
    flagged_cohort = bool(cohort) and cohort in ctx.controlled_cohorts()
    if data_use and data_use not in DATA_USE_VALUES:
        return fail(f'data_use is {data_use!r}; it must be open or controlled',
                    'Set data_use=open or data_use=controlled in the .h5ad.mus.')
    if data_use == 'controlled':
        outcome = warn('data_use is controlled: an admin approves before publication',
                       'Nothing to fix; controlled-access data is never published automatically.', data_use=data_use)
        outcome.held.append(HELD_CONTROLLED)
        return outcome
    if flagged_cohort:
        outcome = warn('the donor ids join a cohort that the site marks as controlled-access while data_use is '
                       f'{data_use or "not set"}: an admin decides',
                       'Say data_use=controlled if the data carry a data use agreement.', data_use=data_use or None)
        outcome.held.append(HELD_CONTROLLED)
        outcome.admin_detail = {'cohort': cohort}
        return outcome
    if not data_use:
        outcome = warn('data_use is not set; an admin confirms the consent status before publication',
                       'Add data_use=open (no data use agreement) or data_use=controlled to the .h5ad.mus.')
        outcome.held.append(HELD_DATA_USE)
        return outcome
    return ok('data_use is open', data_use=data_use)


@check('access.licence')
def access_licence(ctx) -> Outcome:
    """``license`` is an SPDX-like id or ``in-house``; a missing or unknown licence waits for an admin."""
    licence = _first(ctx.annotation.get('license') or ctx.annotation.get('licence'))
    if licence.lower() in thresholds.LICENCE_UNKNOWN:
        outcome = warn('licence is ' + (f'{licence!r}' if licence else 'not set') + ': an admin decides',
                       'Set license= to an SPDX id (CC-BY-4.0, CC0-1.0, ...) or in-house in the .h5ad.mus.')
        outcome.held.append(HELD_LICENCE)
        return outcome
    if licence.lower() != 'in-house' and not _SPDX_LIKE.match(licence):
        return fail(f'licence {licence!r} is not an SPDX style id', 'Use an SPDX id such as CC-BY-4.0, CC0-1.0, '
                    'CC-BY-NC-4.0, or in-house.', licence=licence)
    return ok(f'licence {licence}', licence=licence)
