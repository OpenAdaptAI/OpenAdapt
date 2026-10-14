"""Tests for ScrubMiddleware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.adapters.scrub_middleware import ScrubMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"
SCRUBBED_PNG = b"\x89PNG\r\n\x1a\nSCRUBBED"


class DummyAdapter(BenchmarkAdapter):
    """Minimal adapter for testing the middleware wrapper."""

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def benchmark_type(self) -> str:
        return "interactive"

    def list_tasks(self, domain=None):
        return []

    def load_task(self, task_id):
        raise KeyError(task_id)

    def reset(self, task):
        return BenchmarkObservation(screenshot=FAKE_PNG, viewport=(1920, 1080))

    def step(self, action):
        return BenchmarkObservation(screenshot=FAKE_PNG, viewport=(1920, 1080)), False, {}

    def evaluate(self, task):
        return BenchmarkResult(task_id=task.task_id, success=True, score=1.0)

    def observe(self):
        return BenchmarkObservation(screenshot=FAKE_PNG, viewport=(1920, 1080))


@pytest.fixture
def dummy_adapter():
    return DummyAdapter()


@pytest.fixture
def sample_task():
    return BenchmarkTask(
        task_id="scrub_test_1",
        instruction="Test PII scrubbing",
        domain="desktop",
    )


# ---------------------------------------------------------------------------
# Wrapping / delegation
# ---------------------------------------------------------------------------


class TestScrubMiddlewareWrapping:
    def test_wraps_adapter(self, dummy_adapter):
        mw = ScrubMiddleware(dummy_adapter)
        assert mw._adapter is dummy_adapter

    def test_name_includes_inner(self, dummy_adapter):
        mw = ScrubMiddleware(dummy_adapter)
        assert "dummy" in mw.name
        assert "scrub" in mw.name

    def test_benchmark_type_delegated(self, dummy_adapter):
        mw = ScrubMiddleware(dummy_adapter)
        assert mw.benchmark_type == "interactive"

    def test_list_tasks_delegated(self, dummy_adapter):
        mw = ScrubMiddleware(dummy_adapter)
        assert mw.list_tasks() == []

    def test_load_task_delegated(self, dummy_adapter):
        mw = ScrubMiddleware(dummy_adapter)
        with pytest.raises(KeyError):
            mw.load_task("nope")

    def test_evaluate_delegated(self, dummy_adapter, sample_task):
        mw = ScrubMiddleware(dummy_adapter)
        result = mw.evaluate(sample_task)
        assert result.success is True

    def test_close_delegated(self, dummy_adapter):
        dummy_adapter.close = MagicMock()
        mw = ScrubMiddleware(dummy_adapter)
        mw.close()
        dummy_adapter.close.assert_called_once()


# ---------------------------------------------------------------------------
# Scrubbing on observe / reset / step
# ---------------------------------------------------------------------------


class TestScrubMiddlewareScrubbing:
    def _make_middleware_with_mock_provider(self, dummy_adapter):
        """Create middleware with a mock scrubbing provider."""
        mw = ScrubMiddleware(dummy_adapter)
        mock_provider = MagicMock()
        mock_provider.scrub_image.return_value = SCRUBBED_PNG
        mw._provider = mock_provider
        mw._provider_load_attempted = True
        return mw, mock_provider

    def test_observe_scrubs_screenshot(self, dummy_adapter):
        mw, provider = self._make_middleware_with_mock_provider(dummy_adapter)
        obs = mw.observe()
        provider.scrub_image.assert_called_once_with(FAKE_PNG)
        assert obs.screenshot == SCRUBBED_PNG

    def test_reset_scrubs_screenshot(self, dummy_adapter, sample_task):
        mw, provider = self._make_middleware_with_mock_provider(dummy_adapter)
        obs = mw.reset(sample_task)
        provider.scrub_image.assert_called_once_with(FAKE_PNG)
        assert obs.screenshot == SCRUBBED_PNG

    def test_step_scrubs_screenshot(self, dummy_adapter, sample_task):
        mw, provider = self._make_middleware_with_mock_provider(dummy_adapter)
        action = BenchmarkAction(type="click", x=100, y=200)
        obs, done, info = mw.step(action)
        provider.scrub_image.assert_called_once_with(FAKE_PNG)
        assert obs.screenshot == SCRUBBED_PNG

    def test_original_screenshot_stored(self, dummy_adapter, sample_task):
        mw, _ = self._make_middleware_with_mock_provider(dummy_adapter)
        mw.reset(sample_task)
        assert mw.last_original_screenshot == FAKE_PNG

    def test_none_screenshot_skips_scrub(self, dummy_adapter, sample_task):
        """If the inner adapter returns no screenshot, scrubbing is skipped."""
        dummy_adapter.reset = lambda t: BenchmarkObservation(screenshot=None)
        mw, provider = self._make_middleware_with_mock_provider(dummy_adapter)
        obs = mw.reset(sample_task)
        provider.scrub_image.assert_not_called()
        assert obs.screenshot is None


# ---------------------------------------------------------------------------
# Fallback when openadapt-privacy not installed
# ---------------------------------------------------------------------------


class TestScrubMiddlewareFallback:
    def test_fallback_when_not_installed(self, dummy_adapter, sample_task):
        """If openadapt-privacy is not installed, screenshots pass through."""
        mw = ScrubMiddleware(dummy_adapter)
        # Simulate import failure
        mw._provider_load_attempted = True
        mw._provider = None

        obs = mw.reset(sample_task)
        # Screenshot should be the original (unscrubbed)
        assert obs.screenshot == FAKE_PNG

    def test_import_error_logged_once(self, dummy_adapter):
        """The import error warning should only fire once."""
        mw = ScrubMiddleware(dummy_adapter)
        with patch(
            "openadapt_evals.adapters.scrub_middleware.ScrubMiddleware._get_provider",
            return_value=None,
        ):
            mw._scrub(FAKE_PNG)
            mw._scrub(FAKE_PNG)

    def test_scrub_disabled_passes_through(self, dummy_adapter, sample_task):
        """When both scrub_text and scrub_images are False, no scrubbing occurs."""
        mw = ScrubMiddleware(dummy_adapter, scrub_text=False, scrub_images=False)
        mock_provider = MagicMock()
        mw._provider = mock_provider

        obs = mw.reset(sample_task)
        mock_provider.scrub_image.assert_not_called()
        assert obs.screenshot == FAKE_PNG


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestScrubMiddlewareErrors:
    def test_scrub_error_returns_original(self, dummy_adapter, sample_task):
        """If scrubbing raises, the original screenshot is returned."""
        mw = ScrubMiddleware(dummy_adapter)
        mock_provider = MagicMock()
        mock_provider.scrub_image.side_effect = RuntimeError("scrub failed")
        mw._provider = mock_provider
        mw._provider_load_attempted = True

        obs = mw.reset(sample_task)
        assert obs.screenshot == FAKE_PNG

    def test_observe_without_inner_observe_raises(self):
        """If inner adapter lacks observe(), ScrubMiddleware.observe() raises."""
        # Create a bare BenchmarkAdapter mock without observe attribute
        inner = MagicMock(spec=BenchmarkAdapter)
        del inner.observe  # Remove observe from spec
        mw = ScrubMiddleware(inner)

        with pytest.raises(AttributeError, match="does not expose observe"):
            mw.observe()
