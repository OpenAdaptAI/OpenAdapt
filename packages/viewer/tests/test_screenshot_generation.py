"""Tests for README screenshot generation.

This test verifies that:
1. The screenshot generation script can run successfully
2. Dependencies are properly configured
3. We catch failures early in CI before screenshots break

Note: This test requires openadapt-capture to be installed and
capture directories to exist. In CI, these tests may be skipped
if captures are not available.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Every subprocess.run below passes check=False on purpose: each test asserts on
# result.returncode itself, so the call must return rather than raise.

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_readme_screenshots.py"

# Captures are local data that lives outside this repository. Look for a sibling
# openadapt-capture checkout, and let $OPENADAPT_CAPTURE_DIR override that. Every
# test that reads a capture skips when the directory is absent, so CI and any
# contributor without recordings still run the rest of this file.
CAPTURE_DIR = Path(
    os.environ.get("OPENADAPT_CAPTURE_DIR", REPO_ROOT.parent / "openadapt-capture")
).expanduser()


@pytest.fixture
def script_exists():
    """Verify the screenshot generation script exists."""
    assert SCRIPT_PATH.exists(), f"Script not found: {SCRIPT_PATH}"
    return SCRIPT_PATH


def test_script_exists(script_exists):
    """Test that the screenshot generation script exists."""
    assert script_exists.is_file()
    assert script_exists.suffix == ".py"


def test_script_has_shebang(script_exists):
    """Test that the script has a proper shebang."""
    content = script_exists.read_text()
    assert content.startswith("#!/usr/bin/env python3")


def test_script_imports():
    """Test that the script can be imported without errors."""
    try:
        # Add scripts directory to path
        sys.path.insert(0, str(REPO_ROOT / "scripts"))

        # Try importing the module (this checks syntax and import errors)
        import generate_readme_screenshots  # noqa: F401

        # Import successful
        assert True
    except ImportError as e:
        pytest.fail(f"Failed to import script: {e}")
    finally:
        # Clean up path
        if str(REPO_ROOT / "scripts") in sys.path:
            sys.path.remove(str(REPO_ROOT / "scripts"))


def test_dependency_check():
    """Test that dependency checking works."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check-deps"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    # Script should exit with 0 if all deps available, 1 if some missing
    assert result.returncode in (0, 1), f"Unexpected exit code: {result.returncode}"
    assert "Dependency Status:" in result.stdout
    assert "openadapt_capture:" in result.stdout
    assert "playwright:" in result.stdout


@pytest.mark.skipif(
    not CAPTURE_DIR.exists(),
    reason="openadapt-capture directory not found",
)
def test_captures_exist():
    """Test that required capture directories exist.

    Both halves of this used to be stale. The captures live under
    ``examples/captures/``, not at the checkout root, where .gitignore excludes
    them. And the recorder writes ``recording.db``: the bespoke ``capture.db``
    was replaced by openadapt-capture PR #28 on 2026-07-17, so asserting on it
    described a file no current checkout has.
    """
    examples = CAPTURE_DIR / "examples" / "captures"

    for name in ("turn-off-nightshift", "demo_new"):
        capture_path = examples / name
        assert capture_path.exists(), f"Capture not found: {capture_path}"
        assert (capture_path / "recording.db").exists(), (
            f"No recording.db in {capture_path}"
        )


@pytest.mark.skipif(
    not CAPTURE_DIR.exists(),
    reason="openadapt-capture directory not found",
)
def test_help_message():
    """Test that the script shows help message."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "Generate screenshots for openadapt-viewer README" in result.stdout
    assert "--capture-dir" in result.stdout
    assert "--output-dir" in result.stdout
    assert "--max-events" in result.stdout
    assert "--skip-html" in result.stdout
    assert "--skip-screenshots" in result.stdout


@pytest.mark.skipif(
    not CAPTURE_DIR.exists(),
    reason="openadapt-capture directory not found",
)
@pytest.mark.slow
def test_html_generation_only(tmp_path):
    """Test HTML generation without screenshots (faster test).

    This test verifies that:
    1. The script can load captures
    2. HTML generation works
    3. Output files are created

    Note: This skips screenshot generation to keep tests fast.
    """
    # Check if openadapt_capture is available
    try:
        import openadapt_capture  # noqa: F401
    except ImportError:
        pytest.skip("openadapt_capture not installed")

    output_dir = tmp_path / "screenshots"
    output_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--capture-dir",
            str(CAPTURE_DIR),
            "--output-dir",
            str(output_dir),
            "--max-events",
            "10",  # Limit events for faster test
            "--skip-screenshots",  # Skip screenshot generation
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Check output
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    # Script should succeed
    if result.returncode != 0:
        pytest.fail(
            f"Script failed with exit code {result.returncode}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

    # Check that HTML files were mentioned in output
    assert "Generated HTML files:" in result.stdout
    assert "viewer.html" in result.stdout


@pytest.mark.skipif(
    not CAPTURE_DIR.exists(),
    reason="openadapt-capture directory not found",
)
@pytest.mark.slow
@pytest.mark.playwright
def test_full_screenshot_generation(tmp_path):
    """Test full screenshot generation including Playwright screenshots.

    This is the full integration test that:
    1. Generates HTML viewers
    2. Takes screenshots with Playwright
    3. Verifies all outputs exist

    Note: Requires playwright to be installed and browsers downloaded.
    Run: uv pip install playwright && uv run playwright install chromium
    """
    # Check if dependencies are available
    try:
        import openadapt_capture  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        pytest.skip(f"Required dependency not available: {e}")

    output_dir = tmp_path / "screenshots"
    output_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--capture-dir",
            str(CAPTURE_DIR),
            "--output-dir",
            str(output_dir),
            "--max-events",
            "10",  # Limit events for faster test
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,  # Screenshots take longer
    )

    # Check output
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    # Script should succeed
    if result.returncode != 0:
        pytest.fail(
            f"Script failed with exit code {result.returncode}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

    # Check that screenshots were generated
    assert "Generated screenshots:" in result.stdout
    assert "completed successfully" in result.stdout

    # Verify screenshot files exist
    screenshots = list(output_dir.glob("*.png"))
    assert len(screenshots) > 0, "No screenshot files generated"

    # Check expected screenshot files
    expected_patterns = [
        "*_full.png",
        "*_controls.png",
        "*_events.png",
    ]

    for pattern in expected_patterns:
        matching = list(output_dir.glob(pattern))
        assert len(matching) > 0, f"No screenshots matching pattern: {pattern}"


def test_error_handling_invalid_capture():
    """Test that the script handles invalid capture paths gracefully."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--capture-dir",
            "/nonexistent/path",
            "--max-events",
            "5",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    # Script should fail gracefully
    assert result.returncode != 0
    assert "Error" in result.stdout or "Error" in result.stderr or "not found" in result.stdout.lower()


@pytest.mark.parametrize(
    "args,expected_in_output",
    [
        (["--skip-html"], "Skipping HTML generation"),
        (["--skip-screenshots"], "Skipping screenshot generation"),
        (["--max-events", "5"], "Limiting to"),
    ],
)
def test_command_line_options(args, expected_in_output, tmp_path):
    """Test that command-line options are respected."""
    # Skip if captures don't exist
    if not CAPTURE_DIR.exists():
        pytest.skip("openadapt-capture directory not found")

    # Skip if openadapt_capture not installed
    try:
        import openadapt_capture  # noqa: F401
    except ImportError:
        pytest.skip("openadapt_capture not installed")

    output_dir = tmp_path / "screenshots"
    output_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--capture-dir",
            str(CAPTURE_DIR),
            "--output-dir",
            str(output_dir),
        ]
        + args,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Check that option was recognized
    assert expected_in_output in result.stdout or result.returncode == 0
