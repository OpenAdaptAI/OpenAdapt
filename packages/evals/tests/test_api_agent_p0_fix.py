"""Tests for the P0 demo persistence fix in ApiAgent.

The P0 fix ensures that the demo is included at EVERY step, not just step 1.
This is critical for multi-step task completion.

See api_agent.py lines 318-330 for the implementation.
"""

from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from PIL import Image


def create_test_screenshot():
    """Create a minimal test PNG screenshot."""
    img = Image.new('RGB', (100, 100), color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()


def create_mock_response():
    """Create a mock API response."""
    mock_response = Mock()
    mock_content = Mock()
    mock_content.type = "text"
    mock_content.text = """```memory
# Working on task
```

```decision
CONTINUE
```

```python
computer.click(500, 300)
```"""
    mock_response.content = [mock_content]
    return mock_response


class TestDemoPersistence:
    """Tests for the P0 demo persistence fix."""

    @pytest.fixture
    def demo_text(self):
        """Sample demo trajectory text."""
        return """Step 1: Open Start menu by clicking Windows button
Step 2: Type 'notepad' in search
Step 3: Click Notepad app
Step 4: Type test text
Step 5: Save file"""

    def test_demo_included_at_step_1(self, demo_text):
        """Test that demo is included on the first step."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo_text
            )

            # Call predict for step 1
            obs = {"screenshot": create_test_screenshot()}
            _, _, logs, _ = agent.predict("Open Notepad", obs)

            # Verify demo was included
            assert logs.get("demo_included") is True
            assert logs.get("demo_length") == len(demo_text)
            assert "DEMONSTRATION" in logs.get("user_question", "")
            assert "Step 1: Open Start menu" in logs.get("user_question", "")

    def test_demo_included_at_step_2(self, demo_text):
        """Test that demo is STILL included on step 2 (P0 fix)."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo_text
            )

            obs = {"screenshot": create_test_screenshot()}

            # Step 1
            agent.predict("Open Notepad", obs)

            # Step 2 - this is where the bug would manifest without the P0 fix
            _, _, logs, _ = agent.predict("Open Notepad", obs)

            # CRITICAL: Demo must still be included!
            assert logs.get("demo_included") is True, \
                "P0 FIX FAILED: Demo was not included at step 2!"
            assert "DEMONSTRATION" in logs.get("user_question", ""), \
                "P0 FIX FAILED: DEMONSTRATION section missing at step 2!"

    def test_demo_included_at_step_5(self, demo_text):
        """Test that demo persists across many steps (P0 fix)."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo_text
            )

            obs = {"screenshot": create_test_screenshot()}

            # Run 5 steps
            for step in range(1, 6):
                _, _, logs, _ = agent.predict("Open Notepad", obs)

                # Demo must be included at EVERY step
                assert logs.get("demo_included") is True, \
                    f"P0 FIX FAILED: Demo not included at step {step}!"
                assert f"step {step}" in logs.get("user_question", "").lower(), \
                    f"Step counter not properly updated at step {step}"

    def test_step_counter_increments(self, demo_text):
        """Test that step counter is properly included in demo context."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo_text
            )

            obs = {"screenshot": create_test_screenshot()}

            # Step 1
            _, _, logs1, _ = agent.predict("Open Notepad", obs)
            assert "currently at step 1" in logs1.get("user_question", "")

            # Step 2
            _, _, logs2, _ = agent.predict("Open Notepad", obs)
            assert "currently at step 2" in logs2.get("user_question", "")

            # Step 3
            _, _, logs3, _ = agent.predict("Open Notepad", obs)
            assert "currently at step 3" in logs3.get("user_question", "")

    def test_no_demo_means_no_demo_in_prompt(self):
        """Test that without demo, no demo section appears."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=None  # No demo
            )

            obs = {"screenshot": create_test_screenshot()}
            _, _, logs, _ = agent.predict("Open Notepad", obs)

            # Demo should not be included
            assert logs.get("demo_included") is None or logs.get("demo_included") is False
            assert "DEMONSTRATION" not in logs.get("user_question", "")

    def test_reset_does_not_clear_demo(self, demo_text):
        """Test that reset() preserves the demo (it's a constructor parameter)."""
        mock_client = Mock()
        mock_client.messages.create.return_value = create_mock_response()

        with patch('anthropic.Anthropic', return_value=mock_client):
            from openadapt_evals.agents.api_agent import ApiAgent

            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo_text
            )

            obs = {"screenshot": create_test_screenshot()}

            # Step 1
            agent.predict("Open Notepad", obs)

            # Reset (new task)
            agent.reset()

            # After reset, demo should still be present
            _, _, logs, _ = agent.predict("Different task", obs)

            assert logs.get("demo_included") is True, \
                "Demo was lost after reset!"
            assert "DEMONSTRATION" in logs.get("user_question", "")


class TestDemoIntegration:
    """Integration tests for demo-conditioned evaluation."""

    def test_demo_improves_multi_step_task(self):
        """Verify the P0 fix concept: demo at every step enables multi-step success."""
        # This test validates the architecture, not actual API responses

        with patch('anthropic.Anthropic') as mock_anthropic:
            # Track how many times demo appears in API calls
            demo_appearances = []

            def track_demo_call(*args, **kwargs):
                messages = kwargs.get('messages', [])
                for msg in messages:
                    content = msg.get('content', [])
                    for item in content if isinstance(content, list) else []:
                        if isinstance(item, dict) and 'text' in item:
                            if 'DEMONSTRATION' in item['text']:
                                demo_appearances.append(True)

                # Return mock response with proper type attribute
                mock_response = Mock()
                mock_content = Mock()
                mock_content.type = "text"
                mock_content.text = """```memory
# task in progress
```

```decision
CONTINUE
```

```python
computer.click(100, 100)
```"""
                mock_response.content = [mock_content]
                return mock_response

            mock_client = Mock()
            mock_client.messages.create.side_effect = track_demo_call
            mock_anthropic.return_value = mock_client

            from openadapt_evals.agents.api_agent import ApiAgent

            demo = "Step 1: Click button\nStep 2: Type text\nStep 3: Submit"
            agent = ApiAgent(
                provider="anthropic",
                api_key="test-key",
                demo=demo
            )

            obs = {"screenshot": create_test_screenshot()}

            # Simulate 5 steps
            for _ in range(5):
                agent.predict("Complete the form", obs)

            # P0 FIX VALIDATION: Demo should appear in ALL 5 API calls
            assert len(demo_appearances) == 5, \
                f"P0 FIX VALIDATION: Demo appeared in {len(demo_appearances)} calls, expected 5"
