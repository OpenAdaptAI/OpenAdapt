"""Tests for custom task configuration (YAML and WAA JSON)."""

from __future__ import annotations

import io
import json
import os
import textwrap
from unittest.mock import patch

import pytest
from PIL import Image

from openadapt_evals.adapters.base import EvaluationUnavailableError
from openadapt_evals.errors import RolloutEvaluationError
from openadapt_evals.task_config import Milestone, TaskCheck, TaskConfig


def _tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()

# ---------------------------------------------------------------------------
# YAML loading tests
# ---------------------------------------------------------------------------


class TestTaskConfigFromYaml:
    def test_minimal_yaml(self, tmp_path):
        config = tmp_path / "task.yaml"
        config.write_text(
            textwrap.dedent("""\
            name: "Open Notepad"
            evaluate:
              - check: screenshot
                description: "Notepad is open"
            """)
        )
        task = TaskConfig.from_yaml(str(config))
        assert task.name == "Open Notepad"
        assert task.id.startswith("custom-")
        assert task.domain == "desktop"
        assert task.max_steps == 15
        assert task.combine == "and"
        assert len(task.checks) == 1
        assert task.checks[0].check == "screenshot"

    def test_full_yaml(self, tmp_path):
        config = tmp_path / "task.yaml"
        config.write_text(
            textwrap.dedent("""\
            name: "Change font to Arial"
            id: custom-font-001
            domain: desktop
            max_steps: 20
            combine: or

            setup:
              - launch: "soffice --calc"
              - sleep: 3
              - execute: "powershell -c 'echo hello'"

            evaluate:
              - check: command
                run: "powershell -c 'echo Arial'"
                expect: "Arial"
                match: contains
              - check: file
                path: "C:\\\\test.txt"
                exists: true
              - check: screenshot
                description: "Font is Arial"

            milestones:
              - name: "App open"
                check: command
                run: "powershell -c 'Get-Process soffice | Measure | Select -Expand Count'"
                expect: "1"
                match: exact
            """)
        )
        task = TaskConfig.from_yaml(str(config))
        assert task.name == "Change font to Arial"
        assert task.id == "custom-font-001"
        assert task.domain == "desktop"
        assert task.max_steps == 20
        assert task.combine == "or"
        assert len(task.setup) == 3
        assert len(task.checks) == 3
        assert len(task.milestones) == 1
        assert task.milestones[0].name == "App open"

    def test_from_dir(self, tmp_path):
        for i in range(3):
            (tmp_path / f"task{i}.yaml").write_text(
                f"name: Task {i}\nevaluate:\n  - check: screenshot\n    description: ok"
            )
        (tmp_path / "readme.txt").write_text("not a yaml")

        tasks = TaskConfig.from_dir(str(tmp_path))
        assert len(tasks) == 3

    def test_from_dir_skips_invalid(self, tmp_path):
        (tmp_path / "good.yaml").write_text(
            "name: Good\nevaluate:\n  - check: screenshot\n    description: ok"
        )
        (tmp_path / "bad.yaml").write_text("not: valid: yaml: [[[")

        tasks = TaskConfig.from_dir(str(tmp_path))
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# WAA translation tests
# ---------------------------------------------------------------------------


class TestWaaTranslation:
    def _make_task(self, **kwargs) -> TaskConfig:
        defaults = {
            "name": "Test task",
            "id": "test-001",
            "domain": "desktop",
            "setup": [],
            "checks": [],
            "combine": "and",
            "max_steps": 15,
            "milestones": [],
        }
        defaults.update(kwargs)
        return TaskConfig(**defaults)

    def test_command_check_to_waa(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="command", run="echo hello", expect="hello", match="exact")
            ]
        )
        waa = task.to_waa_config()
        evaluator = waa["evaluator"]
        assert evaluator["func"] == "exact_match"
        assert evaluator["result"]["type"] == "vm_command_line"
        assert evaluator["result"]["command"] == "echo hello"
        assert evaluator["expected"]["value"] == "hello"

    def test_contains_match(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="command", run="echo hello world", expect="hello", match="contains")
            ]
        )
        evaluator = task.to_waa_config()["evaluator"]
        assert evaluator["func"] == "contains"

    def test_file_exists_check(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="file", path="C:\\test.txt", exists=True)
            ]
        )
        evaluator = task.to_waa_config()["evaluator"]
        assert evaluator["func"] == "exact_match"
        assert "vm_command_line" in evaluator["result"]["type"]

    def test_file_contains_check(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="file", path="C:\\test.txt", contains="expected content")
            ]
        )
        evaluator = task.to_waa_config()["evaluator"]
        assert evaluator["func"] == "contains"
        assert evaluator["result"]["type"] == "vm_file"

    def test_python_check(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="python", code="print(1 + 1 == 2)")
            ]
        )
        evaluator = task.to_waa_config()["evaluator"]
        assert evaluator["func"] == "exact_match"
        assert evaluator["expected"]["value"] == "True"

    def test_screenshot_only_no_server_evaluator(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="screenshot", description="Notepad is open")
            ]
        )
        waa = task.to_waa_config()
        assert "evaluator" not in waa

    def test_multiple_server_checks_use_conjunction(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="command", run="echo a", expect="a"),
                TaskCheck(check="command", run="echo b", expect="b"),
            ],
            combine="and",
        )
        evaluator = task.to_waa_config()["evaluator"]
        assert isinstance(evaluator["func"], list)
        assert len(evaluator["func"]) == 2
        assert evaluator["conj"] == "and"

    def test_vlm_checks_separated(self):
        task = self._make_task(
            checks=[
                TaskCheck(check="command", run="echo ok", expect="ok"),
                TaskCheck(check="screenshot", description="Looks good"),
            ]
        )
        vlm_checks = task.get_vlm_checks()
        assert len(vlm_checks) == 1
        assert vlm_checks[0].description == "Looks good"

    def test_setup_translation(self):
        task = self._make_task(
            setup=[
                {"launch": "notepad.exe"},
                {"sleep": 2},
                {"download": {"url": "http://example.com/f.txt", "dest": "C:\\f.txt"}},
            ]
        )
        waa = task.to_waa_config()
        config = waa["config"]
        assert config[0]["type"] == "launch"
        assert config[1]["type"] == "sleep"
        assert config[2]["type"] == "download"


# ---------------------------------------------------------------------------
# BenchmarkTask conversion
# ---------------------------------------------------------------------------


class TestBenchmarkTaskConversion:
    def test_to_benchmark_task(self):
        task = TaskConfig(
            name="Test",
            id="test-001",
            domain="desktop",
            setup=[{"launch": "notepad.exe"}],
            checks=[TaskCheck(check="command", run="echo ok", expect="ok")],
            combine="and",
            max_steps=10,
            milestones=[],
        )
        bt = task.to_benchmark_task()
        assert bt.task_id == "test-001"
        assert bt.instruction == "Test"
        assert bt.domain == "desktop"
        assert bt.time_limit_steps == 10
        assert bt.evaluation_spec is not None


# ---------------------------------------------------------------------------
# Milestone evaluation
# ---------------------------------------------------------------------------


class TestMilestones:
    def test_milestone_command_check(self):
        task = TaskConfig(
            name="Test",
            id="test-001",
            domain="desktop",
            setup=[],
            checks=[],
            combine="and",
            max_steps=15,
            milestones=[
                Milestone(
                    name="App open",
                    check=TaskCheck(check="command", run="echo 1", expect="1", match="exact"),
                ),
                Milestone(
                    name="File saved",
                    check=TaskCheck(check="command", run="echo 0", expect="1", match="exact"),
                ),
            ],
        )

        with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
            mock_cmd.side_effect = ["1", "0"]
            passed, total = task.evaluate_milestones(b"fake-screenshot", "http://localhost:5001")

        assert total == 2
        assert passed == 1  # first matches, second doesn't

    @patch("openadapt_evals.vlm_evaluator.vlm_judge")
    def test_milestone_screenshot_check(self, mock_vlm):
        mock_vlm.return_value = (True, 0.95)

        task = TaskConfig(
            name="Test",
            id="test-001",
            domain="desktop",
            setup=[],
            checks=[],
            combine="and",
            max_steps=15,
            milestones=[
                Milestone(
                    name="App visible",
                    check=TaskCheck(check="screenshot", description="Notepad is open"),
                ),
            ],
        )

        passed, total = task.evaluate_milestones(b"fake-screenshot", "http://localhost:5001")
        assert total == 1
        assert passed == 1
        mock_vlm.assert_called_once()

    def test_no_milestones(self):
        task = TaskConfig(
            name="Test",
            id="test-001",
            domain="desktop",
            setup=[],
            checks=[],
            combine="and",
            max_steps=15,
            milestones=[],
        )
        with pytest.raises(EvaluationUnavailableError, match="No milestone"):
            task.evaluate_milestones(_tiny_png(), "http://localhost:5001")

    def test_unmeasured_milestone_is_not_counted_as_failed(self):
        task = TaskConfig(
            name="Test",
            id="test-001",
            domain="desktop",
            setup=[],
            checks=[],
            combine="and",
            max_steps=15,
            milestones=[
                Milestone(name="Unknown", check=TaskCheck(check="file")),
            ],
        )

        with pytest.raises(EvaluationUnavailableError, match="could not be evaluated"):
            task.evaluate_milestones(_tiny_png(), "http://localhost:5001")


# ---------------------------------------------------------------------------
# VLM evaluator tests
# ---------------------------------------------------------------------------


class TestVlmEvaluator:
    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_yes(self, mock_vlm):
        mock_vlm.return_value = '{"verdict": "YES", "confidence": 0.95, "explanation": "Font is Arial"}'
        from openadapt_evals.vlm_evaluator import vlm_judge

        success, confidence = vlm_judge(_tiny_png(), "Font is Arial")
        assert success is True
        assert confidence == 0.95

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_no(self, mock_vlm):
        mock_vlm.return_value = '{"verdict": "NO", "confidence": 0.8, "explanation": "Font is Times"}'
        from openadapt_evals.vlm_evaluator import vlm_judge

        success, confidence = vlm_judge(_tiny_png(), "Font is Arial")
        assert success is False
        assert confidence == 0.8

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_bad_json_is_unmeasured(self, mock_vlm):
        mock_vlm.return_value = "YES, the font looks like Arial to me."
        from openadapt_evals.vlm_evaluator import vlm_judge

        with pytest.raises(RolloutEvaluationError, match="required JSON"):
            vlm_judge(_tiny_png(), "Font is Arial")

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_rejects_non_exact_verdict(self, mock_vlm):
        mock_vlm.return_value = '{"verdict":"YES, probably","confidence":0.8}'
        from openadapt_evals.vlm_evaluator import vlm_judge

        with pytest.raises(RolloutEvaluationError, match="exactly"):
            vlm_judge(_tiny_png(), "Font is Arial")

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), "0.9", True])
    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_rejects_invalid_confidence(self, mock_vlm, confidence):
        mock_vlm.return_value = json.dumps(
            {"verdict": "YES", "confidence": confidence}
        )
        from openadapt_evals.vlm_evaluator import vlm_judge

        with pytest.raises(RolloutEvaluationError, match="confidence"):
            vlm_judge(_tiny_png(), "Font is Arial")

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_judge_rejects_unreadable_screenshot_before_model_call(self, mock_vlm):
        from openadapt_evals.vlm_evaluator import vlm_judge

        with pytest.raises(RolloutEvaluationError, match="decodable"):
            vlm_judge(b"not an image", "Font is Arial")
        mock_vlm.assert_not_called()


# ---------------------------------------------------------------------------
# Example YAML files load correctly
# ---------------------------------------------------------------------------


class TestExampleTasks:
    def test_load_example_tasks(self):
        example_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "example_tasks"
        )
        if not os.path.isdir(example_dir):
            pytest.skip("example_tasks/ not found")

        tasks = TaskConfig.from_dir(example_dir)
        assert len(tasks) >= 1
        for task in tasks:
            assert task.name
            assert task.id
            bt = task.to_benchmark_task()
            assert bt.task_id == task.id


# ---------------------------------------------------------------------------
# WAA JSON import tests
# ---------------------------------------------------------------------------

# Sample WAA JSON fixtures used across multiple tests.

WAA_SIMPLE_COMMAND = {
    "id": "notepad-hello-001",
    "instruction": "Type hello in Notepad",
    "related_apps": ["notepad"],
    "snapshot": "notepad",
    "config": [
        {"type": "launch", "parameters": {"command": "notepad.exe"}},
        {"type": "sleep", "parameters": {"seconds": 2}},
    ],
    "evaluator": {
        "func": "exact_match",
        "result": {"type": "vm_command_line", "command": "echo hello"},
        "expected": {"type": "literal", "value": "hello"},
    },
}

WAA_FILE_CONTAINS = {
    "id": "file-check-001",
    "instruction": "Create a file with content",
    "config": [],
    "evaluator": {
        "func": "contains",
        "result": {"type": "vm_file", "path": "C:\\Users\\Docker\\test.txt"},
        "expected": {"type": "literal", "value": "expected text"},
    },
}

WAA_MULTI_METRIC = {
    "id": "multi-check-001",
    "instruction": "Perform two operations",
    "config": [
        {"type": "execute", "parameters": {"command": "echo setup"}},
    ],
    "evaluator": {
        "func": ["exact_match", "contains"],
        "result": [
            {"type": "vm_command_line", "command": "echo a"},
            {"type": "vm_command_line", "command": "echo a b c"},
        ],
        "expected": [
            {"type": "literal", "value": "a"},
            {"type": "literal", "value": "b"},
        ],
        "conj": "and",
    },
}

WAA_SPECIALISED_EVALUATOR = {
    "id": "font-change-001",
    "instruction": "Change font to Times New Roman",
    "related_apps": ["libreoffice_writer"],
    "snapshot": "libreoffice_writer",
    "config": [
        {
            "type": "download",
            "parameters": {
                "files": [
                    {
                        "path": "C:\\Users\\Docker\\Downloads\\Doc.docx",
                        "url": "https://example.com/Doc.docx",
                    }
                ]
            },
        },
        {"type": "open", "parameters": {"path": "C:\\Users\\Docker\\Downloads\\Doc.docx"}},
    ],
    "evaluator": {
        "func": "compare_font_names",
        "result": {
            "type": "vm_file",
            "path": "C:\\Users\\Docker\\Downloads\\Doc.docx",
            "dest": "Doc.docx",
        },
        "expected": {
            "type": "rule",
            "rules": {"font_name": "Times New Roman"},
        },
        "postconfig": [
            {"type": "activate_window", "parameters": {"window_name": "Doc.docx", "strict": True}},
            {"type": "sleep", "parameters": {"seconds": 0.5}},
        ],
    },
}

WAA_DOWNLOAD_SINGLE = {
    "id": "dl-single-001",
    "instruction": "Download and check a file",
    "config": [
        {
            "type": "download",
            "parameters": {"url": "https://example.com/f.txt", "path": "C:\\f.txt"},
        },
    ],
    "evaluator": {
        "func": "exact_match",
        "result": {"type": "vm_command_line", "command": "type C:\\f.txt"},
        "expected": {"type": "literal", "value": "contents"},
    },
}

WAA_LAUNCH_LIST_CMD = {
    "id": "vscode-001",
    "instruction": "Open VS Code",
    "related_apps": ["vscode"],
    "config": [
        {"type": "launch", "parameters": {"command": ["code", "--new-window"]}},
    ],
}


class TestWaaJsonImport:
    """Tests for loading WAA native JSON task configs."""

    def test_simple_command_check(self, tmp_path):
        """Simple evaluator: exact_match + vm_command_line + literal."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SIMPLE_COMMAND))

        task = TaskConfig.from_waa_json(str(f))
        assert task.id == "notepad-hello-001"
        assert task.name == "Type hello in Notepad"
        assert task.domain == "notepad"
        assert len(task.setup) == 2
        assert task.setup[0] == {"launch": "notepad.exe"}
        assert task.setup[1] == {"sleep": 2}
        assert len(task.checks) == 1
        assert task.checks[0].check == "command"
        assert task.checks[0].run == "echo hello"
        assert task.checks[0].expect == "hello"
        assert task.checks[0].match == "exact"
        # No raw evaluator for simple patterns
        assert task._raw_evaluator is None

    def test_file_contains_check(self, tmp_path):
        """contains + vm_file + literal -> file check."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_FILE_CONTAINS))

        task = TaskConfig.from_waa_json(str(f))
        assert len(task.checks) == 1
        assert task.checks[0].check == "file"
        assert task.checks[0].path == "C:\\Users\\Docker\\test.txt"
        assert task.checks[0].contains == "expected text"

    def test_multi_metric(self, tmp_path):
        """Multiple metrics with conjunction."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_MULTI_METRIC))

        task = TaskConfig.from_waa_json(str(f))
        assert len(task.checks) == 2
        assert task.combine == "and"
        assert task.checks[0].match == "exact"
        assert task.checks[1].match == "contains"

    def test_specialised_evaluator_preserved(self, tmp_path):
        """Specialised WAA evaluators are preserved as raw_evaluator."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SPECIALISED_EVALUATOR))

        task = TaskConfig.from_waa_json(str(f))
        assert task.id == "font-change-001"
        assert task.domain == "libreoffice_writer"
        assert len(task.checks) == 0  # not reverse-translated
        assert task._raw_evaluator is not None
        assert task._raw_evaluator["func"] == "compare_font_names"
        assert "postconfig" in task._raw_evaluator

    def test_setup_download_with_files_array(self, tmp_path):
        """Download config with 'files' array parameter."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SPECIALISED_EVALUATOR))

        task = TaskConfig.from_waa_json(str(f))
        assert len(task.setup) == 2
        assert "download" in task.setup[0]
        assert task.setup[0]["download"]["url"] == "https://example.com/Doc.docx"
        assert task.setup[0]["download"]["dest"] == "C:\\Users\\Docker\\Downloads\\Doc.docx"
        assert "open" in task.setup[1]

    def test_setup_download_single_file(self, tmp_path):
        """Download config with direct url/path parameters."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_DOWNLOAD_SINGLE))

        task = TaskConfig.from_waa_json(str(f))
        assert "download" in task.setup[0]
        assert task.setup[0]["download"]["url"] == "https://example.com/f.txt"
        assert task.setup[0]["download"]["dest"] == "C:\\f.txt"

    def test_launch_with_list_command(self, tmp_path):
        """Launch command given as list is joined."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_LAUNCH_LIST_CMD))

        task = TaskConfig.from_waa_json(str(f))
        assert task.setup[0] == {"launch": "code --new-window"}

    def test_no_evaluator(self, tmp_path):
        """JSON with no evaluator -> empty checks, no raw_evaluator."""
        data = {"id": "no-eval", "instruction": "Do nothing", "config": []}
        f = tmp_path / "task.json"
        f.write_text(json.dumps(data))

        task = TaskConfig.from_waa_json(str(f))
        assert task.checks == []
        assert task._raw_evaluator is None

    def test_domain_from_parent_dir(self, tmp_path):
        """Domain inferred from parent directory when no related_apps."""
        writer_dir = tmp_path / "writer"
        writer_dir.mkdir()
        f = writer_dir / "task.json"
        f.write_text(json.dumps({"id": "w-001", "instruction": "Write", "config": []}))

        task = TaskConfig.from_waa_json(str(f))
        assert task.domain == "writer"

    def test_domain_from_related_apps_calc(self, tmp_path):
        """Domain inferred from related_apps containing 'calc'."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps({
            "id": "c-001",
            "instruction": "Calculate",
            "related_apps": ["libreoffice-calc"],
            "config": [],
        }))

        task = TaskConfig.from_waa_json(str(f))
        assert task.domain == "libreoffice_calc"


class TestWaaJsonRoundTrip:
    """Test that WAA JSON -> TaskConfig -> to_waa_config() round-trips."""

    def test_simple_round_trip(self, tmp_path):
        """Simple evaluator round-trips through checks."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SIMPLE_COMMAND))

        task = TaskConfig.from_waa_json(str(f))
        waa = task.to_waa_config()

        assert waa["task_id"] == "notepad-hello-001"
        assert waa["instruction"] == "Type hello in Notepad"
        evaluator = waa["evaluator"]
        assert evaluator["func"] == "exact_match"
        assert evaluator["result"]["type"] == "vm_command_line"
        assert evaluator["result"]["command"] == "echo hello"
        assert evaluator["expected"]["type"] == "literal"
        assert evaluator["expected"]["value"] == "hello"

    def test_specialised_round_trip(self, tmp_path):
        """Specialised evaluators round-trip via raw_evaluator."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SPECIALISED_EVALUATOR))

        task = TaskConfig.from_waa_json(str(f))
        waa = task.to_waa_config()

        # Evaluator should be preserved exactly
        assert waa["evaluator"] == WAA_SPECIALISED_EVALUATOR["evaluator"]

    def test_multi_metric_round_trip(self, tmp_path):
        """Multi-metric evaluator round-trips via checks."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_MULTI_METRIC))

        task = TaskConfig.from_waa_json(str(f))
        waa = task.to_waa_config()

        evaluator = waa["evaluator"]
        assert isinstance(evaluator["func"], list)
        assert evaluator["func"] == ["exact_match", "contains"]
        assert evaluator["conj"] == "and"

    def test_setup_round_trip(self, tmp_path):
        """Setup config round-trips: YAML-style -> WAA format."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SIMPLE_COMMAND))

        task = TaskConfig.from_waa_json(str(f))
        waa = task.to_waa_config()

        config = waa["config"]
        assert len(config) == 2
        assert config[0]["type"] == "launch"
        assert config[0]["parameters"]["command"] == "notepad.exe"
        assert config[1]["type"] == "sleep"
        assert config[1]["parameters"]["seconds"] == 2.0

    def test_benchmark_task_from_waa_json(self, tmp_path):
        """WAA JSON -> TaskConfig -> BenchmarkTask works."""
        f = tmp_path / "task.json"
        f.write_text(json.dumps(WAA_SPECIALISED_EVALUATOR))

        task = TaskConfig.from_waa_json(str(f))
        bt = task.to_benchmark_task()
        assert bt.task_id == "font-change-001"
        assert bt.instruction == "Change font to Times New Roman"
        assert bt.evaluation_spec is not None
        assert bt.evaluation_spec["func"] == "compare_font_names"


class TestWaaDir:
    """Tests for loading WAA examples directory tree."""

    def test_from_waa_dir(self, tmp_path):
        """Load all JSONs from nested domain directories."""
        calc_dir = tmp_path / "calc"
        calc_dir.mkdir()
        writer_dir = tmp_path / "writer"
        writer_dir.mkdir()

        (calc_dir / "task1.json").write_text(json.dumps({
            "id": "calc-001", "instruction": "Sum cells", "config": [],
        }))
        (writer_dir / "task2.json").write_text(json.dumps({
            "id": "writer-001", "instruction": "Change font", "config": [],
        }))
        # Non-JSON file should be ignored
        (tmp_path / "README.md").write_text("ignore me")

        tasks = TaskConfig.from_waa_dir(str(tmp_path))
        assert len(tasks) == 2
        ids = {t.id for t in tasks}
        assert "calc-001" in ids
        assert "writer-001" in ids

    def test_from_waa_dir_nonexistent(self, tmp_path):
        """Non-existent directory returns empty list."""
        tasks = TaskConfig.from_waa_dir(str(tmp_path / "no_such_dir"))
        assert tasks == []

    def test_from_waa_dir_skips_invalid(self, tmp_path):
        """Invalid JSON files are skipped with a warning."""
        (tmp_path / "good.json").write_text(json.dumps({
            "id": "good", "instruction": "Good task", "config": [],
        }))
        (tmp_path / "bad.json").write_text("{invalid json")

        tasks = TaskConfig.from_waa_dir(str(tmp_path))
        assert len(tasks) == 1
        assert tasks[0].id == "good"


class TestFromDirMixed:
    """Tests for from_dir loading both YAML and JSON."""

    def test_mixed_yaml_and_json(self, tmp_path):
        """from_dir loads both .yaml and .json files."""
        (tmp_path / "task1.yaml").write_text(
            "name: YAML Task\nevaluate:\n  - check: screenshot\n    description: ok"
        )
        (tmp_path / "task2.json").write_text(json.dumps({
            "id": "json-001", "instruction": "JSON Task", "config": [],
        }))
        (tmp_path / "readme.txt").write_text("not a task")

        tasks = TaskConfig.from_dir(str(tmp_path))
        assert len(tasks) == 2
        names = {t.name for t in tasks}
        assert "YAML Task" in names
        assert "JSON Task" in names

    def test_json_skipped_on_error(self, tmp_path):
        """Invalid JSON is skipped, valid YAML still loads."""
        (tmp_path / "good.yaml").write_text(
            "name: Good\nevaluate:\n  - check: screenshot\n    description: ok"
        )
        (tmp_path / "bad.json").write_text("{broken")

        tasks = TaskConfig.from_dir(str(tmp_path))
        assert len(tasks) == 1
        assert tasks[0].name == "Good"
