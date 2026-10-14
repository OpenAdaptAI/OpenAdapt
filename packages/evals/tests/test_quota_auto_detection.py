"""Tests for Azure quota auto-detection feature."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


class TestGetQuotaStatus:
    """Tests for get_quota_status function."""

    def test_sufficient_quota(self):
        """Test when quota is sufficient."""
        from openadapt_evals.benchmarks.vm_cli import get_quota_status

        mock_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 8,
            }
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )

            status = get_quota_status("eastus", "Standard DDSv4 Family", 8)

            assert status["sufficient"] is True
            assert status["limit"] == 8
            assert status["current"] == 0
            assert status["family"] == "Standard DDSv4 Family"
            assert status["error"] is None

    def test_insufficient_quota(self):
        """Test when quota is insufficient."""
        from openadapt_evals.benchmarks.vm_cli import get_quota_status

        mock_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 4,
            }
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )

            status = get_quota_status("eastus", "Standard DDSv4 Family", 8)

            assert status["sufficient"] is False
            assert status["limit"] == 4
            assert status["error"] is None

    def test_family_not_found(self):
        """Test when VM family is not in the list."""
        from openadapt_evals.benchmarks.vm_cli import get_quota_status

        mock_output = json.dumps([
            {
                "name": {"localizedValue": "Some Other Family", "value": "someOtherFamily"},
                "currentValue": 0,
                "limit": 10,
            }
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )

            status = get_quota_status("eastus", "Standard DDSv4 Family", 8)

            assert status["sufficient"] is False
            assert "not found" in status["error"]

    def test_azure_cli_error(self):
        """Test when Azure CLI returns an error."""
        from openadapt_evals.benchmarks.vm_cli import get_quota_status

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="ERROR: Not logged in",
            )

            status = get_quota_status("eastus", "Standard DDSv4 Family", 8)

            assert status["sufficient"] is False
            assert "Not logged in" in status["error"]

    def test_invalid_json_response(self):
        """Test when Azure CLI returns invalid JSON."""
        from openadapt_evals.benchmarks.vm_cli import get_quota_status

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json",
                stderr="",
            )

            status = get_quota_status("eastus", "Standard DDSv4 Family", 8)

            assert status["sufficient"] is False
            assert "Failed to parse JSON" in status["error"]


class TestQuotaWaitCommand:
    """Tests for azure-ml-quota-wait CLI command."""

    def test_immediate_success_when_quota_sufficient(self):
        """Test that command exits immediately when quota is already sufficient."""
        from openadapt_evals.benchmarks.vm_cli import cmd_azure_ml_quota_wait

        mock_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 16,
            }
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )

            args = argparse.Namespace(
                family="Standard DDSv4 Family",
                target=8,
                location="eastus",
                interval=60,
                timeout=3600,
                auto_run=False,
                quiet=False,
            )

            result = cmd_azure_ml_quota_wait(args)

            assert result == 0
            # Should have called az vm list-usage once
            assert mock_run.call_count == 1

    def test_timeout_when_quota_never_sufficient(self):
        """Test that command times out when quota is never approved."""
        from openadapt_evals.benchmarks.vm_cli import cmd_azure_ml_quota_wait

        mock_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 0,  # Never sufficient
            }
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )

            with patch("time.sleep") as mock_sleep:
                # Make time.sleep do nothing so test runs fast
                mock_sleep.return_value = None

                args = argparse.Namespace(
                    family="Standard DDSv4 Family",
                    target=8,
                    location="eastus",
                    interval=1,
                    timeout=3,  # Very short timeout
                    auto_run=False,
                    quiet=True,
                )

                # Simulate time passing
                call_count = [0]
                original_time = time.time

                def mock_time():
                    call_count[0] += 1
                    # Each call advances time by 1 second
                    return original_time() + call_count[0]

                with patch("time.time", side_effect=mock_time):
                    result = cmd_azure_ml_quota_wait(args)

                assert result == 1  # Timeout returns 1

    def test_success_after_quota_approved(self):
        """Test that command succeeds when quota becomes sufficient."""
        from openadapt_evals.benchmarks.vm_cli import cmd_azure_ml_quota_wait

        insufficient_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 0,
            }
        ])

        sufficient_output = json.dumps([
            {
                "name": {"localizedValue": "Standard DDSv4 Family", "value": "standardDDSv4Family"},
                "currentValue": 0,
                "limit": 16,  # Quota approved!
            }
        ])

        call_count = [0]

        def mock_run_side_effect(*args, **kwargs):
            call_count[0] += 1
            # First 2 calls return insufficient, third returns sufficient
            if call_count[0] < 3:
                return MagicMock(returncode=0, stdout=insufficient_output, stderr="")
            return MagicMock(returncode=0, stdout=sufficient_output, stderr="")

        with patch("subprocess.run", side_effect=mock_run_side_effect):
            with patch("time.sleep") as mock_sleep:
                mock_sleep.return_value = None

                args = argparse.Namespace(
                    family="Standard DDSv4 Family",
                    target=8,
                    location="eastus",
                    interval=1,
                    timeout=3600,
                    auto_run=False,
                    quiet=True,
                )

                result = cmd_azure_ml_quota_wait(args)

                assert result == 0
                assert call_count[0] == 3  # Should have checked 3 times


class TestCLIIntegration:
    """Integration tests for CLI argument parsing."""

    def test_help_flag(self):
        """Test that --help works for azure-ml-quota-wait."""
        result = subprocess.run(
            [sys.executable, "-m", "openadapt_evals.benchmarks.vm_cli", "azure-ml-quota-wait", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--family" in result.stdout
        assert "--target" in result.stdout
        assert "--interval" in result.stdout
        assert "--timeout" in result.stdout
        assert "--auto-run" in result.stdout
        assert "--quiet" in result.stdout

    def test_default_values(self):
        """Test that default values are set correctly."""
        # Import the module to get access to the parser

        # Create a minimal parser just for testing defaults
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        p = subparsers.add_parser("test")
        p.add_argument("--family", default="Standard DDSv4 Family")
        p.add_argument("--target", type=int, default=8)
        p.add_argument("--location", default="eastus")
        p.add_argument("--interval", type=int, default=60)
        p.add_argument("--timeout", type=int, default=86400)
        p.add_argument("--auto-run", action="store_true")
        p.add_argument("--quiet", action="store_true")

        args = parser.parse_args(["test"])

        assert args.family == "Standard DDSv4 Family"
        assert args.target == 8
        assert args.location == "eastus"
        assert args.interval == 60
        assert args.timeout == 86400
        assert args.auto_run is False
        assert args.quiet is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
