"""Access checks: level, data use and licence. Level and licence decide whether a pass can publish by itself.

Data use agreements and the internal/external split apply to a whole server
(decision D7, revised 2026-10-20): everybody on a server may see all its data,
so ``data_use`` (controlled, missing, or a controlled cohort in the donor join)
only warns the depositor and never holds a deposit.
"""
from __future__ import annotations

import re

from . import HELD_LICENCE, HELD_PUBLIC, Outcome, check, fail, ok, warn
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


DATA_USE_FIX = ('Nothing to fix if the users of this server are covered. Data use agreements apply to a whole '
                'server: every user with access to it can see every dataset.')


@check('access.controlled')
def access_controlled(ctx) -> Outcome:
    """``data_use`` is open or controlled. Controlled data, a controlled cohort in the donor join or a missing
    ``data_use`` warn (everyone on the server sees every dataset); they never hold a deposit."""
    from obs_derive_forms import FORM_JOIN, derive_form  # noqa: PLC0415

    data_use = _first(ctx.annotation.get('data_use') or ctx.md.get('data_use')).lower()
    spec = (ctx.api.get('obs_derive') or {}).get('ant_donor_uid')
    cohort = str(spec.get('cohort') or '').strip().lower() if isinstance(spec, dict) and derive_form(spec) == FORM_JOIN else ''
    flagged_cohort = bool(cohort) and cohort in ctx.controlled_cohorts()
    if data_use and data_use not in DATA_USE_VALUES:
        return fail(f'data_use is {data_use!r}; it must be open or controlled',
                    'Set data_use=open or data_use=controlled in the .h5ad.mus.')
    if data_use == 'controlled':
        outcome = warn('data_use is controlled: everyone with access to this server can see this dataset; make '
                       "sure the server's users are covered by the data use agreement", DATA_USE_FIX,
                       data_use=data_use)
        if flagged_cohort:
            outcome.admin_detail = {'cohort': cohort}
        return outcome
    if flagged_cohort:
        outcome = warn('the donor ids join a cohort that this server marks as controlled-access while data_use is '
                       f'{data_use or "not set"}: everyone with access to this server can see this dataset; make '
                       "sure the server's users are covered by the cohort's data use agreement",
                       'Say data_use=controlled if the data carry a data use agreement. ' + DATA_USE_FIX,
                       data_use=data_use or None)
        outcome.admin_detail = {'cohort': cohort}
        return outcome
    if not data_use:
        return warn('data_use is not set: say whether the data carry a data use agreement (everyone with access to '
                    'this server can see this dataset)',
                    'Add data_use=open (no data use agreement) or data_use=controlled to the .h5ad.mus.')
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
    if licence.lower() != 'in-house' and not _SPDX_LIKE.fullmatch(licence):
        return fail(f'licence {licence!r} is not an SPDX style id', 'Use an SPDX id such as CC-BY-4.0, CC0-1.0, '
                    'CC-BY-NC-4.0, or in-house.', licence=licence)
    return ok(f'licence {licence}', licence=licence)
