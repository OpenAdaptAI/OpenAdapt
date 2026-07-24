"""Contract between the Desktop engine and the installed Capture runtime."""

from __future__ import annotations

import inspect


def test_installed_capture_exposes_native_recorder_contract() -> None:
    """The packaged dependency must expose the recorder Desktop invokes."""

    from openadapt_capture import Recorder

    assert Recorder is not None, "openadapt-capture installed without a usable Recorder"
    parameters = inspect.signature(Recorder).parameters
    assert "capture_dir" in parameters
    assert "task_description" in parameters
    for member in ("__enter__", "__exit__", "wait_for_ready", "stop", "event_count"):
        assert hasattr(Recorder, member), f"Recorder is missing {member}"
