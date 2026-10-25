"""The stop-review queue on the paired phone.

Every route needs an approved device session; answers also need the portal's
own origin and the session's CSRF token. The phone's answers are signed with
the decision portal's approver key, the receipt names that key and the device,
and no command on the window's wire can ask for that key. Uses the real index
database, the real dispatcher and a throwaway portal key. Item values are
synthetic.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openadapt_desktop import stop_queue
from openadapt_desktop.portal.ingress import resolve_ingress
from openadapt_desktop.portal.pairing import DevicePairingStore
from openadapt_desktop.portal.server import PortalApp
from openadapt_desktop.portal.stops import DispatcherStopQueue
from tests.test_engine.test_stop_queue import (  # noqa: F401  (fixture)
    _stop,
    enclave,  # noqa: F401  (fixture)
    queue,
)

ORIGIN = "https://openadapt.clinic.example"


class Config:
    portal_ingress_mode = "customer_ingress"
    portal_public_origin = ORIGIN
    portal_bind_host = ""
    portal_ingress_acknowledged = True
    portal_port = 8443


class FakeStops:
    """Records every call; the route tests check nothing reaches it."""

    def __init__(self) -> None:
        self.calls: list = []

    def list_cards(self):
        self.calls.append(("list",))
        return {"ok": True, "cards": [], "portal_key": {"ready": True}}

    def screenshot(self, card_id):
        self.calls.append(("screenshot", card_id))
        return b"\x89PNG\r\n\x1a\nframe"

    def answer(self, card_ids, choice, *, shown_ms, device):
        self.calls.append(("answer", card_ids, choice, shown_ms, device))
        return {"ok": True, "results": [], "changed": 0}


def _app(stops) -> tuple[PortalApp, DevicePairingStore]:
    pairings = DevicePairingStore(runner_id="runner_test")
    app = PortalApp(resolve_ingress(Config()), pairings, None, stops=stops)
    return app, pairings


def _paired(pairings: DevicePairingStore) -> dict[str, str]:
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "Ward phone")
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    return {
        "authorization": f"Bearer {claim['session_token']}",
        "origin": ORIGIN,
        "content-type": "application/json",
        "x-openadapt-portal-csrf": claim["csrf_token"],
    }


HEX = "a" * 64
ROUTES = [
    ("GET", "/api/portal/stops", b""),
    ("GET", f"/api/portal/stops/{HEX}/screenshot", b""),
    ("POST", f"/api/portal/stops/{HEX}/answer", b'{"choice":"same"}'),
    (
        "POST",
        "/api/portal/stops/answer",
        json.dumps({"card_ids": [f"sha256:{HEX}"], "choice": "same"}).encode(),
    ),
]


# -- route auth --------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_no_session_is_refused_on_every_stop_route(method, path, body):
    stops = FakeStops()
    app, _pairings = _app(stops)

    status, headers, raw = app.handle(
        method, path, {"origin": ORIGIN, "content-type": "application/json"}, body
    )

    assert status == 401
    assert json.loads(raw)["reason"] == "unauthorized"
    assert headers["Cache-Control"].startswith("no-store")
    assert stops.calls == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_an_unapproved_phone_sees_no_stop(method, path, body):
    stops = FakeStops()
    app, pairings = _app(stops)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    headers = {"authorization": f"Bearer {claim['session_token']}", "origin": ORIGIN}

    status, _headers, _raw = app.handle(method, path, headers, body)

    assert status == 202
    assert stops.calls == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_another_sites_request_is_refused(method, path, body):
    stops = FakeStops()
    app, pairings = _app(stops)
    headers = {**_paired(pairings), "origin": "https://evil.example"}

    status, _headers, raw = app.handle(method, path, headers, body)

    assert status == 403
    assert json.loads(raw)["reason"] == "cross_origin"
    assert stops.calls == []


def test_a_cross_site_read_without_an_origin_is_refused():
    stops = FakeStops()
    app, pairings = _app(stops)
    headers = {k: v for k, v in _paired(pairings).items() if k != "origin"}

    status, _h, _b = app.handle(
        "GET", "/api/portal/stops", {**headers, "sec-fetch-site": "cross-site"}, b""
    )
    assert status == 403
    # A same-origin read leaves Origin off; that is served.
    status, _h, _b = app.handle(
        "GET", "/api/portal/stops", {**headers, "sec-fetch-site": "same-origin"}, b""
    )
    assert status == 200


@pytest.mark.parametrize("drop", ["origin", "x-openadapt-portal-csrf", "content-type"])
def test_an_answer_needs_the_origin_the_csrf_token_and_json(drop):
    stops = FakeStops()
    app, pairings = _app(stops)
    headers = {k: v for k, v in _paired(pairings).items() if k != drop}

    status, _h, _b = app.handle(
        "POST", f"/api/portal/stops/{HEX}/answer", headers, b'{"choice":"same"}'
    )

    assert status in {403, 415}
    assert stops.calls == []


def test_the_routes_pass_the_device_session_and_never_the_token():
    stops = FakeStops()
    app, pairings = _app(stops)
    headers = _paired(pairings)

    assert app.handle("GET", "/api/portal/stops", headers, b"")[0] == 200
    status, image_headers, image = app.handle(
        "GET", f"/api/portal/stops/{HEX}/screenshot", headers, b""
    )
    assert status == 200 and image.startswith(b"\x89PNG")
    assert image_headers["Content-Type"] == "image/png"
    assert image_headers["Cache-Control"].startswith("no-store")
    body = json.dumps({"choice": "same", "shown_ms": 1500}).encode()
    assert app.handle("POST", f"/api/portal/stops/{HEX}/answer", headers, body)[0] == 200

    (answer,) = [c for c in stops.calls if c[0] == "answer"]
    _, card_ids, choice, shown_ms, device = answer
    assert card_ids == [f"sha256:{HEX}"] and choice == "same"
    assert shown_ms == {f"sha256:{HEX}": 1500}
    (session,) = pairings.devices()
    assert device == {"id": session["session_id"], "label": "Ward phone"}
    assert headers["authorization"].split()[1] not in json.dumps(device)


@pytest.mark.parametrize(
    "payload",
    [
        {"card_ids": [], "choice": "same"},
        {"card_ids": ["not-a-card"], "choice": "same"},
        {"card_ids": [f"sha256:{HEX}"] * 26, "choice": "same"},
        {"card_ids": [f"sha256:{HEX}"], "choice": "Same!"},
        {"card_ids": f"sha256:{HEX}", "choice": "same"},
    ],
)
def test_a_malformed_batch_never_reaches_the_queue(payload):
    stops = FakeStops()
    app, pairings = _app(stops)

    status, _h, _b = app.handle(
        "POST", "/api/portal/stops/answer", _paired(pairings), json.dumps(payload).encode()
    )

    assert status == 400
    assert stops.calls == []


def test_the_phone_page_is_served_and_its_cards_are_not():
    app, _pairings = _app(FakeStops())
    for path in ("/stops", "/stops.js", "/stops.css"):
        status, headers, body = app.handle("GET", path, {}, b"")
        assert status == 200 and body
        assert headers["Cache-Control"] == "no-cache"


def test_without_a_queue_the_routes_say_so():
    app, pairings = _app(None)
    status, _h, raw = app.handle("GET", "/api/portal/stops", _paired(pairings), b"")
    assert status == 503 and json.loads(raw)["reason"] == "stops_unavailable"


# -- signing with the portal key ----------------------------------------------------


@pytest.fixture
def portal_key(monkeypatch, tmp_path: Path):
    from openadapt_desktop import approver_key

    monkeypatch.setattr(approver_key, "KEY_DIR", tmp_path / "approver")
    return approver_key.setup_approver_key("portal")


@pytest.fixture
def phone(queue):  # noqa: F811
    disp, db, keychain = queue
    disp.serial_lock = threading.RLock()
    app, pairings = _app(DispatcherStopQueue(disp))
    return app, _paired(pairings), pairings, disp, db, keychain


def _phone_cards(app, headers) -> dict:
    status, _h, raw = app.handle("GET", "/api/portal/stops", headers, b"")
    assert status == 200
    return json.loads(raw)


def _receipts(disp) -> list[dict]:
    path = disp.config.data_dir / "stop_decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_phone_lists_the_same_cards_as_the_window(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-01", lname="Fairwick")

    listed = _phone_cards(app, headers)

    assert listed["cards"] == disp.dispatch("list_stop_cards", {})["cards"]
    assert listed["portal_key"] == {"ready": True}


def test_the_phone_gets_the_cards_own_screenshot_and_nothing_else(phone, monkeypatch):
    import base64

    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-06", lname="Fairwick")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    asked: list = []
    monkeypatch.setattr(
        disp,
        "get_attention_screenshot",
        lambda **p: (
            asked.append(p)
            or {"ok": True, "data_url": "data:image/png;base64," + base64.b64encode(png).decode()}
        ),
    )
    (card,) = _phone_cards(app, headers)["cards"]

    status, _h, body = app.handle(
        "GET",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/screenshot",
        headers,
        b"",
    )
    assert status == 200 and body == png
    assert asked == [{"item_id": "item-06"}]
    # A card id that isn't in the queue shows nothing.
    status, _h, _b = app.handle("GET", f"/api/portal/stops/{HEX}/screenshot", headers, b"")
    assert status == 404


def test_a_phone_answer_is_signed_with_the_portal_key(phone, portal_key, enclave):  # noqa: F811
    app, headers, pairings, disp, db, keychain = phone
    _stop(db, "item-02", lname="Fairwick")
    (card,) = _phone_cards(app, headers)["cards"]

    status, _h, raw = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        json.dumps({"choice": "same", "shown_ms": 3100}).encode(),
    )

    assert status == 200 and json.loads(raw)["ok"] is True
    assert db.get_work_item("item-02")["status"] == "skipped"
    # No Touch ID prompt on the computer: the phone can't answer one.
    assert enclave.prompts == []
    (receipt,) = _receipts(disp)
    assert receipt["key"] == "portal"
    assert receipt["public_key"] == portal_key.public_key
    assert receipt["public_key"] != keychain.stored().public_key
    (session,) = pairings.devices()
    assert receipt["device"] == {"id": session["session_id"], "label": "Ward phone"}
    assert receipt["shown_ms"] == {card["card_id"]: 3100}
    message = stop_queue.decision_message(
        [card["card_id"]], "same", decided_at=receipt["decided_at"]
    )
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(portal_key.public_key)).verify(
        bytes.fromhex(receipt["signature"].removeprefix("ed25519:")), message
    )


def test_the_window_cannot_choose_the_portal_key(queue, portal_key, enclave):  # noqa: F811
    disp, db, keychain = queue
    _stop(db, "item-03", lname="Fairwick")
    (card,) = disp.dispatch("list_stop_cards", {})["cards"]

    answer = disp.dispatch(
        "answer_stop_cards",
        {
            "card_ids": [card["card_id"]],
            "choice": "same",
            # None of these is a parameter; the window's answer is still
            # signed with this computer's key, after Touch ID.
            "key": "portal",
            "route": "portal",
            "sign": "portal",
            "device": {"id": "forged", "label": "forged"},
        },
    )

    assert answer["ok"] is True
    assert enclave.prompts == ["record your answer to this stop"]
    (receipt,) = _receipts(disp)
    assert receipt["key"] == "keychain"
    assert receipt["public_key"] == keychain.stored().public_key
    assert "device" not in receipt
    # The portal's entry point is no command on any wire.
    assert "answer_stop_cards_from_portal" not in disp.commands
    with pytest.raises(KeyError):
        disp.dispatch("answer_stop_cards_from_portal", {})


def test_a_stale_card_from_the_phone_changes_nothing(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-04", lname="Fairwick")
    (card,) = _phone_cards(app, headers)["cards"]
    db.update_work_item("item-04", halt_reason="a different stop")

    status, _h, raw = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        b'{"choice":"same"}',
    )

    assert status == 409 and json.loads(raw)["code"] == "card_changed"
    assert db.get_work_item("item-04")["status"] == "open"
    assert _receipts(disp) == []


def test_a_choice_the_card_doesnt_offer_changes_nothing_from_the_phone(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-05", lname="Fairwick")
    (card,) = _phone_cards(app, headers)["cards"]

    status, _h, raw = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        b'{"choice":"approve"}',
    )

    assert status == 422 and json.loads(raw)["code"] == "choice_not_offered"
    assert db.get_work_item("item-05")["status"] == "open"


def test_a_batch_from_the_phone_is_one_portal_signature(phone, portal_key, enclave):  # noqa: F811
    app, headers, _pairings, disp, db, _keychain = phone
    for index, name in enumerate(["Fairwick", "Caldley", "Jesswood"], start=10):
        _stop(db, f"item-{index}", lname=name)
    cards = _phone_cards(app, headers)["cards"]
    ids = [c["card_id"] for c in cards]

    status, _h, raw = app.handle(
        "POST",
        "/api/portal/stops/answer",
        headers,
        json.dumps({"card_ids": ids, "choice": "same", "shown_ms": {ids[0]: 900}}).encode(),
    )

    assert status == 200
    assert json.loads(raw)["changed"] == 3
    assert enclave.prompts == []
    (receipt,) = _receipts(disp)
    assert receipt["key"] == "portal" and receipt["card_ids"] == sorted(ids)
    message = stop_queue.decision_message(ids, "same", decided_at=receipt["decided_at"])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(portal_key.public_key)).verify(
        bytes.fromhex(receipt["signature"].removeprefix("ed25519:")), message
    )
    assert _phone_cards(app, headers)["cards"] == []


def test_one_stale_card_stops_the_phones_whole_batch(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-20", lname="Fairwick")
    _stop(db, "item-21", lname="Caldley")
    ids = [c["card_id"] for c in _phone_cards(app, headers)["cards"]]
    db.update_work_item("item-21", status="skipped")

    status, _h, raw = app.handle(
        "POST",
        "/api/portal/stops/answer",
        headers,
        json.dumps({"card_ids": ids, "choice": "same"}).encode(),
    )

    assert status == 409 and json.loads(raw)["code"] == "card_changed"
    assert db.get_work_item("item-20")["status"] == "open"
    assert _receipts(disp) == []


def test_without_the_portal_key_the_phone_changes_nothing(phone, monkeypatch, tmp_path):
    from openadapt_desktop import approver_key

    monkeypatch.setattr(approver_key, "KEY_DIR", tmp_path / "no-keys")
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-30", lname="Fairwick")
    listed = _phone_cards(app, headers)
    assert listed["portal_key"] == {"ready": False}
    (card,) = listed["cards"]

    status, _h, raw = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        b'{"choice":"same"}',
    )

    assert status == 503 and json.loads(raw)["code"] == "approver_key_missing"
    assert db.get_work_item("item-30")["status"] == "open"


def test_a_waiting_save_is_view_only_on_the_stop_queue(phone, portal_key, monkeypatch):
    from tests.test_engine.test_stop_queue import _waiting_state

    app, headers, _pairings, disp, _db, _keychain = phone
    disp._approval_watches["run"] = {"workflow_id": "wf-ref"}
    monkeypatch.setattr(
        disp, "get_item_approval", lambda **_: _waiting_state("2999-01-01T00:00:00+00:00")
    )
    monkeypatch.setattr(
        disp, "answer_item_approval", lambda **_: pytest.fail("answered a save from the queue")
    )
    (card,) = _phone_cards(app, headers)["cards"]
    assert card["kind"] == "approve_save"

    status, _h, raw = app.handle(
        "POST",
        f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
        headers,
        b'{"choice":"approve"}',
    )

    assert status == 409 and json.loads(raw)["code"] == "use_approval"
    assert _receipts(disp) == []


def _while_the_window_is_busy(disp, answer):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with disp.serial_lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    held.wait(5)
    try:
        return answer()
    finally:
        release.set()
        worker.join()


def test_an_answer_that_runs_an_item_again_waits_for_the_window(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-40", lname="Fairwick", reason="a step's target wasn't found")
    (card,) = _phone_cards(app, headers)["cards"]
    assert any(c["id"] == "retry" for c in card["choices"])

    status, _h, raw = _while_the_window_is_busy(
        disp,
        lambda: app.handle(
            "POST",
            f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
            headers,
            b'{"choice":"retry"}',
        ),
    )

    assert status == 503 and json.loads(raw)["code"] == "busy"
    assert db.get_work_item("item-40")["status"] == "open"


def test_closing_a_stop_goes_through_while_the_window_is_busy(phone, portal_key):
    app, headers, _pairings, disp, db, _keychain = phone
    _stop(db, "item-41", lname="Fairwick")
    (card,) = _phone_cards(app, headers)["cards"]

    status, _h, raw = _while_the_window_is_busy(
        disp,
        lambda: app.handle(
            "POST",
            f"/api/portal/stops/{card['card_id'].removeprefix('sha256:')}/answer",
            headers,
            b'{"choice":"same"}',
        ),
    )

    assert status == 200, raw
    assert db.get_work_item("item-41")["status"] == "skipped"


def test_the_ipc_handler_shares_its_lock_with_the_phone(tmp_path):
    from openadapt_desktop.config import EngineConfig
    from openadapt_desktop.ipc import IPCHandler

    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    handler = IPCHandler(config=config)
    assert handler.dispatcher.serial_lock is handler.dispatch_lock
