"""
Configuration management for anthive

Loads configuration from anthive.yaml or uses defaults from anthive.default.yaml
"""

import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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
    anthive_root = get_anthive_root()
    user_config_path = anthive_root / "anthive.yaml"

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


def validate_config(config: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """
    Validate configuration values

    Args:
        config: User configuration to validate
        defaults: Default configuration for reference

    Raises:
        ConfigError: If configuration is invalid
    """
    # Check for unknown parameters
    unknown_params = set(config.keys()) - set(defaults.keys())
    if unknown_params:
        raise ConfigError(
            f"ERROR: [Config] Unknown configuration parameters\n"
            f"Details: {', '.join(unknown_params)}\n"
            f"Location: anthive.yaml\n"
            f"Action: Remove unknown parameters or check anthive.default.yaml for valid options"
        )

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
    for subkey in ('s3', 'mcp', 'chat', 'sync_policy'):
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


def get_state_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Directory for persistent per-host state (created if missing).

    Resolution: env ANTHIVE_STATE_DIR, else ``state_dir`` from the config,
    relative paths anchored at the repo root.
    """
    raw = os.environ.get('ANTHIVE_STATE_DIR')
    if not raw:
        cfg = cfg if cfg is not None else load_config()
        raw = cfg.get('state_dir') or './var'
    path = Path(raw)
    if not path.is_absolute():
        path = get_anthive_root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        except ConfigError:
            return {}
        value = cfg.get('internal_secret')
        if not value:
            try:
                value = (get_state_dir(cfg) / 'secrets' / 'internal_secret').read_text().strip()
            except OSError:
                value = None
    return {'X-Anthive-Internal': value} if value else {}
