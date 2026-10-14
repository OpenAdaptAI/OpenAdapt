"""Tests for scripts/generate_trace_report.py."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest


def _make_png() -> bytes:
    """Create a minimal valid 1x1 white PNG."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xFF\xFF\xFF"
    idat = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@pytest.fixture
def screenshot_dir(tmp_path: Path) -> Path:
    """Create a temp directory with numbered step screenshots."""
    d = tmp_path / "screenshots"
    d.mkdir()
    png = _make_png()
    (d / "step_00_reset.png").write_bytes(png)
    (d / "step_01.png").write_bytes(png)
    (d / "step_02.png").write_bytes(png)
    (d / "step_03.png").write_bytes(png)
    return d


@pytest.fixture
def trajectory_jsonl(tmp_path: Path) -> Path:
    """Create a sample trajectory JSONL file."""
    p = tmp_path / "trajectories.jsonl"
    records = [
        {
            "episode_id": "task_1",
            "step_index": 0,
            "screenshot_path": "task_1/step_000.png",
            "task_instruction": "Open Notepad and type Hello World",
            "action_history": [],
            "planner_output": {
                "decision": "COMMAND",
                "instruction": "Click the Start button in the taskbar",
                "reasoning": "Need to open Start menu to find Notepad",
            },
        },
        {
            "episode_id": "task_1",
            "step_index": 1,
            "screenshot_path": "task_1/step_001.png",
            "task_instruction": "Open Notepad and type Hello World",
            "action_history": ["click(0.5, 0.97)"],
            "planner_output": {
                "decision": "COMMAND",
                "instruction": "Type 'Notepad' in the search box",
                "reasoning": "Start menu is open, need to search for Notepad",
            },
        },
        {
            "episode_id": "task_1",
            "step_index": 2,
            "screenshot_path": "task_1/step_002.png",
            "task_instruction": "Open Notepad and type Hello World",
            "action_history": ["click(0.5, 0.97)", "type('Notepad')"],
            "planner_output": {
                "decision": "COMMAND",
                "instruction": "Click on Notepad in the search results",
                "reasoning": "Notepad appeared in search results",
            },
        },
    ]
    lines = [json.dumps(r) for r in records]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestGenerateReport:
    def test_basic_report_generation(self, screenshot_dir: Path, tmp_path: Path):
        """Report is generated with correct header and step sections."""
        from scripts.generate_trace_report import generate_report

        output = tmp_path / "report" / "trace.md"
        result = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=None,
            output_path=output,
            task_name="Test Task",
            score=0.75,
            run_date="2026-03-18",
        )

        assert result.exists()
        content = result.read_text()

        assert "# Execution Trace: Test Task" in content
        assert "> Date: 2026-03-18" in content
        assert "> Score: 0.75" in content
        assert "> Steps: 3" in content  # steps 1, 2, 3 (not step 0)
        assert "## Step 0 (Reset)" in content
        assert "![Reset](step_00_reset.png)" in content
        assert "## Step 1" in content
        assert "## Step 2" in content
        assert "## Step 3" in content

    def test_report_with_trajectory(
        self, screenshot_dir: Path, trajectory_jsonl: Path, tmp_path: Path
    ):
        """Trajectory data is included in the report."""
        from scripts.generate_trace_report import generate_report

        output = tmp_path / "report" / "trace.md"
        result = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=trajectory_jsonl,
            output_path=output,
            task_name="Notepad Hello",
            score=1.0,
            run_date="2026-03-18",
        )

        content = result.read_text()

        # Trajectory step_index=0 maps to Step 0 (Reset) which skips trajectory
        # data by design.  step_index=1 and 2 map to Steps 1 and 2.
        assert "**Planner**: Click the Start button in the taskbar" not in content
        assert "**Planner**: Type 'Notepad' in the search box" in content
        assert "**Reasoning**: Start menu is open, need to search for Notepad" in content
        assert "**Decision**: COMMAND" in content
        assert "**Planner**: Click on Notepad in the search results" in content
        assert "**Reasoning**: Notepad appeared in search results" in content

    def test_screenshots_copied_to_output_dir(
        self, screenshot_dir: Path, tmp_path: Path
    ):
        """Screenshots are copied next to the output markdown file."""
        from scripts.generate_trace_report import generate_report

        output = tmp_path / "output_dir" / "trace.md"
        generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=None,
            output_path=output,
            task_name="Copy Test",
            score=None,
            run_date=None,
        )

        output_dir = output.parent
        assert (output_dir / "step_00_reset.png").exists()
        assert (output_dir / "step_01.png").exists()
        assert (output_dir / "step_02.png").exists()
        assert (output_dir / "step_03.png").exists()

    def test_no_score(self, screenshot_dir: Path, tmp_path: Path):
        """Report omits score line when score is None."""
        from scripts.generate_trace_report import generate_report

        output = tmp_path / "report" / "trace.md"
        result = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=None,
            output_path=output,
            task_name="No Score Task",
            score=None,
            run_date="2026-03-18",
        )

        content = result.read_text()
        assert "> Score:" not in content

    def test_default_date(self, screenshot_dir: Path, tmp_path: Path):
        """Report uses today's date when run_date is None."""
        from datetime import date

        from scripts.generate_trace_report import generate_report

        output = tmp_path / "report" / "trace.md"
        result = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=None,
            output_path=output,
            task_name="Default Date",
            score=0.5,
            run_date=None,
        )

        content = result.read_text()
        assert f"> Date: {date.today().isoformat()}" in content

    def test_empty_screenshot_dir(self, tmp_path: Path):
        """Exits with error when no PNG files found."""
        from scripts.generate_trace_report import generate_report

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        output = tmp_path / "report" / "trace.md"

        with pytest.raises(SystemExit):
            generate_report(
                screenshots_dir=empty_dir,
                trajectory_path=None,
                output_path=output,
                task_name="Empty",
                score=None,
                run_date=None,
            )

    def test_missing_trajectory_file(self, screenshot_dir: Path, tmp_path: Path):
        """Report generates without error when trajectory file is missing."""
        from scripts.generate_trace_report import generate_report

        output = tmp_path / "report" / "trace.md"
        missing = tmp_path / "nonexistent.jsonl"

        result = generate_report(
            screenshots_dir=screenshot_dir,
            trajectory_path=missing,
            output_path=output,
            task_name="Missing Trajectory",
            score=0.0,
            run_date="2026-03-18",
        )

        assert result.exists()
        content = result.read_text()
        # Should still have step headers, just no planner/reasoning lines
        assert "## Step 1" in content
        assert "**Planner**:" not in content

    def test_nested_screenshots(self, tmp_path: Path):
        """Handles screenshots in episode subdirectories."""
        from scripts.generate_trace_report import generate_report

        sub = tmp_path / "screenshots" / "episode_001"
        sub.mkdir(parents=True)
        png = _make_png()
        (sub / "step_000.png").write_bytes(png)
        (sub / "step_001.png").write_bytes(png)

        output = tmp_path / "report" / "trace.md"
        result = generate_report(
            screenshots_dir=tmp_path / "screenshots",
            trajectory_path=None,
            output_path=output,
            task_name="Nested",
            score=None,
            run_date="2026-03-18",
        )

        assert result.exists()
        content = result.read_text()
        assert "## Step 0 (Reset)" in content
        assert "## Step 1" in content


class TestHelpers:
    def test_step_number_extraction(self):
        from scripts.generate_trace_report import _step_number_from_filename

        assert _step_number_from_filename("step_00_reset.png") == 0
        assert _step_number_from_filename("step_01.png") == 1
        assert _step_number_from_filename("step_15.png") == 15
        assert _step_number_from_filename("random.png") is None

    def test_load_trajectory_valid(self, trajectory_jsonl: Path):
        from scripts.generate_trace_report import _load_trajectory

        steps = _load_trajectory(trajectory_jsonl)
        assert len(steps) == 3
        assert 0 in steps
        assert 1 in steps
        assert 2 in steps
        assert steps[0]["planner_output"]["instruction"] == (
            "Click the Start button in the taskbar"
        )

    def test_load_trajectory_missing_file(self, tmp_path: Path):
        from scripts.generate_trace_report import _load_trajectory

        steps = _load_trajectory(tmp_path / "missing.jsonl")
        assert steps == {}

    def test_load_trajectory_malformed_lines(self, tmp_path: Path):
        from scripts.generate_trace_report import _load_trajectory

        p = tmp_path / "bad.jsonl"
        p.write_text(
            'not valid json\n{"step_index": 0, "planner_output": {}}\n\n',
            encoding="utf-8",
        )
        steps = _load_trajectory(p)
        assert len(steps) == 1
        assert 0 in steps
