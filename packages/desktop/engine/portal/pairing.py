"""One-use QR device pairing for the runner-local mobile decision portal.

This mirrors the discipline the installed application already uses for Cloud
``openadapt://connect`` pairing (:mod:`engine.auth.pairing`): a short-lived,
single-use, narrowly scoped secret that is claimed exactly once.  It is a
separate mechanism because it answers a different question -- *which phone may
open this runner's decision portal* -- and it deliberately uses a distinct
secret prefix so a Cloud pairing secret can never be replayed here, or the
reverse.

Security properties, each enforced here rather than by the user interface:

* **The QR carries only a pairing secret.**  It is not an execution capability,
  not a Flow console bearer token, and not a decision authority.  A claimed
  device receives a portal session that can *read a projected task and relay a
  decision to Flow*; Flow still requires its own pause capability, fresh
  revalidation, and allowed-action check before anything actuates.
* **Single use, atomically.**  Claiming compares and swaps the record state
  under one lock.  Two phones scanning the same QR cannot both pair; the loser
  is refused with ``already_claimed``.
* **Five-minute expiry, server-side.**  The deadline is re-evaluated on every
  access -- including approval -- and never depends on a countdown rendered by
  the phone.  Both clocks are consulted and either can expire a record:
  monotonic time so a wall-clock change cannot extend a deadline, and wall time
  because ``CLOCK_MONOTONIC`` stalls while a machine is suspended.
* **Runner-bound.**  Every record and session names the exact runner and the
  exact ingress origin it was minted for; a session presented to a different
  runner identity is refused.
* **Confirmation code generated at claim time, entered by the operator.**  The
  short code is minted when a device claims the secret, returned only to that
  device, and retained here only as a digest.  Desktop asks the operator to
  type the code their phone is showing, and approval fails without it.

  This direction matters.  An earlier shape derived the code from the pairing
  and displayed it on Desktop, so an attacker who photographed the QR from
  across the room and claimed it first would be shown the very code the
  operator's screen was displaying -- the "matching code" would have confirmed
  the attacker.  Minting per claim means a remote attacker's phone shows a code
  the operator cannot see, so the operator types the code from the phone in
  their hand and the attacker's claim is refused.  Attempts are bounded, and
  exhausting them cancels the pairing rather than allowing a guessing loop.

Secrets are never stored in the clear: only SHA-256 digests are retained, and
every comparison uses :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

#: Server-side lifetime of a pairing secret.  RFC 8628-style: short, one-use.
PAIRING_TTL_S = 300

#: Lifetime of a paired device session before the phone must pair again.
SESSION_TTL_S = 12 * 60 * 60

#: Upper bound on retained pairing records, so a stuck kiosk cannot grow memory.
MAX_PAIRINGS = 16

#: Upper bound on simultaneously paired devices for one runner.
MAX_SESSIONS = 16

#: Distinct from Cloud's ``oap_`` local-bridge secret on purpose: the two
#: surfaces must never accept each other's credentials.
PORTAL_PAIRING_SECRET_RE = re.compile(r"^oapp_[A-Za-z0-9_-]{43}$")
PORTAL_SESSION_TOKEN_RE = re.compile(r"^oaps_[A-Za-z0-9_-]{43}$")

#: Unambiguous when read aloud or typed: no 0/O, 1/I/L.
_MATCH_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_MATCH_LENGTH = 6

#: Operator attempts allowed at the confirmation prompt before the pairing is
#: cancelled outright. Six characters of a 31-symbol alphabet is ~29.7 bits, so
#: three tries is far below any useful guessing rate.
MAX_CONFIRM_ATTEMPTS = 3

_DEVICE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")

PairingState = Literal["pending", "claimed", "approved", "expired", "cancelled"]


class PairingRefused(RuntimeError):
    """A refused pairing or session operation.

    Attributes:
        reason: A closed reason code safe to return to an unauthenticated
            caller.  The message text never distinguishes "no such secret" from
            "wrong secret", so the endpoint cannot be used as an oracle.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _match_code() -> str:
    body = "".join(secrets.choice(_MATCH_ALPHABET) for _ in range(_MATCH_LENGTH))
    return f"{body[:3]}-{body[3:]}"


def _safe_device_label(raw: Any) -> str:
    """Bound and sanitize a phone-supplied label; never trust it verbatim."""
    value = re.sub(r"[\x00-\x1f\x7f]", "", str(raw or "")).strip()[:40]
    if not value or not _DEVICE_LABEL_RE.fullmatch(value):
        return "Paired phone"
    return value


@dataclass
class _Pairing:
    pairing_id: str
    secret_digest: str
    runner_id: str
    origin: str
    created_monotonic: float
    created_wall: datetime
    state: PairingState = "pending"
    session_id: str | None = None
    #: Digest of the code the claiming device was shown. Set at claim time.
    confirm_digest: str | None = None
    confirm_attempts: int = 0


@dataclass
class _Session:
    session_id: str
    token_digest: str
    csrf_digest: str
    runner_id: str
    device_label: str
    pairing_id: str
    created_monotonic: float
    created_wall: datetime
    approved: bool = False
    revoked: bool = False


@dataclass(frozen=True)
class NewPairing:
    """A freshly minted pairing.  ``secret`` exists only in this return value."""

    pairing_id: str
    secret: str
    url: str
    expires_at: str
    expires_in_s: int
    reachable_from_phone: bool

    def public(self) -> dict[str, Any]:
        """The Desktop-facing view.  Includes the URL (which carries the secret
        in its fragment) because Desktop renders the QR locally; it is never
        logged or transmitted anywhere else."""
        return {
            "pairing_id": self.pairing_id,
            "url": self.url,
            "expires_at": self.expires_at,
            "expires_in_s": self.expires_in_s,
            "reachable_from_phone": self.reachable_from_phone,
        }


@dataclass(frozen=True)
class DeviceSession:
    """An authenticated portal session belonging to one paired phone."""

    session_id: str
    runner_id: str
    device_label: str
    pairing_id: str


@dataclass
class DevicePairingStore:
    """Atomic, single-use pairing and session state for one runner.

    Args:
        runner_id: The exact runner every pairing and session is bound to.
        clock: Injected monotonic clock (tests advance it without sleeping).
            Must be monotonic; wall-clock values are only ever used for display.
    """

    runner_id: str
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pairings: dict[str, _Pairing] = field(default_factory=dict, repr=False)
    _sessions: dict[str, _Session] = field(default_factory=dict, repr=False)

    # --------------------------------------------------------------- pairings

    def create(self, origin: str, *, reachable_from_phone: bool) -> NewPairing:
        """Mint one pairing secret, valid once for :data:`PAIRING_TTL_S`."""
        secret = f"oapp_{secrets.token_urlsafe(32)}"
        if not PORTAL_PAIRING_SECRET_RE.fullmatch(secret):  # pragma: no cover
            raise PairingRefused("internal", "Generated an invalid pairing secret")
        now = self.clock()
        created_wall = datetime.now(timezone.utc)
        record = _Pairing(
            pairing_id=secrets.token_urlsafe(12),
            secret_digest=_digest(secret),
            runner_id=self.runner_id,
            origin=origin,
            created_monotonic=now,
            created_wall=created_wall,
        )
        with self._lock:
            self._expire_locked(now)
            # A newly displayed QR supersedes every earlier unapproved one, so
            # neither an abandoned code on a previous screen nor a claim already
            # made against it can be completed afterwards.
            for existing in self._pairings.values():
                if existing.state in ("pending", "claimed"):
                    self._retire_locked(existing, "cancelled")
            if len(self._pairings) >= MAX_PAIRINGS:
                oldest = min(self._pairings.values(), key=lambda p: p.created_monotonic)
                self._pairings.pop(oldest.pairing_id, None)
            self._pairings[record.pairing_id] = record
        expires_at = created_wall + timedelta(seconds=PAIRING_TTL_S)
        return NewPairing(
            pairing_id=record.pairing_id,
            secret=secret,
            url=f"{origin}/pair#c={secret}",
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
            expires_in_s=PAIRING_TTL_S,
            reachable_from_phone=reachable_from_phone,
        )

    def claim(self, secret: Any, device_label: Any = None) -> dict[str, Any]:
        """Claim a pairing secret exactly once and mint a pending session.

        The whole find-validate-transition sequence happens under one lock, so
        two concurrent scans of the same QR cannot both succeed.

        Returns:
            ``{pairing_id, match_code, session_token, csrf_token, state}``.  The
            session is ``pending_approval`` and cannot read a task or relay a
            decision until the Desktop operator confirms the matching code.

        Raises:
            PairingRefused: With a closed ``reason`` of ``malformed``,
                ``unknown_or_used``, ``expired``, or ``already_claimed``.
        """
        candidate = str(secret or "")
        if not PORTAL_PAIRING_SECRET_RE.fullmatch(candidate):
            raise PairingRefused(
                "malformed", "That pairing code is not a valid OpenAdapt portal code."
            )
        wanted = _digest(candidate)
        label = _safe_device_label(device_label)
        now = self.clock()

        with self._lock:
            self._expire_locked(now)
            record = None
            for existing in self._pairings.values():
                # Compare every candidate so a caller cannot time the lookup.
                if hmac.compare_digest(existing.secret_digest, wanted):
                    record = existing
            if record is None:
                raise PairingRefused(
                    "unknown_or_used",
                    "That pairing code is not valid. Show a new code on the computer "
                    "running OpenAdapt.",
                )
            if self._past_deadline(record, now, PAIRING_TTL_S):
                self._retire_locked(record, "expired")
                raise PairingRefused(
                    "expired",
                    "That pairing code expired. Show a new code on the computer "
                    "running OpenAdapt.",
                )
            if record.state != "pending":
                # The single-use guarantee. A second scanner lands here.
                raise PairingRefused(
                    "already_claimed",
                    "That pairing code was already used by another device. Show a "
                    "new code on the computer running OpenAdapt.",
                )

            token = f"oaps_{secrets.token_urlsafe(32)}"
            csrf = secrets.token_urlsafe(24)
            # Minted per claim and returned only to the claiming device. The
            # operator must type it back, so a remote attacker who photographed
            # the QR cannot produce it.
            confirm_code = _match_code()
            record.confirm_digest = _digest(confirm_code)
            record.confirm_attempts = 0
            session = _Session(
                session_id=secrets.token_urlsafe(12),
                token_digest=_digest(token),
                csrf_digest=_digest(csrf),
                runner_id=record.runner_id,
                device_label=label,
                pairing_id=record.pairing_id,
                created_monotonic=now,
                created_wall=datetime.now(timezone.utc),
            )
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.created_monotonic)
                self._sessions.pop(oldest.session_id, None)
            self._sessions[session.session_id] = session
            record.state = "claimed"
            record.session_id = session.session_id
            return {
                "pairing_id": record.pairing_id,
                "confirm_code": confirm_code,
                "session_token": token,
                "csrf_token": csrf,
                "device_label": label,
                "state": "pending_approval",
            }

    def approve(self, pairing_id: str, confirm_code: Any) -> dict[str, Any]:
        """Approve a claimed pairing using the code the phone is displaying.

        Args:
            pairing_id: The pairing awaiting approval.
            confirm_code: What the operator read off the phone in their hand.

        Raises:
            PairingRefused: ``not_claimed`` when nothing is waiting,
                ``expired`` once the five-minute deadline has passed, or
                ``wrong_code`` when the code does not match. Exhausting
                :data:`MAX_CONFIRM_ATTEMPTS` cancels the pairing and revokes the
                session outright, so a wrong device cannot be guessed in.
        """
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            record = self._pairings.get(str(pairing_id or ""))
            if record is None:
                raise PairingRefused(
                    "not_claimed",
                    "There is no scanned device waiting for approval.",
                )
            # Checked before the state test so an expired pairing reports why,
            # and belt-and-braces beside the sweep above: approval is the step
            # that grants a twelve-hour session, so it re-checks the deadline.
            if record.state == "expired" or self._past_deadline(
                record, now, PAIRING_TTL_S
            ):
                self._retire_locked(record, "expired")
                raise PairingRefused(
                    "expired",
                    "That pairing expired before it was approved. Show a new code.",
                )
            if record.state != "claimed" or record.session_id is None:
                raise PairingRefused(
                    "not_claimed",
                    "There is no scanned device waiting for approval.",
                )
            session = self._sessions.get(record.session_id)
            if session is None or session.revoked or record.confirm_digest is None:
                raise PairingRefused(
                    "not_claimed", "That device is no longer waiting for approval."
                )
            supplied = re.sub(r"[^A-Za-z0-9]", "", str(confirm_code or "")).upper()
            expected_len = _MATCH_LENGTH
            candidate = (
                f"{supplied[:3]}-{supplied[3:]}" if len(supplied) == expected_len else ""
            )
            if not hmac.compare_digest(record.confirm_digest, _digest(candidate)):
                record.confirm_attempts += 1
                if record.confirm_attempts >= MAX_CONFIRM_ATTEMPTS:
                    self._retire_locked(record, "cancelled")
                    raise PairingRefused(
                        "wrong_code",
                        "That code was wrong too many times, so the pairing was "
                        "cancelled. Show a new code.",
                    )
                raise PairingRefused(
                    "wrong_code",
                    "That code does not match the one on the phone. Check the "
                    "phone in your hand.",
                )
            session.approved = True
            record.state = "approved"
            return {
                "pairing_id": record.pairing_id,
                "session_id": session.session_id,
                "device_label": session.device_label,
                "state": "approved",
            }

    def cancel(self, pairing_id: str) -> dict[str, Any]:
        """Cancel a pairing and revoke any session it already minted."""
        with self._lock:
            self._expire_locked(self.clock())
            record = self._pairings.get(str(pairing_id or ""))
            if record is None:
                raise PairingRefused("unknown", "There is no such pairing.")
            self._retire_locked(record, "cancelled", revoke_approved=True)
            return {"pairing_id": record.pairing_id, "state": "cancelled"}

    def pairing_status(self, pairing_id: str) -> dict[str, Any]:
        """Poll one pairing's state for the Desktop approval screen."""
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            record = self._pairings.get(str(pairing_id or ""))
            if record is None:
                raise PairingRefused("unknown", "There is no such pairing.")
            session = (
                self._sessions.get(record.session_id)
                if record.session_id is not None
                else None
            )
            remaining = max(0, int(PAIRING_TTL_S - (now - record.created_monotonic)))
            return {
                "pairing_id": record.pairing_id,
                "state": record.state,
                "expires_in_s": remaining,
                "attempts_remaining": max(
                    0, MAX_CONFIRM_ATTEMPTS - record.confirm_attempts
                ),
                "device_label": session.device_label if session is not None else None,
            }

    # --------------------------------------------------------------- sessions

    def authenticate(self, token: Any) -> DeviceSession:
        """Resolve a bearer token to an approved, unexpired device session.

        Raises:
            PairingRefused: For a malformed, unknown, unapproved, revoked, or
                expired session.  ``reason`` is ``pending_approval`` only for a
                claimed-but-unapproved session, so the phone can show the
                matching code and wait.
        """
        candidate = str(token or "")
        if not PORTAL_SESSION_TOKEN_RE.fullmatch(candidate):
            raise PairingRefused("unauthorized", "This device is not paired.")
        wanted = _digest(candidate)
        now = self.clock()
        with self._lock:
            found = None
            for session in self._sessions.values():
                if hmac.compare_digest(session.token_digest, wanted):
                    found = session
            if found is None or found.revoked:
                raise PairingRefused("unauthorized", "This device is not paired.")
            if self._past_deadline(found, now, SESSION_TTL_S):
                found.revoked = True
                raise PairingRefused("expired", "This device's pairing expired.")
            if not found.approved:
                raise PairingRefused(
                    "pending_approval",
                    "Waiting for approval on the computer running OpenAdapt.",
                )
            if found.runner_id != self.runner_id:  # pragma: no cover - defensive
                raise PairingRefused("wrong_runner", "This device is not paired.")
            return DeviceSession(
                session_id=found.session_id,
                runner_id=found.runner_id,
                device_label=found.device_label,
                pairing_id=found.pairing_id,
            )

    def verify_csrf(self, session: DeviceSession, token: Any) -> bool:
        """Constant-time check of a session's mutation token."""
        wanted = _digest(str(token or ""))
        with self._lock:
            record = self._sessions.get(session.session_id)
            if record is None or record.revoked:
                return False
            return hmac.compare_digest(record.csrf_digest, wanted)

    def revoke(self, session_id: str) -> dict[str, Any]:
        """Revoke one paired device immediately."""
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
            if session is None:
                raise PairingRefused("unknown", "There is no such paired device.")
            session.revoked = True
            return {"session_id": session.session_id, "state": "revoked"}

    def devices(self) -> list[dict[str, Any]]:
        """List live paired devices.  Never exposes a token or its digest."""
        now = self.clock()
        with self._lock:
            return [
                {
                    "session_id": session.session_id,
                    "device_label": session.device_label,
                    "approved": session.approved,
                    "expires_in_s": max(
                        0, int(SESSION_TTL_S - (now - session.created_monotonic))
                    ),
                }
                for session in self._sessions.values()
                if not session.revoked
                and not self._past_deadline(session, now, SESSION_TTL_S)
            ]

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _past_deadline(record: Any, now: float, ttl: float) -> bool:
        """Whether ``record`` is past ``ttl`` by either clock.

        Monotonic time is authoritative because a wall-clock change must not
        extend a deadline.  It is not sufficient on its own, though:
        ``CLOCK_MONOTONIC`` does not advance while a laptop is suspended, so a
        machine slept overnight would otherwise carry a live pairing or session
        far past its intended lifetime.  Whichever clock says "expired" wins.
        """
        if now - record.created_monotonic >= ttl:
            return True
        elapsed_wall = (
            datetime.now(timezone.utc) - record.created_wall
        ).total_seconds()
        return elapsed_wall >= ttl

    def _retire_locked(
        self, record: _Pairing, state: PairingState, *, revoke_approved: bool = False
    ) -> None:
        """Retire a pairing and revoke the session it minted, if any.

        An already-approved session normally outlives its pairing: it has its
        own twelve-hour lifetime, and a later QR should not sign a working
        phone out.  An explicit operator cancel is different -- that is a
        direct instruction to undo this pairing -- so it passes
        ``revoke_approved``.
        """
        record.state = state
        if record.session_id is not None:
            session = self._sessions.get(record.session_id)
            if session is not None and (revoke_approved or not session.approved):
                session.revoked = True

    def _expire_locked(self, now: float) -> None:
        """Retire timed-out pairings.  Caller holds the lock.

        Both ``pending`` and ``claimed`` records expire.  Expiring only
        ``pending`` would let a pairing claimed at T+299s be approved hours
        later, minting a full-length session from a long-dead code.
        """
        for record in self._pairings.values():
            if record.state in ("pending", "claimed") and self._past_deadline(
                record, now, PAIRING_TTL_S
            ):
                self._retire_locked(record, "expired")
