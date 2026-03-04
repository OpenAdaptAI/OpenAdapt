"""Tests for the CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.cli import _init_engine, main
from engine.config import EngineConfig


@pytest.fixture
def cli_config(tmp_data_dir: Path) -> EngineConfig:
    """Config for CLI tests."""
    return EngineConfig(
        data_dir=tmp_data_dir,
        storage_mode="air-gapped",
        max_storage_gb=1.0,
        log_level="WARNING",
    )


@pytest.fixture
def engine(cli_config: EngineConfig):
    """Initialized engine for CLI tests."""
    eng = _init_engine(cli_config)
    yield eng
    eng.db.close()


class TestCLI:
    """Tests for CLI commands."""

    def test_list_empty(self, cli_config: EngineConfig, capsys) -> None:
        """List with no captures should show 'No captures found'."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["list"])
        captured = capsys.readouterr()
        assert "No captures found" in captured.out

    def test_config_shows_json(self, cli_config: EngineConfig, capsys) -> None:
        """Config command should output valid JSON."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["config"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "storage_mode" in data

    def test_storage_shows_usage(self, cli_config: EngineConfig, capsys) -> None:
        """Storage command should show usage info."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["storage"])
        captured = capsys.readouterr()
        assert "Storage usage" in captured.out

    def test_health_shows_status(self, cli_config: EngineConfig, capsys) -> None:
        """Health command should show memory and disk info."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["health"])
        captured = capsys.readouterr()
        assert "Health status" in captured.out
        assert "Memory" in captured.out

    def test_review_empty(self, cli_config: EngineConfig, capsys) -> None:
        """Review with no pending captures."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["review"])
        captured = capsys.readouterr()
        assert "No captures pending" in captured.out

    def test_cleanup_runs(self, cli_config: EngineConfig, capsys) -> None:
        """Cleanup should run without errors."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["cleanup"])
        captured = capsys.readouterr()
        assert "Cleanup complete" in captured.out

    def test_info_nonexistent(self, cli_config: EngineConfig) -> None:
        """Info on nonexistent capture should exit with error."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            with pytest.raises(SystemExit):
                main(["info", "nonexistent"])

    def test_backends_shows_wormhole(self, cli_config: EngineConfig, capsys) -> None:
        """Backends should at least show wormhole (always available)."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["backends"])
        captured = capsys.readouterr()
        assert "wormhole" in captured.out
