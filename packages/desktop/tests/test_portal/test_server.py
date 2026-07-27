"""The portal relays; it never decides, and it never lets evidence be cached."""

from __future__ import annotations

import json

import pytest

from engine.portal.flow_client import FLOW_ROUTES, FlowConsoleUnavailable, FlowResponse
from engine.portal.ingress import resolve_ingress
from engine.portal.pairing import DevicePairingStore
from engine.portal.server import PROTECTED_PREFIX, SHELL_ASSETS, PortalApp

ORIGIN = "https://openadapt.clinic.example"


class Config:
    portal_ingress_mode = "customer_ingress"
    portal_public_origin = ORIGIN
    portal_bind_host = ""
    portal_ingress_acknowledged = True
    portal_port = 8443


class FakeFlow:
    """Records every upstream call and returns canned console responses."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def request(self, route: str, **kwargs):
        self.calls.append((route, kwargs))
        if route not in FLOW_ROUTES:
            raise FlowConsoleUnavailable("not allowlisted", reason="route_not_allowed")
        return self.responses.get(
            route, FlowResponse(200, {"route": route}, None, "application/json")
        )


def build(flow: object | None = None) -> tuple[PortalApp, DevicePairingStore]:
    pairings = DevicePairingStore(runner_id="runner_test")
    app = PortalApp(resolve_ingress(Config()), pairings, flow)  # type: ignore[arg-type]
    return app, pairings


def paired(app: PortalApp, pairings: DevicePairingStore) -> dict[str, str]:
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "Phone")
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    return {
        "authorization": f"Bearer {claim['session_token']}",
        "origin": ORIGIN,
        "content-type": "application/json",
        "x-openadapt-portal-csrf": claim["csrf_token"],
    }


# ------------------------------------------------------------------ no-store


def test_every_protected_response_is_no_store() -> None:
    app, pairings = build(FakeFlow())
    headers = paired(app, pairings)
    for path in (
        "/api/portal/session",
        "/api/portal/tasks",
        "/api/portal/tasks/run-1",
        "/api/portal/tasks/run-1/evidence?id=frame-a",
    ):
        _, response_headers, _ = app.handle("GET", path, headers, b"")
        assert response_headers["Cache-Control"].startswith("no-store"), path
        assert response_headers["Pragma"] == "no-cache", path


def test_a_protected_evidence_image_is_relayed_no_store_and_untouched() -> None:
    png = b"\x89PNG\r\n\x1a\nprotected-crop-bytes"
    flow = FakeFlow({"task_evidence": FlowResponse(200, None, png, "image/png")})
    app, pairings = build(flow)
    headers = paired(app, pairings)
    status, response_headers, body = app.handle(
        "GET", "/api/portal/tasks/run-1/evidence?id=frame-a", headers, b""
    )
    assert status == 200
    assert body == png
    assert response_headers["Content-Type"] == "image/png"
    assert response_headers["Cache-Control"].startswith("no-store")
    assert flow.calls == [("task_evidence", {"run_id": "run-1", "params": {"id": "frame-a"}})]


def test_shell_assets_are_the_only_paths_outside_the_protected_prefix() -> None:
    app, _ = build(FakeFlow())
    for path in SHELL_ASSETS:
        status, headers, body = app.handle("GET", path, {}, b"")
        assert status == 200 and body
        assert not path.startswith(PROTECTED_PREFIX)
    for path in ("/", "/pair"):
        assert app.handle("GET", path, {}, b"")[0] == 200
    # Nothing else is served at all.
    for path in ("/etc/passwd", "/api/session", "/api/attention", "/index.html"):
        assert app.handle("GET", path, {}, b"")[0] == 404


# ------------------------------------------------------------------- pairing


def test_pairing_is_the_only_route_reachable_without_a_session() -> None:
    app, pairings = build(FakeFlow())
    for path in ("/api/portal/session", "/api/portal/tasks", "/api/portal/tasks/r"):
        status, _, body = app.handle("GET", path, {"origin": ORIGIN}, b"")
        assert status == 401
        assert json.loads(body)["reason"] == "unauthorized"


def test_claiming_through_the_portal_is_single_use() -> None:
    app, pairings = build(FakeFlow())
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    body = json.dumps({"secret": pairing.secret, "device_label": "Phone"}).encode()
    headers = {"origin": ORIGIN, "content-type": "application/json"}

    status, _, first = app.handle("POST", "/api/portal/pair/claim", headers, body)
    assert status == 200
    assert json.loads(first)["state"] == "pending_approval"

    status, _, second = app.handle("POST", "/api/portal/pair/claim", headers, body)
    assert status == 410
    assert json.loads(second)["reason"] == "already_claimed"


def test_an_unapproved_phone_is_told_to_wait_rather_than_shown_a_task() -> None:
    app, pairings = build(FakeFlow())
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    headers = {"authorization": f"Bearer {claim['session_token']}", "origin": ORIGIN}
    status, _, body = app.handle("GET", "/api/portal/tasks", headers, b"")
    assert status == 202
    assert json.loads(body)["reason"] == "pending_approval"


def test_a_cross_origin_claim_or_decision_is_refused() -> None:
    app, pairings = build(FakeFlow())
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    body = json.dumps({"secret": pairing.secret}).encode()
    status, _, refused = app.handle(
        "POST",
        "/api/portal/pair/claim",
        {"origin": "https://evil.example", "content-type": "application/json"},
        body,
    )
    assert status == 403
    assert json.loads(refused)["reason"] == "cross_origin"

    headers = paired(app, pairings) | {"origin": "https://evil.example"}
    status, _, refused = app.handle(
        "POST", "/api/portal/tasks/run-1/actions/continue", headers, b"{}"
    )
    assert status == 403


def test_a_decision_requires_the_session_csrf_token() -> None:
    app, pairings = build(FakeFlow())
    headers = paired(app, pairings) | {"x-openadapt-portal-csrf": "wrong"}
    status, _, body = app.handle(
        "POST", "/api/portal/tasks/run-1/actions/continue", headers, b"{}"
    )
    assert status == 403
    assert json.loads(body)["reason"] == "csrf"


# ------------------------------------------------- Desktop adds no authority


def test_a_decision_payload_is_forwarded_verbatim() -> None:
    """Desktop must not rewrite a capability digest, action, or idempotency key."""
    flow = FakeFlow(
        {
            "task_action": FlowResponse(
                200, {"status": "halted", "message": "m"}, None, "application/json"
            )
        }
    )
    app, pairings = build(flow)
    headers = paired(app, pairings)
    payload = {
        "capability_digest": "sha256:" + "a" * 64,
        "task_digest": "sha256:" + "b" * 64,
        "task_signature": "hmac-sha256:" + "c" * 64,
        "idempotency_key": "fixture-idempotency-key",
        "action": "continue",
        "disposition": "completed_by_operator",
    }
    status, _, body = app.handle(
        "POST",
        "/api/portal/tasks/run-1/actions/continue",
        headers,
        json.dumps(payload).encode(),
    )
    assert status == 200
    route, kwargs = flow.calls[-1]
    assert route == "task_action"
    assert kwargs["json_body"] == payload
    # The runner's outcome is passed through, not reinterpreted as success.
    assert json.loads(body) == {"status": "halted", "message": "m"}


def test_a_refusal_from_flow_is_relayed_with_its_status() -> None:
    flow = FakeFlow(
        {
            "task_action": FlowResponse(
                409,
                {"detail": "the pause is no longer current"},
                None,
                "application/json",
            )
        }
    )
    app, pairings = build(flow)
    status, _, body = app.handle(
        "POST",
        "/api/portal/tasks/run-1/actions/continue",
        paired(app, pairings),
        b"{}",
    )
    assert status == 409
    assert json.loads(body)["detail"] == "the pause is no longer current"


def test_the_phone_never_receives_the_flow_console_capability() -> None:
    from engine.portal.flow_client import FlowConsoleClient

    flow = FlowConsoleClient(port=7863, access_token="console-bearer-secret")
    app, pairings = build(flow)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    _, _, body = app.handle(
        "GET",
        "/api/portal/session",
        {"authorization": f"Bearer {claim['session_token']}", "origin": ORIGIN},
        b"",
    )
    assert "console-bearer-secret" not in body.decode()


def test_the_portal_stops_a_request_for_an_unallowlisted_console_route() -> None:
    """The relay is an allowlist; there is no generic proxy path."""
    from engine.portal.flow_client import FlowConsoleClient

    client = FlowConsoleClient(port=7863, access_token="t")
    with pytest.raises(FlowConsoleUnavailable) as refused:
        client.request("workflows")
    assert refused.value.reason == "route_not_allowed"


def test_a_missing_console_reports_unavailable_rather_than_inventing_an_answer() -> None:
    app, pairings = build(None)
    status, _, body = app.handle("GET", "/api/portal/tasks", paired(app, pairings), b"")
    assert status == 503
    assert json.loads(body)["reason"] == "portal_upstream_unavailable"


# ------------------------------------------------------------------ hardening


@pytest.mark.parametrize(
    "path",
    [
        "/api/portal/tasks/../../etc/passwd",
        "/api/portal/tasks//run",
        "/api/portal/tasks/run%00",
        "/api/portal/tasks/run-1/actions/DROP",
    ],
)
def test_traversal_and_malformed_identifiers_are_refused(path: str) -> None:
    app, pairings = build(FakeFlow())
    status, _, _ = app.handle("GET", path, paired(app, pairings), b"")
    assert status == 404


def test_an_evidence_reference_must_be_a_plain_artifact_id() -> None:
    app, pairings = build(FakeFlow())
    headers = paired(app, pairings)
    status, _, body = app.handle(
        "GET", "/api/portal/tasks/run-1/evidence?id=../secret", headers, b""
    )
    assert status == 400
    assert json.loads(body)["reason"] == "bad_request"


def test_security_headers_are_present_on_every_response() -> None:
    app, pairings = build(FakeFlow())
    for method, path, headers in (
        ("GET", "/", {}),
        ("GET", "/app.js", {}),
        ("GET", "/api/portal/tasks", paired(app, pairings)),
    ):
        _, response_headers, _ = app.handle(method, path, headers, b"")
        assert response_headers["X-Frame-Options"] == "DENY"
        assert response_headers["X-Content-Type-Options"] == "nosniff"
        assert response_headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response_headers["Content-Security-Policy"]


def test_the_portal_keeps_no_access_log() -> None:
    """Paths carry run and artifact identifiers, so nothing is logged."""
    from engine.portal import server as module

    assert module._Handler.log_message(object(), "%s", "x") is None


# ------------------------------------------- regressions from security review


def test_the_handler_sets_a_socket_timeout() -> None:
    """Without one, every read blocks forever and idle sockets pin threads."""
    from engine.portal import server as module

    assert module._Handler.timeout is not None
    assert 0 < module._Handler.timeout <= 60
    # HTTP/1.0 closes each connection, so a refused over-long body cannot
    # desync into the next request on the same socket.
    assert module._Handler.protocol_version == "HTTP/1.0"


def test_an_oversized_or_malformed_body_is_refused_not_silently_emptied() -> None:
    app, pairings = build(FakeFlow())
    headers = paired(app, pairings)
    status, _, body = app.handle(
        "POST",
        "/api/portal/tasks/run-1/actions/continue",
        headers,
        b"x" * (16 * 1024 + 1),
    )
    assert status == 413
    assert json.loads(body)["reason"] == "bad_request"


def test_an_evidence_id_cannot_traverse_even_though_it_has_no_separator() -> None:
    app, pairings = build(FakeFlow())
    status, _, _ = app.handle(
        "GET", "/api/portal/tasks/run-1/evidence?id=a..b", paired(app, pairings), b""
    )
    assert status == 400


def test_an_active_image_format_is_not_relayed_to_a_phone() -> None:
    """An SVG is a document, not a crop."""
    import httpx

    from engine.portal.flow_client import FlowConsoleClient

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<svg onload=alert(1)>",
            headers={"content-type": "image/svg+xml"},
        )

    client = FlowConsoleClient(
        port=7863,
        access_token="t",
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(FlowConsoleUnavailable) as refused:
        client.request("task_evidence", run_id="r", params={"id": "frame-a"})
    assert refused.value.reason == "artifact_type_refused"

    # The portal turns that into a refusal, never a rendered document.
    app, pairings = build(client)
    status, _, body = app.handle(
        "GET", "/api/portal/tasks/r/evidence?id=frame-a", paired(app, pairings), b""
    )
    assert status == 503
    assert b"<svg" not in body


def test_an_upstream_server_error_body_is_not_forwarded_to_a_phone() -> None:
    """A 5xx can carry a traceback or a deployment path; a 4xx carries a refusal."""
    import httpx

    from engine.portal.flow_client import FlowConsoleClient

    def transport(request: httpx.Request) -> httpx.Response:
        if "actions" in str(request.url):
            return httpx.Response(
                500, json={"detail": 'File "/srv/phi/openemr/run.py", line 42'}
            )
        return httpx.Response(409, json={"detail": "the pause is no longer current"})

    client = FlowConsoleClient(
        port=7863,
        access_token="t",
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    crash = client.request("task_action", run_id="r", action_id="continue")
    assert crash.status == 500
    assert "/srv/phi" not in json.dumps(crash.json)

    # An operator-actionable refusal still reaches the phone intact.
    refusal = client.request("task_detail", run_id="r")
    assert refusal.json["detail"] == "the pause is no longer current"


def test_the_relay_refuses_an_identifier_the_http_surface_would_have_rejected() -> None:
    """Defense in depth: the two boundaries cannot silently drift apart."""
    from engine.portal.flow_client import FlowConsoleClient

    client = FlowConsoleClient(port=7863, access_token="t")
    for kwargs in (
        {"run_id": "../etc/passwd"},
        {"run_id": "a/b"},
        {"run_id": ""},
    ):
        with pytest.raises(FlowConsoleUnavailable) as refused:
            client.request("task_detail", **kwargs)
        assert refused.value.reason == "bad_identifier"
    with pytest.raises(FlowConsoleUnavailable) as refused:
        client.request("task_action", run_id="ok", action_id="DROP TABLE")
    assert refused.value.reason == "bad_identifier"
