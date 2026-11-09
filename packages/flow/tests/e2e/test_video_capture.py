"""Opt-in Playwright session-video capture (used to film the website media).

``PlaywrightBackend.launch(record_video_dir=...)`` is OFF by default and must
have zero effect on a normal launch; when a directory is given it records a
WebM of the session, finalized on ``close()``. These tests pin both halves of
that contract so the recorder/replayer capture flag (``--record-video``) stays
a no-op on the default path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend

pytestmark = pytest.mark.timeout(120)


def test_launch_without_record_video_writes_no_video(mockmed_url: str) -> None:
    """Default launch records nothing and exposes no page video."""
    backend, close = PlaywrightBackend.launch(mockmed_url, headless=True)
    try:
        assert backend.page.video is None
        assert backend.screenshot()  # still a working session
    finally:
        close()


def test_launch_with_record_video_writes_webm(mockmed_url: str, tmp_path: Path) -> None:
    """Opt-in launch records a WebM that is flushed to disk on close()."""
    vid_dir = tmp_path / "video"
    vid_dir.mkdir()
    backend, close = PlaywrightBackend.launch(
        mockmed_url, headless=True, record_video_dir=str(vid_dir)
    )
    try:
        assert backend.page.video is not None
        backend.click(10, 10)  # produce a few frames
        backend.screenshot()
    finally:
        close()  # closes the context, finalizing the video
    videos = list(vid_dir.glob("*.webm"))
    assert videos, "expected a .webm to be written to record_video_dir"
    assert videos[0].stat().st_size > 0
