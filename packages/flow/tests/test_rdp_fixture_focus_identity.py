"""Software-only controls for exact X wrapper proof; never open a display."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from tests.e2e import rdp_fixture_focus as focus


def _proof():
    return {"root": 10, "parent": 30, "wrapper_root": 10,
            "wrapper_parent": 10, "children": [20]}


def test_exact_wrapper_relation_and_focus():
    wrapper = focus._verified_wrapper(20, _proof())
    assert wrapper == 30
    assert focus._focus_matches(30, wrapper, literal_parent=False)
    assert focus._focus_matches(1, wrapper, literal_parent=True)
    assert not focus._focus_matches(30, wrapper, literal_parent=True)


@pytest.mark.parametrize("field,value", [
    ("root", 0), ("root", 1), ("root", True), ("root", None),
    ("root", 2**32), ("root", "10"), ("parent", 20), ("parent", 10),
    ("parent", None), ("wrapper_root", 11), ("wrapper_parent", 11),
    ("children", []), ("children", [21]), ("children", [20, 21]),
    ("children", [20, 20]), ("children", [True]), ("children", (20,)),
    ("children", None),
])
def test_wrong_or_unknown_relation_refuses(field, value):
    proof = _proof()
    proof[field] = value
    with pytest.raises(ValueError, match="wrapper"):
        focus._verified_wrapper(20, proof)


@pytest.mark.parametrize("field", list(_proof()))
def test_missing_relation_refuses(field):
    proof = _proof()
    del proof[field]
    with pytest.raises(ValueError, match="shape"):
        focus._verified_wrapper(20, proof)


def test_extra_relation_field_refuses():
    proof = _proof()
    proof["claimed_owned"] = True
    with pytest.raises(ValueError, match="shape"):
        focus._verified_wrapper(20, proof)


@pytest.mark.parametrize("window", [0, 1, 10, 20, 21, 31, None, True, "30", 2**32])
def test_root_child_foreign_or_unknown_focus_refuses(window):
    assert not focus._focus_matches(window, 30, literal_parent=False)


@pytest.mark.parametrize("toplevel", [0, 1, True, None, "20", 2**32, 10, 30])
def test_invalid_or_aliased_toplevel_refuses(toplevel):
    with pytest.raises(ValueError, match="wrapper"):
        focus._verified_wrapper(toplevel, _proof())


def test_read_only_child_contract(monkeypatch):
    calls = []
    env = {"DISPLAY": ":123", "PATH": "/usr/bin:/bin"}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(_proof()).encode(), b"")
    monkeypatch.setattr(focus.subprocess, "run", run)
    assert focus._read_wrapper(env, 20) == 30
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/python3", "-I", "-S", str(focus.Path(focus.__file__).resolve()),
                    "--tree", ":123", "20"]
    assert kwargs == {"env": env, "capture_output": True, "timeout": 3, "check": True}


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(-11, ["synthetic"], stderr=b"private X failure"),
    subprocess.TimeoutExpired(["synthetic"], 3),
    OSError("synthetic unavailable"),
])
def test_query_failure_preserves_original_exception(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(focus.subprocess, "run", fail)
    with pytest.raises(type(failure)) as actual:
        focus._read_wrapper({"DISPLAY": ":123"}, 20)
    assert actual.value is failure


@pytest.mark.parametrize("payload", [b"x" * 1025, b"not-json", b"null", b"{}"])
def test_bad_child_output_refuses(monkeypatch, payload):
    monkeypatch.setattr(focus.subprocess, "run", lambda *a, **k:
        subprocess.CompletedProcess(a, 0, payload, b""))
    with pytest.raises((ValueError, TypeError)):
        focus._read_wrapper({"DISPLAY": ":123"}, 20)


class _Process:
    def __init__(self, pipe=False):
        self.stdout = io.BytesIO() if pipe else None
        self.returncode = None
        self.terminated = self.waited = False
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = -15
    def wait(self, **kwargs):
        self.waited = True
        return self.returncode


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(-11, ["synthetic"], stderr=b"private X failure"),
    subprocess.TimeoutExpired(["synthetic"], 3),
])
def test_query_crash_timeout_still_closes_owned_processes(monkeypatch, tmp_path, failure):
    processes = []
    class Pipe(io.BytesIO):
        def fileno(self):
            return 99
    def popen(*args, **kwargs):
        proc = _Process(pipe=len(processes) == 1)
        if proc.stdout is not None:
            proc.stdout = Pipe()
        processes.append(proc)
        return proc
    ready = {"toplevel": 20, "wm_frame": 20, "mapped": True,
             "geometry": [0, 0, 1280, 800], "tk_version": "8.6.14"}
    lines = iter([b"123", json.dumps(ready).encode()])
    monkeypatch.setattr(focus.subprocess, "Popen", popen)
    monkeypatch.setattr(focus, "_line", lambda *args: next(lines))
    monkeypatch.setattr(focus, "_owned", lambda *args: True)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(focus.subprocess, "run", fail)
    with pytest.raises(AssertionError, match="fixed safe evidence"):
        focus._condition(tmp_path, literal_parent=False)
    assert len(processes) == 2
    assert all(p.terminated and p.waited for p in processes)
    summary = json.loads((tmp_path / "fixture-focus/focus-evidence.json").read_text())
    assert summary["status"] == "ERROR"
    assert summary["stage"] == "wrapper_relation"
    assert summary["exception_type"] == type(failure).__name__
    assert summary["pipe_cleanup_complete"] is True
    assert all(p["started"] and p["waited"] for p in summary["cleanup"].values())
    assert "private X failure" not in json.dumps(summary)
    assert "toplevel" not in summary and "wrapper" not in summary


@pytest.mark.parametrize("changed_before_query", [True, False])
def test_changed_owned_server_refuses_and_cleans_up(monkeypatch, tmp_path, changed_before_query):
    processes = []
    class Pipe(io.BytesIO):
        def fileno(self):
            return 99
    def popen(*args, **kwargs):
        proc = _Process()
        if processes:
            proc.stdout = Pipe()
        processes.append(proc)
        return proc
    ready = {"toplevel": 20, "wm_frame": 20, "mapped": True,
             "geometry": [0, 0, 1280, 800], "tk_version": "8.6.14"}
    lines = iter([b"123", json.dumps(ready).encode()])
    owned = iter([True, False] if changed_before_query else [True, True, False])
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if "--tree" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_proof()).encode(), b"")
        assert argv == ["/usr/bin/xdotool", "getwindowfocus", "-f"]
        return subprocess.CompletedProcess(argv, 0, b"30\n", b"")
    monkeypatch.setattr(focus.subprocess, "Popen", popen)
    monkeypatch.setattr(focus, "_line", lambda *args: next(lines))
    monkeypatch.setattr(focus, "_owned", lambda *args: next(owned))
    monkeypatch.setattr(focus.subprocess, "run", run)
    with pytest.raises(AssertionError, match="fixed safe evidence"):
        focus._condition(tmp_path, literal_parent=False)
    assert len(calls) == (0 if changed_before_query else 2)
    assert all(p.terminated and p.waited for p in processes)
    summary = json.loads((tmp_path / "fixture-focus/focus-evidence.json").read_text())
    assert summary["status"] == ("ERROR" if changed_before_query else "FAILED")
    if not changed_before_query:
        assert summary["server_owned"] is False
        assert summary["raw_focus_matches_verified_wrapper"] is True
    assert summary["pipe_cleanup_complete"] is True
