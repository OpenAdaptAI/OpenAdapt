"""Headless FreeRDP session profile, transport wiring and session pool.

Unit tests run anywhere. The live round trip needs Xvfb, xfreerdp3,
freerdp-shadow-cli3, xdotool and python3-tk and runs only when
``OAFLOW_XFREERDP_E2E=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import pytest

from openadapt_flow.backends import xfreerdp as X
from openadapt_flow.backends.xfreerdp import (
    RdpTarget,
    SessionPool,
    SessionProfile,
    XFreeRDPTransport,
)


def test_profile_pins_display_settings():
    args = SessionProfile().client_args()
    for a in ("/size:1280x800", "/bpp:24", "/scale:100", "/scale-desktop:100"):
        assert a in args
    for a in ("-gfx", "-rfx", "-nsc", "-fonts", "-wallpaper", "-themes", "+clipboard"):
        assert a in args


def test_profile_digest_changes_with_any_setting():
    base = SessionProfile()
    assert base.sha256 == SessionProfile().sha256
    assert base.sha256 != SessionProfile(scale=140).sha256
    assert base.sha256 != SessionProfile(font_smoothing=True).sha256
    assert base.sha256 != SessionProfile(width=1920, height=1080).sha256


@pytest.mark.parametrize("kw", [{"scale": 125}, {"desktop_scale": 50}, {"width": 10}])
def test_profile_rejects_bad_values(kw):
    with pytest.raises(ValueError):
        SessionProfile(**kw)


def test_password_goes_to_stdin_not_argv(monkeypatch):
    spawned: list[tuple[list[str], dict]] = []

    class Proc:
        pid = 4242
        returncode = None

        def __init__(self, argv, **kw):
            spawned.append((argv, kw))
            self.stdin = self if kw.get("stdin") is subprocess.PIPE else None
            self.written = b""

        def write(self, data):
            self.written += data

        def close(self):
            pass

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    procs: list[Proc] = []

    def fake_popen(argv, **kw):
        p = Proc(argv, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(X.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(X.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(X, "_claim_display", lambda: 99)
    monkeypatch.setattr(XFreeRDPTransport, "_wait", lambda self, ready, t: None)
    t = XFreeRDPTransport(
        RdpTarget("10.0.0.5", 3389, username="svc"), password=lambda: "s3cret-synthetic"
    )
    t.connect()
    client = next(p for p in procs if p.stdin is not None)
    argv = " ".join(client.__dict__.get("argv", spawned[procs.index(client)][0]))
    assert "s3cret-synthetic" not in argv
    assert "/from-stdin:force" in argv and "/u:svc" in argv
    assert client.written == b"s3cret-synthetic\n"
    env = spawned[procs.index(client)][1]["env"]
    assert "s3cret-synthetic" not in " ".join(env.values())
    assert env["DISPLAY"] == ":99"
    assert "s3cret" not in repr(t.__dict__.get("target"))
    assert t.session_identity() and len(t.session_identity()) == 64
    t.disconnect()
    assert t.session_identity() is None


def test_key_mapping(monkeypatch):
    calls = []
    t = XFreeRDPTransport(RdpTarget("h"), password=lambda: "")
    monkeypatch.setattr(t, "_xdotool", lambda *a: calls.append(a))
    t.key("enter", True)
    t.key("enter", False)
    t.key("pagedown", True)
    t.key("a", True)
    t.key("a", False)
    assert calls[0] == ("keydown", "Return")
    assert calls[1] == ("keyup", "Return")
    assert calls[2] == ("keydown", "Next")
    assert calls[3][0] == "type" and calls[3][-1] == "a"
    assert len(calls) == 4
    with pytest.raises(ValueError):
        t.key("nosuchkey", True)


class FakeSession:
    def __init__(self, profile=SessionProfile()):
        self.profile = profile
        self.connected = False
        self.busy = 0
        self.max_busy = 0
        self.lock = threading.Lock()

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False


def test_pool_runs_jobs_in_parallel_one_per_session():
    sessions = [FakeSession() for _ in range(3)]
    # The first three jobs wait for each other. A pool that ran them one at a
    # time would break the barrier, so passing it proves they ran together,
    # without a wall-clock bound that a loaded host can miss.
    together = threading.Barrier(len(sessions), timeout=30)

    def work(s, job):
        if job < len(sessions):
            together.wait()
        with s.lock:
            s.busy += 1
            s.max_busy = max(s.max_busy, s.busy)
        time.sleep(0.05)
        with s.lock:
            s.busy -= 1
        if job == 4:
            raise RuntimeError("synthetic failure")
        return job * 2

    with SessionPool(sessions) as pool:  # type: ignore[arg-type]
        assert all(s.connected for s in sessions)
        run = pool.run(list(range(9)), work)
    assert not any(s.connected for s in sessions)
    assert run.jobs == 9 and run.ok == 8
    assert all(s.max_busy == 1 for s in sessions)
    assert {r.session for r in run.results} == {0, 1, 2}
    assert not together.broken
    failed = [r for r in run.results if not r.ok]
    assert failed[0].error == "RuntimeError"
    assert run.summary()["failed"] == 1


def test_pool_refuses_mixed_profiles():
    with pytest.raises(ValueError):
        SessionPool([FakeSession(), FakeSession(SessionProfile(scale=140))])  # type: ignore[list-item]


_E2E = os.environ.get("OAFLOW_XFREERDP_E2E") == "1" and all(
    shutil.which(t) for t in ("Xvfb", "xfreerdp3", "freerdp-shadow-cli3", "xdotool")
)


@pytest.mark.skipif(not _E2E, reason="set OAFLOW_XFREERDP_E2E=1 with FreeRDP tools")
def test_live_round_trip_through_backend():
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "benchmark" / "rdp_pool"
    sys.path.insert(0, str(here))
    import run_pool_throughput as bench  # type: ignore[import-not-found]

    data = bench.accounts(bench.SEED)
    jobs = sorted(data)[:3]
    out = bench.run_size(1, jobs, data)
    assert out["ok"] >= 2


def test_session_file_round_trip(tmp_path):
    from openadapt_flow.backends.xfreerdp import (
        RdpGateway,
        RdpTarget,
        SessionProfile,
        load_session,
        save_session,
    )

    target = RdpTarget(host="10.20.0.15", port=3390, username="svc-replay")
    profile = SessionProfile(width=1440, height=900)
    gateway = RdpGateway(host="rdgw.internal.example")
    path = save_session(tmp_path / "s.json", target, profile, gateway)
    body = path.read_text()
    assert "password" not in body
    assert load_session(path) == (target, profile, gateway)


def test_session_file_rejects_edited_profile(tmp_path):
    import json

    import pytest

    from openadapt_flow.backends.xfreerdp import (
        RdpTarget,
        load_session,
        save_session,
    )

    path = save_session(tmp_path / "s.json", RdpTarget(host="10.20.0.15"))
    data = json.loads(path.read_text())
    data["profile"]["lossless"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="profile_sha256"):
        load_session(path)
    data["schema"] = "other"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not a"):
        load_session(path)
