"""Who may see ``access: internal`` datasets.

A dataset is public only when its resolved annotation says ``access: public``.
Everything else (``internal``, a missing value, an unknown value) is internal.
When the site setting ``internal_requires_auth`` is on (the default, see
``config.get_internal_requires_auth``), ant-serve hides internal datasets from
callers without an authenticated identity: they vanish from every listing and
their routes answer 404, exactly as for a dataset the site does not have.

Who counts as authenticated (``caller_sees_internal``):

* a request with a non-empty ``Remote-User``. Only the proxy sets it (Caddy
  basic or Authelia, or nginx in trusted-proxy mode, all of which drop a
  client-sent copy); ant-browse and ant-mcp forward it on behalf of the
  signed-in user. On ``auth.mode: none`` nobody is authenticated, so
  ``Remote-User`` is ignored there.
* a local maintenance tool: loopback peer, no ``X-Forwarded-For``, no
  ``Remote-User`` (ant-sync, ant-refresh-views, ant-score, ...).
* a tool in another container that presents the internal secret without a
  user.

A request that carries ``X-Anthive-Anonymous`` is anonymous whatever else it
says: ant-browse and ant-mcp send it when they act for a visitor who is not
signed in, so a proxied anonymous request never passes for a local tool. The
header can only lower a caller's rights, so a client may send it freely.

Pure functions; ant-serve wires them into a request middleware and the
registry (``bin/api/ant-serve``). Input: request headers, peer host, dataset
metadata. Output: booleans and frozensets of db_ids.
"""
from __future__ import annotations

import contextvars
from typing import Any, Collection, Iterable, List, Mapping, Optional

ANONYMOUS_HEADER = 'X-Anthive-Anonymous'

# Query parameters that name one or more datasets (comma separated).
DATASET_QUERY_PARAMS = ('db_id', 'db_ids', 'dataset', 'datasets', 'db_x', 'db_y')

_LOOPBACK = ('127.0.0.1', '::1', 'localhost')

# The db_ids hidden from the request being served. Empty outside a request
# (startup, warm-up threads, CLI tools), so caches built there see everything.
HIDDEN_DB_IDS: contextvars.ContextVar = contextvars.ContextVar('anthive_hidden_db_ids', default=frozenset())


def current_hidden() -> frozenset:
    """The db_ids hidden from the current request (empty for authenticated callers)."""
    return HIDDEN_DB_IDS.get()


def visible(db_ids: Iterable[str]) -> List[str]:
    """``db_ids`` without the ones hidden from the current request (order kept)."""
    hidden = HIDDEN_DB_IDS.get()
    return [db_id for db_id in db_ids if db_id not in hidden]


def is_public(metadata: Optional[Mapping[str, Any]]) -> bool:
    """True only when the dataset's annotation says ``access: public``."""
    value = (metadata or {}).get('access')
    if isinstance(value, (list, tuple, set)):
        values = {str(item).strip().lower() for item in value}
        return values == {'public'}
    return str(value).strip().lower() == 'public' if value is not None else False


def hidden_db_ids(database_ids: Iterable[str],
                  metadata: Mapping[str, Mapping[str, Any]]) -> frozenset:
    """The served db_ids an anonymous caller must not see (every non-public one)."""
    return frozenset(db_id for db_id in database_ids if not is_public(metadata.get(db_id)))


def is_local_tool(headers: Mapping[str, str], peer_host: str) -> bool:
    """A direct call from a tool on this host: loopback peer, no forwarding
    header, no user, and not marked as acting for an anonymous visitor."""
    return (peer_host in _LOOPBACK
            and not headers.get('x-forwarded-for')
            and not (headers.get('remote-user') or '').strip()
            and not headers.get(ANONYMOUS_HEADER.lower()))


def caller_sees_internal(headers: Mapping[str, str], peer_host: str, *, requires_auth: bool,
                         auth_mode: Optional[str], has_internal_secret: bool) -> bool:
    """Whether this caller may see ``access: internal`` datasets.

    ``headers`` must be case-insensitive (Starlette's ``Headers``) or use
    lower-case keys.
    """
    if not requires_auth:
        return True
    if headers.get(ANONYMOUS_HEADER.lower()):
        return False
    user = (headers.get('remote-user') or '').strip()
    if user:
        return auth_mode != 'none'
    if is_local_tool(headers, peer_host):
        return True
    return has_internal_secret


def path_names_hidden(path: str, hidden: Collection[str]) -> Optional[str]:
    """The hidden db_id a ``/databases/{db_id}/...`` path names, else None."""
    prefix = '/databases/'
    if not hidden or not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    for db_id in hidden:
        if rest == db_id or rest.startswith(db_id + '/'):
            return db_id
    return None


def query_names_hidden(query: Mapping[str, Any], hidden: Collection[str]) -> Optional[str]:
    """The hidden db_id a dataset query parameter names, else None.

    ``query`` maps a parameter name to a value or a list of values
    (Starlette ``QueryParams.multi_items`` style is handled by the caller).
    """
    if not hidden:
        return None
    for name in DATASET_QUERY_PARAMS:
        raw = query.get(name)
        if raw is None:
            continue
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            for db_id in str(value).split(','):
                db_id = db_id.strip()
                if db_id in hidden:
                    return db_id
    return None
