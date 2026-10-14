"""Tests for WAA benchmark adapter."""

from unittest.mock import MagicMock

import pytest

from openadapt_evals import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    RandomAgent,
    ScriptedAgent,
    WAAAdapter,
    WAAMockAdapter,
    compute_metrics,
    evaluate_agent_on_benchmark,
)
from openadapt_evals.errors import RolloutEvaluationError, RolloutInfrastructureError


class TestWAAAdapterEvaluation:
    """Tests for strict native WAA evaluator result handling."""

    @staticmethod
    def _adapter(tmp_path, response=None, error=None):
        adapter = WAAAdapter(waa_repo_path=tmp_path)
        adapter._desktop_env = MagicMock()
        if error is not None:
            adapter._desktop_env.evaluate.side_effect = error
        else:
            adapter._desktop_env.evaluate.return_value = response
        return adapter

    @pytest.mark.parametrize(
        "response",
        [
            None,
            [],
            {},
            True,
            float("nan"),
            float("inf"),
            -0.1,
            1.1,
            {"success": 1, "score": 1.0},
            {"success": True, "score": True},
            {"success": False, "score": float("nan")},
            {"success": False, "score": float("inf")},
            {"success": False, "score": -0.1},
            {"success": True, "score": 1.1},
            {"success": True, "score": 0.5},
            {"success": False, "score": 1.0},
        ],
    )
    def test_malformed_measurement_stays_unscored(self, tmp_path, response):
        task = BenchmarkTask(task_id="task", instruction="test", domain="desktop")
        result = self._adapter(tmp_path, response=response).evaluate(task)

        assert result.success is False
        assert result.score == 0.0
        assert result.error_type == "evaluation"
        assert result.reason and result.reason.startswith(
            "Malformed evaluation response:"
        )

    @pytest.mark.parametrize("error_type", ["infrastructure", "evaluation"])
    def test_unavailable_response_preserves_type(self, tmp_path, error_type):
        task = BenchmarkTask(task_id="task", instruction="test", domain="desktop")
        response = {
            "success": False,
            "score": 0.0,
            "scored": False,
            "error_type": error_type,
            "reason": "evaluator unavailable",
        }

        result = self._adapter(tmp_path, response=response).evaluate(task)

        assert (result.success, result.score, result.error_type) == (
            False,
            0.0,
            error_type,
        )
        assert result.reason == "evaluator unavailable"

    @pytest.mark.parametrize(
        ("error", "expected_type"),
        [
            (RuntimeError("invalid evaluator"), "evaluation"),
            (RolloutInfrastructureError("VM offline"), "infrastructure"),
            (RolloutEvaluationError("oracle offline"), "evaluation"),
        ],
    )
    def test_evaluator_exception_preserves_stable_type(
        self, tmp_path, error, expected_type
    ):
        task = BenchmarkTask(task_id="task", instruction="test", domain="desktop")

        result = self._adapter(tmp_path, error=error).evaluate(task)

        assert result.success is False
        assert result.score == 0.0
        assert result.error_type == expected_type
        assert result.error == str(error)

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ({"success": False, "score": 0.25}, (False, 0.25)),
            ({"success": True, "score": 1.0}, (True, 1.0)),
        ],
    )
    def test_valid_measurement_is_preserved(self, tmp_path, response, expected):
        task = BenchmarkTask(task_id="task", instruction="test", domain="desktop")

        result = self._adapter(tmp_path, response=response).evaluate(task)

        assert (result.success, result.score) == expected
        assert result.error_type is None

    @pytest.mark.parametrize(
        ("response", "expected"),
        [(0.0, (False, 0.0)), (0.25, (False, 0.25)), (1.0, (True, 1.0))],
    )
    def test_native_scalar_measurement_is_preserved(
        self, tmp_path, response, expected
    ):
        task = BenchmarkTask(task_id="task", instruction="test", domain="desktop")

        result = self._adapter(tmp_path, response=response).evaluate(task)

        assert (result.success, result.score) == expected
        assert result.error_type is None


class TestWAAMockAdapter:
    """Tests for WAAMockAdapter."""

    def test_list_tasks(self):
        """Test listing tasks."""
        adapter = WAAMockAdapter(num_tasks=10)
        tasks = adapter.list_tasks()
        assert len(tasks) == 10
        assert all(isinstance(t, BenchmarkTask) for t in tasks)

    def test_list_tasks_by_domain(self):
        """Test filtering tasks by domain."""
        adapter = WAAMockAdapter(num_tasks=10, domains=["browser", "notepad"])
        browser_tasks = adapter.list_tasks(domain="browser")
        assert len(browser_tasks) == 5
        assert all(t.domain == "browser" for t in browser_tasks)

    def test_load_task(self):
        """Test loading a specific task."""
        adapter = WAAMockAdapter(num_tasks=5, domains=["browser"])
        # Mock adapter uses "mock_{domain}_{number:03d}" format
        task = adapter.load_task("mock_browser_001")
        assert task.task_id == "mock_browser_001"
        assert task.domain == "browser"

    def test_load_task_not_found(self):
        """Test loading a non-existent task."""
        adapter = WAAMockAdapter(num_tasks=5)
        with pytest.raises(KeyError):
            adapter.load_task("nonexistent_task")

    def test_reset_returns_observation(self):
        """Test reset returns observation."""
        adapter = WAAMockAdapter(num_tasks=5)
        task = adapter.list_tasks()[0]
        obs = adapter.reset(task)
        assert isinstance(obs, BenchmarkObservation)
        assert obs.viewport is not None
        assert obs.accessibility_tree is not None

    def test_step_returns_observation_done_info(self):
        """Test step returns correct tuple."""
        adapter = WAAMockAdapter(num_tasks=5)
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        obs, done, info = adapter.step(action)

        assert isinstance(obs, BenchmarkObservation)
        assert isinstance(done, bool)
        assert isinstance(info, dict)

    def test_step_done_on_max_steps(self):
        """Test step returns done after max steps."""
        adapter = WAAMockAdapter(num_tasks=5)
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        action = BenchmarkAction(type="click", x=0.5, y=0.5)
        for _ in range(14):
            obs, done, info = adapter.step(action)
            assert not done

        obs, done, info = adapter.step(action)
        assert done

    def test_step_done_on_done_action(self):
        """Test step returns done when action is 'done'."""
        adapter = WAAMockAdapter(num_tasks=5)
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        action = BenchmarkAction(type="done")
        obs, done, info = adapter.step(action)
        assert done

    def test_evaluate_returns_result(self):
        """Test evaluate returns BenchmarkResult."""
        adapter = WAAMockAdapter(num_tasks=5)
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        result = adapter.evaluate(task)
        assert isinstance(result, BenchmarkResult)
        assert result.task_id == task.task_id
        assert result.score in [0.0, 1.0]

    def test_benchmark_properties(self):
        """Test benchmark properties."""
        adapter = WAAMockAdapter()
        assert adapter.name == "waa-mock"
        assert adapter.benchmark_type == "interactive"


class TestEvaluationRunner:
    """Tests for evaluation runner with mock adapter."""

    def test_evaluate_with_random_agent(self):
        """Test running evaluation with RandomAgent."""
        adapter = WAAMockAdapter(num_tasks=5)
        agent = RandomAgent(seed=42)

        results = evaluate_agent_on_benchmark(agent, adapter, max_steps=10)

        assert len(results) == 5
        assert all(isinstance(r, BenchmarkResult) for r in results)

    def test_evaluate_specific_tasks(self):
        """Test evaluating specific tasks."""
        adapter = WAAMockAdapter(num_tasks=10, domains=["browser", "notepad"])
        agent = RandomAgent(seed=42)

        # Mock adapter uses "mock_{domain}_{number:03d}" format
        results = evaluate_agent_on_benchmark(
            agent, adapter, task_ids=["mock_browser_001", "mock_browser_002"], max_steps=10
        )

        assert len(results) == 2
        assert results[0].task_id == "mock_browser_001"
        assert results[1].task_id == "mock_browser_002"

    def test_evaluate_with_scripted_agent(self):
        """Test running evaluation with ScriptedAgent."""
        adapter = WAAMockAdapter(num_tasks=3)

        # Create script that clicks then says done
        actions = [
            BenchmarkAction(type="click", x=0.5, y=0.5),
            BenchmarkAction(type="type", text="hello"),
            BenchmarkAction(type="done"),
        ]
        agent = ScriptedAgent(actions=actions)

        results = evaluate_agent_on_benchmark(agent, adapter, max_steps=10)

        assert len(results) == 3


class TestComputeMetrics:
    """Tests for compute_metrics function."""

    def test_compute_metrics_empty(self):
        """Test metrics with empty results."""
        metrics = compute_metrics([])
        assert metrics["num_tasks"] == 0
        assert metrics["success_rate"] == 0.0

    def test_compute_metrics_all_success(self):
        """Test metrics with all successful tasks."""
        results = [
            BenchmarkResult(task_id=f"task_{i}", success=True, score=1.0, num_steps=5)
            for i in range(3)
        ]
        metrics = compute_metrics(results)
        assert metrics["num_tasks"] == 3
        assert metrics["success_rate"] == 1.0
        assert metrics["avg_score"] == 1.0

    def test_compute_metrics_all_fail(self):
        """Test metrics with all failed tasks."""
        results = [
            BenchmarkResult(task_id=f"task_{i}", success=False, score=0.0, num_steps=10)
            for i in range(3)
        ]
        metrics = compute_metrics(results)
        assert metrics["num_tasks"] == 3
        assert metrics["success_rate"] == 0.0
        assert metrics["avg_score"] == 0.0

    def test_compute_metrics_partial_success(self):
        """Test metrics with partial success."""
        results = [
            BenchmarkResult(task_id="task_1", success=True, score=1.0, num_steps=5),
            BenchmarkResult(task_id="task_2", success=False, score=0.0, num_steps=10),
            BenchmarkResult(task_id="task_3", success=True, score=1.0, num_steps=3),
            BenchmarkResult(task_id="task_4", success=False, score=0.0, num_steps=8),
        ]
        metrics = compute_metrics(results)
        assert metrics["num_tasks"] == 4
        assert metrics["success_rate"] == 0.5
        assert metrics["success_count"] == 2
        assert metrics["fail_count"] == 2


# ---------------------------------------------------------------------------
# XML Accessibility Tree Parsing
# ---------------------------------------------------------------------------


class TestParseXmlA11yTree:
    """Tests for _parse_xml_a11y_tree in the live adapter."""

    def test_basic_xml(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        xml = (
            '<Window Name="Notepad" AutomationId="NotepadWindow"'
            ' BoundingRectangle="0,0,1920,1200">'
            '<Edit Name="Text Editor" AutomationId="15"'
            ' BoundingRectangle="0,40,1920,1170"/>'
            "</Window>"
        )
        result = _parse_xml_a11y_tree(xml)
        assert result is not None
        assert result["role"] == "Window"
        assert result["name"] == "Notepad"
        assert result["id"] == "NotepadWindow"
        assert result["BoundingRectangle"] == "0,0,1920,1200"
        assert len(result["children"]) == 1
        child = result["children"][0]
        assert child["role"] == "Edit"
        assert child["name"] == "Text Editor"
        assert child["id"] == "15"

    def test_nested_xml(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        xml = (
            '<Window Name="App" AutomationId="app1">'
            '<Pane Name="Toolbar">'
            '<Button Name="Save" AutomationId="saveBtn"'
            ' BoundingRectangle="10,10,60,40"/>'
            '<Button Name="Open" AutomationId="openBtn"'
            ' BoundingRectangle="70,10,120,40"/>'
            "</Pane>"
            "</Window>"
        )
        result = _parse_xml_a11y_tree(xml)
        assert result["children"][0]["role"] == "Pane"
        buttons = result["children"][0]["children"]
        assert len(buttons) == 2
        assert buttons[0]["id"] == "saveBtn"
        assert buttons[1]["id"] == "openBtn"
        assert buttons[0]["BoundingRectangle"] == "10,10,60,40"

    def test_invalid_xml_returns_none(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        result = _parse_xml_a11y_tree("not valid xml <><>")
        assert result is None

    def test_empty_string_returns_none(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        result = _parse_xml_a11y_tree("")
        assert result is None

    def test_runtime_id_fallback(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        xml = '<Button Name="OK" RuntimeId="42" BoundingRectangle="100,100,200,140"/>'
        result = _parse_xml_a11y_tree(xml)
        assert result["id"] == "42"

    def test_no_id_element(self):
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        xml = '<Pane Name="Content"/>'
        result = _parse_xml_a11y_tree(xml)
        assert result is not None
        # Name is used as fallback ID when AutomationId/RuntimeId are absent
        assert result["id"] == "Content"
        assert result["name"] == "Content"

    def test_atspi_format_parsing(self):
        """AT-SPI format with lowercase name and cp:screencoord/cp:size."""
        from openadapt_evals.adapters.waa.live import _parse_xml_a11y_tree

        xml = (
            '<desktop xmlns:cp="uri:deskat:component.at-spi.gnome.org"'
            ' xmlns:st="uri:deskat:state.at-spi.gnome.org">'
            '<togglebutton name="Start" st:enabled="true" st:visible="true"'
            ' cp:screencoord="(418, 672)" cp:size="(45, 48)"/>'
            '<togglebutton name="Search" st:enabled="true" st:visible="true"'
            ' cp:screencoord="(465, 680)" cp:size="(220, 32)"/>'
            '</desktop>'
        )
        result = _parse_xml_a11y_tree(xml)
        assert result is not None
        assert len(result["children"]) == 2
        start = result["children"][0]
        assert start["name"] == "Start"
        assert start["id"] == "Start"
        assert start["BoundingRectangle"] == "418,672,463,720"
        search = result["children"][1]
        assert search["name"] == "Search"
        assert search["id"] == "Search"
        assert search["BoundingRectangle"] == "465,680,685,712"

    def test_atspi_rect_extraction(self):
        """AT-SPI XML should produce usable rects for element grounding."""
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        xml = (
            '<desktop xmlns:cp="uri:deskat:component.at-spi.gnome.org">'
            '<togglebutton name="Start"'
            ' cp:screencoord="(418, 672)" cp:size="(45, 48)"/>'
            '<togglebutton name="Search"'
            ' cp:screencoord="(465, 680)" cp:size="(220, 32)"/>'
            '</desktop>'
        )
        adapter = WAALiveAdapter.__new__(WAALiveAdapter)
        adapter.config = WAALiveConfig()
        adapter._current_rects = {}

        rects = adapter._extract_rects_from_a11y(xml)
        assert "Start" in rects
        assert rects["Start"] == [418, 672, 463, 720]
        assert "Search" in rects
        assert rects["Search"] == [465, 680, 685, 712]

    def test_xml_rect_extraction_integration(self):
        """XML a11y tree should produce usable rects via _extract_rects_from_a11y."""
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        xml = (
            '<Window Name="App" AutomationId="win1" BoundingRectangle="0,0,1920,1200">'
            '<Button Name="Submit" AutomationId="submitBtn"'
            ' BoundingRectangle="400,100,500,140"/>'
            "</Window>"
        )
        # Create adapter (won't connect to server)
        adapter = WAALiveAdapter.__new__(WAALiveAdapter)
        adapter.config = WAALiveConfig()
        adapter._current_rects = {}

        rects = adapter._extract_rects_from_a11y(xml)
        assert "submitBtn" in rects
        # BoundingRectangle "400,100,500,140" → [400, 100, 500, 140]
        assert rects["submitBtn"] == [400, 100, 500, 140]


class TestFormatAccessibilityTree:
    """Tests for _format_accessibility_tree in api_agent."""

    def test_format_parsed_atspi_dict(self):
        """Parsed AT-SPI dict should produce [ID] role: name format."""
        from openadapt_evals.agents.api_agent import _format_accessibility_tree

        tree = {
            "role": "desktop",
            "name": "",
            "id": "",
            "children": [
                {
                    "role": "togglebutton",
                    "name": "Start",
                    "id": "Start",
                    "BoundingRectangle": "418,672,463,720",
                    "children": [],
                },
                {
                    "role": "togglebutton",
                    "name": "Search",
                    "id": "Search",
                    "BoundingRectangle": "465,680,685,712",
                    "children": [],
                },
            ],
        }
        result = _format_accessibility_tree(tree)
        assert "[Start] togglebutton: Start" in result
        assert "[Search] togglebutton: Search" in result
        assert "[418,672,463,720]" in result
        assert "[465,680,685,712]" in result

    def test_format_uia_dict(self):
        """UIA-style dict should also format correctly."""
        from openadapt_evals.agents.api_agent import _format_accessibility_tree

        tree = {
            "role": "Window",
            "name": "Notepad",
            "id": "NotepadWindow",
            "BoundingRectangle": "0,0,1920,1200",
            "children": [
                {
                    "role": "Edit",
                    "name": "Text Editor",
                    "id": "15",
                    "BoundingRectangle": "0,40,1920,1170",
                    "children": [],
                },
            ],
        }
        result = _format_accessibility_tree(tree)
        assert "[NotepadWindow] Window: Notepad" in result
        assert "[15] Edit: Text Editor" in result
        assert "[0,0,1920,1200]" in result


# ---------------------------------------------------------------------------
# Recording Script Argument Parsing
# ---------------------------------------------------------------------------


class TestRecordWaaArgParsing:
    """Tests for cmd_record_waa argument validation."""

    def test_tasks_bool_rejected(self, capsys):
        """Fire passes True when --tasks has no value; should print error."""
        from scripts.record_waa_demos import cmd_record_waa

        cmd_record_waa(tasks=True, server="http://fake", verify=False)
        captured = capsys.readouterr()
        assert "ERROR" in captured.out
        assert "--tasks" in captured.out

    def test_tasks_prefix_matching(self):
        """Prefix matching should resolve short IDs to full HARDER_TASK_IDS."""
        from openadapt_evals.constants import HARDER_TASK_IDS

        # "04d9aeaf" should match "04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS"
        full = [t for t in HARDER_TASK_IDS if t.startswith("04d9aeaf")]
        assert len(full) == 1
        assert full[0].startswith("04d9aeaf-")


class TestFailsafeDetection:
    """Tests for _is_failsafe_error in the live adapter."""

    def test_detects_failsafe_exception(self):
        from openadapt_evals.adapters.waa.live import _is_failsafe_error

        assert _is_failsafe_error("pyautogui.FailSafeException: blah")

    def test_detects_lowercase(self):
        from openadapt_evals.adapters.waa.live import _is_failsafe_error

        assert _is_failsafe_error("failsafeexception occurred")

    def test_no_false_positive_on_normal_stderr(self):
        from openadapt_evals.adapters.waa.live import _is_failsafe_error

        assert not _is_failsafe_error("Command completed successfully")
        assert not _is_failsafe_error("Warning: file not found")
        assert not _is_failsafe_error("")

    def test_no_false_positive_on_generic_fail_safe_text(self):
        from openadapt_evals.adapters.waa.live import _is_failsafe_error

        assert not _is_failsafe_error("The application has a fail-safe mechanism")


# ---------------------------------------------------------------------------
# _escape_for_pyautogui
# ---------------------------------------------------------------------------


class TestBuildTypeCommands:
    """Tests for _build_type_commands which uses repr() for escaping."""

    def test_simple_text(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("hello")
        assert result == "pyautogui.write('hello', interval=0.02)"

    def test_text_with_newline(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("abc\ndef")
        assert "pyautogui.write('abc', interval=0.02)" in result
        assert "pyautogui.press('enter')" in result
        assert "pyautogui.write('def', interval=0.02)" in result

    def test_trailing_newline(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("abc\n")
        assert "pyautogui.write('abc', interval=0.02)" in result
        assert "pyautogui.press('enter')" in result
        # Should NOT have a write('') after the enter
        assert result.endswith("pyautogui.press('enter')")

    def test_leading_newline(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("\nabc")
        assert result.startswith("pyautogui.press('enter')")
        assert "pyautogui.write('abc', interval=0.02)" in result

    def test_just_newline(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("\n")
        assert result == "pyautogui.press('enter')"

    def test_empty_string(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("")
        assert "pyautogui.write('', interval=0.02)" in result

    def test_multiple_newlines(self):
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("a\nb\nc")
        parts = result.split("; ")
        assert parts[0] == "pyautogui.write('a', interval=0.02)"
        assert parts[1] == "pyautogui.press('enter')"
        assert parts[2] == "pyautogui.write('b', interval=0.02)"
        assert parts[3] == "pyautogui.press('enter')"
        assert parts[4] == "pyautogui.write('c', interval=0.02)"

    def test_special_chars_repr(self):
        """repr() handles quotes and backslashes correctly."""
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("it's a\\path")
        # repr() produces valid Python — verify by compiling
        compile(f"import pyautogui; {result}", "<test>", "exec")
        # The text should be representable and contain the original content
        assert "it" in result and "path" in result

    def test_no_import_prefix(self):
        """_build_type_commands should return body only, no import statement."""
        from openadapt_evals.adapters.waa.live import _build_type_commands

        result = _build_type_commands("hello")
        assert not result.startswith("import ")

    def test_formula_with_newlines(self):
        """Reproduces the original LibreOffice Calc bug."""
        from openadapt_evals.adapters.waa.live import _build_type_commands

        text = "=(Sheet1.C4-Sheet1.C3)/Sheet1.C3\n=(Sheet1.C5-Sheet1.C4)/Sheet1.C4"
        result = _build_type_commands(text)
        # Should have two write commands separated by enter
        assert "=(Sheet1.C4-Sheet1.C3)/Sheet1.C3" in result
        assert "pyautogui.press('enter')" in result
        assert "=(Sheet1.C5-Sheet1.C4)/Sheet1.C4" in result
        # No literal newlines in the result
        assert "\n" not in result
        # Must be valid Python
        compile(f"import pyautogui; {result}", "<test>", "exec")

    def test_all_special_chars_produce_valid_python(self):
        """repr() handles ALL characters correctly — the key invariant."""
        from openadapt_evals.adapters.waa.live import _build_type_commands

        test_cases = [
            "simple",
            "it's a quote",
            "tab\there",
            "back\\slash",
            'double"quote',
            "unicode: \u00e9\u00e8\u00ea",
            "null\x00byte",
            "line1\nline2\nline3",
        ]
        for text in test_cases:
            result = _build_type_commands(text)
            compile(f"import pyautogui; {result}", "<test>", "exec")
