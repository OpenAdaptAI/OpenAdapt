"""Portable controls for the native fixture's readiness and process lifecycle."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _native_app_startup as startup


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    pid = 1234

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def _signal(path: Path, **changes):
    signal = {"event": "content_rendered", "pid": 1234, "hwnd": 5678, "rows": 2}
    signal.update(changes)
    path.write_text(json.dumps(signal))


@pytest.fixture
def fixture_state(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(startup, "monotonic", clock.monotonic)
    monkeypatch.setattr(startup, "sleep", clock.sleep)
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("")
    stderr.write_text("")
    return clock, Process(), tmp_path / "ready.json", stdout, stderr


def _wait(state, find_items, timeout_s=120):
    _, proc, ready, stdout, stderr = state
    return startup.wait_for_rendered_items(
        proc, ready, 2, find_items, stdout, stderr, timeout_s=timeout_s
    )


def test_render_signal_can_arrive_after_old_title_poll_bound(
    fixture_state, monkeypatch
):
    clock, _, ready, _, _ = fixture_state
    calls = []

    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 35 and not ready.exists():
            _signal(ready)

    monkeypatch.setattr(startup, "sleep", sleep)
    result = _wait(fixture_state, lambda hwnd: calls.append(hwnd) or ["a", "b"])
    assert result == ["a", "b"] and calls == [5678]
    assert 35 <= clock.now < 36  # simulated seconds, not a WPF timing claim


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": 999},
        {"pid": True},
        {"hwnd": 0},
        {"hwnd": "5678"},
        {"rows": 1},
        {"rows": True},
        {"event": "loaded"},
        {"extra": 1},
    ],
)
def test_signal_must_identify_owned_rendered_fixture(fixture_state, changes):
    _signal(fixture_state[2], **changes)
    with pytest.raises(startup.NativeAppStartupError, match="invalid ContentRendered"):
        _wait(fixture_state, lambda hwnd: pytest.fail("untrusted HWND was used"))


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"x" * 4097])
def test_malformed_or_oversized_signal_fails_without_discovery(fixture_state, raw):
    fixture_state[2].write_bytes(raw)
    with pytest.raises(startup.NativeAppStartupError, match="invalid ContentRendered"):
        _wait(fixture_state, lambda hwnd: pytest.fail("invalid signal was used"))


def test_ready_but_exited_process_is_not_accepted(fixture_state):
    _, proc, ready, _, stderr = fixture_state
    _signal(ready)
    proc.code = 7
    stderr.write_text("startup failed")
    with pytest.raises(startup.NativeAppStartupError, match="exit=7.*startup failed"):
        _wait(fixture_state, lambda hwnd: pytest.fail("dead process was used"))


def test_exit_during_discovery_is_not_accepted(fixture_state):
    _, proc, ready, _, _ = fixture_state
    _signal(ready)

    def discover(hwnd):
        proc.code = 8
        return ["a", "b"]

    with pytest.raises(startup.NativeAppStartupError, match="exited during UIA"):
        _wait(fixture_state, discover)


def test_signal_and_items_share_one_deadline(fixture_state, monkeypatch):
    clock, _, ready, _, _ = fixture_state

    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 0.8 and not ready.exists():
            _signal(ready)

    monkeypatch.setattr(startup, "sleep", sleep)
    with pytest.raises(startup.NativeAppStartupError, match="phase=owned window UIA"):
        _wait(fixture_state, lambda hwnd: ["only one row"], timeout_s=1)
    assert 1 <= clock.now < 1.1


def test_slow_discovery_cannot_return_after_deadline(fixture_state):
    clock, _, ready, _, _ = fixture_state
    _signal(ready)

    def discover(hwnd):
        clock.now = 2
        return ["a", "b"]

    with pytest.raises(startup.NativeAppStartupError, match="deadline exceeded"):
        _wait(fixture_state, discover, timeout_s=1)


def test_discovery_failure_contains_owned_diagnostics(fixture_state):
    _signal(fixture_state[2])

    def discover(hwnd):
        raise ValueError("wrong process for rendered HWND")

    with pytest.raises(startup.NativeAppStartupError, match="wrong process.*pid=1234"):
        _wait(fixture_state, discover)


@pytest.mark.parametrize("mode", ["ready", "exit", "hang"])
def test_real_owned_child_readiness_exit_and_cleanup(tmp_path, mode):
    script = tmp_path / "child.py"
    ready = tmp_path / "ready.json"
    stdout_path, stderr_path = tmp_path / "stdout.log", tmp_path / "stderr.log"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "mode, ready = sys.argv[1:]\n"
        "print('owned stdout', flush=True)\n"
        "print('owned stderr', file=sys.stderr, flush=True)\n"
        "if mode == 'exit': sys.exit(9)\n"
        "if mode == 'ready':\n"
        " time.sleep(0.05)\n"
        " path = pathlib.Path(ready)\n"
        " temporary = path.with_suffix('.tmp')\n"
        " temporary.write_text(json.dumps(dict(event='content_rendered', "
        "pid=os.getpid(), hwnd=5678, rows=2)))\n"
        " temporary.replace(path)\n"
        "time.sleep(60)\n"
    )
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(script), mode, str(ready)],
            stdout=stdout,
            stderr=stderr,
        )
        try:
            if mode == "ready":
                assert startup.wait_for_rendered_items(
                    proc,
                    ready,
                    2,
                    lambda hwnd: ["a", "b"],
                    stdout_path,
                    stderr_path,
                    timeout_s=5,
                ) == ["a", "b"]
            else:
                reason = "owned child exited" if mode == "exit" else "deadline exceeded"
                with pytest.raises(
                    startup.NativeAppStartupError, match=reason
                ) as error:
                    startup.wait_for_rendered_items(
                        proc,
                        ready,
                        2,
                        lambda hwnd: pytest.fail("not ready"),
                        stdout_path,
                        stderr_path,
                        timeout_s=1,
                    )
                assert "owned stdout" in str(error.value)
                assert "owned stderr" in str(error.value)
                if mode == "exit":
                    assert "exit=9" in str(error.value)
        finally:
            startup.stop_owned_process(proc)
        assert proc.poll() is not None


def test_diagnostic_tails_are_bounded(tmp_path):
    path = tmp_path / "stderr"
    path.write_bytes(b"prefix" + b"x" * 5000 + b"end")
    tail = startup._tail(path)
    assert len(tail) == 4096 and tail.endswith("end") and "prefix" not in tail


def test_cleanup_escalates_only_owned_process():
    events = []

    class Stubborn(Process):
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append(("wait", timeout))
            if self.code is None:
                raise subprocess.TimeoutExpired("owned fixture", timeout)
            return self.code

        def kill(self):
            events.append("kill")
            self.code = -9

    proc = Stubborn()
    startup.stop_owned_process(proc, timeout_s=0.5)
    assert events == ["terminate", ("wait", 0.5), "kill", ("wait", 0.5)]


@pytest.mark.parametrize("identity", ["owned", "other_pid", "other_title"])
def test_wpf_fixture_uses_signalled_handle_and_owned_window(
    tmp_path, monkeypatch, identity
):
    from tests import test_native_census_real_apps as native

    calls = []
    items = [SimpleNamespace(ControlTypeName="ListItemControl") for _ in range(2)]
    listing = SimpleNamespace(Exists=lambda *args: True, GetChildren=lambda: items)
    window = SimpleNamespace(
        ProcessId=1234 if identity != "other_pid" else 999,
        Name=native.TITLE if identity != "other_title" else "other window",
        ListControl=lambda **kwargs: listing,
    )
    auto = SimpleNamespace(ControlFromHandle=lambda hwnd: calls.append(hwnd) or window)

    class Owned(Process):
        def terminate(self):
            self.code = -15

        def wait(self, timeout):
            return self.code

    proc = Owned()

    def spawn(argv, stdout, stderr):
        assert all(flag in argv for flag in ("-STA", "-NonInteractive", "-NoProfile"))
        _signal(Path(argv[argv.index("-ReadyPath") + 1]))
        return proc

    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("OPENADAPT_TEST_WINDOWS_UIA", "1")
    monkeypatch.setattr(
        native, "pytest", SimpleNamespace(importorskip=lambda name: auto)
    )
    monkeypatch.setattr(native, "subprocess", SimpleNamespace(Popen=spawn))
    fixture = native.wpf_worklist.__wrapped__(tmp_path)
    try:
        _, start = next(fixture)
        if identity == "owned":
            assert start([{}, {}]) == items
        else:
            with pytest.raises(startup.NativeAppStartupError, match="owned worklist"):
                start([{}, {}])
        assert calls == [5678]
    finally:
        fixture.close()
    assert proc.poll() == -15
