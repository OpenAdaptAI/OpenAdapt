"""Screen settle detection: poll until the frame stops changing."""

from __future__ import annotations

import logging
import time

from openadapt_flow.backend import Backend
from openadapt_flow.vision.hashing import phash_distance, phash_png

logger = logging.getLogger(__name__)


def wait_settled(
    backend: Backend,
    *,
    interval_s: float = 0.1,
    stable_frames: int = 2,
    timeout_s: float = 3.0,
) -> bytes:
    """Poll screenshots until the screen is visually stable, then return it.

    Screenshots are taken every ``interval_s`` seconds; the screen counts as
    settled once ``stable_frames`` consecutive frames have identical
    perceptual hashes (phash distance 0). On timeout the most recent frame
    is returned anyway and a warning is logged: a screen that never settles
    (animation, spinner, video) means downstream template matching and
    postcondition checks run against an arbitrary mid-transition frame.

    Args:
        backend: Screen source implementing the :class:`Backend` protocol.
        interval_s: Seconds between polls.
        stable_frames: Number of consecutive identical frames required.
        timeout_s: Maximum seconds to wait before giving up.

    Returns:
        The last captured frame as PNG bytes.
    """
    deadline = time.monotonic() + timeout_s
    png = backend.screenshot()
    last_hash = phash_png(png)
    streak = 1
    while streak < stable_frames and time.monotonic() < deadline:
        time.sleep(interval_s)
        png = backend.screenshot()
        current = phash_png(png)
        if phash_distance(current, last_hash) == 0:
            streak += 1
        else:
            streak = 1
        last_hash = current
    if streak < stable_frames:
        logger.warning(
            "wait_settled: screen did not settle within %.1fs "
            "(%d/%d stable frames); proceeding with the most recent frame — "
            "resolution and postconditions may be evaluated mid-transition",
            timeout_s,
            streak,
            stable_frames,
        )
    return png
