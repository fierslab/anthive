"""
Configuration management for anthive

Loads configuration from anthive.yaml or uses defaults from anthive.default.yaml
"""

import sys
from pathlib import Path
from typing import Any, Dict

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
    for subkey in ('s3', 'mcp'):
        if isinstance(user_config.get(subkey), dict):
            merged = defaults.get(subkey, {}).copy()
            merged.update(user_config[subkey])
            config[subkey] = merged

    return config


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
