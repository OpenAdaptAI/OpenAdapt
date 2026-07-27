"""One-use QR pairing: single claim, server-side expiry, runner binding."""

from __future__ import annotations

import threading

import pytest

from engine.auth.pairing import PAIRING_SECRET_RE as CLOUD_SECRET_RE
from engine.portal.pairing import (
    MAX_CONFIRM_ATTEMPTS,
    PAIRING_TTL_S,
    PORTAL_PAIRING_SECRET_RE,
    SESSION_TTL_S,
    DevicePairingStore,
    PairingRefused,
)

ORIGIN = "https://openadapt.clinic.example"


class Clock:
    """A monotonic clock the tests advance without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def store(clock: Clock | None = None) -> DevicePairingStore:
    return DevicePairingStore(runner_id="runner_test", clock=clock or Clock())


def test_a_pairing_secret_is_claimable_exactly_once() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)

    first = pairings.claim(pairing.secret, "Phone A")
    assert first["state"] == "pending_approval"

    with pytest.raises(PairingRefused) as refused:
        pairings.claim(pairing.secret, "Phone B")
    assert refused.value.reason == "already_claimed"


def test_concurrent_scans_of_one_qr_produce_exactly_one_session() -> None:
    """The whole threat: two phones scanning the same code must not both pair."""
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)

    barrier = threading.Barrier(12)
    results: list[object] = []
    lock = threading.Lock()

    def scan() -> None:
        barrier.wait()
        try:
            outcome: object = pairings.claim(pairing.secret, "Phone")
        except PairingRefused as exc:
            outcome = exc.reason
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=scan) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    assert all(r == "already_claimed" for r in results if not isinstance(r, dict))
    # Exactly one device session exists, approved or not.
    pairings.approve(winners[0]["pairing_id"], winners[0]["confirm_code"])
    assert len(pairings.devices()) == 1


def test_expiry_is_five_minutes_and_enforced_by_the_server() -> None:
    clock = Clock()
    pairings = store(clock)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    assert PAIRING_TTL_S == 300
    assert pairing.expires_in_s == 300

    # One second before the deadline the code still works...
    clock.advance(PAIRING_TTL_S - 1)
    assert pairings.claim(pairing.secret)["state"] == "pending_approval"

    # ...and one second after it, the refusal comes from the server, with no
    # user-interface countdown involved.
    later = store(clock)
    expiring = later.create(ORIGIN, reachable_from_phone=True)
    clock.advance(PAIRING_TTL_S)
    with pytest.raises(PairingRefused) as refused:
        later.claim(expiring.secret)
    assert refused.value.reason == "expired"


def test_expiry_uses_a_monotonic_clock_not_the_wall_clock(monkeypatch) -> None:
    """Moving the system clock backwards must not extend a pairing."""
    import datetime as real

    import engine.portal.pairing as module

    clock = Clock()
    pairings = store(clock)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    clock.advance(PAIRING_TTL_S + 1)

    class RewoundDatetime(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(1999, 1, 1, tzinfo=real.timezone.utc)

    monkeypatch.setattr(module, "datetime", RewoundDatetime)
    with pytest.raises(PairingRefused) as refused:
        pairings.claim(pairing.secret)
    assert refused.value.reason == "expired"


def test_a_claimed_session_is_unusable_until_the_operator_matches_the_code() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "Phone")

    with pytest.raises(PairingRefused) as refused:
        pairings.authenticate(claim["session_token"])
    assert refused.value.reason == "pending_approval"

    approved = pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert approved["state"] == "approved"
    session = pairings.authenticate(claim["session_token"])
    assert session.runner_id == "runner_test"


def test_showing_a_new_code_retires_the_previous_unclaimed_one() -> None:
    pairings = store()
    stale = pairings.create(ORIGIN, reachable_from_phone=True)
    pairings.create(ORIGIN, reachable_from_phone=True)
    with pytest.raises(PairingRefused) as refused:
        pairings.claim(stale.secret)
    assert refused.value.reason == "already_claimed"


def test_cancelling_a_pairing_revokes_the_session_it_minted() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert pairings.authenticate(claim["session_token"]) is not None

    pairings.cancel(claim["pairing_id"])
    with pytest.raises(PairingRefused) as refused:
        pairings.authenticate(claim["session_token"])
    assert refused.value.reason == "unauthorized"


def test_a_device_session_expires_and_can_be_revoked() -> None:
    clock = Clock()
    pairings = store(clock)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    pairings.approve(claim["pairing_id"], claim["confirm_code"])

    session = pairings.authenticate(claim["session_token"])
    pairings.revoke(session.session_id)
    with pytest.raises(PairingRefused):
        pairings.authenticate(claim["session_token"])

    second = store(clock)
    other = second.create(ORIGIN, reachable_from_phone=True)
    other_claim = second.claim(other.secret)
    second.approve(other_claim["pairing_id"], other_claim["confirm_code"])
    clock.advance(SESSION_TTL_S + 1)
    with pytest.raises(PairingRefused) as refused:
        second.authenticate(other_claim["session_token"])
    assert refused.value.reason == "expired"


@pytest.mark.parametrize(
    "secret",
    ["", "oapp_short", "oap_" + "A" * 43, "x" * 48, None, 17],
)
def test_a_malformed_secret_is_refused_without_touching_state(secret) -> None:
    pairings = store()
    pairings.create(ORIGIN, reachable_from_phone=True)
    with pytest.raises(PairingRefused) as refused:
        pairings.claim(secret)
    assert refused.value.reason == "malformed"


def test_portal_and_cloud_pairing_secrets_are_mutually_unusable() -> None:
    """A Cloud local-bridge secret must never open the runner-local portal."""
    pairings = store()
    portal_secret = pairings.create(ORIGIN, reachable_from_phone=True).secret
    cloud_secret = "oap_" + "A" * 43

    assert PORTAL_PAIRING_SECRET_RE.fullmatch(portal_secret)
    assert not CLOUD_SECRET_RE.fullmatch(portal_secret)
    assert CLOUD_SECRET_RE.fullmatch(cloud_secret)
    assert not PORTAL_PAIRING_SECRET_RE.fullmatch(cloud_secret)


def test_the_qr_link_carries_a_pairing_secret_and_nothing_else() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    assert pairing.url == f"{ORIGIN}/pair#c={pairing.secret}"
    # No capability, token, tenant, run, or pause identifier is in the link.
    for forbidden in ("token", "capability", "run", "pause", "bearer"):
        assert forbidden not in pairing.url.lower().replace(pairing.secret.lower(), "")


def test_the_desktop_view_of_a_pairing_never_leaks_a_session_credential() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    public = pairing.public()
    assert "secret" not in public
    claim = pairings.claim(pairing.secret)
    status = pairings.pairing_status(claim["pairing_id"])
    assert "session_token" not in status
    assert "csrf_token" not in status
    assert set(status) == {
        "pairing_id",
        "state",
        "expires_in_s",
        "attempts_remaining",
        "device_label",
    }


def test_listed_devices_never_include_a_token_or_digest() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "Ward phone")
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    (device,) = pairings.devices()
    assert set(device) == {"session_id", "device_label", "approved", "expires_in_s"}
    assert device["device_label"] == "Ward phone"


def test_a_hostile_device_label_is_sanitized() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret, "<script>alert(1)</script>")
    assert claim["device_label"] == "Paired phone"


def test_the_confirmation_code_is_minted_per_claim_not_per_pairing() -> None:
    """The whole anti-phishing property.

    An attacker who photographs the QR and claims it first must not be handed
    the code the operator's screen is showing. The code is created at claim
    time, so the operator can only obtain it from the phone in their hand.
    """
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    # Nothing the operator can see before a claim contains a code.
    assert "confirm_code" not in pairing.public()
    assert "match_code" not in pairing.public()
    assert not hasattr(pairing, "match_code")

    attacker = pairings.claim(pairing.secret, "Phone")
    status = pairings.pairing_status(pairing.pairing_id)
    # ...and it is not readable from the pairing state either.
    assert "confirm_code" not in status
    assert "match_code" not in status
    assert attacker["confirm_code"] not in str(status)

    body = attacker["confirm_code"].replace("-", "")
    assert len(body) == 6
    # Characters that are read wrong when typed are excluded.
    assert not set(body) & set("01IOL")


def test_approval_requires_the_code_the_phone_displayed() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)

    with pytest.raises(PairingRefused) as refused:
        pairings.approve(claim["pairing_id"], "AAA-AAA")
    assert refused.value.reason == "wrong_code"
    # The session is still unusable.
    with pytest.raises(PairingRefused):
        pairings.authenticate(claim["session_token"])

    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert pairings.authenticate(claim["session_token"]).runner_id == "runner_test"


def test_the_code_is_accepted_regardless_of_case_or_separator() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    typed = claim["confirm_code"].replace("-", "").lower()
    assert pairings.approve(claim["pairing_id"], typed)["state"] == "approved"


def test_guessing_the_code_cancels_the_pairing() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    for _ in range(MAX_CONFIRM_ATTEMPTS - 1):
        with pytest.raises(PairingRefused):
            pairings.approve(claim["pairing_id"], "AAA-AAA")
    with pytest.raises(PairingRefused) as refused:
        pairings.approve(claim["pairing_id"], "AAA-AAA")
    assert refused.value.reason == "wrong_code"
    # Exhausted: even the correct code no longer works, and the session is dead.
    with pytest.raises(PairingRefused) as after:
        pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert after.value.reason == "not_claimed"
    with pytest.raises(PairingRefused):
        pairings.authenticate(claim["session_token"])


def test_a_claimed_pairing_still_expires_after_five_minutes() -> None:
    """Expiring only `pending` would let a stale claim be approved hours later."""
    clock = Clock()
    pairings = store(clock)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)

    clock.advance(PAIRING_TTL_S + 1)
    with pytest.raises(PairingRefused) as refused:
        pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert refused.value.reason == "expired"
    assert pairings.pairing_status(claim["pairing_id"])["state"] == "expired"
    with pytest.raises(PairingRefused):
        pairings.authenticate(claim["session_token"])


def test_showing_a_new_code_also_retires_an_already_claimed_one() -> None:
    """A stolen-and-claimed code must not survive the operator retrying."""
    pairings = store()
    stolen = pairings.create(ORIGIN, reachable_from_phone=True)
    attacker = pairings.claim(stolen.secret, "Phone")

    pairings.create(ORIGIN, reachable_from_phone=True)

    with pytest.raises(PairingRefused) as refused:
        pairings.approve(attacker["pairing_id"], attacker["confirm_code"])
    assert refused.value.reason == "not_claimed"
    with pytest.raises(PairingRefused):
        pairings.authenticate(attacker["session_token"])


def test_a_suspended_machine_cannot_extend_a_session(monkeypatch) -> None:
    """Monotonic time stalls across suspend, so wall time is a ceiling too."""
    import datetime as real

    import engine.portal.pairing as module

    clock = Clock()
    pairings = store(clock)
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    assert pairings.authenticate(claim["session_token"]) is not None

    # The monotonic clock has not moved at all -- as if the laptop slept.
    later = real.datetime.now(real.timezone.utc) + real.timedelta(
        seconds=SESSION_TTL_S + 60
    )

    class SleptDatetime(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(module, "datetime", SleptDatetime)
    with pytest.raises(PairingRefused) as refused:
        pairings.authenticate(claim["session_token"])
    assert refused.value.reason == "expired"


def test_csrf_is_bound_to_the_exact_session() -> None:
    pairings = store()
    pairing = pairings.create(ORIGIN, reachable_from_phone=True)
    claim = pairings.claim(pairing.secret)
    pairings.approve(claim["pairing_id"], claim["confirm_code"])
    session = pairings.authenticate(claim["session_token"])
    assert pairings.verify_csrf(session, claim["csrf_token"]) is True
    assert pairings.verify_csrf(session, "not-the-token") is False
