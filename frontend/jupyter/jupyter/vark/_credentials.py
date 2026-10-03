"""Discover and reuse credentials stored by the Go ``vark`` CLI.

The Go ``vark`` CLI (https://codeberg.org/mfiers/vark) keeps its server
registry in ``~/.config/vark/config.yaml`` and per-server basic-auth
passwords in an age-encrypted file at ``~/.config/vark/secrets.age``
(decrypted with the X25519 identity in ``~/.config/vark/secrets.key``).

This module reads both, joins them, and lets ``AnthiveClient`` auto-pick
credentials based on the request's ``base_url`` so users with the Go CLI
already configured don't need to repeat passwords in Python.

Encryption / YAML parsing live behind optional imports — pyvark works
fine without ``pyrage`` / ``PyYAML`` installed; auto-discovery just
silently returns nothing in that case. Install the ``vark-config``
extras (``pip install pyvark[vark-config]``) to enable it.

Plaintext format inside ``secrets.age`` matches the Go CLI's writer in
``internal/secret/secret.go``: one ``name=value`` per line, with server
passwords stored under ``server:<server_name>`` keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Union
from urllib.parse import urlsplit, urlunsplit


__all__ = [
    'VarkServer',
    'load_vark_servers',
    'find_vark_credentials_for',
    'default_config_dir',
    'canonical_base_url',
    'VarkConfigError',
]


class VarkConfigError(RuntimeError):
    """Raised when vark-config discovery was explicitly requested but
    cannot be performed (missing extras, missing files, decryption
    failure, missing password for a named server)."""


class VarkServer(NamedTuple):
    """A single entry from the Go vark CLI's server registry."""

    name: str
    url: str
    user: str
    insecure: bool = False
    password: Optional[str] = None


# ── path helpers ──────────────────────────────────────────────────────────────


def default_config_dir() -> Path:
    """Return ``~/.config/vark/`` (no XDG_CONFIG_HOME logic — matches the
    Go CLI's default; users who relocate the dir pass ``config_dir=``
    explicitly)."""
    return Path.home() / '.config' / 'vark'


def canonical_base_url(url: str) -> str:
    """Canonicalise a base URL for fuzzy-matching against vark's registry.

    Lowercases scheme + host, drops a trailing slash on the path. Keeps
    the path itself (the ``/api`` prefix matters — see the Go vark
    CLAUDE.md note on the Authelia/Caddy gate).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip('/')
    return urlunsplit((scheme, netloc, path, '', ''))


# ── optional-extras gate ──────────────────────────────────────────────────────


def _import_extras(*, required: bool):
    """Import pyrage + yaml. When ``required`` is False, return
    ``(None, None)`` if either is missing (caller silently no-ops).
    When ``required`` is True, raise a helpful VarkConfigError.
    """
    try:
        import pyrage  # type: ignore
        import yaml  # type: ignore
    except ImportError as exc:
        if not required:
            return None, None
        raise VarkConfigError(
            "pyvark[vark-config] extras not installed — install with "
            "`pip install pyvark[vark-config]` to auto-discover vark "
            f"credentials (missing dependency: {exc.name})"
        ) from exc
    return pyrage, yaml


# ── core loaders ──────────────────────────────────────────────────────────────


def _parse_secrets_blob(plaintext: str) -> Dict[str, str]:
    """Parse the decrypted plaintext from ``secrets.age``.

    Format matches the Go writer in ``internal/secret/secret.go``: one
    ``key=value`` per line, blank lines skipped. The first ``=`` is the
    separator; passwords are not allowed to contain ``\\n``.
    """
    out: Dict[str, str] = {}
    for line in plaintext.splitlines():
        if not line:
            continue
        idx = line.find('=')
        if idx <= 0:
            continue
        out[line[:idx]] = line[idx + 1:]
    return out


def _read_secrets(
    config_dir: Path,
    *,
    required: bool,
) -> Dict[str, str]:
    """Decrypt ``<config_dir>/secrets.age`` with the identity in
    ``secrets.key`` and return ``{name: password}`` (raw keys, including
    the ``server:`` prefix that the Go CLI uses)."""
    pyrage, _ = _import_extras(required=required)
    if pyrage is None:
        return {}

    secrets_path = config_dir / 'secrets.age'
    key_path = config_dir / 'secrets.key'
    if not secrets_path.exists() or not key_path.exists():
        if required:
            raise VarkConfigError(
                f"vark secrets not found under {config_dir} "
                f"(expected secrets.age + secrets.key)"
            )
        return {}

    identity_text = key_path.read_text().strip()
    try:
        identity = pyrage.x25519.Identity.from_str(identity_text)
    except Exception as exc:
        raise VarkConfigError(
            f"could not parse age identity at {key_path}: {exc}"
        ) from exc

    ciphertext = secrets_path.read_bytes()
    try:
        plaintext = pyrage.decrypt(ciphertext, [identity])
    except Exception as exc:
        raise VarkConfigError(
            f"could not decrypt {secrets_path}: {exc}"
        ) from exc

    return _parse_secrets_blob(plaintext.decode('utf-8'))


def _read_servers(
    config_dir: Path,
    *,
    required: bool,
) -> List[Dict]:
    """Parse ``<config_dir>/config.yaml`` and return its ``servers:``
    list (or ``[]`` when the file is missing / extras unavailable)."""
    _, yaml = _import_extras(required=required)
    if yaml is None:
        return []

    config_path = config_dir / 'config.yaml'
    if not config_path.exists():
        if required:
            raise VarkConfigError(
                f"vark config not found at {config_path}"
            )
        return []

    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        raise VarkConfigError(
            f"could not parse {config_path}: {exc}"
        ) from exc

    servers = data.get('servers', []) if isinstance(data, dict) else []
    return servers if isinstance(servers, list) else []


def load_vark_servers(
    config_dir: Optional[Union[str, Path]] = None,
    *,
    required: bool = False,
) -> List[VarkServer]:
    """Read the Go vark CLI's config + secrets and return one
    :class:`VarkServer` per registered server.

    Args:
        config_dir: Override ``~/.config/vark/``. ``str`` is coerced
            to :class:`Path`.
        required: When True, missing files or missing extras raise
            :class:`VarkConfigError`. When False (default), they return
            an empty list — used by the implicit auto-discovery path.

    A ``VarkServer`` with ``password is None`` indicates the server is
    registered but has no password stored (e.g. anonymous endpoint, or
    Go CLI written before that login completed). Callers that need a
    real password should treat ``password is None`` as a miss.
    """
    if config_dir is None:
        config_dir = default_config_dir()
    else:
        config_dir = Path(config_dir)

    if not config_dir.exists():
        if required:
            raise VarkConfigError(
                f"vark config directory not found: {config_dir}"
            )
        return []

    raw_servers = _read_servers(config_dir, required=required)
    secrets = _read_secrets(config_dir, required=required)

    out: List[VarkServer] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        url = entry.get('url')
        if not name or not url:
            continue
        out.append(VarkServer(
            name=str(name),
            url=str(url),
            user=str(entry.get('user', '')),
            insecure=bool(entry.get('insecure', False)),
            password=secrets.get(f'server:{name}'),
        ))
    return out


def find_vark_credentials_for(
    base_url: str,
    *,
    config_dir: Optional[Union[str, Path]] = None,
    required: bool = False,
) -> Optional[VarkServer]:
    """Find the registered vark server whose URL matches ``base_url``.

    Matching uses :func:`canonical_base_url` (scheme + host lowercased,
    trailing slash stripped). Returns the matching :class:`VarkServer`
    (with ``password`` populated when one is stored), or ``None`` when
    no server matches or no password is available.

    Args:
        base_url: The URL the client is about to talk to.
        config_dir: Override ``~/.config/vark/``.
        required: When True, raise :class:`VarkConfigError` instead of
            returning ``None`` if no match (or no password) is found.
            Used by the explicit ``vark_config=True`` constructor path
            and by :meth:`AnthiveClient.from_vark_server`.
    """
    servers = load_vark_servers(config_dir=config_dir, required=required)
    target = canonical_base_url(base_url)
    for server in servers:
        if canonical_base_url(server.url) == target:
            if server.password is None:
                if required:
                    raise VarkConfigError(
                        f"vark server '{server.name}' matches {base_url!r} "
                        f"but has no stored password"
                    )
                return None
            return server
    if required:
        raise VarkConfigError(
            f"no vark server matches base_url={base_url!r} "
            f"(checked {len(servers)} registered server(s))"
        )
    return None


def find_vark_server_by_name(
    name: str,
    *,
    config_dir: Optional[Union[str, Path]] = None,
) -> VarkServer:
    """Look up a vark server by its registry name. Always raises
    :class:`VarkConfigError` on a miss — this helper is only used by
    explicit-opt-in callers (:meth:`AnthiveClient.from_vark_server`)."""
    servers = load_vark_servers(config_dir=config_dir, required=True)
    for server in servers:
        if server.name == name:
            if server.password is None:
                raise VarkConfigError(
                    f"vark server {name!r} has no stored password"
                )
            return server
    available = ', '.join(repr(s.name) for s in servers) or '(none)'
    raise VarkConfigError(
        f"no vark server named {name!r} (registered: {available})"
    )
