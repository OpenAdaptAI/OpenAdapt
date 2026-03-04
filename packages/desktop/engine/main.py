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

import sys

from loguru import logger

from engine.config import EngineConfig


def main() -> None:
    """Initialize the engine and start the appropriate mode."""
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

    logger.info("OpenAdapt Desktop Engine starting (v{version})", version="0.1.0")
    logger.info("Storage mode: {mode}", mode=config.storage_mode)

    from engine.audit import AuditLogger
    from engine.db import IndexDB
    from engine.ipc import IPCHandler
    from engine.monitor import HealthMonitor
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

    handler = IPCHandler(config=config)

    try:
        handler.run()
    except KeyboardInterrupt:
        logger.info("Engine shutting down (keyboard interrupt)")
    except Exception:
        logger.exception("Engine crashed")
        sys.exit(1)
    finally:
        monitor.stop()
        db.close()
        logger.info("Engine stopped")


if __name__ == "__main__":
    main()
