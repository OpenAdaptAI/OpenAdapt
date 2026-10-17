"""Load OpenCV and the OCR models on a background thread.

On a fresh install, macOS verifies each new native library on first load; for
OpenCV that alone can take ten seconds. Starting the load early lets the
browser start and the demo app come up at the same time. This module imports
nothing heavy itself.
"""

from __future__ import annotations

import threading

_started = False
_lock = threading.Lock()


def _load() -> None:
    try:
        import cv2  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401

        from openadapt_flow.vision.ocr import _get_engine

        _get_engine()
    except Exception:  # noqa: BLE001 - the first real call reports it
        pass


def start() -> None:
    """Start the background load once per process and return at once."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_load, name="ocr-prewarm", daemon=True).start()
