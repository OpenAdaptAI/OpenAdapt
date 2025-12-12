"""Tests using synthetic fixtures."""

from typing import ClassVar

import pytest

from openadapt_privacy.base import ScrubbingProvider
from openadapt_privacy.loaders import DictRecordingLoader
from tests.fixtures import (
    SYNTHETIC_DICTS,
    SYNTHETIC_RECORDING,
    SYNTHETIC_TEXT,
    get_dict_examples,
    get_recording_example,
    get_text_examples,
)

# Patterns to replace (simulating Presidio behavior)
_MOCK_PATTERNS = {
    "john.smith@example.com": "<EMAIL_ADDRESS>",
    "john@example.com": "<EMAIL_ADDRESS>",
    "555-123-4567": "<PHONE_NUMBER>",
    "923-45-6789": "<US_SSN>",
    "4532-1234-5678-9012": "<CREDIT_CARD>",
    "John Smith": "<PERSON>",
    "01/15/1985": "<DATE_TIME>",
}


class MockScrubbingProvider(ScrubbingProvider):
    """Mock provider that replaces known PII patterns."""

    name: str = "mock"
    capabilities: list[str] = ["TEXT"]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        if text is None:
            return None
        result = text
        for pattern, replacement in _MOCK_PATTERNS.items():
            result = result.replace(pattern, replacement)
        return result

    def scrub_dict(self, input_dict: dict) -> dict:
        result = {}
        for k, v in input_dict.items():
            if isinstance(v, str):
                result[k] = self.scrub_text(v)
            elif isinstance(v, dict):
                result[k] = self.scrub_dict(v)
            elif isinstance(v, list):
                result[k] = [
                    self.scrub_dict(item) if isinstance(item, dict)
                    else self.scrub_text(item) if isinstance(item, str)
                    else item
                    for item in v
                ]
            else:
                result[k] = v
        return result


class TestSyntheticTextExamples:
    """Tests using synthetic text examples."""

    def test_get_text_examples_returns_copy(self) -> None:
        """Test that get_text_examples returns a copy."""
        examples1 = get_text_examples()
        examples2 = get_text_examples()
        assert examples1 is not examples2

    def test_synthetic_text_structure(self) -> None:
        """Test that synthetic text has expected structure."""
        examples = get_text_examples()

        for name, data in examples.items():
            assert "input" in data, f"Missing 'input' in {name}"
            assert "entities" in data, f"Missing 'entities' in {name}"
            assert isinstance(data["input"], str)
            assert isinstance(data["entities"], list)

    def test_scrub_synthetic_email(self) -> None:
        """Test scrubbing email example."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_TEXT["email"]

        result = scrubber.scrub_text(example["input"])

        assert "john.smith@example.com" not in result
        assert "<EMAIL_ADDRESS>" in result

    def test_scrub_synthetic_phone(self) -> None:
        """Test scrubbing phone example."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_TEXT["phone"]

        result = scrubber.scrub_text(example["input"])

        assert "555-123-4567" not in result
        assert "<PHONE_NUMBER>" in result

    def test_scrub_synthetic_mixed(self) -> None:
        """Test scrubbing mixed PII example."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_TEXT["mixed"]

        result = scrubber.scrub_text(example["input"])

        # All PII should be replaced
        assert "john.smith@example.com" not in result
        assert "555-123-4567" not in result
        assert "923-45-6789" not in result
        assert "4532-1234-5678-9012" not in result

    def test_no_pii_unchanged(self) -> None:
        """Test that text without PII is unchanged."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_TEXT["no_pii"]

        result = scrubber.scrub_text(example["input"])

        assert result == example["input"]


class TestSyntheticDictExamples:
    """Tests using synthetic dict examples."""

    def test_get_dict_examples_returns_copy(self) -> None:
        """Test that get_dict_examples returns a copy."""
        examples1 = get_dict_examples()
        examples2 = get_dict_examples()
        assert examples1 is not examples2

    def test_synthetic_dict_structure(self) -> None:
        """Test that synthetic dicts have expected structure."""
        examples = get_dict_examples()

        for name, data in examples.items():
            assert "input" in data, f"Missing 'input' in {name}"
            assert "scrubbed_keys" in data, f"Missing 'scrubbed_keys' in {name}"
            assert isinstance(data["input"], dict)
            assert isinstance(data["scrubbed_keys"], list)

    def test_scrub_simple_action(self) -> None:
        """Test scrubbing simple action dict."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_DICTS["simple_action"]

        result = scrubber.scrub_dict(example["input"])

        # Text should be scrubbed
        assert "john@example.com" not in result["text"]
        # Non-scrubbed fields preserved
        assert result["timestamp"] == example["input"]["timestamp"]
        assert result["action_type"] == example["input"]["action_type"]

    def test_scrub_nested_action(self) -> None:
        """Test scrubbing nested action dict."""
        scrubber = MockScrubbingProvider()
        example = SYNTHETIC_DICTS["nested_action"]

        result = scrubber.scrub_dict(example["input"])

        # Nested text should be scrubbed
        assert "John Smith" not in result["text"]
        assert "John Smith" not in result["metadata"]["title"]
        assert "john@example.com" not in result["metadata"]["tooltip"]
        # Coordinates preserved
        assert result["coordinates"] == example["input"]["coordinates"]


class TestSyntheticRecordingExample:
    """Tests using synthetic recording example."""

    def test_get_recording_example_returns_deep_copy(self) -> None:
        """Test that get_recording_example returns a deep copy."""
        rec1 = get_recording_example()
        rec2 = get_recording_example()

        # Different objects
        assert rec1 is not rec2
        assert rec1["actions"] is not rec2["actions"]

        # Modify one, other unchanged
        rec1["task_description"] = "modified"
        assert rec2["task_description"] == SYNTHETIC_RECORDING["task_description"]

    def test_load_synthetic_recording(self) -> None:
        """Test loading synthetic recording with DictRecordingLoader."""
        loader = DictRecordingLoader()
        data = get_recording_example()

        recording = loader.load_from_dict(data)

        assert recording.task_description == data["task_description"]
        assert len(recording.actions) == len(data["actions"])
        assert len(recording.screenshots) == len(data["screenshots"])

    def test_scrub_synthetic_recording(self) -> None:
        """Test scrubbing synthetic recording."""
        loader = DictRecordingLoader()
        scrubber = MockScrubbingProvider()
        data = get_recording_example()

        recording = loader.load_from_dict(data)
        scrubbed = recording.scrub(scrubber, scrub_images=False)

        # Task description scrubbed
        assert "John Smith" not in scrubbed.task_description
        assert "john@example.com" not in scrubbed.task_description

        # Actions scrubbed
        for action in scrubbed.actions:
            if action.text:
                assert "john@example.com" not in action.text
            if action.window_title:
                assert "john@example.com" not in action.window_title

    def test_round_trip_synthetic_recording(self) -> None:
        """Test loading, scrubbing, and converting back to dict."""
        loader = DictRecordingLoader()
        scrubber = MockScrubbingProvider()
        data = get_recording_example()

        # Load -> scrub -> to_dict
        recording = loader.load_from_dict(data)
        scrubbed = recording.scrub(scrubber, scrub_images=False)
        result = loader.to_dict(scrubbed)

        # Structure preserved
        assert "task_description" in result
        assert "actions" in result
        assert len(result["actions"]) == len(data["actions"])

        # Content scrubbed
        assert "john@example.com" not in result["task_description"]
