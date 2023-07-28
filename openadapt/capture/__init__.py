"""Capture the screen, audio, and camera as a video on macOS and Windows.

Module: capture.py
"""
import sys

if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform == "win32":
    from . import _windows as impl
else:
    raise Exception(f"Unsupported platform: {sys.platform}")


def get_capture() -> impl.Capture:
    """Get the capture object.

    Returns:
        Capture: The capture object.
    """
    return impl.Capture()


if __name__ in ("__main__", "capture"):
    capture = get_capture()
    capture.start(audio=True, camera=False)
    input("Press enter to stop")
    capture.stop()
