"""Tests for CLI functionality and demo command."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openadapt_viewer.cli import main, run_benchmark_command, run_demo_command

# Every subprocess.run below passes check=False on purpose: each test asserts on
# result.returncode itself, so the call must return rather than raise.


class TestDemoCommand:
    """Tests for the demo command."""

    def test_demo_generates_output_file(self, temp_dir):
        """Test that demo command generates an output file."""
        output_path = temp_dir / "demo_output.html"

        # Create a mock args object
        args = MagicMock()
        args.output = output_path
        args.tasks = 5
        args.open = False

        run_demo_command(args)

        assert output_path.exists()
        html_content = output_path.read_text()
        assert len(html_content) > 0
        assert "<!DOCTYPE html>" in html_content

    def test_demo_generates_correct_number_of_tasks(self, temp_dir):
        """Test that demo creates the correct number of tasks."""
        output_path = temp_dir / "demo_output.html"

        args = MagicMock()
        args.output = output_path
        args.tasks = 3
        args.open = False

        run_demo_command(args)

        html_content = output_path.read_text()
        # Task IDs task_001, task_002, task_003 should be present
        assert "task_001" in html_content
        assert "task_002" in html_content
        assert "task_003" in html_content

    def test_demo_default_output_name(self, temp_dir, monkeypatch):
        """Test that demo uses correct default output name."""
        monkeypatch.chdir(temp_dir)

        args = MagicMock()
        args.output = Path("demo_viewer.html")
        args.tasks = 2
        args.open = False

        run_demo_command(args)

        assert (temp_dir / "demo_viewer.html").exists()

    def test_demo_with_open_flag(self, temp_dir):
        """Test that demo command respects the open flag (mocked)."""
        output_path = temp_dir / "demo_output.html"

        args = MagicMock()
        args.output = output_path
        args.tasks = 1
        args.open = True

        with patch("openadapt_viewer.cli.webbrowser") as mock_browser:
            run_demo_command(args)

            # Should have called webbrowser.open
            mock_browser.open.assert_called_once()
            call_arg = mock_browser.open.call_args[0][0]
            assert "demo_output.html" in call_arg


class TestCLIIntegration:
    """Integration tests for CLI using subprocess."""

    def test_cli_demo_command(self, temp_dir):
        """Test running the demo command via subprocess."""
        output_path = temp_dir / "cli_demo.html"

        result = subprocess.run(
            [
                sys.executable, "-m", "openadapt_viewer.cli",
                "demo",
                "--output", str(output_path),
                "--tasks", "3"
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(temp_dir),
            env={**dict(__import__("os").environ), "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )

        # Check the command succeeded
        assert result.returncode == 0
        assert output_path.exists()

        # Verify output
        html_content = output_path.read_text()
        assert "Sample Benchmark" in html_content

    def test_cli_no_command_shows_help(self):
        """Test that running CLI without command shows help."""
        result = subprocess.run(
            [sys.executable, "-m", "openadapt_viewer.cli"],
            check=False,
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )

        # Should exit with non-zero (because no command provided)
        assert result.returncode != 0
        # Should show help or usage info
        assert "usage" in result.stdout.lower() or "usage" in result.stderr.lower() or "openadapt-viewer" in result.stdout.lower() or "openadapt-viewer" in result.stderr.lower()

    def test_cli_help_flag(self):
        """Test that --help flag works."""
        result = subprocess.run(
            [sys.executable, "-m", "openadapt_viewer.cli", "--help"],
            check=False,
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )

        assert result.returncode == 0
        assert "demo" in result.stdout
        assert "benchmark" in result.stdout

    def test_cli_demo_help_flag(self):
        """Test that demo --help works."""
        result = subprocess.run(
            [sys.executable, "-m", "openadapt_viewer.cli", "demo", "--help"],
            check=False,
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )

        assert result.returncode == 0
        assert "--output" in result.stdout
        assert "--tasks" in result.stdout


class TestBenchmarkCommand:
    """Tests for the benchmark command."""

    def test_benchmark_command_missing_data_dir(self, temp_dir):
        """Test that benchmark command fails gracefully with missing data dir."""
        args = MagicMock()
        args.data = temp_dir / "nonexistent"
        args.output = temp_dir / "output.html"
        args.standalone = False
        args.open = False

        with pytest.raises(SystemExit) as exc_info:
            run_benchmark_command(args)

        assert exc_info.value.code == 1

    def test_benchmark_command_with_valid_data(self, benchmark_data_dir, temp_dir):
        """Test benchmark command with valid data directory."""
        output_path = temp_dir / "benchmark_output.html"

        args = MagicMock()
        args.data = benchmark_data_dir
        args.output = output_path
        args.standalone = False
        args.open = False

        run_benchmark_command(args)

        assert output_path.exists()
        html_content = output_path.read_text()
        assert "Test Benchmark" in html_content

    def test_benchmark_command_with_open_flag(self, benchmark_data_dir, temp_dir):
        """Test benchmark command respects open flag."""
        output_path = temp_dir / "benchmark_output.html"

        args = MagicMock()
        args.data = benchmark_data_dir
        args.output = output_path
        args.standalone = False
        args.open = True

        with patch("openadapt_viewer.cli.webbrowser") as mock_browser:
            run_benchmark_command(args)

            mock_browser.open.assert_called_once()


class TestMainEntryPoint:
    """Tests for the main() entry point."""

    def test_main_with_demo_args(self, temp_dir, monkeypatch):
        """Test main() parses demo command correctly."""
        output_path = temp_dir / "main_demo.html"

        test_args = ["prog", "demo", "--output", str(output_path), "--tasks", "2"]
        monkeypatch.setattr(sys, "argv", test_args)

        main()

        assert output_path.exists()

    def test_main_with_no_args_exits(self, monkeypatch):
        """Test main() exits when no command provided."""
        test_args = ["prog"]
        monkeypatch.setattr(sys, "argv", test_args)

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1


class TestCLIOutputMessages:
    """Tests for CLI output messages."""

    def test_demo_prints_generation_message(self, temp_dir, capsys):
        """Test that demo command prints helpful messages."""
        output_path = temp_dir / "demo_output.html"

        args = MagicMock()
        args.output = output_path
        args.tasks = 5
        args.open = False

        run_demo_command(args)

        captured = capsys.readouterr()
        assert "5" in captured.out  # Should mention number of tasks
        assert "sample" in captured.out.lower() or "demo" in captured.out.lower()

    def test_benchmark_prints_generation_message(self, benchmark_data_dir, temp_dir, capsys):
        """Test that benchmark command prints helpful messages."""
        output_path = temp_dir / "benchmark_output.html"

        args = MagicMock()
        args.data = benchmark_data_dir
        args.output = output_path
        args.standalone = False
        args.open = False

        run_benchmark_command(args)

        captured = capsys.readouterr()
        assert "Generating" in captured.out
        assert str(output_path) in captured.out or "benchmark" in captured.out.lower()
