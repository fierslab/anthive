"""
Configuration management for anthive

Loads configuration from anthive.yaml or uses defaults from anthive.default.yaml
"""

import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Configuration-related errors"""
    pass


def get_anthive_root() -> Path:
    """
    Find the anthive root directory (where this script is located)

    Returns:
        Path to anthive root directory
    """
    # Get the directory containing this file, then go up to root
    return Path(__file__).parent.parent.resolve()


def get_user_config_path() -> Path:
    """Path of the site config: ``$ANTHIVE_CONFIG`` or ``<root>/anthive.yaml``.

    Containers mount a rendered config read-only outside the image and
    point ANTHIVE_CONFIG at it, so the image never carries secrets.
    """
    override = os.environ.get('ANTHIVE_CONFIG', '').strip()
    if override:
        return Path(override).expanduser()
    return get_anthive_root() / "anthive.yaml"


def load_default_config() -> Dict[str, Any]:
    """
    Load default configuration from anthive.default.yaml

    Returns:
        Dictionary with default configuration values

    Raises:
        ConfigError: If default config file cannot be loaded
    """
    anthive_root = get_anthive_root()
    default_config_path = anthive_root / "anthive.default.yaml"

    if not default_config_path.exists():
        raise ConfigError(
            f"Default configuration file not found: {default_config_path}\n"
            "This is a critical error - anthive installation may be corrupted."
        )

    try:
        with open(default_config_path, 'r') as file:
            config = yaml.safe_load(file)
            if config is None:
                config = {}
            return config
    except yaml.YAMLError as error:
        raise ConfigError(
            f"Failed to parse default configuration file: {default_config_path}\n"
            f"Error: {error}"
        )


def load_user_config() -> Dict[str, Any]:
    """
    Load user configuration from anthive.yaml if it exists

    Returns:
        Dictionary with user configuration values, or empty dict if file doesn't exist

    Raises:
        ConfigError: If config file exists but cannot be parsed
    """
    user_config_path = get_user_config_path()

    if not user_config_path.exists():
        return {}

    try:
        with open(user_config_path, 'r') as file:
            config = yaml.safe_load(file)
            if config is None:
                config = {}
            return config
    except yaml.YAMLError as error:
        raise ConfigError(
            f"ERROR: [Config] Failed to parse configuration file\n"
            f"Details: Invalid YAML syntax in {user_config_path}\n"
            f"Location: {user_config_path}\n"
            f"Error: {error}\n"
            f"Action: Fix YAML syntax or remove invalid configuration"
        )


_WARNED_UNKNOWN: set = set()
_UNKNOWN_KEYS_LENIENT = {'on': False}

# Validation rule (2.46.1):
#   * host tools (ant-sync, ant-stack, ant-ingest, ant-ingest-watch, ...): every unknown key is an error,
#     at the top level and inside the critical blocks below.
#   * servers (ant-serve, ant-browse, ant-mcp; ``set_unknown_keys_lenient()``): unknown keys, also unknown
#     sub-keys of the critical blocks, are warnings, so a container of release N never crash-loops on a
#     config rendered for N+1 (which may add keys).
#   * everywhere, lenient or not: a wrong type or value of a known security/sync key (sync_policy.role,
#     internal_requires_auth, datasets_upload_groups, a critical block that is not a mapping) and an
#     unknown top-level key that looks like a critical one (``internal_require_auth``) are errors: there a
#     silent fallback would change who sees what or what a sync deletes.
# Keys whose mistakes change who sees what or what a sync deletes:
CRITICAL_KEYS = ('sync_policy', 'auth', 'internal_requires_auth', 'datasets_upload_groups', 's3',
                 'artefacts_upload_groups', 'collections_create_groups')
_CRITICAL_BLOCKS = ('sync_policy', 'auth', 's3')


def set_unknown_keys_lenient(lenient: bool = True) -> None:
    """Long-running servers (ant-serve, ant-browse, ant-mcp) call this before loading the config: an
    unknown top-level key is then a warning, so a container of release N never crash-loops on a config
    rendered for N+1 (2.46.1). Host tools stay strict: a typo there is an error."""
    _UNKNOWN_KEYS_LENIENT['on'] = bool(lenient)


def _critical_problems(config: Dict[str, Any], defaults: Dict[str, Any], unknown: set,
                       sub_key_warnings: Optional[List[str]] = None) -> List[str]:
    """Errors in the critical keys. Unknown sub-keys of the critical blocks go to ``sub_key_warnings``
    when that list is given (servers), else they are errors too (host tools)."""
    import difflib  # noqa: PLC0415
    problems = []
    for key in sorted(unknown):
        close = difflib.get_close_matches(key, CRITICAL_KEYS, n=1, cutoff=0.8)
        if close:
            problems.append(f"unknown key {key!r} (did you mean {close[0]!r}?)")
    for block in _CRITICAL_BLOCKS:
        if block not in config or config[block] is None:
            continue
        if not isinstance(config[block], dict):
            problems.append(f"{block} must be a mapping")
            continue
        extra = set(config[block]) - set(defaults.get(block) or {})
        if extra:
            message = f"unknown key(s) in {block}: {', '.join(sorted(extra))}"
            (sub_key_warnings if sub_key_warnings is not None else problems).append(message)
    role = (config.get('sync_policy') or {}).get('role') if isinstance(config.get('sync_policy'), dict) else None
    if role is not None and str(role).strip().lower() not in ('primary', 'slave'):
        problems.append(f"sync_policy.role must be primary or slave, not {role!r}")
    if 'internal_requires_auth' in config and not isinstance(config['internal_requires_auth'], bool):
        problems.append(f"internal_requires_auth must be true or false, not {config['internal_requires_auth']!r}")
    for key in ('datasets_upload_groups', 'artefacts_upload_groups', 'collections_create_groups'):
        groups = config.get(key)
        if groups is not None and (not isinstance(groups, list) or not all(isinstance(g, str) for g in groups)):
            problems.append(f"{key} must be a list of group names")
    return problems


def validate_config(config: Dict[str, Any], defaults: Dict[str, Any], strict: Optional[bool] = None) -> None:
    """
    Validate configuration values

    Unknown top-level keys are an error, except in a process that called
    ``set_unknown_keys_lenient()`` (the api, web and mcp servers) or runs
    with ``ANTHIVE_CONFIG_LENIENT=1``: there they are a warning printed once,
    so mixed releases cannot crash-loop. ``ANTHIVE_CONFIG_STRICT=1`` (set by
    ``ant-stack`` when it validates a render) always makes them an error.
    ``CRITICAL_KEYS`` are checked strictly everywhere.

    Args:
        config: User configuration to validate
        defaults: Default configuration for reference
        strict: Raise on unknown keys (default: from the process setting and environment)

    Raises:
        ConfigError: If configuration is invalid
    """
    if strict is None:
        if os.environ.get('ANTHIVE_CONFIG_STRICT', '').strip() in ('1', 'true', 'yes'):
            strict = True
        else:
            strict = not (_UNKNOWN_KEYS_LENIENT['on']
                          or os.environ.get('ANTHIVE_CONFIG_LENIENT', '').strip() in ('1', 'true', 'yes'))
    unknown_params = set(config.keys()) - set(defaults.keys())
    sub_key_warnings: List[str] = []
    critical = _critical_problems(config, defaults, unknown_params, None if strict else sub_key_warnings)
    for message in sub_key_warnings:
        if message not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(message)
            text = f"WARNING: [Config] ignoring {message} (a config written for another release?)"
            print(text, file=sys.stderr)
            logging.getLogger('anthive.config').warning(text)
    if critical:
        raise ConfigError(
            f"ERROR: [Config] Invalid security- or sync-relevant configuration\n"
            f"Details: {'; '.join(critical)}\n"
            f"Location: anthive.yaml\n"
            f"Action: fix these keys (they decide access and what a sync may delete; checked in every process)"
        )
    if unknown_params and strict:
        raise ConfigError(
            f"ERROR: [Config] Unknown configuration parameters\n"
            f"Details: {', '.join(sorted(unknown_params))}\n"
            f"Location: anthive.yaml\n"
            f"Action: Remove unknown parameters or check anthive.default.yaml for valid options"
        )
    if unknown_params:
        key = tuple(sorted(unknown_params))
        if key not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(key)
            message = (f"WARNING: [Config] ignoring unknown configuration parameters: {', '.join(key)} "
                       f"(a config written for another release? check anthive.default.yaml)")
            print(message, file=sys.stderr)
            logging.getLogger('anthive.config').warning(message)

    # Validate specific parameters
    if 'max_obscat_values' in config:
        value = config['max_obscat_values']
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(
                f"ERROR: [Config] Invalid value for max_obscat_values\n"
                f"Details: Expected positive integer, got {value} ({type(value).__name__})\n"
                f"Location: anthive.yaml\n"
                f"Action: Set to a positive integer (e.g., 50)"
            )

    if 'melt_chunk_size' in config:
        value = config['melt_chunk_size']
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(
                f"ERROR: [Config] Invalid value for melt_chunk_size\n"
                f"Details: Expected positive integer, got {value} ({type(value).__name__})\n"
                f"Location: anthive.yaml\n"
                f"Action: Set to a positive integer (e.g., 10000)"
            )

    if 'rest_api_port' in config:
        value = config['rest_api_port']
        if not isinstance(value, int) or value <= 0 or value > 65535:
            raise ConfigError(
                f"ERROR: [Config] Invalid value for rest_api_port\n"
                f"Details: Expected port number 1-65535, got {value}\n"
                f"Location: anthive.yaml\n"
                f"Action: Set to a valid port number (e.g., 8080)"
            )


def load_config() -> Dict[str, Any]:
    """
    Load configuration (user config merged with defaults)

    Returns:
        Dictionary with merged configuration values

    Raises:
        ConfigError: If configuration cannot be loaded or is invalid
    """
    # Load defaults
    defaults = load_default_config()

    # Load user config
    user_config = load_user_config()

    # Validate user config
    if user_config:
        validate_config(user_config, defaults)

    # Merge (user config overrides defaults)
    config = defaults.copy()
    config.update(user_config)

    # Deep-merge nested sub-dicts so partial user overrides don't drop defaults
    # (e.g. user yaml sets chat.api_key + chat.enabled; the allowed_models /
    # provider_order / base_url / history_path defaults from
    # anthive.default.yaml would otherwise be silently dropped by a shallow
    # merge).
    for subkey in ('s3', 'mcp', 'chat', 'sync_policy', 'auth', 'ingest_watch'):
        if isinstance(user_config.get(subkey), dict):
            merged = defaults.get(subkey, {}).copy()
            merged.update(user_config[subkey])
            config[subkey] = merged

    return config


def is_slave(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True iff anthive.yaml declares this deployment as a slave mirror.

    Slave mode is OPT-IN ONLY — an unset / null / missing role is
    treated as primary. There is no auto-detection. See
    anthive.default.yaml's sync_policy.role block for the contract.

    Importers should call this at the top of any operation that
    WRITES (sync-push, AnnotationWriter, ant-priority set, ingest
    pipelines, etc.) and refuse with a clear error.
    """
    cfg = cfg if cfg is not None else load_config()
    role = (cfg.get('sync_policy') or {}).get('role')
    return str(role or '').strip().lower() == 'slave'


def require_primary(cfg: Optional[Dict[str, Any]] = None,
                    tool: str = 'this tool') -> None:
    """Exit-2 with a clear error message when running under slave mode.

    Centralised guard for every write-path entry point. ``tool`` is
    the name printed in the refusal message — pass the script's name
    (e.g. 'ant-score') so the user sees which command refused.
    """
    if is_slave(cfg):
        msg = (
            f"{tool}: refusing to write — this deployment is configured "
            f"as a slave (sync_policy.role=slave in anthive.yaml). "
            f"Edit on the primary deployment instead; any change here "
            f"would be overwritten on the next sync-pull."
        )
        print(msg, file=sys.stderr)
        sys.exit(2)


def get_config() -> Dict[str, Any]:
    """
    Get configuration, exiting with error message if loading fails

    This is the main entry point for getting configuration in commands.
    It handles errors gracefully and exits with appropriate error code.

    Returns:
        Dictionary with configuration values
    """
    try:
        return load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


# ── Resolved paths and runtime switches ─────────────────────────────────────
#
# Every location a deployment may need to move (containers, HPC scratch,
# multi-instance hosts) resolves here, in one precedence order:
#   1. environment variable (ANTHIVE_<NAME>), for containers and batch jobs
#   2. anthive.yaml key
#   3. built-in default relative to the repo root (the historical layout)
# Values may contain $VARS and ~ (e.g. convert_staging_dir: $TMPDIR).

_DEFAULTED_VAR = re.compile(r'\$\{(\w+):-([^}]*)\}')


def expand_value(value: Any) -> str:
    """Expand ``${VAR:-default}``, ``$VAR``/``${VAR}`` and ``~`` at use time.

    Values stay unexpanded in config files and container images so they pick
    up the environment of the process that runs (a batch job's $TMPDIR, not
    the one present when an image or config was built). ``UID`` falls back to
    the numeric user id, since shells do not export it.
    """
    text = str(value)

    def _defaulted(match: 're.Match[str]') -> str:
        name, default = match.group(1), match.group(2)
        found = os.environ.get(name) or (str(os.getuid()) if name == 'UID' else '')
        return found or default

    text = _DEFAULTED_VAR.sub(_defaulted, text)
    if '$UID' in text or '${UID}' in text:
        text = text.replace('${UID}', os.environ.get('UID', str(os.getuid())))
        text = text.replace('$UID', os.environ.get('UID', str(os.getuid())))
    return os.path.expanduser(os.path.expandvars(text))


def resolve_path(value: Any, base: Optional[Path] = None) -> Path:
    """Expand variables/``~`` and anchor a relative path at the repo root."""
    candidate = Path(expand_value(value))
    if not candidate.is_absolute():
        candidate = (base or get_anthive_root()) / candidate
    return candidate


def _setting(cfg: Optional[Dict[str, Any]], key: str, env: str, default: Any) -> Any:
    env_value = os.environ.get(env, '').strip()
    if env_value:
        return env_value
    cfg = cfg if cfg is not None else load_config()
    value = cfg.get(key)
    return default if value in (None, '') else value


def get_state_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Persistent per-site state: SQLite caches, MCP tokens, click history."""
    path = resolve_path(_setting(cfg, 'state_dir', 'ANTHIVE_STATE_DIR', 'var'))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_tmp_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Disposable scratch: metadata cache, analytics sessions, job logs."""
    path = resolve_path(_setting(cfg, 'tmp_dir', 'ANTHIVE_TMP_DIR', 'tmp'))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Where background jobs write their log files (default: the tmp dir)."""
    configured = _setting(cfg, 'log_dir', 'ANTHIVE_LOG_DIR', None)
    path = resolve_path(configured) if configured else get_tmp_dir(cfg, create=create)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


LOG_STDERR_LEVELS = ('debug', 'info', 'warning', 'error')


def get_log_stderr_level(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Lowest ant-serve log level copied to stderr (``log_stderr_level``, default warning)."""
    level = str(_setting(cfg, 'log_stderr_level', 'ANTHIVE_LOG_STDERR_LEVEL', 'warning') or 'warning').strip().lower()
    if level not in LOG_STDERR_LEVELS:
        raise ConfigError(
            f"ERROR: [Config] Invalid log_stderr_level {level!r}; expected {', '.join(LOG_STDERR_LEVELS)}"
        )
    return level


def get_data_folder(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """The h5adstore root."""
    return resolve_path(_setting(cfg, 'h5adstore', 'ANTHIVE_H5ADSTORE', 'data/h5adstore'))


def get_store_file(name: str, cfg: Optional[Dict[str, Any]] = None) -> Path:
    """A shared table that lives in the h5adstore root, e.g. ``donor_crosswalk.tsv``.

    ``name`` is relative to the store root (``get_data_folder``). Absolute
    paths and ``..`` components are refused (ValueError), so an ``.md`` can
    only ever name a file inside the store. The file may not exist; callers
    check.
    """
    text = str(name or '').strip()
    candidate = Path(text)
    if not text or candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError(f'store file must be a relative path inside the store: {name!r}')
    return get_data_folder(cfg) / candidate


def get_facet_results_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Folder with the donor-level facet test results (read-only for the server).

    ``facet_results_dir`` / env ``ANTHIVE_FACET_RESULTS_DIR``; empty means
    ``<h5adstore>/_facet_tests``, where ant-facet-test writes them.
    """
    cfg = cfg if cfg is not None else load_config()
    value = _setting(cfg, 'facet_results_dir', 'ANTHIVE_FACET_RESULTS_DIR', '')
    if value in (None, ''):
        return get_data_folder(cfg) / '_facet_tests'
    return resolve_path(value)


def get_library_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """The paper library root (its own repository; see doc/LIBRARY.md)."""
    return resolve_path(_setting(cfg, 'library', 'ANTHIVE_LIBRARY', 'data/library'))


def get_library_lab_authors(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Author names that send new papers to papers/lab/ (library_lab_authors)."""
    cfg = cfg if cfg is not None else get_config()
    value = cfg.get('library_lab_authors')
    if value is None:
        return ['De Strooper']
    return [str(v) for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


def get_library_upload_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paper upload and library sync switches (see doc/LIBRARY.md):
    upload_groups (who may upload; default admins), max_pdf_mb, git committer
    identity, whether to push the library repo after a commit, whether to push
    an uploaded PDF to S3 at once, and whether this site publishes the
    literature index to S3."""
    cfg = cfg if cfg is not None else get_config()
    groups = cfg.get('library_upload_groups')
    if groups is None:
        groups = ['admins']
    elif not isinstance(groups, list):
        groups = [groups]
    try:
        max_mb = int(cfg.get('library_upload_max_pdf_mb') or 25)
    except (TypeError, ValueError):
        max_mb = 25
    return {
        'upload_groups': [str(g).strip().lower() for g in groups if str(g).strip()],
        'max_pdf_mb': max_mb,
        'git_committer': str(cfg.get('library_git_committer') or 'anthive <noreply@anthive.local>'),
        'git_push': bool(cfg.get('library_git_push', False)),
        's3_autopush': bool(cfg.get('library_s3_autopush', False)),
        'index_publisher': bool(cfg.get('library_index_publisher', False)),
        'index_pull': bool(cfg.get('library_index_pull', True)),
    }


def get_library_index_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Folder holding library_chunks.parquet (see doc/LIBRARY_INDEX.md)."""
    return resolve_path(_setting(cfg, 'library_index', 'ANTHIVE_LIBRARY_INDEX', 'data/library_index'))


def get_ingest_state_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """State of dataset ingestion (submissions, reports, overlap indexes).

    Key ``ingest_state_dir`` / env ``ANTHIVE_INGEST_STATE_DIR``, default
    ``<state_dir>/ingest``. Never inside the repo checkout or the store.
    """
    configured = _setting(cfg, 'ingest_state_dir', 'ANTHIVE_INGEST_STATE_DIR', None)
    path = resolve_path(configured) if configured else get_state_dir(cfg, create=create) / 'ingest'
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_ingest_index_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Overlap indexes of ``ant ingest-index`` (donor and cell-barcode digests).

    Key ``ingest_index_dir`` / env ``ANTHIVE_INGEST_INDEX_DIR``, default
    ``<ingest_state_dir>/index``. The files hold quasi-identifiers (donor
    demographics as digests plus values), so they stay out of the store.
    """
    configured = _setting(cfg, 'ingest_index_dir', 'ANTHIVE_INGEST_INDEX_DIR', None)
    path = resolve_path(configured) if configured else get_ingest_state_dir(cfg, create=create) / 'index'
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_ingest_scratch_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Scratch space of ``ant ingest-check`` (hard-linked h5ad, scratch converts): ``<tmp_dir>/ingest_check``."""
    path = get_tmp_dir(cfg, create=create) / 'ingest_check'
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_ingest_work_dir(cfg: Optional[Dict[str, Any]] = None, create: bool = True) -> Path:
    """Working space of ``ant-ingest`` and ``ant-ingest-watch``: pulled uploads and staged datasets.

    Key ``ingest_work_dir`` / env ``ANTHIVE_INGEST_WORK_DIR``, default
    ``<tmp_dir>/ingest_work``. Never inside the store: the store only receives
    finished files, each placed with an atomic rename.
    """
    configured = _setting(cfg, 'ingest_work_dir', 'ANTHIVE_INGEST_WORK_DIR', None)
    path = resolve_path(configured) if configured else get_tmp_dir(cfg, create=create) / 'ingest_work'
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


INGEST_WATCH_DEFAULTS: Dict[str, Any] = {
    'enabled': False,
    'interval_seconds': 300,
    'settle_seconds': 120,
    'prefix': '__incoming/',
    'catalog_prefix': '',
    'post_steps': ['score', 'gene_stats', 'module_variability', 'facet_test', 'facet_test_modules',
                   'ingest_index', 'manifest'],
    'post_step_timeout_seconds': 21600,
    'store_git_commit': True,
    'store_git_push': False,
    'git_identity': 'anthive ingest-watch <noreply@anthive.local>',
    'delete_incoming_after_ingest': True,
    'delete_despite_post_warnings': True,
    'api_url': '',
    'low_priority': True,
    'require_update_marker': False,
    'min_free_gb': 50,
    'work_ttl_days': 14,
    'allow_repo_dirs': False,
    'accept_unlinked': True,
    'incoming_bucket': '',
    'sites_dir': '',
    'trusted_admin_sites': [],
    'keep_local_versions': True,
    'merge_window': '01:00-06:00',
    'merge_max_load': 8.0,
    'merge_min_free_gb': 40.0,
    'copy_mode': 'auto',
    'incoming_s3': {},          # read by get_incoming_s3 only (a key pair never enters the settings dict)
    'notify': {'on': ['live', 'held', 'failed', 'needs_decision'], 'webhook_url': '', 'email_to': '',
               'email_from': 'anthive-ingest@localhost', 'smtp_host': '', 'smtp_port': 25},
}


def _coerce_like(default: Any, value: Any, key: str) -> Any:
    """Convert an env or yaml value to the type of its default (bool, int, list, str)."""
    if isinstance(default, bool):
        return _flag(value, default)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f'ERROR: [Config] ingest_watch.{key} must be an integer, got {value!r}')
    if isinstance(default, list):
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return [str(item) for item in (value or [])]
    return '' if value is None else str(value)


def get_ingest_watch_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Settings of ``ant-ingest-watch`` (the S3 watch route).

    Precedence per key: env ``ANTHIVE_INGEST_WATCH_<KEY>`` (``notify`` keys:
    ``ANTHIVE_INGEST_WATCH_NOTIFY_<KEY>``) > ``ingest_watch:`` in anthive.yaml
    > :data:`INGEST_WATCH_DEFAULTS`. The prefixes always end in ``/`` (an
    empty catalog prefix stays empty).
    """
    cfg = cfg if cfg is not None else get_config()
    block = cfg.get('ingest_watch') or {}
    settings: Dict[str, Any] = {}
    for key, default in INGEST_WATCH_DEFAULTS.items():
        if key in ('notify', 'incoming_s3'):
            continue
        env_value = os.environ.get(f'ANTHIVE_INGEST_WATCH_{key.upper()}', '').strip()
        raw = env_value if env_value else block.get(key, default)
        settings[key] = _coerce_like(default, raw, key)
    notify_block = dict(block.get('notify') or {})
    notify: Dict[str, Any] = {}
    for key, default in INGEST_WATCH_DEFAULTS['notify'].items():
        env_value = os.environ.get(f'ANTHIVE_INGEST_WATCH_NOTIFY_{key.upper()}', '').strip()
        raw = env_value if env_value else notify_block.get(key, default)
        notify[key] = _coerce_like(default, raw, f'notify.{key}')
    settings['notify'] = notify
    if settings['interval_seconds'] < 10:
        raise ConfigError('ERROR: [Config] ingest_watch.interval_seconds must be at least 10')
    for key in ('prefix', 'catalog_prefix'):
        value = settings[key].strip().lstrip('/')
        settings[key] = (value.rstrip('/') + '/') if value else ''
    if not settings['prefix']:
        raise ConfigError('ERROR: [Config] ingest_watch.prefix must not be empty')
    if settings['copy_mode'] not in ('auto', 'server', 'stream'):
        raise ConfigError("ERROR: [Config] ingest_watch.copy_mode must be 'auto', 'server' or 'stream'")
    return settings


INCOMING_S3_FIELDS = ('endpoint', 'region', 'bucket', 'access_key_id', 'secret_access_key')
INCOMING_S3_SECRET_FILES = {'access_key_id': 'incoming_s3_access_key_id',
                            'secret_access_key': 'incoming_s3_secret_access_key'}


def get_incoming_s3(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The ingestion bucket's own key pair of a central watcher (``ingest_watch.incoming_s3``, design 24.9).

    Per field: env ``ANTHIVE_INGEST_WATCH_INCOMING_S3_<FIELD>`` > for the key pair the secrets files
    ``<ingest_state_dir>/secrets/incoming_s3_access_key_id`` and ``incoming_s3_secret_access_key`` > the block
    in anthive.yaml (keep keys out of the config text: use the files) > endpoint and region of the ``s3`` block,
    bucket ``ingest_watch.incoming_bucket``. None without a key pair: then the ``s3`` keys reach both buckets
    (one client, server-side copies).

    Raises:
        ConfigError: a key pair without a bucket, or half a key pair.
    """
    cfg = cfg if cfg is not None else get_config()
    block = dict((cfg.get('ingest_watch') or {}).get('incoming_s3') or {})
    base = dict(cfg.get('s3') or {})
    secrets_dir = get_ingest_state_dir(cfg, create=False) / 'secrets'
    out: Dict[str, Any] = {}
    for field in INCOMING_S3_FIELDS:
        value = os.environ.get(f'ANTHIVE_INGEST_WATCH_INCOMING_S3_{field.upper()}', '').strip()
        if not value and field in INCOMING_S3_SECRET_FILES:
            path = secrets_dir / INCOMING_S3_SECRET_FILES[field]
            try:
                value = path.read_text(encoding='utf-8').strip() if path.is_file() else ''
            except OSError:
                value = ''
        if not value:
            value = str(block.get(field) or '').strip()
        if not value and field in ('endpoint', 'region'):
            value = str(base.get(field) or '').strip()
        if not value and field == 'bucket':
            value = str((cfg.get('ingest_watch') or {}).get('incoming_bucket') or '').strip()
        out[field] = value
    if not out['access_key_id'] and not out['secret_access_key']:
        return None
    if not out['access_key_id'] or not out['secret_access_key']:
        raise ConfigError('ERROR: [Config] ingest_watch.incoming_s3 needs both access_key_id and secret_access_key')
    if not out['bucket']:
        raise ConfigError('ERROR: [Config] ingest_watch.incoming_s3 has a key pair but no bucket '
                          '(incoming_s3.bucket or ingest_watch.incoming_bucket)')
    return out


def get_dataset_ingest_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dataset ingestion switches that ``ant ingest-check`` reads
    (doc/DESIGN_dataset_ingestion.md, section 12): the upload size limit,
    whether ``access=public`` may publish without an admin, and the access
    level new datasets default to."""
    cfg = cfg if cfg is not None else get_config()
    try:
        max_gb = float(cfg.get('datasets_upload_max_gb') or 200)
    except (TypeError, ValueError):
        max_gb = 200.0
    return {
        'max_upload_gb': max_gb,
        'auto_publish_public': bool(cfg.get('datasets_auto_publish_public', False)),
        'default_access': str(cfg.get('datasets_default_access') or 'internal'),
    }


def _watch_catalog_prefix(cfg: Dict[str, Any]) -> str:
    try:
        return get_ingest_watch_settings(cfg)['catalog_prefix']
    except ConfigError:
        return ''


def deposit_site_id(cfg: Optional[Dict[str, Any]] = None) -> str:
    """This install's folder below the deposit staging prefix (``datasets_deposit_site_id``, env
    ``ANTHIVE_DATASETS_DEPOSIT_SITE_ID``, default the host name). Two installs sharing a bucket must differ:
    each one sweeps only its own staging folder."""
    import re
    import socket

    configured = str(_setting(cfg, 'datasets_deposit_site_id', 'ANTHIVE_DATASETS_DEPOSIT_SITE_ID', '') or '')
    raw = configured or socket.gethostname().split('.')[0] or 'site'
    site_id = re.sub(r'[^A-Za-z0-9._-]+', '-', raw).strip('.-_').lower()[:64]
    if not site_id:
        raise ConfigError(f'ERROR: [Config] datasets_deposit_site_id {raw!r} is not usable as a folder name')
    return site_id


def get_dataset_deposit_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dataset deposits through the API (doc/DESIGN_dataset_ingestion.md, section 22).

    Who may deposit (``datasets_upload_groups``; basic auth mode: ``auth.admins``
    count as admins), whether deposits are on (``datasets_deposit_enabled``,
    env ``ANTHIVE_DATASETS_DEPOSIT_ENABLED``), the limits, the incoming prefix
    (the S3 watch route's ``ingest_watch.prefix``) and the S3 credentials used
    for presigning: ``datasets_deposit_s3`` when it holds a key pair, else the
    ``s3`` block. The credentials never leave the server.
    """
    cfg = cfg if cfg is not None else get_config()
    groups = cfg.get('datasets_upload_groups')
    if groups is None:
        groups = ['admins', 'curators']
    elif not isinstance(groups, list):
        groups = [groups]

    def number(key: str, default: float, cast=float) -> Any:
        try:
            value = cfg.get(key)
            return cast(default if value is None or value == '' else value)
        except (TypeError, ValueError):
            raise ConfigError(f'ERROR: [Config] {key} must be a number, got {cfg.get(key)!r}')

    base_s3 = dict(cfg.get('s3') or {})
    deposit_s3 = dict(cfg.get('datasets_deposit_s3') or {})
    if deposit_s3.get('access_key_id') and deposit_s3.get('secret_access_key'):
        s3 = {**{k: base_s3.get(k) for k in ('endpoint', 'region', 'bucket', 'use_path_style')}, **deposit_s3}
        credential_source = 'datasets_deposit_s3'
    else:
        s3 = base_s3
        credential_source = 's3'
    policy = str(cfg.get('datasets_deposit_update_policy') or 'own').strip().lower()
    if policy not in ('own', 'any'):
        raise ConfigError("ERROR: [Config] datasets_deposit_update_policy must be 'own' or 'any'")
    try:
        watch = get_ingest_watch_settings(cfg)
        prefix, notify = watch['prefix'], watch['notify']
    except ConfigError:
        prefix, notify = '__incoming/', {}
    central = _flag(_setting(cfg, 'datasets_deposit_central_watcher', 'ANTHIVE_DATASETS_DEPOSIT_CENTRAL_WATCHER',
                             False), False)
    return {
        'upload_groups': [str(g).strip().lower() for g in groups if str(g).strip()],
        'enabled': _flag(_setting(cfg, 'datasets_deposit_enabled', 'ANTHIVE_DATASETS_DEPOSIT_ENABLED', False), False),
        'max_gb': number('datasets_deposit_max_gb', 200.0),
        'url_ttl_hours': number('datasets_deposit_url_ttl_hours', 24.0),
        'max_open_days': number('datasets_deposit_max_open_days', 7.0),
        'decision_days': number('datasets_deposit_decision_days', 0.0),
        'max_open_per_user': number('datasets_deposit_max_open_per_user', 5, int),
        'max_pending_gb_per_user': number('datasets_deposit_max_pending_gb_per_user', 400.0),
        'presign_per_minute': number('datasets_deposit_presign_per_minute', 30, int),
        'parts_per_batch': number('datasets_deposit_parts_per_batch', 100, int),
        'single_put_max_mb': number('datasets_deposit_single_put_max_mb', 64, int),
        'max_files': number('datasets_deposit_max_files', 100, int),
        'max_per_day': number('datasets_deposit_max_per_day', 20, int),
        'max_refreshes': number('datasets_deposit_max_refreshes', 500, int),
        'async_complete_gb': number('datasets_deposit_async_complete_gb', 5.0),
        'complete_lease_minutes': number('datasets_deposit_complete_lease_minutes', 10.0),
        'site_id': deposit_site_id(cfg),
        'catalog_prefix': _watch_catalog_prefix(cfg),
        'staging_prefix': (str(cfg.get('datasets_deposit_staging_prefix') or '__deposits/').strip().lstrip('/')
                           .rstrip('/') + '/' + deposit_site_id(cfg) + '/'),
        'update_policy': policy,
        'prefix': prefix,
        'bucket': str(s3.get('bucket') or ''),
        's3': s3,
        'credential_source': credential_source,
        # one central watcher for several sites (design section 24)
        'catalog_bucket': str(cfg.get('datasets_deposit_catalog_bucket') or base_s3.get('bucket') or ''),
        'central_watcher': central,
        'site_key_id': str(cfg.get('datasets_deposit_site_key_id') or 'k1'),
        'site_secret': (get_or_create_secret('deposit_site_secret', env_var='ANTHIVE_DEPOSIT_SITE_SECRET', cfg=cfg)
                        if central else ''),
        'watcher_public_keys': [str(key) for key in (cfg.get('datasets_deposit_watcher_public_keys') or [])],
        'notify': notify,
    }


#: Defaults of the artefact store keys (doc/DESIGN_artefacts.md, section 14). Mirrors anthive.default.yaml.
ARTEFACT_DEFAULTS: Dict[str, Any] = {
    'artefacts_enabled': False,
    'artefacts_upload_groups': ['admins', 'analysts'],
    'collections_create_groups': ['admins', 'analysts'],
    'artefacts_max_file_gb': 100.0,
    'artefacts_max_deposit_gb': 200.0,
    'artefacts_max_files': 50,
    'artefacts_url_ttl_seconds': 300,
    'artefacts_inline_max_mb': 2.0,
    'artefacts_content_check_max_gb': 2.0,
    'artefacts_download_per_minute': 60,
    'artefacts_max_stored_gb_per_user': 1000.0,
    'artefacts_max_versions': 100,
    'artefacts_hold_public': True,
    'artefacts_serve_local': 'fallback',
    # obs-columns deposits (dataset deposits of kind obs_columns, design section 26 of the ingestion design)
    'obs_columns_max_file_gb': 2.0,
    'obs_columns_max_columns': 50,
    'obs_columns_max_rows': 20000000,
}
ARTEFACT_SERVE_LOCAL_MODES = ('fallback', 'prefer')
ARTEFACT_URL_TTL_RANGE = (10, 3600)


def get_artefact_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Settings of the artefact store (doc/DESIGN_artefacts.md, section 14).

    Precedence per key: env ``ANTHIVE_<KEY>`` (``ANTHIVE_ARTEFACTS_ENABLED``, ...) > anthive.yaml >
    :data:`ARTEFACT_DEFAULTS`. Group lists are lower-cased; a comma-separated env value is split.
    ``s3`` is the configuration used to HEAD and sign artefact GETs: ``artefacts_s3`` when it holds a key
    pair (env ``ANTHIVE_ARTEFACTS_S3_ACCESS_KEY_ID`` / ``_SECRET_ACCESS_KEY``), merged over the ``s3``
    block's endpoint, region and bucket, else the ``s3`` block itself. ``s3_readable`` is true when a bucket
    is configured. ``catalog_prefix`` is where the store's relative paths live in the bucket:
    ``ingest_watch.catalog_prefix``, else ``s3.prefix``.

    Raises:
        ConfigError: a number that is not one, ``artefacts_serve_local`` not fallback or prefer,
            ``artefacts_url_ttl_seconds`` outside 10..3600, a group list that is not a list of names.
    """
    cfg = cfg if cfg is not None else get_config()
    settings: Dict[str, Any] = {}
    for key, default in ARTEFACT_DEFAULTS.items():
        env_value = os.environ.get(f'ANTHIVE_{key.upper()}', '').strip()
        raw = env_value if env_value else cfg.get(key)
        if raw is None or raw == '':
            raw = default
        if isinstance(default, bool):
            settings[key] = _flag(raw, default)
        elif isinstance(default, list):
            if isinstance(raw, str):
                raw = [item for item in raw.split(',')]
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ConfigError(f'ERROR: [Config] {key} must be a list of group names, got {raw!r}')
            settings[key] = [item.strip().lower() for item in raw if item.strip()]
        elif isinstance(default, (int, float)):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ConfigError(f'ERROR: [Config] {key} must be a number, got {raw!r}')
            if value < 0 or value != value:
                raise ConfigError(f'ERROR: [Config] {key} must not be negative, got {raw!r}')
            settings[key] = int(value) if isinstance(default, int) else value
        else:
            settings[key] = str(raw).strip().lower()
    if settings['artefacts_serve_local'] not in ARTEFACT_SERVE_LOCAL_MODES:
        raise ConfigError("ERROR: [Config] artefacts_serve_local must be 'fallback' or 'prefer', "
                          f"not {settings['artefacts_serve_local']!r}")
    low, high = ARTEFACT_URL_TTL_RANGE
    if not low <= settings['artefacts_url_ttl_seconds'] <= high:
        raise ConfigError(f'ERROR: [Config] artefacts_url_ttl_seconds must be between {low} and {high}')
    base_s3 = dict(cfg.get('s3') or {})
    own_s3 = dict(cfg.get('artefacts_s3') or {})
    for name in ('access_key_id', 'secret_access_key'):
        env_value = os.environ.get(f'ANTHIVE_ARTEFACTS_S3_{name.upper()}', '').strip()
        if env_value:
            own_s3[name] = env_value
    if own_s3.get('access_key_id') and own_s3.get('secret_access_key'):
        settings['s3'] = {**{k: base_s3.get(k) for k in ('endpoint', 'region', 'bucket', 'use_path_style')},
                          **own_s3}
        settings['credential_source'] = 'artefacts_s3'
    else:
        settings['s3'] = base_s3
        settings['credential_source'] = 's3'
    settings['bucket'] = str(settings['s3'].get('bucket') or '')
    settings['s3_readable'] = bool(settings['bucket'])
    catalog_prefix = _watch_catalog_prefix(cfg) or str(base_s3.get('prefix') or '')
    catalog_prefix = catalog_prefix.strip().strip('/')
    settings['catalog_prefix'] = (catalog_prefix + '/') if catalog_prefix else ''
    return settings


def may_upload_artefacts(groups: Any, is_admin: bool, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a signed-in caller may deposit artefacts: admins always, members of
    ``artefacts_upload_groups`` and, as the design's default, of ``datasets_upload_groups`` (curators)."""
    if is_admin:
        return True
    cfg = cfg if cfg is not None else get_config()
    member = {str(group).strip().lower() for group in (groups or []) if str(group).strip()}
    allowed = set(get_artefact_settings(cfg)['artefacts_upload_groups'])
    allowed |= set(get_dataset_deposit_settings(cfg)['upload_groups'])
    return bool(member & allowed)


def may_create_collections(groups: Any, is_admin: bool, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a signed-in caller may create a collection without datasets: admins always, members of
    ``collections_create_groups``. Dataset uploaders are not included."""
    if is_admin:
        return True
    cfg = cfg if cfg is not None else get_config()
    member = {str(group).strip().lower() for group in (groups or []) if str(group).strip()}
    return bool(member & set(get_artefact_settings(cfg)['collections_create_groups']))


def get_model_cache_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Cache for the query-embedding model: model_cache / ANTHIVE_MODEL_CACHE,
    else FASTEMBED_CACHE_PATH (baked into the image), else <state_dir>/models."""
    configured = _setting(cfg, 'model_cache', 'ANTHIVE_MODEL_CACHE', '')
    if configured:
        return resolve_path(configured)
    if os.environ.get('FASTEMBED_CACHE_PATH'):
        return Path(os.environ['FASTEMBED_CACHE_PATH'])
    return get_state_dir(cfg) / 'models'


def get_webapp_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Static bundle root served by Caddy (jupyter/, plots/, artifacts/)."""
    return resolve_path(_setting(cfg, 'webapp_dir', 'ANTHIVE_WEBAPP_DIR', 'webapp'))


def get_duckdb_temp_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """DuckDB spill directory (ant-serve and ant-convert)."""
    configured = _setting(cfg, 'duckdb_temp_directory', 'ANTHIVE_DUCKDB_TEMP_DIR', None)
    if configured:
        return resolve_path(configured)
    return get_tmp_dir(cfg, create=False) / 'duckdb_spill'


def get_convert_staging_dir(cfg: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Where ant-convert builds a .duckdb before moving it into place.

    None means next to the target (the historical behaviour). On HPC point
    it at node-local scratch ($TMPDIR): parallel filesystems handle DuckDB's
    many small writes and its file locks badly.
    """
    configured = _setting(cfg, 'convert_staging_dir', 'ANTHIVE_CONVERT_STAGING_DIR', None)
    if not configured:
        return None
    return resolve_path(configured)


def get_api_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Base URL tools use to reach ant-serve (release/restore, rescans)."""
    cfg = cfg if cfg is not None else load_config()
    default = f"http://localhost:{cfg.get('rest_api_port') or 8080}"
    return str(_setting(cfg, 'api_url', 'ANTHIVE_API_URL', default)).rstrip('/')


DEFAULT_ISSUE_TRACKER_URL = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/issues'


def get_issue_tracker_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Target of the web UI's "Report an issue" menu entry; '' hides it.

    Unlike most settings an explicit empty value is honoured (it hides the
    entry) instead of falling back to the default.
    """
    if 'ANTHIVE_ISSUE_TRACKER_URL' in os.environ:
        return os.environ['ANTHIVE_ISSUE_TRACKER_URL'].strip()
    cfg = cfg if cfg is not None else load_config()
    if 'issue_tracker_url' in cfg:
        return str(cfg.get('issue_tracker_url') or '').strip()
    return DEFAULT_ISSUE_TRACKER_URL


DEFAULT_DOCS_BASE_URL = 'https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc'


def get_docs_base_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Folder the help page links to for detailed docs (MCP_CLIENTS.md, ...).

    The docs live in the repo so they can be fixed without a release; a site
    running a fork can point this at its own copy. Empty falls back to the
    default (the help page always needs somewhere to link to).
    """
    cfg = cfg if cfg is not None else load_config()
    value = str(_setting(cfg, 'docs_base_url', 'ANTHIVE_DOCS_BASE_URL', '') or '').strip()
    return (value or DEFAULT_DOCS_BASE_URL).rstrip('/')


DEFAULT_FACETS_VISIBLE = ('ant_sex', 'ant_disease', 'ant_apoe4_carrier', 'ant_braak', 'ant_age', 'ant_app_nlgf')


def get_facets_visible(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Facets (``ant_*`` columns) the facet pages and the dataset facet card show.

    Temporary: while the facet audit decisions are pending, only the facets
    that the donor-level tests analyse are shown. The other ``ant_*``
    columns stay in the data, SQL, plots and API listings (the ``/facets``
    API marks them ``hidden_by_config``).

    Precedence: env ``ANTHIVE_FACETS_VISIBLE`` (comma separated; ``*``
    means all), then the ``facets_visible`` key. A missing key gives
    ``DEFAULT_FACETS_VISIBLE``; an empty list or null means show every facet.

    Returns:
        The visible facet names, or ``[]`` for "show all".
    """
    env_value = os.environ.get('ANTHIVE_FACETS_VISIBLE', '').strip()
    if env_value:
        if env_value == '*':
            return []
        return _current_facet_names(env_value.split(','))
    cfg = cfg if cfg is not None else load_config()
    if 'facets_visible' not in cfg:
        return list(DEFAULT_FACETS_VISIBLE)
    value = cfg.get('facets_visible')
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(',')
    return _current_facet_names(value)


def _current_facet_names(names: List[Any]) -> List[str]:
    """Strip names, drop empties and duplicates. The retired name
    ``ant_apoe_carrier`` is no longer mapped (API 2.45.0); it matches no facet."""
    current: List[str] = []
    for name in names:
        text = str(name).strip()
        if text and text not in current:
            current.append(text)
    return current


def facet_hidden_by_config(facet: str, visible: Optional[List[str]] = None) -> bool:
    """True when ``facet`` is not in the configured visible list."""
    visible = get_facets_visible() if visible is None else visible
    return bool(visible) and facet not in visible


UI_SWITCHES = ('facets', 'caveats', 'api', 'mcp')
_TRUE_WORDS = ('true', '1', 'yes', 'on')
_FALSE_WORDS = ('false', '0', 'no', 'off')


def _ui_flag(value: Any, where: str) -> bool:
    """A strict boolean for a ``ui`` switch: true/false (or yes/no, on/off, 1/0)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ConfigError(
        f"ERROR: [Config] Invalid value for {where}\n"
        f"Details: expected true or false, got {value!r}\n"
        f"Location: anthive.yaml\n"
        f"Action: set {where} to true or false"
    )


def get_ui_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Which parts of the web UI this site shows (block ``ui``).

    Returns ``{'facets', 'caveats', 'api', 'mcp'}`` as booleans, all True by
    default. Env ``ANTHIVE_UI_<NAME>`` (true/false) wins over the config
    value. A non-boolean value is a ConfigError everywhere (a typo must not
    silently switch a part on or off); an unknown key inside ``ui`` is an
    error under ``ANTHIVE_CONFIG_STRICT=1`` (ant-stack's render check) and a
    warning otherwise.
    """
    cfg = cfg if cfg is not None else load_config()
    block = cfg.get('ui')
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ConfigError(
            "ERROR: [Config] Invalid value for ui\n"
            f"Details: expected a mapping of {', '.join(UI_SWITCHES)}, got {block!r}\n"
            "Location: anthive.yaml\n"
            "Action: write ui: as a block, e.g. ui: {facets: false}"
        )
    unknown = sorted(set(map(str, block)) - set(UI_SWITCHES))
    if unknown:
        message = (f"unknown key(s) in ui: {', '.join(unknown)} "
                   f"(known: {', '.join(UI_SWITCHES)})")
        if os.environ.get('ANTHIVE_CONFIG_STRICT', '').strip() in ('1', 'true', 'yes'):
            raise ConfigError(f"ERROR: [Config] {message}\nLocation: anthive.yaml")
        _warn_once(f'ui-unknown:{",".join(unknown)}', f"WARNING: [Config] ignoring {message}")
    settings: Dict[str, bool] = {}
    for name in UI_SWITCHES:
        env_value = os.environ.get(f'ANTHIVE_UI_{name.upper()}', '').strip()
        if env_value:
            settings[name] = _ui_flag(env_value, f'ANTHIVE_UI_{name.upper()}')
        elif block.get(name) is None:
            settings[name] = True
        else:
            settings[name] = _ui_flag(block[name], f'ui.{name}')
    return settings


_COORDINATE_MODES = ('auto', 'always', 'never')


def get_coordinate_mode(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Whether duckdb writers coordinate with ant-serve.

    auto   -> try the release/restore handshake; if no server answers, carry
              on (a batch node has no server). Default.
    always -> an unreachable server is an error.
    never  -> skip the handshake (safe only when no server holds the file).
    """
    mode = str(_setting(cfg, 'coordinate', 'ANTHIVE_COORDINATE', 'auto')).strip().lower()
    if mode not in _COORDINATE_MODES:
        raise ConfigError(
            f"ERROR: [Config] Invalid coordinate mode {mode!r}; "
            f"expected one of {', '.join(_COORDINATE_MODES)}"
        )
    return mode


_AUTH_MODES = ('authelia', 'basic', 'trusted-proxy', 'none')


def get_auth_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{'mode': str | None, 'admins': set[str]} from the ``auth`` block.

    mode None means "not declared" (legacy installs): no fallbacks and no
    warnings; identity comes from whatever headers the proxy forwards.
    """
    cfg = cfg if cfg is not None else load_config()
    block = cfg.get('auth') or {}
    mode = os.environ.get('ANTHIVE_AUTH_MODE', '').strip() or block.get('mode')
    mode = str(mode).strip().lower() if mode else None
    if mode is not None and mode not in _AUTH_MODES:
        raise ConfigError(
            f"ERROR: [Config] Invalid auth.mode {mode!r}; "
            f"expected one of {', '.join(_AUTH_MODES)}"
        )
    admins = {str(name).strip() for name in (block.get('admins') or []) if str(name).strip()}
    return {'mode': mode, 'admins': admins}


def get_internal_requires_auth(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether access=internal datasets need an authenticated caller.

    env ANTHIVE_INTERNAL_REQUIRES_AUTH > ``internal_requires_auth`` > True.
    Anything but an explicit false value keeps the protection on.
    """
    value = _setting(cfg, 'internal_requires_auth', 'ANTHIVE_INTERNAL_REQUIRES_AUTH', True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ('false', '0', 'no', 'off')


_WARMUP_MODULE_RANK_MODES = ('all', 'priority', 'off')


def _flag(value: Any, default: bool) -> bool:
    """A yaml or env boolean: true/false, yes/no, on/off, 1/0; ``default`` for None or empty."""
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ('false', '0', 'no', 'off')


def get_warmup_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The background cache warm-up of ant-serve (block ``warmup``, see anthive.default.yaml).

    Returns ``{enabled, anonymous, donor_genes, donor_cell_type, module_ranks, module_rank_datasets, workers,
    pause_seconds}``. Env overrides: ANTHIVE_WARMUP (enabled; the older ANTHIVE_FACET_WARMUP=0 also turns it off),
    ANTHIVE_WARMUP_ANONYMOUS, ANTHIVE_WARMUP_DONOR_GENES (comma list), ANTHIVE_WARMUP_MODULE_RANKS
    (all | priority | off), ANTHIVE_WARMUP_MODULE_RANK_DATASETS (comma list), ANTHIVE_WARMUP_WORKERS.
    Raises ConfigError for an unknown ``module_ranks`` mode or a ``workers`` value that is not 1 or 2.
    """
    cfg = cfg if cfg is not None else load_config()
    block = cfg.get('warmup') or {}
    if not isinstance(block, dict):
        block = {}

    def pick(key: str, env: str, default: Any) -> Any:
        env_value = os.environ.get(env, '').strip()
        if env_value:
            return env_value
        value = block.get(key)
        return default if value in (None, '') else value

    def names(value: Any) -> List[str]:
        items = value.split(',') if isinstance(value, str) else list(value or [])
        return [str(item).strip() for item in items if str(item).strip()]

    enabled = _flag(pick('enabled', 'ANTHIVE_WARMUP', True), True)
    if os.environ.get('ANTHIVE_FACET_WARMUP', '').strip() == '0':
        enabled = False
    mode = str(pick('module_ranks', 'ANTHIVE_WARMUP_MODULE_RANKS', 'all')).strip().lower()
    if mode not in _WARMUP_MODULE_RANK_MODES:
        raise ConfigError(f"ERROR: [Config] Invalid warmup.module_ranks {mode!r}; "
                          f"expected one of {', '.join(_WARMUP_MODULE_RANK_MODES)}")
    try:
        workers = int(pick('workers', 'ANTHIVE_WARMUP_WORKERS', 1))
    except (TypeError, ValueError):
        workers = 0
    if workers not in (1, 2):
        raise ConfigError("ERROR: [Config] warmup.workers must be 1 or 2 (ant-serve builds at most 2 "
                          "module rank tables at once; 1 leaves one for requests)")
    try:
        pause = max(0.0, float(pick('pause_seconds', 'ANTHIVE_WARMUP_PAUSE_SECONDS', 0.5)))
    except (TypeError, ValueError):
        pause = 0.5
    return {
        'enabled': enabled,
        'anonymous': _flag(pick('anonymous', 'ANTHIVE_WARMUP_ANONYMOUS', True), True),
        'donor_genes': names(pick('donor_genes', 'ANTHIVE_WARMUP_DONOR_GENES', ['TREM2'])),
        'donor_cell_type': str(pick('donor_cell_type', 'ANTHIVE_WARMUP_DONOR_CELL_TYPE', 'microglia')).strip(),
        'module_ranks': mode,
        'module_rank_datasets': names(pick('module_rank_datasets', 'ANTHIVE_WARMUP_MODULE_RANK_DATASETS', [])),
        'workers': workers,
        'pause_seconds': pause,
    }


def get_catalog_scan_mode(cfg: Optional[Dict[str, Any]] = None) -> str:
    """local | s3 | auto. auto (default) consults S3 only when a bucket is set."""
    mode = str(_setting(cfg, 'catalog_scan', 'ANTHIVE_CATALOG_SCAN', 'auto')).strip().lower()
    if mode not in ('local', 's3', 'auto'):
        raise ConfigError(
            f"ERROR: [Config] Invalid catalog_scan {mode!r}; expected local, s3 or auto"
        )
    return mode


# ── Secrets ──────────────────────────────────────────────────────────────────

def get_or_create_secret(name: str, configured: Optional[str] = None,
                         env_var: Optional[str] = None,
                         cfg: Optional[Dict[str, Any]] = None) -> str:
    """Return a secret: configured value, else env var, else a file in
    <state_dir>/secrets/<name> created once with 0600 permissions."""
    if configured:
        return str(configured)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    secrets_dir = get_state_dir(cfg) / 'secrets'
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    secret_path = secrets_dir / name
    if not secret_path.exists():
        value = secrets.token_urlsafe(48)
        tmp_path = secrets_dir / f".{name}.{os.getpid()}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode())
        finally:
            os.close(fd)
        try:
            os.link(tmp_path, secret_path)   # atomic; loses cleanly to a racer
        except FileExistsError:
            pass
        finally:
            tmp_path.unlink(missing_ok=True)
    return secret_path.read_text().strip()


def internal_auth_headers() -> Dict[str, str]:
    """``{'X-Anthive-Internal': secret}`` when an internal secret is
    configured (config, env, or an existing secrets file), else ``{}``.

    For tools that call ant-serve's admin endpoints from another host or
    container, where the loopback exemption does not apply. Never creates
    the secret.
    """
    value = os.environ.get('ANTHIVE_INTERNAL_SECRET')
    if not value:
        try:
            cfg = load_config()
        except ConfigError as exc:
            _warn_once('internal-secret-config', f"WARNING: [Config] no internal secret: the config does not load ({exc})")
            return {}
        value = cfg.get('internal_secret')
        if not value:
            secret_path = get_state_dir(cfg, create=False) / 'secrets' / 'internal_secret'
            try:
                value = secret_path.read_text().strip()
            except FileNotFoundError:
                # Normal for a loopback tool on a dev box before ant-serve made one;
                # fatal for a tool in another container or on a stack host (state_dir
                # pointing at a container path): say so instead of failing later.
                _warn_once('internal-secret-missing',
                           f"WARNING: [Config] no internal secret at {secret_path} (state_dir); calls to ant-serve "
                           f"from outside its host/container will be refused")
                value = None
            except OSError as exc:
                _warn_once('internal-secret-unreadable',
                           f"WARNING: [Config] cannot read the internal secret {secret_path}: {exc}")
                value = None
    return {'X-Anthive-Internal': value} if value else {}


_WARNED_ONCE: set = set()


def _warn_once(key: str, message: str) -> None:
    """Print ``message`` to stderr (and the log) once per process."""
    if key in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(key)
    print(message, file=sys.stderr)
    logging.getLogger('anthive.config').warning(message)
