"""Focus failure diagnostics preserve refusal; all OS/display boundaries doubled."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from openadapt_flow.qualify import linux_pixel_fault_renderer as renderer
from openadapt_flow.qualify import linux_pixel_fault_task as tasks
from tests.test_linux_pixel_fault_task import IDENTITY, request

FOCUS_STAGES = (
    "focus_missing_tool",
    "focus_nonzero",
    "focus_timeout",
    "focus_malformed",
    "focus_none",
    "focus_pointer_root",
)


def command_env(tmp_path):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DISPLAY": ":999"}


@pytest.mark.parametrize("stage_name", [None, "capture", "render", "map", "liveness"])
def test_success_keeps_exact_query_contract_and_restores_outer_stage(
    tmp_path, monkeypatch, stage_name
):
    env = command_env(tmp_path)
    calls = []

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b"  76543\n")

    monkeypatch.setattr(renderer.subprocess, "run", query)
    stage = [stage_name] if stage_name is not None else None
    result = (
        renderer.focus(env, stage=stage) if stage is not None else renderer.focus(env)
    )
    assert result == 76543
    assert stage == ([stage_name] if stage_name is not None else None)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/xdotool", "getwindowfocus"]
    assert kwargs == {"env": env, "capture_output": True, "timeout": 3, "check": True}
    assert kwargs["env"] is env


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("missing", "focus_missing_tool"),
        ("nonzero", "focus_nonzero"),
        ("timeout", "focus_timeout"),
        ("permission", "map"),
    ],
)
@pytest.mark.parametrize("with_stage", [False, True])
def test_query_raises_identical_original_exception_once(
    tmp_path, monkeypatch, kind, expected, with_stage
):
    argv = ["/usr/bin/xdotool", "getwindowfocus"]
    errors = {
        "missing": FileNotFoundError("private tool path"),
        "nonzero": subprocess.CalledProcessError(
            1, argv, output=b"private stdout", stderr=b"private stderr"
        ),
        "timeout": subprocess.TimeoutExpired(
            argv, 3, output=b"private stdout", stderr=b"private stderr"
        ),
        "permission": PermissionError("private denied path"),
    }
    error = errors[kind]
    calls = []

    def query(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    monkeypatch.setattr(renderer.subprocess, "run", query)
    stage = ["map"]
    with pytest.raises(type(error)) as caught:
        if with_stage:
            renderer.focus(command_env(tmp_path), stage=stage)
        else:
            renderer.focus(command_env(tmp_path))
    assert caught.value is error and len(calls) == 1
    assert stage == ([expected] if with_stage else ["map"])


@pytest.mark.parametrize(
    "output,expected",
    [
        (b"0\n", "focus_none"),
        (b"1\n", "focus_pointer_root"),
        (b"", "focus_malformed"),
        (b"private contents\n", "focus_malformed"),
        (b"-1", "focus_malformed"),
        (b"12junk", "focus_malformed"),
    ],
)
def test_original_greater_than_one_guard_still_refuses(
    tmp_path, monkeypatch, output, expected
):
    calls = []

    def query(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(renderer.subprocess, "run", query)
    stage = ["capture"]
    with pytest.raises(ValueError, match="^display focus unavailable$"):
        renderer.focus(command_env(tmp_path), stage=stage)
    assert stage == [expected] and calls == [True]


@pytest.mark.parametrize("stage_name", FOCUS_STAGES)
def test_cli_exports_fixed_focus_token_without_raw_query_error(
    tmp_path, monkeypatch, capsys, stage_name
):
    env = command_env(tmp_path)
    errors = {
        "focus_missing_tool": FileNotFoundError("private missing path"),
        "focus_nonzero": subprocess.CalledProcessError(
            1, ["private argv"], output=b"private output", stderr=b"private error"
        ),
        "focus_timeout": subprocess.TimeoutExpired(
            ["private argv"], 3, output=b"private output"
        ),
    }
    outputs = {
        "focus_malformed": b"private arbitrary text",
        "focus_none": b"0",
        "focus_pointer_root": b"1",
    }

    def query(*args, **kwargs):
        if stage_name in errors:
            raise errors[stage_name]
        return SimpleNamespace(stdout=outputs[stage_name])

    def rendering(_envelope, *, stage):
        stage[0] = "capture"
        renderer.focus(env, stage=stage)
        pytest.fail("refused focus must not reach native rendering")

    monkeypatch.setattr(renderer.subprocess, "run", query)
    monkeypatch.setattr(renderer, "render", rendering)
    monkeypatch.setattr(
        renderer.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))
    )
    assert renderer.main() == 1
    output = capsys.readouterr()
    assert output.out == f"refused:{stage_name}\n" and output.err == ""
    assert "private" not in output.out


def owned_protocol_receiver(tmp_path, monkeypatch, stream):
    # Synthetic process observations; real bounded pipe/selector and real parent
    # protocol. No constructor/native-launch guard is edited or skipped.
    monkeypatch.setattr(renderer, "display_identity", lambda _display: dict(IDENTITY))
    monkeypatch.setattr(
        tasks.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected child")
    )
    owner = tasks.LinuxPixelTasks(
        server=SimpleNamespace(pid=IDENTITY["pid"], poll=lambda: None),
        children=(),
        display=":999",
        viewport=(300, 200),
        home=tmp_path,
        session_binding_sha256="b" * 64,
    )
    handle = tasks.LinuxPixelTaskHandle(
        SimpleNamespace(stdout=stream, poll=lambda: 1),
        "c" * 32,
        "b" * 64,
        renderer.identity_digest(IDENTITY),
        owner._source_digest,
        "d" * 64,
        time.monotonic() + 5,
    )
    owner._handles.append(handle)
    return owner, handle


@pytest.mark.parametrize("stage_name", FOCUS_STAGES)
def test_real_parent_accepts_only_fixed_refusal_not_readiness(
    tmp_path, monkeypatch, stage_name
):
    readfd, writefd = os.pipe()
    with os.fdopen(readfd, "rb", buffering=0) as stream:
        try:
            os.write(writefd, f"refused:{stage_name}\n".encode())
        finally:
            os.close(writefd)
        owner, handle = owned_protocol_receiver(tmp_path, monkeypatch, stream)
        assert owner.wait_ready(handle, timeout_s=0.5) is None
        assert handle.refusal_stage == stage_name and handle.ready_status is None


@pytest.mark.parametrize(
    "payload",
    [
        b"refused:arbitrary private data\n",
        b"refused:focus_timeout\nready:other\n",
        b"refused:focus_timeout:private\n",
        b"refused:focus_timeout\nrefused:focus_timeout\n",
        b"refused:focus_timeout",
        b"refused:\xff\n",
    ],
)
def test_parent_refuses_unknown_partial_or_mixed_output(tmp_path, monkeypatch, payload):
    readfd, writefd = os.pipe()
    with os.fdopen(readfd, "rb", buffering=0) as stream:
        try:
            os.write(writefd, payload)
        finally:
            os.close(writefd)
        owner, handle = owned_protocol_receiver(tmp_path, monkeypatch, stream)
        assert owner.wait_ready(handle, timeout_s=0.5) is None
        assert handle.refusal_stage is None and handle.ready_status is None


@pytest.mark.parametrize("failure_stage,changed_call", [("map", 3), ("liveness", 4)])
def test_successful_changed_focus_remains_original_map_or_liveness_refusal(
    tmp_path, monkeypatch, failure_stage, changed_call
):
    calls = []
    destroyed = []
    env = command_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(renderer, "display_identity", lambda _: dict(IDENTITY))

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b"24" if len(calls) == changed_call else b"23")

    monkeypatch.setattr(renderer.subprocess, "run", query)

    class Root:
        def withdraw(self):
            pass

        def winfo_screenwidth(self):
            return 300

        def winfo_screenheight(self):
            return 200

        def overrideredirect(self, *args):
            pass

        def attributes(self, *args):
            pass

        def geometry(self, *args):
            pass

        def deiconify(self):
            pass

        def update(self):
            pass

        def winfo_rootx(self):
            return 0

        def winfo_rooty(self):
            return 0

        def winfo_width(self):
            return 52

        def winfo_height(self):
            return 52

        def after(self, _delay, callback):
            self.callback = callback

        def mainloop(self):
            self.callback()

        def quit(self):
            pass

        def destroy(self):
            destroyed.append(True)

    class Canvas:
        def __init__(self, *args, **kwargs):
            pass

        def pack(self):
            pass

        def create_image(self, *args, **kwargs):
            pass

    tk = SimpleNamespace(
        Tk=lambda **kwargs: Root(),
        Canvas=Canvas,
        PhotoImage=lambda **kwargs: SimpleNamespace(
            width=lambda: 52, height=lambda: 52
        ),
    )
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    data = request()
    envelope = {
        "display": env["DISPLAY"],
        "identity": dict(IDENTITY),
        "request": data,
        "binding": data["connection_digest"],
        "viewport": [300, 200],
    }
    stage = ["request"]
    with pytest.raises(
        ValueError,
        match="overlay changed display focus or geometry|display changed while overlay was live",
    ):
        renderer.render(envelope, stage=stage)
    assert stage == [failure_stage] and destroyed == [True]
    assert len(calls) == changed_call
    assert all(
        argv == ["/usr/bin/xdotool", "getwindowfocus"]
        and kwargs["timeout"] == 3
        and kwargs["check"] is True
        for argv, kwargs in calls
    )
