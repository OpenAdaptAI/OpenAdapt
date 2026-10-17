"""Load the OCR engine and check the browser driver on background threads.

On a fresh install, macOS verifies each new native library on first load; for
OpenCV that alone can take ten seconds or more. Starting the load early lets
the browser start and the demo app come up at the same time. With the native
OCR backend this loads Apple Vision or Windows OCR instead, and OpenCV not at
all.

macOS also checks Playwright's Node.js driver the first time it runs from a
new environment, which takes about a second. Running ``node --version`` on a
second thread gets that check done while Python imports the rest of the demo.
This module imports nothing heavy itself.
"""

from __future__ import annotations

import threading

_started = False
_lock = threading.Lock()


def _blank_png(width: int = 64, height: int = 16) -> bytes:
    """A small white RGB PNG, built with the standard library.

    Big enough for a native engine to run a real request and load its models.
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
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
            recognize(_blank_png())
    except Exception:  # noqa: BLE001 - the first real call reports it
        pass


def _touch_driver() -> None:
    try:
        import subprocess

        from playwright._impl._driver import compute_driver_executable

        executable = compute_driver_executable()
        node = executable[0] if isinstance(executable, tuple) else executable
        subprocess.run(
            [str(node), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001 - the real driver start reports problems
        pass


def start() -> None:
    """Start the background work once per process and return at once."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_touch_driver, name="driver-prewarm", daemon=True).start()
    threading.Thread(target=_load, name="ocr-prewarm", daemon=True).start()
