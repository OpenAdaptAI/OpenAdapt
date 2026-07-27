"""Portal lifecycle: fail closed, supervise the console, expose the seam."""

from __future__ import annotations

import io

import pytest

from engine.config import EngineConfig
from engine.portal.service import PortalError, PortalService, _parse_console_banner


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


def test_a_console_that_never_announces_itself_fails_loud(monkeypatch) -> None:
    class Silent(FakeProcess):
        def __init__(self) -> None:
            super().__init__("")
            self.stdout = io.StringIO("")

    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )
    service = PortalService(config(), popen=lambda *a, **k: Silent())
    with pytest.raises(PortalError, match="console"):
        service.start()
    assert service.running is False


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


def _started(monkeypatch, **overrides) -> PortalService:
    token = "b" * 43
    process = FakeProcess(f"  http://127.0.0.1:7863/#token={token}")
    monkeypatch.setattr(
        "engine.portal.service._flow_command", lambda _bin: ["openadapt-flow"]
    )

    class FakeClient:
        def __init__(self, port, access_token, csrf_token="", client=None):
            self.port = port
            self.access_token = access_token
            self.csrf_token = csrf_token

        def request(self, route, **kwargs):
            from engine.portal.flow_client import FlowResponse

            if route == "session":
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
    service = PortalService(config(**overrides), popen=lambda *a, **k: process)
    service.start()
    return service


def test_a_started_loopback_portal_reports_its_posture(monkeypatch) -> None:
    service = _started(monkeypatch)
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


def test_a_loopback_pairing_says_a_phone_cannot_reach_it(monkeypatch) -> None:
    service = _started(monkeypatch)
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


def test_the_notification_reads_only_the_upstream_count(monkeypatch) -> None:
    service = _started(monkeypatch)
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
