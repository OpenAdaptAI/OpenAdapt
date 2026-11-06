"""Finite diagnostics from already-captured errors; no display or native query."""

from __future__ import annotations

import io
import os
import subprocess
from types import SimpleNamespace

import pytest

from openadapt_flow.qualify import linux_pixel_fault_renderer as renderer
from tests.test_linux_pixel_focus_diagnostics import (
    command_env,
    owned_protocol_receiver,
)

BADWINDOW = (
    b"X Error of failed request:  BadWindow (invalid Window parameter)\n"
    b"  Major opcode of failed request:  20 (X_GetProperty)\n"
    b"  Resource id in failed request:  0x1\n"
    b"  Serial number of failed request:  18\n"
    b"  Current serial number in output stream:  19\n"
)
POINTER_ROOT = (
    b"XGetInputFocus returned the focused window of 1. "
    b"This is likely a bug in the X server.\n"
)
DISPLAY_OPEN = b"Error: Can't open display: (null)\nFailed creating new xdo instance\n"
SANE_QUERY = (
    b"xdo_get_focused_window_sane failed (code=1)\nxdo_focus_window reported an error\n"
)
KNOWN = (
    (BADWINDOW, "focus_badwindow_property"),
    (POINTER_ROOT + BADWINDOW, "focus_reported_pointer_root"),
    (DISPLAY_OPEN, "focus_display_open"),
    (SANE_QUERY, "focus_sane_query"),
)


class Untrusted:
    def __str__(self):
        pytest.fail("must not stringify diagnostic payload")

    def __len__(self):
        pytest.fail("must not inspect untrusted length")


@pytest.mark.parametrize(
    "stderr",
    [
        None,
        "private contents",
        bytearray(DISPLAY_OPEN),
        Untrusted(),
        b"",
        b"private stderr",
        b"x" * 8193,
        DISPLAY_OPEN + b"x" * 8193,
        BADWINDOW + DISPLAY_OPEN,
        SANE_QUERY + BADWINDOW,
        POINTER_ROOT,
        POINTER_ROOT + BADWINDOW.replace(b"0x1", b"0x2"),
        BADWINDOW[:-1],
        BADWINDOW.replace(b"X_GetProperty", b"X_QueryTree"),
        BADWINDOW.replace(b"0x1", b"private XID"),
        BADWINDOW.replace(b"18", b"9" * 21),
        BADWINDOW.replace(b"\n", b"\r\n"),
        b"private prefix\n" + DISPLAY_OPEN,
        DISPLAY_OPEN + b"private suffix\n",
    ],
)
def test_unknown_malformed_oversized_or_mixed_remains_generic(stderr):
    assert renderer.focus_failure_stage(stderr) == "focus_nonzero"


@pytest.mark.parametrize("stderr,expected", KNOWN)
def test_real_error_identity_exact_query_and_no_raw_output(
    tmp_path, monkeypatch, capsys, stderr, expected
):
    env = command_env(tmp_path)
    argv = ["/usr/bin/xdotool", "getwindowfocus"]
    error = subprocess.CalledProcessError(
        1, argv, output=b"PRIVATE_OUTPUT_SENTINEL", stderr=stderr
    )
    calls = []

    def query(actual_argv, **kwargs):
        calls.append((actual_argv, kwargs))
        raise error

    monkeypatch.setattr(renderer.subprocess, "run", query)
    stage = ["capture"]
    with pytest.raises(subprocess.CalledProcessError) as caught:
        renderer.focus(env, stage=stage)
    assert caught.value is error
    assert error.stderr is stderr
    assert error.output == b"PRIVATE_OUTPUT_SENTINEL"
    assert stage == [expected]
    assert calls == [
        (argv, {"env": env, "capture_output": True, "timeout": 3, "check": True})
    ]
    assert calls[0][1]["env"] is env
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("stderr,expected", KNOWN)
def test_cli_exports_only_fixed_refusal_and_parent_never_accepts_ready(
    tmp_path, monkeypatch, capsys, stderr, expected
):
    error = subprocess.CalledProcessError(
        1, ["PRIVATE_ARGV"], output=b"PRIVATE_OUTPUT", stderr=stderr
    )

    def query(*args, **kwargs):
        raise error

    def render(_envelope, *, stage):
        stage[0] = "capture"
        renderer.focus(command_env(tmp_path), stage=stage)
        pytest.fail("nonzero focus must not render")

    monkeypatch.setattr(renderer.subprocess, "run", query)
    monkeypatch.setattr(renderer, "render", render)
    monkeypatch.setattr(
        renderer.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))
    )
    assert renderer.main() == 1
    output = capsys.readouterr()
    assert output == (f"refused:{expected}\n", "")
    readfd, writefd = os.pipe()
    with os.fdopen(readfd, "rb", buffering=0) as stream:
        try:
            os.write(writefd, output.out.encode())
        finally:
            os.close(writefd)
        owner, handle = owned_protocol_receiver(tmp_path, monkeypatch, stream)
        assert owner.wait_ready(handle, timeout_s=0.5) is None
        assert handle.refusal_stage == expected
        assert handle.ready_status is None
