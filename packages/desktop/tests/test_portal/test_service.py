"""Portal lifecycle: fail closed, supervise the console, expose the seam."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.portal.service import PortalError, PortalService, _parse_console_banner

#: A deployment config shaped like the ones that make this file sensitive: it
#: carries a reusable credential, not just a backend name and a URL.
DEPLOYMENT = {
    "backend": {
        "kind": "rdp",
        "rdp_host": "vdi.example.internal",
        "rdp_username": "svc-openadapt",
        "rdp_password": "correct-horse-battery-staple",
    }
}


class FakeProcess:
    """A stand-in for the attended-console subprocess."""

    def __init__(self, banner: str) -> None:
        self.stdout = io.StringIO(
            "operator console on http://127.0.0.1:7863  [ACTIONS ENABLED]\n"
            f"{banner}\n"
        )
        self.stderr = io.StringIO()
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def config(**overrides) -> EngineConfig:
    return EngineConfig(**overrides)


def configured(data_dir: Path, **overrides) -> EngineConfig:
    """A config whose data dir carries the operator's deployment target."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "deployment.json").write_text(json.dumps(DEPLOYMENT), encoding="utf-8")
    return EngineConfig(data_dir=data_dir, **overrides)


# --------------------------------------------------------------- fail closed


def test_start_refuses_an_incompletely_configured_ingress() -> None:
    service = PortalService(
        config(
            portal_ingress_mode="customer_ingress",
            portal_public_origin="https://openadapt.clinic.example",
            portal_ingress_acknowledged=False,
        )
    )
    with pytest.raises(PortalError, match="acknowledged"):
        service.start()
    assert service.running is False
    # It stays stopped; it does not fall back to a wider bind address.
    assert service.status()["running"] is False


def test_describe_ingress_explains_a_misconfiguration_without_starting() -> None:
    service = PortalService(config(portal_ingress_mode="lan"))
    described = service.describe_ingress()
    assert described["configured"] is False
    assert described["reachable_from_phone"] is False
    assert described["loopback_only"] is True
    assert "Unknown portal ingress mode" in described["error"]


def test_the_default_posture_is_loopback_only() -> None:
    described = PortalService(config()).describe_ingress()
    assert described["configured"] is True
    assert described["mode"] == "loopback"
    assert described["loopback_only"] is True
    assert described["reachable_from_phone"] is False


def test_a_console_that_never_announces_itself_fails_loud(monkeypatch, tmp_path) -> None:
    class Silent(FakeProcess):
        def __init__(self) -> None:
            super().__init__("")
            self.stdout = io.StringIO("")

    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    service = PortalService(
        configured(tmp_path / "data"), popen=lambda *a, **k: Silent()
    )
    with pytest.raises(PortalError, match="console"):
        service.start()
    assert service.running is False
    # The staged secret does not survive a console that failed to start.
    assert list((tmp_path / "data" / "portal").glob(".deployment-*.yaml")) == []


def test_a_missing_flow_runtime_fails_loud(monkeypatch) -> None:
    monkeypatch.setattr("engine.portal.service._flow_command", lambda _bin: None)
    service = PortalService(config())
    with pytest.raises(PortalError, match="not available"):
        service.start()


# ------------------------------------------------------------- console banner


def test_the_console_banner_parser_is_strict() -> None:
    token = "a" * 43
    assert _parse_console_banner(f"  http://127.0.0.1:7863/#token={token}") == (
        7863,
        token,
    )
    assert _parse_console_banner(f"  http://localhost:9000/#token={token}") == (
        9000,
        token,
    )
    for hostile in (
        f"  http://evil.example:7863/#token={token}",
        f"  https://127.0.0.1:7863/#token={token}",
        "  http://127.0.0.1:7863/#token=short",
        "operator console on http://127.0.0.1:7863  [read-only]",
        "",
    ):
        assert _parse_console_banner(hostile) is None


# -------------------------------------------------------- end-to-end lifecycle


def _fake_client(monkeypatch, *, fail_first: int = 0) -> list[list[str]]:
    """Install a console client that answers the portal's allowlisted routes.

    ``fail_first`` makes the first N ``session`` calls raise the exact transport
    failure a not-yet-bound uvicorn produces.
    """
    from engine.portal.flow_client import FlowConsoleUnavailable, FlowResponse

    state = {"session_calls": 0}

    class FakeClient:
        def __init__(self, port, access_token, csrf_token="", client=None):
            self.port = port
            self.access_token = access_token
            self.csrf_token = csrf_token

        def request(self, route, **kwargs):
            if route == "session":
                state["session_calls"] += 1
                if state["session_calls"] <= fail_first:
                    raise FlowConsoleUnavailable(
                        "The local OpenAdapt decision service is not reachable"
                    )
                return FlowResponse(200, {"csrf_token": "csrf"}, None, "application/json")
            if route == "notification":
                return FlowResponse(
                    200,
                    {"title": "MRN 4417092", "body": "Coverage: active", "open_count": 2},
                    None,
                    "application/json",
                )
            return FlowResponse(200, [], None, "application/json")

    monkeypatch.setattr("engine.portal.service.FlowConsoleClient", FakeClient)
    return state


def _started(monkeypatch, tmp_path, **overrides) -> PortalService:
    token = "b" * 43
    process = FakeProcess(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    _fake_client(monkeypatch)
    service = PortalService(
        configured(tmp_path / "data", **overrides), popen=lambda *a, **k: process
    )
    service.start()
    return service


def test_a_started_loopback_portal_reports_its_posture(monkeypatch, tmp_path) -> None:
    service = _started(monkeypatch, tmp_path)
    try:
        status = service.status()
        assert status["running"] is True
        assert status["ingress"]["loopback_only"] is True
        assert status["ingress"]["reachable_from_phone"] is False
        assert status["port"] and status["port"] > 0
        assert status["devices"] == []
    finally:
        service.stop()
    assert service.status()["running"] is False


def test_a_loopback_pairing_says_a_phone_cannot_reach_it(monkeypatch, tmp_path) -> None:
    service = _started(monkeypatch, tmp_path)
    try:
        pairing = service.create_pairing()
        assert pairing["reachable_from_phone"] is False
        assert "loopback-only" in pairing["note"]
        assert pairing["url"].startswith("http://127.0.0.1:")
        assert "#c=oapp_" in pairing["url"]
        assert "secret" not in pairing
        # The QR is rendered locally as an inert data URI, never as raw markup
        # the Desktop window would have to inject.
        qr = pairing["qr_svg"]
        assert qr is None or qr.startswith("data:image/png;base64,")
        assert qr is None or "<svg" not in qr
    finally:
        service.stop()


def test_pairing_operations_require_a_running_portal() -> None:
    service = PortalService(config())
    for call in (
        service.create_pairing,
        lambda: service.approve_pairing("x", "ABC-123"),
        lambda: service.cancel_pairing("x"),
        lambda: service.pairing_status("x"),
        lambda: service.revoke_device("x"),
    ):
        with pytest.raises(PortalError, match="Start the decision portal"):
            call()


def test_the_notification_reads_only_the_upstream_count(monkeypatch, tmp_path) -> None:
    service = _started(monkeypatch, tmp_path)
    try:
        payload = service.notification()
        assert payload["open_count"] == 2
        assert "MRN 4417092" not in json_text(payload)
        assert "Coverage" not in json_text(payload)
        assert payload["title"] == "OpenAdapt needs a decision"
    finally:
        service.stop()


def test_a_stopped_portal_still_yields_a_safe_notification() -> None:
    payload = PortalService(config()).notification()
    assert payload["open_count"] == 0
    assert payload["title"] == "OpenAdapt needs a decision"


def json_text(payload: dict) -> str:
    import json

    return json.dumps(payload)


def test_the_runner_identifier_is_stable_and_not_a_hostname() -> None:
    service = PortalService(config())
    runner_id = service.runner_id()
    assert runner_id == service.runner_id()
    assert runner_id.startswith("runner_")
    import socket

    assert socket.gethostname() not in runner_id


# ------------------------------------------------- deployment target lifetime


def _spawned(monkeypatch, tmp_path, **overrides):
    """Start the portal and return (service, captured argv, staged path)."""
    token = "c" * 43
    process = FakeProcess(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    _fake_client(monkeypatch)
    seen: dict = {}

    def popen(command, **kwargs):
        seen["command"] = list(command)
        staged = Path(command[command.index("--config") + 1])
        # Read the config from inside the spawn, which is the only window the
        # console itself has: this is what Flow's eager load_deployment sees.
        seen["staged"] = staged
        seen["payload"] = staged.read_text(encoding="utf-8")
        seen["mode"] = staged.stat().st_mode & 0o777
        return process

    service = PortalService(configured(tmp_path / "data", **overrides), popen=popen)
    service.start()
    return service, seen


def test_the_portal_passes_the_operators_deployment_target(monkeypatch, tmp_path) -> None:
    """Flow refuses attended mutations with no target; Desktop supplies one."""
    service, seen = _spawned(monkeypatch, tmp_path)
    try:
        command = seen["command"]
        assert "--allow-actions" in command
        assert "--config" in command
        # The console is handed the private staged copy, never the operator's
        # own file.
        assert seen["staged"] != tmp_path / "data" / "deployment.json"
        assert "vdi.example.internal" in seen["payload"]
        assert "correct-horse-battery-staple" in seen["payload"]
    finally:
        service.stop()


def test_the_staged_deployment_config_is_private_while_it_exists(
    monkeypatch, tmp_path
) -> None:
    service, seen = _spawned(monkeypatch, tmp_path)
    try:
        if os.name != "nt":
            assert seen["mode"] == 0o600
    finally:
        service.stop()


def test_the_staged_config_does_not_outlive_the_console_banner(
    monkeypatch, tmp_path
) -> None:
    """The decisive invariant: a credential-bearing file is not session-lived.

    Flow reads ``--config`` eagerly, before it prints the capability banner, and
    keeps only the parsed object. So the file is removed as soon as the banner
    proves the read happened -- long before the portal starts serving, and
    without waiting for the portal session to end.
    """
    service, seen = _spawned(monkeypatch, tmp_path)
    try:
        assert service.running is True
        assert not seen["staged"].exists()
        assert list((tmp_path / "data" / "portal").glob(".deployment-*.yaml")) == []
    finally:
        service.stop()
    assert not seen["staged"].exists()


def test_a_missing_deployment_target_refuses_before_spawning(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    spawned: list = []

    def popen(command, **kwargs):  # pragma: no cover - must never run
        spawned.append(command)
        raise AssertionError("the portal spawned a console with no deployment target")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    service = PortalService(EngineConfig(data_dir=data_dir), popen=popen)
    with pytest.raises(PortalError, match="deployment configuration"):
        service.start()
    assert spawned == []
    assert service.running is False


def test_an_unreadable_deployment_config_refuses_without_echoing_it(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "deployment.json").write_text("[not, an, object]", encoding="utf-8")
    service = PortalService(EngineConfig(data_dir=data_dir))
    with pytest.raises(PortalError, match="could not be prepared"):
        service.start()


def test_a_staging_left_by_a_killed_process_is_swept(monkeypatch, tmp_path) -> None:
    """``finally`` cannot survive SIGKILL, so start-up sweeps old leftovers."""
    from engine.portal.service import STALE_STAGING_AGE_S

    staging = tmp_path / "data" / "portal"
    staging.mkdir(parents=True)
    stale = staging / ".deployment-orphan.yaml"
    stale.write_text("backend: {}\n", encoding="utf-8")
    old = time.time() - STALE_STAGING_AGE_S - 60
    os.utime(stale, (old, old))

    fresh = staging / ".deployment-concurrent.yaml"
    fresh.write_text("backend: {}\n", encoding="utf-8")

    service, _seen = _spawned(monkeypatch, tmp_path)
    try:
        assert not stale.exists()
        # A config belonging to a console that is still starting is younger
        # than the start timeout, so a concurrent start keeps its own file.
        assert fresh.exists()
    finally:
        service.stop()


# ------------------------------------------------------ uvicorn bind readiness


def test_the_readiness_check_retries_until_uvicorn_binds(monkeypatch, tmp_path) -> None:
    """Flow prints the banner before uvicorn binds; the first call can fail."""
    token = "d" * 43
    process = FakeProcess(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    monkeypatch.setattr("engine.portal.service.CONSOLE_READY_POLL_S", 0.0)
    state = _fake_client(monkeypatch, fail_first=3)
    service = PortalService(configured(tmp_path / "data"), popen=lambda *a, **k: process)
    try:
        service.start()
        assert service.running is True
        assert state["session_calls"] == 4
    finally:
        service.stop()


def test_the_readiness_wait_is_bounded_and_fails_loud(monkeypatch, tmp_path) -> None:
    token = "e" * 43
    process = FakeProcess(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    monkeypatch.setattr("engine.portal.service.CONSOLE_READY_POLL_S", 0.0)
    monkeypatch.setattr("engine.portal.service.CONSOLE_READY_TIMEOUT_S", 0.05)
    _fake_client(monkeypatch, fail_first=10**6)
    service = PortalService(configured(tmp_path / "data"), popen=lambda *a, **k: process)
    with pytest.raises(PortalError, match="did not answer"):
        service.start()
    assert service.running is False
    assert process.terminated is True


def test_a_console_that_exits_is_not_waited_out(monkeypatch, tmp_path) -> None:
    """A dead console fails immediately rather than burning the whole deadline."""
    token = "f" * 43

    class Exited(FakeProcess):
        def poll(self):
            return 1

    process = Exited(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    monkeypatch.setattr("engine.portal.service.CONSOLE_READY_TIMEOUT_S", 300.0)
    _fake_client(monkeypatch, fail_first=10**6)
    service = PortalService(configured(tmp_path / "data"), popen=lambda *a, **k: process)
    started = time.monotonic()
    with pytest.raises(PortalError, match="did not answer"):
        service.start()
    assert time.monotonic() - started < 30.0
