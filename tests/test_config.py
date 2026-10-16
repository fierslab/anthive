"""Tests for lib/config.py — configuration loading, merging, and validation."""

import pytest

from config import ConfigError, load_config, load_default_config, validate_config


# ── load_default_config ───────────────────────────────────────────────────────

class TestLoadDefaultConfig:

    def test_loads_successfully(self):
        config = load_default_config()
        assert isinstance(config, dict)

    def test_contains_expected_keys(self):
        config = load_default_config()
        assert "max_obscat_values" in config
        assert "rest_api_port" in config
        assert "s3" in config
        assert "mcp" in config

    def test_default_values(self):
        config = load_default_config()
        assert config["max_obscat_values"] == 100
        assert config["rest_api_port"] == 8080
        assert config["melt_chunk_size"] == 10000

    def test_s3_section_complete(self):
        config = load_default_config()
        s3 = config["s3"]
        assert "bucket" in s3
        assert "endpoint" in s3
        assert "access_key_id" in s3
        assert "secret_access_key" in s3


# ── validate_config ───────────────────────────────────────────────────────────

class TestValidateConfig:

    @pytest.fixture
    def defaults(self):
        return load_default_config()

    def test_empty_config_valid(self, defaults):
        validate_config({}, defaults)

    def test_unknown_param_raises_when_strict(self, defaults, monkeypatch):
        with pytest.raises(ConfigError, match="Unknown"):
            validate_config({"bogus_key": 42}, defaults, strict=True)
        monkeypatch.setenv("ANTHIVE_CONFIG_STRICT", "1")
        with pytest.raises(ConfigError, match="Unknown"):
            validate_config({"bogus_key": 42}, defaults)

    def test_unknown_param_is_a_warning_by_default(self, defaults, monkeypatch, capsys):
        """A config rendered for a newer release must not crash-loop an older image (2.46.1)."""
        monkeypatch.delenv("ANTHIVE_CONFIG_STRICT", raising=False)
        validate_config({"key_of_a_later_release": 1}, defaults)
        validate_config({"key_of_a_later_release": 1}, defaults)
        err = capsys.readouterr().err
        assert err.count("ignoring unknown configuration parameters: key_of_a_later_release") == 1

    def test_valid_override(self, defaults):
        validate_config({"max_obscat_values": 200}, defaults)

    # max_obscat_values
    def test_max_obscat_negative_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"max_obscat_values": -1}, defaults)

    def test_max_obscat_zero_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"max_obscat_values": 0}, defaults)

    def test_max_obscat_string_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"max_obscat_values": "fifty"}, defaults)

    # melt_chunk_size
    def test_melt_chunk_zero_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"melt_chunk_size": 0}, defaults)

    def test_melt_chunk_negative_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"melt_chunk_size": -100}, defaults)

    # rest_api_port
    def test_port_zero_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"rest_api_port": 0}, defaults)

    def test_port_too_high_raises(self, defaults):
        with pytest.raises(ConfigError):
            validate_config({"rest_api_port": 99999}, defaults)

    def test_port_valid(self, defaults):
        validate_config({"rest_api_port": 9090}, defaults)


# ── load_config (integration: merging defaults with user overrides) ───────────

class TestLoadConfig:

    def test_returns_dict(self):
        config = load_config()
        assert isinstance(config, dict)

    def test_has_defaults(self):
        config = load_config()
        # Even without a user config, defaults are present
        assert "max_obscat_values" in config
        assert "s3" in config

    def test_s3_deep_merge(self, monkeypatch, tmp_path):
        """Partial s3 override should keep other s3 defaults."""
        import config as config_module

        # Create a minimal user config overriding only s3.bucket
        user_yaml = tmp_path / "anthive.yaml"
        user_yaml.write_text("s3:\n  bucket: test-bucket\n")

        # Create a minimal default config
        default_yaml = tmp_path / "anthive.default.yaml"
        default_yaml.write_text(
            "s3:\n  bucket: null\n  endpoint: null\n  region: us-east-1\n"
            "max_obscat_values: 100\nmelt_chunk_size: 10000\nrest_api_port: 8080\n"
            "mcp:\n  host: 0.0.0.0\n  port: 8090\n"
        )

        monkeypatch.setattr(config_module, "get_anthive_root", lambda: tmp_path)
        config = config_module.load_config()
        assert config["s3"]["bucket"] == "test-bucket"
        assert config["s3"]["region"] == "us-east-1"  # kept from defaults
