"""Run the BYOC connector as a Windows service (``OpenAdaptConnector``).

``openadapt-flow connector service install|start|stop|remove|status`` manages
a Windows service that runs the same daemon loop as ``openadapt-flow connector
run`` (:func:`openadapt_flow.connector.daemon.run_loop` with the governed
subprocess runner), in-process, with:

* settings from a machine-wide directory, by default
  ``%ProgramData%\\OpenAdapt\\connector`` (the service sets ``OPENADAPT_HOME``
  to it, so ``connector.toml`` is read from there);
* a rotating log at ``<config dir>\\logs\\connector.log``; the connector token
  is redacted from every record;
* the virtual account ``NT SERVICE\\OpenAdaptConnector``, delayed automatic
  start, and restart on failure;
* a config directory ACL that lets only SYSTEM, Administrators and the
  service account read ``connector.toml``.

The connector makes outbound connections only. Installing the service never
adds a firewall rule.

The service host is the interpreter's own ``python.exe`` running this module,
so ``sys.executable`` in the service is a real Python and governed runs start
the same way as under ``connector run``.

Everything above the pywin32 section imports on any OS; pywin32 (the
``connector-windows`` extra) is imported only on Windows.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

SERVICE_NAME = "OpenAdaptConnector"
SERVICE_DISPLAY_NAME = "OpenAdapt Connector"
SERVICE_DESCRIPTION = (
    "OpenAdapt BYOC connector: polls OpenAdapt Cloud over outbound HTTPS and "
    "runs governed jobs on this machine. Opens no inbound ports."
)
SERVICE_ACCOUNT = f"NT SERVICE\\{SERVICE_NAME}"
SERVICE_MODULE = "openadapt_flow.connector.windows_service"
HOST_FLAG = "--service-host"

LOG_FILE_NAME = "connector.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

#: Exit code for "this command needs Windows".
EXIT_NOT_WINDOWS = 2
NOT_WINDOWS_MESSAGE = (
    "connector service: Windows services are only available on Windows. "
    "On this OS, run `openadapt-flow connector run` under your service "
    "manager (systemd, launchd) instead."
)

#: Well-known SIDs, so the ACL does not depend on the OS display language.
_SID_SYSTEM = "*S-1-5-18"
_SID_ADMINISTRATORS = "*S-1-5-32-544"

#: Backoff after a failed poll cycle (seconds), capped.
_RETRY_START_S = 2.0
_RETRY_MAX_S = 60.0
#: Extra seconds the SCM should wait for a stop beyond one poll window.
_STOP_GRACE_S = 30


def is_windows() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def default_config_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """``%ProgramData%\\OpenAdapt\\connector`` (``C:\\ProgramData`` if unset)."""
    env = os.environ if environ is None else environ
    program_data = (
        env.get("ProgramData") or env.get("PROGRAMDATA") or ("C:\\ProgramData")
    )
    return Path(program_data) / "OpenAdapt" / "connector"


def resolve_config_dir(
    value: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> Path:
    return Path(value) if value else default_config_dir(environ)


def log_dir(config_dir: Path) -> Path:
    return config_dir / "logs"


def log_path(config_dir: Path) -> Path:
    return log_dir(config_dir) / LOG_FILE_NAME


def config_file(config_dir: Path) -> Path:
    return config_dir / "connector.toml"


# ---------------------------------------------------------------------------
# Logging (never the token)
# ---------------------------------------------------------------------------
class TokenRedactingFilter(logging.Filter):
    """Replace every occurrence of the given secrets in a formatted record."""

    def __init__(self, secrets: Sequence[Optional[str]]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def add(self, secret: Optional[str]) -> None:
        if secret and len(secret) >= 4 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        def scrub(value: str) -> str:
            for secret in self._secrets:
                value = value.replace(secret, "<redacted>")
            return value

        record.msg = scrub(record.getMessage())
        record.args = None
        # Tracebacks are formatted after filters normally run. Redact their
        # complete rendered text as well, without exposing exception arguments.
        if record.exc_info:
            record.exc_text = scrub(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def build_logger(
    config_dir: Path, *, name: str = "openadapt.connector.service"
) -> tuple[logging.Logger, TokenRedactingFilter]:
    """A logger writing to ``<config dir>/logs/connector.log`` with rotation."""
    log_dir(config_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(
        log_path(config_dir),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    redactor = TokenRedactingFilter([])
    handler.addFilter(redactor)
    logger.addHandler(handler)
    return logger, redactor


# ---------------------------------------------------------------------------
# The service body (OS-independent, so it is unit tested everywhere)
# ---------------------------------------------------------------------------
def _stoppable_client(
    control_plane_url: str, token: str, stop_event: threading.Event, **kwargs: Any
) -> Any:
    """A ConnectorClient whose poll raises KeyboardInterrupt once stop is set.

    ``run_loop`` already exits cleanly on KeyboardInterrupt, so this stops the
    unmodified loop between jobs: an in-flight job finishes, reports and acks
    before the service stops.
    """
    from openadapt_flow.connector.client import ConnectorClient

    class _StoppableClient(ConnectorClient):
        def poll(self, wait_s: int, **kwargs: Any) -> Optional[dict[str, Any]]:
            if stop_event.is_set():
                raise KeyboardInterrupt
            envelope = super().poll(wait_s, **kwargs)
            if stop_event.is_set():
                # A lease received while stopping must never start new work.
                # Leave it unacknowledged for control-plane reconciliation;
                # pretending the job completed would permit an unsafe retry.
                raise KeyboardInterrupt
            return envelope

    return _StoppableClient(control_plane_url, token=token, **kwargs)


def serve(
    config_dir: Path,
    stop_event: threading.Event,
    logger: logging.Logger,
    *,
    redactor: Optional[TokenRedactingFilter] = None,
    runner: Optional[Callable[..., Any]] = None,
    client_kwargs: Optional[dict[str, Any]] = None,
) -> int:
    """Run the connector loop until ``stop_event`` is set. Returns an exit code.

    A transport error or an unparseable job ends one ``run_loop`` call; the
    service logs the error type and polls again after a capped backoff rather
    than exiting.
    """
    os.environ["OPENADAPT_HOME"] = str(config_dir)

    from openadapt_flow.connector.config import ConnectorConfigError, load_settings
    from openadapt_flow.connector.daemon import run_loop

    if runner is None:
        from openadapt_flow.connector.executor import _subprocess_runner

        runner = _subprocess_runner

    try:
        settings = load_settings()
    except ConnectorConfigError as exc:
        logger.error("connector config error: %s", type(exc).__name__)
        return 2
    if redactor is not None:
        redactor.add(settings.token)
    if not settings.token:
        logger.error(
            'no connector token: put token = "..." in %s or set '
            "BYOC_CONNECTOR_TOKEN for the service",
            config_file(config_dir),
        )
        return 2
    logger.info(
        "service starting: config %s, settings %s",
        config_file(config_dir),
        settings.redacted(),
    )

    delay = _RETRY_START_S
    while not stop_event.is_set():
        started = time.monotonic()
        client = _stoppable_client(
            settings.control_plane_url,
            settings.token,
            stop_event,
            **(client_kwargs or {}),
        )
        try:
            code = run_loop(client, settings, runner=runner, log=logger.info)
            if stop_event.is_set():
                break
            if code != 0:
                logger.error("connector loop exited with code %s", code)
                return code
        except Exception as exc:  # noqa: BLE001 - keep polling; never crash out
            if stop_event.is_set():
                break
            if time.monotonic() - started > _RETRY_MAX_S:
                delay = _RETRY_START_S
            logger.warning(
                "connector cycle failed (%s: %s); retrying in %.0f s",
                type(exc).__name__,
                exc,
                delay,
            )
            stop_event.wait(delay)
            delay = min(delay * 2, _RETRY_MAX_S)
        finally:
            client.close()
    logger.info("service stopped")
    return 0


# ---------------------------------------------------------------------------
# Install-time helpers (subprocess commands built here; run only on Windows)
# ---------------------------------------------------------------------------
VENV_MESSAGE = (
    "connector service install: this Python is a virtual environment. A "
    "venv's python.exe is a launcher that runs the real interpreter as a "
    "child process, which the service control manager cannot talk to. "
    "Install openadapt-flow[connector-windows] into a regular Python "
    "installation (for example the python.org installer, for all users) and "
    "run install from there."
)


def service_python(
    executable: Optional[str] = None,
    *,
    prefix: Optional[str] = None,
    base_prefix: Optional[str] = None,
) -> str:
    """The python.exe that hosts the service: this interpreter, never a venv.

    Raises:
        RuntimeError: when this interpreter runs inside a virtual environment.
    """
    if (prefix or sys.prefix) != (base_prefix or sys.base_prefix):
        raise RuntimeError(VENV_MESSAGE)
    return executable or sys.executable


def service_command_line(python: str, config_dir: Path) -> str:
    return f'"{python}" -m {SERVICE_MODULE} {HOST_FLAG} --config-dir "{config_dir}"'


def protect_config_directory(config_dir: Path) -> None:  # pragma: no cover - Windows CI
    """Replace the DACL, including any pre-existing explicit broad grants.

    ``icacls /grant:r`` replaces entries only for the named principals; it
    leaves other explicit grants behind. Build the complete protected DACL
    before attaching it, so no permissive reset is exposed in between.
    """
    import ntsecuritycon
    import win32con
    import win32security

    acl = win32security.ACL()
    inherited = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    service_sid = win32security.LookupAccountName(None, SERVICE_ACCOUNT)[0]
    entries = (
        (
            win32security.ConvertStringSidToSid(_SID_SYSTEM[1:]),
            ntsecuritycon.FILE_ALL_ACCESS,
        ),
        (
            win32security.ConvertStringSidToSid(_SID_ADMINISTRATORS[1:]),
            ntsecuritycon.FILE_ALL_ACCESS,
        ),
        (
            service_sid,
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
        ),
    )
    for sid, access in entries:
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherited, access, sid)
    win32security.SetNamedSecurityInfo(
        str(config_dir),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


def acl_commands(config_dir: Path) -> list[list[str]]:
    """Make existing children inherit the exact directory ACL, then allow logs.

    The parent DACL has already been replaced by protect_config_directory.
    The service account gets read on connector.toml and modify on logs only.
    """
    return [
        ["icacls", str(config_dir / "*"), "/reset", "/T", "/C", "/Q"],
        ["icacls", str(log_dir(config_dir)), "/grant", f"{SERVICE_ACCOUNT}:(OI)(CI)M"],
    ]


def recovery_commands() -> list[list[str]]:
    """Restart on failure: 5 s, 5 s, then 60 s; reset the count after a day."""
    return [
        [
            "sc.exe",
            "failure",
            SERVICE_NAME,
            "reset=",
            "86400",
            "actions=",
            "restart/5000/restart/5000/restart/60000",
        ],
        # Also run the actions when the service stops itself with an error.
        ["sc.exe", "failureflag", SERVICE_NAME, "1"],
    ]


def _run(command: list[str]) -> None:
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{command[0]} {command[1]} failed ({proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()[:500]}"
        )


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# pywin32 service and service control (Windows only)
# ---------------------------------------------------------------------------
if is_windows():  # pragma: no cover - exercised by the Windows CI workflow
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError:  # the connector-windows extra is not installed
        win32serviceutil = None  # type: ignore[assignment]
else:
    win32serviceutil = None  # type: ignore[assignment]


if win32serviceutil is not None:  # pragma: no cover - Windows only

    class ConnectorService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        #: Set by the service host from ``--config-dir`` before dispatch.
        config_dir: Path = default_config_dir()

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self.stop_event = threading.Event()

        def SvcStop(self) -> None:  # noqa: N802 - pywin32 API
            wait_hint = (_poll_wait_hint(self.config_dir) + _STOP_GRACE_S) * 1000
            self.ReportServiceStatus(
                win32service.SERVICE_STOP_PENDING, waitHint=wait_hint
            )
            self.stop_event.set()

        def SvcDoRun(self) -> None:  # noqa: N802 - pywin32 API
            logger, redactor = build_logger(self.config_dir)
            try:
                code = serve(
                    self.config_dir, self.stop_event, logger, redactor=redactor
                )
            except Exception:
                logger.exception("service crashed")
                raise
            finally:
                for handler in logger.handlers:
                    handler.flush()
            if code != 0 and not self.stop_event.is_set():
                # Raising reports a non-zero exit, which triggers recovery.
                raise RuntimeError(f"connector exited with code {code}")

else:
    ConnectorService = None  # type: ignore[assignment,misc]


def _poll_wait_hint(config_dir: Path) -> int:
    try:
        os.environ["OPENADAPT_HOME"] = str(config_dir)
        from openadapt_flow.connector.config import load_settings

        return int(load_settings().poll_wait_s)
    except Exception:  # noqa: BLE001
        return 25


def _require_pywin32() -> None:
    if win32serviceutil is None:
        raise RuntimeError(
            "pywin32 is not installed; run: pip install "
            "'openadapt-flow[connector-windows]'"
        )


def install_service(config_dir: Path) -> None:  # pragma: no cover - Windows
    _require_pywin32()
    python = service_python()
    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir(config_dir).mkdir(parents=True, exist_ok=True)

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.CreateService(
            scm,
            SERVICE_NAME,
            SERVICE_DISPLAY_NAME,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            service_command_line(python, config_dir),
            None,
            0,
            None,
            SERVICE_ACCOUNT,
            None,
        )
        try:
            win32service.ChangeServiceConfig2(
                handle, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESCRIPTION
            )
            win32service.ChangeServiceConfig2(
                handle, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, True
            )
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)

    for command in recovery_commands():
        _run(command)
    # The service SID exists now. Replace the whole parent DACL rather than
    # leaving any explicit Users/Everyone entries from an earlier install.
    protect_config_directory(config_dir)
    for command in acl_commands(config_dir):
        _run(command)


def _wait_for(state: int, timeout_s: float) -> bool:  # pragma: no cover - Windows
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1] == state:
            return True
        time.sleep(0.5)
    return False


def _state_name(state: int) -> str:  # pragma: no cover - Windows
    names = {
        win32service.SERVICE_STOPPED: "STOPPED",
        win32service.SERVICE_START_PENDING: "START_PENDING",
        win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
        win32service.SERVICE_RUNNING: "RUNNING",
        win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",
        win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
        win32service.SERVICE_PAUSED: "PAUSED",
    }
    return names.get(state, str(state))


def _service_pid() -> int:  # pragma: no cover - Windows
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(
            scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS
        )
        try:
            status = win32service.QueryServiceStatusEx(handle)
            return int(status.get("ProcessId") or 0)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)


def _act(action: str, config_dir: Path, timeout_s: float) -> int:  # pragma: no cover
    _require_pywin32()
    if action == "install":
        install_service(config_dir)
        print(
            f"installed {SERVICE_NAME} as {SERVICE_ACCOUNT} (delayed auto start, "
            f"restart on failure); config {config_file(config_dir)}, logs "
            f"{log_path(config_dir)}"
        )
        return 0
    if action == "start":
        win32serviceutil.StartService(SERVICE_NAME)
        if not _wait_for(win32service.SERVICE_RUNNING, timeout_s):
            print(f"{SERVICE_NAME} did not reach RUNNING; see {log_path(config_dir)}")
            return 1
        print(f"{SERVICE_NAME} RUNNING (pid {_service_pid()})")
        return 0
    if action == "stop":
        state = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        if state != win32service.SERVICE_STOPPED:
            win32serviceutil.StopService(SERVICE_NAME)
            if not _wait_for(win32service.SERVICE_STOPPED, timeout_s):
                print(f"{SERVICE_NAME} did not stop within {timeout_s:.0f} s")
                return 1
        print(f"{SERVICE_NAME} STOPPED")
        return 0
    if action == "remove":
        try:
            state = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        except Exception:  # noqa: BLE001
            print(f"{SERVICE_NAME} is not installed")
            return 1
        if state != win32service.SERVICE_STOPPED:
            win32serviceutil.StopService(SERVICE_NAME)
            if not _wait_for(win32service.SERVICE_STOPPED, timeout_s):
                print(f"{SERVICE_NAME} did not stop; the service was not removed")
                return 1
        win32serviceutil.RemoveService(SERVICE_NAME)
        print(
            f"removed {SERVICE_NAME}; {config_dir} (with the token) was kept, "
            "delete it to remove the enrollment"
        )
        return 0
    # status
    try:
        state = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
    except Exception:  # noqa: BLE001
        print(f"{SERVICE_NAME}: not installed")
        return 1
    pid = _service_pid()
    suffix = f" (pid {pid})" if pid else ""
    print(f"{SERVICE_NAME}: {_state_name(state)}{suffix}; logs {log_path(config_dir)}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
SERVICE_ACTIONS = ("install", "start", "stop", "remove", "status")


def register_service_parser(connector_subparsers: Any) -> None:
    """Add ``connector service <action>`` to the connector subcommands."""
    p = connector_subparsers.add_parser(
        "service",
        help="Install and control the connector as a Windows service",
        allow_abbrev=False,
    )
    p.add_argument("service_action", choices=SERVICE_ACTIONS)
    p.add_argument(
        "--config-dir",
        default=None,
        help="Machine-wide config dir (default: %%ProgramData%%\\OpenAdapt\\connector)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for start/stop (default: 90)",
    )
    p.set_defaults(func=cmd_connector_service)


def cmd_connector_service(args: argparse.Namespace) -> int:
    if not is_windows():
        print(NOT_WINDOWS_MESSAGE, file=sys.stderr)
        return EXIT_NOT_WINDOWS
    action = args.service_action
    config_dir = resolve_config_dir(getattr(args, "config_dir", None))
    if action in ("install", "start", "stop", "remove") and not _is_admin():
        print(
            f"connector service {action}: run this from an elevated "
            "(Administrator) prompt",
            file=sys.stderr,
        )
        return 1
    try:
        return _act(action, config_dir, float(getattr(args, "timeout", 90.0)))
    except RuntimeError as exc:
        print(f"connector service {action}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - pywin32 raises pywintypes.error
        print(f"connector service {action}: {exc}", file=sys.stderr)
        return 1


def _host(argv: Sequence[str]) -> int:  # pragma: no cover - Windows service host
    """Entry point the service control manager starts."""
    parser = argparse.ArgumentParser(prog=SERVICE_MODULE, allow_abbrev=False)
    parser.add_argument(HOST_FLAG, action="store_true", required=True)
    parser.add_argument("--config-dir", default=None)
    args = parser.parse_args(list(argv))
    _require_pywin32()
    # A service has no console; keep stray prints from raising.
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = devnull
    sys.stderr = devnull
    ConnectorService.config_dir = resolve_config_dir(args.config_dir)
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ConnectorService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_host(sys.argv[1:]))
