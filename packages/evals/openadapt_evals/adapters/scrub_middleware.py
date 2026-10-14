"""Scrubbing middleware that wraps any BenchmarkAdapter with PII removal.

Every screenshot captured by the wrapped adapter is scrubbed of personally
identifiable information (PII) **before** it reaches the agent. The original
(unscrubbed) screenshot is stored separately for audit purposes.

The middleware delegates all methods to the wrapped adapter but intercepts
:meth:`observe`, :meth:`reset`, and :meth:`step` to scrub returned
screenshots.

Dependencies:
    Scrubbing is provided by ``openadapt-privacy`` (Presidio-based). If the
    package is not installed, a warning is logged and screenshots pass
    through unmodified.

Example:
    ```python
    from openadapt_evals.adapters.local import LocalAdapter
    from openadapt_evals.adapters.scrub_middleware import ScrubMiddleware

    raw_adapter = LocalAdapter()
    adapter = ScrubMiddleware(raw_adapter)
    obs = adapter.observe()  # screenshot is scrubbed
    ```
"""

from __future__ import annotations

import logging
from typing import Any

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)

logger = logging.getLogger(__name__)


class ScrubMiddleware(BenchmarkAdapter):
    """Wraps any adapter with mandatory PII scrubbing.

    Every screenshot is scrubbed BEFORE the agent sees it. The original
    (unscrubbed) bytes are available via :attr:`last_original_screenshot`
    for audit logging.

    Args:
        adapter: The inner adapter to wrap.
        scrub_text: Whether to redact detected text PII (default True).
        scrub_images: Whether to redact detected image PII such as faces
            (default True).
        strict: When True, raise errors instead of silently falling back
            to unscrubbed data. Use during benchmarking/training to ensure
            scrubbing is actually active.
    """

    def __init__(
        self,
        adapter: BenchmarkAdapter,
        scrub_text: bool = True,
        scrub_images: bool = True,
        strict: bool = False,
    ):
        self._adapter = adapter
        self._scrub_text = scrub_text
        self._scrub_images = scrub_images
        self._strict = strict
        self._provider = None  # lazy-loaded
        self._provider_load_attempted = False
        self.last_original_screenshot: bytes | None = None

    # ------------------------------------------------------------------
    # BenchmarkAdapter properties — delegated
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return f"scrub({self._adapter.name})"

    @property
    def benchmark_type(self) -> str:
        return self._adapter.benchmark_type

    @property
    def supports_parallel(self) -> bool:
        return self._adapter.supports_parallel

    # ------------------------------------------------------------------
    # Delegation with scrubbing
    # ------------------------------------------------------------------

    def list_tasks(self, domain: str | None = None) -> list[BenchmarkTask]:
        return self._adapter.list_tasks(domain)

    def load_task(self, task_id: str) -> BenchmarkTask:
        return self._adapter.load_task(task_id)

    def reset(self, task: BenchmarkTask) -> BenchmarkObservation:
        obs = self._adapter.reset(task)
        return self._scrub_observation(obs)

    def step(
        self, action: BenchmarkAction
    ) -> tuple[BenchmarkObservation, bool, dict[str, Any]]:
        obs, done, info = self._adapter.step(action)
        return self._scrub_observation(obs), done, info

    def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
        return self._adapter.evaluate(task)

    def close(self) -> None:
        self._adapter.close()

    # ------------------------------------------------------------------
    # Public observe (if called directly)
    # ------------------------------------------------------------------

    def observe(self) -> BenchmarkObservation:
        """Capture and scrub an observation.

        If the inner adapter exposes an ``observe`` method, it is called
        directly. Otherwise we fall back to the interface contract (which
        does not define ``observe`` — callers should use ``reset``/``step``).
        """
        if hasattr(self._adapter, "observe"):
            obs = self._adapter.observe()  # type: ignore[attr-defined]
        else:
            raise AttributeError(
                f"Inner adapter {self._adapter.name!r} does not expose observe()"
            )
        return self._scrub_observation(obs)

    # ------------------------------------------------------------------
    # Scrubbing internals
    # ------------------------------------------------------------------

    def _scrub_observation(self, obs: BenchmarkObservation) -> BenchmarkObservation:
        """Scrub PII from the observation's screenshot."""
        if obs.screenshot is not None:
            self.last_original_screenshot = obs.screenshot
            obs.screenshot = self._scrub(obs.screenshot)
        return obs

    def _scrub(self, screenshot_bytes: bytes) -> bytes:
        """Scrub PII from raw PNG screenshot bytes.

        Uses ``openadapt_privacy.PresidioScrubbingProvider`` when available.
        Falls back to returning the original bytes with a warning if the
        privacy package is not installed.
        """
        if not (self._scrub_text or self._scrub_images):
            return screenshot_bytes

        provider = self._get_provider()
        if provider is None:
            return screenshot_bytes

        try:
            return provider.scrub_image(screenshot_bytes)
        except Exception:
            if self._strict:
                raise
            logger.warning(
                "PII scrubbing failed, returning original screenshot",
                exc_info=True,
            )
            return screenshot_bytes

    def _get_provider(self):
        """Lazily load the Presidio scrubbing provider."""
        if self._provider is not None:
            return self._provider

        if self._provider_load_attempted:
            return None

        self._provider_load_attempted = True
        try:
            from openadapt_privacy import PresidioScrubbingProvider  # type: ignore[import-untyped]

            self._provider = PresidioScrubbingProvider()
            logger.info("PII scrubbing enabled via openadapt-privacy (Presidio)")
            return self._provider
        except ImportError:
            if self._strict:
                raise ImportError(
                    "openadapt-privacy is not installed and strict mode is enabled. "
                    "PII scrubbing cannot be skipped. "
                    "Install with: pip install openadapt-privacy"
                )
            logger.warning(
                "openadapt-privacy is not installed. "
                "PII scrubbing is DISABLED — screenshots will pass through unmodified. "
                "Install with: pip install openadapt-privacy"
            )
            return None
