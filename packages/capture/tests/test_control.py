"""Security and lifecycle tests for cross-process Capture control."""

from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from PIL import Image

from openadapt_capture import control
from openadapt_capture import recorder as recorder_module
from openadapt_capture.config import RecordingConfig, config_override
from openadapt_capture.control import (
    CaptureControlError,
    CaptureControlUnavailable,
    RecorderControlServer,
    discover_recorders,
    status_recording,
    stop_recording,
)
from openadapt_capture.db import create_db, crud
from openadapt_capture.terminal import verify_capture_artifacts


def _terminal_payload(capture_dir: Path, session_id: str) -> dict:
    return {
        "schema_version": control.TERMINAL_STATE_SCHEMA_VERSION,
        "session_id": session_id,
        "pid": os.getpid(),
        "process_started_at": psutil.Process().create_time(),
        "capture_dir": str(capture_dir),
        "phase": "recording",
        "ready": True,
        "complete": False,
        "integrity_verified": False,
        "error_code": None,
        "started_at": time.time(),
        "finalized_at": None,
        "event_counts": {
            "action": 0,
            "screen": 0,
            "window": 0,
            "browser": 0,
            "video": 0,
        },
    }


def _create_minimal_recording(
    capture_dir: Path,
    *,
    browser_messages: list[object] | None = None,
) -> None:
    capture_dir.mkdir(parents=True, exist_ok=True)
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    try:
        recording = crud.insert_recording(
            session,
            {
                "timestamp": time.time(),
                "monitor_width": 1280,
                "monitor_height": 720,
                "pixel_ratio": 1.0,
                "double_click_interval_seconds": 0.5,
                "double_click_distance_pixels": 5.0,
                "platform": sys.platform,
                "task_description": "control verification test",
            },
        )
        for offset, message in enumerate(browser_messages or []):
            crud.insert_browser_event(
                session,
                recording,
                time.time() + offset,
                {"message": message},
            )
    finally:
        session.close()
        engine.dispose()


def _wait_for_session(runtime_dir: Path, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sessions = discover_recorders(runtime_dir)
        if len(sessions) == 1:
            try:
                if status_recording(sessions[0], runtime_dir=runtime_dir, timeout=1).ready:
                    return sessions[0]
            except CaptureControlError:
                pass
        time.sleep(0.05)
    raise AssertionError("the subprocess recorder did not publish its control session")


def _start_child(
    tmp_path: Path,
    *,
    stall: bool = False,
) -> tuple[subprocess.Popen[str], Path, Path]:
    capture_dir = tmp_path / "capture"
    runtime_dir = tmp_path / "runtime"
    env = os.environ.copy()
    if stall:
        env["OPENADAPT_CONTROL_TEST_STALL"] = "1"
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("control_recorder_process.py")),
            str(capture_dir),
            str(runtime_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return child, capture_dir, runtime_dir


def _finish_child(child: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = child.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        raise AssertionError(f"recorder child did not exit\nstdout={stdout}\nstderr={stderr}")
    return stdout, stderr


def test_subprocess_ready_status_stop_and_complete(tmp_path: Path) -> None:
    """The same public contract runs on the macOS, Windows, and Linux matrix."""
    child, capture_dir, runtime_dir = _start_child(tmp_path)
    try:
        session_id = _wait_for_session(runtime_dir)
        current = status_recording(session_id, runtime_dir=runtime_dir)
        assert current.phase == "recording"
        assert current.ready
        assert not current.complete

        completed = stop_recording(session_id, runtime_dir=runtime_dir, timeout=10)
        assert completed.phase == "complete"
        assert completed.complete
        assert completed.integrity_verified
        stdout, stderr = _finish_child(child)
        assert child.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
        terminal = json.loads(
            (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert terminal["session_id"] == session_id
        assert terminal["complete"] is True
        assert terminal["integrity_verified"] is True
        assert "token" not in terminal
        assert "capture_dir" not in terminal
        _, manifest = verify_capture_artifacts(capture_dir)
        assert "capture-state.json" in {
            artifact.path for artifact in manifest.artifacts
        }
        assert discover_recorders(runtime_dir) == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_concurrent_repeated_stop_is_idempotent(tmp_path: Path) -> None:
    child, _capture_dir, runtime_dir = _start_child(tmp_path)
    try:
        session_id = _wait_for_session(runtime_dir)
        results: list[object] = []

        def request_stop() -> None:
            try:
                results.append(stop_recording(session_id, runtime_dir=runtime_dir, timeout=10))
            except BaseException as exc:  # retained for an exact assertion below
                results.append(exc)

        callers = [threading.Thread(target=request_stop) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=15)
        assert len(results) == 2
        assert all(not isinstance(result, BaseException) for result in results), repr(results)
        assert all(result.complete for result in results)  # type: ignore[union-attr]
        _finish_child(child)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_crash_recovery_removes_only_proven_stale_endpoint(tmp_path: Path) -> None:
    child, capture_dir, runtime_dir = _start_child(tmp_path)
    session_id = _wait_for_session(runtime_dir)
    descriptor = control._parse_descriptor(runtime_dir / f"{session_id}.json")
    recorder_process = psutil.Process(descriptor.pid)
    recorder_process.kill()
    recorder_process.wait(timeout=15)
    _finish_child(child)

    kernel_live = (
        control._windows_process_live(descriptor.pid)
        if sys.platform == "win32"
        else None
    )
    instance_live = control._process_instance_live(
        descriptor.pid,
        descriptor.process_started_at,
    )
    assert instance_live is False, {
        "child_pid": child.pid,
        "descriptor_pid": descriptor.pid,
        "child_returncode": child.returncode,
        "kernel_live": kernel_live,
        "pid_exists": psutil.pid_exists(descriptor.pid),
    }
    assert discover_recorders(runtime_dir) == []
    assert not (runtime_dir / f"{session_id}.json").exists()
    terminal = json.loads(
        (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert terminal["phase"] == "crashed"
    assert terminal["complete"] is False
    assert terminal["error_code"] == "recorder_process_exited"


def test_finalization_timeout_is_not_success(tmp_path: Path) -> None:
    child, capture_dir, runtime_dir = _start_child(tmp_path, stall=True)
    try:
        session_id = _wait_for_session(runtime_dir)
        with pytest.raises(CaptureControlError, match="finalization_timeout"):
            stop_recording(session_id, runtime_dir=runtime_dir, timeout=0.05)
        terminal = json.loads(
            (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert terminal["complete"] is False
        assert terminal["integrity_verified"] is False
        assert terminal["error_code"] == "finalization_timeout"
        stdout, stderr = _finish_child(child)
        assert child.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
        terminal = json.loads(
            (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert terminal["complete"] is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_wrong_token_and_replaced_instance_fail_closed(tmp_path: Path) -> None:
    state = _terminal_payload(tmp_path / "capture", str(uuid.uuid4()))
    stop_calls = 0

    def snapshot() -> dict:
        return dict(state)

    def stop(_timeout: float) -> dict:
        nonlocal stop_calls
        stop_calls += 1
        return dict(state)

    server = RecorderControlServer(
        capture_dir=state["capture_dir"],
        snapshot=snapshot,
        stop=stop,
        session_id=state["session_id"],
        runtime_dir=tmp_path / "runtime",
    ).start()
    try:
        descriptor = control._parse_descriptor(server.descriptor_path)  # type: ignore[arg-type]
        request = {
            "schema_version": control.CONTROL_SCHEMA_VERSION,
            "command": "status",
            "session_id": descriptor.session_id,
            "pid": descriptor.pid,
            "process_started_at": descriptor.process_started_at,
            "request_id": str(uuid.uuid4()),
            "issued_at": time.time(),
            "timeout_seconds": 1.0,
        }
        request["mac"] = control._message_mac("x" * 64, request)
        with socket.create_connection((descriptor.host, descriptor.port), timeout=2) as peer:
            peer.sendall(control._canonical_json(request) + b"\n")
            response = json.loads(control._recv_line(peer))
        assert response["ok"] is False
        assert response["error_code"] == "authentication_failed"
        assert "token" not in response

        request["request_id"] = str(uuid.uuid4())
        request["mac"] = control._message_mac(descriptor.token, request)
        with socket.create_connection((descriptor.host, descriptor.port), timeout=2) as peer:
            peer.sendall(control._canonical_json(request) + b"\n")
            first_response = json.loads(control._recv_line(peer))
        assert first_response["ok"] is True
        with socket.create_connection((descriptor.host, descriptor.port), timeout=2) as peer:
            peer.sendall(control._canonical_json(request) + b"\n")
            replay_response = json.loads(control._recv_line(peer))
        assert replay_response["ok"] is False
        assert replay_response["error_code"] == "authentication_failed"

        request["request_id"] = str(uuid.uuid4())
        request["process_started_at"] += 10
        request["mac"] = control._message_mac(descriptor.token, request)
        with socket.create_connection((descriptor.host, descriptor.port), timeout=2) as peer:
            peer.sendall(control._canonical_json(request) + b"\n")
            response = json.loads(control._recv_line(peer))
        assert response["ok"] is False
        assert response["error_code"] == "authentication_failed"
        assert stop_calls == 0
    finally:
        server.close()


def test_control_thread_start_failure_removes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _terminal_payload(tmp_path / "capture", str(uuid.uuid4()))

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    server = RecorderControlServer(
        capture_dir=state["capture_dir"],
        snapshot=lambda: dict(state),
        stop=lambda _timeout: dict(state),
        session_id=state["session_id"],
        runtime_dir=tmp_path / "runtime",
    )
    with pytest.raises(RuntimeError, match="thread start failure"):
        server.start()
    assert list((tmp_path / "runtime").glob("*.json")) == []


def test_unauthenticated_connections_have_a_hard_thread_limit(tmp_path: Path) -> None:
    state = _terminal_payload(tmp_path / "capture", str(uuid.uuid4()))
    server = RecorderControlServer(
        capture_dir=state["capture_dir"],
        snapshot=lambda: dict(state),
        stop=lambda _timeout: dict(state),
        session_id=state["session_id"],
        runtime_dir=tmp_path / "runtime",
    ).start()
    peers: list[socket.socket] = []
    try:
        descriptor = control._parse_descriptor(server.descriptor_path)  # type: ignore[arg-type]
        for _ in range(control._MAX_CONTROL_REQUEST_THREADS):
            peers.append(socket.create_connection((descriptor.host, descriptor.port), timeout=2))
        overflow = socket.create_connection((descriptor.host, descriptor.port), timeout=2)
        try:
            overflow.settimeout(2)
            assert overflow.recv(1) == b""
        finally:
            overflow.close()
    finally:
        for peer in peers:
            peer.close()
        server.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode assertion")
def test_runtime_secret_is_owner_only_and_wrong_owner_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _terminal_payload(tmp_path / "capture", str(uuid.uuid4()))
    server = RecorderControlServer(
        capture_dir=state["capture_dir"],
        snapshot=lambda: dict(state),
        stop=lambda _timeout: dict(state),
        session_id=state["session_id"],
        runtime_dir=tmp_path / "runtime",
    ).start()
    try:
        assert stat.S_IMODE((tmp_path / "runtime").stat().st_mode) == 0o700
        assert server.descriptor_path is not None
        assert stat.S_IMODE(server.descriptor_path.stat().st_mode) == 0o600
        raw = server.descriptor_path.read_text(encoding="utf-8")
        token = json.loads(raw)["token"]
        assert token not in repr(control._parse_descriptor(server.descriptor_path))

        actual_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
        with pytest.raises(PermissionError, match="does not own"):
            control._parse_descriptor(server.descriptor_path)
    finally:
        server.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL contract")
def test_macos_extended_acl_is_removed(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(runtime_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    descriptor = os.open(runtime_dir, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert control._macos_extended_acl_present(descriptor, runtime_dir)
    finally:
        os.close(descriptor)

    control._protect_path(runtime_dir, directory=True)
    descriptor = os.open(runtime_dir, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert not control._macos_extended_acl_present(descriptor, runtime_dir)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owner and DACL contract")
def test_windows_owner_check_rejects_a_foreign_sid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = control._secure_runtime_dir(tmp_path / "runtime")
    control._set_and_verify_windows_owner_acl(runtime_dir, _apply=False)

    monkeypatch.setattr(control, "_windows_current_user_sid", lambda: "S-1-5-18")
    with pytest.raises(PermissionError, match="current user does not own"):
        control._set_and_verify_windows_owner_acl(runtime_dir, _apply=False)


def test_multiple_sessions_require_exact_selection(tmp_path: Path) -> None:
    state_one = _terminal_payload(tmp_path / "one", str(uuid.uuid4()))
    state_two = _terminal_payload(tmp_path / "two", str(uuid.uuid4()))
    servers = [
        RecorderControlServer(
            capture_dir=state["capture_dir"],
            snapshot=lambda state=state: dict(state),
            stop=lambda _timeout, state=state: dict(state),
            session_id=state["session_id"],
            runtime_dir=tmp_path / "runtime",
        ).start()
        for state in (state_one, state_two)
    ]
    try:
        with pytest.raises(CaptureControlUnavailable, match="More than one"):
            status_recording(runtime_dir=tmp_path / "runtime")
        assert (
            status_recording(state_one["session_id"], runtime_dir=tmp_path / "runtime").session_id
            == state_one["session_id"]
        )
    finally:
        for server in servers:
            server.close()


def test_unauthenticated_descriptor_is_never_deleted(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    invalid = runtime_dir / f"{uuid.uuid4()}.json"
    invalid.write_text('{"pid": 999999}\n', encoding="utf-8")
    if sys.platform != "win32":
        invalid.chmod(0o600)
    assert discover_recorders(runtime_dir) == []
    assert invalid.exists()


def test_pid_reuse_descriptor_is_marked_crashed_and_removed(tmp_path: Path) -> None:
    runtime_dir = control._secure_runtime_dir(tmp_path / "runtime")
    capture_dir = tmp_path / "capture"
    session_id = str(uuid.uuid4())
    stale_start = psutil.Process().create_time() - 100.0
    terminal = _terminal_payload(capture_dir, session_id)
    terminal["process_started_at"] = stale_start
    control.write_terminal_state(capture_dir, terminal)
    descriptor = control._ControlDescriptor(
        session_id=session_id,
        pid=os.getpid(),
        process_started_at=stale_start,
        capture_dir=str(capture_dir),
        host="127.0.0.1",
        port=65534,
        created_at=time.time(),
        path=runtime_dir / f"{session_id}.json",
        token="x" * 64,
    )
    control._write_json_atomic(
        descriptor.path,
        descriptor.serialized(),
        owner_only=True,
    )

    assert discover_recorders(runtime_dir) == []
    assert not descriptor.path.exists()
    recovered = json.loads(
        (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert recovered["phase"] == "crashed"
    assert recovered["complete"] is False


def test_staged_complete_state_without_a_seal_is_marked_crashed(
    tmp_path: Path,
) -> None:
    capture_dir = tmp_path / "capture"
    session_id = str(uuid.uuid4())
    process_started_at = psutil.Process().create_time() - 100.0
    staged = _terminal_payload(capture_dir, session_id)
    staged.update(
        {
            "process_started_at": process_started_at,
            "phase": "complete",
            "complete": True,
            "integrity_verified": True,
            "finalized_at": time.time(),
        }
    )
    control.write_terminal_state(capture_dir, staged)
    descriptor = control._ControlDescriptor(
        session_id=session_id,
        pid=os.getpid(),
        process_started_at=process_started_at,
        capture_dir=str(capture_dir),
        host="127.0.0.1",
        port=65534,
        created_at=time.time(),
        path=tmp_path / "unused.json",
        token="x" * 64,
    )

    control._mark_crashed_if_bound(descriptor)

    recovered = json.loads(
        (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert recovered["phase"] == "crashed"
    assert recovered["complete"] is False
    assert recovered["integrity_verified"] is False


def test_exited_but_inspectable_process_is_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows can retain an exited process object while a handle stays open."""
    started_at = 1234.5

    class ExitedProcess:
        def create_time(self) -> float:
            return started_at

        def is_running(self) -> bool:
            raise AssertionError("Windows must use the kernel process signal")

        def status(self) -> str:
            raise AssertionError("Windows must use the kernel process signal")

    def fail_psutil(_pid: int) -> ExitedProcess:
        raise AssertionError("a signaled Windows process must bypass psutil")

    monkeypatch.setattr(control.psutil, "Process", fail_psutil)
    monkeypatch.setattr(control.sys, "platform", "win32")
    monkeypatch.setattr(control, "_windows_process_live", lambda _pid: False)

    assert control._process_instance_live(123, started_at) is False


def test_exact_running_process_instance_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = 1234.5

    class RunningProcess:
        def create_time(self) -> float:
            return started_at

        def is_running(self) -> bool:
            raise AssertionError("Windows must use the kernel process signal")

        def status(self) -> str:
            raise AssertionError("Windows must use the kernel process signal")

    monkeypatch.setattr(control.psutil, "Process", lambda _pid: RunningProcess())
    monkeypatch.setattr(control.sys, "platform", "win32")
    monkeypatch.setattr(control, "_windows_process_live", lambda _pid: True)

    assert control._process_instance_live(123, started_at) is True


def test_windows_signaled_process_object_is_terminal_and_handle_is_closed() -> None:
    class Kernel32:
        closed: list[int] = []

        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> int:
            assert access == 0x00100000
            assert inherit is False
            assert pid == 123
            return 456

        @staticmethod
        def WaitForSingleObject(handle: int, timeout: int) -> int:
            assert handle == 456
            assert timeout == 0
            return 0x00000000

        @classmethod
        def CloseHandle(cls, handle: int) -> bool:
            cls.closed.append(handle)
            return True

    kernel32 = Kernel32()

    assert control._windows_process_live(123, _kernel32=kernel32) is False
    assert kernel32.closed == [456]


@pytest.mark.parametrize("wait_result", [0x00000102, 0xFFFFFFFF])
def test_windows_live_and_unknown_wait_results_fail_closed(wait_result: int) -> None:
    class Kernel32:
        closed: list[int] = []

        @staticmethod
        def OpenProcess(_access: int, _inherit: bool, _pid: int) -> int:
            return 456

        @staticmethod
        def WaitForSingleObject(_handle: int, _timeout: int) -> int:
            return wait_result

        @classmethod
        def CloseHandle(cls, handle: int) -> bool:
            cls.closed.append(handle)
            return True

    kernel32 = Kernel32()

    expected = True if wait_result == 0x00000102 else None
    assert control._windows_process_live(123, _kernel32=kernel32) is expected
    assert kernel32.closed == [456]


def test_windows_access_denied_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel32:
        @staticmethod
        def OpenProcess(_access: int, _inherit: bool, _pid: int) -> int:
            return 0

    monkeypatch.setattr(control.ctypes, "get_last_error", lambda: 5, raising=False)

    assert control._windows_process_live(123, _kernel32=Kernel32()) is None


def test_recorder_failure_keeps_incomplete_state_and_removes_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_dir = tmp_path / "capture"
    runtime_dir = tmp_path / "runtime"

    def fail_after_ready(*, status_pipe, **_kwargs) -> None:
        status_pipe.send({"type": "record.started"})
        raise RuntimeError("synthetic recorder failure")

    monkeypatch.setattr(recorder_module, "record", fail_after_ready)
    recorder = recorder_module.Recorder(
        str(capture_dir),
        capture_video=False,
        capture_images=True,
        control_runtime_dir=str(runtime_dir),
    )
    with pytest.raises(RuntimeError, match="synthetic recorder failure"):
        with recorder:
            recorder.wait_for_ready(timeout=2)

    assert discover_recorders(runtime_dir) == []
    terminal = json.loads(
        (capture_dir / control.TERMINAL_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert terminal["phase"] == "failed"
    assert terminal["complete"] is False
    assert terminal["integrity_verified"] is False
    assert terminal["error_code"] == "recording_or_finalization_failed"


def test_complete_state_write_failure_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = recorder_module.Recorder(
        str(tmp_path / "capture"),
        capture_video=False,
        capture_images=True,
    )
    persisted: list[dict] = []

    def fail_complete_write(_capture_dir: str, payload: dict) -> Path:
        if payload.get("complete") is True:
            raise OSError("synthetic terminal metadata failure")
        persisted.append(dict(payload))
        return tmp_path / "capture" / control.TERMINAL_STATE_FILENAME

    monkeypatch.setattr(control, "write_terminal_state", fail_complete_write)
    with pytest.raises(OSError, match="terminal metadata failure"):
        recorder._transition_control(
            "complete",
            complete=True,
            integrity_verified=True,
            finalized=True,
        )

    rolled_back = recorder._control_payload()
    assert rolled_back["phase"] == "starting"
    assert rolled_back["complete"] is False
    assert rolled_back["integrity_verified"] is False

    recorder._transition_control(
        "failed",
        error_code="recording_or_finalization_failed",
        finalized=True,
    )
    recorder._finalized_event.set()
    returned = recorder._control_stop(0.1)
    assert returned["phase"] == "failed"
    assert returned["complete"] is False
    assert returned["integrity_verified"] is False
    assert persisted[-1]["phase"] == "failed"


def test_delayed_finalizing_message_cannot_erase_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = recorder_module.Recorder(
        str(tmp_path / "capture"),
        capture_video=False,
        capture_images=True,
    )
    persisted: list[dict] = []

    def retain_state(_capture_dir: str, payload: dict) -> Path:
        persisted.append(dict(payload))
        return tmp_path / "capture" / control.TERMINAL_STATE_FILENAME

    monkeypatch.setattr(control, "write_terminal_state", retain_state)
    recorder._transition_control("finalizing", error_code="finalization_timeout")
    recorder._transition_control("finalizing")

    assert recorder._control_payload()["error_code"] == "finalization_timeout"
    assert persisted[-1]["error_code"] == "finalization_timeout"


def test_completion_race_after_stop_timeout_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = recorder_module.Recorder(
        str(tmp_path / "capture"),
        capture_video=False,
        capture_images=True,
    )
    monkeypatch.setattr(
        control,
        "write_terminal_state",
        lambda _capture_dir, _payload: tmp_path / "capture" / "capture-state.json",
    )

    class CompletionRaceEvent:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            del timeout
            recorder._transition_control(
                "complete",
                complete=True,
                integrity_verified=True,
                finalized=True,
            )
            return False

    recorder._finalized_event = CompletionRaceEvent()  # type: ignore[assignment]

    returned = recorder._control_stop(0.01)

    assert returned["phase"] == "finalizing"
    assert returned["complete"] is False
    assert returned["integrity_verified"] is False
    assert returned["error_code"] == "finalization_timeout"
    assert recorder._control_payload()["phase"] == "complete"
    assert recorder._control_payload()["integrity_verified"] is True


def test_integrity_verification_rejects_missing_committed_events(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    _create_minimal_recording(capture_dir)
    recorder = recorder_module.Recorder(
        str(capture_dir),
        capture_video=False,
        capture_images=True,
    )
    recorder._num_action_events.value = 1

    with pytest.raises(RuntimeError, match="lost action_event events"):
        recorder._verify_completed_capture()


def test_completion_revalidates_the_exact_sealed_database_snapshot(
    tmp_path: Path,
) -> None:
    capture_dir = tmp_path / "capture"
    _create_minimal_recording(capture_dir)
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "black").save(output, format="PNG")
    png = output.getvalue()
    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    try:
        recording = session.query(crud.Recording).one()
        crud.insert_screenshot(
            session,
            recording,
            recording.timestamp + 1,
            {
                "source_ordinal": 1,
                "png_data": png,
                "png_sha256": hashlib.sha256(png).hexdigest(),
            },
        )
    finally:
        session.close()
        engine.dispose()

    recorder = recorder_module.Recorder(
        str(capture_dir),
        capture_video=False,
        capture_images=True,
    )
    recorder._num_screen_events.value = 1
    recorder._last_source_ordinal = 1
    recorder._verify_completed_capture()

    database = create_db(str(capture_dir / "recording.db"))[0]
    try:
        with database.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE screenshot SET png_sha256 = ?",
                ("f" * 64,),
            )
    finally:
        database.dispose()

    recorder._stage_completed_control_state()
    with pytest.raises(ValueError, match="PNG digest differs"):
        recorder._seal_completed_capture()

    status = recorder._control_payload()
    assert status["complete"] is False
    assert status["integrity_verified"] is False


def test_integrity_verification_rejects_malformed_browser_event(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    _create_minimal_recording(capture_dir, browser_messages=["not-an-object"])
    recorder = recorder_module.Recorder(
        str(capture_dir),
        capture_video=False,
        capture_images=True,
    )
    recorder._num_browser_events.value = 1

    with pytest.raises(RuntimeError, match="invalid browser event"):
        recorder._verify_completed_capture()


def test_browser_events_increment_the_persisted_count() -> None:
    event_q: queue.Queue = queue.Queue()
    event_q.put(
        recorder_module.Event(
            timestamp=time.time(),
            type="browser",
            data={"message": {"eventType": "navigate", "url": "https://example.test"}},
        )
    )
    queues = [queue.Queue() for _ in range(6)]
    counters = [multiprocessing.Value("i", 0) for _ in range(5)]
    terminate = multiprocessing.Event()
    terminate.set()

    with config_override(RecordingConfig(capture_browser_events=True)):
        recorder_module.process_events(
            event_q,
            queues[0],
            queues[1],
            queues[2],
            queues[3],
            queues[4],
            queues[5],
            SimpleNamespace(timestamp=time.time()),
            terminate,
            threading.Event(),
            *counters,
        )

    assert counters[3].value == 1
    assert queues[3].qsize() == 1
