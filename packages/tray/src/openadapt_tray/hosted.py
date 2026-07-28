"""Cloud needs-attention poller for OpenAdapt Tray.

Polls the hosted control plane for the number of automations that need
attention and surfaces it as a tray badge + a desktop notification when the
count rises. Authenticated with the ingest bearer token from the OS keychain.

Contract (spec §3c) — the exact request this module issues::

    GET {hosted_url}/api/needs-attention/count
    Authorization: Bearer <ingest token>
    → 200 { "count": <int>, "halts": <int>, "uncertain_dispatches": <int> }

Click routing is lane-aware:
  * cloud lane → open ``{hosted_url}/dashboard`` in the browser
  * byoc lane  → IPC ``open_teach`` to the desktop (the fix stays local)
"""

import hashlib
import hmac
import json
import os
import re
import threading
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from openadapt_tray.config import (
    MIN_POLL_INTERVAL_S,
    OFFLINE_POLL_INTERVAL_S,
    TrayConfig,
)
from openadapt_tray.state import CredentialState, CredentialStatus

COUNT_ENDPOINT_PATH = "/api/needs-attention/count"
REQUEST_TIMEOUT_S = 10.0
CREDENTIAL_WARNING_DAYS = 14
CREDENTIAL_CONTRACT_KEYS = {
    "expires_at",
    "expires_in_days",
    "expiring_soon",
    "legacy_non_expiring",
    "warning_days",
}
CREDENTIAL_WARNING_STATE_VERSION = 1
MAX_DELIVERED_CREDENTIAL_WARNINGS = 32
INGEST_TOKEN_PATTERN = re.compile(r"^oai_ingest_[A-Za-z0-9_-]{43}$")
CREDENTIAL_IDENTITY_DOMAIN = b"openadapt-tray/credential-identity/v1"


class InvalidCountPayload(ValueError):
    """The count endpoint returned a body we cannot read as a count.

    Raised instead of defaulting, because an unreadable body is NOT a count of
    zero: zero renders as "nothing needs attention", which is the one answer we
    must never invent.
    """


class InvalidCredentialPayload(ValueError):
    """The additive credential block does not match the closed contract."""


class CredentialWarningStateError(RuntimeError):
    """The local warning-deduplication state could not be read or written."""


def _is_json_int(value: object) -> bool:
    """Return whether a value is a JSON integer rather than a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_utc_expiry(value: object) -> str:
    """Validate and return an ISO-8601 UTC expiry string."""
    if not isinstance(value, str) or not value:
        raise InvalidCredentialPayload("expires_at must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidCredentialPayload("expires_at must be ISO-8601") from e
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise InvalidCredentialPayload("expires_at must use UTC")
    return value


def _header_values(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return case-insensitive response headers for contract validation."""
    if headers is None:
        return None
    return {str(key).lower(): str(value) for key, value in headers.items()}


def is_valid_ingest_token(token: object) -> bool:
    """Return whether a value matches the closed hosted bearer format."""
    return isinstance(token, str) and INGEST_TOKEN_PATTERN.fullmatch(token) is not None


def parse_credential_status(
    value: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> CredentialStatus:
    """Parse the control plane's closed credential-status contract.

    The server's ``expiring_soon`` value is authoritative. The tray validates
    it, but never recomputes it from the local clock or rounded day count.
    """
    if not isinstance(value, dict):
        raise InvalidCredentialPayload("credential must be an object")
    if set(value) != CREDENTIAL_CONTRACT_KEYS:
        raise InvalidCredentialPayload("credential has unexpected or missing fields")

    warning_days = value["warning_days"]
    if not _is_json_int(warning_days) or warning_days != CREDENTIAL_WARNING_DAYS:
        raise InvalidCredentialPayload("warning_days does not match the contract")
    expiring_soon = value["expiring_soon"]
    legacy = value["legacy_non_expiring"]
    if not isinstance(expiring_soon, bool) or not isinstance(legacy, bool):
        raise InvalidCredentialPayload("credential flags must be booleans")

    normalized_headers = _header_values(headers)
    if normalized_headers is not None:
        if normalized_headers.get("cache-control") != "no-store":
            raise InvalidCredentialPayload("cache-control header does not match")
        if normalized_headers.get(
            "x-openadapt-credential-warning-days"
        ) != str(CREDENTIAL_WARNING_DAYS):
            raise InvalidCredentialPayload("warning-days header does not match")

    if legacy:
        if value["expires_at"] is not None or value["expires_in_days"] is not None:
            raise InvalidCredentialPayload("legacy credential must not carry expiry")
        if expiring_soon:
            raise InvalidCredentialPayload("legacy credential cannot be expiring soon")
        if normalized_headers is not None and (
            "x-openadapt-credential-expires-in-days" in normalized_headers
        ):
            raise InvalidCredentialPayload("legacy credential must omit expiry header")
        return CredentialStatus(
            state=CredentialState.LEGACY,
            warning_days=CREDENTIAL_WARNING_DAYS,
        )

    expires_at = _parse_utc_expiry(value["expires_at"])
    expires_in_days = value["expires_in_days"]
    if not _is_json_int(expires_in_days) or expires_in_days < 0:
        raise InvalidCredentialPayload("expires_in_days must be a non-negative integer")
    if expires_in_days < CREDENTIAL_WARNING_DAYS and not expiring_soon:
        raise InvalidCredentialPayload("expiry status contradicts the day count")
    if expires_in_days > CREDENTIAL_WARNING_DAYS and expiring_soon:
        raise InvalidCredentialPayload("expiry status contradicts the day count")
    if normalized_headers is not None and normalized_headers.get(
        "x-openadapt-credential-expires-in-days"
    ) != str(expires_in_days):
        raise InvalidCredentialPayload("expiry-days header does not match")

    return CredentialStatus(
        state=(CredentialState.EXPIRING if expiring_soon else CredentialState.ACTIVE),
        expires_at=expires_at,
        expires_in_days=expires_in_days,
        warning_days=CREDENTIAL_WARNING_DAYS,
    )


def credential_identity(hosted_url: str, token: str) -> str:
    """Return a non-secret identity digest without storing or logging a token."""
    message = CREDENTIAL_IDENTITY_DOMAIN + b"\0" + hosted_url.rstrip("/").encode()
    return hmac.new(
        token.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def credential_warning_key(identity: str, credential: CredentialStatus) -> str:
    """Bind a delivered warning to the credential identity and expiry version."""
    material = json.dumps(
        {
            "contract": CREDENTIAL_WARNING_STATE_VERSION,
            "credential": identity,
            "expires_at": credential.expires_at,
            "warning_days": credential.warning_days,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


class CredentialWarningStore:
    """Small persistent store for delivered credential-warning identities."""

    def __init__(self, path: Path | None = None):
        self.path = path or TrayConfig.config_path().with_name(
            "credential-warning-state.json"
        )

    def _read(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise CredentialWarningStateError("warning state is unreadable") from e
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "delivered"}
            or payload["version"] != CREDENTIAL_WARNING_STATE_VERSION
            or not isinstance(payload["delivered"], list)
            or any(not isinstance(item, str) for item in payload["delivered"])
        ):
            raise CredentialWarningStateError("warning state has an invalid schema")
        return payload["delivered"][-MAX_DELIVERED_CREDENTIAL_WARNINGS:]

    def was_delivered(self, warning_key: str) -> bool:
        """Return whether the exact credential warning was delivered."""
        return warning_key in self._read()

    def mark_delivered(self, warning_key: str) -> None:
        """Persist one delivered warning without storing the credential."""
        try:
            delivered = self._read()
        except CredentialWarningStateError:
            # The failed read remains visible to the caller of ``was_delivered``.
            # A confirmed new delivery can safely replace this non-secret cache.
            delivered = []
        delivered = [item for item in delivered if item != warning_key]
        delivered.append(warning_key)
        delivered = delivered[-MAX_DELIVERED_CREDENTIAL_WARNINGS:]
        payload = {
            "version": CREDENTIAL_WARNING_STATE_VERSION,
            "delivered": delivered,
        }
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as e:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CredentialWarningStateError("warning state could not be saved") from e


def _optional_int(payload: dict, key: str) -> int:
    """Read an optional integer subfield.

    Absent means zero (documented tolerance for the display-only subfields).
    Present but unreadable means the body is malformed, which is an error.
    """
    if key not in payload:
        return 0
    try:
        return int(payload[key])
    except (TypeError, ValueError) as e:
        raise InvalidCountPayload(f"{key!r} is not an integer: {payload[key]!r}") from e


@dataclass
class CountResult:
    """Parsed response from the needs-attention count endpoint."""

    count: int
    halts: int = 0
    uncertain_dispatches: int = 0
    credential: CredentialStatus = field(default_factory=CredentialStatus.unknown)
    credential_error: str | None = field(default=None, repr=False, compare=False)
    credential_identity: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
        credential_id: str | None = None,
    ) -> "CountResult":
        """Build a result from the JSON body.

        ``count`` is the safety-critical number: it drives the badge and the
        "N automations need attention" notification. A body without a readable
        integer ``count`` means we do NOT know the count, so this raises
        :class:`InvalidCountPayload` rather than substituting ``0`` -- an
        absent field used to render as a confident all-clear.

        The display-only subfields (``halts``, ``uncertain_dispatches``) stay
        tolerant of absence; see :func:`_optional_int`.

        Args:
            payload: The decoded JSON body.

        Returns:
            A parsed :class:`CountResult`.

        Raises:
            InvalidCountPayload: The body is not a JSON object, has no
                ``count``, or carries a count that is not a non-negative
                integer.
        """
        if not isinstance(payload, dict):
            raise InvalidCountPayload(
                f"expected a JSON object, got {type(payload).__name__}"
            )
        if "count" not in payload:
            raise InvalidCountPayload("response body has no 'count' field")
        count = payload["count"]
        # JSON booleans are Python integers, and ``int(1.9)`` truncates. Both
        # conversions can turn an unreadable response into a confident badge
        # value, including the unsafe ``false`` -> ``0`` all-clear.
        if isinstance(count, bool) or not isinstance(count, int):
            raise InvalidCountPayload(f"'count' is not an integer: {count!r}")
        if count < 0:
            raise InvalidCountPayload(f"'count' is negative: {count}")
        credential = CredentialStatus.unknown()
        credential_error = None
        if "credential" not in payload:
            credential_error = "credential block is absent"
        else:
            try:
                credential = parse_credential_status(
                    payload["credential"], headers=headers
                )
            except InvalidCredentialPayload as e:
                credential_error = str(e)
        return cls(
            count=count,
            halts=_optional_int(payload, "halts"),
            uncertain_dispatches=_optional_int(payload, "uncertain_dispatches"),
            credential=credential,
            credential_error=credential_error,
            credential_identity=credential_id,
        )


def count_url(hosted_url: str) -> str:
    """Return the fully-qualified count endpoint URL for a hosted host."""
    return f"{hosted_url.rstrip('/')}{COUNT_ENDPOINT_PATH}"


class HostedPoller:
    """Background poller for the cloud needs-attention count.

    The poller is transport-thin and testable: :meth:`poll_once` performs a
    single authenticated request and returns a :class:`CountResult` (or
    ``None`` on error/offline), while :meth:`start`/:meth:`stop` run it on an
    interval with exponential-ish back-off.
    """

    def __init__(
        self,
        config: TrayConfig,
        on_count: Callable[[CountResult], None],
        notifier: object | None = None,
        on_break_clicked: Callable[[], None] | None = None,
        on_credential: Callable[[CredentialStatus], None] | None = None,
        on_credential_clicked: Callable[[], None] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        set_offline: Callable[[bool], None] | None = None,
        warning_store: CredentialWarningStore | None = None,
    ):
        """Initialize the poller.

        Args:
            config: Tray configuration (hosted_url, lane, poll interval).
            on_count: Called with each successful :class:`CountResult` (used to
                drive the tray badge / state).
            notifier: Optional object with a ``show(title, body, on_clicked=…)``
                method (a :class:`NotificationManager`).
            on_break_clicked: Optional lane-aware click handler for the
                notification. Defaults to :func:`route_break_click` behaviour.
            on_credential: Receives each validated credential status, including
                the fail-visible unknown and signed-out states.
            on_credential_clicked: Opens the hosted credential renewal page.
            token_provider: Returns the current bearer token. Defaults to
                ``config.get_ingest_token``.
            set_offline: Optional callback invoked with the offline boolean each
                cycle (used to drive the tray sync channel).
            warning_store: Persistent delivered-warning store. Tests can supply
                an isolated store.
        """
        self.config = config
        self._on_count = on_count
        self._notifier = notifier
        self._on_break_clicked = on_break_clicked
        self._on_credential = on_credential
        self._on_credential_clicked = on_credential_clicked
        self._token_provider = token_provider or config.get_ingest_token
        self._set_offline = set_offline
        self._warning_store = warning_store or CredentialWarningStore()

        self._last_count = 0
        self._last_had_token = False
        self._current_interval = config.effective_poll_interval_s()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- single-shot request ------------------------------------------------

    def poll_once(self) -> CountResult | None:
        """Perform one authenticated count request.

        Returns:
            A :class:`CountResult` on success, or ``None`` if unauthenticated,
            offline, or the server errored.
        """
        token = self._token_provider()
        if not token:
            self._last_had_token = False
            # Not logged in yet — treat as offline for badge purposes.
            return None
        self._last_had_token = True
        if not is_valid_ingest_token(token):
            print("needs-attention poll refused an invalid credential format")
            return None

        url = count_url(self.config.hosted_url)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"needs-attention count returned {resp.status_code}")
                return None
            try:
                identity = credential_identity(self.config.hosted_url, token)
                result = CountResult.from_payload(
                    resp.json(),
                    headers=getattr(resp, "headers", None),
                    credential_id=identity,
                )
                if result.credential_error:
                    print(
                        "needs-attention credential status unusable: "
                        f"{result.credential_error}"
                    )
                return result
            except InvalidCountPayload as e:
                # A body we cannot read is not a count of zero. Report it as a
                # failed poll so the badge keeps its last known value instead of
                # clearing to a confident all-clear.
                print(f"needs-attention count response unusable: {e}")
                return None
        except Exception as e:
            # Network error / DNS / timeout / bad JSON → offline.
            print(f"needs-attention poll failed: {e}")
            return None

    # --- badge + notification -----------------------------------------------

    def _handle_result(self, result: CountResult | None) -> None:
        """Apply a poll result: update badge, notify on increase, set interval."""
        if result is None:
            # Offline / unauthenticated → back off, mark offline.
            self._current_interval = OFFLINE_POLL_INTERVAL_S
            if self._set_offline:
                self._set_offline(True)
            if self._on_credential:
                credential = (
                    CredentialStatus.unknown()
                    if self._last_had_token
                    else CredentialStatus.signed_out()
                )
                self._on_credential(credential)
            return

        # Online: restore configured interval (respecting the floor).
        self._current_interval = max(
            MIN_POLL_INTERVAL_S, self.config.effective_poll_interval_s()
        )
        if self._set_offline:
            self._set_offline(False)

        # Drive the badge/state.
        self._on_count(result)
        if self._on_credential:
            self._on_credential(result.credential)

        self._maybe_warn_credential(result)

        # Notify only when the count RISES (0→N or N→N+1), never on a decrease.
        if result.count > self._last_count and result.count > 0:
            if self._fire_notification(result.count):
                self._last_count = result.count
            # Not delivered: leave ``_last_count`` behind on purpose. Advancing
            # it would record the user as informed about breaks they were never
            # shown, and the count would have to rise AGAIN before we tried a
            # second time. Holding it back makes the next poll retry.
        else:
            self._last_count = result.count

    def _maybe_warn_credential(self, result: CountResult) -> None:
        """Show one persistent warning for an expiring credential version."""
        credential = result.credential
        if credential.state != CredentialState.EXPIRING:
            return
        if not result.credential_identity or credential.expires_at is None:
            print("credential warning not sent: credential identity is unavailable")
            return

        warning_key = credential_warning_key(
            result.credential_identity,
            credential,
        )
        try:
            if self._warning_store.was_delivered(warning_key):
                return
        except CredentialWarningStateError as e:
            print(f"credential warning state failure: {e}")

        if not self._fire_credential_notification(credential):
            return
        try:
            self._warning_store.mark_delivered(warning_key)
        except CredentialWarningStateError as e:
            # Do not mark a warning as durable when the local record failed.
            # The next poll retries the user-visible warning.
            print(f"credential warning state failure: {e}")

    def _fire_credential_notification(self, credential: CredentialStatus) -> bool:
        """Show a PHI-free actionable warning for an expiring credential."""
        if not self._notifier:
            return False
        days = credential.expires_in_days
        if days is None or credential.expires_at is None:
            return False
        deadline = datetime.fromisoformat(
            credential.expires_at.replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
        day_text = "day" if days == 1 else "days"
        try:
            delivered = self._notifier.show(
                "OpenAdapt sign-in expires soon",
                f"Your local connection expires in {days} {day_text} "
                f"({deadline}). Renew it now.",
                urgency="critical",
                on_clicked=self._on_credential_clicked
                or self._default_credential_click,
            )
        except Exception as e:
            print(f"Failed to show credential warning: {e}")
            return False
        if not delivered:
            print("credential expiry warning was not delivered; retrying later")
            return False
        return True

    def _default_credential_click(self) -> None:
        """Open the account credential settings page."""
        try:
            opened = webbrowser.open(
                f"{self.config.hosted_url.rstrip('/')}/dashboard/settings/ingest"
            )
        except Exception as e:
            print(f"Could not open credential settings: {e}")
            return
        if not opened:
            print("Could not open credential settings: no usable browser")

    def _fire_notification(self, count: int) -> bool:
        """Fire the 'N automations need attention' notification.

        Args:
            count: The number of automations needing attention.

        Returns:
            True only if the notifier reported the notification as delivered.
            A notifier that cannot deliver (no toast backend, a dead
            notification daemon) returns False from its ``show``; that answer is
            now propagated instead of discarded.
        """
        if not self._notifier:
            return False
        noun = "automation" if count == 1 else "automations"
        try:
            delivered = self._notifier.show(
                "Automations need attention",
                f"{count} {noun} need attention",
                urgency="critical",
                on_clicked=self._on_break_clicked or self._default_click,
            )
        except Exception as e:
            print(f"Failed to show needs-attention notification: {e}")
            return False
        if not delivered:
            print(
                f"needs-attention notification was NOT delivered ({count} {noun} "
                "need attention); retrying on the next poll"
            )
            return False
        return True

    def _default_click(self) -> None:
        """Fallback click handler (cloud-lane browser open)."""
        route_break_click(self.config, ipc_client=None)

    # --- loop ---------------------------------------------------------------

    def start(self) -> None:
        """Start the background poll loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Poll on an interval until stopped."""
        while not self._stop.is_set():
            result = self.poll_once()
            self._handle_result(result)
            # Wait for the (possibly backed-off) interval, or until stopped.
            self._stop.wait(self._current_interval)

    def stop(self) -> None:
        """Stop the background poll loop."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def current_interval(self) -> int:
        """The interval (seconds) the loop will wait after the last cycle."""
        return self._current_interval


def route_break_click(
    config: TrayConfig,
    ipc_client: object | None = None,
) -> bool:
    """Route a break/needs-attention click by deployment lane.

    Args:
        config: Tray configuration (provides lane + hosted_url).
        ipc_client: Optional IPC client used for the byoc local-teach route.

    Returns:
        True if the click was routed somewhere the user can see. False means
        neither route worked and NOTHING opened -- the user clicked and the
        screen did not change, so the caller must say so rather than assume the
        click landed.
    """
    # PHI stays local on the byoc lane: open the desktop teach view over IPC,
    # and fall through to the hosted dashboard only if the desktop is
    # unreachable (or there is no IPC client to reach it with).
    if config.deployment_lane == "byoc" and ipc_client is not None:
        try:
            if ipc_client.send_open_teach():
                return True
            print("Desktop refused the open-teach command; trying the dashboard")
        except Exception as e:
            print(f"Failed to route byoc break click to desktop: {e}")
    # webbrowser.open returns False when it could not find or start a browser.
    # Discarding that made a dead click indistinguishable from a served one.
    try:
        opened = webbrowser.open(f"{config.hosted_url.rstrip('/')}/dashboard")
    except Exception as e:
        print(f"Could not open the hosted dashboard: {e}")
        return False
    if not opened:
        print("Could not open the hosted dashboard: no usable browser")
    return bool(opened)
