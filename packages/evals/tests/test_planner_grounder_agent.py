"""Tests for PlannerGrounderAgent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.planner_grounder_agent import (
    _PLANNER_PROMPT,
    PlannerGrounderAgent,
    _action_to_planner_output,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def task() -> BenchmarkTask:
    return BenchmarkTask(
        task_id="test_1",
        instruction="Open the Settings app and enable dark mode",
        domain="desktop",
    )


@pytest.fixture
def observation() -> BenchmarkObservation:
    return BenchmarkObservation(
        screenshot=b"\x89PNG\r\n\x1a\nfake",
        viewport=(1920, 1200),
        accessibility_tree={"role": "window", "name": "Desktop", "id": "1"},
    )


class MockPlannerAgent:
    """Mock planner that returns a COMMAND action with instruction in raw_action."""

    def __init__(self, instruction: str = "Click the Settings icon"):
        self.instruction = instruction
        self._reset_called = False

    def act(self, observation, task, history=None):
        return BenchmarkAction(
            type="click",
            x=0.5,
            y=0.5,
            raw_action={"instruction": self.instruction},
        )

    def reset(self):
        self._reset_called = True


class MockGrounderAgent:
    """Mock grounder that returns a click at specific coordinates."""

    def __init__(self, x: float = 0.8, y: float = 0.3):
        self.x = x
        self.y = y
        self._reset_called = False

    def act(self, observation, task, history=None):
        return BenchmarkAction(type="click", x=self.x, y=self.y)

    def reset(self):
        self._reset_called = True


class MockDonePlannerAgent:
    """Mock planner that returns a done action."""

    def act(self, observation, task, history=None):
        return BenchmarkAction(type="done", raw_action={"reasoning": "Task complete"})

    def reset(self):
        pass


class MockErrorPlannerAgent:
    """Mock planner that reports a terminal provider failure."""

    def act(self, observation, task, history=None):
        return BenchmarkAction(
            type="error",
            raw_action={
                "error": "provider offline",
                "error_type": "infrastructure",
            },
        )

    def reset(self):
        pass


# -- Tests: Agent-based planner + grounder ------------------------------------


class TestAgentBasedPipeline:
    def test_planner_instruction_flows_to_grounder(self, observation, task):
        """Planner instruction is passed to the grounder as the task instruction."""
        planner = MockPlannerAgent(instruction="Click the Settings icon")
        grounder = MockGrounderAgent(x=0.8, y=0.3)

        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.8
        assert action.y == 0.3

    def test_planner_done_returns_done(self, observation, task):
        """When planner returns DONE, agent returns done without calling grounder."""
        planner = MockDonePlannerAgent()
        grounder = MockGrounderAgent()

        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)
        action = agent.act(observation, task)

        assert action.type == "done"
        assert action.raw_action["source"] == "planner"

    def test_planner_error_stops_before_grounding(self, observation, task):
        """A planner failure cannot be converted into a grounded action."""
        planner = MockErrorPlannerAgent()
        grounder = MagicMock()

        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)
        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["error"] == "provider offline"
        assert action.raw_action["error_type"] == "infrastructure"
        grounder.act.assert_not_called()

    def test_planner_metadata_attached_to_action(self, observation, task):
        """Planner output dict is attached to the final action's raw_action."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()

        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)
        action = agent.act(observation, task)

        assert "planner_output" in action.raw_action
        assert action.raw_action["planner_output"]["decision"] == "COMMAND"


class TestActionHistory:
    def test_history_accumulates(self, observation, task):
        """Action history grows with each step."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent.act(observation, task)
        assert len(agent._action_history) == 1

        agent.act(observation, task)
        assert len(agent._action_history) == 2

    def test_reset_clears_history(self, observation, task):
        """reset() clears action history."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent.act(observation, task)
        assert len(agent._action_history) == 1

        agent.reset()
        assert len(agent._action_history) == 0

    def test_reset_propagates_to_sub_agents(self):
        """reset() calls reset on planner and grounder agents."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent.reset()
        assert planner._reset_called
        assert grounder._reset_called

    def test_done_recorded_in_history(self, observation, task):
        """DONE decision is recorded in action history."""
        planner = MockDonePlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent.act(observation, task)
        assert len(agent._action_history) == 1
        assert "DONE" in agent._action_history[0]


# -- Tests: VLM-based planner + grounder -------------------------------------


class TestVLMBasedPipeline:
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    @patch("openadapt_evals.training.trl_rollout.parse_action_json")
    def test_vlm_planner_command_grounder_click(
        self, mock_parse, mock_extract, mock_vlm, observation, task
    ):
        """VLM planner outputs COMMAND, VLM grounder outputs click coordinates."""
        # Planner returns COMMAND with instruction.
        mock_vlm.return_value = '{"decision": "COMMAND", "instruction": "Click Settings"}'
        mock_extract.return_value = {
            "decision": "COMMAND",
            "instruction": "Click Settings",
            "reasoning": "Need to open settings",
        }
        # Grounder returns a click action.
        mock_parse.return_value = BenchmarkAction(type="click", x=0.5, y=0.3)

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder="gpt-4.1-mini",
            planner_provider="anthropic",
            grounder_provider="openai",
        )
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3
        # vlm_call should be called twice: once for planner, once for grounder.
        assert mock_vlm.call_count == 2

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_vlm_planner_done(self, mock_extract, mock_vlm, observation, task):
        """VLM planner outputs DONE, agent returns done without calling grounder."""
        mock_vlm.return_value = '{"decision": "DONE"}'
        mock_extract.return_value = {
            "decision": "DONE",
            "instruction": "",
            "reasoning": "Task is complete",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder="gpt-4.1-mini",
            planner_provider="anthropic",
            grounder_provider="openai",
        )
        action = agent.act(observation, task)

        assert action.type == "done"
        assert action.raw_action["source"] == "planner"
        # vlm_call should only be called once (planner only).
        assert mock_vlm.call_count == 1

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_vlm_planner_fail(self, mock_extract, mock_vlm, observation, task):
        """VLM planner FAIL stays distinct from DONE."""
        mock_vlm.return_value = '{"decision": "FAIL"}'
        mock_extract.return_value = {
            "decision": "FAIL",
            "instruction": "",
            "reasoning": "Cannot find the required element",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder="gpt-4.1-mini",
            planner_provider="anthropic",
            grounder_provider="openai",
        )
        action = agent.act(observation, task)

        assert action.type == "error"
        assert "fail_reason" in action.raw_action

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_vlm_planner_json_parse_failure_uses_raw_text(
        self, mock_extract, mock_vlm, observation, task
    ):
        """When planner JSON parse fails, raw text is used as instruction."""
        mock_vlm.return_value = "Click the big red button"
        mock_extract.return_value = None  # JSON extraction fails

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        # Should still produce an action because raw text fallback is used.
        assert action.type == "click"


class TestGrounderRetry:
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.training.trl_rollout.parse_action_json")
    def test_grounder_retries_on_parse_failure(
        self, mock_parse, mock_vlm, observation, task
    ):
        """Grounder retries once with simplified prompt when first parse fails."""
        # First call fails (returns done from parse failure), second succeeds.
        mock_parse.side_effect = [
            BenchmarkAction(type="done"),  # First parse fails
            BenchmarkAction(type="click", x=0.5, y=0.3),  # Retry succeeds
        ]
        mock_vlm.return_value = "some output"

        agent = PlannerGrounderAgent(
            planner=MockPlannerAgent(),
            grounder="gpt-4.1-mini",
            grounder_provider="openai",
        )
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.5
        # vlm_call called twice for grounder (original + retry).
        assert mock_vlm.call_count == 2

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.training.trl_rollout.parse_action_json")
    def test_grounder_returns_error_after_both_fail(
        self, mock_parse, mock_vlm, observation, task
    ):
        """Two parse failures do not become task completion."""
        mock_parse.return_value = BenchmarkAction(type="done")
        mock_vlm.return_value = "unparseable gibberish"

        agent = PlannerGrounderAgent(
            planner=MockPlannerAgent(),
            grounder="gpt-4.1-mini",
            grounder_provider="openai",
        )
        action = agent.act(observation, task)

        assert action.type == "error"


# -- Tests: HTTP grounder ----------------------------------------------------


class TestHTTPGrounder:
    def test_http_grounder_requires_endpoint(self):
        """grounder_provider='http' requires grounder_endpoint."""
        with pytest.raises(ValueError, match="grounder_endpoint is required"):
            PlannerGrounderAgent(
                planner=MockPlannerAgent(),
                grounder="http",
                grounder_provider="http",
            )

    @patch("requests.post")
    def test_http_grounder_calls_endpoint(self, mock_post, observation, task):
        """HTTP grounder calls OpenAI-compatible endpoint."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"type": "click", "x": 0.7, "y": 0.2}'}}]
        }
        mock_post.return_value = mock_resp

        agent = PlannerGrounderAgent(
            planner=MockPlannerAgent(),
            grounder="http",
            grounder_provider="http",
            grounder_endpoint="http://gpu-box:8080",
        )
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.7
        assert action.y == 0.2
        # Verify it called the OpenAI-compatible endpoint
        call_url = mock_post.call_args[0][0]
        assert "/v1/chat/completions" in call_url

    @pytest.mark.parametrize(
        "raw",
        [
            '{"type":"done","x":0.5,"y":0.5}',
            '{"type":"click","x":0.1,"x":0.9,"y":0.5}',
            '{"type":"click","x":0.5,"y":0.5,"key":"enter"}',
            '{"type":"click","x":0.5,"y":0.5}{"type":"done"}',
            '{"type":"click","x":NaN,"y":0.5}',
            '{"type":"click","x":500,"y":0.5}',
            "[900, 100, 100, 200]",
            "[500, 0.5]",
            "[-1, 100]",
            "[1001, 500]",
        ],
    )
    def test_grounder_rejects_ambiguous_or_invalid_coordinates(self, raw):
        action = PlannerGrounderAgent._parse_bbox_to_action(raw)

        assert action.type == "error"
        assert action.raw_action["parse_error"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('{"type":"click","x":0.25,"y":1.0}', (0.25, 1.0)),
            ("[250, 200, 750, 800]", (0.5, 0.5)),
            ("[0.25, 1.0]", (0.25, 1.0)),
            ("[250, 750]", (0.25, 0.75)),
        ],
    )
    def test_grounder_accepts_one_coherent_coordinate_space(self, raw, expected):
        action = PlannerGrounderAgent._parse_bbox_to_action(raw)

        assert action.type == "click"
        assert (action.x, action.y) == expected


# -- Tests: _action_to_planner_output helper ----------------------------------


class TestActionToPlannerOutput:
    def test_done_action(self):
        """Done action maps to DONE decision."""
        action = BenchmarkAction(
            type="done", raw_action={"reasoning": "all done"}
        )
        result = _action_to_planner_output(action)

        assert result["decision"] == "DONE"
        assert result["reasoning"] == "all done"

    def test_click_action_with_instruction(self):
        """Click action with instruction in raw_action uses that instruction."""
        action = BenchmarkAction(
            type="click",
            x=0.5,
            y=0.5,
            raw_action={"instruction": "Click the submit button"},
        )
        result = _action_to_planner_output(action)

        assert result["decision"] == "COMMAND"
        assert result["instruction"] == "Click the submit button"

    def test_click_action_without_raw_action(self):
        """Click action without raw_action falls back to action_to_string."""
        action = BenchmarkAction(type="click", x=0.5, y=0.3)
        result = _action_to_planner_output(action)

        assert result["decision"] == "COMMAND"
        assert "CLICK" in result["instruction"]
        assert "0.500" in result["instruction"]

    def test_done_action_no_raw_action(self):
        """Done action without raw_action still returns DONE."""
        action = BenchmarkAction(type="done")
        result = _action_to_planner_output(action)

        assert result["decision"] == "DONE"
        assert result["reasoning"] == ""

    def test_error_action_maps_to_fail_decision(self):
        """Planner errors remain terminal and retain their diagnostics."""
        action = BenchmarkAction(
            type="error",
            raw_action={
                "error": "provider offline",
                "error_type": "infrastructure",
            },
        )

        result = _action_to_planner_output(action)

        assert result == {
            "decision": "ERROR",
            "instruction": "",
            "reasoning": "provider offline",
            "error": "provider offline",
            "error_type": "infrastructure",
        }


# -- Tests: Accessibility tree formatting -------------------------------------


class TestA11yTreeInPlanner:
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_a11y_tree_included_in_planner_prompt(
        self, mock_extract, mock_vlm, task
    ):
        """Accessibility tree is formatted and included in the planner prompt."""
        observation = BenchmarkObservation(
            screenshot=b"\x89PNG\r\n\x1a\nfake",
            viewport=(1920, 1200),
            accessibility_tree={
                "role": "window",
                "name": "Desktop",
                "id": "1",
                "children": [
                    {"role": "button", "name": "Settings", "id": "2"},
                ],
            },
        )

        mock_extract.return_value = {
            "decision": "DONE",
            "instruction": "",
            "reasoning": "",
        }
        mock_vlm.return_value = "{}"

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder="gpt-4.1-mini",
            planner_provider="anthropic",
            grounder_provider="openai",
        )
        agent.act(observation, task)

        # Verify the prompt passed to vlm_call includes the a11y tree.
        call_args = mock_vlm.call_args_list[0]
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "Settings" in prompt
        assert "button" in prompt

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_no_a11y_tree_uses_fallback(self, mock_extract, mock_vlm, task):
        """When no accessibility tree is available, prompt shows 'not available'."""
        observation = BenchmarkObservation(
            screenshot=b"\x89PNG\r\n\x1a\nfake",
            viewport=(1920, 1200),
            accessibility_tree=None,
        )

        mock_extract.return_value = {
            "decision": "DONE",
            "instruction": "",
            "reasoning": "",
        }
        mock_vlm.return_value = "{}"

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder="gpt-4.1-mini",
            planner_provider="anthropic",
            grounder_provider="openai",
        )
        agent.act(observation, task)

        call_args = mock_vlm.call_args_list[0]
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "(not available)" in prompt


# -- Tests: Mixed planner/grounder types -------------------------------------


class TestMixedTypes:
    def test_agent_planner_vlm_grounder(self, observation, task):
        """Agent planner + VLM grounder works correctly."""
        planner = MockPlannerAgent(instruction="Click the big button")

        with patch("openadapt_evals.vlm.vlm_call") as mock_vlm, patch(
            "openadapt_evals.training.trl_rollout.parse_action_json"
        ) as mock_parse:
            mock_vlm.return_value = '{"type": "click", "x": 0.5, "y": 0.3}'
            mock_parse.return_value = BenchmarkAction(type="click", x=0.5, y=0.3)

            agent = PlannerGrounderAgent(
                planner=planner,
                grounder="gpt-4.1-mini",
                grounder_provider="openai",
            )
            action = agent.act(observation, task)

            assert action.type == "click"
            assert action.x == 0.5

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_vlm_planner_agent_grounder(
        self, mock_extract, mock_vlm, observation, task
    ):
        """VLM planner + agent grounder works correctly."""
        mock_extract.return_value = {
            "decision": "COMMAND",
            "instruction": "Click OK",
            "reasoning": "",
        }
        mock_vlm.return_value = "{}"

        grounder = MockGrounderAgent(x=0.9, y=0.1)

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.9
        assert action.y == 0.1


# -- Tests: Structured planner output ----------------------------------------


class TestStructuredPlannerOutput:
    """Tests for structured action fields (action_type, action_value, target_description)."""

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_type_action(self, mock_extract, mock_vlm, observation, task):
        """Structured output with action_type='type' produces a type action."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "type",
            "action_value": "Hello World",
            "target_description": "the search box",
            "reasoning": "Need to type in search",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "type"
        assert action.text == "Hello World"
        # Grounder should NOT have been called for a type action.
        assert mock_vlm.call_count == 1  # planner only

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_key_action(self, mock_extract, mock_vlm, observation, task):
        """Structured output with action_type='key' produces a key action."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "key",
            "action_value": "Enter",
            "target_description": "",
            "reasoning": "Submit the form",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "key"
        assert action.key == "enter"

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_key_with_modifier(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Structured output with modifier+key combo like 'Ctrl+A'."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "key",
            "action_value": "Ctrl+A",
            "target_description": "",
            "reasoning": "Select all",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "key"
        assert action.key == "a"
        assert action.modifiers == ["ctrl"]

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_scroll_action(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Structured output with action_type='scroll' produces a scroll action."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "scroll",
            "action_value": "down",
            "target_description": "",
            "reasoning": "Scroll to see more",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "scroll"
        assert action.scroll_direction == "down"

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_click_calls_grounder(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Structured output with action_type='click' calls the grounder."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "click",
            "action_value": "",
            "target_description": "the OK button",
            "reasoning": "Confirm dialog",
        }

        grounder = MockGrounderAgent(x=0.6, y=0.4)
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "click"
        assert action.x == 0.6
        assert action.y == 0.4

    @pytest.mark.parametrize("action_type", ["click", "double_click"])
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_pointer_action_requires_a_target(
        self, mock_extract, mock_vlm, action_type, observation, task
    ):
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": action_type,
            "target_description": "",
        }
        grounder = MagicMock()
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["parse_error"] == "missing_target_description"
        grounder.act.assert_not_called()

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_backward_compat_instruction_field(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Legacy 'instruction' field still works without action_type."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "instruction": "Click the Settings button",
            "reasoning": "",
        }

        grounder = MockGrounderAgent(x=0.5, y=0.5)
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        # Should fall through to grounder via backward compat path
        assert action.type == "click"
        assert action.x == 0.5

    @pytest.mark.parametrize("decision", ["MAYBE", "", 1])
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_unknown_decision_cannot_act(
        self, mock_extract, mock_vlm, decision, observation, task
    ):
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": decision,
            "action_type": "click",
            "target_description": "delete account",
        }
        grounder = MagicMock()
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["parse_error"] == "invalid_decision"
        grounder.act.assert_not_called()

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_unknown_structured_action_cannot_fall_back_to_click(
        self, mock_extract, mock_vlm, observation, task
    ):
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "delete",
            "target_description": "delete account",
        }
        grounder = MagicMock()
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["parse_error"] == "invalid_action_type"
        grounder.act.assert_not_called()

    @pytest.mark.parametrize("direction", ["", "left", None])
    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_scroll_requires_explicit_supported_direction(
        self, mock_extract, mock_vlm, direction, observation, task
    ):
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "scroll",
            "action_value": direction,
        }
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MagicMock(),
            planner_provider="anthropic",
        )

        action = agent.act(observation, task)

        assert action.type == "error"
        assert action.raw_action["parse_error"] == "invalid_scroll_direction"


# -- Tests: Type-then-Enter queuing ------------------------------------------


class TestTypeWithNewlineSuffix:
    """Tests for the \\n suffix that queues an Enter keypress after typing."""

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_type_with_newline_queues_enter(
        self, mock_extract, mock_vlm, observation, task
    ):
        """action_value ending with \\n queues an Enter keypress."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "type",
            "action_value": "Hello\\n",
            "target_description": "the input field",
            "reasoning": "Type and submit",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )

        # First call: should return the type action
        action = agent.act(observation, task)
        assert action.type == "type"
        assert action.text == "Hello"
        assert len(agent._pending_actions) == 1

        # Second call: should drain the queued Enter
        action2 = agent.act(observation, task)
        assert action2.type == "key"
        assert action2.key == "enter"
        assert len(agent._pending_actions) == 0

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_type_without_newline_no_queue(
        self, mock_extract, mock_vlm, observation, task
    ):
        """action_value without \\n does not queue anything."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "type",
            "action_value": "Hello",
            "target_description": "the input field",
            "reasoning": "Just type",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "type"
        assert action.text == "Hello"
        assert len(agent._pending_actions) == 0


# -- Tests: Pending action queue ---------------------------------------------


class TestPendingActionQueue:
    """Tests for the _pending_actions queue behavior."""

    def test_pending_actions_execute_before_planner(self, observation, task):
        """Queued actions are returned before calling the planner."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        # Manually enqueue an action
        agent._pending_actions.append(
            BenchmarkAction(type="key", key="enter")
        )

        action = agent.act(observation, task)
        assert action.type == "key"
        assert action.key == "enter"
        # The planner should NOT have been consulted
        assert len(agent._pending_actions) == 0

    def test_pending_action_recorded_in_history(self, observation, task):
        """Queued actions are recorded in action history with (queued) suffix."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent._pending_actions.append(
            BenchmarkAction(type="key", key="tab")
        )
        agent.act(observation, task)

        assert len(agent._action_history) == 1
        assert "(queued)" in agent._action_history[0]
        assert "KEY" in agent._action_history[0]

    def test_reset_clears_pending_actions(self):
        """reset() clears the pending action queue."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent._pending_actions.append(
            BenchmarkAction(type="key", key="enter")
        )
        agent._pending_actions.append(
            BenchmarkAction(type="key", key="tab")
        )
        assert len(agent._pending_actions) == 2

        agent.reset()
        assert len(agent._pending_actions) == 0

    def test_multiple_pending_actions_drain_in_order(self, observation, task):
        """Multiple queued actions are drained FIFO across successive act() calls."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent._pending_actions.append(
            BenchmarkAction(type="key", key="enter")
        )
        agent._pending_actions.append(
            BenchmarkAction(type="key", key="tab")
        )

        action1 = agent.act(observation, task)
        assert action1.type == "key"
        assert action1.key == "enter"

        action2 = agent.act(observation, task)
        assert action2.type == "key"
        assert action2.key == "tab"

        # Third call should go to the planner (queue is empty)
        action3 = agent.act(observation, task)
        assert action3.type == "click"  # From MockGrounderAgent


# -- Tests: Continuation extraction ------------------------------------------


class TestContinuationExtraction:
    """Tests for _extract_continuation safety net on compound instructions."""

    def test_then_press_enter(self):
        """'type X then press Enter' extracts 'press Enter'."""
        result = PlannerGrounderAgent._extract_continuation(
            "type 'Hello' then press Enter"
        )
        assert result is not None
        assert "press Enter" in result

    def test_and_then_press_tab(self):
        """'type X and then press Tab' extracts 'press Tab'."""
        result = PlannerGrounderAgent._extract_continuation(
            "type 'username' and then press Tab"
        )
        assert result is not None
        assert "press Tab" in result

    def test_followed_by_press(self):
        """'type X followed by press Enter' extracts 'press Enter'."""
        result = PlannerGrounderAgent._extract_continuation(
            "type 'data' followed by press Enter"
        )
        assert result is not None
        assert "press Enter" in result

    def test_no_continuation(self):
        """Simple instruction without continuation returns None."""
        result = PlannerGrounderAgent._extract_continuation(
            "type 'Hello World'"
        )
        assert result is None

    def test_click_no_continuation(self):
        """Click instruction returns None."""
        result = PlannerGrounderAgent._extract_continuation(
            "Click the Submit button"
        )
        assert result is None

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_continuation_queues_followup(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Compound instruction via legacy format queues the follow-up action."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "instruction": "type 'Hello' then press Enter",
            "reasoning": "Type and submit",
        }

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )

        # First call: type action parsed from instruction
        action = agent.act(observation, task)
        assert action.type == "type"
        assert action.text == "Hello"

        # Follow-up Enter should be queued
        assert len(agent._pending_actions) == 1
        assert agent._pending_actions[0].type == "key"
        assert agent._pending_actions[0].key == "enter"

        # Second call: drains the queued Enter
        action2 = agent.act(observation, task)
        assert action2.type == "key"
        assert action2.key == "enter"


# -- Tests: double_click action type ------------------------------------------


class TestDoubleClick:
    """Tests for double_click action type parsing and handling."""

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_structured_double_click_calls_grounder_and_overrides_type(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Structured double_click calls grounder and sets action type to double_click."""
        mock_vlm.return_value = "{}"
        mock_extract.return_value = {
            "decision": "COMMAND",
            "action_type": "double_click",
            "action_value": "",
            "target_description": "the Notepad icon on desktop",
            "reasoning": "Need to open Notepad",
        }

        grounder = MockGrounderAgent(x=0.3, y=0.7)
        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=grounder,
            planner_provider="anthropic",
        )
        action = agent.act(observation, task)

        assert action.type == "double_click"
        assert action.x == 0.3
        assert action.y == 0.7

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_double_click_in_planner_prompt(
        self, mock_extract, mock_vlm, observation, task
    ):
        """Planner prompt includes double_click as a valid action type."""
        mock_extract.return_value = {
            "decision": "DONE",
            "instruction": "",
            "reasoning": "",
        }
        mock_vlm.return_value = "{}"

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )
        agent.act(observation, task)

        # Check that the prompt sent to vlm_call mentions double_click.
        call_args = mock_vlm.call_args_list[0]
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "double_click" in prompt

    def test_double_click_in_prompt_template(self):
        """The planner prompt template includes double_click as a valid action type."""
        assert "double_click" in _PLANNER_PROMPT
        assert "open/launch applications" in _PLANNER_PROMPT

    def test_parse_non_click_returns_none_for_double_click_instruction(self):
        """_parse_non_click_action returns None for 'double-click' text (needs grounder)."""
        result = PlannerGrounderAgent._parse_non_click_action(
            "Double-click the Notepad icon"
        )
        # Should return None so the grounder is called for coordinates.
        assert result is None


# -- Tests: Anti-loop detection ------------------------------------------------


class TestAntiLoopDetection:
    """Tests for the anti-loop detection that triggers after repeated identical actions."""

    def test_no_warning_with_few_actions(self, observation, task):
        """No anti-loop warning when fewer than threshold actions recorded."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        # Only 2 actions — below the threshold of 3.
        agent._action_history = [
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
        ]
        warning = agent._check_action_loop()
        assert warning == ""

    def test_warning_after_threshold_identical_actions(self, observation, task):
        """Anti-loop warning triggers after 3 identical instructions."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        # Simulate 3 identical instruction entries.
        agent._action_history = [
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
        ]
        warning = agent._check_action_loop()
        assert "WARNING" in warning
        assert "completely different approach" in warning

    def test_no_warning_with_varied_actions(self, observation, task):
        """No anti-loop warning when actions differ."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent._action_history = [
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "TYPE('hello') (instruction: Type hello)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
        ]
        warning = agent._check_action_loop()
        assert warning == ""

    def test_no_warning_with_queued_entries(self, observation, task):
        """No anti-loop warning when entries include queued actions (no instruction suffix)."""
        planner = MockPlannerAgent()
        grounder = MockGrounderAgent()
        agent = PlannerGrounderAgent(planner=planner, grounder=grounder)

        agent._action_history = [
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "KEY(enter) (queued)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
        ]
        warning = agent._check_action_loop()
        assert warning == ""

    @patch("openadapt_evals.vlm.vlm_call")
    @patch("openadapt_evals.vlm.extract_json")
    def test_anti_loop_warning_injected_in_planner_prompt(
        self, mock_extract, mock_vlm, observation, task
    ):
        """When anti-loop triggers, the warning is injected into the planner prompt."""
        mock_extract.return_value = {
            "decision": "DONE",
            "instruction": "",
            "reasoning": "",
        }
        mock_vlm.return_value = "{}"

        agent = PlannerGrounderAgent(
            planner="claude-sonnet-4-20250514",
            grounder=MockGrounderAgent(),
            planner_provider="anthropic",
        )

        # Seed history with 3 identical instructions.
        agent._action_history = [
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
            "CLICK(0.5, 0.5) (instruction: Click Settings)",
        ]

        agent.act(observation, task)

        # Check the prompt sent to vlm_call includes the warning.
        call_args = mock_vlm.call_args_list[0]
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "WARNING" in prompt
        assert "completely different approach" in prompt


# -- Tests: Dialog dismissal in planner prompt ----------------------------------


class TestDialogDismissalPrompt:
    """Tests for dialog dismissal guidance in the planner prompt."""

    def test_dialog_dismissal_in_prompt_template(self):
        """Planner prompt includes guidance to dismiss blocking dialogs."""
        assert "dialog boxes" in _PLANNER_PROMPT
        assert "dismiss them first" in _PLANNER_PROMPT
        assert "Escape" in _PLANNER_PROMPT
