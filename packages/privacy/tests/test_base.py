"""Tests for base classes."""

import pytest

from openadapt_privacy.base import (
    Modality,
    ScrubbingProvider,
    ScrubbingProviderFactory,
    TextScrubbingMixin,
)
from openadapt_privacy.config import config


class TestModality:
    """Tests for Modality class."""

    def test_modality_constants(self) -> None:
        """Test that modality constants are defined."""
        assert Modality.TEXT == "TEXT"
        assert Modality.PIL_IMAGE == "PIL_IMAGE"
        assert Modality.PDF == "PDF"
        assert Modality.MP4 == "MP4"


class TestTextScrubbingMixin:
    """Tests for TextScrubbingMixin."""

    def test_scrub_text_all(self) -> None:
        """Test scrub_text_all replaces all characters."""

        class MockMixin(TextScrubbingMixin):
            def scrub_text(self, text: str, is_separated: bool = False) -> str:
                return text

        mixin = MockMixin()

        # Test normal text
        result = mixin.scrub_text_all("Hello World")
        assert result == config.SCRUB_CHAR * 11

        # Test empty string
        result = mixin.scrub_text_all("")
        assert result == ""

        # Test None
        result = mixin.scrub_text_all(None)
        assert result is None

    def test_should_scrub_text(self) -> None:
        """Test _should_scrub_text logic."""

        class MockMixin(TextScrubbingMixin):
            def scrub_text(self, text: str, is_separated: bool = False) -> str:
                return text

        mixin = MockMixin()

        # Should scrub when key is in list and value is string
        assert mixin._should_scrub_text("text", "hello", ["text", "title"], False)
        assert mixin._should_scrub_text("title", "hello", ["text", "title"], False)

        # Should not scrub when key is not in list (unless scrub_all)
        assert not mixin._should_scrub_text("other", "hello", ["text", "title"], False)

        # Should scrub when scrub_all is True
        assert mixin._should_scrub_text("other", "hello", ["text"], True)

        # Should not scrub non-string values
        assert not mixin._should_scrub_text("text", 123, ["text"], False)
        assert not mixin._should_scrub_text("text", ["list"], ["text"], False)

    def test_is_scrubbed(self) -> None:
        """Test _is_scrubbed comparison."""

        class MockMixin(TextScrubbingMixin):
            def scrub_text(self, text: str, is_separated: bool = False) -> str:
                return text

        mixin = MockMixin()

        assert mixin._is_scrubbed("original", "changed")
        assert not mixin._is_scrubbed("same", "same")


class TestScrubbingProvider:
    """Tests for ScrubbingProvider base class."""

    def test_scrub_text_not_implemented(self) -> None:
        """Test that scrub_text raises NotImplementedError."""

        class MockProvider(ScrubbingProvider):
            name: str = "mock"
            capabilities: list[str] = []

        provider = MockProvider()
        with pytest.raises(NotImplementedError):
            provider.scrub_text("test")

    def test_scrub_image_not_implemented(self) -> None:
        """Test that scrub_image raises NotImplementedError."""

        class MockProvider(ScrubbingProvider):
            name: str = "mock"
            capabilities: list[str] = []

        provider = MockProvider()
        with pytest.raises(NotImplementedError):
            provider.scrub_image(None)

    def test_scrub_pdf_not_implemented(self) -> None:
        """Test that scrub_pdf raises NotImplementedError."""

        class MockProvider(ScrubbingProvider):
            name: str = "mock"
            capabilities: list[str] = []

        provider = MockProvider()
        with pytest.raises(NotImplementedError):
            provider.scrub_pdf("/path/to/pdf")

    def test_scrub_mp4_not_implemented(self) -> None:
        """Test that scrub_mp4 raises NotImplementedError."""

        class MockProvider(ScrubbingProvider):
            name: str = "mock"
            capabilities: list[str] = []

        provider = MockProvider()
        with pytest.raises(NotImplementedError):
            provider.scrub_mp4("/path/to/mp4")


class TestScrubbingProviderFactory:
    """Tests for ScrubbingProviderFactory."""

    def test_get_for_modality_returns_list(self) -> None:
        """Test that get_for_modality returns a list."""
        # Without any registered providers, should return empty list
        result = ScrubbingProviderFactory.get_for_modality(Modality.TEXT)
        assert isinstance(result, list)
