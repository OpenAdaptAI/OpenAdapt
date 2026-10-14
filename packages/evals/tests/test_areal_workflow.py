"""Tests for AReaL AgentWorkflow wrapping WAADesktopEnv.

Verifies that WAADesktopWorkflow:
- Runs episodes to completion with a mock adapter (no real VM or AReaL)
- Parses LLM action responses correctly
- Computes rewards via evaluate_dense()
- Returns the correct reward format
- Handles edge cases (immediate done, max steps, LLM errors)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.adapters.rl_env import RLEnvironment
from openadapt_evals.errors import ActionParseError
from openadapt_evals.task_config import Milestone, TaskCheck, TaskConfig
from openadapt_evals.training.areal_workflow import (
    SYSTEM_PROMPT,
    WAADesktopWorkflow,
    _build_messages,
    _screenshot_to_base64,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_adapter(steps_to_done: int = 3) -> MagicMock:
    """Create a mock BenchmarkAdapter for testing without a VM.

    Uses spec=BenchmarkAdapter to prevent MagicMock from auto-creating
    attributes like pixel_action and screen_size that would confuse
    RLEnvironment's feature detection.
    """
    from openadapt_evals.adapters.base import BenchmarkAdapter

    adapter = MagicMock(spec=BenchmarkAdapter)
    call_count = {"n": 0}

    # Minimal valid PNG (1x1 gray pixel)
    fake_png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        + b"\x00" * 50
    )

    def mock_step(action):
        call_count["n"] += 1
        done = call_count["n"] >= steps_to_done
        return (
            BenchmarkObservation(
                screenshot=fake_png,
                viewport=(1920, 1200),
                raw_observation={},
            ),
            done,
            {"step": call_count["n"]},
        )

    adapter.step.side_effect = mock_step
    adapter.reset.return_value = BenchmarkObservation(
        screenshot=fake_png,
        viewport=(1920, 1200),
        raw_observation={},
    )
    adapter.load_task.return_value = BenchmarkTask(
        task_id="test-task", instruction="Test instruction", domain="desktop"
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test-task", success=False, score=0.0
    )
    # BenchmarkAdapter spec does not include config, so set it explicitly
    adapter.config = MagicMock(server_url="http://mock:5000")
    return adapter


def _make_task_config(passed_milestones: int = 1, total_milestones: int = 2) -> TaskConfig:
    """Create a TaskConfig with milestones for dense reward testing."""
    milestones = []
    for i in range(total_milestones):
        milestones.append(
            Milestone(
                name=f"Step {i + 1}",
                check=TaskCheck(
                    check="command",
                    run=f"echo {1 if i < passed_milestones else 0}",
                    expect="1",
                    match="exact",
                ),
            )
        )
    return TaskConfig(
        name="Test task",
        id="test-task",
        domain="desktop",
        setup=[],
        checks=[],
        combine="and",
        max_steps=15,
        milestones=milestones,
    )


def _make_mock_completion(action_json: str, comp_id: str = "cmpl-test") -> MagicMock:
    """Create a mock OpenAI ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = action_json
    completion = MagicMock()
    completion.choices = [choice]
    completion.id = comp_id
    return completion


def _make_mock_client(actions: list[str]) -> AsyncMock:
    """Create a mock AsyncOpenAI client that returns a sequence of actions."""
    client = AsyncMock()
    completions = [_make_mock_completion(a, f"cmpl-{i}") for i, a in enumerate(actions)]
    client.chat.completions.create = AsyncMock(side_effect=completions)
    return client


# ---------------------------------------------------------------------------
# Unit tests: message building and screenshot encoding
# ---------------------------------------------------------------------------


class TestScreenshotEncoding:
    def test_base64_roundtrip(self):
        """Verify screenshot bytes survive base64 encoding."""
        data = b"\x89PNG\r\n\x1a\n" + b"\x42" * 20
        encoded = _screenshot_to_base64(data)
        assert encoded.startswith("data:image/png;base64,")
        import base64 as b64
        decoded = b64.b64decode(encoded.split(",", 1)[1])
        assert decoded == data

    def test_empty_screenshot(self):
        encoded = _screenshot_to_base64(b"")
        assert encoded == "data:image/png;base64,"


class TestBuildMessages:
    def test_basic_messages(self):
        msgs = _build_messages("Click OK", b"\x89PNG", history=[])
        assert msgs[0]["role"] == "system"
        assert "CLICK" in SYSTEM_PROMPT or "click" in SYSTEM_PROMPT
        assert msgs[1]["role"] == "user"
        # User message should contain text and image_url
        assert any(c["type"] == "text" for c in msgs[1]["content"])
        assert any(c["type"] == "image_url" for c in msgs[1]["content"])

    def test_with_history(self):
        history = [
            {"action_text": '{"type": "click", "x": 0.5, "y": 0.5}', "screenshot": b"\x89PNG"},
        ]
        msgs = _build_messages("Click OK", b"\x89PNG", history=history)
        # system + user + assistant + user (screenshot after action)
        assert len(msgs) == 4
        assert msgs[2]["role"] == "assistant"
        assert msgs[3]["role"] == "user"

    def test_no_screenshot(self):
        msgs = _build_messages("Click OK", None, history=[])
        # Should still have text content, just no image_url
        user_content = msgs[1]["content"]
        assert any(c["type"] == "text" for c in user_content)
        assert not any(c.get("type") == "image_url" for c in user_content)


# ---------------------------------------------------------------------------
# Integration tests: full episode with mock adapter + mock OpenAI client
# ---------------------------------------------------------------------------


class TestWAADesktopWorkflow:
    def test_episode_runs_to_completion(self):
        """Test a full episode with mock adapter and mock LLM client."""
        adapter = _make_mock_adapter(steps_to_done=2)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = [
            json.dumps({"type": "click", "x": 0.5, "y": 0.3}),
            json.dumps({"type": "click", "x": 0.2, "y": 0.8}),
        ]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                reward = asyncio.run(
                    wf.run(
                        data={
                            "task_id": "test-task",
                            "instruction": "Click the button",
                            "max_steps": 5,
                        },
                        base_url="http://fake:8000/v1",
                        api_key="fake-key",
                    )
                )

        assert isinstance(reward, float)
        assert 0.0 <= reward <= 1.0

    def test_immediate_done_action(self):
        """Agent says DONE immediately -- episode should end in 0 env steps."""
        adapter = _make_mock_adapter(steps_to_done=10)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = [json.dumps({"type": "done"})]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                reward = asyncio.run(
                    wf.run(
                        data={
                            "task_id": "test-task",
                            "instruction": "Do nothing",
                            "max_steps": 5,
                        },
                        base_url="http://fake:8000/v1",
                        api_key="fake-key",
                    )
                )

        assert isinstance(reward, float)
        # No actions taken, reward should be 0.0
        assert reward == 0.0
        # Adapter step should NOT have been called (done before first step)
        assert adapter.step.call_count == 0

    def test_max_steps_reached(self):
        """Episode should stop at max_steps even if agent never says done."""
        adapter = _make_mock_adapter(steps_to_done=100)  # Never done via adapter
        env = RLEnvironment(adapter, default_task_id="test-task")

        max_steps = 3
        actions = [
            json.dumps({"type": "click", "x": 0.1, "y": 0.1}),
            json.dumps({"type": "click", "x": 0.2, "y": 0.2}),
            json.dumps({"type": "click", "x": 0.3, "y": 0.3}),
        ]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                reward = asyncio.run(
                    wf.run(
                        data={
                            "task_id": "test-task",
                            "instruction": "Keep clicking",
                            "max_steps": max_steps,
                        },
                        base_url="http://fake:8000/v1",
                        api_key="fake-key",
                    )
                )

        assert isinstance(reward, float)
        # Should have called step exactly max_steps times
        assert adapter.step.call_count == max_steps

    def test_dense_reward_with_milestones(self):
        """Test that evaluate_dense() returns partial credit from milestones."""
        adapter = _make_mock_adapter(steps_to_done=1)
        tc = _make_task_config(passed_milestones=1, total_milestones=2)
        env = RLEnvironment(adapter, default_task_id="test-task", task_config=tc)

        actions = [json.dumps({"type": "click", "x": 0.5, "y": 0.5})]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                with patch.object(TaskConfig, "_run_vm_command") as mock_cmd:
                    mock_cmd.side_effect = ["1", "0"]
                    reward = asyncio.run(
                        wf.run(
                            data={
                                "task_id": "test-task",
                                "instruction": "Test milestones",
                                "max_steps": 5,
                            },
                            base_url="http://fake:8000/v1",
                            api_key="fake-key",
                        )
                    )

        # 1/2 milestones = 0.5, binary = 0.0, max = 0.5
        assert reward == pytest.approx(0.5)

    def test_unparseable_llm_output_is_not_task_completion(self):
        adapter = _make_mock_adapter(steps_to_done=10)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = ["I don't know what to do, this is confusing"]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                with pytest.raises(ActionParseError):
                    asyncio.run(
                        wf.run(
                            data={
                                "task_id": "test-task",
                                "instruction": "Do something",
                                "max_steps": 5,
                            },
                            base_url="http://fake:8000/v1",
                            api_key="fake-key",
                        )
                    )

        assert adapter.step.call_count == 0

    def test_type_action_no_coordinates(self):
        """TYPE actions have no coordinates -- should use env.step() directly."""
        adapter = _make_mock_adapter(steps_to_done=2)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = [
            json.dumps({"type": "type", "text": "hello world"}),
            json.dumps({"type": "done"}),
        ]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                asyncio.run(
                    wf.run(
                        data={
                            "task_id": "test-task",
                            "instruction": "Type some text",
                            "max_steps": 5,
                        },
                        base_url="http://fake:8000/v1",
                        api_key="fake-key",
                    )
                )

        # Should have called step once (type), then done
        assert adapter.step.call_count == 1
        action_arg = adapter.step.call_args[0][0]
        assert action_arg.type == "type"
        assert action_arg.text == "hello world"

    def test_reward_is_float(self):
        """Verify the return type is a plain float (AReaL expects this)."""
        adapter = _make_mock_adapter(steps_to_done=1)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = [json.dumps({"type": "click", "x": 0.5, "y": 0.5})]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client):
                reward = asyncio.run(
                    wf.run(
                        data={
                            "task_id": "test-task",
                            "instruction": "Click",
                            "max_steps": 1,
                        },
                        base_url="http://fake:8000/v1",
                        api_key="fake-key",
                    )
                )

        assert type(reward) is float

    def test_base_url_from_env_var(self):
        """When base_url not in extra_kwargs, fall back to OPENAI_BASE_URL env var."""
        adapter = _make_mock_adapter(steps_to_done=1)
        env = RLEnvironment(adapter, default_task_id="test-task")

        actions = [json.dumps({"type": "done"})]
        mock_client = _make_mock_client(actions)

        wf = WAADesktopWorkflow()

        with patch.object(wf, "_create_env", return_value=env):
            with patch("openadapt_evals.training.areal_workflow.AsyncOpenAI", return_value=mock_client) as mock_cls:
                with patch.dict("os.environ", {"OPENAI_BASE_URL": "http://env-var:8000/v1"}):
                    asyncio.run(
                        wf.run(
                            data={
                                "task_id": "test-task",
                                "instruction": "Test",
                                "max_steps": 1,
                            },
                            # No base_url in kwargs
                            api_key="test",
                        )
                    )

                # AsyncOpenAI should have been called with the env var URL
                call_kwargs = mock_cls.call_args
                assert call_kwargs.kwargs["base_url"] == "http://env-var:8000/v1"

    def test_constructor_kwargs_forwarded(self):
        """Verify temperature and max_tokens are customizable."""
        wf = WAADesktopWorkflow(temperature=0.3, max_tokens=128)
        assert wf.temperature == 0.3
        assert wf.max_tokens == 128

        # Extra kwargs should be stored
        wf2 = WAADesktopWorkflow(top_p=0.95)
        assert wf2.kwargs == {"top_p": 0.95}
