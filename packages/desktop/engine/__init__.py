"""OpenAdapt Desktop Engine -- Python sidecar for the Tauri shell.

This package implements the recording engine, PII scrubbing, storage management,
upload backends, and health monitoring. It runs as a standalone process that
communicates with the Tauri shell via JSON-over-stdin/stdout IPC.

Architecture:
    Tauri Shell (Rust + WebView)
        |  IPC (JSON over stdin/stdout)
        v
    Python Engine (this package)
        +-- controller.py      Recording start/stop/pause
        +-- ipc.py             JSON line protocol handler
        +-- storage_manager.py Storage tiers, cleanup, index
        +-- upload_manager.py  Multi-backend upload with queue
        +-- scrubber.py        PII scrubbing orchestration
        +-- review.py          Upload review state machine
        +-- config.py          Settings (pydantic-settings)
        +-- monitor.py         Health monitoring (memory, disk, watchdog)
        +-- audit.py           Network audit logging
        +-- backends/          Storage backend plugins
"""

__version__ = "0.1.0"
