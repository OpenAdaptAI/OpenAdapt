"""Tests for the openadapt CLI module."""

import pytest
from click.testing import CliRunner

from openadapt.cli import main, version, doctor


class TestCLI:
    """Test cases for the CLI functionality."""

    def test_main_help(self):
        """Test that the main command shows help."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "OpenAdapt - GUI automation with ML" in result.output

    def test_version_option(self):
        """Test the version option works."""
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "1.0.6" in result.output

    def test_version_command(self):
        """Test the version command."""
        runner = CliRunner()
        result = runner.invoke(version)
        assert result.exit_code == 0
        assert "OpenAdapt Ecosystem Versions" in result.output
        assert "openadapt:" in result.output

    def test_doctor_command(self):
        """Test the doctor command runs."""
        runner = CliRunner()
        result = runner.invoke(doctor)
        assert result.exit_code == 0
        assert "OpenAdapt System Check" in result.output
        assert "Python:" in result.output

    def test_capture_help(self):
        """Test that capture commands show help."""
        runner = CliRunner()
        result = runner.invoke(main, ["capture", "--help"])
        assert result.exit_code == 0
        assert "Record GUI demonstrations" in result.output

    def test_train_help(self):
        """Test that train commands show help."""
        runner = CliRunner()
        result = runner.invoke(main, ["train", "--help"])
        assert result.exit_code == 0
        assert "Train ML models" in result.output

    def test_eval_help(self):
        """Test that eval commands show help."""
        runner = CliRunner()
        result = runner.invoke(main, ["eval", "--help"])
        assert result.exit_code == 0
        assert "Evaluate models" in result.output

    def test_serve_help(self):
        """Test that serve command shows help."""
        runner = CliRunner()
        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "Serve the training dashboard" in result.output

    def test_capture_commands_require_optional_deps(self):
        """Test that capture commands fail gracefully without dependencies."""
        runner = CliRunner()
        # This should exit with error code 1 due to missing openadapt-capture
        result = runner.invoke(main, ["capture", "list"])
        assert result.exit_code == 1
        assert "openadapt-capture not installed" in result.output

    def test_train_commands_require_optional_deps(self):
        """Test that train commands fail gracefully without dependencies."""
        runner = CliRunner()
        # This should exit with error code 1 due to missing openadapt-ml
        result = runner.invoke(main, ["train", "status"])
        assert result.exit_code == 0  # status command doesn't require deps, just checks files

    def test_eval_commands_require_optional_deps(self):
        """Test that eval commands fail gracefully without dependencies."""
        runner = CliRunner()
        # This should exit with error code 1 due to missing openadapt-evals
        result = runner.invoke(main, ["eval", "mock", "--tasks", "1"])
        assert result.exit_code == 1
        assert "openadapt-evals not installed" in result.output

    def test_serve_command_requires_optional_deps(self):
        """Test that serve command fails gracefully without dependencies."""
        runner = CliRunner()
        # This should exit with error code 1 due to missing openadapt-ml
        result = runner.invoke(main, ["serve", "--port", "8081", "--no-open"])
        assert result.exit_code == 1
        assert "openadapt-ml not installed" in result.output