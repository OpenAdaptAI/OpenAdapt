"""Screen stability detection for WAA VM screenshots.

Provides utilities for comparing screenshots and waiting until the VM
screen stabilises before proceeding.  Extracted from
``scripts/record_waa_demos.py`` so that tests and other callers can
import the functions directly without ``importlib`` hacks.

Example:
    ```python
    from openadapt_evals.infrastructure.screen_stability import (
        compare_screenshots,
        wait_for_stable_screen,
    )

    similarity = compare_screenshots(png_a, png_b)
    stable_png = wait_for_stable_screen("http://localhost:5001")
    ```
"""

from __future__ import annotations

import io
import time
from typing import Callable


def _take_screenshot(server: str) -> bytes:
    """Take a screenshot from the WAA server, raising on failure."""
    import requests

    resp = requests.get(f"{server}/screenshot", timeout=30)
    resp.raise_for_status()
    return resp.content


def compare_screenshots(png_a: bytes, png_b: bytes) -> float:
    """Compare two PNG screenshots and return pixel similarity (0.0-1.0).

    Uses raw RGB pixel comparison.  Returns 1.0 for identical images.
    The 99.5% threshold used by ``wait_for_stable_screen`` tolerates
    minor differences like the taskbar clock updating (~0.14% of pixels)
    or cursor blink.
    """
    from PIL import Image

    img_a = Image.open(io.BytesIO(png_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(png_b)).convert("RGB")

    if img_a.size != img_b.size:
        return 0.0

    bytes_a = img_a.tobytes()
    bytes_b = img_b.tobytes()

    if bytes_a == bytes_b:
        return 1.0  # fast path: identical raw bytes

    # Vectorized comparison via numpy (already a transitive dep via open-clip-torch)
    import numpy as np

    arr_a = np.frombuffer(bytes_a, dtype=np.uint8).reshape(-1, 3)
    arr_b = np.frombuffer(bytes_b, dtype=np.uint8).reshape(-1, 3)
    matching = int(np.all(arr_a == arr_b, axis=1).sum())
    return matching / arr_a.shape[0]


def wait_for_stable_screen(
    server: str,
    poll_interval: float = 2.0,
    stability_timeout: float = 30.0,
    similarity_threshold: float = 0.995,
    required_stable_checks: int = 3,
    screenshot_fn: Callable[[str], bytes] | None = None,
) -> bytes:
    """Wait for the VM screen to stabilize, then return the screenshot.

    Polls the QEMU framebuffer (free -- local HTTP call) until
    ``required_stable_checks`` consecutive screenshot pairs exceed
    ``similarity_threshold``.  With the defaults (3 checks at 2s
    intervals), the screen must be stable for 6 seconds before
    proceeding.

    Args:
        server: WAA server URL (``/screenshot`` endpoint).
        poll_interval: Seconds between screenshots.
        stability_timeout: Maximum seconds to wait.  If exceeded, the
            last screenshot is returned with a warning.
        similarity_threshold: Pixel-match fraction (0.0-1.0).  0.995
            tolerates taskbar clock and cursor blink.
        required_stable_checks: Consecutive stable pairs required.
            With poll_interval=2 and required_stable_checks=3, the
            screen must be unchanged for 6 seconds.
        screenshot_fn: Optional callable ``(server) -> bytes`` used to
            capture a screenshot.  Defaults to an internal helper that
            calls ``GET {server}/screenshot``.

    Returns:
        PNG screenshot bytes of the stable screen.
    """
    take = screenshot_fn or _take_screenshot

    prev_png = take(server)
    stable_count = 0
    deadline = time.time() + stability_timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        curr_png = take(server)

        similarity = compare_screenshots(prev_png, curr_png)

        if similarity >= similarity_threshold:
            stable_count += 1
            if stable_count >= required_stable_checks:
                elapsed = stability_timeout - (deadline - time.time())
                print(f"    Screen stable after {elapsed:.0f}s "
                      f"({stable_count} checks, {similarity:.4f} similarity)")
                return curr_png
        else:
            if stable_count > 0:
                print(f"    Screen changed ({similarity:.3f}), resetting stability count")
            stable_count = 0

        prev_png = curr_png

    print(f"    WARNING: Screen did not stabilize within {stability_timeout:.0f}s. "
          "Using last screenshot.")
    return prev_png
