"""Entry point for the OpenAdapt Desktop Python sidecar.

This module initializes the engine and starts the IPC message loop.
It is invoked by the Tauri shell as a sidecar process.

Usage (development):
    uv run python -m engine

Usage (production):
    The sidecar is bundled as a standalone executable via PyInstaller
    and spawned by the Tauri shell on startup.
"""

from __future__ import annotations

import sys

from loguru import logger

from engine.config import EngineConfig
from engine.ipc import IPCHandler


def main() -> None:
    """Initialize the engine and start the IPC message loop."""
    config = EngineConfig()

    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}",
    )

    logger.info("OpenAdapt Desktop Engine starting (v{version})", version="0.1.0")
    logger.info("Storage mode: {mode}", mode=config.storage_mode)

    # TODO: Initialize subsystems
    # - StorageManager (index.db, cleanup scheduler)
    # - UploadManager (backend registry, upload queue)
    # - Monitor (memory watchdog, disk usage tracker)
    # - AuditLogger (append-only JSONL)

    handler = IPCHandler(config=config)

    try:
        handler.run()
    except KeyboardInterrupt:
        logger.info("Engine shutting down (keyboard interrupt)")
    except Exception:
        logger.exception("Engine crashed")
        sys.exit(1)
    finally:
        # TODO: Graceful shutdown
        # - Stop active recording
        # - Flush upload queue
        # - Close databases
        logger.info("Engine stopped")


if __name__ == "__main__":
    main()
