"""Tests for DemoExecutor HTTP grounder path.

Verifies that the DemoExecutor correctly routes click grounding through
an HTTP endpoint when ``grounder_endpoint`` is set, using the UI-Venus
native bbox prompt format.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation
from openadapt_evals.agents.demo_executor import DemoExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def executor_with_endpoint():
    """DemoExecutor configured with an HTTP grounder endpoint."""
    return DemoExecutor(
        grounder_model="gpt-4.1-mini",
        grounder_provider="openai",
        grounder_endpoint="http://fake-gpu:8000",
        planner_model="gpt-4.1-mini",
        planner_provider="openai",
    )


@pytest.fixture
def executor_without_endpoint():
    """DemoExecutor configured without an HTTP grounder endpoint."""
    return DemoExecutor(
        grounder_model="gpt-4.1-mini",
        grounder_provider="openai",
        planner_model="gpt-4.1-mini",
        planner_provider="openai",
    )


@pytest.fixture
def fake_observation():
    """Observation with a small PNG screenshot."""
    # 1x1 transparent PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return BenchmarkObservation(screenshot=png_bytes)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDemoExecutorInit:
    """Test DemoExecutor initialization with grounder_endpoint."""

    def test_endpoint_stored(self, executor_with_endpoint):
        assert executor_with_endpoint._grounder_endpoint == "http://fake-gpu:8000"

    def test_no_endpoint_is_none(self, executor_without_endpoint):
        assert executor_without_endpoint._grounder_endpoint is None


class TestGroundClickRouting:
    """Test that _ground_click routes to HTTP when endpoint is set."""

    def test_routes_to_http_when_endpoint_set(
        self, executor_with_endpoint, fake_observation
    ):
        """When grounder_endpoint is set, _ground_click calls _ground_click_http."""
        with patch.object(
            executor_with_endpoint, "_ground_click_http",
            return_value=BenchmarkAction(type="click", x=0.5, y=0.3),
        ) as mock_http:
            action = executor_with_endpoint._ground_click(
                fake_observation, "the OK button"
            )
            mock_http.assert_called_once_with(fake_observation, "the OK button")
            assert action.type == "click"
            assert action.x == 0.5
            assert action.y == 0.3

    def test_routes_to_vlm_when_no_endpoint(
        self, executor_without_endpoint, fake_observation
    ):
        """When no grounder_endpoint, _ground_click calls _ground_click_vlm."""
        with patch.object(
            executor_without_endpoint, "_ground_click_vlm",
            return_value=BenchmarkAction(type="click", x=0.7, y=0.8),
        ) as mock_vlm:
            action = executor_without_endpoint._ground_click(
                fake_observation, "the Cancel button"
            )
            mock_vlm.assert_called_once_with(fake_observation, "the Cancel button")
            assert action.type == "click"


class TestGroundClickHttp:
    """Test the HTTP grounder call uses UI-Venus prompt format."""

    def test_uses_ui_venus_prompt_format(
        self, executor_with_endpoint, fake_observation
    ):
        """Verify the prompt sent to the HTTP endpoint matches UI-Venus format."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "[100, 200, 300, 400]"},
            }],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            executor_with_endpoint._ground_click_http(
                fake_observation, "the Settings icon"
            )

            # Verify the HTTP call was made
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args

            # Extract the payload
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert payload is not None

            # Check model name is UI-Venus
            assert payload["model"] == "UI-Venus-1.5-8B"

            # Check the prompt uses UI-Venus bbox format
            messages = payload["messages"]
            assert len(messages) == 1
            content = messages[0]["content"]

            # Find the text content item
            text_items = [c for c in content if c["type"] == "text"]
            assert len(text_items) == 1
            prompt_text = text_items[0]["text"]

            # Verify it uses the UI-Venus native format
            assert "Outline the position corresponding to the instruction" in prompt_text
            assert "the Settings icon" in prompt_text
            assert "[x1,y1,x2,y2]" in prompt_text

            # Verify image is included
            image_items = [c for c in content if c["type"] == "image_url"]
            assert len(image_items) == 1

    def test_endpoint_url_construction(
        self, executor_with_endpoint, fake_observation
    ):
        """Verify the endpoint URL has /v1/chat/completions appended."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[50, 50, 100, 100]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            executor_with_endpoint._ground_click_http(
                fake_observation, "button"
            )
            url = mock_post.call_args[0][0]
            assert url == "http://fake-gpu:8000/v1/chat/completions"

    def test_endpoint_with_trailing_slash(self, fake_observation):
        """Endpoint with trailing slash should still work."""
        executor = DemoExecutor(
            grounder_endpoint="http://gpu:8000/",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[10, 20, 30, 40]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            executor._ground_click_http(fake_observation, "button")
            url = mock_post.call_args[0][0]
            assert url == "http://gpu:8000/v1/chat/completions"

    def test_endpoint_already_has_v1(self, fake_observation):
        """Endpoint that already has /v1 should not double it."""
        executor = DemoExecutor(
            grounder_endpoint="http://gpu:8000/v1",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[10, 20, 30, 40]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            executor._ground_click_http(fake_observation, "button")
            url = mock_post.call_args[0][0]
            assert url == "http://gpu:8000/v1/chat/completions"


class TestBboxParsing:
    """Test bbox response parsing returns correct click coordinates."""

    def test_bbox_center_click(
        self, executor_with_endpoint, fake_observation
    ):
        """[x1, y1, x2, y2] should produce a click at the center."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[100, 200, 300, 400]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response):
            action = executor_with_endpoint._ground_click_http(
                fake_observation, "target"
            )
            assert action.type == "click"
            # Center of [100, 200, 300, 400] = (200, 300)
            # These are > 1 but <= 1000, so normalized to 0-1
            assert abs(action.x - 0.2) < 0.01
            assert abs(action.y - 0.3) < 0.01

    def test_http_error_does_not_invent_center_click(
        self, executor_with_endpoint, fake_observation
    ):
        with patch("requests.post", side_effect=Exception("Connection refused")):
            action = executor_with_endpoint._ground_click_http(
                fake_observation, "target"
            )
            assert action.type == "error"
            assert "Connection refused" in action.raw_action["grounder_error"]

    def test_no_screenshot(self, executor_with_endpoint):
        """Should still work without a screenshot (text-only grounding)."""
        obs = BenchmarkObservation(screenshot=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[500, 500, 600, 600]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            action = executor_with_endpoint._ground_click_http(obs, "button")

            # Verify no image in the content
            payload = (
                mock_post.call_args.kwargs.get("json")
                or mock_post.call_args[1].get("json")
            )
            content = payload["messages"][0]["content"]
            image_items = [c for c in content if c["type"] == "image_url"]
            assert len(image_items) == 0

            assert action.type == "click"


class TestPromptConsistency:
    """Verify DemoExecutor uses the same prompt format as PlannerGrounderAgent."""

    def test_prompt_matches_planner_grounder_format(
        self, executor_with_endpoint, fake_observation
    ):
        """The HTTP prompt should match _GROUNDER_PROMPT_BBOX from
        planner_grounder_agent.py."""
        from openadapt_evals.agents.planner_grounder_agent import (
            _GROUNDER_PROMPT_BBOX,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "[10, 20, 30, 40]"}}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response) as mock_post:
            executor_with_endpoint._ground_click_http(
                fake_observation, "the Start menu"
            )

            payload = (
                mock_post.call_args.kwargs.get("json")
                or mock_post.call_args[1].get("json")
            )
            content = payload["messages"][0]["content"]
            text_items = [c for c in content if c["type"] == "text"]
            sent_prompt = text_items[0]["text"]

            # The reference prompt from PlannerGrounderAgent
            expected_prompt = _GROUNDER_PROMPT_BBOX.format(
                instruction="the Start menu"
            )

            # Compare the core content (strip whitespace differences)
            assert "Outline the position" in sent_prompt
            assert "Outline the position" in expected_prompt
            assert "[x1,y1,x2,y2]" in sent_prompt
            assert "[x1,y1,x2,y2]" in expected_prompt
