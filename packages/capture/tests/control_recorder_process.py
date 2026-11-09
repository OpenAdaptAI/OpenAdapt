"""Cross-process helper for the Capture control contract tests."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from openadapt_capture import recorder as recorder_module
from openadapt_capture.db import create_db, crud


def _fake_record(
    *,
    capture_dir,
    terminate_processing,
    terminate_recording,
    status_pipe,
    **_kwargs,
) -> None:
    Path(capture_dir).mkdir(parents=True, exist_ok=True)
    db_path = Path(capture_dir) / "recording.db"
    engine, session_factory = create_db(str(db_path))
    session = session_factory()
    try:
        crud.insert_recording(
            session,
            {
                "timestamp": time.time(),
                "monitor_width": 1280,
                "monitor_height": 720,
                "pixel_ratio": 1.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5.0,
                "platform": sys.platform,
                "task_description": "control contract subprocess",
            },
        )
    finally:
        session.close()
        engine.dispose()
    status_pipe.send({"type": "record.started"})
    terminate_processing.wait()
    status_pipe.send({"type": "record.stopping"})
    if os.environ.get("OPENADAPT_CONTROL_TEST_STALL") == "1":
        time.sleep(2.0)
    terminate_recording.set()
    status_pipe.send({"type": "record.stopped"})


def main() -> None:
    capture_dir, runtime_dir = sys.argv[1:3]
    recorder_module.record = _fake_record
    recorder = recorder_module.Recorder(
        capture_dir,
        capture_video=False,
        capture_images=True,
        plot_performance=False,
        control_runtime_dir=runtime_dir,
    )
    with recorder:
        if not recorder.wait_for_ready(timeout=15):
            raise RuntimeError("test recorder did not become ready")
        print(recorder.control_session_id, flush=True)
        while recorder.is_recording:
            time.sleep(0.05)


if __name__ == "__main__":
    main()
