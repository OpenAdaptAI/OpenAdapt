"""Tests for APIBenchmarkAgent."""

import pytest

pytestmark = pytest.mark.heavy
openadapt_ml_benchmarks = pytest.importorskip("openadapt_ml.benchmarks")
APIBenchmarkAgent = openadapt_ml_benchmarks.APIBenchmarkAgent

from openadapt_evals import BenchmarkAction, BenchmarkObservation, BenchmarkTask


class TestAPIBenchmarkAgentParsing:
    """Test action parsing from VLM responses."""

    @pytest.fixture
    def agent(self):
        """Create an agent without API connection for parsing tests."""
        return APIBenchmarkAgent(provider="anthropic")

    def test_parse_click_coordinates(self, agent):
        """Test parsing CLICK with coordinates."""
        response = "I'll click at the button.\n\nACTION: CLICK(0.5, 0.3)"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3

    def test_parse_click_coordinates_no_prefix(self, agent):
        """Test parsing CLICK without ACTION: prefix."""
        response = "The button is at the center.\nCLICK(0.25, 0.75)"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.x == 0.25
        assert action.y == 0.75

    def test_parse_click_element_id(self, agent):
        """Test parsing CLICK with element ID."""
        response = "ACTION: CLICK([5])"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.target_node_id == "5"

    def test_parse_click_element_id_no_brackets(self, agent):
        """Test parsing CLICK with element ID without square brackets."""
        response = "ACTION: CLICK(12)"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.target_node_id == "12"

    def test_parse_type_action(self, agent):
        """Test parsing TYPE action."""
        response = "ACTION: TYPE(\"hello world\")"
        action = agent._parse_response(response)
        assert action.type == "type"
        assert action.text == "hello world"

    def test_parse_type_action_single_quotes(self, agent):
        """Test parsing TYPE action with single quotes."""
        response = "ACTION: TYPE('test input')"
        action = agent._parse_response(response)
        assert action.type == "type"
        assert action.text == "test input"

    def test_parse_key_action_simple(self, agent):
        """Test parsing KEY action with simple key."""
        response = "ACTION: KEY(Enter)"
        action = agent._parse_response(response)
        assert action.type == "key"
        assert action.key == "Enter"

    def test_parse_key_action_with_modifier(self, agent):
        """Test parsing KEY action with modifier."""
        response = "ACTION: KEY(Ctrl+c)"
        action = agent._parse_response(response)
        assert action.type == "key"
        assert action.key == "c"
        assert action.modifiers == ["Ctrl"]

    def test_parse_key_action_multiple_modifiers(self, agent):
        """Test parsing KEY action with multiple modifiers."""
        response = "ACTION: KEY(Ctrl+Shift+s)"
        action = agent._parse_response(response)
        assert action.type == "key"
        assert action.key == "s"
        assert action.modifiers == ["Ctrl", "Shift"]

    def test_parse_scroll_up(self, agent):
        """Test parsing SCROLL up action."""
        response = "ACTION: SCROLL(up)"
        action = agent._parse_response(response)
        assert action.type == "scroll"
        assert action.scroll_direction == "up"

    def test_parse_scroll_down(self, agent):
        """Test parsing SCROLL down action."""
        response = "ACTION: SCROLL(down)"
        action = agent._parse_response(response)
        assert action.type == "scroll"
        assert action.scroll_direction == "down"

    def test_parse_drag_action(self, agent):
        """Test parsing DRAG action."""
        response = "ACTION: DRAG(0.1, 0.2, 0.8, 0.9)"
        action = agent._parse_response(response)
        assert action.type == "drag"
        assert action.x == 0.1
        assert action.y == 0.2
        assert action.end_x == 0.8
        assert action.end_y == 0.9

    def test_parse_done_action(self, agent):
        """Test parsing DONE action."""
        response = "The task is complete.\n\nACTION: DONE()"
        action = agent._parse_response(response)
        assert action.type == "done"

    def test_parse_answer_action(self, agent):
        """Test parsing ANSWER action."""
        response = 'ACTION: ANSWER("The answer is 42")'
        action = agent._parse_response(response)
        assert action.type == "answer"
        assert action.answer == "The answer is 42"

    def test_parse_no_action_returns_done(self, agent):
        """Test that unparseable response returns done action."""
        response = "I'm not sure what to do here."
        action = agent._parse_response(response)
        assert action.type == "done"
        assert action.raw_action.get("parse_error") == "No action pattern found"

    def test_parse_stores_raw_response(self, agent):
        """Test that raw response is stored in action."""
        response = "ACTION: CLICK(0.5, 0.5)"
        action = agent._parse_response(response)
        assert action.raw_action["response"] == response

    def test_parse_case_insensitive(self, agent):
        """Test that parsing is case insensitive."""
        response = "action: click(0.5, 0.5)"
        action = agent._parse_response(response)
        assert action.type == "click"

    def test_parse_click_with_spaces(self, agent):
        """Test parsing CLICK with extra spaces."""
        response = "ACTION: CLICK( 0.5 , 0.3 )"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3

    def test_parse_click_pixel_coordinates_normalized(self, agent):
        """Test that pixel coordinates are normalized using viewport."""
        observation = BenchmarkObservation(viewport=(1000, 800))
        response = "ACTION: CLICK(500, 400)"
        action = agent._parse_response(response, observation)
        assert action.type == "click"
        assert action.x == 0.5  # 500 / 1000
        assert action.y == 0.5  # 400 / 800
        assert action.raw_action.get("normalized") is True
        assert action.raw_action.get("original_coords") == {"x": 500, "y": 400}

    def test_parse_click_normalized_coordinates_unchanged(self, agent):
        """Test that normalized coordinates (0-1) are not modified."""
        observation = BenchmarkObservation(viewport=(1000, 800))
        response = "ACTION: CLICK(0.5, 0.3)"
        action = agent._parse_response(response, observation)
        assert action.type == "click"
        assert action.x == 0.5
        assert action.y == 0.3
        assert action.raw_action.get("normalized") is None  # No normalization needed

    def test_parse_click_pixel_coordinates_without_viewport(self, agent):
        """Test that pixel coordinates without viewport info are stored as-is."""
        # No observation provided
        response = "ACTION: CLICK(1000, 34)"
        action = agent._parse_response(response)
        assert action.type == "click"
        assert action.x == 1000  # Stored as-is without viewport
        assert action.y == 34

    def test_parse_drag_pixel_coordinates_normalized(self, agent):
        """Test that drag pixel coordinates are normalized."""
        observation = BenchmarkObservation(viewport=(1920, 1080))
        response = "ACTION: DRAG(100, 200, 1800, 900)"
        action = agent._parse_response(response, observation)
        assert action.type == "drag"
        assert abs(action.x - 100/1920) < 0.001
        assert abs(action.y - 200/1080) < 0.001
        assert abs(action.end_x - 1800/1920) < 0.001
        assert abs(action.end_y - 900/1080) < 0.001
        assert action.raw_action.get("normalized") is True


class TestAPIBenchmarkAgentSample:
    """Test sample building for API calls."""

    @pytest.fixture
    def agent(self):
        """Create an agent without API connection."""
        return APIBenchmarkAgent(provider="anthropic")

    @pytest.fixture
    def task(self):
        """Create a sample task."""
        return BenchmarkTask(
            task_id="test_task",
            instruction="Click the submit button",
            domain="browser",
        )

    @pytest.fixture
    def observation(self, tmp_path):
        """Create a sample observation."""
        # Create a dummy screenshot
        screenshot_path = tmp_path / "screenshot.png"
        screenshot_path.write_bytes(b"PNG_DATA")

        return BenchmarkObservation(
            screenshot_path=str(screenshot_path),
            url="https://example.com",
            window_title="Test Window",
        )

    def test_build_sample_basic(self, agent, task, observation):
        """Test basic sample building."""
        sample = agent._build_sample(observation, task, None)

        assert "messages" in sample
        assert "images" in sample
        assert sample["images"] == [observation.screenshot_path]

        messages = sample["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "Click the submit button" in messages[1]["content"]

    def test_build_sample_includes_context(self, agent, task, observation):
        """Test that URL and window title are included."""
        sample = agent._build_sample(observation, task, None)
        user_content = sample["messages"][1]["content"]

        assert "https://example.com" in user_content
        assert "Test Window" in user_content

    def test_build_sample_with_accessibility_tree(self, agent, task, observation):
        """Test sample building with accessibility tree."""
        observation.accessibility_tree = {
            "role": "button",
            "name": "Submit",
            "id": "1",
            "children": [],
        }

        sample = agent._build_sample(observation, task, None)
        user_content = sample["messages"][1]["content"]

        assert "UI Elements:" in user_content
        assert "[1] button: Submit" in user_content

    def test_build_sample_with_history(self, agent, task, observation):
        """Test sample building with action history."""
        history = [
            (
                observation,
                BenchmarkAction(type="click", x=0.5, y=0.5),
            ),
            (
                observation,
                BenchmarkAction(type="type", text="test"),
            ),
        ]

        sample = agent._build_sample(observation, task, history)
        user_content = sample["messages"][1]["content"]

        assert "Previous actions:" in user_content
        assert "CLICK" in user_content
        assert "TYPE" in user_content

    def test_build_sample_no_accessibility_tree_when_disabled(self, agent, task, observation):
        """Test that accessibility tree is excluded when disabled."""
        agent.use_accessibility_tree = False
        observation.accessibility_tree = {"role": "button", "name": "Submit", "id": "1"}

        sample = agent._build_sample(observation, task, None)
        user_content = sample["messages"][1]["content"]

        assert "UI Elements:" not in user_content

    def test_build_sample_no_history_when_disabled(self, agent, task, observation):
        """Test that history is excluded when disabled."""
        agent.use_history = False
        history = [(observation, BenchmarkAction(type="click", x=0.5, y=0.5))]

        sample = agent._build_sample(observation, task, history)
        user_content = sample["messages"][1]["content"]

        assert "Previous actions:" not in user_content


class TestAPIBenchmarkAgentActionString:
    """Test action to string conversion."""

    @pytest.fixture
    def agent(self):
        """Create an agent without API connection."""
        return APIBenchmarkAgent(provider="anthropic")

    def test_click_with_node_id(self, agent):
        """Test click action with node ID."""
        action = BenchmarkAction(type="click", target_node_id="5")
        assert agent._action_to_string(action) == "CLICK([5])"

    def test_click_with_coordinates(self, agent):
        """Test click action with coordinates."""
        action = BenchmarkAction(type="click", x=0.5, y=0.3)
        assert agent._action_to_string(action) == "CLICK(0.500, 0.300)"

    def test_type_action(self, agent):
        """Test type action."""
        action = BenchmarkAction(type="type", text="hello")
        assert agent._action_to_string(action) == "TYPE('hello')"

    def test_key_with_modifier(self, agent):
        """Test key action with modifier."""
        action = BenchmarkAction(type="key", key="c", modifiers=["Ctrl"])
        assert agent._action_to_string(action) == "KEY(Ctrl+c)"

    def test_scroll_action(self, agent):
        """Test scroll action."""
        action = BenchmarkAction(type="scroll", scroll_direction="down")
        assert agent._action_to_string(action) == "SCROLL(down)"

    def test_done_action(self, agent):
        """Test done action."""
        action = BenchmarkAction(type="done")
        assert agent._action_to_string(action) == "DONE()"


class TestAPIBenchmarkAgentInit:
    """Test agent initialization."""

    def test_init_anthropic(self):
        """Test initialization with Anthropic provider."""
        agent = APIBenchmarkAgent(provider="anthropic")
        assert agent.provider == "anthropic"
        assert agent._adapter is None  # Lazy initialization

    def test_init_openai(self):
        """Test initialization with OpenAI provider."""
        agent = APIBenchmarkAgent(provider="openai")
        assert agent.provider == "openai"

    def test_init_custom_max_tokens(self):
        """Test initialization with custom max tokens."""
        agent = APIBenchmarkAgent(provider="anthropic", max_tokens=1024)
        assert agent.max_tokens == 1024

    def test_init_disabled_options(self):
        """Test initialization with disabled options."""
        agent = APIBenchmarkAgent(
            provider="anthropic",
            use_accessibility_tree=False,
            use_history=False,
        )
        assert not agent.use_accessibility_tree
        assert not agent.use_history

    def test_reset_is_noop(self):
        """Test that reset does nothing (agent is stateless)."""
        agent = APIBenchmarkAgent(provider="anthropic")
        agent.reset()  # Should not raise
