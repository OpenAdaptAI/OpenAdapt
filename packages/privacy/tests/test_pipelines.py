"""Tests for scrubbing pipelines."""

import pytest

from openadapt_privacy.base import ScrubbingProvider
from openadapt_privacy.pipelines.dicts import DictScrubber, scrub_dict, scrub_list_dicts


class MockScrubbingProvider(ScrubbingProvider):
    """Mock scrubbing provider for testing."""

    name: str = "mock"
    capabilities: list[str] = ["TEXT"]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Replace 'secret' with '<REDACTED>'."""
        if text is None:
            return None
        return text.replace("secret", "<REDACTED>")


class TestDictScrubber:
    """Tests for DictScrubber class."""

    def test_scrub_text_delegates(self) -> None:
        """Test that scrub_text delegates to provider."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        result = scrubber.scrub_text("my secret password")
        assert result == "my <REDACTED> password"

    def test_scrub_dict_basic(self) -> None:
        """Test basic dict scrubbing."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {"text": "my secret"}
        result = scrubber.scrub_dict(input_dict)

        assert result["text"] == "my <REDACTED>"

    def test_scrub_dict_nested(self) -> None:
        """Test nested dict scrubbing."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {
            "text": "secret data",
            "metadata": {
                "title": "secret title",
                "count": 42,
            },
        }
        result = scrubber.scrub_dict(input_dict)

        assert result["text"] == "<REDACTED> data"
        assert result["metadata"]["title"] == "<REDACTED> title"
        assert result["metadata"]["count"] == 42

    def test_scrub_dict_with_list(self) -> None:
        """Test dict with list values."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {
            "children": ["secret item 1", "secret item 2"],
        }
        result = scrubber.scrub_dict(input_dict)

        assert result["children"] == ["<REDACTED> item 1", "<REDACTED> item 2"]

    def test_scrub_dict_preserves_non_string_values(self) -> None:
        """Test that non-string values are preserved."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {
            "text": "secret",
            "count": 42,
            "enabled": True,
            "ratio": 3.14,
            "items": [1, 2, 3],
        }
        result = scrubber.scrub_dict(input_dict)

        assert result["text"] == "<REDACTED>"
        assert result["count"] == 42
        assert result["enabled"] is True
        assert result["ratio"] == 3.14
        assert result["items"] == [1, 2, 3]

    def test_scrub_dict_custom_keys(self) -> None:
        """Test scrubbing with custom key list."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {
            "text": "secret",  # Not in custom list, won't be scrubbed
            "custom_field": "secret",  # In custom list, will be scrubbed
        }
        result = scrubber.scrub_dict(input_dict, list_keys=["custom_field"])

        assert result["text"] == "secret"  # Not scrubbed
        assert result["custom_field"] == "<REDACTED>"

    def test_scrub_dict_scrub_all(self) -> None:
        """Test scrub_all flag."""
        provider = MockScrubbingProvider()
        scrubber = DictScrubber(provider)

        input_dict = {
            "any_key": "secret",
            "another": "secret data",
        }
        result = scrubber.scrub_dict(input_dict, scrub_all=True)

        assert result["any_key"] == "<REDACTED>"
        assert result["another"] == "<REDACTED> data"


class TestScrubDictFunction:
    """Tests for scrub_dict convenience function."""

    def test_scrub_dict_function(self) -> None:
        """Test the convenience function."""
        provider = MockScrubbingProvider()

        input_dict = {"text": "secret data"}
        result = scrub_dict(input_dict, provider)

        assert result["text"] == "<REDACTED> data"

    def test_scrub_dict_function_with_options(self) -> None:
        """Test convenience function with options."""
        provider = MockScrubbingProvider()

        input_dict = {"custom": "secret"}
        result = scrub_dict(input_dict, provider, list_keys=["custom"])

        assert result["custom"] == "<REDACTED>"


class TestScrubListDictsFunction:
    """Tests for scrub_list_dicts convenience function."""

    def test_scrub_list_dicts(self) -> None:
        """Test scrubbing a list of dicts."""
        provider = MockScrubbingProvider()

        input_list = [
            {"text": "secret 1"},
            {"text": "secret 2"},
        ]
        result = scrub_list_dicts(input_list, provider)

        assert len(result) == 2
        assert result[0]["text"] == "<REDACTED> 1"
        assert result[1]["text"] == "<REDACTED> 2"

    def test_scrub_empty_list(self) -> None:
        """Test scrubbing empty list."""
        provider = MockScrubbingProvider()

        result = scrub_list_dicts([], provider)

        assert result == []
