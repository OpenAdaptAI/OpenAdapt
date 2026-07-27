"""Portal lifecycle: start, describe, pair, notify, and stop.

Desktop's half of the mobile attended-decision loop is a lifecycle, not a
decision.  :class:`PortalService` owns:

* resolving and validating ingress before anything binds
  (:mod:`engine.portal.ingress`);
* launching and supervising the ``openadapt-flow console --attend`` process
  that produces every task, evidence artifact, and decision outcome;
* running the phone-facing portal socket (:mod:`engine.portal.server`);
* minting and approving one-use QR device pairings
  (:mod:`engine.portal.pairing`);
* projecting a generic operating-system notification
  (:mod:`engine.portal.notifications`).

Everything downstream of "relay this request" belongs to ``openadapt-flow``.

Known seam, called out deliberately: the attended console generates its bearer
capability inside ``serve()`` and only prints it on stdout.  There is no
injection point today, so :func:`_parse_console_banner` reads the exact banner
line.  A narrow ``--capability-file`` option in Flow would replace this with a
supported interface; until then the parser is strict and fails loud rather than
guessing.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from engine.flow_bridge import FLOW_BIN, _flow_command, _subprocess_env
from engine.portal.flow_client import FlowConsoleClient, FlowConsoleUnavailable
from engine.portal.ingress import IngressError, PortalIngress, resolve_ingress
from engine.portal.notifications import (
    assert_generic_notification,
    build_notification,
    notification_from_upstream,
)
from engine.portal.pairing import DevicePairingStore, PairingRefused
from engine.portal.server import PortalApp, PortalServer

#: The exact banner ``openadapt_flow.console.server.serve`` prints.
_CONSOLE_BANNER = re.compile(
    r"^\s*http://(?:127\.0\.0\.1|localhost):(?P<port>\d{1,5})/#token="
    r"(?P<token>[A-Za-z0-9_-]{16,})\s*$"
)

#: How long to wait for the console to announce itself before failing loud.
CONSOLE_START_TIMEOUT_S = 60.0


class PortalError(RuntimeError):
    """The portal could not be started or is not in a state to serve."""


def _terminate(process: Any) -> None:
    """Stop a console process without leaving a zombie behind."""
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:  # pragma: no cover - defensive
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def _drain(stream: Any) -> None:
    """Consume a pipe forever so the child never blocks writing to it."""
    if stream is None:  # pragma: no cover - defensive
        return
    try:
        for _line in iter(stream.readline, ""):
            pass
    except (OSError, ValueError):  # pragma: no cover - pipe closed
        pass


def _drain_for_banner(stream: Any, found: "queue.Queue") -> None:
    """Publish the console's capability banner, then keep draining stdout.

    When the stream ends without a banner -- the console exited, or printed an
    install hint instead -- a ``None`` sentinel is published so the caller
    fails immediately instead of waiting out the whole start timeout.
    """
    published = False
    try:
        if stream is not None:
            for line in iter(stream.readline, ""):
                if not published:
                    parsed = _parse_console_banner(line)
                    if parsed is not None:
                        published = True
                        try:
                            found.put_nowait(parsed)
                        except queue.Full:  # pragma: no cover - defensive
                            pass
    except (OSError, ValueError):  # pragma: no cover - pipe closed
        pass
    if not published:
        try:
            found.put_nowait(None)
        except queue.Full:  # pragma: no cover - defensive
            pass


def _parse_console_banner(line: str) -> tuple[int, str] | None:
    match = _CONSOLE_BANNER.match(line)
    if match is None:
        return None
    return int(match.group("port")), match.group("token")


@dataclass
class ConsoleProcess:
    """A supervised attended-console subprocess."""

    process: Any
    port: int
    access_token: str

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if not self.alive():
            return
        _terminate(self.process)


class PortalService:
    """Owns the runner-local decision portal for one Desktop process.

    Args:
        config: Engine configuration supplying the ``portal_*`` fields.
        popen: Injected process spawner (tests supply a fake).
        clock: Injected monotonic clock shared with the pairing store.
    """

    def __init__(
        self,
        config: Any,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._ingress: PortalIngress | None = None
        self._server: PortalServer | None = None
        self._console: ConsoleProcess | None = None
        self._flow: FlowConsoleClient | None = None
        self._pairings: DevicePairingStore | None = None
        self._last_error: str | None = None

    # -------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._server is not None

    def runner_id(self) -> str:
        """A stable, non-secret local runner identifier for pairing binding."""
        import hashlib
        import socket

        raw = f"{socket.gethostname()}|{Path.home()}"
        return f"runner_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"

    def start(self) -> dict[str, Any]:
        """Start the portal, or fail closed with an exact reason.

        Raises:
            PortalError: If ingress is not fully configured, the attended
                console cannot start, or the socket cannot bind.  The portal is
                left stopped; it never falls back to a wider bind address.
        """
        with self._lock:
            if self._server is not None:
                return self._status_locked()
            try:
                # Fail closed before spawning anything: a misconfigured
                # ingress must never start a console it cannot serve.
                resolve_ingress(self.config)
            except IngressError as exc:
                self._last_error = str(exc)
                raise PortalError(str(exc)) from exc

        # Spawning and waiting for the console happens outside the lock: a
        # console that starts but never announces itself must not be able to
        # wedge status(), stop(), or any portal command for the whole timeout.
        console_process = self._start_console()

        with self._lock:
            if self._server is not None:  # pragma: no cover - concurrent start
                console_process.stop()
                return self._status_locked()
            try:
                ingress = resolve_ingress(self.config)
            except IngressError as exc:
                self._last_error = str(exc)
                raise PortalError(str(exc)) from exc

            console = console_process
            flow = FlowConsoleClient(
                port=console.port, access_token=console.access_token
            )
            try:
                session = flow.request("session")
                if isinstance(session.json, dict):
                    flow.csrf_token = str(session.json.get("csrf_token") or "")
            except FlowConsoleUnavailable as exc:
                console.stop()
                self._last_error = str(exc)
                raise PortalError(
                    "The local decision service started but did not answer. "
                    "Stop the portal and try again."
                ) from exc

            pairings = DevicePairingStore(
                runner_id=self.runner_id(), clock=self._clock
            )
            server = PortalServer(PortalApp(ingress, pairings, flow))
            try:
                port = server.start()
            except OSError as exc:
                console.stop()
                self._last_error = str(exc)
                raise PortalError(
                    f"The portal could not listen on {ingress.bind_host}:{ingress.port}."
                ) from exc

            if ingress.port == 0:
                # An ephemeral port only makes sense for the loopback default;
                # a published ingress must name the port it forwards to.
                ingress = PortalIngress(
                    mode=ingress.mode,
                    bind_host=ingress.bind_host,
                    port=port,
                    public_origin=(
                        f"http://{ingress.bind_host}:{port}"
                        if ingress.mode == "loopback"
                        else ingress.public_origin
                    ),
                    reachable_from_phone=ingress.reachable_from_phone,
                )
                server.app.ingress = ingress

            self._ingress = ingress
            self._console = console
            self._flow = flow
            self._pairings = pairings
            self._server = server
            self._last_error = None
            logger.info(
                "Decision portal listening on {h}:{p} ({m})",
                h=ingress.bind_host,
                p=port,
                m=ingress.mode,
            )
            return self._status_locked()

    def _start_console(self) -> ConsoleProcess:
        prefix = _flow_command(FLOW_BIN)
        if prefix is None:
            raise PortalError(
                "OpenAdapt Flow is not available, so there is nothing to decide "
                "about. Install openadapt-flow with its console extra."
            )
        command = [
            *prefix,
            "console",
            "--attend",
            "--allow-actions",
            "--bundles",
            str(Path(self.config.data_dir) / "bundles"),
            "--runs",
            str(Path(self.config.data_dir) / "runs"),
            "--port",
            str(int(getattr(self.config, "portal_console_port", 7863))),
        ]
        process = self._popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subprocess_env(),
        )
        # Both pipes are drained for the process's lifetime. A blocking
        # readline() here would ignore the timeout entirely (it is only checked
        # between lines) and would wedge the caller's lock; an undrained stderr
        # or stdout would block the console itself once its pipe buffer filled.
        banner: queue.Queue = queue.Queue(maxsize=1)
        threading.Thread(
            target=_drain_for_banner, args=(process.stdout, banner), daemon=True
        ).start()
        threading.Thread(target=_drain, args=(process.stderr,), daemon=True).start()
        try:
            parsed = banner.get(timeout=CONSOLE_START_TIMEOUT_S)
        except queue.Empty:
            parsed = None
        port, token = parsed if parsed is not None else (0, "")
        if token and 1 <= port <= 65535:
            return ConsoleProcess(process=process, port=port, access_token=token)
        _terminate(process)
        raise PortalError(
            "The local decision service did not start. It requires "
            "openadapt-flow with the 'console' extra (fastapi and uvicorn) and a "
            "qualified deployment configuration."
        )

    def stop(self) -> dict[str, Any]:
        """Stop the portal and the attended console it supervises."""
        with self._lock:
            if self._server is not None:
                self._server.stop()
            if self._console is not None:
                self._console.stop()
            self._server = None
            self._console = None
            self._flow = None
            self._pairings = None
            self._ingress = None
            return {"running": False, "ingress": self.describe_ingress()}

    def describe_ingress(self) -> dict[str, Any]:
        """Describe configured ingress without starting anything."""
        try:
            return resolve_ingress(self.config).describe() | {"configured": True}
        except IngressError as exc:
            return {
                "configured": False,
                "mode": str(getattr(self.config, "portal_ingress_mode", "loopback")),
                "loopback_only": True,
                "reachable_from_phone": False,
                "error": str(exc),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        if self._server is None or self._ingress is None:
            return {
                "running": False,
                "ingress": self.describe_ingress(),
                "devices": [],
                "error": self._last_error,
            }
        return {
            "running": True,
            "console_alive": self._console is not None and self._console.alive(),
            "ingress": self._ingress.describe(),
            "port": self._server.port,
            "devices": self._pairings.devices() if self._pairings else [],
            "error": None,
        }

    # ---------------------------------------------------------------- pairing

    def _require_pairings(self) -> DevicePairingStore:
        if self._pairings is None or self._ingress is None:
            raise PortalError("Start the decision portal before pairing a phone.")
        return self._pairings

    def create_pairing(self) -> dict[str, Any]:
        """Mint one QR pairing.  The secret rides in the link's fragment only."""
        with self._lock:
            pairings = self._require_pairings()
            ingress = self._ingress
            assert ingress is not None
            pairing = pairings.create(
                ingress.public_origin,
                reachable_from_phone=ingress.reachable_from_phone,
            )
        result = pairing.public()
        result["qr_svg"] = _qr_svg(pairing.url)
        if not ingress.reachable_from_phone:
            result["note"] = (
                "This portal is loopback-only, so a phone cannot reach this link. "
                "Configure your organization's HTTPS or VPN ingress to publish it."
            )
        return result

    def approve_pairing(self, pairing_id: str, confirm_code: Any) -> dict[str, Any]:
        with self._lock:
            return self._require_pairings().approve(pairing_id, confirm_code)

    def cancel_pairing(self, pairing_id: str) -> dict[str, Any]:
        with self._lock:
            return self._require_pairings().cancel(pairing_id)

    def pairing_status(self, pairing_id: str) -> dict[str, Any]:
        with self._lock:
            return self._require_pairings().pairing_status(pairing_id)

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._pairings.devices() if self._pairings else []

    def revoke_device(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._require_pairings().revoke(session_id)

    # ----------------------------------------------------------- notification

    def notification(self) -> dict[str, Any]:
        """Return the generic operating-system notification payload.

        Only an integer count crosses this boundary.  No upstream string is
        read, so no question, value, identifier, or application name can reach
        a lock screen.
        """
        flow = self._flow
        if flow is None:
            return assert_generic_notification(build_notification(0))
        try:
            response = flow.request("notification")
        except FlowConsoleUnavailable:
            return assert_generic_notification(build_notification(0))
        return assert_generic_notification(notification_from_upstream(response.json))


def _qr_svg(url: str) -> str | None:
    """Render the pairing link as an inert PNG data URI, if segno is present.

    A ``data:`` image is deliberate.  The QR encodes the one-use pairing
    secret, so returning raw SVG markup for the Desktop window to inject would
    make this a raw-HTML sink for a secret-bearing value.  A base64 PNG cannot
    carry script, needs no sanitizing, and renders identically.

    Rendering happens locally; the link is never sent anywhere to be encoded.
    When segno is unavailable the caller still has the URL, so pairing degrades
    to opening a link rather than failing.
    """
    try:
        import segno
    except ImportError:  # pragma: no cover - optional dependency
        return None
    try:
        import base64
        import io

        buffer = io.BytesIO()
        segno.make(url, error="m").save(buffer, kind="png", scale=6, border=2)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:  # pragma: no cover - defensive
        return None


__all__ = [
    "ConsoleProcess",
    "PairingRefused",
    "PortalError",
    "PortalService",
    "_parse_console_banner",
]
