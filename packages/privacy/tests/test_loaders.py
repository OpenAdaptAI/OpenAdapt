"""Tests for data loaders."""

import json
import tempfile
from pathlib import Path

import pytest

from openadapt_privacy.base import ScrubbingProvider
from openadapt_privacy.loaders import (
    Action,
    DictRecordingLoader,
    Recording,
    Screenshot,
)


class MockScrubbingProvider(ScrubbingProvider):
    """Mock scrubbing provider for testing."""

    name: str = "mock"
    capabilities: list[str] = ["TEXT"]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Replace 'secret' with '<REDACTED>'."""
        if text is None:
            return None
        return text.replace("secret", "<REDACTED>").replace("john@example.com", "<EMAIL>")

    def scrub_dict(self, input_dict: dict) -> dict:
        """Simple dict scrubbing for tests."""
        result = {}
        for k, v in input_dict.items():
            if isinstance(v, str):
                result[k] = self.scrub_text(v)
            elif isinstance(v, dict):
                result[k] = self.scrub_dict(v)
            else:
                result[k] = v
        return result


class TestAction:
    """Tests for Action dataclass."""

    def test_action_creation(self) -> None:
        """Test creating an Action."""
        action = Action(
            id=1,
            action_type="click",
            timestamp=1234567890.0,
            text="Click here",
        )

        assert action.id == 1
        assert action.action_type == "click"
        assert action.timestamp == 1234567890.0
        assert action.text == "Click here"

    def test_action_scrub(self) -> None:
        """Test scrubbing an Action."""
        action = Action(
            id=1,
            action_type="type",
            timestamp=1234567890.0,
            text="secret password",
            window_title="Login - secret app",
        )

        scrubber = MockScrubbingProvider()
        scrubbed = action.scrub(scrubber)

        assert scrubbed.id == 1
        assert scrubbed.action_type == "type"
        assert scrubbed.text == "<REDACTED> password"
        assert scrubbed.window_title == "Login - <REDACTED> app"

    def test_action_scrub_none_fields(self) -> None:
        """Test scrubbing Action with None fields."""
        action = Action(
            id=1,
            action_type="click",
            timestamp=1234567890.0,
        )

        scrubber = MockScrubbingProvider()
        scrubbed = action.scrub(scrubber)

        assert scrubbed.text is None
        assert scrubbed.window_title is None


class TestScreenshot:
    """Tests for Screenshot dataclass."""

    def test_screenshot_creation(self) -> None:
        """Test creating a Screenshot."""
        screenshot = Screenshot(
            id=1,
            action_id=1,
            timestamp=1234567890.0,
            path="/path/to/screenshot.png",
        )

        assert screenshot.id == 1
        assert screenshot.action_id == 1
        assert screenshot.path == "/path/to/screenshot.png"

    def test_screenshot_scrub_no_image(self) -> None:
        """Test scrubbing Screenshot without image."""
        screenshot = Screenshot(
            id=1,
            action_id=1,
            timestamp=1234567890.0,
        )

        scrubber = MockScrubbingProvider()
        scrubbed = screenshot.scrub(scrubber)

        assert scrubbed.image is None


class TestRecording:
    """Tests for Recording dataclass."""

    def test_recording_creation(self) -> None:
        """Test creating a Recording."""
        recording = Recording(
            id=1,
            task_description="Test task",
            timestamp=1234567890.0,
            actions=[
                Action(id=1, action_type="click", timestamp=1234567891.0),
            ],
        )

        assert recording.id == 1
        assert recording.task_description == "Test task"
        assert len(recording.actions) == 1

    def test_recording_scrub(self) -> None:
        """Test scrubbing a Recording."""
        recording = Recording(
            id=1,
            task_description="Send email to john@example.com",
            timestamp=1234567890.0,
            actions=[
                Action(
                    id=1,
                    action_type="type",
                    timestamp=1234567891.0,
                    text="secret message",
                ),
            ],
        )

        scrubber = MockScrubbingProvider()
        scrubbed = recording.scrub(scrubber, scrub_images=False)

        assert scrubbed.task_description == "Send email to <EMAIL>"
        assert scrubbed.actions[0].text == "<REDACTED> message"

    def test_iter_actions_with_screenshots(self) -> None:
        """Test iterating over actions with screenshots."""
        recording = Recording(
            actions=[
                Action(id=1, action_type="click", timestamp=1.0),
                Action(id=2, action_type="type", timestamp=2.0),
            ],
            screenshots=[
                Screenshot(id=1, action_id=1, timestamp=1.0),
            ],
        )

        pairs = list(recording.iter_actions_with_screenshots())

        assert len(pairs) == 2
        assert pairs[0][0].id == 1
        assert pairs[0][1] is not None
        assert pairs[0][1].id == 1
        assert pairs[1][0].id == 2
        assert pairs[1][1] is None


class TestDictRecordingLoader:
    """Tests for DictRecordingLoader."""

    def test_load_from_dict(self) -> None:
        """Test loading from dict."""
        loader = DictRecordingLoader()
        data = {
            "id": 1,
            "task_description": "Test task",
            "timestamp": 1234567890.0,
            "actions": [
                {
                    "id": 1,
                    "action_type": "click",
                    "timestamp": 1234567891.0,
                    "text": "Click me",
                }
            ],
        }

        recording = loader.load_from_dict(data)

        assert recording.id == 1
        assert recording.task_description == "Test task"
        assert len(recording.actions) == 1
        assert recording.actions[0].text == "Click me"

    def test_to_dict(self) -> None:
        """Test converting to dict."""
        loader = DictRecordingLoader()
        recording = Recording(
            id=1,
            task_description="Test task",
            timestamp=1234567890.0,
            actions=[
                Action(id=1, action_type="click", timestamp=1234567891.0, text="Click"),
            ],
        )

        data = loader.to_dict(recording)

        assert data["id"] == 1
        assert data["task_description"] == "Test task"
        assert len(data["actions"]) == 1
        assert data["actions"][0]["text"] == "Click"

    def test_load_save_json_file(self) -> None:
        """Test loading and saving JSON files."""
        loader = DictRecordingLoader()
        original = Recording(
            id=1,
            task_description="Test task",
            actions=[
                Action(id=1, action_type="click", timestamp=1.0, text="Test"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "recording.json"

            # Save
            loader.save(original, str(path))

            # Verify file exists and is valid JSON
            assert path.exists()
            with open(path) as f:
                data = json.load(f)
            assert data["task_description"] == "Test task"

            # Load
            loaded = loader.load(str(path))
            assert loaded.task_description == "Test task"
            assert len(loaded.actions) == 1

    def test_load_and_scrub(self) -> None:
        """Test load_and_scrub convenience method."""
        loader = DictRecordingLoader()
        scrubber = MockScrubbingProvider()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "recording.json"

            # Create test file
            data = {
                "task_description": "Send secret message",
                "actions": [
                    {"id": 1, "action_type": "type", "timestamp": 1.0, "text": "secret"},
                ],
            }
            with open(path, "w") as f:
                json.dump(data, f)

            # Load and scrub
            scrubbed = loader.load_and_scrub(str(path), scrubber, scrub_images=False)

            assert scrubbed.task_description == "Send <REDACTED> message"
            assert scrubbed.actions[0].text == "<REDACTED>"
