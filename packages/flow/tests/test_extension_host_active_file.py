"""The native host writes only while Desktop's active marker exists (OpenAdapt#1872).

OpenAdapt Desktop registers the host for one recording with ``--recording`` and
``--active-file`` and removes the marker on stop. Chrome may keep the host
process alive after that, so the host itself must refuse to write once the
marker or the recording folder is gone. These tests run the real host script
over real stdio framing (no Chrome).
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

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

    def __init__(self, recording: Path, active: Path) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(HOST),
                "--recording",
                str(recording),
                "--active-file",
                str(active),
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
    active.write_text("")
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
    active.write_text("")
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
