"""``run --self-repair`` outside a browser: repair.session opens each session.

The app here is a real child process (``sleep``) and the backend is a stub the
factory returns, so these test the CLI wiring: which sessions start, what is
refused, and how the replayer is told the target. Repair's own rules are in
tests/test_repair_element_identity.py; a live run is in
benchmark/repair_every_backend.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from openadapt_flow.cli_self_repair import CliTarget, _refusal
from openadapt_flow.deployment import RepairSection, RepairSessionSpec
from openadapt_flow.repair.self_repair import SelfRepairRefused

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
EXITS = [sys.executable, "-c", "pass"]
FAILS = [sys.executable, "-c", "raise SystemExit(3)"]


def _args(**kwargs: Any) -> SimpleNamespace:
    values = dict(
        managed_dispatch_file=None,
        qualification_authority_file=None,
        _qualification_case_execution=None,
        pin_digest=None,
        repair_requalify=None,
        hold_on_halt=None,
        save_healed_to=None,
        record_video=None,
        repair_agent="pkg:agent",
        repair_screen_access_by="ops-lead",
        headed=False,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def _backend_cfg(kind: str = "macos") -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        browser_cdp_endpoint=None,
        browser_page_url=None,
        deployment_surface=None,
    )


def _target(spec: RepairSessionSpec | None, *, kind: str = "macos", calls=None):
    cfg = SimpleNamespace(
        repair=RepairSection(session=spec) if spec else RepairSection(),
        backend=SimpleNamespace(headed=False),
        runtime=SimpleNamespace(pixel_verify_enabled=False),
        policy=SimpleNamespace(policy="clinical-write"),
    )
    profile = SimpleNamespace(profile=SimpleNamespace(value="standard"))

    def run_replayer(backend, **kwargs):
        (calls if calls is not None else []).append(kwargs)
        return "report"

    return CliTarget(
        _args(),
        bundle=Path("."),
        url="",
        cfg=cfg,
        backend_cfg=_backend_cfg(kind),
        gate_kwargs={"profile_contract": profile},
        params={},
        worklists={},
        effect_verifier=object(),
        api_actuator=None,
        durable=True,
        allow_egress=False,
        run_replayer=run_replayer,
        approval_gate_for_run=lambda *a, **k: None,
    )


class StubBackend:
    """Binds to the newest window: the started app's, unless ``pid`` is set."""

    started: list[int] = []

    def __init__(self, pid: int | None = None) -> None:
        self.closed = False
        self.pid = pid

    def _resolve_window(self, refresh: bool = False) -> Any:
        return SimpleNamespace(pid=self.pid or _newest_child())

    def close(self) -> None:
        self.closed = True


def _newest_child() -> int:
    import subprocess

    out = subprocess.run(
        ["pgrep", "-n", "-P", str(__import__("os").getpid())],
        capture_output=True,
        text=True,
    ).stdout.split()
    return int(out[0]) if out else -1


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie child still answers kill(0); reap it first
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


@pytest.fixture
def factory(monkeypatch):
    built: list[StubBackend] = []

    def build(cfg, **kwargs):
        backend = StubBackend()
        built.append(backend)
        return backend

    monkeypatch.setattr("openadapt_flow.backends.factory.build_backend", build)
    return built


# -- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["macos", "windows", "linux", "rdp", "citrix"])
def test_non_browser_run_needs_repair_session(kind) -> None:
    reason = _refusal(_args(), _backend_cfg(kind), None, RepairSection())
    assert reason is not None and "repair.session" in reason
    spec = RepairSessionSpec(command=SLEEP)
    assert (
        _refusal(_args(), _backend_cfg(kind), None, RepairSection(session=spec)) is None
    )


def test_browser_rules_are_unchanged() -> None:
    web = _backend_cfg("web")
    assert _refusal(_args(), web, None, None) == "--url is required"
    assert _refusal(_args(), web, "http://app", None) is None
    attached = SimpleNamespace(**{**vars(web), "browser_cdp_endpoint": "ws://x"})
    assert "attached browser" in _refusal(_args(), attached, "http://app", None)


def test_session_spec_validates() -> None:
    with pytest.raises(ValidationError):
        RepairSessionSpec(command=[])
    with pytest.raises(ValidationError):
        RepairSessionSpec(command=SLEEP, fault_commands={"x": []})


# -- sessions -----------------------------------------------------------------


def test_each_session_starts_the_app_and_stops_it(factory) -> None:
    target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.3))
    session = target.open(None)
    assert len(factory) == 1
    pid = factory[0]._resolve_window().pid
    assert _alive(pid)
    session.close()
    session.close()  # idempotent: self-repair may close a session twice
    assert factory[0].closed
    assert not _alive(pid)


def test_a_window_the_app_did_not_open_is_refused(monkeypatch, tmp_path) -> None:
    import subprocess

    # The operator's (or the halted session's) copy is already running; the
    # backend binds to it instead of the started app.
    other = subprocess.Popen(SLEEP)
    try:
        monkeypatch.setattr(
            "openadapt_flow.backends.factory.build_backend",
            lambda cfg, **kw: StubBackend(pid=other.pid),
        )
        target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.3))
        with pytest.raises(SelfRepairRefused, match="does not belong"):
            target.open(None)
        assert _alive(other.pid)  # never ours to stop
    finally:
        other.kill()
        other.wait()


def test_a_backend_that_cannot_name_its_window_owner_is_refused(monkeypatch) -> None:
    class Agent:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "openadapt_flow.backends.factory.build_backend", lambda cfg, **kw: Agent()
    )
    target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.2), kind="windows")
    with pytest.raises(SelfRepairRefused, match="does not belong"):
        target.open(None)


def test_close_ends_a_wrapper_scripts_app(factory, tmp_path) -> None:
    pidfile = tmp_path / "app.pid"
    wrapper = [
        "/bin/sh",
        "-c",
        f"{sys.executable} -c 'import time; time.sleep(60)' & echo $! > {pidfile}; wait",
    ]
    target = _target(RepairSessionSpec(command=wrapper, ready_s=0.5))
    session = target.open(None)
    app_pid = int(pidfile.read_text())
    assert _alive(app_pid)
    session.close()
    import time

    time.sleep(0.3)
    assert not _alive(app_pid)


def test_a_failing_close_command_is_refused(factory) -> None:
    target = _target(RepairSessionSpec(command=SLEEP, close_command=FAILS, ready_s=0.2))
    session = target.open(None)
    with pytest.raises(SelfRepairRefused, match="close_command exited 3"):
        session.close()


def test_desktop_target_runs_one_session_at_a_time() -> None:
    assert _target(RepairSessionSpec(command=SLEEP)).one_session_at_a_time
    assert not _target(None, kind="web").one_session_at_a_time


def test_fault_probe_uses_its_fault_command(factory, tmp_path) -> None:
    marker = tmp_path / "fault"
    fault = [
        sys.executable,
        "-c",
        f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(60)",
    ]
    target = _target(
        RepairSessionSpec(command=SLEEP, fault_commands={"other": fault}, ready_s=0.5)
    )
    assert target.fault_probes() == ("other",)
    session = target.open("other")
    assert marker.exists()
    session.close()
    with pytest.raises(SelfRepairRefused, match="no fault command"):
        target.open("missing")


def test_an_app_that_exits_at_once_is_not_a_fresh_session(factory) -> None:
    target = _target(RepairSessionSpec(command=EXITS, ready_s=0.3))
    with pytest.raises(SelfRepairRefused, match="exited at once"):
        target.open(None)
    assert factory == []


def test_a_backend_that_cannot_bind_stops_the_app(monkeypatch) -> None:
    started: list[int] = []

    def build(cfg, **kwargs):
        started.append(_newest_child())
        raise ValueError("no unique window")

    monkeypatch.setattr("openadapt_flow.backends.factory.build_backend", build)
    target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.3))
    with pytest.raises(SelfRepairRefused, match="could not bind"):
        target.open(None)
    assert started and not _alive(started[0])


# -- the run ------------------------------------------------------------------


def test_profile_reaches_the_record_check_cover() -> None:
    assert _target(RepairSessionSpec(command=SLEEP)).profile == "standard"


def test_desktop_execute_names_the_desktop_target(monkeypatch) -> None:
    calls: list[dict] = []
    target = _target(RepairSessionSpec(command=SLEEP), calls=calls)
    monkeypatch.setattr(
        "openadapt_flow.run_gate.evaluate_run_gate",
        lambda *a, **k: SimpleNamespace(passed=True),
    )
    monkeypatch.setattr(
        "openadapt_flow.run_gate.build_runtime_authorization", lambda *a, **k: "auth"
    )
    monkeypatch.setattr(
        "openadapt_flow.__main__._surface_target_origin_or_none",
        lambda cfg: "macos://Chart",
    )
    session = SimpleNamespace(backend=StubBackend())
    target.execute(object(), Path("patched"), session, Path("run"), approval_gate="g")
    (kwargs,) = calls
    assert kwargs["execution_target_kind"] == "macos"
    assert kwargs["execution_target_origin"] == "macos://Chart"
    assert "execution_entry_url" not in kwargs
    assert kwargs["approval_gate"] == "g"
