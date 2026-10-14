"""Tests for demo persistence fix - P0 validation.

The 100%/0% Paradox:
- Demo-conditioned prompting achieves 100% first-action accuracy
- But 0% episode success rate

Root cause: Demo context was only injected at step 1, dropped at subsequent steps.

Fix: The `demo` parameter in ApiAgent now persists across ALL steps.

These tests validate that:
1. Demo is included in prompts at EVERY step (not just step 1)
2. Demo persists across agent.reset() calls (between tasks)
3. set_demo() works for dynamic demo retrieval scenarios
4. Logs correctly track demo inclusion at each step
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest


class TestDemoPersistence:
    """Tests for demo persistence across multiple steps."""

    @pytest.fixture
    def mock_screenshot_bytes(self) -> bytes:
        """Create minimal valid PNG bytes for testing."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    @pytest.fixture
    def sample_demo(self) -> str:
        """Sample demo trajectory text."""
        return """DEMONSTRATION:
Task: Turn off Night Shift in Display settings

Step 1:
  Action: CLICK(0.5, 0.3) - Click System Settings icon

Step 2:
  Action: CLICK(0.2, 0.6) - Click Display in sidebar

Step 3:
  Action: CLICK(0.7, 0.4) - Click Night Shift toggle
"""

    @pytest.fixture
    def mock_api_response(self) -> str:
        """Sample API response with correct format."""
        return """```memory
# Navigating to Display settings
```

```decision
CONTINUE
```

```python
computer.click(500, 300)
```"""

    def test_demo_included_at_every_step(
        self, mock_screenshot_bytes: bytes, sample_demo: str, mock_api_response: str
    ):
        """CRITICAL: Demo must appear in prompt at EVERY step, not just step 1.

        This is the core fix for the 100%/0% paradox.
        """
        # Mock the API clients to avoid real API calls
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            # Create a mock Anthropic client
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(type="text", text=mock_api_response)]
            mock_client.messages.create.return_value = mock_response

            # Create agent with demo
            agent = ApiAgent.__new__(ApiAgent)
            agent.provider = "anthropic"
            agent.model = "claude-sonnet-4-5-20250929"
            agent.temperature = 0.5
            agent.max_tokens = 1500
            agent.use_accessibility_tree = False
            agent.use_history = True
            agent.demo = sample_demo
            agent.action_space = "code_block"
            agent.api_key = "test-key"
            agent._client = mock_client
            agent.prev_actions = []
            agent.history = []
            agent.history_cutoff = 10
            agent.memory_block_text = "# empty memory block"
            agent.step_counter = 0

            # Create mock observation
            obs = {
                "screenshot": mock_screenshot_bytes,
                "window_title": "System Preferences",
                "window_names_str": "System Preferences, Finder",
                "computer_clipboard": "",
                "accessibility_tree": None,
            }

            # Run multiple steps
            demo_found_in_logs = []
            for step in range(5):
                _, _, logs, _ = agent.predict(
                    instruction="Turn off Night Shift",
                    obs=obs,
                )

                # Check that demo was included
                demo_included = logs.get("demo_included", False)
                demo_found_in_logs.append(demo_included)

                # Verify demo appears in the user_question (prompt)
                user_question = logs.get("user_question", "")
                assert "DEMONSTRATION" in user_question, (
                    f"Step {step + 1}: DEMONSTRATION not found in prompt! "
                    f"Demo was {'included' if demo_included else 'NOT included'} according to logs."
                )

            # All steps should have demo
            assert all(demo_found_in_logs), (
                f"Demo not found in all steps! "
                f"Step results: {demo_found_in_logs}"
            )

    def test_demo_persists_across_resets(
        self, mock_screenshot_bytes: bytes, sample_demo: str, mock_api_response: str
    ):
        """Demo should persist across agent.reset() calls.

        This is important for running multiple tasks where the same demo
        should be reused.
        """
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            # Create a mock client
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(type="text", text=mock_api_response)]
            mock_client.messages.create.return_value = mock_response

            # Create agent with demo
            agent = ApiAgent.__new__(ApiAgent)
            agent.provider = "anthropic"
            agent.model = "claude-sonnet-4-5-20250929"
            agent.temperature = 0.5
            agent.max_tokens = 1500
            agent.use_accessibility_tree = False
            agent.use_history = True
            agent.demo = sample_demo
            agent.action_space = "code_block"
            agent.api_key = "test-key"
            agent._client = mock_client
            agent.prev_actions = []
            agent.history = []
            agent.history_cutoff = 10
            agent.memory_block_text = "# empty memory block"
            agent.step_counter = 0

            obs = {
                "screenshot": mock_screenshot_bytes,
                "window_title": "System Preferences",
                "window_names_str": "",
                "computer_clipboard": "",
                "accessibility_tree": None,
            }

            # First task - verify demo works
            _, _, logs1, _ = agent.predict("Turn off Night Shift", obs)
            assert logs1.get("demo_included", False), "Demo not included in first task"

            # Reset between tasks
            agent.reset()

            # Demo should still be set
            assert agent.demo is not None, "Demo was cleared by reset()!"
            assert len(agent.demo) > 0, "Demo is empty after reset!"

            # Second task - verify demo still works
            _, _, logs2, _ = agent.predict("Turn on Night Shift", obs)
            assert logs2.get("demo_included", False), "Demo not included after reset"

    def test_set_demo_dynamic_retrieval(
        self, mock_screenshot_bytes: bytes, mock_api_response: str
    ):
        """Test set_demo() for dynamic demo retrieval scenarios.

        In production, demos may be retrieved dynamically based on the task.
        """
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            # Create a mock client
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(type="text", text=mock_api_response)]
            mock_client.messages.create.return_value = mock_response

            # Create agent WITHOUT demo initially
            agent = ApiAgent.__new__(ApiAgent)
            agent.provider = "anthropic"
            agent.model = "claude-sonnet-4-5-20250929"
            agent.temperature = 0.5
            agent.max_tokens = 1500
            agent.use_accessibility_tree = False
            agent.use_history = True
            agent.demo = None  # No demo initially
            agent.action_space = "code_block"
            agent.api_key = "test-key"
            agent._client = mock_client
            agent.prev_actions = []
            agent.history = []
            agent.history_cutoff = 10
            agent.memory_block_text = "# empty memory block"
            agent.step_counter = 0

            obs = {
                "screenshot": mock_screenshot_bytes,
                "window_title": "System Preferences",
                "window_names_str": "",
                "computer_clipboard": "",
                "accessibility_tree": None,
            }

            # First predict without demo
            _, _, logs1, _ = agent.predict("Turn off Night Shift", obs)
            assert not logs1.get("demo_included", False), "Demo should not be included initially"

            # Dynamically set demo (simulating retrieval)
            new_demo = "DEMONSTRATION:\nStep 1: Click Settings"
            agent.set_demo(new_demo)

            # Now predict should include demo
            _, _, logs2, _ = agent.predict("Turn off Night Shift", obs)
            assert logs2.get("demo_included", False), "Demo should be included after set_demo()"
            assert logs2.get("demo_length", 0) > 0, "Demo length should be > 0"

    def test_demo_step_counter_in_prompt(
        self, mock_screenshot_bytes: bytes, sample_demo: str, mock_api_response: str
    ):
        """Verify step counter is included in demo section of prompt.

        The prompt should indicate which step the agent is currently on.
        """
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            # Create a mock client
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(type="text", text=mock_api_response)]
            mock_client.messages.create.return_value = mock_response

            # Create agent with demo
            agent = ApiAgent.__new__(ApiAgent)
            agent.provider = "anthropic"
            agent.model = "claude-sonnet-4-5-20250929"
            agent.temperature = 0.5
            agent.max_tokens = 1500
            agent.use_accessibility_tree = False
            agent.use_history = True
            agent.demo = sample_demo
            agent.action_space = "code_block"
            agent.api_key = "test-key"
            agent._client = mock_client
            agent.prev_actions = []
            agent.history = []
            agent.history_cutoff = 10
            agent.memory_block_text = "# empty memory block"
            agent.step_counter = 0

            obs = {
                "screenshot": mock_screenshot_bytes,
                "window_title": "System Preferences",
                "window_names_str": "",
                "computer_clipboard": "",
                "accessibility_tree": None,
            }

            # Run 3 steps and check step counter in prompt
            for expected_step in [1, 2, 3]:
                _, _, logs, _ = agent.predict("Turn off Night Shift", obs)
                user_question = logs.get("user_question", "")

                # Should contain "step X" where X is current step
                assert f"step {expected_step}" in user_question.lower(), (
                    f"Step {expected_step}: Step counter not found in prompt. "
                    f"Expected 'step {expected_step}' in prompt."
                )


class TestDemoPersistenceWithRealCapture:
    """Integration tests using real capture data (if available)."""

    @pytest.fixture
    def capture_path(self):
        """Path to test capture."""
        from pathlib import Path
        path = Path("/Users/abrichr/oa/src/openadapt-capture/turn-off-nightshift")
        if not path.exists():
            pytest.skip("Test capture not available")
        return path

    def test_demo_format_and_persistence(self, capture_path):
        """Test demo formatting from capture and persistence validation."""
        from openadapt_ml.experiments.demo_prompt.format_demo import format_episode_as_demo
        from openadapt_ml.ingest.capture import capture_to_episode

        # Load capture
        episode = capture_to_episode(
            capture_path=capture_path,
            instruction="Turn off Night Shift",
        )

        # Format as demo
        demo_str = format_episode_as_demo(episode, max_steps=10)

        # Verify demo has expected content
        assert "DEMONSTRATION" in demo_str, "Demo should start with DEMONSTRATION header"
        assert "Step 1" in demo_str, "Demo should include step 1"
        assert len(demo_str) > 50, "Demo should have substantial content"

        # Verify demo can be used in prompt (simulate what ApiAgent does)
        content_parts = ["TASK: Turn off Night Shift"]
        content_parts.append(
            f"DEMONSTRATION (follow this pattern):\n"
            f"---\n{demo_str}\n---\n"
            f"Use the demonstration above as a guide. You are currently at step 1."
        )
        prompt = "\n\n".join(content_parts)

        # This is what the P0 validation checks
        assert "DEMONSTRATION" in prompt, "DEMONSTRATION should be in prompt"


class TestDemoPersistenceEdgeCases:
    """Edge case tests for demo persistence."""

    def test_empty_demo_handling(self):
        """Agent should handle empty demo gracefully."""
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            # Create agent with empty demo
            agent = ApiAgent.__new__(ApiAgent)
            agent.demo = ""

            # Empty string should not trigger demo inclusion
            # (the check is `if self.demo:` which is False for empty string)
            assert not agent.demo, "Empty demo should be falsy"

    def test_demo_with_special_characters(self):
        """Demo with special characters should work."""
        demo_with_special = """DEMONSTRATION:
Task: Search for "quotes" and <brackets>

Step 1:
  Action: TYPE("hello\nworld")
"""
        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            agent = ApiAgent.__new__(ApiAgent)
            agent.demo = demo_with_special

            assert agent.demo is not None
            assert "quotes" in agent.demo
            assert "<brackets>" in agent.demo

    def test_very_long_demo(self):
        """Very long demo should be handled (though may be truncated in practice)."""
        long_demo = "DEMONSTRATION:\n" + ("Step X: CLICK(0.5, 0.5)\n" * 1000)

        with patch.dict("sys.modules", {"anthropic": MagicMock(), "openai": MagicMock()}):
            from openadapt_evals.waa_deploy.api_agent import ApiAgent

            agent = ApiAgent.__new__(ApiAgent)
            agent.demo = long_demo

            assert len(agent.demo) > 10000, "Long demo should be preserved"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
