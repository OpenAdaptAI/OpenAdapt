"""Tests for the private canonical Flow record process entry."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import ModuleType

import yaml

from engine import flow_record_entry


class _InertThread:
    def __init__(self, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass


def test_private_request_is_deleted_and_exact_target_reaches_flow(
    tmp_path: Path, monkeypatch
) -> None:
    request = tmp_path / ".record-request.yaml"
    ready = tmp_path / ".ready"
    stop = tmp_path / ".stop"
    out_dir = tmp_path / "recording"
    request.write_text(
        yaml.safe_dump(
            {
                "target": {
                    "backend": "citrix",
                    "rdp_window": "Citrix Viewer",
                    "rdp_window_title": "Patient Jane Doe",
                    "rdp_readiness_text": "MRN 12345",
                },
                "out_dir": str(out_dir),
                "task": "Review patient workflow",
                "stop_path": str(stop),
                "ready_path": str(ready),
            }
        )
    )
    received: list[list[str]] = []
    module = ModuleType("openadapt_flow.__main__")
    module.main = lambda args: received.append(args) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openadapt_flow.__main__", module)
    monkeypatch.setattr(flow_record_entry.threading, "Thread", _InertThread)

    assert flow_record_entry.main([str(request)]) == 0

    assert not request.exists()
    assert not ready.exists()
    assert received == [
        [
            "record",
            "--out",
            str(out_dir),
            "--backend",
            "citrix",
            "--task",
            "Review patient workflow",
            "--rdp-window",
            "Citrix Viewer",
            "--rdp-window-title",
            "Patient Jane Doe",
            "--rdp-readiness-text",
            "MRN 12345",
        ]
    ]


def test_parent_pipe_eof_interrupts_capture_exactly_once(monkeypatch) -> None:
    interrupts: list[bool] = []
    monkeypatch.setattr(
        flow_record_entry._thread,
        "interrupt_main",
        lambda: interrupts.append(True),
    )
    triggered = threading.Event()
    lock = threading.Lock()

    flow_record_entry._watch_parent_stdin(
        io.BytesIO(b""),
        triggered,
        lock,
    )
    flow_record_entry._watch_parent_stdin(
        io.BytesIO(b""),
        triggered,
        lock,
    )

    assert triggered.is_set()
    assert interrupts == [True]
