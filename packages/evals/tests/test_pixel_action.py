"""Tests for pixel_action direct path in WAALiveAdapter.

Verifies that pixel_action() builds pyautogui commands directly and sends
them via _send_command(), bypassing the element-based _translate_action path.
"""

import base64
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.adapters.waa.live import (
    AdapterInfrastructureError,
    SetupReadinessError,
    WAALiveAdapter,
    WAALiveConfig,
)
from openadapt_evals.errors import (
    ActionDeliveredObservationError,
    ActionDeliveryState,
    ActionDeliveryUncertainError,
    ActionExecutionError,
)


def _make_adapter(**config_kwargs) -> WAALiveAdapter:
    """Create a WAALiveAdapter without connecting to a server."""
    adapter = WAALiveAdapter.__new__(WAALiveAdapter)
    adapter.config = WAALiveConfig(**config_kwargs)
    adapter._current_task = BenchmarkTask(
        task_id="test-task", instruction="test", domain="desktop"
    )
    adapter._step_count = 0
    adapter._current_a11y = None
    adapter._current_rects = {}
    adapter._current_screenshot = None
    adapter._actions = []
    adapter._actual_screen_size = (1920, 1200)
    return adapter


class TestBuildPixelCommand:
    """Tests for _build_pixel_command -- the direct pyautogui command builder."""

    def test_click_absolute_pixels(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="click", x=500, y=300)
        assert cmd == "import pyautogui; pyautogui.click(500, 300)"

    def test_double_click(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="double_click", x=100, y=200)
        assert cmd == "import pyautogui; pyautogui.doubleClick(100, 200)"

    def test_right_click(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="right_click", x=800, y=600)
        assert cmd == "import pyautogui; pyautogui.rightClick(800, 600)"

    def test_type_action_clicks_then_types(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="type", x=100, y=200, text="hello")
        assert "pyautogui.click(100, 200)" in cmd
        assert "time.sleep(0.2)" in cmd
        assert "pyautogui.write('hello'" in cmd

    def test_key_action(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="key", key="enter")
        assert "pyautogui.press('enter')" in cmd

    def test_scroll_at_position(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="scroll", x=500, y=400)
        assert "pyautogui.scroll(-3, x=500, y=400)" in cmd

    def test_wait_action(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="wait")
        assert cmd == "import time; time.sleep(1)"

    def test_done_returns_none(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="done")
        assert cmd is None

    def test_error_returns_none(self):
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="error")
        assert cmd is None

    def test_unknown_action_is_rejected(self):
        adapter = _make_adapter()
        with pytest.raises(ActionExecutionError, match="Unsupported WAA pixel"):
            adapter._build_pixel_command(action_type="unknown_action")

    def test_fail_safe_corner_is_rejected_without_target_shift(self):
        adapter = _make_adapter()
        with pytest.raises(ActionExecutionError, match="fail-safe"):
            adapter._build_pixel_command(action_type="click", x=0, y=0)

        cmd = adapter._build_pixel_command(action_type="click", x=0, y=500)
        assert cmd == "import pyautogui; pyautogui.click(0, 500)"

    @pytest.mark.parametrize(
        ("x", "y"),
        [(-1, 20), (20, -1), (1920, 20), (20, 1200), (2000, 1300)],
    )
    def test_out_of_viewport_coordinates_are_rejected(self, x, y):
        adapter = _make_adapter()
        with pytest.raises(ActionExecutionError, match="outside"):
            adapter._build_pixel_command(action_type="click", x=x, y=y)

    def test_none_coords_are_rejected(self):
        adapter = _make_adapter()
        with pytest.raises(ActionExecutionError, match="requires x and y"):
            adapter._build_pixel_command(action_type="click", x=None, y=None)

    def test_float_coords_are_truncated(self):
        """Float pixel values (not fracs) should be cast to int."""
        adapter = _make_adapter()
        cmd = adapter._build_pixel_command(action_type="click", x=500.7, y=300.3)
        assert cmd == "import pyautogui; pyautogui.click(500, 300)"

    def test_generated_commands_are_valid_python(self):
        """All generated commands should be syntactically valid Python."""
        adapter = _make_adapter()
        cases = [
            ("click", 500, 300, None, None),
            ("double_click", 100, 200, None, None),
            ("right_click", 800, 600, None, None),
            ("type", 100, 200, "it's a test", None),
            ("key", None, None, None, "enter"),
            ("scroll", 500, 400, None, None),
            ("wait", None, None, None, None),
        ]
        for action_type, x, y, text, key in cases:
            cmd = adapter._build_pixel_command(
                action_type=action_type,
                x=x,
                y=y,
                text=text,
                key=key,
            )
            if cmd is not None:
                compile(cmd, f"<{action_type}>", "exec")


class TestPixelActionBypassesTranslateAction:
    """Tests that pixel_action() bypasses _translate_action entirely."""

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_does_not_call_translate_action(self, mock_send, mock_obs):
        """pixel_action should never call _translate_action."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        with patch.object(adapter, "_translate_action") as mock_translate:
            adapter.pixel_action(x=500, y=300, action_type="click")
            mock_translate.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_calls_send_command_directly(self, mock_send, mock_obs):
        """pixel_action should call _send_command with the direct command."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        adapter.pixel_action(x=500, y=300, action_type="click")
        mock_send.assert_called_once_with("import pyautogui; pyautogui.click(500, 300)")

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_returns_pixel_direct_flag(self, mock_send, mock_obs):
        """info dict should contain pixel_direct=True."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        _, _, info = adapter.pixel_action(x=500, y=300, action_type="click")
        assert info["pixel_direct"] is True

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_increments_step_count(self, mock_send, mock_obs):
        """pixel_action should increment _step_count."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()
        assert adapter._step_count == 0

        adapter.pixel_action(x=500, y=300)
        assert adapter._step_count == 1

        adapter.pixel_action(x=600, y=400)
        assert adapter._step_count == 2

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_records_action_history(self, mock_send, mock_obs):
        """pixel_action should append to _actions list."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        adapter.pixel_action(x=500, y=300, action_type="click")
        assert len(adapter._actions) == 1
        assert adapter._actions[0].type == "click"
        assert adapter._actions[0].x == 500
        assert adapter._actions[0].y == 300

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_action_done_returns_true(self, mock_send, mock_obs):
        """pixel_action with action_type='done' should return done=True."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        _, done, _ = adapter.pixel_action(action_type="done")
        assert done is True
        # _send_command should NOT be called for done actions
        mock_send.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pixel_observation_failure_preserves_confirmed_delivery(
        self, mock_send, mock_obs
    ):
        adapter = _make_adapter()
        mock_obs.side_effect = RuntimeError("capture failed")

        with pytest.raises(ActionDeliveredObservationError) as raised:
            adapter.pixel_action(x=500, y=300, action_type="click")

        mock_send.assert_called_once()
        assert raised.value.delivery_state is ActionDeliveryState.DELIVERED
        assert raised.value.retry_safe is False
        assert adapter._step_count == 1


class TestPixelActionFracConversion:
    """Tests that fractional coordinates are converted to absolute pixels."""

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_frac_to_pixel_conversion(self, mock_send, mock_obs):
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        adapter.pixel_action(x_frac=0.5, y_frac=0.5)
        # 0.5 * 1920 = 960, 0.5 * 1200 = 600
        mock_send.assert_called_once_with("import pyautogui; pyautogui.click(960, 600)")

    def test_screen_size_refuses_configured_fallback_before_measurement(self):
        adapter = _make_adapter(screen_width=1920, screen_height=1200)
        adapter._actual_screen_size = None

        with pytest.raises(ActionExecutionError, match="exact measured viewport"):
            _ = adapter.screen_size

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pointer_action_before_reset_is_rejected(self, mock_send, mock_obs):
        adapter = _make_adapter()
        adapter._current_task = None

        with pytest.raises(ActionExecutionError, match="loaded task"):
            adapter.pixel_action(x=500, y=300)

        mock_send.assert_not_called()
        mock_obs.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_keyboard_action_before_reset_is_rejected(self, mock_send, mock_obs):
        adapter = _make_adapter()
        adapter._current_task = None

        with pytest.raises(ActionExecutionError, match="loaded task"):
            adapter.pixel_action(action_type="key", key="enter")

        mock_send.assert_not_called()
        mock_obs.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_pointer_action_before_measured_viewport_is_rejected(
        self, mock_send, mock_obs
    ):
        adapter = _make_adapter()
        adapter._actual_screen_size = None

        with pytest.raises(ActionExecutionError, match="exact measured viewport"):
            adapter.pixel_action(x=500, y=300)

        mock_send.assert_not_called()
        mock_obs.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_absolute_pixel_uses_measured_not_configured_viewport(
        self, mock_send, mock_obs
    ):
        adapter = _make_adapter(screen_width=1920, screen_height=1200)
        adapter._actual_screen_size = (800, 600)

        with pytest.raises(ActionExecutionError, match="800x600 viewport"):
            adapter.pixel_action(x=900, y=300)

        mock_send.assert_not_called()
        mock_obs.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_frac_overrides_absolute(self, mock_send, mock_obs):
        """x_frac/y_frac should override x/y when both are provided."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        adapter.pixel_action(x=100, y=100, x_frac=0.5, y_frac=0.5)
        # Fracs override: 0.5 * 1920 = 960, 0.5 * 1200 = 600
        mock_send.assert_called_once_with("import pyautogui; pyautogui.click(960, 600)")

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_normalized_corner_is_not_shifted_to_another_target(
        self, mock_send, mock_obs
    ):
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="fail-safe"):
            adapter.pixel_action(x_frac=1.0, y_frac=1.0)
        mock_send.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_invalid_fraction_is_rejected(self, mock_send, mock_obs):
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        with pytest.raises(ValueError, match="between 0 and 1"):
            adapter.pixel_action(x_frac=1.1, y_frac=0.5)

        mock_send.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_corner_fraction_is_rejected(self, mock_send, mock_obs):
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="fail-safe"):
            adapter.pixel_action(x_frac=0.0, y_frac=0.0)
        mock_send.assert_not_called()


class TestSendCommandRefactor:
    """Tests that step() still works correctly after _send_command extraction."""

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_step_delegates_to_send_command(self, mock_send, mock_obs):
        """step() should delegate command execution to _send_command."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()
        adapter._current_rects = {"btn1": [400, 100, 500, 140]}

        action = BenchmarkAction(type="click", target_node_id="btn1")
        adapter.step(action)
        mock_send.assert_called_once()
        # The command should be an element-grounded click via _translate_action
        call_arg = mock_send.call_args[0][0]
        assert "pyautogui.click(450, 120)" in call_arg

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_step_done_does_not_send(self, mock_send, mock_obs):
        """step() with done action should not call _send_command."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        _, done, _ = adapter.step(BenchmarkAction(type="done"))
        assert done is True
        mock_send.assert_not_called()

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_step_increments_step_count(self, mock_send, mock_obs):
        """step() should still increment step count after refactor."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        adapter.step(BenchmarkAction(type="click", x=500, y=300))
        assert adapter._step_count == 1

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_step_returns_command_in_info(self, mock_send, mock_obs):
        """step() info dict should contain the command."""
        mock_obs.return_value = BenchmarkObservation(viewport=(1920, 1200))
        adapter = _make_adapter()

        _, _, info = adapter.step(BenchmarkAction(type="wait"))
        assert info["command"] == "import time; time.sleep(1)"

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_delivery_failure_does_not_advance_or_observe(self, mock_send, mock_obs):
        adapter = _make_adapter()
        mock_send.side_effect = ActionExecutionError("not delivered")

        with pytest.raises(ActionExecutionError, match="not delivered"):
            adapter.step(BenchmarkAction(type="click", x=500, y=300))

        assert adapter._step_count == 0
        assert adapter._actions == []
        mock_obs.assert_not_called()

    @pytest.mark.parametrize(
        "action",
        [
            BenchmarkAction(type="click", x=None, y=300),
            BenchmarkAction(type="click", x=500, y=None),
        ],
    )
    def test_click_requires_both_coordinates(self, action):
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="requires x and y"):
            adapter.step(action)

        assert adapter._step_count == 0
        assert adapter._actions == []

    def test_stale_element_without_fallback_is_rejected(self):
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="stale"):
            adapter.step(BenchmarkAction(type="click", target_node_id="gone"))

        assert adapter._step_count == 0
        assert adapter._actions == []

    def test_stale_element_does_not_trust_recorded_coordinates(self):
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="not authorized"):
            adapter.step(
                BenchmarkAction(
                    type="click", target_node_id="gone", x=500, y=300
                )
            )

        assert adapter._step_count == 0
        assert adapter._actions == []

    def test_unknown_action_is_rejected_before_rollout_advances(self):
        adapter = _make_adapter()

        with pytest.raises(ActionExecutionError, match="Unsupported WAA action"):
            adapter.step(BenchmarkAction(type="teleport"))

        assert adapter._step_count == 0
        assert adapter._actions == []

    @patch.object(WAALiveAdapter, "_get_observation")
    @patch.object(WAALiveAdapter, "_send_command")
    def test_observation_failure_preserves_confirmed_delivery(
        self, mock_send, mock_obs
    ):
        adapter = _make_adapter()
        mock_obs.side_effect = RuntimeError("capture failed")

        with pytest.raises(ActionDeliveredObservationError) as raised:
            adapter.step(BenchmarkAction(type="click", x=500, y=300))

        mock_send.assert_called_once()
        assert raised.value.delivery_state is ActionDeliveryState.DELIVERED
        assert raised.value.retry_safe is False
        assert adapter._step_count == 1
        assert len(adapter._actions) == 1


def _response(status: int, *, text: str = "", payload: object | None = None):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = {} if payload is None else payload
    return response


class TestSendCommandDeliveryReceipts:
    def test_http_500_is_not_success(self):
        adapter = _make_adapter()

        with (
            patch("requests.post", return_value=_response(500, text="failed")),
            pytest.raises(ActionDeliveryUncertainError, match="HTTP 500") as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.UNCERTAIN
        assert raised.value.retry_safe is False

    def test_http_error_with_explicit_non_delivery_is_retry_safe(self):
        adapter = _make_adapter()
        response = _response(
            409,
            text="rejected before dispatch",
            payload={"delivery_state": "not_delivered"},
        )

        with (
            patch("requests.post", return_value=response),
            pytest.raises(ActionExecutionError) as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.NOT_DELIVERED
        assert raised.value.retry_safe is True

    def test_stderr_is_not_success(self):
        adapter = _make_adapter()
        response = _response(200, payload={"stderr": "permission denied"})

        with (
            patch("requests.post", return_value=response),
            pytest.raises(ActionExecutionError, match="stderr: permission denied"),
        ):
            adapter._send_command("do_work()")

    def test_connection_error_is_uncertain_not_success(self):
        adapter = _make_adapter()

        with (
            patch("requests.post", side_effect=ConnectionError("offline")),
            pytest.raises(
                ActionDeliveryUncertainError, match="outcome is uncertain"
            ) as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.UNCERTAIN
        assert raised.value.retry_safe is False

    def test_failed_failsafe_recovery_is_not_success(self):
        adapter = _make_adapter()
        response = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={"message": "pyautogui.FailSafeException"},
        )

        with (
            patch("requests.post", return_value=response),
            patch.object(adapter, "_recover_failsafe", return_value=False),
            pytest.raises(ActionExecutionError, match="recovery failed"),
        ):
            adapter._send_command("do_work()")

    def test_failed_recovery_preserves_confirmed_non_delivery(self):
        adapter = _make_adapter()
        response = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={
                "message": "pyautogui.FailSafeException",
                "delivery_state": "not_delivered",
            },
        )

        with (
            patch("requests.post", return_value=response),
            patch.object(adapter, "_recover_failsafe", return_value=False),
            pytest.raises(ActionExecutionError) as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.NOT_DELIVERED
        assert raised.value.retry_safe is True

    def test_failed_retry_is_not_success(self):
        adapter = _make_adapter()
        initial = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={
                "message": "pyautogui.FailSafeException",
                "delivery_state": "not_delivered",
            },
        )
        retry = _response(500, text="still failed")

        with (
            patch("requests.post", side_effect=[initial, retry]),
            patch.object(adapter, "_recover_failsafe", return_value=True),
            pytest.raises(ActionExecutionError, match="retry was not confirmed"),
        ):
            adapter._send_command("do_work()")

    @pytest.mark.parametrize(
        "retry",
        [
            _response(
                409,
                text="not dispatched",
                payload={"delivery_state": "not_delivered"},
            ),
            _response(
                200,
                payload={
                    "stderr": "not dispatched",
                    "delivery_state": "not_delivered",
                },
            ),
        ],
    )
    def test_retry_preserves_confirmed_non_delivery(self, retry):
        adapter = _make_adapter()
        initial = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={
                "message": "pyautogui.FailSafeException",
                "delivery_state": "not_delivered",
            },
        )

        with (
            patch("requests.post", side_effect=[initial, retry]),
            patch.object(adapter, "_recover_failsafe", return_value=True),
            pytest.raises(ActionExecutionError) as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.NOT_DELIVERED
        assert raised.value.retry_safe is True

    def test_partial_failsafe_delivery_is_not_blindly_retried(self):
        adapter = _make_adapter()
        response = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={"message": "pyautogui.FailSafeException"},
        )
        command = "pyautogui.click(100, 100); pyautogui.moveTo(0, 0)"

        with (
            patch("requests.post", return_value=response) as post,
            patch.object(adapter, "_recover_failsafe", return_value=True),
            pytest.raises(
                ActionDeliveryUncertainError, match="refusing a blind retry"
            ) as raised,
        ):
            adapter._send_command(command)

        assert post.call_count == 1
        assert raised.value.delivery_state is ActionDeliveryState.UNCERTAIN
        assert raised.value.retry_safe is False

    def test_explicit_non_delivery_receipt_allows_one_retry(self):
        adapter = _make_adapter()
        initial = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={
                "message": "pyautogui.FailSafeException",
                "delivery_state": "not_delivered",
            },
        )
        retry = _response(200, payload={"stdout": "ok"})

        with (
            patch("requests.post", side_effect=[initial, retry]) as post,
            patch.object(adapter, "_recover_failsafe", return_value=True),
        ):
            adapter._send_command("do_work()")

        assert post.call_count == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {"message": "pyautogui.FailSafeException"},
            {"stdout": "pyautogui.FailSafeException"},
        ],
    )
    def test_retry_failsafe_in_any_diagnostic_is_not_success(self, payload):
        adapter = _make_adapter()
        initial = _response(
            500,
            text="pyautogui.FailSafeException",
            payload={
                "message": "pyautogui.FailSafeException",
                "delivery_state": "not_delivered",
            },
        )
        retry = _response(200, payload=payload)

        with (
            patch("requests.post", side_effect=[initial, retry]) as post,
            patch.object(adapter, "_recover_failsafe", return_value=True),
            pytest.raises(
                ActionDeliveryUncertainError,
                match="retry triggered a fail-safe",
            ) as raised,
        ):
            adapter._send_command("do_work()")

        assert post.call_count == 2
        assert raised.value.delivery_state is ActionDeliveryState.UNCERTAIN
        assert raised.value.retry_safe is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"success": False},
            {"success": "true"},
            {"error": "command rejected"},
            {"delivery_state": "uncertain"},
            {"delivery_state": "delivered-ish"},
        ],
    )
    def test_explicit_failed_http_200_receipt_is_not_success(self, payload):
        adapter = _make_adapter()

        with (
            patch("requests.post", return_value=_response(200, payload=payload)),
            pytest.raises(
                ActionDeliveryUncertainError,
                match="receipt did not confirm execution",
            ),
        ):
            adapter._send_command("do_work()")

    def test_explicit_non_delivery_http_200_receipt_is_retry_safe_failure(self):
        adapter = _make_adapter()
        response = _response(200, payload={"delivery_state": "not_delivered"})

        with (
            patch("requests.post", return_value=response),
            pytest.raises(ActionExecutionError) as raised,
        ):
            adapter._send_command("do_work()")

        assert raised.value.delivery_state is ActionDeliveryState.NOT_DELIVERED
        assert raised.value.retry_safe is True

    def test_recovery_rejects_stderr_and_restores_failsafe_in_script(self):
        adapter = _make_adapter()
        response = _response(200, payload={"stderr": "move failed"})

        with patch("requests.post", return_value=response) as post:
            assert adapter._recover_failsafe() is False

        command = post.call_args.kwargs["json"]["command"]
        encoded = command.split("base64.b64decode('", 1)[1].split("')", 1)[0]
        script = base64.b64decode(encoded).decode()
        assert "finally:" in script
        assert "pyautogui.FAILSAFE = previous" in script

    @pytest.mark.parametrize(
        "payload",
        [
            {"success": False},
            {"error": "recovery rejected"},
            {"delivery_state": "uncertain"},
            {"delivery_state": "delivered-ish"},
        ],
    )
    def test_recovery_rejects_explicit_failed_receipt(self, payload):
        adapter = _make_adapter()

        with patch(
            "requests.post",
            return_value=_response(200, payload=payload),
        ):
            assert adapter._recover_failsafe() is False


class TestObservationFailure:
    def test_reset_failure_invalidates_previous_task_evidence(self):
        adapter = _make_adapter()
        adapter._current_a11y = {"name": "old task"}
        adapter._current_rects = {"old": [10, 10, 20, 20]}
        adapter._current_screenshot = b"old"

        with (
            patch.object(adapter, "check_connection", return_value=False),
            pytest.raises(RuntimeError, match="Cannot connect"),
        ):
            adapter.reset(
                BenchmarkTask(
                    task_id="new-task",
                    instruction="new",
                    domain="desktop",
                )
            )

        assert adapter._current_task is None
        assert adapter._current_a11y is None
        assert adapter._current_rects == {}
        assert adapter._current_screenshot is None
        assert adapter._actual_screen_size is None

    @pytest.mark.parametrize(
        "action",
        [
            BenchmarkAction(type="key", key="enter"),
            BenchmarkAction(type="type", text="sensitive value"),
        ],
    )
    def test_failed_setup_leaves_non_pointer_actions_disabled(self, action):
        adapter = _make_adapter(lightweight=True)
        task = BenchmarkTask(
            task_id="new-task",
            instruction="new",
            domain="desktop",
            raw_config={"config": [{"type": "execute", "parameters": {}}]},
        )

        with (
            patch.object(adapter, "check_connection", return_value=True),
            patch.object(
                adapter,
                "_run_task_setup",
                side_effect=SetupReadinessError("setup failed"),
            ),
            pytest.raises(SetupReadinessError, match="setup failed"),
        ):
            adapter.reset(task)

        with pytest.raises(ActionExecutionError, match="loaded task"):
            adapter.step(action)

    @pytest.mark.parametrize(
        "payload",
        [
            {"stderr": "setup failed"},
            {"success": False},
            {"error": "setup rejected"},
            {"delivery_state": "uncertain"},
            {"delivery_state": "delivered-ish"},
            {"returncode": 7},
        ],
    )
    def test_setup_http_200_failed_receipt_is_not_ok(self, payload):
        adapter = _make_adapter(lightweight=True)
        raw_config = {
            "config": [{"type": "execute", "parameters": {}}],
        }

        with (
            patch.object(adapter, "_config_entry_to_command", return_value="setup()"),
            patch("requests.post", return_value=_response(200, payload=payload)),
            pytest.raises(SetupReadinessError, match="failing steps"),
        ):
            adapter._run_task_setup(raw_config)

        assert adapter._last_setup_results[0]["status"] == "error"

    def test_execute_setup_command_raises_on_nonzero_shell_exit(self):
        command = WAALiveAdapter._config_entry_to_command(
            {
                "type": "execute",
                "parameters": {"command": "exit 7"},
            }
        )

        assert command is not None
        with pytest.raises(subprocess.CalledProcessError) as raised:
            exec(command, {})

        assert raised.value.returncode == 7

    def test_partial_observation_does_not_reuse_old_accessibility_geometry(self):
        adapter = _make_adapter()
        adapter._current_a11y = {"name": "old task"}
        adapter._current_rects = {"old": [10, 10, 20, 20]}
        screenshot = _response(200)
        screenshot.content = b"fresh-but-not-an-image"
        missing_a11y = _response(503, text="unavailable")

        with patch("requests.get", side_effect=[screenshot, missing_a11y]):
            observation = adapter._get_observation()

        assert observation.screenshot == b"fresh-but-not-an-image"
        assert observation.accessibility_tree is None
        assert adapter._current_a11y is None
        assert adapter._current_rects == {}
        assert adapter._current_screenshot == b"fresh-but-not-an-image"
        assert adapter._actual_screen_size is None

    def test_missing_screenshot_and_accessibility_is_not_normal_observation(self):
        adapter = _make_adapter()
        missing = _response(503, text="unavailable")

        with (
            patch("requests.get", side_effect=[missing, missing]),
            pytest.raises(AdapterInfrastructureError, match="no screenshot"),
        ):
            adapter._get_observation()
