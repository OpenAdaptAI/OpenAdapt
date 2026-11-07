"""The native host writes only while Desktop's active marker exists (OpenAdapt#1872).

OpenAdapt Desktop registers the host for one recording with ``--recording``,
``--active-file`` and ``--active-lease`` and removes the marker on stop. Chrome may keep the host
process alive after that, so the host itself must refuse to write once the
marker or the recording folder is gone. These tests run the real host script
over real stdio framing (no Chrome).
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOST = (
    Path(__file__).resolve().parents[1]
    / "chrome_extension"
    / "native_host"
    / "openadapt_extension_host.py"
)
EVIDENCE = {
    "type": "EXTENSION_EVIDENCE",
    "evidence": {"kind": "target", "event": "click", "path": "button#save"},
}


def _frame(message: dict) -> bytes:
    data = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(data)) + data


def _replies(raw: bytes) -> list[dict]:
    out = []
    while raw:
        (length,) = struct.unpack("<I", raw[:4])
        out.append(json.loads(raw[4 : 4 + length]))
        raw = raw[4 + length :]
    return out


class _Host:
    """The real host over a pipe, one framed message at a time."""

    def __init__(self, recording: Path, active: Path, *extra: str) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(HOST),
                "--recording",
                str(recording),
                "--active-file",
                str(active),
                *extra,
                "chrome-extension://abcdefghijklmnopabcdefghijklmnop/",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def send(self, message: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(_frame(message))
        self.proc.stdin.flush()
        (length,) = struct.unpack("<I", self.proc.stdout.read(4))
        return json.loads(self.proc.stdout.read(length))

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def test_writes_while_active_then_refuses_after_stop(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    host = _Host(recording, active)
    try:
        assert host.send(EVIDENCE) == {"ok": True, "count": 1}
        active.unlink()  # Desktop stopped the recording; Chrome kept the host
        assert host.send(EVIDENCE) == {"ok": False, "error": "not_recording"}
    finally:
        host.close()
    lines = (recording / "extension_evidence.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["events"] == 1
    assert len(lines) == 2


def test_never_recreates_a_deleted_recording_folder(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    host = _Host(recording, active)
    try:
        recording.rmdir()  # retention removed the capture
        assert host.send(EVIDENCE) == {"ok": False, "error": "not_recording"}
    finally:
        host.close()
    assert not recording.exists()


def test_inactive_from_start_writes_nothing(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            str(HOST),
            "--recording",
            str(recording),
            "--active-file",
            str(tmp_path / "missing"),
        ],
        input=_frame(EVIDENCE),
        capture_output=True,
        timeout=60,
    )
    assert _replies(proc.stdout) == [{"ok": False, "error": "not_recording"}]
    assert list(recording.iterdir()) == []


def test_without_active_file_behaviour_is_unchanged(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    proc = subprocess.run(
        [sys.executable, str(HOST), "--recording", str(recording)],
        input=_frame(EVIDENCE),
        capture_output=True,
        timeout=60,
    )
    assert _replies(proc.stdout) == [{"ok": True, "count": 1}]
    assert (recording / "extension_evidence.jsonl").is_file()


def _load_host():
    spec = importlib.util.spec_from_file_location("openadapt_extension_host", HOST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text", ["", "revoked\n", "active", "active\n\n", " active\n"])
def test_marker_must_hold_exactly_active(tmp_path: Path, text: str) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text(text)
    host = _Host(recording, active)
    try:
        assert host.send(EVIDENCE) == {"ok": False, "error": "not_recording"}
    finally:
        host.close()
    assert list(recording.iterdir()) == []


def test_overwritten_marker_revokes_a_running_host(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    host = _Host(recording, active, "--active-lease", "30")
    try:
        assert host.send(EVIDENCE) == {"ok": True, "count": 1}
        active.write_text("revoked\n")  # Desktop could not remove the file
        assert host.send(EVIDENCE) == {"ok": False, "error": "not_recording"}
    finally:
        host.close()
    assert len((recording / "extension_evidence.jsonl").read_text().splitlines()) == 2


def test_lapsed_lease_revokes_a_running_host(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    host = _Host(recording, active, "--active-lease", "30")
    try:
        assert host.send(EVIDENCE) == {"ok": True, "count": 1}
        stale = time.time() - 31  # Desktop stopped renewing the lease
        os.utime(active, (stale, stale))
        assert host.send(EVIDENCE) == {"ok": False, "error": "not_recording"}
        os.utime(active)  # renewed again (for example after sleep)
        assert host.send(EVIDENCE) == {"ok": True, "count": 2}
    finally:
        host.close()


@pytest.mark.parametrize(
    "extra",
    [
        ["--active-lease", "0"],
        ["--active-lease", "-5"],
        ["--active-lease", "nan"],
        ["--active-lease", "inf"],
        ["--active-lease", "soon"],
    ],
)
def test_unenforceable_lease_refuses_to_start(tmp_path: Path, extra: list[str]) -> None:
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    proc = subprocess.run(
        [
            sys.executable,
            str(HOST),
            "--recording",
            str(recording),
            "--active-file",
            str(active),
        ]
        + extra,
        input=_frame(EVIDENCE),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert _replies(proc.stdout) == [{"ok": False, "error": "bad_active_lease"}]
    assert list(recording.iterdir()) == []


def test_lease_without_active_file_refuses_to_start(tmp_path: Path) -> None:
    recording = tmp_path / "capture"
    proc = subprocess.run(
        [
            sys.executable,
            str(HOST),
            "--recording",
            str(recording),
            "--active-lease",
            "30",
        ],
        input=_frame(EVIDENCE),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert _replies(proc.stdout) == [{"ok": False, "error": "bad_active_lease"}]
    assert not recording.exists()


def test_recording_active_checks(tmp_path: Path) -> None:
    module = _load_host()
    recording = tmp_path / "capture"
    recording.mkdir()
    active = tmp_path / ".extension-active-cap1"
    active.write_text("active\n")
    os.utime(active, (1000, 1000))
    assert module.recording_active(active, recording, None)
    assert module.recording_active(active, recording, 30, now=1029)
    assert not module.recording_active(active, recording, 30, now=1031)
    assert not module.recording_active(
        active, recording, 30, now=969
    )  # clock moved back
    assert not module.recording_active(active, tmp_path / "gone", None)
    assert not module.recording_active(tmp_path / "missing", recording, None)
    (tmp_path / "dir-marker").mkdir()
    assert not module.recording_active(tmp_path / "dir-marker", recording, None)
