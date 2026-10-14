"""Tests for Qwen3VLAgent.

Tests cover:
1. Action parsing from Qwen3-VL output to BenchmarkAction (with denormalization)
2. Coordinate denormalization ([0, 1000] -> [0, 1])
3. Think block extraction and handling
4. Prompt building (training-format alignment)
5. Demo injection in prompts
6. Reset behavior
7. Agent initialization and imports
8. Edge cases in parsing
"""


from openadapt_evals.adapters.base import (
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.qwen3vl_agent import (
    QWEN_COORD_SCALE,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_A11Y,
    Qwen3VLAgent,
    _denorm_coord,
    _extract_action_string,
    _format_a11y_tree,
    parse_qwen_action,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_task(instruction="Open Notepad"):
    return BenchmarkTask(task_id="test_001", instruction=instruction, domain="desktop")


def make_observation(viewport=(1280, 720)):
    return BenchmarkObservation(screenshot=None, viewport=viewport)


# ---------------------------------------------------------------------------
# Action Parsing — parse_qwen_action()
# ---------------------------------------------------------------------------


class TestParseQwenAction:
    """Test parsing Qwen3-VL action strings into BenchmarkAction.

    parse_qwen_action returns coordinates already denormalized to [0, 1].
    """

    def test_click_basic(self):
        action = parse_qwen_action("click(x=500, y=300)")
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6
        assert abs(action.y - 0.3) < 1e-6

    def test_click_origin(self):
        action = parse_qwen_action("click(x=0, y=0)")
        assert action.type == "click"
        assert action.x == 0.0
        assert action.y == 0.0

    def test_click_max(self):
        action = parse_qwen_action("click(x=1000, y=1000)")
        assert action.type == "click"
        assert abs(action.x - 1.0) < 1e-6
        assert abs(action.y - 1.0) < 1e-6

    def test_click_with_spaces(self):
        action = parse_qwen_action("click( x = 500 , y = 300 )")
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6

    def test_click_stores_qwen_coords(self):
        action = parse_qwen_action("click(x=500, y=300)")
        assert action.raw_action["qwen_coords"] == {"x": 500, "y": 300}

    def test_double_click(self):
        action = parse_qwen_action("double_click(x=500, y=300)")
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6
        assert abs(action.y - 0.3) < 1e-6
        assert action.raw_action["click_variant"] == "double_click"
        assert action.raw_action["qwen_coords"] == {"x": 500, "y": 300}

    def test_right_click(self):
        action = parse_qwen_action("right_click(x=100, y=200)")
        assert action.type == "click"
        assert abs(action.x - 0.1) < 1e-6
        assert abs(action.y - 0.2) < 1e-6
        assert action.raw_action["click_variant"] == "right_click"

    def test_type_action(self):
        action = parse_qwen_action('type(text="hello world")')
        assert action.type == "type"
        assert action.text == "hello world"

    def test_type_with_escaped_quotes(self):
        action = parse_qwen_action(r'type(text="say \"hi\"")')
        assert action.type == "type"
        assert action.text == 'say "hi"'

    def test_press_single_key(self):
        action = parse_qwen_action('press(keys=["Return"])')
        assert action.type == "key"
        assert action.key == "Return"
        assert action.modifiers is None

    def test_press_with_modifiers(self):
        action = parse_qwen_action('press(keys=["ctrl", "s"])')
        assert action.type == "key"
        assert action.key == "s"
        assert action.modifiers == ["ctrl"]

    def test_press_multiple_modifiers(self):
        action = parse_qwen_action('press(keys=["ctrl", "shift", "s"])')
        assert action.type == "key"
        assert action.key == "s"
        assert action.modifiers == ["ctrl", "shift"]

    def test_scroll_with_amount(self):
        action = parse_qwen_action('scroll(direction="down", amount=5)')
        assert action.type == "scroll"
        assert action.scroll_direction == "down"
        assert action.scroll_amount == 5.0

    def test_scroll_without_amount_defaults_to_3(self):
        action = parse_qwen_action('scroll(direction="up")')
        assert action.type == "scroll"
        assert action.scroll_direction == "up"
        assert action.scroll_amount == 3.0

    def test_scroll_direction_left(self):
        action = parse_qwen_action('scroll(direction="left", amount=2)')
        assert action.type == "scroll"
        assert action.scroll_direction == "left"

    def test_drag(self):
        action = parse_qwen_action(
            "drag(from_coord=[200, 300], to_coord=[800, 500])"
        )
        assert action.type == "drag"
        assert abs(action.x - 0.2) < 1e-6
        assert abs(action.y - 0.3) < 1e-6
        assert abs(action.end_x - 0.8) < 1e-6
        assert abs(action.end_y - 0.5) < 1e-6

    def test_drag_stores_qwen_coords(self):
        action = parse_qwen_action(
            "drag(from_coord=[200, 300], to_coord=[800, 500])"
        )
        assert action.raw_action["qwen_coords"]["from"] == {"x": 200, "y": 300}
        assert action.raw_action["qwen_coords"]["to"] == {"x": 800, "y": 500}

    def test_wait(self):
        action = parse_qwen_action("wait()")
        assert action.type == "wait"
        assert action.raw_action.get("is_wait") is True

    def test_finished(self):
        action = parse_qwen_action("finished()")
        assert action.type == "done"

    def test_no_action_found(self):
        action = parse_qwen_action("I don't know what to do")
        assert action.type == "error"
        assert "parse_error" in action.raw_action

    def test_empty_response(self):
        action = parse_qwen_action("")
        assert action.type == "error"
        assert "parse_error" in action.raw_action

    def test_viewport_stored_in_raw_action(self):
        action = parse_qwen_action("click(x=500, y=300)", viewport=(1920, 1080))
        assert action.raw_action["viewport"] == (1920, 1080)

    def test_action_embedded_in_text(self):
        """Parser finds action even embedded in natural language."""
        action = parse_qwen_action(
            "I need to click the start button so I will\nclick(x=500, y=950)"
        )
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6
        assert abs(action.y - 0.95) < 1e-6


# ---------------------------------------------------------------------------
# Think block handling
# ---------------------------------------------------------------------------


class TestThinkBlocks:
    """Test handling of <think>...</think> blocks."""

    def test_think_block_extracted(self):
        response = (
            "<think>\nI see the desktop. I should click the start button.\n</think>\n"
            "click(x=500, y=950)"
        )
        action = parse_qwen_action(response)
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6
        assert abs(action.y - 0.95) < 1e-6
        assert "thinking" in action.raw_action
        assert "start button" in action.raw_action["thinking"]

    def test_think_block_multiline(self):
        response = (
            "<think>\nLine 1\nLine 2\nLine 3\n</think>\n"
            'type(text="hello")'
        )
        action = parse_qwen_action(response)
        assert action.type == "type"
        assert action.text == "hello"
        assert "Line 2" in action.raw_action["thinking"]

    def test_no_think_block(self):
        action = parse_qwen_action("click(x=100, y=200)")
        assert "thinking" not in action.raw_action

    def test_think_block_only_no_action(self):
        """Reasoning without an action is a parse error."""
        action = parse_qwen_action("<think>I'm not sure what to do</think>")
        assert action.type == "error"

    def test_think_with_finished(self):
        response = (
            "<think>The task is complete.</think>\n"
            "finished()"
        )
        action = parse_qwen_action(response)
        assert action.type == "done"
        assert "thinking" in action.raw_action


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


class TestDenormCoord:
    """Test _denorm_coord helper."""

    def test_origin(self):
        x, y = _denorm_coord(0, 0)
        assert x == 0.0
        assert y == 0.0

    def test_center(self):
        x, y = _denorm_coord(500, 500)
        assert abs(x - 0.5) < 1e-6
        assert abs(y - 0.5) < 1e-6

    def test_max(self):
        x, y = _denorm_coord(1000, 1000)
        assert abs(x - 1.0) < 1e-6
        assert abs(y - 1.0) < 1e-6

    def test_quarter(self):
        x, y = _denorm_coord(250, 750)
        assert abs(x - 0.25) < 1e-6
        assert abs(y - 0.75) < 1e-6


class TestExtractActionString:
    """Test _extract_action_string helper."""

    def test_simple_action(self):
        result = _extract_action_string("click(x=500, y=300)")
        assert result == "click(x=500, y=300)"

    def test_with_think_block(self):
        result = _extract_action_string(
            "<think>thinking...</think>\nclick(x=500, y=300)"
        )
        assert result == "click(x=500, y=300)"

    def test_multiple_lines_returns_first_action(self):
        result = _extract_action_string(
            "Some preamble\nclick(x=500, y=300)\nfinished()"
        )
        assert result == "click(x=500, y=300)"

    def test_empty_response(self):
        result = _extract_action_string("")
        assert result is None

    def test_only_think_block(self):
        result = _extract_action_string("<think>just thinking</think>")
        assert result is None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Test prompt construction matching convert_demos format."""

    def test_basic_prompt_has_image_tag(self):
        agent = Qwen3VLAgent()
        prompt = agent._build_prompt("Open Notepad")
        assert prompt.startswith("<image>")

    def test_basic_prompt_has_instruction(self):
        agent = Qwen3VLAgent()
        prompt = agent._build_prompt("Open Notepad")
        assert "Instruction: Open Notepad" in prompt

    def test_prompt_no_demo_no_history(self):
        agent = Qwen3VLAgent()
        prompt = agent._build_prompt("Open Notepad")
        assert "Demonstration" not in prompt
        assert "Previous actions:" not in prompt

    def test_prompt_includes_demo(self):
        demo = "Step 0: click(x=450, y=950)\nStep 1: click(x=300, y=200)"
        agent = Qwen3VLAgent(demo=demo)
        prompt = agent._build_prompt("Open Notepad")
        assert "Demonstration" in prompt
        assert "click(x=450, y=950)" in prompt
        assert "Open Notepad" in prompt

    def test_prompt_includes_action_history(self):
        agent = Qwen3VLAgent()
        agent._previous_actions = ["click(x=500, y=950)", 'type(text="notepad")']
        prompt = agent._build_prompt("Open Notepad")
        assert "Previous actions:" in prompt
        assert "Step 0: click(x=500, y=950)" in prompt
        assert 'Step 1: type(text="notepad")' in prompt

    def test_prompt_with_thinking_mode(self):
        agent = Qwen3VLAgent(use_thinking=True)
        prompt = agent._build_prompt("Open Notepad")
        assert "<think>" in prompt
        assert "then output exactly one action" in prompt

    def test_prompt_without_thinking_mode(self):
        agent = Qwen3VLAgent(use_thinking=False)
        prompt = agent._build_prompt("Open Notepad")
        assert "<think>" not in prompt
        assert "Output exactly one action." in prompt

    def test_prompt_with_demo_and_history(self):
        """Demo and history should both appear in the prompt."""
        agent = Qwen3VLAgent(demo="Step 0: click(x=100, y=200)")
        agent._previous_actions = ["click(x=500, y=300)"]
        prompt = agent._build_prompt("Do something")
        assert "Demonstration" in prompt
        assert "Previous actions:" in prompt
        assert "Step 0: click(x=500, y=300)" in prompt


class TestBuildMessages:
    """Test chat message construction for the processor."""

    def test_messages_have_system_and_user(self):
        agent = Qwen3VLAgent()
        messages = agent._build_messages("test content")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_message_matches_training(self):
        agent = Qwen3VLAgent()
        messages = agent._build_messages("test")
        assert messages[0]["content"] == SYSTEM_PROMPT

    def test_user_message_has_image_and_text(self):
        agent = Qwen3VLAgent()
        messages = agent._build_messages("test content")
        user_content = messages[1]["content"]
        assert len(user_content) == 2
        assert user_content[0]["type"] == "image"
        assert user_content[1]["type"] == "text"
        assert user_content[1]["text"] == "test content"


# ---------------------------------------------------------------------------
# System prompt alignment
# ---------------------------------------------------------------------------


class TestSystemPromptAlignment:
    """Verify the system prompt matches the training data format."""

    def test_system_prompt_contains_all_actions(self):
        """All actions from the action space must be in the system prompt."""
        for action_name in [
            "click(x=", "double_click(x=", "right_click(x=",
            'type(text="', 'press(keys=[', 'scroll(direction="',
            "drag(from_coord=", "wait()", "finished()",
        ]:
            assert action_name in SYSTEM_PROMPT, (
                f"Missing action '{action_name}' in SYSTEM_PROMPT"
            )

    def test_system_prompt_coordinate_range(self):
        assert "[0, 1000]" in SYSTEM_PROMPT
        assert "(1000,1000)" in SYSTEM_PROMPT

    def test_qwen_coord_scale_matches_prompt(self):
        assert QWEN_COORD_SCALE == 1000


# ---------------------------------------------------------------------------
# Agent reset
# ---------------------------------------------------------------------------


class TestReset:
    """Test agent reset behavior."""

    def test_reset_clears_state(self):
        agent = Qwen3VLAgent()
        agent._step_count = 5
        agent._previous_actions = ["click(x=100, y=200)", "wait()"]

        agent.reset()

        assert agent._step_count == 0
        assert agent._previous_actions == []

    def test_reset_preserves_config(self):
        demo = "Step 0: click(x=500, y=500)"
        agent = Qwen3VLAgent(demo=demo, use_thinking=True)
        agent._step_count = 3
        agent._previous_actions = ["click(x=100, y=200)"]

        agent.reset()

        assert agent.demo == demo
        assert agent.use_thinking is True
        assert agent._step_count == 0
        assert agent._previous_actions == []

    def test_set_demo(self):
        agent = Qwen3VLAgent()
        assert agent.demo is None

        agent.set_demo("Step 0: click(x=100, y=200)")
        assert agent.demo is not None
        assert "click(x=100, y=200)" in agent.demo


# ---------------------------------------------------------------------------
# Agent initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Test agent initialization and defaults."""

    def test_default_model_path(self):
        agent = Qwen3VLAgent()
        assert agent.model_path == "Qwen/Qwen3-VL-8B-Instruct"

    def test_custom_model_path(self):
        agent = Qwen3VLAgent(model_path="Qwen/Qwen3-VL-2B-Instruct")
        assert agent.model_path == "Qwen/Qwen3-VL-2B-Instruct"

    def test_lazy_loading(self):
        """Model should NOT be loaded on initialization."""
        agent = Qwen3VLAgent()
        assert agent._model is None
        assert agent._processor is None

    def test_default_device(self):
        agent = Qwen3VLAgent()
        assert agent.device == "auto"

    def test_default_thinking_disabled(self):
        agent = Qwen3VLAgent()
        assert agent.use_thinking is False

    def test_default_max_new_tokens(self):
        agent = Qwen3VLAgent()
        assert agent.max_new_tokens == 512


# ---------------------------------------------------------------------------
# Import / registration
# ---------------------------------------------------------------------------


class TestImport:
    """Test that the agent can be imported from the package."""

    def test_import_from_agents_package(self):
        from openadapt_evals.agents import Qwen3VLAgent as Imported
        assert Imported is not None
        assert Imported is Qwen3VLAgent

    def test_in_all(self):
        from openadapt_evals import agents
        assert "Qwen3VLAgent" in agents.__all__

    def test_parse_qwen_action_importable(self):
        from openadapt_evals.agents.qwen3vl_agent import parse_qwen_action as fn
        assert callable(fn)

    def test_system_prompt_importable(self):
        from openadapt_evals.agents.qwen3vl_agent import SYSTEM_PROMPT as sp
        assert isinstance(sp, str)
        assert len(sp) > 100


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and robustness tests for action parsing."""

    def test_case_insensitive_click(self):
        """The regex should match case-insensitively (though model should output lowercase)."""
        action = parse_qwen_action("Click(x=500, y=300)")
        assert action.type == "click"

    def test_left_click_alias(self):
        """left_click should be treated as click."""
        action = parse_qwen_action("left_click(x=500, y=300)")
        assert action.type == "click"
        assert abs(action.x - 0.5) < 1e-6

    def test_press_empty_keys(self):
        """An empty key list is a parse error."""
        action = parse_qwen_action("press(keys=[])")
        assert action.type == "error"
        assert "parse_error" in action.raw_action

    def test_type_empty_string(self):
        """type(text='') should produce a type action with empty text."""
        action = parse_qwen_action('type(text="")')
        assert action.type == "type"
        assert action.text == ""

    def test_response_preserves_raw(self):
        """raw_action should always contain the original response."""
        response = "click(x=100, y=200)"
        action = parse_qwen_action(response)
        assert action.raw_action["response"] == response

    def test_whitespace_only_response(self):
        action = parse_qwen_action("   \n  \t  ")
        assert action.type == "error"

    def test_multiline_think_then_action(self):
        """Realistic model output with thinking then action."""
        response = (
            "<think>\n"
            "I can see the Windows desktop with the taskbar at the bottom.\n"
            "The Start button is in the lower-left corner.\n"
            "I need to click it to open the Start menu.\n"
            "</think>\n"
            "click(x=24, y=980)"
        )
        action = parse_qwen_action(response)
        assert action.type == "click"
        assert abs(action.x - 0.024) < 1e-6
        assert abs(action.y - 0.98) < 1e-6
        assert "Start button" in action.raw_action["thinking"]


# ---------------------------------------------------------------------------
# PEFT adapter loading tests
# ---------------------------------------------------------------------------


class TestPEFTAdapterLoading:
    """Test that _load_model detects and loads PEFT adapters."""

    def test_detects_peft_adapter_directory(self, tmp_path):
        """When model_path contains adapter_config.json, treat as PEFT."""
        import json

        adapter_config = {
            "base_model_name_or_path": "Qwen/Qwen3-VL-2B-Instruct",
            "peft_type": "LORA",
            "r": 16,
            "target_modules": ["q_proj", "v_proj"],
        }
        (tmp_path / "adapter_config.json").write_text(json.dumps(adapter_config))

        agent = Qwen3VLAgent(model_path=str(tmp_path))

        # Verify the adapter path exists check
        from pathlib import Path

        adapter_config_path = Path(agent.model_path) / "adapter_config.json"
        assert adapter_config_path.exists()

    def test_non_adapter_path_not_detected(self, tmp_path):
        """A regular model path (no adapter_config.json) is not PEFT."""
        agent = Qwen3VLAgent(model_path=str(tmp_path))

        from pathlib import Path

        adapter_config_path = Path(agent.model_path) / "adapter_config.json"
        assert not adapter_config_path.exists()

    def test_adapter_config_base_model_extraction(self, tmp_path):
        """Verify base_model_name_or_path is read from adapter config."""
        import json

        adapter_config = {
            "base_model_name_or_path": "Qwen/Qwen3-VL-2B-Instruct",
            "peft_type": "LORA",
        }
        (tmp_path / "adapter_config.json").write_text(json.dumps(adapter_config))

        with open(tmp_path / "adapter_config.json") as f:
            cfg = json.load(f)

        assert cfg["base_model_name_or_path"] == "Qwen/Qwen3-VL-2B-Instruct"

    def test_adapter_config_defaults_to_default_model(self, tmp_path):
        """If base_model_name_or_path is missing, DEFAULT_MODEL is used."""
        import json

        from openadapt_evals.agents.qwen3vl_agent import DEFAULT_MODEL

        adapter_config = {"peft_type": "LORA"}
        (tmp_path / "adapter_config.json").write_text(json.dumps(adapter_config))

        with open(tmp_path / "adapter_config.json") as f:
            cfg = json.load(f)

        base = cfg.get("base_model_name_or_path", DEFAULT_MODEL)
        assert base == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Remote inference tests
# ---------------------------------------------------------------------------


class TestRemoteInference:
    """Test remote inference via model_endpoint."""

    def test_modal_endpoint_skips_local_model_loading(self):
        """When model_endpoint='modal', _load_model is not called in act."""
        agent = Qwen3VLAgent(model_endpoint="modal")
        assert agent.model_endpoint == "modal"
        # Model should not be loaded
        assert agent._model is None

    def test_http_endpoint_stored(self):
        """HTTP endpoint is stored correctly."""
        agent = Qwen3VLAgent(model_endpoint="http://localhost:8080")
        assert agent.model_endpoint == "http://localhost:8080"

    def test_no_endpoint_defaults_to_none(self):
        """Default model_endpoint is None (local inference)."""
        agent = Qwen3VLAgent()
        assert agent.model_endpoint is None

    def test_endpoint_in_repr(self):
        """model_endpoint is included in agent info."""
        agent = Qwen3VLAgent(model_endpoint="modal")
        assert agent.model_endpoint == "modal"


# ---------------------------------------------------------------------------
# Accessibility tree grounding
# ---------------------------------------------------------------------------


MOCK_A11Y_TREE = {
    "role": "window",
    "name": "Mock Window",
    "children": [
        {"role": "button", "name": "OK", "id": "1"},
        {"role": "textfield", "name": "Input", "id": "2"},
        {"role": "button", "name": "Cancel", "id": "3"},
        {"role": "button", "name": "Submit", "id": "4"},
    ],
}


class TestClickElementParsing:
    """Test parsing click_element and type_element actions."""

    def test_click_element_basic(self):
        action = parse_qwen_action('click_element(id="4")')
        assert action.type == "click"
        assert action.target_node_id == "4"
        assert action.x is None
        assert action.y is None

    def test_click_element_string_id(self):
        action = parse_qwen_action('click_element(id="submit_btn")')
        assert action.type == "click"
        assert action.target_node_id == "submit_btn"

    def test_click_element_positional(self):
        """Positional arg without id= keyword."""
        action = parse_qwen_action('click_element("4")')
        assert action.type == "click"
        assert action.target_node_id == "4"

    def test_click_element_stores_raw(self):
        action = parse_qwen_action('click_element(id="4")')
        assert action.raw_action["element_action"] == "click_element"
        assert action.raw_action["target_element_id"] == "4"

    def test_click_element_case_insensitive(self):
        action = parse_qwen_action('Click_Element(id="4")')
        assert action.type == "click"
        assert action.target_node_id == "4"

    def test_type_element_basic(self):
        action = parse_qwen_action('type_element(id="2", text="hello")')
        assert action.type == "type"
        assert action.target_node_id == "2"
        assert action.text == "hello"

    def test_type_element_positional(self):
        action = parse_qwen_action('type_element("2", "hello world")')
        assert action.type == "type"
        assert action.target_node_id == "2"
        assert action.text == "hello world"

    def test_type_element_with_escaped_quotes(self):
        action = parse_qwen_action(r'type_element(id="2", text="say \"hi\"")')
        assert action.type == "type"
        assert action.text == 'say "hi"'

    def test_click_element_takes_priority_over_click(self):
        """click_element should be parsed before click (with coordinates)."""
        # This tests that element-based actions are checked first
        action = parse_qwen_action('click_element(id="4")')
        assert action.target_node_id == "4"
        assert action.x is None

    def test_click_element_in_think_response(self):
        response = (
            "<think>I should click the Submit button (ID 4).</think>\n"
            'click_element(id="4")'
        )
        action = parse_qwen_action(response)
        assert action.type == "click"
        assert action.target_node_id == "4"
        assert "thinking" in action.raw_action


class TestFormatA11yTree:
    """Test _format_a11y_tree helper."""

    def test_basic_tree(self):
        formatted = _format_a11y_tree(MOCK_A11Y_TREE)
        assert '[1] button "OK"' in formatted
        assert '[2] textfield "Input"' in formatted
        assert '[3] button "Cancel"' in formatted
        assert '[4] button "Submit"' in formatted

    def test_root_node_shown(self):
        formatted = _format_a11y_tree(MOCK_A11Y_TREE)
        assert "window" in formatted

    def test_indentation(self):
        formatted = _format_a11y_tree(MOCK_A11Y_TREE)
        lines = formatted.strip().split("\n")
        # Root (window) at indent 0, children at indent 1 (2 spaces)
        assert lines[0].startswith("window")
        assert lines[1].startswith("  [1]")

    def test_empty_tree(self):
        formatted = _format_a11y_tree({})
        assert formatted == ""

    def test_nested_tree(self):
        tree = {
            "role": "window",
            "name": "App",
            "children": [
                {
                    "role": "panel",
                    "name": "Toolbar",
                    "children": [
                        {"role": "button", "name": "Save", "id": "10"},
                    ],
                }
            ],
        }
        formatted = _format_a11y_tree(tree)
        assert '[10] button "Save"' in formatted


class TestA11yTreePromptIntegration:
    """Test that a11y tree is included in prompts when enabled."""

    def test_a11y_tree_in_prompt_when_enabled(self):
        agent = Qwen3VLAgent(use_accessibility_tree=True)
        obs = BenchmarkObservation(
            screenshot=None,
            viewport=(1920, 1200),
            accessibility_tree=MOCK_A11Y_TREE,
        )
        prompt = agent._build_prompt("Click the Submit button", obs)
        assert "Available UI elements" in prompt
        assert '[4] button "Submit"' in prompt

    def test_a11y_tree_not_in_prompt_when_disabled(self):
        agent = Qwen3VLAgent(use_accessibility_tree=False)
        obs = BenchmarkObservation(
            screenshot=None,
            viewport=(1920, 1200),
            accessibility_tree=MOCK_A11Y_TREE,
        )
        prompt = agent._build_prompt("Click the Submit button", obs)
        assert "Available UI elements" not in prompt

    def test_a11y_tree_not_in_prompt_when_none(self):
        agent = Qwen3VLAgent(use_accessibility_tree=True)
        obs = BenchmarkObservation(screenshot=None, viewport=(1920, 1200))
        prompt = agent._build_prompt("Click the Submit button", obs)
        assert "Available UI elements" not in prompt

    def test_a11y_tree_prompt_backward_compatible(self):
        """Prompt without observation arg still works (backward compat)."""
        agent = Qwen3VLAgent()
        prompt = agent._build_prompt("Open Notepad")
        assert "Instruction: Open Notepad" in prompt

    def test_a11y_system_prompt_used_when_enabled(self):
        agent = Qwen3VLAgent(use_accessibility_tree=True)
        messages = agent._build_messages("test")
        assert messages[0]["content"] == SYSTEM_PROMPT_A11Y
        assert "click_element" in messages[0]["content"]

    def test_standard_system_prompt_used_when_disabled(self):
        agent = Qwen3VLAgent(use_accessibility_tree=False)
        messages = agent._build_messages("test")
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert "click_element" not in messages[0]["content"]

    def test_default_a11y_tree_disabled(self):
        agent = Qwen3VLAgent()
        assert agent.use_accessibility_tree is False


# ---------------------------------------------------------------------------
# Integration: Mock adapter end-to-end with a11y tree grounding
# ---------------------------------------------------------------------------


class TestA11yTreeMockAdapterIntegration:
    """End-to-end test: a11y tree grounding through mock adapter evaluation.

    Validates the full flow:
    1. Mock adapter provides observation with a11y tree
    2. Agent prompt includes formatted a11y tree
    3. Agent response uses click_element(id="4") (Submit button)
    4. Parser sets target_node_id on BenchmarkAction
    5. Mock adapter scores based on target_node_id → success
    """

    def test_click_element_scores_success_on_mock_adapter(self):
        """Clicking Submit via element ID should score 1.0 on mock adapter."""
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        tasks = adapter.list_tasks()
        task = tasks[0]

        # Reset adapter
        obs = adapter.reset(task)

        # Verify observation has a11y tree
        assert obs.accessibility_tree is not None
        assert any(
            child.get("id") == "4" and child.get("name") == "Submit"
            for child in obs.accessibility_tree.get("children", [])
        )

        # Simulate agent producing click_element action
        action = parse_qwen_action('click_element(id="4")')
        assert action.type == "click"
        assert action.target_node_id == "4"

        # Step the adapter with this action
        obs2, done, info = adapter.step(action)

        # Signal done
        done_action = parse_qwen_action("finished()")
        obs3, done2, info2 = adapter.step(done_action)
        assert done2 is True

        # Evaluate
        result = adapter.evaluate(task)
        assert result.success is True
        assert result.score == 1.0

    def test_type_then_click_ok_scores_success(self):
        """Type in field + click OK via element IDs → success."""
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        # Type text in field
        type_action = parse_qwen_action('type_element(id="2", text="hello")')
        assert type_action.type == "type"
        assert type_action.target_node_id == "2"
        adapter.step(type_action)

        # Click OK
        ok_action = parse_qwen_action('click_element(id="1")')
        assert ok_action.target_node_id == "1"
        adapter.step(ok_action)

        # Finish
        adapter.step(parse_qwen_action("finished()"))

        result = adapter.evaluate(task)
        assert result.success is True
        assert result.score == 1.0

    def test_coordinate_click_still_works(self):
        """Coordinate-based clicks (fallback) still work for scoring."""
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        adapter.reset(task)

        # Click Submit button area with coordinates
        # Submit button is at (350, 80)-(450, 160) in mock adapter
        # Normalized: (350/1920, 100/1200) ≈ (0.182, 0.083)
        action = parse_qwen_action("click(x=208, y=100)")  # Qwen coords -> ~0.208, 0.1 -> pixels ~399, 120
        adapter.step(action)
        adapter.step(parse_qwen_action("finished()"))

        result = adapter.evaluate(task)
        # Coordinate click should also work
        assert result.score > 0

    def test_full_prompt_contains_a11y_elements(self):
        """Verify the prompt built from a mock observation has the a11y tree."""
        from openadapt_evals.adapters.waa.mock import WAAMockAdapter

        adapter = WAAMockAdapter(num_tasks=1, domains=["notepad"])
        task = adapter.list_tasks()[0]
        obs = adapter.reset(task)

        agent = Qwen3VLAgent(use_accessibility_tree=True)
        prompt = agent._build_prompt(task.instruction, obs)

        # Verify all 4 mock elements are in the prompt
        assert '[1] button "OK"' in prompt
        assert '[2] textfield "Input"' in prompt
        assert '[3] button "Cancel"' in prompt
        assert '[4] button "Submit"' in prompt
        assert "click_element/type_element" in prompt
