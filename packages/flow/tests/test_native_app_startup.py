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
    items = []
    points = {}
    for index in range(2):
        rect = SimpleNamespace(
            left=10, top=20 * index, right=100, bottom=20 * index + 10
        )
        cell = SimpleNamespace(BoundingRectangle=rect)
        item = SimpleNamespace(
            ControlTypeName="ListItemControl",
            ProcessId=1234,
            GetChildren=lambda cell=cell: [cell],
        )
        items.append(item)
        points[(55, 20 * index + 5)] = item
    listing = SimpleNamespace(Exists=lambda *args: True, GetChildren=lambda: items)
    window = SimpleNamespace(
        ProcessId=1234 if identity != "other_pid" else 999,
        Name=native.TITLE if identity != "other_title" else "other window",
        ListControl=lambda **kwargs: listing,
    )
    auto = SimpleNamespace(
        ControlFromHandle=lambda hwnd: calls.append(hwnd) or window,
        ControlFromPoint=lambda x, y: points[(x, y)],
        ControlsAreSame=lambda left, right: left is right,
    )

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


@pytest.mark.parametrize("fault", ["empty", "missing", "other_pid", "other_row"])
def test_wpf_points_are_not_ready_when_the_physical_target_is_unproven(fault):
    from tests import test_native_census_real_apps as native

    rect = SimpleNamespace(
        left=10, top=20, right=10 if fault == "empty" else 100, bottom=30
    )
    item = SimpleNamespace(
        GetChildren=lambda: [SimpleNamespace(BoundingRectangle=rect)]
    )
    hit = SimpleNamespace(
        ProcessId=999 if fault == "other_pid" else 1234,
        ControlTypeName="TextControl",
        GetParentControl=lambda: None,
    )
    auto = SimpleNamespace(
        ControlFromPoint=lambda *point: None if fault == "missing" else hit,
        ControlsAreSame=lambda left, right: left is right,
    )
    ready, detail = native._wpf_points_ready(auto, [item], 1234)
    assert ready is False
    assert "row=0" in detail


def test_wpf_points_accept_the_owned_row_ancestor_only():
    from tests import test_native_census_real_apps as native

    rect = SimpleNamespace(left=10, top=20, right=100, bottom=30)
    item = SimpleNamespace(
        GetChildren=lambda: [SimpleNamespace(BoundingRectangle=rect)]
    )
    hit = SimpleNamespace(
        ProcessId=1234,
        ControlTypeName="TextControl",
        GetParentControl=lambda: item,
    )
    auto = SimpleNamespace(
        ControlFromPoint=lambda *point: hit,
        ControlsAreSame=lambda left, right: left is right,
    )
    assert native._wpf_points_ready(auto, [item], 1234)[0] is True


def test_wpf_readiness_waits_for_the_point_before_returning_items(fixture_state):
    from tests import test_native_census_real_apps as native

    clock, _, ready_path, _, _ = fixture_state
    _signal(ready_path)
    rect = SimpleNamespace(left=10, top=20, right=100, bottom=30)
    item = SimpleNamespace(
        GetChildren=lambda: [SimpleNamespace(BoundingRectangle=rect)]
    )
    outside = SimpleNamespace(
        ProcessId=999, ControlTypeName="PaneControl", GetParentControl=lambda: None
    )
    inside = SimpleNamespace(
        ProcessId=1234, ControlTypeName="TextControl", GetParentControl=lambda: item
    )
    hits = iter([outside, inside, inside])
    auto = SimpleNamespace(
        ControlFromPoint=lambda *point: next(hits),
        ControlsAreSame=lambda left, right: left is right,
    )

    def find_items(hwnd):
        assert hwnd == 5678
        return (
            [item, item]
            if native._wpf_points_ready(auto, [item, item], 1234)[0]
            else None
        )

    assert _wait(fixture_state, find_items) == [item, item]
    assert 0.1 <= clock.now < 0.2


# Every history uses only fake UIA objects and the existing simulated clock.
# The native fixture's process launcher is replaced before its start is called.
def _probe_sequence(tmp_path, monkeypatch, fixture_state, states):
    from tests import test_native_census_real_apps as native

    clock = fixture_state[0]
    current = "not_started"
    attempts = []
    items, points = [], {}
    for index in range(3):
        rect = SimpleNamespace(
            left=55, top=75 + 20 * index, right=85, bottom=91 + 20 * index
        )
        item = SimpleNamespace(
            ControlTypeName="ListItemControl",
            GetChildren=lambda rect=rect: [SimpleNamespace(BoundingRectangle=rect)],
        )
        items.append(item)
        points[(70, 83 + 20 * index)] = item

    def rows():
        if current == "rows_error":
            raise RuntimeError("synthetic row enumeration failure")
        return items[:2] if current == "short_list" else items

    def exists(*args):
        if current == "exists_error":
            raise RuntimeError("synthetic list existence failure")
        return current != "list_missing"

    listing = SimpleNamespace(Exists=exists, GetChildren=rows)

    def list_control(**kwargs):
        if current == "list_error":
            raise RuntimeError("synthetic list lookup failure")
        return listing

    window = SimpleNamespace(
        ProcessId=1234, Name=native.TITLE, ListControl=list_control
    )

    def from_handle(hwnd):
        nonlocal current
        assert hwnd == 5678
        current = states[min(len(attempts), len(states) - 1)]
        attempts.append(current)
        clock.now += 40  # Simulated discovery cost, not a longer startup budget.
        if current == "window_error":
            raise RuntimeError("synthetic window lookup failure")
        window.ProcessId = 999 if current == "other_pid" else 1234
        window.Name = (
            "different synthetic window" if current == "other_title" else native.TITLE
        )
        return None if current == "window_missing" else window

    def from_point(x, y):
        if (x, y) == (70, 123):
            if current == "point_error":
                raise RuntimeError("synthetic point lookup failure")
            if current == "foreign_point":
                return SimpleNamespace(ProcessId=999, ControlTypeName="WindowControl")
        return SimpleNamespace(
            ProcessId=1234,
            ControlTypeName="TextControl",
            GetParentControl=lambda: points[(x, y)],
        )

    auto = SimpleNamespace(
        ControlFromHandle=from_handle,
        ControlFromPoint=from_point,
        ControlsAreSame=lambda left, right: left is right,
    )

    class Owned(Process):
        def terminate(self):
            self.code = -15

        def wait(self, timeout):
            return self.code

    proc = Owned()

    def spawn(argv, stdout, stderr):
        _signal(Path(argv[argv.index("-ReadyPath") + 1]), rows=3)
        return proc

    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("OPENADAPT_TEST_WINDOWS_UIA", "1")
    monkeypatch.setattr(
        native, "pytest", SimpleNamespace(importorskip=lambda name: auto)
    )
    monkeypatch.setattr(native, "subprocess", SimpleNamespace(Popen=spawn))
    fixture = native.wpf_worklist.__wrapped__(tmp_path)
    result, error = None, None
    try:
        _, start = next(fixture)
        try:
            result = start([{}, {}, {}])
        except startup.NativeAppStartupError as exc:
            error = str(exc)
    finally:
        fixture.close()
    assert proc.poll() == -15
    return result, error, attempts, clock.now


_PRIOR_PROBES = ("foreign_point", "window_missing", "list_missing", "short_list")
_PROBE_STATES = {
    "window_missing": "window_missing",
    "list_missing": "list_missing",
    "short_list": "row_count_mismatch",
    "window_error": "window_lookup",
    "list_error": "list_lookup",
    "exists_error": "list_existence",
    "other_pid": "window_identity",
    "other_title": "window_identity",
    "rows_error": "row_enumeration",
    "point_error": "points_unproven",
    "foreign_point": "points_unproven",
}


@pytest.mark.parametrize("prior", _PRIOR_PROBES)
@pytest.mark.parametrize("current,expected", _PROBE_STATES.items())
def test_each_probe_replaces_earlier_observations(
    tmp_path, monkeypatch, fixture_state, prior, current, expected
):
    result, error, attempts, elapsed = _probe_sequence(
        tmp_path, monkeypatch, fixture_state, [prior, current]
    )
    assert result is None and error is not None
    detail = error.rsplit("; latest_probe_state=", 1)[-1]
    if current != "foreign_point":
        assert "hit_pid=999" not in detail  # An earlier foreign point is not current.
    assert detail.startswith(expected), detail
    failed = (current.endswith("_error") and current != "point_error") or current in (
        "other_pid",
        "other_title",
    )
    attempt = 2 if failed else 3
    started = 40.1 * (attempt - 1)
    finished = started + 40
    assert attempts[-1] == current and len(attempts) == attempt
    assert f"latest_probe_attempt={attempt};" in error
    fields = dict(
        part.split("=", 1)
        for part in error.split("; ")
        if part.startswith("latest_probe_")
    )
    assert float(fields["latest_probe_started_s"]) == pytest.approx(started)
    assert float(fields["latest_probe_finished_s"]) == pytest.approx(finished)
    assert fields["latest_probe_outcome"] == ("error" if failed else "no_items")
    assert elapsed == pytest.approx(finished)
    assert "UIA discovery failed" in error if failed else "deadline exceeded" in error


@pytest.mark.parametrize("prior", [None, *_PRIOR_PROBES])
def test_prior_refusal_does_not_change_owned_success(
    tmp_path, monkeypatch, fixture_state, prior
):
    states = ["ready"] if prior is None else [prior, "ready"]
    result, error, attempts, elapsed = _probe_sequence(
        tmp_path, monkeypatch, fixture_state, states
    )
    assert error is None and result is not None and len(result) == 3
    assert attempts == states
    assert elapsed == pytest.approx(40 * len(states) + 0.1 * (len(states) - 1))
    assert elapsed < 120


def test_no_discovery_has_no_attributed_probe(fixture_state):
    with pytest.raises(startup.NativeAppStartupError) as caught:
        _wait(
            fixture_state, lambda hwnd: pytest.fail("no rendered signal"), timeout_s=1
        )
    error = str(caught.value)
    assert "latest_probe_attempt=0;" in error
    assert "latest_probe_started_s=None;" in error
    assert "latest_probe_finished_s=None;" in error
    assert "latest_probe_outcome=not_attempted;" in error
