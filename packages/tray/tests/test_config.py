"""Tests for configuration management."""

from pathlib import Path
from unittest.mock import patch

from openadapt_tray.config import TrayConfig
from openadapt_tray.shortcuts import HotkeyConfig


class TestHotkeyConfig:
    """Tests for HotkeyConfig dataclass."""

    def test_default_values(self):
        """Test default hotkey values."""
        config = HotkeyConfig()
        assert config.toggle_recording == "<ctrl>+<shift>+r"
        assert config.open_dashboard == "<ctrl>+<shift>+d"
        assert config.stop_recording == "<ctrl>+<ctrl>+<ctrl>"

    def test_custom_values(self):
        """Test custom hotkey values."""
        config = HotkeyConfig(
            toggle_recording="<cmd>+r",
            open_dashboard="<cmd>+d",
        )
        assert config.toggle_recording == "<cmd>+r"
        assert config.open_dashboard == "<cmd>+d"


class TestTrayConfig:
    """Tests for TrayConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = TrayConfig()
        assert config.dashboard_port == 8080
        assert config.auto_launch_dashboard is True
        assert config.show_notifications is True
        assert config.stop_on_triple_ctrl is True
        assert config.captures_directory == "~/openadapt/captures"

    def test_hotkeys_default(self):
        """Test that default hotkeys are created."""
        config = TrayConfig()
        assert isinstance(config.hotkeys, HotkeyConfig)
        assert config.hotkeys.toggle_recording == "<ctrl>+<shift>+r"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        config = TrayConfig()
        data = config.to_dict()

        assert "hotkeys" in data
        assert data["hotkeys"]["toggle_recording"] == "<ctrl>+<shift>+r"
        assert data["dashboard_port"] == 8080
        assert data["show_notifications"] is True

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "hotkeys": {
                "toggle_recording": "<cmd>+r",
                "open_dashboard": "<cmd>+d",
                "stop_recording": "<ctrl>+<ctrl>+<ctrl>",
            },
            "dashboard_port": 9000,
            "show_notifications": False,
        }
        config = TrayConfig._from_dict(data)

        assert config.hotkeys.toggle_recording == "<cmd>+r"
        assert config.dashboard_port == 9000
        assert config.show_notifications is False

    def test_get_captures_path(self):
        """Test captures path expansion."""
        config = TrayConfig(captures_directory="~/test/captures")
        path = config.get_captures_path()

        assert isinstance(path, Path)
        assert not str(path).startswith("~")
        assert path.parts[-2:] == ("test", "captures")

    def test_hosted_defaults(self):
        """Test hosted/loop config defaults."""
        config = TrayConfig()
        assert config.hosted_url == "https://app.openadapt.ai"
        assert config.deployment_lane == "cloud"
        assert config.poll_interval_s == 60
        assert config.desktop_ipc_port is None

    def test_hosted_keys_roundtrip(self):
        """Hosted keys survive to_dict/_from_dict."""
        config = TrayConfig(
            hosted_url="https://example.test",
            deployment_lane="byoc",
            poll_interval_s=120,
            desktop_ipc_port=54321,
        )
        data = config.to_dict()
        assert data["hosted_url"] == "https://example.test"
        assert data["deployment_lane"] == "byoc"
        assert data["poll_interval_s"] == 120
        assert data["desktop_ipc_port"] == 54321

        loaded = TrayConfig._from_dict(data)
        assert loaded.hosted_url == "https://example.test"
        assert loaded.deployment_lane == "byoc"
        assert loaded.poll_interval_s == 120
        assert loaded.desktop_ipc_port == 54321

    def test_effective_poll_interval_clamps_to_floor(self):
        """Poll interval never drops below the safe minimum."""
        assert TrayConfig(poll_interval_s=5).effective_poll_interval_s() == 30
        assert TrayConfig(poll_interval_s=90).effective_poll_interval_s() == 90

    def test_from_dict_ignores_stale_keys(self):
        """Unknown/retired keys (e.g. training_output_directory) are dropped."""
        data = {
            "dashboard_port": 9000,
            "training_output_directory": "~/old/training",  # retired
            "unknown_future_key": 123,
        }
        config = TrayConfig._from_dict(data)
        assert config.dashboard_port == 9000
        assert not hasattr(config, "training_output_directory")

    def test_get_ingest_token_reads_keychain(self):
        """Token resolution delegates to the keychain helper (never the file)."""
        config = TrayConfig(hosted_url="https://example.test")
        with patch(
            "openadapt_tray.keychain.get_ingest_token", return_value="oai_ingest_x"
        ) as mock_get:
            assert config.get_ingest_token() == "oai_ingest_x"
            mock_get.assert_called_once_with("https://example.test")

    def test_token_never_in_serialized_config(self):
        """The serialized config must not contain any token field."""
        data = TrayConfig().to_dict()
        assert not any("token" in k for k in data)

    def test_save_and_load(self, tmp_path):
        """Test saving and loading configuration."""
        config_file = tmp_path / "tray.json"

        # Create config with custom values
        config = TrayConfig(
            dashboard_port=9000,
            show_notifications=False,
            hotkeys=HotkeyConfig(toggle_recording="<cmd>+r"),
        )

        # Mock config_path to use temp directory
        with patch.object(TrayConfig, "config_path", return_value=config_file):
            config.save()

            # Verify file was created
            assert config_file.exists()

            # Load and verify
            loaded = TrayConfig.load()
            assert loaded.dashboard_port == 9000
            assert loaded.show_notifications is False
            assert loaded.hotkeys.toggle_recording == "<cmd>+r"

    def test_load_missing_file_returns_defaults(self, tmp_path):
        """Test that loading missing config returns defaults."""
        config_file = tmp_path / "nonexistent" / "tray.json"

        with patch.object(TrayConfig, "config_path", return_value=config_file):
            config = TrayConfig.load()

            assert config.dashboard_port == 8080
            assert config.show_notifications is True

    def test_load_invalid_json_returns_defaults(self, tmp_path):
        """Test that loading invalid JSON returns defaults."""
        config_file = tmp_path / "tray.json"
        config_file.write_text("invalid json {{{")

        with patch.object(TrayConfig, "config_path", return_value=config_file):
            config = TrayConfig.load()

            assert config.dashboard_port == 8080
