"""Tests for LocalAdapter."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

try:
    from pynput.keyboard import Key  # type: ignore[import-untyped]

    HAS_PYNPUT = True
except (ImportError, ValueError):
    HAS_PYNPUT = False

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.adapters.local import LocalAdapter
from openadapt_evals.adapters.local.adapter import _get_input_monitor_geometry
from openadapt_evals.errors import (
    ActionDeliveredObservationError,
    ActionDeliveryState,
    ActionDeliveryUncertainError,
    ActionExecutionError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter():
    """Create a LocalAdapter with no action delay for fast tests."""
    return LocalAdapter(action_delay=0.0)


@pytest.fixture
def sample_task():
    """A minimal task for testing."""
    return BenchmarkTask(
        task_id="local_test_1",
        instruction="Open Notepad and type hello",
        domain="desktop",
    )


# Minimal valid 1x1 PNG for mocking screenshot captures.
MINIMAL_PNG = bytes(
    [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
        0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
        0x00, 0x00, 0x03, 0x00, 0x01, 0x00, 0x05, 0xFE,
        0xD4, 0xEF, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
        0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ]
)


def _make_mock_observe(adapter):
    """Patch adapter.observe to return a fake observation without screen capture."""

    def _fake_observe():
        adapter._record_capture_geometry(
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            (1920, 1080),
            {"left": 0.0, "top": 0.0, "width": 1920.0, "height": 1080.0},
        )
        return BenchmarkObservation(
            screenshot=MINIMAL_PNG,
            viewport=(1920, 1080),
        )

    adapter.observe = _fake_observe  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestLocalAdapterProperties:
    def test_name(self, adapter):
        assert adapter.name == "local"

    def test_benchmark_type(self, adapter):
        assert adapter.benchmark_type == "interactive"


# ---------------------------------------------------------------------------
# observe()
# ---------------------------------------------------------------------------


class TestLocalAdapterObserve:
    @patch("openadapt_evals.adapters.local.adapter.mss", create=True)
    def test_observe_returns_observation(self, adapter):
        """observe() returns a BenchmarkObservation with screenshot bytes."""
        _make_mock_observe(adapter)
        obs = adapter.observe()
        assert isinstance(obs, BenchmarkObservation)
        assert obs.screenshot is not None
        assert isinstance(obs.screenshot, bytes)
        assert len(obs.screenshot) > 0

    @patch("openadapt_evals.adapters.local.adapter.mss", create=True)
    def test_observe_has_viewport(self, adapter):
        """observe() populates the viewport tuple."""
        _make_mock_observe(adapter)
        obs = adapter.observe()
        assert obs.viewport is not None
        assert len(obs.viewport) == 2


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestLocalAdapterReset:
    def test_reset_returns_observation(self, adapter, sample_task):
        _make_mock_observe(adapter)
        obs = adapter.reset(sample_task)
        assert isinstance(obs, BenchmarkObservation)

    def test_reset_records_task(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)
        assert adapter._current_task is sample_task

    def test_reset_clears_step_count(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter._step_count = 5
        adapter.reset(sample_task)
        assert adapter._step_count == 0

    @pytest.mark.parametrize(
        "action",
        [
            BenchmarkAction(type="click", x=1, y=1),
            BenchmarkAction(type="type", text="sensitive"),
            BenchmarkAction(type="key", key="enter"),
        ],
    )
    def test_failed_reset_invalidates_task_and_geometry(
        self,
        adapter,
        sample_task,
        action,
    ):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with (
            patch.object(adapter, "observe", side_effect=RuntimeError("capture failed")),
            pytest.raises(RuntimeError, match="capture failed"),
        ):
            adapter.reset(sample_task)

        with (
            patch.object(adapter, "_execute_action") as execute,
            pytest.raises(ActionExecutionError, match=r"reset\(\)"),
        ):
            adapter.step(action)

        execute.assert_not_called()
        assert adapter._current_task is None
        assert adapter._last_viewport is None
        assert adapter._capture_origin is None
        assert adapter._capture_scale is None
        assert adapter._capture_pixel_size is None


# ---------------------------------------------------------------------------
# step()
# ---------------------------------------------------------------------------


class TestLocalAdapterStep:
    def test_step_returns_tuple(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action"):
            obs, done, info = adapter.step(BenchmarkAction(type="click", x=100, y=200))
        assert isinstance(obs, BenchmarkObservation)
        assert isinstance(done, bool)
        assert isinstance(info, dict)

    def test_step_increments_count(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action"):
            adapter.step(BenchmarkAction(type="click", x=100, y=200))
        assert adapter._step_count == 1

    def test_step_done_action_sets_done_true(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action"):
            _, done, _ = adapter.step(BenchmarkAction(type="done"))
        assert done is True

    def test_step_click_dispatches(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_do_click") as mock_click:
            with patch.object(adapter, "observe", return_value=BenchmarkObservation()):
                adapter.step(BenchmarkAction(type="click", x=50, y=60))
            mock_click.assert_called_once()

    def test_step_type_dispatches(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_do_type") as mock_type:
            with patch.object(adapter, "observe", return_value=BenchmarkObservation()):
                adapter.step(BenchmarkAction(type="type", text="hello"))
            mock_type.assert_called_once()

    def test_step_key_dispatches(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_do_key") as mock_key:
            with patch.object(adapter, "observe", return_value=BenchmarkObservation()):
                adapter.step(BenchmarkAction(type="key", key="enter"))
            mock_key.assert_called_once()

    def test_step_error_action_sets_done_true(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action"):
            _, done, _ = adapter.step(BenchmarkAction(type="error"))
        assert done is True

    def test_step_does_not_report_execution_error_as_completion(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action", side_effect=RuntimeError("perm")):
            with pytest.raises(ActionDeliveryUncertainError, match="perm") as raised:
                adapter.step(BenchmarkAction(type="click", x=1, y=1))

        assert adapter._step_count == 0
        assert raised.value.delivery_state is ActionDeliveryState.UNCERTAIN
        assert raised.value.retry_safe is False

    @pytest.mark.parametrize(
        ("action", "message"),
        [
            (BenchmarkAction(type="unknown"), "Unsupported local action"),
            (BenchmarkAction(type="click", x=None, y=2), "requires x"),
            (BenchmarkAction(type="click", x=1, y=None), "requires y"),
            (BenchmarkAction(type="type", text=None), "requires text"),
            (BenchmarkAction(type="key", key=None), "requires key"),
            (
                BenchmarkAction(type="scroll", scroll_direction=None),
                "requires direction",
            ),
            (
                BenchmarkAction(type="scroll", scroll_direction="diagonal"),
                "requires direction",
            ),
            (
                BenchmarkAction(type="drag", x=1, y=2, end_x=3, end_y=None),
                "requires end_y",
            ),
        ],
    )
    def test_invalid_action_is_rejected_before_dispatch(
        self, adapter, sample_task, action, message
    ):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with (
            patch.object(adapter, "_execute_action") as execute,
            pytest.raises(ActionExecutionError, match=message),
        ):
            adapter.step(action)

        execute.assert_not_called()
        assert adapter._step_count == 0

    def test_explicit_empty_text_remains_valid(self, adapter, sample_task):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with patch.object(adapter, "_execute_action") as execute:
            adapter.step(BenchmarkAction(type="type", text=""))

        execute.assert_called_once()
        assert adapter._step_count == 1

    @pytest.mark.parametrize(
        "action",
        [
            BenchmarkAction(type="click", x=-1, y=20),
            BenchmarkAction(type="click", x=1920, y=20),
            BenchmarkAction(type="drag", x=1, y=2, end_x=30, end_y=1080),
        ],
    )
    def test_out_of_viewport_pointer_action_is_rejected(
        self, adapter, sample_task, action
    ):
        _make_mock_observe(adapter)
        adapter._last_viewport = (1920, 1080)
        adapter.reset(sample_task)

        with pytest.raises(ActionExecutionError, match="viewport|non-negative") as raised:
            adapter.step(action)

        assert raised.value.delivery_state is ActionDeliveryState.NOT_DELIVERED
        assert raised.value.retry_safe is True
        assert adapter._step_count == 0

    def test_observation_failure_preserves_confirmed_delivery(
        self, adapter, sample_task
    ):
        _make_mock_observe(adapter)
        adapter.reset(sample_task)

        with (
            patch.object(adapter, "_execute_action"),
            patch.object(adapter, "observe", side_effect=RuntimeError("capture failed")),
            pytest.raises(ActionDeliveredObservationError) as raised,
        ):
            adapter.step(BenchmarkAction(type="click", x=1, y=1))

        assert raised.value.delivery_state is ActionDeliveryState.DELIVERED
        assert raised.value.retry_safe is False
        assert adapter._step_count == 1


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


class TestLocalAdapterEvaluate:
    def test_evaluate_returns_result(self, adapter, sample_task):
        result = adapter.evaluate(sample_task)
        assert isinstance(result, BenchmarkResult)
        assert result.task_id == sample_task.task_id

    def test_evaluate_score_is_zero(self, adapter, sample_task):
        result = adapter.evaluate(sample_task)
        assert result.score == 0.0
        assert result.success is False
        assert result.error_type == "evaluation"


# ---------------------------------------------------------------------------
# list_tasks / load_task
# ---------------------------------------------------------------------------


class TestLocalAdapterTasks:
    def test_list_tasks_empty(self, adapter):
        assert adapter.list_tasks() == []

    def test_load_task_raises(self, adapter):
        with pytest.raises(KeyError):
            adapter.load_task("nonexistent")


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestLocalAdapterContextManager:
    def test_context_manager(self):
        with LocalAdapter() as adapter:
            assert isinstance(adapter, LocalAdapter)


# ---------------------------------------------------------------------------
# HiDPI scaling
# ---------------------------------------------------------------------------


class TestLocalAdapterScaling:
    def test_to_logical_scale_1(self, adapter):
        adapter._record_capture_geometry(
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            (1920, 1080),
            {"left": 0.0, "top": 0.0, "width": 1920.0, "height": 1080.0},
        )
        assert adapter._to_logical(100, 200) == (100.0, 200.0)

    def test_to_logical_uses_retina_secondary_monitor_input_geometry(self, adapter):
        adapter._record_capture_geometry(
            {"left": -2560, "top": 240, "width": 2560, "height": 1440},
            (2560, 1440),
            {"left": -1280.0, "top": 120.0, "width": 1280.0, "height": 720.0},
        )
        lx, ly = adapter._to_logical(200, 400)
        assert lx == pytest.approx(-1180.0)
        assert ly == pytest.approx(320.0)

    def test_to_logical_rejects_missing_geometry(self, adapter):
        with pytest.raises(ActionExecutionError, match="capture geometry"):
            adapter._to_logical(100, 200)

    def test_to_logical_rejects_viewport_geometry_mismatch(self, adapter):
        adapter._record_capture_geometry(
            {"left": 0, "top": 0, "width": 1280, "height": 720},
            (2560, 1440),
            {"left": 0.0, "top": 0.0, "width": 1280.0, "height": 720.0},
        )
        adapter._last_viewport = (1280, 720)

        with pytest.raises(ActionExecutionError, match="matching capture geometry"):
            adapter._to_logical(100, 200)

    def test_rotated_macos_display_refuses_unmodeled_transform(self):
        quartz = SimpleNamespace(
            kCGErrorSuccess=0,
            CGGetActiveDisplayList=lambda *_args: (0, [42], 1),
            CGDisplayRotation=lambda _display_id: 90.0,
        )
        with (
            patch("platform.system", return_value="Darwin"),
            patch.dict(sys.modules, {"Quartz": quartz}),
            pytest.raises(ActionExecutionError, match="bind the selected macOS display"),
        ):
            _get_input_monitor_geometry(
                1,
                {"left": 0, "top": 0, "width": 1440, "height": 2560},
            )


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_PYNPUT, reason="pynput requires display")
class TestKeyResolution:
    def test_known_key(self):
        resolved = LocalAdapter._resolve_key("enter")
        assert resolved == Key.enter

    def test_single_char(self):
        resolved = LocalAdapter._resolve_key("a")
        assert resolved == "a"

    def test_case_insensitive(self):
        assert LocalAdapter._resolve_key("ENTER") == Key.enter
        assert LocalAdapter._resolve_key("Tab") == Key.tab
