"""One real socket: pair a phone, read a task, relay a decision, get a receipt.

This binds an actual loopback port and speaks HTTP, so the handler, the
headers, and the single-use claim are exercised through the same path a phone
would take.  The upstream attended console is faked at the seam
(:class:`engine.portal.flow_client.FlowConsoleClient`) because the decision
semantics belong to ``openadapt-flow``, not to Desktop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from engine.portal.flow_client import FlowResponse
from engine.portal.ingress import resolve_ingress
from engine.portal.pairing import DevicePairingStore
from engine.portal.server import PortalApp, PortalServer

RUN_ID = "a1b2c3d4e5f6a1b2c3d4e5f6"
TASK = {
    "item": {"id": RUN_ID, "headline": "The effect could not be proven.", "category": "effect"},
    "task": {
        "capability_digest": "sha256:" + "a" * 64,
        "signature": "hmac-sha256:" + "c" * 64,
        "allowed_actions": ["verify_and_resume", "teach", "escalate"],
        "delivery_state": "unknown",
        "expires_at": "2026-07-27T18:00:00Z",
        "evidence": {"effect_required_count": 2, "effect_confirmed_count": 1},
    },
    "task_digest": "sha256:" + "b" * 64,
    "presentation": {
        "question": "Is the intended result present in the destination record?",
        "before_artifact_id": "frame-before",
    },
}


class FakeConsole:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    def request(self, route, **kwargs):
        if route == "tasks":
            return FlowResponse(200, [TASK["item"]], None, "application/json")
        if route == "task_detail":
            return FlowResponse(200, TASK, None, "application/json")
        if route == "task_evidence":
            return FlowResponse(200, None, b"\x89PNG\r\n\x1a\ncrop", "image/png")
        if route == "task_action":
            self.decisions.append(kwargs["json_body"])
            return FlowResponse(
                200,
                {
                    "action": "continue",
                    "status": "halted",
                    "message": "Live identity changed after the displayed screen.",
                    "report_success": False,
                },
                None,
                "application/json",
            )
        raise AssertionError(route)


class LoopbackConfig:
    """Loopback ingress with a real ephemeral port -- the shipped default.

    The tests deliberately do not widen the bind address; they exercise the
    production configuration.
    """

    portal_ingress_mode = "loopback"
    portal_public_origin = ""
    portal_bind_host = ""
    portal_ingress_acknowledged = False
    portal_port = 0


@pytest.fixture()
def portal():
    pairings = DevicePairingStore(runner_id="runner_e2e")
    console = FakeConsole()
    app = PortalApp(resolve_ingress(LoopbackConfig()), pairings, console)
    server = PortalServer(app)
    port = server.start()
    # The advertised origin must match the port actually bound, or the
    # same-origin check would refuse the phone's own requests.
    app.ingress = resolve_ingress(LoopbackConfig(), port=port)
    try:
        yield app, pairings, console, port
    finally:
        server.stop()


def call(port, path, *, method="GET", headers=None, body=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=body,
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_the_full_local_loop_over_a_real_socket(portal) -> None:
    app, pairings, console, port = portal
    origin = app.ingress.public_origin

    # 1. Desktop shows a QR. Its link carries only the pairing secret.
    pairing = pairings.create(origin, reachable_from_phone=True)
    assert pairing.url == f"{origin}/pair#c={pairing.secret}"

    # 2. The shell loads without any credential.
    status, headers, body = call(port, "/pair")
    assert status == 200 and b"OpenAdapt decisions" in body
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")

    # 3. The phone claims the secret once.
    status, _, claimed = call(
        port,
        "/api/portal/pair/claim",
        method="POST",
        headers={"Origin": origin, "Content-Type": "application/json"},
        body=json.dumps({"secret": pairing.secret, "device_label": "Ward phone"}).encode(),
    )
    assert status == 200
    claim = json.loads(claimed)
    # The confirmation code exists only on the phone; nothing the operator can
    # read before this point contains it.
    assert claim["confirm_code"] and "confirm_code" not in pairing.public()

    auth = {"Authorization": f"Bearer {claim['session_token']}", "Origin": origin}

    # 4. Before approval the phone is told to wait; it sees no task.
    status, _, waiting = call(port, "/api/portal/tasks", headers=auth)
    assert status == 202
    assert json.loads(waiting)["reason"] == "pending_approval"

    # 5. The operator types the code the phone is showing and approves.
    pairings.approve(claim["pairing_id"], claim["confirm_code"])

    # 6. The queue and the task projection come through verbatim.
    status, headers, listed = call(port, "/api/portal/tasks", headers=auth)
    assert status == 200
    assert headers["Cache-Control"].startswith("no-store")
    assert json.loads(listed)[0]["id"] == RUN_ID

    status, headers, detail = call(port, f"/api/portal/tasks/{RUN_ID}", headers=auth)
    assert status == 200
    assert headers["Cache-Control"].startswith("no-store")
    assert json.loads(detail) == TASK

    # 7. Protected evidence is relayed no-store.
    status, headers, crop = call(
        port, f"/api/portal/tasks/{RUN_ID}/evidence?id=frame-before", headers=auth
    )
    assert status == 200
    assert crop.startswith(b"\x89PNG")
    assert headers["Cache-Control"].startswith("no-store")
    assert headers["Content-Type"] == "image/png"

    # 8. The decision is relayed exactly as the phone composed it, and the
    #    runner's refusal is reported instead of a success.
    payload = {
        "capability_digest": TASK["task"]["capability_digest"],
        "task_digest": TASK["task_digest"],
        "task_signature": TASK["task"]["signature"],
        "idempotency_key": "fixture-idempotency-key",
        "action": "continue",
        "disposition": "completed_by_operator",
    }
    status, headers, decided = call(
        port,
        f"/api/portal/tasks/{RUN_ID}/actions/continue",
        method="POST",
        headers=auth
        | {
            "Content-Type": "application/json",
            "X-OpenAdapt-Portal-CSRF": claim["csrf_token"],
        },
        body=json.dumps(payload).encode(),
    )
    assert status == 200
    assert console.decisions == [payload]
    assert json.loads(decided)["status"] == "halted"
    assert json.loads(decided)["report_success"] is False


def test_a_second_phone_scanning_the_same_qr_over_http_is_refused(portal) -> None:
    app, pairings, _console, port = portal
    origin = app.ingress.public_origin
    pairing = pairings.create(origin, reachable_from_phone=True)
    headers = {"Origin": origin, "Content-Type": "application/json"}
    body = json.dumps({"secret": pairing.secret}).encode()

    assert call(port, "/api/portal/pair/claim", method="POST", headers=headers, body=body)[0] == 200
    status, _, refused = call(
        port, "/api/portal/pair/claim", method="POST", headers=headers, body=body
    )
    assert status == 410
    assert json.loads(refused)["reason"] == "already_claimed"


def test_the_loopback_default_is_not_reachable_from_another_interface(portal) -> None:
    """The shipped default binds 127.0.0.1 only."""
    app, _pairings, _console, _port = portal
    assert app.ingress.bind_host == "127.0.0.1"
    assert app.ingress.loopback_only is True
    assert app.ingress.reachable_from_phone is False
