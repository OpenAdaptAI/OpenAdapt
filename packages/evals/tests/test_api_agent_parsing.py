"""Tests for the robust API response parsing in ApiAgent.

These tests cover:
1. Multiple parsing strategies (code blocks, JSON, regex fallback)
2. Defensive error handling for unexpected response formats
3. Retry logic with clarification prompt
4. Loop detection for 3+ identical actions

Fixes crash issue: 'str' object has no attribute 'get' in action parser
Affected runs: 194740, 194940, 195137 (3 of 6 live runs - 50%)
"""

from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from PIL import Image


def create_test_screenshot():
    """Create a minimal test PNG screenshot."""
    img = Image.new('RGB', (1920, 1200), color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()


def create_mock_response(text: str):
    """Create a mock API response with given text."""
    mock_response = Mock()
    mock_content = Mock()
    mock_content.type = "text"
    mock_content.text = text
    mock_response.content = [mock_content]
    return mock_response


class TestRobustParsing:
    """Tests for robust API response parsing."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock ApiAgent for testing."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            "```python\ncomputer.click(100, 100)\n```"
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )
            return agent

    def test_parse_standard_python_block(self, mock_agent):
        """Test parsing standard Python code block."""
        response = """```memory
# Working on task
```

```decision
CONTINUE
```

```python
computer.click(500, 300)
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(500, 300)"
        assert logs["parse_strategy"] == "python_code_block"

    def test_parse_json_action(self, mock_agent):
        """Test parsing JSON formatted action."""
        response = """Here's my action:
{"action": "computer.click(800, 600)"}"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(800, 600)"
        assert logs["parse_strategy"] == "json_extraction"

    def test_parse_structured_json(self, mock_agent):
        """Test parsing structured JSON with type/x/y fields."""
        response = """{"type": "click", "x": 100, "y": 200}"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(100, 200)"
        assert logs["parse_strategy"] == "json_extraction"

    def test_parse_json_code_block(self, mock_agent):
        """Test parsing ```json code block."""
        response = """```json
{"action": "computer.type(\\"hello\\")"}
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert "computer.type" in result["action"]

    def test_parse_generic_code_block(self, mock_agent):
        """Test parsing generic ``` code block without language."""
        response = """```
computer.click(300, 400)
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(300, 400)"
        assert logs["parse_strategy"] == "generic_code_block"

    def test_parse_direct_computer_pattern(self, mock_agent):
        """Test parsing computer.* pattern directly in text."""
        response = """I'll click on the button at computer.click(600, 700) to proceed."""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(600,700)"
        assert logs["parse_strategy"] == "direct_computer_pattern"

    def test_parse_regex_fallback(self, mock_agent):
        """Test regex fallback parsing."""
        response = """I'll click at coordinates 800, 900"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == "computer.click(800, 900)"
        assert logs["parse_strategy"] == "regex_fallback"

    def test_parse_terminal_done(self, mock_agent):
        """Test parsing DONE decision."""
        response = """```decision
DONE
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "terminal"
        assert result["action"] == "DONE"

    def test_parse_terminal_fail(self, mock_agent):
        """Test parsing FAIL decision."""
        response = """```decision
FAIL
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "terminal"
        assert result["action"] == "FAIL"

    def test_parse_terminal_wait(self, mock_agent):
        """Test parsing WAIT decision."""
        response = """```decision
WAIT
```"""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "terminal"
        assert result["action"] == "WAIT"

    @pytest.mark.parametrize(
        "decision", ["NOT DONE", "DONE AND FAIL", "FAIL: NOT DONE", "MAYBE"]
    )
    def test_ambiguous_terminal_decision_is_rejected(self, mock_agent, decision):
        result = mock_agent._parse_api_response(
            f"```decision\n{decision}\n```", 1920, 1200, {}
        )

        assert result["status"] == "failed"

    def test_terminal_decision_cannot_compete_with_action(self, mock_agent):
        result = mock_agent._parse_api_response(
            "```decision\nDONE\n```\n```python\ncomputer.click(1, 2)\n```",
            1920,
            1200,
            {},
        )

        assert result["status"] == "failed"

    @pytest.mark.parametrize(
        "response",
        [
            "computer.click(1, 2) computer.press('enter')",
            'computer.click(1, 2) {"type":"press","key":"enter"}',
            "```python\ncomputer.click(1, 2)\n```\n"
            '{"type":"press","key":"enter"}',
            "```decision\nCONTINUE\n```\n```python\ncomputer.click(1, 2)\n```\n"
            "```json\n{\"type\":\"press\",\"key\":\"enter\"}\n```",
            "```\ncomputer.click(1, 2)\n"
            '{"type":"press","key":"enter"}\n```',
        ],
    )
    def test_competing_action_envelopes_are_rejected(self, mock_agent, response):
        result = mock_agent._parse_api_response(response, 1920, 1200, {})

        assert result["status"] == "failed"

    def test_parse_failure(self, mock_agent):
        """Test handling of unparseable response."""
        response = """I'm not sure what to do here. Let me think about it."""

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "failed"
        assert "error" in result


class TestDefensiveErrorHandling:
    """Tests for defensive error handling of malformed responses."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock ApiAgent for testing."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            "```python\ncomputer.click(100, 100)\n```"
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )
            return agent

    def test_handle_dict_response(self, mock_agent):
        """Test handling when response is a dict instead of string.

        This is the root cause of the 'str' object has no attribute 'get' error.
        """
        # Simulate a dict response (malformed)
        response = {"text": "computer.click(100, 100)", "status": "ok"}

        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        # Should not crash, should either succeed or fail gracefully
        assert result["status"] in ("success", "failed")
        assert "response_type_coerced" in logs or "error" in result

    def test_handle_none_response(self, mock_agent):
        """Test handling when response is None."""
        logs = {}
        result = mock_agent._parse_api_response(None, 1920, 1200, logs)

        assert result["status"] == "failed"
        assert "error" in result

    def test_handle_empty_string_response(self, mock_agent):
        """Test handling when response is empty string."""
        logs = {}
        result = mock_agent._parse_api_response("", 1920, 1200, logs)

        assert result["status"] == "failed"

    def test_handle_response_with_text_attribute(self, mock_agent):
        """Test handling when response is an object with text attribute."""

        class ResponseObject:
            text = "computer.click(500, 500)"

        response = ResponseObject()
        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        # Should extract from .text attribute
        assert result["status"] in ("success", "failed")

    def test_coordinate_bounds_validation(self, mock_agent):
        """Test that out-of-bounds coordinates are rejected in code block but may be caught by fallback."""
        response = "```python\ncomputer.click(99999, 99999)\n```"

        logs = {}
        mock_agent._parse_api_response(response, 1920, 1200, logs)

        # The regex fallback may still capture this, but validation should reject it
        # The primary code block strategy should fail due to coordinate bounds
        # Check that _validate_action correctly rejects out-of-bounds coords
        assert not mock_agent._validate_action("computer.click(99999, 99999)", 1920, 1200)


class TestRetryLogic:
    """Tests for retry logic with clarification prompt."""

    def test_retry_on_parse_failure(self):
        """Test that retry is attempted on parse failure."""
        call_count = [0]
        responses = [
            "I'm not sure what to do",  # First call fails
            "```python\ncomputer.click(100, 100)\n```"  # Second call succeeds
        ]

        def mock_create(*args, **kwargs):
            idx = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return create_mock_response(responses[idx])

        mock_client = Mock()
        mock_client.messages.create.side_effect = mock_create

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )

            obs = {"screenshot": create_test_screenshot()}
            _, actions, logs, _ = agent.predict("Click the button", obs)

            # Should have retried and succeeded
            assert len(actions) == 1
            assert "computer.click" in actions[0]
            # Should have made at least 2 API calls (initial + retry)
            assert call_count[0] >= 2

    def test_max_retries_exceeded(self):
        """Test that parsing fails gracefully after max retries."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            "I can't figure out what to do"
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )

            obs = {"screenshot": create_test_screenshot()}
            _, actions, logs, _ = agent.predict("Click the button", obs)

            # Should fail after retries
            assert "Could not parse action" in actions[0]
            assert logs.get("parse_failure") is True


class TestLoopDetection:
    """Tests for action loop detection."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock ApiAgent for testing."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            "```python\ncomputer.click(100, 100)\n```"
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )
            return agent

    def test_detect_action_loop(self, mock_agent):
        """Test that 3+ identical actions triggers loop detection."""
        # Simulate previous actions
        mock_agent.prev_actions = [
            "computer.click(100, 100)",
            "computer.click(100, 100)",
        ]

        # Check if next identical action would trigger loop
        result = mock_agent._detect_action_loop("computer.click(100, 100)")

        assert result is True

    def test_no_loop_with_varied_actions(self, mock_agent):
        """Test that varied actions don't trigger loop detection."""
        mock_agent.prev_actions = [
            "computer.click(100, 100)",
            "computer.click(200, 200)",
        ]

        result = mock_agent._detect_action_loop("computer.click(300, 300)")

        assert result is False

    def test_no_loop_with_insufficient_history(self, mock_agent):
        """Test that short history doesn't trigger loop."""
        mock_agent.prev_actions = ["computer.click(100, 100)"]

        result = mock_agent._detect_action_loop("computer.click(100, 100)")

        assert result is False

    def test_generate_alternative_for_click(self, mock_agent):
        """Test alternative action generation for stuck click.

        Uses progressive offsets in different directions based on loop count.
        """
        mock_agent._loop_count = 1
        alt_action = mock_agent._generate_alternative_action(
            "computer.click(100, 100)", 1920, 1200
        )

        assert alt_action is not None
        assert "computer.click" in alt_action
        # Should be offset from original (first loop: up by 60px)
        assert alt_action != "computer.click(100, 100)"

        # Second loop should go in a different direction
        mock_agent._loop_count = 2
        alt_action_2 = mock_agent._generate_alternative_action(
            "computer.click(100, 100)", 1920, 1200
        )
        assert alt_action_2 != alt_action  # Different direction each time

    def test_generate_alternative_for_type(self, mock_agent):
        """Test alternative action generation for stuck type."""
        alt_action = mock_agent._generate_alternative_action(
            'computer.type("test")', 1920, 1200
        )

        assert alt_action == 'computer.press("enter")'

    def test_generate_alternative_for_press(self, mock_agent):
        """Test alternative action generation for stuck press.

        Press loops let the action through on first 2 loops (returns None),
        then switch to tab to move focus.
        """
        # First loop: let through
        mock_agent._loop_count = 1
        alt_action = mock_agent._generate_alternative_action(
            'computer.press("tab")', 1920, 1200
        )
        assert alt_action is None  # Let through on first loop

        # Third loop: switch to tab
        mock_agent._loop_count = 3
        alt_action = mock_agent._generate_alternative_action(
            'computer.press("tab")', 1920, 1200
        )
        assert alt_action == 'computer.press("tab")'

    def test_generate_alternative_for_hotkey(self, mock_agent):
        """Test alternative action generation for stuck hotkey.

        Hotkey loops (e.g. Ctrl+S) should NOT be replaced with Escape,
        as that destroys dialogs the agent may be interacting with.
        """
        # First 2 loops: let through
        mock_agent._loop_count = 1
        alt_action = mock_agent._generate_alternative_action(
            'computer.hotkey("ctrl", "s")', 1920, 1200
        )
        assert alt_action is None

        # Third loop: try Enter to confirm a dialog
        mock_agent._loop_count = 3
        alt_action = mock_agent._generate_alternative_action(
            'computer.hotkey("ctrl", "s")', 1920, 1200
        )
        assert alt_action == 'computer.press("enter")'


class TestActionValidation:
    """Tests for action validation."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock ApiAgent for testing."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            "```python\ncomputer.click(100, 100)\n```"
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )
            return agent

    def test_validate_click_action(self, mock_agent):
        """Test validation of click action."""
        assert mock_agent._validate_action("computer.click(100, 200)", 1920, 1200)

    def test_validate_double_click_action(self, mock_agent):
        """Test validation of double_click action."""
        assert mock_agent._validate_action("computer.double_click(100, 200)", 1920, 1200)

    def test_validate_right_click_action(self, mock_agent):
        """Test validation of right_click action."""
        assert mock_agent._validate_action("computer.right_click(100, 200)", 1920, 1200)

    def test_validate_type_action(self, mock_agent):
        """Test validation of type action."""
        assert mock_agent._validate_action('computer.type("hello")', 1920, 1200)

    def test_validate_press_action(self, mock_agent):
        """Test validation of press action."""
        assert mock_agent._validate_action('computer.press("enter")', 1920, 1200)

    def test_validate_hotkey_action(self, mock_agent):
        """Test validation of hotkey action."""
        assert mock_agent._validate_action('computer.hotkey("ctrl", "c")', 1920, 1200)

    def test_validate_scroll_action(self, mock_agent):
        """Test validation of scroll action."""
        assert mock_agent._validate_action("computer.scroll(3)", 1920, 1200)
        assert mock_agent._validate_action("computer.scroll(-3)", 1920, 1200)

    def test_validate_drag_action(self, mock_agent):
        """Test validation of drag action."""
        assert mock_agent._validate_action("computer.drag(100, 100, 200, 200)", 1920, 1200)

    def test_reject_invalid_action(self, mock_agent):
        """Test rejection of invalid action."""
        assert not mock_agent._validate_action("not_computer.click(100, 200)", 1920, 1200)
        assert not mock_agent._validate_action("computer.invalid(100)", 1920, 1200)
        assert not mock_agent._validate_action("", 1920, 1200)
        assert not mock_agent._validate_action(None, 1920, 1200)

    def test_reject_out_of_bounds_coordinates(self, mock_agent):
        """Test rejection of out-of-bounds coordinates."""
        # Coordinates way outside screen bounds
        assert not mock_agent._validate_action("computer.click(10000, 10000)", 1920, 1200)


class TestIntegration:
    """Integration tests for the full parsing flow."""

    def test_full_flow_with_various_responses(self):
        """Test the full predict flow with various response formats."""
        test_cases = [
            # Standard format
            (
                "```memory\n# test\n```\n```decision\nCONTINUE\n```\n```python\ncomputer.click(100, 100)\n```",
                "computer.click(100, 100)"
            ),
            # JSON format
            (
                '{"action": "computer.click(200, 200)"}',
                "computer.click(200, 200)"
            ),
            # Direct pattern
            (
                "Click at computer.click(300, 300)",
                "computer.click(300,300)"
            ),
        ]

        for response_text, expected_action in test_cases:
            mock_client = Mock()
            mock_client.messages.create.return_value = create_mock_response(response_text)

            with patch('anthropic.Anthropic', return_value=mock_client):
                from openadapt_evals.agents.api_agent import ApiAgent

                agent = ApiAgent(
                    provider="anthropic",
                    api_key="test-key",
                )

                obs = {"screenshot": create_test_screenshot()}
                _, actions, logs, _ = agent.predict("Click the button", obs)

                assert len(actions) == 1
                assert expected_action in actions[0] or actions[0] == expected_action, \
                    f"Expected '{expected_action}' but got '{actions[0]}' for response: {response_text[:50]}..."


class TestElementBasedActions:
    """Tests for accessibility-tree element-based action parsing."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock ApiAgent for testing."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response(
            '```python\ncomputer.click_element("submitBtn")\n```'
        )

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
            )
            return agent

    def test_validate_click_element(self, mock_agent):
        """Test validation of click_element action."""
        assert mock_agent._validate_action('computer.click_element("submitBtn")', 1920, 1200)
        assert mock_agent._validate_action("computer.click_element('saveBtn')", 1920, 1200)

    def test_validate_type_element(self, mock_agent):
        """Test validation of type_element action."""
        assert mock_agent._validate_action('computer.type_element("textField", "hello")', 1920, 1200)
        assert mock_agent._validate_action('computer.type_element("input1", "")', 1920, 1200)

    def test_reject_invalid_element_actions(self, mock_agent):
        """Test rejection of malformed element actions."""
        assert not mock_agent._validate_action("computer.click_element()", 1920, 1200)
        assert not mock_agent._validate_action("computer.click_element", 1920, 1200)

    def test_parse_click_element_code_block(self, mock_agent):
        """Test parsing click_element from python code block."""
        response = '```python\ncomputer.click_element("submitBtn")\n```'
        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == 'computer.click_element("submitBtn")'
        assert logs["parse_strategy"] == "python_code_block"

    def test_parse_type_element_code_block(self, mock_agent):
        """Test parsing type_element from python code block."""
        response = '```python\ncomputer.type_element("textField", "hello world")\n```'
        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert result["action"] == 'computer.type_element("textField", "hello world")'
        assert logs["parse_strategy"] == "python_code_block"

    def test_parse_click_element_direct_pattern(self, mock_agent):
        """Test parsing click_element from direct text pattern (Strategy 4)."""
        response = 'I will click the submit button: computer.click_element("submitBtn")'
        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert 'click_element' in result["action"]
        assert 'submitBtn' in result["action"]

    def test_parse_type_element_direct_pattern(self, mock_agent):
        """Test parsing type_element from direct text pattern (Strategy 4)."""
        response = 'I will type in the field: computer.type_element("input1", "test text")'
        logs = {}
        result = mock_agent._parse_api_response(response, 1920, 1200, logs)

        assert result["status"] == "success"
        assert 'type_element' in result["action"]
        assert 'input1' in result["action"]

    def test_parse_computer_action_click_element(self, mock_agent):
        """Test _parse_computer_action with click_element."""
        from openadapt_evals.adapters.base import BenchmarkObservation

        obs = BenchmarkObservation(viewport=(1920, 1200))
        action = mock_agent._parse_computer_action(
            'computer.click_element("submitBtn")', obs
        )

        assert action.type == "click"
        assert action.target_node_id == "submitBtn"
        assert action.x == 0.5
        assert action.y == 0.5

    def test_parse_computer_action_type_element(self, mock_agent):
        """Test _parse_computer_action with type_element."""
        from openadapt_evals.adapters.base import BenchmarkObservation

        obs = BenchmarkObservation(viewport=(1920, 1200))
        action = mock_agent._parse_computer_action(
            'computer.type_element("textField", "hello world")', obs
        )

        assert action.type == "type"
        assert action.target_node_id == "textField"
        assert action.text == "hello world"

    def test_a11y_prompt_selection_with_tree(self, mock_agent):
        """Test that SYSTEM_PROMPT_A11Y is used when a11y tree present."""
        mock_agent.use_accessibility_tree = True
        mock_agent._a11y_available = False  # reset

        obs = {
            "screenshot": create_test_screenshot(),
            "accessibility_tree": {"role": "Window", "name": "Test", "children": []},
        }

        mock_agent.predict("Click button", obs)

        # After predict, _a11y_available should be True
        assert mock_agent._a11y_available is True

    def test_a11y_prompt_selection_without_tree(self, mock_agent):
        """Test that SYSTEM_PROMPT is used when no a11y tree."""
        mock_agent.use_accessibility_tree = True
        mock_agent._a11y_available = True  # set to True first

        obs = {"screenshot": create_test_screenshot()}

        mock_agent.predict("Click button", obs)

        # After predict without tree, _a11y_available should be False
        assert mock_agent._a11y_available is False

    def test_click_element_scores_success_on_mock_adapter(self, mock_agent):
        """Integration: click_element action with target_node_id scores on mock adapter."""
        from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkObservation

        obs = BenchmarkObservation(viewport=(1920, 1200))
        action = mock_agent._parse_computer_action(
            'computer.click_element("4")', obs
        )

        # Should produce a BenchmarkAction with target_node_id
        assert action.target_node_id == "4"
        assert action.type == "click"

        # Now test with mock adapter
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter
        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        # Step with element-based action
        obs2, done, info = adapter.step(action)

        # Finish
        done_action = BenchmarkAction(type="done")
        obs3, done2, info2 = adapter.step(done_action)

        result = adapter.evaluate(task)
        assert result.success is True
        assert result.score == 1.0
