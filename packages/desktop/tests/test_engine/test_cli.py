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

    def test_backends_shows_hosted_ingest(self, cli_config: EngineConfig, capsys) -> None:
        """Backends should at least show hosted_ingest (always registered)."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["backends"])
        captured = capsys.readouterr()
        assert "hosted_ingest" in captured.out

    def test_doctor_runs(self, cli_config: EngineConfig, capsys) -> None:
        """Doctor command should show checks and pass count."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["doctor"])
        captured = capsys.readouterr()
        assert "OpenAdapt Doctor" in captured.out
        assert "checks passed" in captured.out

    def test_doctor_checks_python(self, cli_config: EngineConfig, capsys) -> None:
        """Doctor should verify Python version."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["doctor"])
        captured = capsys.readouterr()
        assert "[OK] Python" in captured.out

    def test_doctor_checks_database(self, cli_config: EngineConfig, capsys) -> None:
        """Doctor should verify database connectivity."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["doctor"])
        captured = capsys.readouterr()
        assert "[OK] Database (SQLite)" in captured.out

    def test_doctor_checks_flow(self, cli_config: EngineConfig, capsys) -> None:
        """Doctor should report on the openadapt-flow loop engine."""
        with patch("engine.cli.EngineConfig", return_value=cli_config):
            main(["doctor"])
        captured = capsys.readouterr()
        assert "openadapt-flow (loop engine)" in captured.out

    def test_login_success(self, cli_config: EngineConfig, capsys) -> None:
        """login should dispatch to engine.auth.login and report the org."""
        cred = {"kind": "ingest_token", "token": "t", "refresh_token": None,
                "org_id": "org_5", "host": "https://app.openadapt.ai", "expires_at": None}
        with patch("engine.cli.EngineConfig", return_value=cli_config), \
                patch("engine.auth.login", return_value=cred):
            main(["login"])
        captured = capsys.readouterr()
        assert "Logged in" in captured.out
        assert "org_5" in captured.out

    def test_push_success(self, cli_config: EngineConfig, capsys) -> None:
        """push should print the returned workflow id + dashboard URL."""
        result = {"success": True, "workflow_id": "wf_2",
                  "dashboard_url": "https://app/dashboard/workflows/wf_2", "error": ""}
        with patch("engine.cli.EngineConfig", return_value=cli_config), \
                patch("engine.hosted.push", return_value=result):
            main(["push", "/tmp/rec"])
        captured = capsys.readouterr()
        assert "wf_2" in captured.out

    def test_push_failure_exits(self, cli_config: EngineConfig) -> None:
        """push failure should exit nonzero."""
        result = {"success": False, "workflow_id": "", "dashboard_url": "", "error": "nope"}
        with patch("engine.cli.EngineConfig", return_value=cli_config), \
                patch("engine.hosted.push", return_value=result):
            with pytest.raises(SystemExit):
                main(["push", "/tmp/rec"])
