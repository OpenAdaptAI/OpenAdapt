"""Owned-client liveness checks; no RDP server, display or credentials."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from openadapt_flow.backends.xfreerdp import RdpTarget, XFreeRDPError, XFreeRDPTransport


class Child:
    pid = 12345

    def __init__(self, result=None):
        self.result = result

    def poll(self):
        return self.result


def transport(child):
    value = XFreeRDPTransport(RdpTarget("synthetic.invalid"), password=lambda: "")
    value._client_proc = child
    value._session_client = child
    value._display = 99
    value._session_digest = "a" * 64
    return value


@pytest.mark.parametrize("child", [None, Child(0), Child(1)])
def test_identity_unavailable_for_missing_or_exited_owned_child(child):
    assert transport(child).session_identity() is None


def test_unavailable_child_poll_does_not_expose_cached_identity():
    def unavailable():
        raise OSError("synthetic process metadata unavailable")

    assert transport(SimpleNamespace(poll=unavailable)).session_identity() is None


def test_exited_real_owned_child_never_looks_live():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert transport(child).session_identity() is None


def test_alive_owned_child_retains_identity():
    assert transport(Child()).session_identity() == "a" * 64


def test_dead_client_refuses_before_frame_grab(monkeypatch):
    grabs = []
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: grabs.append(kw))
    with pytest.raises(XFreeRDPError):
        transport(Child(0)).framebuffer()
    assert not grabs


@pytest.mark.parametrize("replace", [False, True])
def test_client_exit_or_same_pid_replacement_during_grab_refuses(monkeypatch, replace):
    child = Child()
    value = transport(child)

    def grab(**kw):
        if replace:
            value._client_proc = Child()  # Same numeric PID, different owned start.
        else:
            child.result = 0
        return Image.new("RGB", (20, 20))

    monkeypatch.setattr(ImageGrab, "grab", grab)
    with pytest.raises(XFreeRDPError):
        value.framebuffer()


def test_connect_bootstrap_can_capture_before_digest_is_assigned(monkeypatch):
    value = transport(Child())
    value._session_digest = None
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: Image.new("RGB", (20, 20)))
    _, width, height = value.framebuffer()
    assert (width, height) == (20, 20)
    assert value.session_identity() is None


def test_dead_client_refuses_input_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(XFreeRDPError):
        transport(Child(0)).pointer(2, 3, "left", True)
    assert not calls


@pytest.mark.parametrize("replace", [False, True])
def test_exit_or_replacement_during_input_is_not_retried(monkeypatch, replace):
    child = Child()
    value = transport(child)
    calls = []

    def dispatch(*args, **kw):
        calls.append(args)
        if replace:
            value._client_proc = Child()
        else:
            child.result = 0

    monkeypatch.setattr(subprocess, "run", dispatch)
    with pytest.raises(XFreeRDPError):
        value.pointer(2, 3, "left", True)
    assert len(calls) == 1


def test_same_pid_new_child_cannot_inherit_old_connection_identity():
    value = transport(Child())
    value._client_proc = Child()
    assert value.session_identity() is None
