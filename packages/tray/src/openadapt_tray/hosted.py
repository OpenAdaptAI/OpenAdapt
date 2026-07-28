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

import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from openadapt_tray.config import (
    MIN_POLL_INTERVAL_S,
    OFFLINE_POLL_INTERVAL_S,
    TrayConfig,
)

COUNT_ENDPOINT_PATH = "/api/needs-attention/count"
REQUEST_TIMEOUT_S = 10.0


class InvalidCountPayload(ValueError):
    """The count endpoint returned a body we cannot read as a count.

    Raised instead of defaulting, because an unreadable body is NOT a count of
    zero: zero renders as "nothing needs attention", which is the one answer we
    must never invent.
    """


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

    @classmethod
    def from_payload(cls, payload: object) -> "CountResult":
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
        return cls(
            count=count,
            halts=_optional_int(payload, "halts"),
            uncertain_dispatches=_optional_int(payload, "uncertain_dispatches"),
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
        token_provider: Callable[[], str | None] | None = None,
        set_offline: Callable[[bool], None] | None = None,
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
            token_provider: Returns the current bearer token. Defaults to
                ``config.get_ingest_token``.
            set_offline: Optional callback invoked with the offline boolean each
                cycle (used to drive the tray sync channel).
        """
        self.config = config
        self._on_count = on_count
        self._notifier = notifier
        self._on_break_clicked = on_break_clicked
        self._token_provider = token_provider or config.get_ingest_token
        self._set_offline = set_offline

        self._last_count = 0
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
            # Not logged in yet — treat as offline for badge purposes.
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
                return CountResult.from_payload(resp.json())
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
            return

        # Online: restore configured interval (respecting the floor).
        self._current_interval = max(
            MIN_POLL_INTERVAL_S, self.config.effective_poll_interval_s()
        )
        if self._set_offline:
            self._set_offline(False)

        # Drive the badge/state.
        self._on_count(result)

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
