"""Capture the screen, audio, and camera as a video on macOS and Windows.

Module: capture.py
"""

import sys

if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform == "win32":
    from . import _windows as impl
elif sys.platform == "linux":
    from . import _linux as impl
else:
    print(f"WARNING: openadapt.capture is not yet supported on {sys.platform=}")
    impl = None

device = impl.Capture() if impl else None


def get_capture() -> impl.Capture:
    """Get the capture object.

    Returns:
        Capture: The capture object.
    """
    return device


def start(audio: bool = False, camera: bool = False) -> None:
    """Start the capture."""
    if device:
        device.start(audio=audio, camera=camera)


def stop() -> None:
    """Stop the capture."""
    if device:
        device.stop()


def test() -> None:
    """Test the capture."""
    if device:
        device.start()
        input("Press enter to stop")
        device.stop()


if __name__ in ("__main__", "capture"):
    test()
