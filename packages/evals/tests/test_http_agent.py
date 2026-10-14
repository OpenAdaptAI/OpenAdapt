"""Tests for HttpAgent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.http_agent import HttpAgent, _parse_action_response


@pytest.fixture
def agent() -> HttpAgent:
    return HttpAgent(endpoint_url="http://fake-agent:8080")


@pytest.fixture
def task() -> BenchmarkTask:
    return BenchmarkTask(
        task_id="test_1",
        instruction="Click the Submit button",
        domain="desktop",
    )


@pytest.fixture
def observation() -> BenchmarkObservation:
    return BenchmarkObservation(
        screenshot=b"\x89PNG\r\n\x1a\nfake",
        viewport=(1920, 1200),
        accessibility_tree={"role": "window", "name": "Test"},
    )


class TestAct:
    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_click(self, mock_post, agent, observation, task):
        """act() sends observation and parses a click response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "click", "x": 0.5, "y": 0.3}
        mock_post.return_value = mock_resp

        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3

        # Verify the request payload
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["instruction"] == "Click the Submit button"
        assert payload["task_id"] == "test_1"
        assert payload["viewport"] == [1920, 1200]
        assert payload["screenshot_b64"] is not None

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_type(self, mock_post, agent, observation, task):
        """act() parses a type response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "type", "text": "hello world"}
        mock_post.return_value = mock_resp

        action = agent.act(observation, task)

        assert action.type == "type"
        assert action.text == "hello world"

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_done(self, mock_post, agent, observation, task):
        """act() parses a done response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "done"}
        mock_post.return_value = mock_resp

        action = agent.act(observation, task)

        assert action.type == "done"

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_connection_error(self, mock_post, agent, observation, task):
        """act() returns an infrastructure error, never a clean `done`."""
        import requests

        mock_post.side_effect = requests.ConnectionError("refused")

        action = agent.act(observation, task)

        # NOT "done": an unreachable agent endpoint is an infrastructure
        # failure, and `done` reads to the eval loop as a clean agent finish,
        # producing a genuine-looking 0% for an agent that never ran.
        assert action.type == "error"
        assert action.raw_action["error_type"] == "infrastructure"
        assert "connection_failed" in action.raw_action["error"]

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_timeout(self, mock_post, agent, observation, task):
        """act() returns an infrastructure error on timeout, never done."""
        import requests

        mock_post.side_effect = requests.Timeout("timed out")

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["error_type"] == "infrastructure"
        assert "timeout" in action.raw_action["error"]

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_http_error(self, mock_post, agent, observation, task):
        """act() returns an infrastructure error on HTTP error, never done."""
        import requests

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("500")
        mock_post.return_value = mock_resp

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["error_type"] == "infrastructure"
        assert "http_error" in action.raw_action["error"]

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_no_screenshot(self, mock_post, agent, task):
        """act() works when observation has no screenshot."""
        obs = BenchmarkObservation(screenshot=None)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "done"}
        mock_post.return_value = mock_resp

        action = agent.act(obs, task)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["screenshot_b64"] is None
        assert action.type == "done"

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_act_step_count_increments(self, mock_post, agent, observation, task):
        """act() increments step_count on each call."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "click", "x": 0.1, "y": 0.1}
        mock_post.return_value = mock_resp

        agent.act(observation, task)
        payload1 = mock_post.call_args.kwargs["json"]
        assert payload1["step_count"] == 0

        agent.act(observation, task)
        payload2 = mock_post.call_args.kwargs["json"]
        assert payload2["step_count"] == 1


class TestReset:
    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_reset_resets_step_count(self, mock_post, agent, observation, task):
        """reset() resets internal step count."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "done"}
        mock_post.return_value = mock_resp

        agent.act(observation, task)
        assert agent._step_count == 1

        agent.reset()
        assert agent._step_count == 0

    @patch("openadapt_evals.agents.http_agent.requests.post")
    def test_reset_tolerates_missing_endpoint(self, mock_post, agent):
        """reset() does not fail if /reset endpoint is not available."""
        import requests

        mock_post.side_effect = requests.ConnectionError("no /reset")

        agent.reset()  # Should not raise


class TestHealthCheck:
    @patch("openadapt_evals.agents.http_agent.requests.get")
    def test_health_check_success(self, mock_get, agent):
        """health_check() returns True when endpoint responds 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        assert agent.health_check() is True

    @patch("openadapt_evals.agents.http_agent.requests.get")
    def test_health_check_failure(self, mock_get, agent):
        """health_check() returns False on connection error."""
        import requests

        mock_get.side_effect = requests.ConnectionError()

        assert agent.health_check() is False

    @patch("openadapt_evals.agents.http_agent.requests.get")
    def test_health_check_non_200(self, mock_get, agent):
        """health_check() returns False on non-200 status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_get.return_value = mock_resp

        assert agent.health_check() is False


class TestParseActionResponse:
    def test_click_action(self):
        action = _parse_action_response({"type": "click", "x": 0.5, "y": 0.3})
        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3

    def test_type_action(self):
        action = _parse_action_response({"type": "type", "text": "hello"})
        assert action.type == "type"
        assert action.text == "hello"

    def test_key_action(self):
        action = _parse_action_response(
            {"type": "key", "key": "Enter", "modifiers": ["ctrl"]}
        )
        assert action.type == "key"
        assert action.key == "Enter"
        assert action.modifiers == ["ctrl"]

    def test_scroll_action(self):
        action = _parse_action_response(
            {"type": "scroll", "scroll_direction": "down"}
        )
        assert action.type == "scroll"
        assert action.scroll_direction == "down"

    def test_drag_action(self):
        action = _parse_action_response(
            {"type": "drag", "x": 0.1, "y": 0.2, "end_x": 0.8, "end_y": 0.9}
        )
        assert action.type == "drag"
        assert action.x == 0.1
        assert action.end_x == 0.8

    def test_element_grounding(self):
        action = _parse_action_response(
            {"type": "click", "target_node_id": "btn_42", "target_role": "button"}
        )
        assert action.target_node_id == "btn_42"
        assert action.target_role == "button"

    def test_missing_type_is_an_error_not_a_clean_finish(self):
        """A body with no `type` is malformed, not a completed task."""
        action = _parse_action_response({})
        assert action.type == "error"
        assert action.raw_action["error_type"] == "agent"

    def test_raw_action_preserved(self):
        data = {"type": "click", "x": 0.5, "y": 0.5, "extra_field": "kept"}
        action = _parse_action_response(data)
        assert action.raw_action == data
