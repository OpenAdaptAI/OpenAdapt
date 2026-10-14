"""Tests for SmolOperatorAgent action parsing and agent behavior."""

import pytest

from openadapt_evals.agents.smol_agent import (
    SmolOperatorAgent,
    _extract_action_string,
    parse_smol_action,
)

# ---------------------------------------------------------------------------
# Action string extraction
# ---------------------------------------------------------------------------


class TestExtractActionString:
    """Tests for _extract_action_string."""

    def test_plain_action(self):
        assert _extract_action_string("click(x=0.5, y=0.3)") == "click(x=0.5, y=0.3)"

    def test_with_think_block(self):
        resp = "<think>I see a button</think>\nclick(x=0.5, y=0.3)"
        assert _extract_action_string(resp) == "click(x=0.5, y=0.3)"

    def test_with_code_block(self):
        resp = "<think>reasoning</think>\n<code>type(text='hello')</code>"
        assert _extract_action_string(resp) == "type(text='hello')"

    def test_empty_response(self):
        assert _extract_action_string("") is None

    def test_think_only(self):
        assert _extract_action_string("<think>just thinking</think>") is None


# ---------------------------------------------------------------------------
# Click actions
# ---------------------------------------------------------------------------


class TestClickParsing:
    """Tests for click action parsing."""

    def test_click_basic(self):
        action = parse_smol_action("click(x=0.5, y=0.3)")
        assert action.type == "click"
        assert action.x == pytest.approx(0.5)
        assert action.y == pytest.approx(0.3)

    def test_click_high_precision(self):
        action = parse_smol_action("click(x=0.8875, y=0.2281)")
        assert action.type == "click"
        assert action.x == pytest.approx(0.8875)
        assert action.y == pytest.approx(0.2281)

    def test_double_click(self):
        action = parse_smol_action("double_click(x=0.81, y=0.95)")
        assert action.type == "click"
        assert action.x == pytest.approx(0.81)
        assert action.y == pytest.approx(0.95)
        assert action.raw_action["click_variant"] == "double_click"

    def test_long_press(self):
        action = parse_smol_action("long_press(x=0.8, y=0.9)")
        assert action.type == "click"
        assert action.raw_action["click_variant"] == "long_press"

    def test_click_not_confused_with_double(self):
        """Ensure 'click' regex doesn't match inside 'double_click'."""
        action = parse_smol_action("double_click(x=0.5, y=0.5)")
        assert action.raw_action.get("click_variant") == "double_click"


# ---------------------------------------------------------------------------
# Type / Press actions
# ---------------------------------------------------------------------------


class TestTypeParsing:
    """Tests for type and press action parsing."""

    def test_type_single_quotes(self):
        action = parse_smol_action("type(text='hello world')")
        assert action.type == "type"
        assert action.text == "hello world"

    def test_type_double_quotes(self):
        action = parse_smol_action('type(text="bread buns")')
        assert action.type == "type"
        assert action.text == "bread buns"

    def test_press_single_key(self):
        action = parse_smol_action("press(keys=['enter'])")
        assert action.type == "key"
        assert action.key == "enter"
        assert action.modifiers is None

    def test_press_modifier_combo(self):
        action = parse_smol_action("press(keys=['ctrl', 'c'])")
        assert action.type == "key"
        assert action.key == "c"
        assert action.modifiers == ["ctrl"]

    def test_press_three_keys(self):
        action = parse_smol_action("press(keys=['ctrl', 'shift', 'n'])")
        assert action.type == "key"
        assert action.key == "n"
        assert action.modifiers == ["ctrl", "shift"]


# ---------------------------------------------------------------------------
# Scroll / Drag / Swipe
# ---------------------------------------------------------------------------


class TestScrollDragSwipe:
    """Tests for scroll, drag, and swipe parsing."""

    def test_scroll_up(self):
        action = parse_smol_action("scroll(direction='up', amount=10)")
        assert action.type == "scroll"
        assert action.scroll_direction == "up"
        assert action.scroll_amount == 10.0

    def test_scroll_default_amount(self):
        action = parse_smol_action("scroll(direction='down')")
        assert action.type == "scroll"
        assert action.scroll_direction == "down"
        assert action.scroll_amount == 3.0

    def test_drag(self):
        action = parse_smol_action(
            "drag(from_coord=[0.1, 0.2], to_coord=[0.8, 0.5])"
        )
        assert action.type == "drag"
        assert action.x == pytest.approx(0.1)
        assert action.y == pytest.approx(0.2)
        assert action.end_x == pytest.approx(0.8)
        assert action.end_y == pytest.approx(0.5)

    def test_swipe_maps_to_drag(self):
        action = parse_smol_action(
            "swipe(from_coord=[0.581, 0.898], to_coord=[0.601, 0.518])"
        )
        assert action.type == "drag"
        assert action.x == pytest.approx(0.581)
        assert action.raw_action.get("action_variant") == "swipe"


# ---------------------------------------------------------------------------
# Special actions
# ---------------------------------------------------------------------------


class TestSpecialActions:
    """Tests for final_answer, navigate_home, open_app."""

    def test_final_answer(self):
        action = parse_smol_action("final_answer('success')")
        assert action.type == "done"
        assert action.answer == "success"

    def test_navigate_home(self):
        action = parse_smol_action("navigate_home()")
        assert action.type == "key"
        assert action.key == "super"

    def test_open_app(self):
        action = parse_smol_action("open_app(app_name='notepad')")
        assert action.type == "type"
        assert action.text == "notepad"

    def test_unknown_action(self):
        action = parse_smol_action("some_unknown_action()")
        assert action.type == "error"
        assert "parse_error" in action.raw_action

    def test_empty_response(self):
        action = parse_smol_action("")
        assert action.type == "error"
        assert "parse_error" in action.raw_action


# ---------------------------------------------------------------------------
# Think/Code block handling
# ---------------------------------------------------------------------------


class TestThinkCodeBlocks:
    """Tests for <think>/<code> block parsing."""

    def test_think_then_action(self):
        resp = "<think>I need to click the submit button</think>\nclick(x=0.5, y=0.3)"
        action = parse_smol_action(resp)
        assert action.type == "click"
        assert action.raw_action.get("thinking") == "I need to click the submit button"

    def test_code_block(self):
        resp = "<think>reasoning</think>\n<code>type(text='hello')</code>"
        action = parse_smol_action(resp)
        assert action.type == "type"
        assert action.text == "hello"


# ---------------------------------------------------------------------------
# Coordinates are [0, 1] — no conversion needed
# ---------------------------------------------------------------------------


class TestCoordinateRange:
    """Verify coordinates pass through as-is (already [0, 1])."""

    def test_coordinates_preserved(self):
        action = parse_smol_action("click(x=0.8875, y=0.2281)")
        assert action.x == pytest.approx(0.8875)
        assert action.y == pytest.approx(0.2281)

    def test_viewport_stored_in_raw(self):
        action = parse_smol_action(
            "click(x=0.5, y=0.5)", viewport=(1920, 1080)
        )
        assert action.raw_action["viewport"] == (1920, 1080)


# ---------------------------------------------------------------------------
# Agent behavior
# ---------------------------------------------------------------------------


class TestSmolOperatorAgent:
    """Tests for SmolOperatorAgent."""

    def test_init_defaults(self):
        agent = SmolOperatorAgent()
        assert agent.model_path == "smolagents/SmolVLM2-2.2B-Instruct-Agentic-GUI"
        assert agent.demo is None
        assert agent._step_count == 0

    def test_init_with_demo(self):
        agent = SmolOperatorAgent(demo="Step 0: click(x=0.5, y=0.5)")
        assert agent.demo is not None

    def test_reset(self):
        agent = SmolOperatorAgent()
        agent._step_count = 5
        agent._previous_actions = ["click(x=0.1, y=0.2)", "type(text='hello')"]
        agent.reset()
        assert agent._step_count == 0
        assert agent._previous_actions == []

    def test_set_demo(self):
        agent = SmolOperatorAgent()
        assert agent.demo is None
        agent.set_demo("Step 0: click(x=0.5, y=0.5)")
        assert agent.demo is not None

    def test_build_prompt_basic(self):
        agent = SmolOperatorAgent()
        prompt = agent._build_prompt("Open Notepad")
        assert "Instruction: Open Notepad" in prompt
        assert "Output exactly one action." in prompt

    def test_build_prompt_with_demo(self):
        agent = SmolOperatorAgent(demo="Step 0: click(x=0.1, y=0.9)")
        prompt = agent._build_prompt("Open Notepad")
        assert "Demonstration" in prompt
        assert "Step 0: click(x=0.1, y=0.9)" in prompt

    def test_build_prompt_with_history(self):
        agent = SmolOperatorAgent()
        agent._previous_actions = ["click(x=0.1, y=0.2)", "type(text='hi')"]
        prompt = agent._build_prompt("Open Notepad")
        assert "Previous actions:" in prompt
        assert "Step 0: click(x=0.1, y=0.2)" in prompt
        assert "Step 1: type(text='hi')" in prompt


# ---------------------------------------------------------------------------
# Mock adapter integration
# ---------------------------------------------------------------------------


class TestMockAdapterIntegration:
    """Test SmolOperatorAgent actions work with mock adapter."""

    def test_click_action_accepted_by_mock(self):
        """Verify parsed click produces valid BenchmarkAction for mock adapter."""
        action = parse_smol_action("click(x=0.5, y=0.5)")
        assert action.type == "click"
        assert 0 <= action.x <= 1
        assert 0 <= action.y <= 1

        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        adapter.reset(task)
        obs, done, info = adapter.step(action)
        assert not done

    def test_done_action_ends_episode(self):
        action = parse_smol_action("final_answer('done')")
        assert action.type == "done"

        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        adapter.reset(task)
        obs, done, info = adapter.step(action)
        assert done
