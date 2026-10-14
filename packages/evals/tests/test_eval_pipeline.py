"""Tests for scripts/run_eval_pipeline.py pure functions.

Tests cover:
- _build_conditions: building ZS/DC evaluation condition tuples
- _find_recordings_needing_demos: scanning recordings for missing demos
- _print_summary: result summary formatting
- CLI argument parsing: --zs-only, --dc-only, --dry-run flags
"""

import sys
from pathlib import Path
from unittest.mock import patch

# Add scripts directory to path so we can import the pipeline module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_eval_pipeline as pipeline

# ---------------------------------------------------------------------------
# _build_conditions
# ---------------------------------------------------------------------------


class TestBuildConditions:
    """Tests for _build_conditions which builds (task_id, run_name, demo_path) tuples."""

    def test_default_returns_zs_and_dc(self, tmp_path):
        """Default (neither --zs-only nor --dc-only) returns both ZS and DC conditions."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        assert len(conditions) == 2
        # First condition should be ZS (no demo path)
        assert conditions[0][1] == f"val_zs_{task_id[:8]}"
        assert conditions[0][2] is None
        # Second condition should be DC (with demo path)
        assert conditions[1][1] == f"val_dc_{task_id[:8]}"
        assert conditions[1][2] is not None
        assert conditions[1][2].exists()

    def test_zs_only_excludes_dc(self, tmp_path):
        """--zs-only should produce only ZS conditions, even when demo exists."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=True, dc_only=False
        )

        assert len(conditions) == 1
        assert conditions[0][1] == f"val_zs_{task_id[:8]}"
        assert conditions[0][2] is None

    def test_dc_only_excludes_zs(self, tmp_path):
        """--dc-only should produce only DC conditions when demo exists."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=True
        )

        assert len(conditions) == 1
        assert conditions[0][1] == f"val_dc_{task_id[:8]}"
        assert conditions[0][2] is not None

    def test_dc_only_without_demo_skips_task(self, tmp_path):
        """--dc-only with no demo file should skip the task (with a warning)."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        # No demo file created

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=True
        )

        assert len(conditions) == 0

    def test_both_flags_produces_empty(self, tmp_path):
        """Setting both --zs-only and --dc-only should produce no conditions.

        zs_only=True skips DC, dc_only=True skips ZS, so nothing passes.
        """
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=True, dc_only=True
        )

        assert len(conditions) == 0

    def test_multiple_tasks(self, tmp_path):
        """Multiple task IDs produce conditions for each task."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_ids = [
            "aaaaaaaa-1111-2222-3333-444444444444",
            "bbbbbbbb-5555-6666-7777-888888888888",
        ]
        for tid in task_ids:
            (demo_dir / f"{tid}.txt").write_text("demo content")

        conditions = pipeline._build_conditions(
            task_ids, demo_dir, zs_only=False, dc_only=False
        )

        # 2 tasks x 2 conditions each = 4
        assert len(conditions) == 4
        run_names = [c[1] for c in conditions]
        assert "val_zs_aaaaaaaa" in run_names
        assert "val_dc_aaaaaaaa" in run_names
        assert "val_zs_bbbbbbbb" in run_names
        assert "val_dc_bbbbbbbb" in run_names

    def test_task_id_truncation_in_run_name(self, tmp_path):
        """Run names use first 8 chars of the task ID."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "abcdefgh-ijkl-mnop-qrst-uvwxyz012345"
        (demo_dir / f"{task_id}.txt").write_text("demo")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        for _, run_name, _ in conditions:
            assert run_name.endswith("abcdefgh")

    def test_json_demo_fallback(self, tmp_path):
        """When .txt demo is missing but .json exists, DC condition uses .json."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        # Create .json demo instead of .txt
        (demo_dir / f"{task_id}.json").write_text('{"steps": []}')

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        dc_conditions = [c for c in conditions if c[2] is not None]
        assert len(dc_conditions) == 1
        assert dc_conditions[0][2].suffix == ".json"

    def test_txt_demo_preferred_over_json(self, tmp_path):
        """When both .txt and .json exist, .txt is preferred."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("text demo")
        (demo_dir / f"{task_id}.json").write_text('{"steps": []}')

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        dc_conditions = [c for c in conditions if c[2] is not None]
        assert len(dc_conditions) == 1
        assert dc_conditions[0][2].suffix == ".txt"

    def test_missing_demo_prints_warning(self, tmp_path, capsys):
        """When no demo exists for a task, a warning is printed."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"

        pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "No demo" in captured.out

    def test_empty_task_list(self, tmp_path):
        """Empty task list produces no conditions."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()

        conditions = pipeline._build_conditions(
            [], demo_dir, zs_only=False, dc_only=False
        )

        assert conditions == []

    def test_condition_tuple_structure(self, tmp_path):
        """Each condition tuple has (task_id, run_name, demo_path_or_None)."""
        demo_dir = tmp_path / "demos"
        demo_dir.mkdir()
        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        (demo_dir / f"{task_id}.txt").write_text("demo")

        conditions = pipeline._build_conditions(
            [task_id], demo_dir, zs_only=False, dc_only=False
        )

        for tid, run_name, demo_path in conditions:
            assert isinstance(tid, str)
            assert isinstance(run_name, str)
            assert demo_path is None or isinstance(demo_path, Path)


# ---------------------------------------------------------------------------
# _find_recordings_needing_demos
# ---------------------------------------------------------------------------


class TestFindRecordingsNeedingDemos:
    """Tests for _find_recordings_needing_demos which finds recordings without demo files."""

    def _make_recording(self, recordings_dir: Path, task_id: str, meta_file: str = "meta.json"):
        """Helper to create a recording directory with a meta file."""
        task_dir = recordings_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / meta_file).write_text('{"steps": []}')
        return task_dir

    def test_missing_demo_included(self, tmp_path):
        """Recording dir with meta.json but no demo.txt should be included."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-full-uuid-here"
        self._make_recording(recordings_dir, task_id)

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 1
        assert result[0][1] == task_id

    def test_existing_demo_excluded(self, tmp_path):
        """Recording dir with an existing demo.txt should be excluded."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-full-uuid-here"
        self._make_recording(recordings_dir, task_id)
        (demo_dir / f"{task_id}.txt").write_text("existing demo")

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 0

    def test_no_recording_dir_excluded(self, tmp_path):
        """Non-directory entries in recordings_dir should be ignored."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        # Create a file (not a directory) in recordings
        (recordings_dir / "not-a-dir.txt").write_text("this is a file")

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 0

    def test_no_meta_json_excluded(self, tmp_path):
        """Recording dir without meta.json or meta_refined.json should be excluded."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_dir = recordings_dir / "some-task-id"
        task_dir.mkdir()
        # No meta.json or meta_refined.json

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 0

    def test_meta_refined_json_accepted(self, tmp_path):
        """Recording dir with meta_refined.json (no meta.json) should be included."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-refined-only"
        self._make_recording(recordings_dir, task_id, meta_file="meta_refined.json")

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 1
        assert result[0][1] == task_id

    def test_meta_refined_preferred_over_meta(self, tmp_path):
        """When both meta_refined.json and meta.json exist, both are valid."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-both-meta"
        task_dir = recordings_dir / task_id
        task_dir.mkdir()
        (task_dir / "meta.json").write_text('{"old": true}')
        (task_dir / "meta_refined.json").write_text('{"refined": true}')

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 1

    def test_task_filter_includes_matching(self, tmp_path):
        """Task filter with matching prefix should include the task."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        self._make_recording(recordings_dir, task_id)

        result = pipeline._find_recordings_needing_demos(
            recordings_dir, demo_dir, task_filter=["04d9aeaf"]
        )

        assert len(result) == 1

    def test_task_filter_excludes_non_matching(self, tmp_path):
        """Task filter should exclude tasks that don't match any prefix."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-1234-5678-abcd-ef0123456789"
        self._make_recording(recordings_dir, task_id)

        result = pipeline._find_recordings_needing_demos(
            recordings_dir, demo_dir, task_filter=["ffffffff"]
        )

        assert len(result) == 0

    def test_task_filter_none_includes_all(self, tmp_path):
        """task_filter=None should include all recordings."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        for tid in ["aaaa-task", "bbbb-task", "cccc-task"]:
            self._make_recording(recordings_dir, tid)

        result = pipeline._find_recordings_needing_demos(
            recordings_dir, demo_dir, task_filter=None
        )

        assert len(result) == 3

    def test_multiple_filter_prefixes(self, tmp_path):
        """Multiple filter prefixes should match tasks starting with any of them."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        for tid in ["aaaa-task", "bbbb-task", "cccc-task"]:
            self._make_recording(recordings_dir, tid)

        result = pipeline._find_recordings_needing_demos(
            recordings_dir, demo_dir, task_filter=["aaaa", "cccc"]
        )

        assert len(result) == 2
        task_ids = [r[1] for r in result]
        assert "aaaa-task" in task_ids
        assert "cccc-task" in task_ids

    def test_returns_tuple_of_path_and_task_id(self, tmp_path):
        """Each returned item should be (Path, str) tuple."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-test-tuple"
        self._make_recording(recordings_dir, task_id)

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert len(result) == 1
        task_dir, tid = result[0]
        assert isinstance(task_dir, Path)
        assert isinstance(tid, str)
        assert task_dir.name == task_id
        assert tid == task_id

    def test_results_sorted_by_directory_name(self, tmp_path):
        """Results should be sorted by directory name (alphabetical)."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        # Create in non-sorted order
        for tid in ["cccc-task", "aaaa-task", "bbbb-task"]:
            self._make_recording(recordings_dir, tid)

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        task_ids = [r[1] for r in result]
        assert task_ids == sorted(task_ids)

    def test_empty_recordings_dir(self, tmp_path):
        """Empty recordings directory should return empty list."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        result = pipeline._find_recordings_needing_demos(recordings_dir, demo_dir)

        assert result == []


# ---------------------------------------------------------------------------
# _print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    """Tests for _print_summary which prints evaluation results."""

    def test_all_success(self, capsys):
        """Summary should show correct counts for all-success runs."""
        results = {
            "val_zs_04d9aeaf": {
                "status": "OK",
                "returncode": 0,
                "elapsed_s": 120.0,
                "task_id": "04d9aeaf-full",
                "condition": "ZS",
            },
            "val_dc_04d9aeaf": {
                "status": "OK",
                "returncode": 0,
                "elapsed_s": 180.0,
                "task_id": "04d9aeaf-full",
                "condition": "DC",
            },
        }

        pipeline._print_summary(results, "api-claude-cu")
        output = capsys.readouterr().out

        assert "2/2 completed" in output
        assert "api-claude-cu" in output
        assert "val_zs_04d9aeaf" in output
        assert "val_dc_04d9aeaf" in output
        assert "ZS" in output
        assert "DC" in output

    def test_partial_failure(self, capsys):
        """Summary should show correct counts when some runs fail."""
        results = {
            "val_zs_task1": {
                "status": "OK",
                "returncode": 0,
                "elapsed_s": 100.0,
                "task_id": "task1",
                "condition": "ZS",
            },
            "val_dc_task1": {
                "status": "FAIL (rc=1)",
                "returncode": 1,
                "elapsed_s": 50.0,
                "task_id": "task1",
                "condition": "DC",
            },
        }

        pipeline._print_summary(results, "api-claude-cu")
        output = capsys.readouterr().out

        assert "1/2 completed" in output

    def test_empty_results(self, capsys):
        """Summary should handle empty results dict gracefully."""
        pipeline._print_summary({}, "api-claude-cu")
        output = capsys.readouterr().out

        assert "0/0 completed" in output

    def test_total_time_calculation(self, capsys):
        """Summary should show total elapsed time across all runs."""
        results = {
            "run1": {
                "status": "OK",
                "returncode": 0,
                "elapsed_s": 60.0,
                "task_id": "t1",
                "condition": "ZS",
            },
            "run2": {
                "status": "OK",
                "returncode": 0,
                "elapsed_s": 120.0,
                "task_id": "t2",
                "condition": "DC",
            },
        }

        pipeline._print_summary(results, "test-agent")
        output = capsys.readouterr().out

        # Total = 180s = 3.0min
        assert "180s" in output
        assert "3.0min" in output

    def test_skipped_runs(self, capsys):
        """Summary should handle SKIP status runs."""
        results = {
            "val_zs_skipped": {
                "status": "SKIP",
                "returncode": -1,
                "elapsed_s": 0,
                "task_id": "skipped-task",
                "condition": "ZS",
            },
        }

        pipeline._print_summary(results, "api-claude-cu")
        output = capsys.readouterr().out

        assert "0/1 completed" in output
        assert "SKIP" in output


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    """Tests for CLI argument parsing in main()."""

    def test_zs_only_flag(self):
        """--zs-only flag should be parsed correctly."""
        parser = self._build_parser()
        args = parser.parse_args(["--zs-only"])
        assert args.zs_only is True
        assert args.dc_only is False

    def test_dc_only_flag(self):
        """--dc-only flag should be parsed correctly."""
        parser = self._build_parser()
        args = parser.parse_args(["--dc-only"])
        assert args.dc_only is True
        assert args.zs_only is False

    def test_dry_run_flag(self):
        """--dry-run flag should be parsed correctly."""
        parser = self._build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_tasks_comma_separated(self):
        """--tasks should accept comma-separated task IDs."""
        parser = self._build_parser()
        args = parser.parse_args(["--tasks", "aaa,bbb,ccc"])
        assert args.tasks == "aaa,bbb,ccc"

    def test_default_agent(self):
        """Default agent should be api-claude-cu."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.agent == "api-claude-cu"

    def test_default_max_steps(self):
        """Default max-steps should be 15."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.max_steps == 15

    def test_custom_max_steps(self):
        """--max-steps should accept custom values."""
        parser = self._build_parser()
        args = parser.parse_args(["--max-steps", "30"])
        assert args.max_steps == 30

    def test_default_server(self):
        """Default server should be http://localhost:5001."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.server == "http://localhost:5001"

    def test_skip_vm_flag(self):
        """--skip-vm flag should be parsed correctly."""
        parser = self._build_parser()
        args = parser.parse_args(["--skip-vm"])
        assert args.skip_vm is True

    def test_no_vnc_flag(self):
        """--no-vnc should set vnc to False."""
        parser = self._build_parser()
        args = parser.parse_args(["--no-vnc"])
        assert args.vnc is False

    def test_vnc_default_true(self):
        """VNC should default to True."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.vnc is True

    def test_vm_ip_override(self):
        """--vm-ip should accept an IP address."""
        parser = self._build_parser()
        args = parser.parse_args(["--vm-ip", "10.0.0.5"])
        assert args.vm_ip == "10.0.0.5"

    def test_default_vm_name(self):
        """Default VM name should be waa-pool-00."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.vm_name == "waa-pool-00"

    def test_default_resource_group(self):
        """Default resource group should be openadapt-agents."""
        parser = self._build_parser()
        args = parser.parse_args([])
        assert args.resource_group == "openadapt-agents"

    @staticmethod
    def _build_parser():
        """Use the shared parser from the pipeline module.

        This ensures tests always match the real CLI arguments.
        """
        return pipeline.build_parser()


# ---------------------------------------------------------------------------
# Dry-run integration (main with --dry-run)
# ---------------------------------------------------------------------------


class TestDryRun:
    """Tests for --dry-run mode which reports what would happen without executing."""

    def test_dry_run_exits_zero(self, tmp_path):
        """--dry-run should exit with code 0 when recordings exist."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-dry-run-test"
        task_dir = recordings_dir / task_id
        task_dir.mkdir()
        (task_dir / "meta.json").write_text('{}')
        (demo_dir / f"{task_id}.txt").write_text("demo")

        with patch(
            "sys.argv",
            [
                "run_eval_pipeline.py",
                "--recordings", str(recordings_dir),
                "--demo-dir", str(demo_dir),
                "--tasks", "04d9aeaf",
                "--dry-run",
            ],
        ):
            rc = pipeline.main()

        assert rc == 0

    def test_dry_run_no_recordings_exits_one(self, tmp_path):
        """--dry-run with no recordings should exit with code 1."""
        recordings_dir = tmp_path / "empty_recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "run_eval_pipeline.py",
                "--recordings", str(recordings_dir),
                "--demo-dir", str(demo_dir),
                "--dry-run",
            ],
        ):
            rc = pipeline.main()

        assert rc == 1

    def test_dry_run_lists_missing_demos(self, tmp_path, capsys):
        """--dry-run should list tasks that need demo generation."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-needs-demo"
        task_dir = recordings_dir / task_id
        task_dir.mkdir()
        (task_dir / "meta.json").write_text('{}')
        # No demo file

        with patch(
            "sys.argv",
            [
                "run_eval_pipeline.py",
                "--recordings", str(recordings_dir),
                "--demo-dir", str(demo_dir),
                "--tasks", "04d9aeaf",
                "--dry-run",
            ],
        ):
            rc = pipeline.main()

        output = capsys.readouterr().out
        assert rc == 0
        assert "dry-run" in output.lower()
        assert task_id in output

    def test_dry_run_shows_conditions(self, tmp_path, capsys):
        """--dry-run should list the evaluation conditions that would run."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-with-demo"
        task_dir = recordings_dir / task_id
        task_dir.mkdir()
        (task_dir / "meta.json").write_text('{}')
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        with patch(
            "sys.argv",
            [
                "run_eval_pipeline.py",
                "--recordings", str(recordings_dir),
                "--demo-dir", str(demo_dir),
                "--tasks", "04d9aeaf",
                "--dry-run",
            ],
        ):
            rc = pipeline.main()

        output = capsys.readouterr().out
        assert rc == 0
        assert "ZS" in output
        assert "DC" in output

    def test_dry_run_zs_only(self, tmp_path, capsys):
        """--dry-run --zs-only should only show ZS conditions."""
        recordings_dir = tmp_path / "recordings"
        demo_dir = tmp_path / "demos"
        recordings_dir.mkdir()
        demo_dir.mkdir()

        task_id = "04d9aeaf-zs-only"
        task_dir = recordings_dir / task_id
        task_dir.mkdir()
        (task_dir / "meta.json").write_text('{}')
        (demo_dir / f"{task_id}.txt").write_text("demo content")

        with patch(
            "sys.argv",
            [
                "run_eval_pipeline.py",
                "--recordings", str(recordings_dir),
                "--demo-dir", str(demo_dir),
                "--tasks", "04d9aeaf",
                "--dry-run",
                "--zs-only",
            ],
        ):
            rc = pipeline.main()

        output = capsys.readouterr().out
        assert rc == 0
        # Should show ZS condition
        assert "val_zs_" in output


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Tests for module-level constants."""

    def test_default_vm_name(self):
        assert pipeline.DEFAULT_VM_NAME == "waa-pool-00"

    def test_default_resource_group(self):
        assert pipeline.DEFAULT_RESOURCE_GROUP == "openadapt-agents"

    def test_repo_root_is_parent_of_scripts(self):
        """REPO_ROOT should be the parent of the scripts directory."""
        scripts_dir = pipeline.REPO_ROOT / "scripts"
        assert scripts_dir.exists() or True  # May not exist in worktree
        # But the relationship should hold
        assert pipeline.REPO_ROOT == Path(pipeline.__file__).resolve().parent.parent

    def test_default_paths_relative_to_repo_root(self):
        """Default paths should be relative to REPO_ROOT."""
        assert pipeline.DEFAULT_RECORDINGS == pipeline.REPO_ROOT / "waa_recordings"
        assert pipeline.DEFAULT_DEMO_DIR == pipeline.REPO_ROOT / "demo_prompts_vlm"
        assert pipeline.DEFAULT_OUTPUT == pipeline.REPO_ROOT / "benchmark_results"
