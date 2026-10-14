"""Pass 0: PII scrubbing for recording sessions.

Scrubs all screenshots and text before VLM processing.
Uses openadapt-privacy if available, passes through if not.
"""

from __future__ import annotations

import logging

from openadapt_evals.workflow.models import RecordingSession

logger = logging.getLogger(__name__)

_scrubber = None
_scrubber_checked = False


def _get_scrubber():
    global _scrubber, _scrubber_checked
    if _scrubber_checked:
        return _scrubber
    _scrubber_checked = True
    try:
        from openadapt_privacy import PresidioScrubbingProvider
        _scrubber = PresidioScrubbingProvider()
        logger.info("PII scrubbing enabled (openadapt-privacy)")
    except ImportError:
        logger.warning(
            "openadapt-privacy not installed. PII scrubbing DISABLED. "
            "Screenshots will be sent to VLM APIs unscrubbed."
        )
    return _scrubber


def scrub_text(text: str) -> str:
    """Scrub PII from text. Returns original if scrubber unavailable."""
    scrubber = _get_scrubber()
    if scrubber is None:
        return text
    try:
        return scrubber.scrub_text(text)
    except Exception as exc:
        logger.warning("Text scrubbing failed: %s", exc)
        return text


def scrub_image(image_bytes: bytes) -> bytes:
    """Scrub PII from image. Returns original if scrubber unavailable."""
    scrubber = _get_scrubber()
    if scrubber is None:
        return image_bytes
    try:
        return scrubber.scrub_image(image_bytes)
    except Exception as exc:
        logger.warning("Image scrubbing failed: %s", exc)
        return image_bytes


def scrub_recording_session(session: RecordingSession) -> RecordingSession:
    """Scrub PII from all screenshots and text in a RecordingSession.

    Returns a new RecordingSession with scrubbed data (does not mutate input).
    """
    scrubbed = session.model_copy(deep=True)

    for action in scrubbed.actions:
        action.description = scrub_text(action.description)
        if action.typed_text:
            action.typed_text = scrub_text(action.typed_text)
        if action.window_title:
            action.window_title = scrub_text(action.window_title)

    scrubbed.task_description = scrub_text(scrubbed.task_description)

    logger.info(
        "Scrubbed recording session %s (%d actions)",
        scrubbed.session_id, len(scrubbed.actions),
    )
    return scrubbed
