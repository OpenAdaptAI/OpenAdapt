"""Entry point for the OpenAdapt Desktop Python sidecar.

This module initializes the engine and starts either:
  - CLI mode: if stdin is a terminal (interactive use)
  - IPC mode: if stdin is piped (Tauri sidecar)

Usage (development CLI):
    uv run python -m openadapt_desktop list
    uv run python -m openadapt_desktop record

Usage (IPC sidecar):
    The sidecar is bundled as a standalone executable via PyInstaller
    and spawned by the Tauri shell on startup.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path

from loguru import logger

from openadapt_desktop import __version__
from openadapt_desktop.config import EngineConfig

ENGINE_VERSION = __version__

# Private process modes used only by the frozen native executable.  Flow still
# runs out-of-process; using the same signed binary avoids a second sidecar path
# and guarantees that the installed cockpit invokes the version it shipped.
EMBEDDED_FLOW_MODE = "__openadapt_flow__"
EMBEDDED_FLOW_RECORD_MODE = "__openadapt_flow_record_config__"


def _embedded_flow_version() -> str:
    """Return the exact Flow distribution version copied into the freeze."""

    try:
        return version("openadapt-flow")
    except PackageNotFoundError as exc:
        raise RuntimeError("The bundled OpenAdapt Flow runtime metadata is missing.") from exc


def _print_embedded_flow_help() -> None:
    """Keep internal Flow discovery offline before optional vision provision."""

    print(
        f"""openadapt-flow {_embedded_flow_version()}

Bundled OpenAdapt Flow runtime.

Usage: openadapt-flow COMMAND [OPTIONS]

Core commands:
  record       Record a demonstrated workflow
  compile      Compile a recording into a deterministic bundle
  replay       Replay a bundle locally
  run          Run a bundle under a deployment configuration
  teach        Teach a governed repair after a halt
  approve      Approve a durably paused escalation
  resume       Resume from the last verified checkpoint
  lint         Inspect bundle coverage
  certify      Enforce a bundle safety policy
  qualify      Draft and confirm qualification pins from a demo
  console      Open the local operator console

Run a command with --help for its complete options."""
    )


def _configure_frozen_browser_cache() -> None:
    """Keep Playwright downloads outside PyInstaller's ephemeral extraction.

    A one-file executable expands into a temporary ``_MEI*`` directory on each
    process start.  Playwright otherwise treats that directory as its bundled
    package root, downloads Chromium there, and loses it when the installer
    subprocess exits.  This stable per-user location is uninstall-neutral user
    data and remains overrideable for an offline enterprise prebundle.
    """

    if getattr(sys, "frozen", False):
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            str(Path.home() / ".openadapt" / "browser-runtime"),
        )


def _normalize_flow_auto_scrub_capability() -> None:
    """Restore Flow's documented ``auto`` semantics in the combined freeze.

    Desktop includes ``openadapt-privacy`` for its local review pipeline, but
    its heavyweight Presidio/spaCy extra is intentionally optional.  Merely
    importing the provider makes Flow think the capability is ready; the
    first scrub then crashes.  In ``auto`` mode only, treat an incomplete extra
    exactly like an absent extra (local plaintext, as Flow documents).  Explicit
    ``SCRUB=on`` is never changed and therefore still fails closed.
    """

    mode = os.environ.get("OPENADAPT_FLOW_SCRUB", "auto").strip().lower()
    if mode not in {"", "auto"}:
        return
    required = ("presidio_analyzer", "presidio_anonymizer", "spacy")
    if any(find_spec(module) is None for module in required):
        os.environ["OPENADAPT_FLOW_SCRUB"] = "off"


def _run_embedded_flow() -> None:
    """Run the bundled Flow CLI in this process image."""

    _configure_frozen_browser_cache()
    _normalize_flow_auto_scrub_capability()
    flow_args = sys.argv[2:]
    if not flow_args or flow_args[0] in {"-h", "--help"}:
        _print_embedded_flow_help()
        return
    if flow_args[0] in {"-V", "--version"}:
        print(f"openadapt-flow {_embedded_flow_version()}")
        return
    if getattr(sys, "frozen", False):
        from openadapt_desktop.managed_vision import ensure_managed_vision_runtime

        ensure_managed_vision_runtime()
    from openadapt_flow.__main__ import main as flow_main

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    flow_main()


def _run_embedded_flow_record() -> None:
    """Run canonical Flow record from one private staged request."""

    _configure_frozen_browser_cache()
    _normalize_flow_auto_scrub_capability()
    if getattr(sys, "frozen", False):
        from openadapt_desktop.managed_vision import ensure_managed_vision_runtime

        ensure_managed_vision_runtime()
    from openadapt_desktop.flow_record_entry import main as record_main

    raise SystemExit(record_main(sys.argv[2:]))


def _run_embedded_playwright() -> None:
    """Support Flow's one-time ``python -m playwright install`` in a freeze.

    PyInstaller replaces ``sys.executable`` with this executable, so Flow's
    normal first-use browser provisioner cannot invoke ``-m playwright``
    directly.  This process mode preserves that upstream contract without a
    system Python installation.
    """

    _configure_frozen_browser_cache()
    from playwright.__main__ import main as playwright_main

    # Match ``python -m playwright <args>``: strip both ``-m`` and the module.
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    playwright_main()


ENGINE_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}"


def configure_engine_logging(level: str) -> None:
    """Send engine logs to stderr, without variable values in tracebacks.

    loguru's ``diagnose`` mode prints the value of every variable on each
    traceback line. A failure while handling a recording or shadow-mode event
    would then copy typed text, labels, or URLs into the log, so it is off.
    """

    logger.remove()
    logger.add(sys.stderr, level=level, format=ENGINE_LOG_FORMAT, diagnose=False)


def main() -> None:
    """Initialize the engine and start the appropriate mode."""
    from openadapt_desktop.private_fs import apply_private_umask

    # Before anything writes to disk, in every process mode (the embedded
    # Flow modes included): local data stays readable only by this account.
    apply_private_umask()
    _configure_frozen_browser_cache()
    if sys.argv[1:2] == [EMBEDDED_FLOW_MODE]:
        _run_embedded_flow()
        return
    if sys.argv[1:2] == [EMBEDDED_FLOW_RECORD_MODE]:
        _run_embedded_flow_record()
        return
    if sys.argv[1:3] == ["-m", "playwright"]:
        _run_embedded_playwright()
        return

    # CLI mode: if run with subcommands or from a terminal
    if len(sys.argv) > 1 or sys.stdin.isatty():
        from openadapt_desktop.cli import main as cli_main

        cli_main()
        return

    # IPC mode: piped stdin from Tauri shell
    config = EngineConfig()

    configure_engine_logging(config.log_level)

    logger.info("OpenAdapt Desktop Engine starting (v{version})", version=ENGINE_VERSION)
    logger.info("Storage mode: {mode}", mode=config.storage_mode)

    from openadapt_desktop.audit import AuditLogger
    from openadapt_desktop.db import IndexDB
    from openadapt_desktop.dispatch import EngineServices
    from openadapt_desktop.ipc import IPCHandler
    from openadapt_desktop.monitor import HealthMonitor
    from openadapt_desktop.socket_server import DesktopSocketServer
    from openadapt_desktop.storage_manager import StorageManager

    audit = AuditLogger(config.audit_log_path, enabled=config.network_audit_log)
    db = IndexDB(config.data_dir / "index.db")
    db.initialize()

    storage = StorageManager(config)
    storage.initialize()
    storage._db = db

    monitor = HealthMonitor(config)
    monitor.start()

    audit.log_startup(storage_mode=config.storage_mode, active_backends=[])

    # One shared services container so BOTH local wires (the Tauri stdin/stdout
    # sidecar and the tray loopback socket) see the same recording/DB state.
    services = EngineServices(config, db=db, storage=storage, audit=audit)

    handler = IPCHandler(config=config, services=services)

    # Both local wires use one dispatcher. The socket server keeps the Tauri
    # event sink and adds its filtered tray broadcast sink.
    socket_server = DesktopSocketServer(
        config,
        dispatcher=handler.dispatcher,
        dispatch_lock=handler.dispatch_lock,
    )
    try:
        socket_server.start()
    except OSError:
        logger.exception("Could not start desktop IPC socket server (tray inert)")

    # Resume the hosted runner only when the operator explicitly enabled
    # it (off by default; toggled on the Runner screen).
    if config.runner_enabled:
        try:
            handler.dispatcher.runner_enable()
        except Exception:
            logger.exception("Runner loop failed to start (lane stays off)")

    # Start shadow mode now, not on the window's first status request, so a
    # buffer left by a crash is erased at once and a standing opt-in resumes.
    try:
        _ = handler.dispatcher.shadow
    except Exception as exc:
        logger.warning("Shadow mode didn't start: {}", type(exc).__name__)

    try:
        handler.run()
    except KeyboardInterrupt:
        logger.info("Engine shutting down (keyboard interrupt)")
    except Exception:
        logger.exception("Engine crashed")
        sys.exit(1)
    finally:
        if services.runner is not None:
            services.runner.stop()
        socket_server.stop()
        monitor.stop()
        try:
            handler.dispatcher.close_shadow()
        except Exception as exc:
            logger.warning("Shadow mode didn't close cleanly: {}", type(exc).__name__)
        db.close()
        logger.info("Engine stopped")


if __name__ == "__main__":
    main()
