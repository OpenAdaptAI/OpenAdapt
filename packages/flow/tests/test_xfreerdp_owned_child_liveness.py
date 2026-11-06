"""Real owned-child liveness with all GUI, RDP and input endpoints intercepted.

Only a harmless Python process runs. The production connect path owns that
process and creates its session digest; no display or native client is opened.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from openadapt_flow.backend import ActionDeliveryUncertain
from openadapt_flow.backends import xfreerdp as X
from openadapt_flow.backends.rdp_backend import FreeRDPBackend

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="X11 transport child-control pipes require POSIX"
)


class _OwnedPythonChild:
    def __init__(self, popen, cwd):
        self._popen = popen
        self._cwd = cwd
        self._stop_read, self._stop_write = os.pipe()
        self._ready_read, self._ready_write = os.pipe()
        self._fds = {
            self._stop_read,
            self._stop_write,
            self._ready_read,
            self._ready_write,
        }
        self.proc = None

    def _close(self, fd):
        if fd in self._fds:
            self._fds.remove(fd)
            os.close(fd)

    def start(self):
        assert self.proc is None
        code = (
            "import os,sys; "
            "os.write(int(sys.argv[2]), b'ready\\n'); "
            "os.close(int(sys.argv[2])); "
            "os.read(int(sys.argv[1]), 1)"
        )
        self.proc = self._popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                code,
                str(self._stop_read),
                str(self._ready_write),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(self._stop_read, self._ready_write),
            cwd=self._cwd,
            env={"PATH": os.defpath},
        )
        self._close(self._stop_read)
        self._close(self._ready_write)
        readable, _, _ = select.select([self._ready_read], [], [], 10)
        assert readable, "owned Python child did not become ready"
        assert os.read(self._ready_read, 6) == b"ready\n"
        self._close(self._ready_read)
        assert self.proc.poll() is None
        return self.proc

    def exit(self):
        assert self.proc is not None and self.proc.poll() is None
        os.write(self._stop_write, b"x")
        self._close(self._stop_write)
        assert self.proc.wait(timeout=10) == 0

    def close(self):
        for fd in tuple(self._fds):
            self._close(fd)
        if self.proc is not None and self.proc.poll() is None:
            # Only the exact child created above is ever signalled.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


class _DisplayStub:
    def __init__(self):
        self.result = None

    def poll(self):
        return self.result

    def terminate(self):
        self.result = 0

    def wait(self, timeout=None):
        return self.result


@pytest.fixture
def connected_child(monkeypatch, tmp_path):
    # Save the real constructor before intercepting the transport's module.
    owned = _OwnedPythonChild(subprocess.Popen, tmp_path)
    state = SimpleNamespace(
        capture_hook=None, dispatch_hook=None, captures=0, dispatches=[], spawns=[]
    )
    display = _DisplayStub()
    released = []
    image = Image.new("RGB", (20, 20), "white")
    image.putpixel((1, 1), (0, 0, 0))

    def popen(argv, **kwargs):
        name = Path(argv[0]).name
        state.spawns.append(name)
        if name == "Xvfb":
            return display
        assert name == "xfreerdp3", "unexpected external process attempt"
        assert kwargs.get("stdin") is subprocess.PIPE
        return owned.start()

    def grab(**_kwargs):
        state.captures += 1
        hook, state.capture_hook = state.capture_hook, None
        if hook is not None:
            hook()
        return image.copy()

    def dispatch(argv, **_kwargs):
        assert argv[0] == "xdotool", "unexpected external input program"
        state.dispatches.append(tuple(argv[1:]))
        hook, state.dispatch_hook = state.dispatch_hook, None
        if hook is not None:
            hook()
        return subprocess.CompletedProcess(argv, 0)

    original_exists = Path.exists

    def exists(path):
        if str(path) == "/tmp/.X11-unix/X9876":
            return True  # Readiness of the intercepted display only.
        return original_exists(path)

    monkeypatch.setattr(X, "_require", lambda name: name)
    monkeypatch.setattr(X, "_claim_display", lambda: 9876)
    monkeypatch.setattr(X, "_release_display", lambda number: released.append(number))
    monkeypatch.setattr(X.shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(X.subprocess, "Popen", popen)
    monkeypatch.setattr(X.subprocess, "run", dispatch)
    monkeypatch.setattr(ImageGrab, "grab", grab)
    transport = X.XFreeRDPTransport(
        X.RdpTarget("synthetic.invalid"), password=lambda: "", connect_timeout_s=5
    )
    try:
        transport.connect()
        assert state.spawns == ["Xvfb", "xfreerdp3"]
        assert owned.proc is not None and owned.proc.poll() is None
        digest = transport.session_identity()
        assert digest is not None and len(digest) == 64
        yield transport, owned, state, digest
    finally:
        transport.disconnect()
        owned.close()
        assert released == [9876]


def test_live_owned_child_preserves_frame_identity_and_ordinary_click(connected_child):
    transport, owned, state, digest = connected_child
    backend = FreeRDPBackend(transport, connect=False)
    assert backend.screenshot()
    assert backend.click(3, 4) is None
    assert owned.proc.poll() is None
    assert transport.session_identity() == digest
    assert state.dispatches == [
        ("mousemove", "3", "4", "mousedown", "1"),
        ("mousemove", "3", "4", "mouseup", "1"),
    ]


def test_owned_child_exit_during_capture_refuses_the_returned_frame(connected_child):
    transport, owned, state, _digest = connected_child
    before = state.captures
    state.capture_hook = owned.exit
    with pytest.raises(X.XFreeRDPError):
        transport.framebuffer()
    assert owned.proc.returncode == 0
    assert state.captures == before + 1
    assert transport.session_identity() is None
    assert state.dispatches == []


def test_owned_child_exit_during_click_is_uncertain_and_never_retried(connected_child):
    transport, owned, state, _digest = connected_child
    backend = FreeRDPBackend(transport, connect=False)
    backend.screenshot()
    state.dispatch_hook = owned.exit
    returned = []
    with pytest.raises(ActionDeliveryUncertain) as caught:
        returned.append(backend.click(3, 4))
    assert returned == []  # No successful delivery return or receipt.
    assert caught.value.operation == "rdp_click"
    assert caught.value.cause_type == "XFreeRDPError"
    assert owned.proc.returncode == 0
    assert transport.session_identity() is None
    # The best-effort release is checked before dispatch, so it cannot emit
    # an input through the dead child; no second actual input call is made.
    assert state.dispatches == [("mousemove", "3", "4", "mousedown", "1")]
