"""Tests for ClaudeComputerUseAgent.

Tests cover:
1. Action mapping from Claude computer_use tool_use blocks to BenchmarkAction
2. Conversation management across steps
3. Demo injection in initial messages
4. Screenshot encoding
5. Coordinate normalization
6. Reset behavior
"""

import base64
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)


def create_test_screenshot(width=1280, height=720):
    """Create a minimal test PNG screenshot."""
    img = Image.new("RGB", (width, height), color="blue")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def create_tool_use_block(action_type, **kwargs):
    """Create a mock tool_use content block."""
    block = Mock()
    block.type = "tool_use"
    block.name = "computer"
    block.id = f"toolu_{action_type}_001"
    block.input = {"action": action_type, **kwargs}
    return block


def create_text_block(text):
    """Create a mock text content block."""
    block = Mock()
    block.type = "text"
    block.text = text
    return block


def create_mock_response(*blocks):
    """Create a mock API response with given content blocks."""
    response = Mock()
    response.content = list(blocks)
    return response


def make_task(instruction="Open Notepad"):
    return BenchmarkTask(task_id="test_001", instruction=instruction, domain="desktop")


def make_observation(screenshot_bytes=None, viewport=(1280, 720)):
    if screenshot_bytes is None:
        screenshot_bytes = create_test_screenshot(*viewport)
    return BenchmarkObservation(screenshot=screenshot_bytes, viewport=viewport)


@pytest.fixture
def mock_anthropic_client():
    """Create a mock Anthropic client."""
    client = Mock()
    client.beta = Mock()
    client.beta.messages = Mock()
    return client


@pytest.fixture
def agent(mock_anthropic_client):
    """Create a ClaudeComputerUseAgent with mocked client."""
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-123"}):
        with patch("anthropic.Anthropic", return_value=mock_anthropic_client):
            from openadapt_evals.agents.claude_computer_use_agent import (
                ClaudeComputerUseAgent,
            )

            return ClaudeComputerUseAgent()


@pytest.fixture
def agent_with_demo(mock_anthropic_client):
    """Create a ClaudeComputerUseAgent with a demo."""
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-123"}):
        with patch("anthropic.Anthropic", return_value=mock_anthropic_client):
            from openadapt_evals.agents.claude_computer_use_agent import (
                ClaudeComputerUseAgent,
            )

            return ClaudeComputerUseAgent(
                demo="Step 1: Click Start menu\nStep 2: Type notepad"
            )


class TestActionMapping:
    """Test mapping from Claude tool_use inputs to BenchmarkAction."""

    def test_left_click(self, agent, mock_anthropic_client):
        """Left click maps to BenchmarkAction(type='click') with normalized coords."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[640, 360])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "click"
        assert abs(action.x - 0.5) < 0.01  # 640/1280 = 0.5
        assert abs(action.y - 0.5) < 0.01  # 360/720 = 0.5

    def test_right_click(self, agent, mock_anthropic_client):
        """Right click maps to click with click_variant in raw_action."""
        response = create_mock_response(
            create_tool_use_block("right_click", coordinate=[100, 200])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "click"
        assert action.raw_action["click_variant"] == "right_click"

    def test_double_click(self, agent, mock_anthropic_client):
        """Double click maps to click with click_variant in raw_action."""
        response = create_mock_response(
            create_tool_use_block("double_click", coordinate=[100, 200])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "click"
        assert action.raw_action["click_variant"] == "double_click"

    def test_type_action(self, agent, mock_anthropic_client):
        """Type action maps to BenchmarkAction(type='type')."""
        response = create_mock_response(
            create_tool_use_block("type", text="Hello world")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "type"
        assert action.text == "Hello world"

    def test_key_simple(self, agent, mock_anthropic_client):
        """Simple key press maps correctly."""
        response = create_mock_response(
            create_tool_use_block("key", text="Return")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "key"
        assert action.key == "Return"
        assert action.modifiers is None

    def test_key_with_modifier(self, agent, mock_anthropic_client):
        """Key with modifier splits correctly."""
        response = create_mock_response(
            create_tool_use_block("key", text="ctrl+s")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "key"
        assert action.key == "s"
        assert action.modifiers == ["ctrl"]

    def test_scroll_action(self, agent, mock_anthropic_client):
        """Scroll maps to BenchmarkAction(type='scroll')."""
        response = create_mock_response(
            create_tool_use_block(
                "scroll",
                coordinate=[640, 400],
                scroll_direction="down",
                scroll_amount=3,
            )
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "scroll"
        assert action.scroll_direction == "down"
        assert action.scroll_amount == 3.0

    def test_drag_action(self, agent, mock_anthropic_client):
        """Drag maps to BenchmarkAction(type='drag') with normalized coords."""
        response = create_mock_response(
            create_tool_use_block(
                "left_click_drag",
                start_coordinate=[128, 72],
                coordinate=[640, 360],
            )
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "drag"
        assert abs(action.x - 0.1) < 0.01  # 128/1280 = 0.1
        assert abs(action.y - 0.1) < 0.01  # 72/720 = 0.1
        assert abs(action.end_x - 0.5) < 0.01
        assert abs(action.end_y - 0.5) < 0.01

    def test_no_tool_use_returns_done(self, agent, mock_anthropic_client):
        """Response with no tool_use block maps to done."""
        response = create_mock_response(
            create_text_block("Task completed successfully.")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "done"
        assert "no_tool_use" in action.raw_action.get("reason", "")

    def test_screenshot_action_returns_error_on_exhaustion(self, agent, mock_anthropic_client):
        """Screenshot action returns error after exhausting retries."""
        response = create_mock_response(
            create_tool_use_block("screenshot")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "error"
        assert action.raw_action["reason"] == "max_internal_retries_exceeded"

    def test_wait_action_returns_error_on_exhaustion(self, agent, mock_anthropic_client):
        """Wait action returns error after exhausting retries."""
        response = create_mock_response(
            create_tool_use_block("wait", duration=1.0)
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "error"
        assert action.raw_action["reason"] == "max_internal_retries_exceeded"


class TestConversationManagement:
    """Test that conversation state is maintained across steps."""

    def test_first_step_sends_task_instruction(self, agent, mock_anthropic_client):
        """First step builds initial messages with task instruction."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent.act(make_observation(), make_task("Open Notepad"))

        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        # After act(): messages = [user(initial), assistant(response)]
        # The list is mutated after the call, so we see the post-call state
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        # Check task instruction is in the content
        content = messages[0]["content"]
        text_parts = [p["text"] for p in content if p.get("type") == "text"]
        assert any("Open Notepad" in t for t in text_parts)

    def test_second_step_sends_tool_result(self, agent, mock_anthropic_client):
        """Second step sends screenshot as tool_result."""
        # Step 1: click
        response1 = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response1
        agent.act(make_observation(), make_task())

        # Step 2: type
        response2 = create_mock_response(
            create_tool_use_block("type", text="hello")
        )
        mock_anthropic_client.beta.messages.create.return_value = response2
        agent.act(make_observation(), make_task())

        # After both steps, messages list has been mutated to:
        # [user(initial), assistant(step1), user(tool_result), assistant(step2)]
        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        assert len(messages) == 4
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        assert messages[3]["role"] == "assistant"
        # Check tool_result structure in second user message
        tool_result = messages[2]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "toolu_left_click_001"

    def test_uses_beta_api(self, agent, mock_anthropic_client):
        """Verifies the beta API is used with correct betas parameter."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent.act(make_observation(), make_task())

        call_args = mock_anthropic_client.beta.messages.create.call_args
        assert "betas" in call_args.kwargs
        assert "computer-use-2025-11-24" in call_args.kwargs["betas"]

    def test_uses_computer_tool(self, agent, mock_anthropic_client):
        """Verifies computer_20251124 tool is passed."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent.act(make_observation(), make_task())

        call_args = mock_anthropic_client.beta.messages.create.call_args
        tools = call_args.kwargs["tools"]
        assert len(tools) == 1
        assert tools[0]["type"] == "computer_20251124"
        assert tools[0]["name"] == "computer"
        assert tools[0]["display_width_px"] == 1280
        assert tools[0]["display_height_px"] == 720

    def test_reset_clears_state(self, agent, mock_anthropic_client):
        """Reset clears conversation history."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent.act(make_observation(), make_task())
        assert agent._step_count == 1
        assert len(agent._messages) > 0

        agent.reset()
        assert agent._step_count == 0
        assert len(agent._messages) == 0
        assert agent._last_tool_use_id is None


class TestDemoInjection:
    """Test demo conditioning support."""

    def test_demo_included_in_first_step(self, agent_with_demo, mock_anthropic_client):
        """Demo text is included in the first step's user message."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent_with_demo.act(make_observation(), make_task("Open Notepad"))

        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        content = messages[0]["content"]
        text_parts = [p["text"] for p in content if p.get("type") == "text"]
        full_text = " ".join(text_parts)
        assert "demonstration" in full_text.lower()
        assert "Click Start menu" in full_text
        assert "Open Notepad" in full_text

    def test_no_demo_sends_plain_instruction(self, agent, mock_anthropic_client):
        """Without demo, just sends task instruction."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent.act(make_observation(), make_task("Open Notepad"))

        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        content = messages[0]["content"]
        text_parts = [p["text"] for p in content if p.get("type") == "text"]
        full_text = " ".join(text_parts)
        assert "Task: Open Notepad" in full_text
        assert "demonstration" not in full_text.lower()


class TestScreenshotEncoding:
    """Test screenshot encoding and display dimension handling."""

    def test_screenshot_bytes_encoded(self, agent, mock_anthropic_client):
        """Screenshot bytes are base64 encoded in messages."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[100, 100])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        obs = make_observation()
        agent.act(obs, make_task())

        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        content = messages[0]["content"]
        image_parts = [p for p in content if p.get("type") == "image"]
        assert len(image_parts) == 1
        assert image_parts[0]["source"]["media_type"] == "image/png"
        # Verify it's valid base64
        decoded = base64.b64decode(image_parts[0]["source"]["data"])
        assert len(decoded) > 0

    def test_viewport_updates_display_dimensions(self, agent, mock_anthropic_client):
        """Display dimensions update from observation viewport."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[960, 540])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        obs = make_observation(
            screenshot_bytes=create_test_screenshot(1920, 1080),
            viewport=(1920, 1080),
        )
        action = agent.act(obs, make_task())

        # Coordinates should be normalized against 1920x1080
        assert abs(action.x - 0.5) < 0.01  # 960/1920 = 0.5
        assert abs(action.y - 0.5) < 0.01  # 540/1080 = 0.5

    def test_no_screenshot_returns_error(self, agent, mock_anthropic_client):
        """Agent returns error when no screenshot is available."""
        obs = BenchmarkObservation(screenshot=None)
        action = agent.act(obs, make_task())

        assert action.type == "error"
        assert action.raw_action["reason"] == "no_screenshot"
        assert action.raw_action["error_type"] == "infrastructure"


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_api_error_returns_error(self, agent, mock_anthropic_client):
        """API error results in error action."""
        mock_anthropic_client.beta.messages.create.side_effect = Exception("API error")

        action = agent.act(make_observation(), make_task())

        assert action.type == "error"
        assert action.raw_action["reason"] == "api_call_failed"
        assert action.raw_action["error_type"] == "infrastructure"

    def test_unknown_action_returns_error(self, agent, mock_anthropic_client):
        """An unknown tool action is not task completion."""
        response = create_mock_response(
            create_tool_use_block("unknown_action_xyz")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "error"

    def test_key_with_multiple_modifiers(self, agent, mock_anthropic_client):
        """Key with multiple modifiers splits correctly."""
        response = create_mock_response(
            create_tool_use_block("key", text="ctrl+shift+s")
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "key"
        assert action.key == "s"
        assert action.modifiers == ["ctrl", "shift"]

    def test_coordinate_edge_values(self, agent, mock_anthropic_client):
        """Coordinates at (0,0) get clamped to avoid PyAutoGUI fail-safe."""
        response = create_mock_response(
            create_tool_use_block("left_click", coordinate=[0, 0])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        action = agent.act(make_observation(), make_task())

        assert action.type == "click"
        # (0,0) is clamped to (_COORD_EPS, _COORD_EPS) to avoid fail-safe
        assert action.x == 0.005
        assert action.y == 0.005

    def test_missing_api_key_raises(self):
        """Missing API key raises RuntimeError."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove ANTHROPIC_API_KEY
            import os

            orig = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                from openadapt_evals.agents.claude_computer_use_agent import (
                    ClaudeComputerUseAgent,
                )

                with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
                    ClaudeComputerUseAgent()
            finally:
                if orig:
                    os.environ["ANTHROPIC_API_KEY"] = orig


class TestImport:
    """Test that the agent can be imported from the package."""

    def test_import_from_agents_package(self):
        """ClaudeComputerUseAgent is importable from agents package."""
        from openadapt_evals.agents import ClaudeComputerUseAgent

        assert ClaudeComputerUseAgent is not None

    def test_in_all(self):
        """ClaudeComputerUseAgent is in __all__."""
        from openadapt_evals import agents

        assert "ClaudeComputerUseAgent" in agents.__all__


# --- Multi-level demo fixture ---

SAMPLE_MULTILEVEL_DEMO = """\
GOAL: Calculate annual asset changes in a new spreadsheet sheet

PLAN:
1. Create a new sheet for calculating annual changes
2. Create a header row with Year, CA changes, FA changes, OA changes
3. Enter years 2015-2019 in column A
4. Enter CA change formula in B2 and drag-fill down
5. Enter FA change formula in C2 and drag-fill down

REFERENCE TRAJECTORY (for disambiguation -- adapt actions to your actual screen):

Step 1:
  Think: I need to create a new sheet for calculating annual changes.
  Action: Right-click on the "Sheet1" tab at the bottom and select "Insert Sheet".
  Expect: A new blank sheet named "Sheet2" should appear.

Step 2:
  Think: I need to create a header row.
  Action: Click cell A1 and type "Year"
  Expect: The text "Year" should appear in cell A1.

Step 3:
  Think: I need to enter years.
  Action: Click cell A2 and type "2015", then press Enter and type "2016".
  Expect: Years 2015 and 2016 appear in cells A2 and A3.

Step 4:
  Think: I need to enter the CA change formula.
  Action: Click cell B2 and type "=(Sheet1.B3-Sheet1.B2)/Sheet1.B2"
  Expect: Cell B2 should contain the percentage change formula.

Step 5:
  Think: I need to enter the FA change formula.
  Action: Click cell C2 and type "=(Sheet1.C3-Sheet1.C2)/Sheet1.C2"
  Expect: Cell C2 should contain the FA percentage change formula.

If your screen doesn't match, re-evaluate based on the PLAN.
"""


class TestParseMultilevelDemo:
    """Test _parse_multilevel_demo() parsing function."""

    def test_parses_goal(self):
        """Goal text is extracted correctly."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        result = _parse_multilevel_demo(SAMPLE_MULTILEVEL_DEMO)
        assert result is not None
        assert "Calculate annual asset changes" in result["goal"]

    def test_parses_plan_steps(self):
        """Plan steps are extracted as a list of strings."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        result = _parse_multilevel_demo(SAMPLE_MULTILEVEL_DEMO)
        assert result is not None
        assert len(result["plan_steps"]) == 5
        assert "Create a new sheet" in result["plan_steps"][0]
        assert "header row" in result["plan_steps"][1]
        assert "FA change formula" in result["plan_steps"][4]

    def test_parses_trajectory_steps(self):
        """Trajectory steps are extracted with think/action/expect."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        result = _parse_multilevel_demo(SAMPLE_MULTILEVEL_DEMO)
        assert result is not None
        assert len(result["trajectory"]) == 5

        step1 = result["trajectory"][0]
        assert step1["step_num"] == 1
        assert "create a new sheet" in step1["think"].lower()
        assert "Right-click" in step1["action"]
        assert "Sheet2" in step1["expect"]

        step5 = result["trajectory"][4]
        assert step5["step_num"] == 5
        assert "FA change formula" in step5["think"]

    def test_returns_none_for_plain_demo(self):
        """Non-multilevel demo returns None."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        result = _parse_multilevel_demo("Step 1: Click Start\nStep 2: Type notepad")
        assert result is None

    def test_returns_none_for_empty_string(self):
        """Empty string returns None."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        assert _parse_multilevel_demo("") is None

    def test_returns_none_for_none(self):
        """None input returns None."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        assert _parse_multilevel_demo(None) is None

    def test_partial_format_returns_none(self):
        """Demo with only GOAL but no PLAN or TRAJECTORY returns None."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        partial = "GOAL: Do something\n\nSome instructions here."
        assert _parse_multilevel_demo(partial) is None

    def test_parses_real_demo_file(self):
        """Parse the actual multilevel demo file from the repo."""
        from pathlib import Path

        from openadapt_evals.agents.claude_computer_use_agent import (
            _parse_multilevel_demo,
        )

        demo_path = Path(
            "/Users/abrichr/oa/src/openadapt-evals/.claude/worktrees/eval-fixes/"
            "demo_prompts_vlm/"
            "04d9aeaf-7bed-4024-bedb-e10e6f00eb7f-WOS_multilevel.txt"
        )
        if not demo_path.exists():
            pytest.skip("Real demo file not available")

        demo_text = demo_path.read_text()
        result = _parse_multilevel_demo(demo_text)
        assert result is not None
        assert len(result["plan_steps"]) == 13
        assert len(result["trajectory"]) == 21
        assert "annual changes" in result["goal"].lower()


class TestBuildPlanProgressText:
    """Test _build_plan_progress_text() formatting function."""

    def test_initial_progress(self):
        """Progress text at the start shows first step as current."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _build_plan_progress_text,
        )

        plan_steps = [
            {"text": "Create sheet", "status": "in_progress", "step_num": 1},
            {"text": "Add headers", "status": "pending", "step_num": 2},
            {"text": "Enter data", "status": "pending", "step_num": 3},
        ]
        trajectory = [
            {"step_num": 1, "think": "Need sheet", "action": "Right-click tab",
             "expect": "New sheet"},
        ]

        text = _build_plan_progress_text("Test goal", plan_steps, trajectory, 1)
        assert "GOAL: Test goal" in text
        assert "step 1/3" in text
        assert "Completed: (none yet)" in text
        assert "Current: step 1 - Create sheet" in text
        assert "Add headers" in text  # in remaining
        assert "CURRENT STEP DETAIL" in text
        assert "Right-click tab" in text
        assert "MUST complete ALL remaining steps" in text

    def test_midway_progress(self):
        """Progress text midway through shows completed and remaining."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _build_plan_progress_text,
        )

        plan_steps = [
            {"text": "Create sheet", "status": "done", "step_num": 1},
            {"text": "Add headers", "status": "in_progress", "step_num": 2},
            {"text": "Enter data", "status": "pending", "step_num": 3},
        ]

        text = _build_plan_progress_text("Goal", plan_steps, [], 5)
        assert "step 2/3" in text
        assert "[1] Create sheet" in text  # in completed
        assert "Current: step 2 - Add headers" in text
        assert "[3] Enter data" in text  # in remaining

    def test_all_done_progress(self):
        """Progress text when all steps are complete."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            _build_plan_progress_text,
        )

        plan_steps = [
            {"text": "Step A", "status": "done", "step_num": 1},
            {"text": "Step B", "status": "done", "step_num": 2},
        ]

        text = _build_plan_progress_text("Goal", plan_steps, [], 10)
        assert "all steps complete" in text.lower()


class TestPlanProgressTracking:
    """Test plan progress tracking in the agent."""

    @pytest.fixture
    def agent_with_multilevel_demo(self, mock_anthropic_client):
        """Create a ClaudeComputerUseAgent with a multi-level demo."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-123"}):
            with patch("anthropic.Anthropic", return_value=mock_anthropic_client):
                from openadapt_evals.agents.claude_computer_use_agent import (
                    ClaudeComputerUseAgent,
                )

                return ClaudeComputerUseAgent(demo=SAMPLE_MULTILEVEL_DEMO)

    def test_multilevel_demo_enables_tracking(self, agent_with_multilevel_demo):
        """Multi-level demo initializes plan step tracking."""
        agent = agent_with_multilevel_demo
        assert len(agent._plan_steps) == 5
        assert agent._plan_steps[0]["status"] == "in_progress"
        assert agent._plan_steps[1]["status"] == "pending"
        assert agent._goal != ""
        assert len(agent._trajectory) == 5

    def test_plain_demo_no_tracking(self, agent_with_demo):
        """Non-multilevel demo does not enable tracking."""
        assert len(agent_with_demo._plan_steps) == 0
        assert agent_with_demo._parsed_demo is None

    def test_no_demo_no_tracking(self, agent):
        """No demo does not enable tracking."""
        assert len(agent._plan_steps) == 0
        assert agent._parsed_demo is None

    def test_reset_reinitializes_plan(self, agent_with_multilevel_demo):
        """Reset re-initializes plan steps to initial state."""
        agent = agent_with_multilevel_demo
        # Manually mark some steps as done
        agent._plan_steps[0]["status"] = "done"
        agent._plan_steps[1]["status"] = "in_progress"
        agent._consecutive_done_overrides = 2

        agent.reset()

        assert agent._plan_steps[0]["status"] == "in_progress"
        assert agent._plan_steps[1]["status"] == "pending"
        assert agent._consecutive_done_overrides == 0

    def test_first_step_injects_plan_progress(
        self, agent_with_multilevel_demo, mock_anthropic_client
    ):
        """First step with multi-level demo uses plan progress text."""
        response = create_mock_response(
            create_tool_use_block("right_click", coordinate=[100, 700])
        )
        mock_anthropic_client.beta.messages.create.return_value = response

        agent_with_multilevel_demo.act(make_observation(), make_task())

        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        content = messages[0]["content"]
        text_parts = [p["text"] for p in content if p.get("type") == "text"]
        full_text = " ".join(text_parts)
        # Should have plan progress, not raw demo
        assert "structured plan" in full_text.lower()
        assert "PLAN PROGRESS" in full_text
        assert "MUST complete ALL" in full_text

    def test_subsequent_step_injects_plan_progress(
        self, agent_with_multilevel_demo, mock_anthropic_client
    ):
        """Subsequent steps inject dynamic plan progress text."""
        # Step 1
        response1 = create_mock_response(
            create_tool_use_block("right_click", coordinate=[100, 700])
        )
        mock_anthropic_client.beta.messages.create.return_value = response1
        agent_with_multilevel_demo.act(make_observation(), make_task())

        # Step 2
        response2 = create_mock_response(
            create_tool_use_block("type", text="Year")
        )
        mock_anthropic_client.beta.messages.create.return_value = response2
        agent_with_multilevel_demo.act(make_observation(), make_task())

        # Check that plan progress was injected in step 2
        call_args = mock_anthropic_client.beta.messages.create.call_args
        messages = call_args.kwargs["messages"]
        # Find the user message for step 2 (should be at index 2)
        step2_user = messages[2]
        assert step2_user["role"] == "user"
        text_parts = [
            p["text"] for p in step2_user["content"]
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        assert any("PLAN PROGRESS" in t for t in text_parts)

    def test_max_done_overrides_constant(self, agent_with_multilevel_demo):
        """MAX_DONE_OVERRIDES class constant exists and defaults to 3."""
        from openadapt_evals.agents.claude_computer_use_agent import (
            ClaudeComputerUseAgent,
        )

        assert ClaudeComputerUseAgent.MAX_DONE_OVERRIDES == 3

    def test_premature_done_override(
        self, agent_with_multilevel_demo, mock_anthropic_client
    ):
        """Premature 'done' is overridden when plan steps remain."""
        agent = agent_with_multilevel_demo

        # First call: Claude declares done (text only, no tool_use)
        done_response = create_mock_response(
            create_text_block("Task completed successfully.")
        )
        # Second call (after override): Claude returns an action
        action_response = create_mock_response(
            create_tool_use_block("right_click", coordinate=[100, 700])
        )
        mock_anthropic_client.beta.messages.create.side_effect = [
            done_response,
            action_response,
        ]

        action = agent.act(make_observation(), make_task())

        # Should NOT be done — override kicked in
        assert action.type == "click"
        # Verify two API calls were made
        assert mock_anthropic_client.beta.messages.create.call_count == 2
        # Override counter should have been incremented then reset
        assert agent._consecutive_done_overrides == 0

    def test_done_accepted_after_max_overrides(
        self, agent_with_multilevel_demo, mock_anthropic_client
    ):
        """Done is accepted after MAX_DONE_OVERRIDES consecutive overrides."""
        agent = agent_with_multilevel_demo
        # Set overrides to the max
        agent._consecutive_done_overrides = agent.MAX_DONE_OVERRIDES

        done_response = create_mock_response(
            create_text_block("I'm done.")
        )
        mock_anthropic_client.beta.messages.create.return_value = done_response

        action = agent.act(make_observation(), make_task())

        # Should be done since overrides are exhausted
        assert action.type == "done"
        # Only one API call (no retry)
        assert mock_anthropic_client.beta.messages.create.call_count == 1

    def test_done_accepted_when_all_steps_complete(
        self, agent_with_multilevel_demo, mock_anthropic_client
    ):
        """Done is accepted when all plan steps are marked done."""
        agent = agent_with_multilevel_demo
        # Mark all steps as done
        for step in agent._plan_steps:
            step["status"] = "done"

        done_response = create_mock_response(
            create_text_block("Task completed successfully.")
        )
        mock_anthropic_client.beta.messages.create.return_value = done_response

        action = agent.act(make_observation(), make_task())

        assert action.type == "done"
        # Only one API call (no retry since all steps are done)
        assert mock_anthropic_client.beta.messages.create.call_count == 1


class TestPlanStepAdvancement:
    """Test heuristic plan step advancement."""

    @pytest.fixture
    def agent_with_multilevel_demo(self, mock_anthropic_client):
        """Create agent with multi-level demo for advancement tests."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-123"}):
            with patch("anthropic.Anthropic", return_value=mock_anthropic_client):
                from openadapt_evals.agents.claude_computer_use_agent import (
                    ClaudeComputerUseAgent,
                )

                return ClaudeComputerUseAgent(demo=SAMPLE_MULTILEVEL_DEMO)

    def test_type_action_matches_header_step(self, agent_with_multilevel_demo):
        """Typing 'Year' matches the header step."""
        agent = agent_with_multilevel_demo
        # Step 1 is in_progress (create sheet). Typing "Year" matches step 2 better.
        action = BenchmarkAction(
            type="type",
            text="Year",
            raw_action={"claude_action": {"action": "type", "text": "Year"}},
        )
        agent._advance_plan_steps(action)
        # Step 1 should be done, step 2 should be in_progress
        assert agent._plan_steps[0]["status"] == "done"
        assert agent._plan_steps[1]["status"] == "in_progress"

    def test_click_action_stays_on_current(self, agent_with_multilevel_demo):
        """A generic click stays on the current step."""
        agent = agent_with_multilevel_demo
        action = BenchmarkAction(
            type="click",
            x=0.5, y=0.5,
            raw_action={"claude_action": {"action": "left_click"},
                        "click_variant": "left_click"},
        )
        agent._advance_plan_steps(action)
        # Step 1 should still be in_progress (generic click matches many steps)
        assert agent._plan_steps[0]["status"] == "in_progress"

    def test_extract_action_keywords(self, agent_with_multilevel_demo):
        """Keywords are extracted from various action types."""
        agent = agent_with_multilevel_demo

        # Type action
        action = BenchmarkAction(
            type="type",
            text="=(Sheet1.B3-Sheet1.B2)/Sheet1.B2",
            raw_action={"claude_action": {"action": "type"}},
        )
        keywords = agent._extract_action_keywords(action)
        assert "type" in keywords
        assert "=(sheet1.b3-sheet1.b2)/sheet1.b2" in keywords

        # Key action
        action2 = BenchmarkAction(
            type="key",
            key="Return",
            raw_action={"claude_action": {"action": "key"}},
        )
        keywords2 = agent._extract_action_keywords(action2)
        assert "key" in keywords2
        assert "return" in keywords2

    def test_no_plan_steps_no_crash(self, agent):
        """Advancement with no plan steps does not crash."""
        action = BenchmarkAction(type="click", x=0.5, y=0.5, raw_action={})
        # Should be a no-op, not crash
        agent._advance_plan_steps(action)

    def test_no_multi_step_jump_on_keyword_match(self, agent_with_multilevel_demo):
        """Action matching a distant step should NOT skip intermediate steps.

        This is the core drift fix: previously, typing a formula like
        '=(Sheet1.B3-Sheet1.B2)' would match step 4 (CA formula) better
        than step 1 (create sheet), causing steps 1-3 to all be marked
        done without any VLM verification. Now it should advance at most
        one step at a time.
        """
        agent = agent_with_multilevel_demo
        # Step 1 is in_progress (create sheet)
        assert agent._plan_steps[0]["status"] == "in_progress"

        # Type a formula that in the old code would match step 4 (CA formula)
        # better than the current step 1 (create sheet), causing steps 1-3
        # to all be marked done
        action = BenchmarkAction(
            type="type",
            text="=(Sheet1.B3-Sheet1.B2)/Sheet1.B2",
            raw_action={"claude_action": {"action": "type",
                        "text": "=(Sheet1.B3-Sheet1.B2)/Sheet1.B2"}},
        )
        agent._advance_plan_steps(action)

        # With the fix: should advance at most one step (step 1 -> step 2)
        # Step 1 should be done (at most)
        # Step 3 should still be pending (NOT done)
        # Step 4 should still be pending (NOT in_progress)
        assert agent._plan_steps[2]["status"] == "pending"
        assert agent._plan_steps[3]["status"] == "pending"

    def test_sequential_advancement_requires_multiple_calls(
        self, agent_with_multilevel_demo
    ):
        """Advancing through all 5 steps requires 5 separate calls.

        Each call to _advance_plan_steps should advance at most one step,
        so reaching step 5 from step 1 requires at least 4 advancement calls.
        """
        agent = agent_with_multilevel_demo
        assert agent._plan_steps[0]["status"] == "in_progress"

        # Simulate step 1 -> step 2 (type "Year" matches header step)
        action1 = BenchmarkAction(
            type="type", text="Year",
            raw_action={"claude_action": {"action": "type", "text": "Year"}},
        )
        agent._advance_plan_steps(action1)
        assert agent._plan_steps[0]["status"] == "done"
        assert agent._plan_steps[1]["status"] == "in_progress"
        assert agent._plan_steps[2]["status"] == "pending"

        # Step 2 -> step 3 (type "2015" matches years step)
        action2 = BenchmarkAction(
            type="type", text="2015",
            raw_action={"claude_action": {"action": "type", "text": "2015"}},
        )
        agent._advance_plan_steps(action2)
        assert agent._plan_steps[1]["status"] == "done"
        assert agent._plan_steps[2]["status"] == "in_progress"
        assert agent._plan_steps[3]["status"] == "pending"

        # Verify that after 2 calls, we are at step 3 -- NOT at step 5
        assert agent._plan_steps[4]["status"] == "pending"

    def test_drift_scenario_from_live_eval(self, agent_with_multilevel_demo):
        """Reproduce the exact drift scenario from the Level 3 live eval.

        In the live eval, at agent step 3 the tracking jumped from step 1
        to step 6, marking steps 2-5 as done without verification. This
        test ensures that cannot happen with the fix.
        """
        agent = agent_with_multilevel_demo
        assert len(agent._plan_steps) == 5

        # Simulate a single action that could heuristically match many steps
        # (e.g., right_click matches "Right-click on Sheet1 tab" in step 1,
        # but also generic click references in other steps)
        action = BenchmarkAction(
            type="click", x=0.1, y=0.9,
            raw_action={
                "claude_action": {"action": "right_click", "coordinate": [128, 648]},
                "click_variant": "right_click",
            },
        )

        # Call advance 1 time
        agent._advance_plan_steps(action)

        # Count how many steps are now done
        done_count = sum(1 for s in agent._plan_steps if s["status"] == "done")
        # At most 1 step should be marked done (the current one, if next matched better)
        assert done_count <= 1, (
            f"Expected at most 1 step done after single advance, got {done_count}. "
            f"Steps: {[(s['step_num'], s['status']) for s in agent._plan_steps]}"
        )

        # Steps 3, 4, 5 must still be pending
        assert agent._plan_steps[2]["status"] == "pending"
        assert agent._plan_steps[3]["status"] == "pending"
        assert agent._plan_steps[4]["status"] == "pending"

    def test_external_step_control_suppresses_heuristic(
        self, agent_with_multilevel_demo
    ):
        """When _external_step_control is True, _advance_plan_steps is a no-op.

        The DemoController sets this flag so that step progression is driven
        by VLM verification, not the agent's keyword heuristic.
        """
        agent = agent_with_multilevel_demo
        assert agent._plan_steps[0]["status"] == "in_progress"

        # Enable external step control (as DemoController does)
        agent._external_step_control = True

        # This action would normally advance step 1 -> step 2
        action = BenchmarkAction(
            type="type", text="Year",
            raw_action={"claude_action": {"action": "type", "text": "Year"}},
        )
        agent._advance_plan_steps(action)

        # With external control enabled, nothing should have changed
        assert agent._plan_steps[0]["status"] == "in_progress"
        assert agent._plan_steps[1]["status"] == "pending"

    def test_external_step_control_default_false(
        self, agent_with_multilevel_demo
    ):
        """_external_step_control defaults to False so the agent works
        standalone (without DemoController).
        """
        agent = agent_with_multilevel_demo
        assert agent._external_step_control is False
