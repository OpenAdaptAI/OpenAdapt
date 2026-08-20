"""Production artifact boundary for the repository-only browser prototype."""

from __future__ import annotations

import socket
import time
import zipfile

import pytest

import openadapt_capture
from openadapt_capture.browser_events import BrowserClickEvent
from openadapt_capture.capture import CaptureSession
from openadapt_capture.cli import record as cli_record
from openadapt_capture.db import create_db
from openadapt_capture.db.crud import insert_browser_event, insert_recording
from openadapt_capture.recorder import Recorder
from scripts.verify_distribution import verify_distribution


def test_public_api_has_no_browser_bridge_or_replay_exports() -> None:
    forbidden = {
        "BrowserBridge",
        "BrowserMode",
        "BrowserEventRecord",
        "run_browser_bridge",
    }

    assert forbidden.isdisjoint(openadapt_capture.__all__)
    for name in forbidden:
        assert not hasattr(openadapt_capture, name)


def test_legacy_browser_opt_in_fails_before_socket_bind(monkeypatch, tmp_path) -> None:
    bind_calls: list[object] = []

    def reject_bind(self, address):
        bind_calls.append(address)
        raise AssertionError("legacy browser opt-in attempted to bind a listener")

    monkeypatch.setattr(socket.socket, "bind", reject_bind)

    with pytest.raises(ValueError, match="openadapt-flow Playwright"):
        Recorder(str(tmp_path / "direct"), capture_browser_events=True)
    with pytest.raises(SystemExit) as exc_info:
        cli_record(str(tmp_path / "cli"), browser_events=True)

    assert exc_info.value.code == 2
    assert bind_calls == []


def test_distribution_validator_rejects_repository_browser_bridge(tmp_path) -> None:
    wheel = tmp_path / "openadapt_capture-1.2.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/browser_bridge.py", "EXECUTE_ACTION = 1\n")
        archive.writestr("openadapt_capture-1.2.2.dist-info/licenses/LICENSE", "MIT\n")
        archive.writestr("openadapt_capture-1.2.2.dist-info/METADATA", "Name: openadapt-capture\n")

    with pytest.raises(AssertionError, match="repository-only path"):
        verify_distribution(wheel)


def test_passive_legacy_browser_event_still_loads(tmp_path) -> None:
    engine, session_factory = create_db(str(tmp_path / "recording.db"))
    session = session_factory()
    timestamp = time.time()
    recording = insert_recording(
        session,
        {
            "timestamp": timestamp,
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "test",
            "task_description": "legacy browser data",
        },
    )
    insert_browser_event(
        session,
        recording,
        timestamp + 1,
        {
            "message": {
                "type": "DOM_EVENT",
                "tabId": 7,
                "payload": {
                    "eventType": "click",
                    "url": "https://example.invalid/legacy",
                    "clientX": 20,
                    "clientY": 30,
                    "pageX": 20,
                    "pageY": 30,
                    "element": {
                        "role": "button",
                        "name": "Continue",
                        "bbox": {"x": 10, "y": 20, "width": 40, "height": 20},
                        "xpath": "/html/body/button",
                    },
                },
            }
        },
    )
    session.close()
    engine.dispose()

    with CaptureSession.load(tmp_path) as capture:
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserClickEvent)
        assert events[0].element.name == "Continue"
        assert events[0].tab_id == 7
