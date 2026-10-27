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

    def test_unknown_param_raises_by_default(self, defaults, monkeypatch):
        """Host tools (ant-sync, ant-stack, ant-ingest-watch) stay strict."""
        import config
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', False)
        monkeypatch.delenv("ANTHIVE_CONFIG_LENIENT", raising=False)
        with pytest.raises(ConfigError, match="Unknown"):
            validate_config({"bogus_key": 42}, defaults)
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', True)
        monkeypatch.setenv("ANTHIVE_CONFIG_STRICT", "1")
        with pytest.raises(ConfigError, match="Unknown"):
            validate_config({"bogus_key": 42}, defaults)

    def test_unknown_param_is_a_warning_in_a_server(self, defaults, monkeypatch, capsys):
        """A config rendered for a newer release must not crash-loop an older api/web/mcp (2.46.1)."""
        import config
        monkeypatch.delenv("ANTHIVE_CONFIG_STRICT", raising=False)
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', True)
        validate_config({"key_of_a_later_release": 1}, defaults)
        validate_config({"key_of_a_later_release": 1}, defaults)
        err = capsys.readouterr().err
        assert err.count("ignoring unknown configuration parameters: key_of_a_later_release") == 1

    @pytest.mark.parametrize("bad, words", [
        ({"internal_require_auth": False}, "did you mean 'internal_requires_auth'"),
        ({"sync_polcy": {"role": "slave"}}, "did you mean 'sync_policy'"),
        ({"sync_policy": {"role": "slaves"}}, "sync_policy.role must be primary or slave"),
        ({"auth": ["basic"]}, "auth must be a mapping"),
        ({"internal_requires_auth": "no"}, "internal_requires_auth must be true or false"),
        ({"datasets_upload_groups": "curators"}, "datasets_upload_groups must be a list"),
    ])
    def test_critical_keys_are_strict_even_in_a_server(self, defaults, monkeypatch, bad, words):
        import config
        monkeypatch.delenv("ANTHIVE_CONFIG_STRICT", raising=False)
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', True)
        with pytest.raises(ConfigError) as excinfo:
            validate_config(bad, defaults)
        assert words in str(excinfo.value)

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


class TestCriticalSubKeys:
    """Re-review 3: unknown sub-keys of auth/s3/sync_policy are errors in host tools, warnings in servers."""

    @pytest.fixture
    def defaults(self):
        return load_default_config()

    @pytest.mark.parametrize("bad, words", [
        ({"sync_policy": {"rol": "slave"}}, "unknown key(s) in sync_policy: rol"),
        ({"auth": {"mod": "basic"}}, "unknown key(s) in auth: mod"),
        ({"s3": {"bukcet": "b"}}, "unknown key(s) in s3: bukcet"),
    ])
    def test_host_tools_refuse_servers_warn(self, defaults, monkeypatch, capsys, bad, words):
        import config
        monkeypatch.delenv("ANTHIVE_CONFIG_STRICT", raising=False)
        monkeypatch.delenv("ANTHIVE_CONFIG_LENIENT", raising=False)
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', False)
        with pytest.raises(ConfigError) as excinfo:
            validate_config(bad, defaults)
        assert words in str(excinfo.value)
        monkeypatch.setitem(config._UNKNOWN_KEYS_LENIENT, 'on', True)
        monkeypatch.setattr(config, '_WARNED_UNKNOWN', set())
        validate_config(bad, defaults)
        assert words in capsys.readouterr().err


class TestLoadConfigReuse:
    """load_config() parses the YAML files once while they are unchanged (it sat on every request through
    get_auth_settings, about 30 ms each, which made the cached GET /stats cost 40 ms per call)."""

    def _site(self, tmp_path, monkeypatch, text):
        import config as config_module
        (tmp_path / "anthive.default.yaml").write_text("rest_api_port: 8080\nauth:\n  mode: null\n  admins: []\n")
        (tmp_path / "anthive.yaml").write_text(text)
        monkeypatch.setattr(config_module, "get_anthive_root", lambda: tmp_path)
        monkeypatch.delenv("ANTHIVE_CONFIG", raising=False)
        return config_module

    def test_unchanged_files_are_parsed_once(self, tmp_path, monkeypatch):
        config_module = self._site(tmp_path, monkeypatch, "rest_api_port: 9000\n")
        calls = []
        real = config_module.yaml.safe_load
        monkeypatch.setattr(config_module.yaml, "safe_load", lambda stream: calls.append(1) or real(stream))
        config_module.load_config()
        n_parses = len(calls)
        for _ in range(5):
            assert config_module.load_config()["rest_api_port"] == 9000
        assert len(calls) == n_parses

    def test_a_changed_file_is_read_again_and_copies_are_independent(self, tmp_path, monkeypatch):
        config_module = self._site(tmp_path, monkeypatch, "rest_api_port: 9000\n")
        first = config_module.load_config()
        first["rest_api_port"] = 1
        first["auth"]["admins"].append("mallory")
        again = config_module.load_config()
        assert again["rest_api_port"] == 9000 and again["auth"]["admins"] == []
        (tmp_path / "anthive.yaml").write_text("rest_api_port: 9100\n")
        assert config_module.load_config()["rest_api_port"] == 9100
