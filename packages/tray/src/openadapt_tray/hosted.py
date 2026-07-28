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


@dataclass
class CountResult:
    """Parsed response from the needs-attention count endpoint."""

    count: int
    halts: int = 0
    uncertain_dispatches: int = 0

    @classmethod
    def from_payload(cls, payload: dict) -> "CountResult":
        """Build a result from the JSON body, tolerating missing subfields."""
        return cls(
            count=int(payload.get("count", 0)),
            halts=int(payload.get("halts", 0)),
            uncertain_dispatches=int(payload.get("uncertain_dispatches", 0)),
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
            return CountResult.from_payload(resp.json())
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
            self._fire_notification(result.count)

        self._last_count = result.count

    def _fire_notification(self, count: int) -> None:
        """Fire the 'N automations need attention' notification."""
        if not self._notifier:
            return
        noun = "automation" if count == 1 else "automations"
        try:
            self._notifier.show(
                "Automations need attention",
                f"{count} {noun} need attention",
                urgency="critical",
                on_clicked=self._on_break_clicked or self._default_click,
            )
        except Exception as e:
            print(f"Failed to show needs-attention notification: {e}")

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
) -> None:
    """Route a break/needs-attention click by deployment lane.

    Args:
        config: Tray configuration (provides lane + hosted_url).
        ipc_client: Optional IPC client used for the byoc local-teach route.
    """
    # PHI stays local on the byoc lane: open the desktop teach view over IPC,
    # and fall through to the hosted dashboard only if the desktop is
    # unreachable (or there is no IPC client to reach it with).
    if config.deployment_lane == "byoc" and ipc_client is not None:
        try:
            ipc_client.send_open_teach()
            return
        except Exception as e:
            print(f"Failed to route byoc break click to desktop: {e}")
    webbrowser.open(f"{config.hosted_url.rstrip('/')}/dashboard")
