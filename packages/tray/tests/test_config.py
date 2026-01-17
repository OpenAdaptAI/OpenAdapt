"""Tests for configuration management."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

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
        assert str(path).endswith("test/captures")

    def test_get_training_path(self):
        """Test training path expansion."""
        config = TrayConfig(training_output_directory="~/test/training")
        path = config.get_training_path()

        assert isinstance(path, Path)
        assert not str(path).startswith("~")
        assert str(path).endswith("test/training")

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
