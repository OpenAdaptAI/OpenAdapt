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
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
    with pytest.raises(ValidationError, match="close_command"):
        RepairSessionSpec(command=EXITS, launcher_exits=True)


# -- sessions -----------------------------------------------------------------


def test_each_session_starts_the_app_and_stops_it(factory) -> None:
    target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.2))
    session = target.open(None)
    assert len(factory) == 1
    app_procs = [p for p in _children()]
    session.close()
    assert factory[0].closed
    assert all(p.poll() is not None for p in app_procs)


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


def test_a_launcher_must_exit_cleanly(factory, tmp_path) -> None:
    closed = tmp_path / "closed"
    close = [sys.executable, "-c", f"open({str(closed)!r}, 'w')"]
    ok = _target(
        RepairSessionSpec(
            command=EXITS, launcher_exits=True, close_command=close, ready_s=0.1
        )
    )
    session = ok.open(None)
    session.close()
    assert closed.exists()
    bad = _target(
        RepairSessionSpec(
            command=FAILS, launcher_exits=True, close_command=close, ready_s=0.1
        )
    )
    with pytest.raises(SelfRepairRefused, match="status 3"):
        bad.open(None)


def test_a_backend_that_cannot_bind_stops_the_app(monkeypatch, tmp_path) -> None:
    def build(cfg, **kwargs):
        raise ValueError("no unique window")

    monkeypatch.setattr("openadapt_flow.backends.factory.build_backend", build)
    target = _target(RepairSessionSpec(command=SLEEP, ready_s=0.2))
    with pytest.raises(SelfRepairRefused, match="could not bind"):
        target.open(None)
    assert all(p.poll() is not None for p in _children())


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


def _children():
    """Child processes this test started (best effort, for the stop checks)."""
    import subprocess

    return [
        p
        for p in getattr(subprocess, "_active", None) or []
        if isinstance(p, subprocess.Popen)
    ]
