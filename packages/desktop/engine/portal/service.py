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

Deployment-config lifetime, decided on the pinned Flow's actual read ordering
-----------------------------------------------------------------------------

Flow refuses ``console --attend --allow-actions`` without a deployment target,
so the portal must hand it one.  That config is the operator's own
``data_dir/deployment.json``, whose schema carries **reusable credentials** --
``rdp_password``, ``rdp_username``, ``rdp_domain``, ``agent_token``,
``agent_tls_pin`` -- alongside PHI-capable selectors.  ``private_flow_config``
already treats every ``password``/``token``/``secret`` key as sensitive when it
builds log redactions.  So this file is *not* "a backend and a URL", and it is
not eligible to sit on disk for a whole portal session.

It is also pointless to re-stage it per run.  In the pinned OpenAdapt Flow runtime
(the exact pin this installer ships) ``__main__._attended_service_from_args``
resolves ``--config`` **eagerly**, through ``load_deployment``, before it
yields; ``AttendedActionService`` is constructed from the parsed
``DeploymentConfig`` object and never sees the path again.  Re-writing the file
later would not change one byte of what the console executes with -- it would
only put the same secret back on disk more times.

The exposure this can actually bound is therefore neither of those.  It is the
window in which a same-user backup, file-sync client, crash reporter, or
support bundle can sweep the staged copy.  So the file lives for exactly as
long as Flow needs to read it: it is staged, the console is spawned, and it is
removed the instant the capability banner proves the read already happened.
``serve()`` -- which prints that banner -- runs strictly downstream of the
config load inside the same ``with`` statement, so the banner is a
happens-after proof, not a timing guess.  Measured window: about five seconds.

``scripts/smoke_test_frozen_flow.py`` proves this against the frozen binary by
deleting the staged config after the banner and *then* driving every portal
route, so the ordering is an artifact-level fact rather than a code reading,
and a future Flow that started re-reading the path would fail that smoke.
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from packaging.version import InvalidVersion, Version

from engine.auth.store import load_runner_credential
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
from engine.private_flow_config import (
    PrivateFlowConfigError,
    prepare_flow_config,
    stage_private_yaml,
)

#: The exact banner ``openadapt_flow.console.server.serve`` prints.
_CONSOLE_BANNER = re.compile(
    r"^\s*http://(?:127\.0\.0\.1|localhost):(?P<port>\d{1,5})/#token="
    r"(?P<token>[A-Za-z0-9_-]{16,})\s*$"
)

#: How long to wait for the console to announce itself before failing loud.
CONSOLE_START_TIMEOUT_S = 60.0

#: The first Flow release whose attended console accepts ``--remote-decisions``.
#: Passing the flag to an older Flow makes argparse exit before the banner, so
#: the operator would see "the console did not start" instead of the real cause.
#: Checking the version first turns that into a sentence that names the fix.
MIN_FLOW_FOR_REMOTE_DECISIONS = (1, 26, 0)

#: Environment variable ``openadapt_flow.console.decision_relay`` reads the
#: runner credential from. It is passed to the child process only, never
#: written to a file and never logged.
RUNNER_TOKEN_ENV = "OPENADAPT_RUNNER_TOKEN"

#: How long uvicorn may take to bind *after* the banner is printed.  Flow
#: prints the capability immediately before ``uvicorn.run()``, so the first
#: request can legitimately arrive at a closed port.
CONSOLE_READY_TIMEOUT_S = 30.0

#: Interval between readiness probes while uvicorn finishes binding.
CONSOLE_READY_POLL_S = 0.25

#: A staged config older than this cannot belong to a console that is still
#: starting, so it is a leftover from a Desktop process that was killed before
#: its removal could run.  ``stage_private_yaml`` unlinks in a ``finally``,
#: which covers every ordinary and exceptional exit but cannot survive SIGKILL.
STALE_STAGING_AGE_S = CONSOLE_START_TIMEOUT_S * 2


class PortalError(RuntimeError):
    """The portal could not be started or is not in a state to serve."""


#: Whether this host needs a tree kill to stop the console (see _kill_tree).
_WINDOWS = sys.platform == "win32"


def _kill_tree(process: Any) -> None:
    """Stop the console *and every child it spawned*.

    The console runs inside the PyInstaller one-file sidecar, which executes the
    real application in a **child** of the process Desktop spawned. On Windows
    ``terminate()`` maps to ``TerminateProcess`` on that outer bootloader only,
    so the inner process keeps running -- an ``--allow-actions`` attended console
    still serving after the operator stopped the portal, still holding the
    extracted runtime's DLLs open. ``taskkill /T`` stops the whole tree.

    POSIX bootloaders ``exec`` into the application rather than forking it, so
    the single ``terminate()`` is already the whole tree there.
    """
    pid = getattr(process, "pid", None)
    if _WINDOWS and isinstance(pid, int):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    process.terminate()


def _terminate(process: Any) -> None:
    """Stop a console process without leaving a zombie or an orphan behind."""
    try:
        _kill_tree(process)
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


def _sweep_stale_stagings(directory: Path, *, now: float) -> None:
    """Remove staged configs a killed predecessor could not clean up.

    Only files older than :data:`STALE_STAGING_AGE_S` are removed.  A config
    belonging to a console that is still starting is younger than the start
    timeout by definition, so a concurrent start can never have its own file
    deleted out from under it.  Best-effort: a portal that cannot tidy an old
    file must still be able to start.
    """
    try:
        candidates = list(directory.glob(".deployment-*.yaml"))
    except OSError:  # pragma: no cover - unreadable staging directory
        return
    for candidate in candidates:
        try:
            if now - candidate.stat().st_mtime <= STALE_STAGING_AGE_S:
                continue
            candidate.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - raced with another sweep
            continue


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
                session = self._await_console_session(console, flow)
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

    def _deployment_source(self) -> Path:
        """The operator's deployment config, or a loud refusal.

        Flow refuses attended mutations that are not bound to a deployment
        target.  Failing here keeps that refusal on Desktop's side of the wire,
        where the message can name the file the operator has to write, instead
        of surfacing as an opaque "the console did not start".
        """
        source = Path(self.config.data_dir) / "deployment.json"
        if not source.is_file():
            raise PortalError(
                "The decision portal approves real actions, so it needs a "
                "deployment configuration naming the target to act on. Expected "
                f"it at {source}."
            )
        return source

    def _start_console(self) -> ConsoleProcess:
        prefix = _flow_command(FLOW_BIN)
        if prefix is None:
            raise PortalError(
                "OpenAdapt Flow is not available, so there is nothing to decide "
                "about. Install openadapt-flow with its console extra."
            )
        source = self._deployment_source()
        try:
            prepared = prepare_flow_config(source, None)
        except PrivateFlowConfigError as exc:
            raise PortalError(
                f"The deployment configuration could not be prepared: {exc}"
            ) from exc
        if prepared is None:  # pragma: no cover - a file source is never None
            raise PortalError("The deployment configuration resolved to nothing.")

        staging_dir = Path(self.config.data_dir) / "portal"
        staging_dir.mkdir(parents=True, exist_ok=True)
        _sweep_stale_stagings(staging_dir, now=time.time())
        # The staged secret-bearing config lives only until the banner proves
        # Flow has read it; leaving this ``with`` block removes the file.
        with stage_private_yaml(staging_dir, prepared=prepared) as config_path:
            return self._spawn_console(
                prefix,
                config_path,
                remote_decisions=prepared.remote_decisions,
                remote_decision_runner_id=prepared.remote_decision_runner_id,
            )

    def _remote_decision_env(
        self,
        *,
        host: str,
        expected_runner_id: str,
    ) -> dict[str, str]:
        """The child's environment with the runner credential, or a refusal.

        A deployment that enabled remote decisions and has no runner credential
        must stop here. Starting the console without the credential would give
        the operator a working local portal and a phone lane that is silently
        absent -- the failure that is worse than the gap, because nothing on
        either surface says the phone will never ring.
        """

        credential = load_runner_credential(host)
        credential_runner_id = str((credential or {}).get("runner_id") or "").strip()
        token = str((credential or {}).get("runner_token") or "").strip()
        if not token:
            raise PortalError(
                "This deployment answers halts on a phone through OpenAdapt "
                f"Cloud, but this computer is not registered with {host} yet. "
                "Connect it once, then start the portal again."
            )
        if credential_runner_id != expected_runner_id:
            raise PortalError(
                "The deployment configuration names a different runner than "
                "the credential registered for this control-plane host. Select "
                "the matching deployment or connect this computer again."
            )
        env = _subprocess_env()
        env[RUNNER_TOKEN_ENV] = token
        return env

    def _assert_flow_supports_remote_decisions(self) -> None:
        """Refuse before spawning when the resolved Flow has no such flag."""

        from importlib.metadata import PackageNotFoundError, version

        try:
            raw = version("openadapt-flow")
        except PackageNotFoundError:  # pragma: no cover - defensive
            raise PortalError(
                "The OpenAdapt Flow runtime version could not be read, so "
                "phone decisions cannot be enabled safely."
            ) from None
        wanted = ".".join(str(part) for part in MIN_FLOW_FOR_REMOTE_DECISIONS)
        try:
            installed = Version(raw)
            required = Version(wanted)
        except InvalidVersion:
            raise PortalError(
                "The OpenAdapt Flow runtime has an invalid version, so phone "
                "decisions cannot be enabled safely. Reinstall OpenAdapt Flow."
            ) from None
        if installed < required:
            raise PortalError(
                "This deployment answers halts on a phone, which needs "
                f"openadapt-flow {wanted} or newer. This computer has {raw}. "
                "Update OpenAdapt, or turn off human_decisions.remote."
            )

    def _spawn_console(
        self,
        prefix: list[str],
        config_path: Path,
        *,
        remote_decisions: bool = False,
        remote_decision_runner_id: str | None = None,
    ) -> ConsoleProcess:
        command = [
            *prefix,
            "console",
            "--attend",
            "--allow-actions",
            "--config",
            str(config_path),
            "--bundles",
            str(Path(self.config.data_dir) / "bundles"),
            "--runs",
            str(Path(self.config.data_dir) / "runs"),
            "--port",
            str(int(getattr(self.config, "portal_console_port", 7863))),
        ]
        env = _subprocess_env()
        if remote_decisions:
            # Both checks happen BEFORE the spawn. A console that starts and
            # then dies on an unknown flag reports "the console did not start",
            # which names neither the missing credential nor the old runtime.
            self._assert_flow_supports_remote_decisions()
            host = str(getattr(self.config, "hosted_host", "") or "").strip()
            if not host:
                raise PortalError("Remote decisions need an exact hosted control-plane URL.")
            if not remote_decision_runner_id:
                raise PortalError(
                    "Remote decisions need an exact runner_id in the deployment configuration."
                )
            env = self._remote_decision_env(
                host=host,
                expected_runner_id=remote_decision_runner_id,
            )
            command.extend(["--remote-decisions", "--remote-decision-host", host])
        process = self._popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
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
            "deployment configuration Flow accepts as a qualified target."
        )

    def _await_console_session(
        self, console: ConsoleProcess, flow: FlowConsoleClient
    ) -> Any:
        """Wait for uvicorn to bind, bounded by a real deadline.

        Flow prints the capability banner *before* ``uvicorn.run()`` binds, so
        the first request legitimately races the listener.  This is not a sleep:
        it returns the moment the console answers, gives up immediately if the
        console exited rather than waiting out the deadline, and re-raises the
        transport failure once the deadline passes.
        """
        # Deliberately the real monotonic clock, not ``self._clock``: that one is
        # the pairing store's logical clock, which tests move to expire pairings.
        # A network readiness deadline must not be coupled to it.
        deadline = time.monotonic() + CONSOLE_READY_TIMEOUT_S
        while True:
            if not console.alive():
                raise FlowConsoleUnavailable(
                    "The local decision service exited before it served a session"
                )
            try:
                return flow.request("session")
            except FlowConsoleUnavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(CONSOLE_READY_POLL_S)

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
