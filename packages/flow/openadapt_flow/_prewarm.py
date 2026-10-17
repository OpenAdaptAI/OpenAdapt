"""Load the OCR engine on a background thread.

On a fresh install, macOS verifies each new native library on first load; for
OpenCV that alone can take ten seconds or more. Starting the load early lets
the browser start and the demo app come up at the same time. With the native
OCR backend this loads Apple Vision or Windows OCR instead, and OpenCV not at
all. This module imports nothing heavy itself.
"""

from __future__ import annotations

import threading

_started = False
_lock = threading.Lock()

#: A 1x1 white PNG, enough to make a native engine load its models.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b30000"
    "000049454e44ae426082"
)


def _load() -> None:
    try:
        from openadapt_flow.vision.ocr_backends import active_backend_name

        if active_backend_name() == "rapidocr":
            import cv2  # noqa: F401
            import rapidocr_onnxruntime  # noqa: F401

        from openadapt_flow.vision.ocr import _get_engine

        engine = _get_engine()
        recognize = getattr(engine, "recognize_png", None)
        if recognize is not None:
            recognize(_TINY_PNG)
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
