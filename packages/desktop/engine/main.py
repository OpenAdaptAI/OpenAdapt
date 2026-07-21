"""Entry point for the OpenAdapt Desktop Python sidecar.

This module initializes the engine and starts either:
  - CLI mode: if stdin is a terminal (interactive use)
  - IPC mode: if stdin is piped (Tauri sidecar)

Usage (development CLI):
    uv run python -m engine list
    uv run python -m engine record

Usage (IPC sidecar):
    The sidecar is bundled as a standalone executable via PyInstaller
    and spawned by the Tauri shell on startup.
"""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path

from loguru import logger

from engine import __version__
from engine.config import EngineConfig

ENGINE_VERSION = __version__

# Private process modes used only by the frozen native executable.  Flow still
# runs out-of-process; using the same signed binary avoids a second sidecar path
# and guarantees that the installed cockpit invokes the version it shipped.
EMBEDDED_FLOW_MODE = "__openadapt_flow__"


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
    importing the provider makes Flow 1.19 think the capability is ready; the
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
    from openadapt_flow.__main__ import main as flow_main

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    flow_main()


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


def main() -> None:
    """Initialize the engine and start the appropriate mode."""
    _configure_frozen_browser_cache()
    if sys.argv[1:2] == [EMBEDDED_FLOW_MODE]:
        _run_embedded_flow()
        return
    if sys.argv[1:3] == ["-m", "playwright"]:
        _run_embedded_playwright()
        return

    # CLI mode: if run with subcommands or from a terminal
    if len(sys.argv) > 1 or sys.stdin.isatty():
        from engine.cli import main as cli_main

        cli_main()
        return

    # IPC mode: piped stdin from Tauri shell
    config = EngineConfig()

    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}",
    )

    logger.info("OpenAdapt Desktop Engine starting (v{version})", version=ENGINE_VERSION)
    logger.info("Storage mode: {mode}", mode=config.storage_mode)

    from engine.audit import AuditLogger
    from engine.db import IndexDB
    from engine.dispatch import EngineServices
    from engine.ipc import IPCHandler
    from engine.monitor import HealthMonitor
    from engine.socket_server import DesktopSocketServer
    from engine.storage_manager import StorageManager

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

    # The tray's loopback socket server + discovery file (spec 3d, P0-1).
    socket_server = DesktopSocketServer(config, services=services)
    try:
        socket_server.start()
    except OSError:
        logger.exception("Could not start desktop IPC socket server (tray inert)")

    handler = IPCHandler(config=config, services=services)

    # EXPERIMENTAL runner lane: resume the outbound dispatch loop only when the
    # operator explicitly enabled it (off by default; toggled on the Runner screen).
    if config.runner_enabled:
        try:
            handler.dispatcher.runner_status()  # builds the shared service
            services.runner.start()
        except Exception:
            logger.exception("Runner loop failed to start (lane stays off)")

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
        db.close()
        logger.info("Engine stopped")


if __name__ == "__main__":
    main()
