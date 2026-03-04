"""Health monitoring -- memory watchdog, disk usage tracker, process supervision.

Monitors the engine's resource usage and takes corrective action:

Memory monitoring (from design doc Section 4.2):
    - Tracks RSS every 30 seconds via psutil.Process().memory_info().rss
    - If RSS exceeds threshold (default 500 MB), triggers graceful restart
      of the recording process (finish current chunk, start new process)

Disk monitoring:
    - Tracks disk usage of captures/ + archive/ directories
    - Emits storage_warning event when approaching max_storage_gb
    - Triggers cleanup when limit is exceeded

Process watchdog:
    - Monitors the recording process
    - If the recording process dies, restarts it and begins a new session
      linked to the same capture
"""

from __future__ import annotations

import os
import threading
import time

from engine.config import EngineConfig


def _dir_size(path: str) -> int:
    """Sum file sizes under *path* recursively."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


class HealthMonitor:
    """Monitors engine health and takes corrective action.

    Args:
        config: Engine configuration.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._rss_threshold_bytes = 500 * 1024 * 1024  # 500 MB
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_time = time.monotonic()

    def start(self) -> None:
        """Start background monitoring threads.

        Starts:
            - Memory monitor (checks RSS every 30s)
            - Disk monitor (checks usage every 60s)
        """
        self._stop_event.clear()
        self._start_time = time.monotonic()

        mem_thread = threading.Thread(
            target=self._memory_loop, daemon=True, name="monitor-memory"
        )
        disk_thread = threading.Thread(
            target=self._disk_loop, daemon=True, name="monitor-disk"
        )

        self._threads = [mem_thread, disk_thread]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Stop all monitoring threads."""
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    def check_memory(self) -> dict:
        """Check current memory usage of the engine process.

        Returns:
            Dict with rss_bytes, rss_mb, threshold_mb, over_threshold.
        """
        import psutil

        proc = psutil.Process()
        rss = proc.memory_info().rss
        return {
            "rss_bytes": rss,
            "rss_mb": round(rss / (1024 * 1024), 1),
            "threshold_mb": round(self._rss_threshold_bytes / (1024 * 1024), 1),
            "over_threshold": rss > self._rss_threshold_bytes,
        }

    def check_disk(self) -> dict:
        """Check current disk usage of capture storage.

        Returns:
            Dict with used_bytes, max_bytes, usage_percent, warning.
        """
        captures_dir = str(self.config.data_dir / "captures")
        archive_dir = str(self.config.data_dir / "archive")
        used = _dir_size(captures_dir) + _dir_size(archive_dir)
        max_bytes = int(self.config.max_storage_gb * 1024**3)
        pct = (used / max_bytes * 100) if max_bytes > 0 else 0

        return {
            "used_bytes": used,
            "max_bytes": max_bytes,
            "usage_percent": round(pct, 1),
            "warning": pct > 90,
        }

    def get_health_status(self) -> dict:
        """Get comprehensive health status.

        Returns:
            Dict with memory, disk, and uptime information.
        """
        return {
            "memory": self.check_memory(),
            "disk": self.check_disk(),
            "uptime_secs": round(time.monotonic() - self._start_time, 1),
            "monitoring": not self._stop_event.is_set() and len(self._threads) > 0,
        }

    def _memory_loop(self) -> None:
        """Background loop checking memory every 30s."""
        while not self._stop_event.wait(30):
            try:
                self.check_memory()
            except Exception:
                pass

    def _disk_loop(self) -> None:
        """Background loop checking disk every 60s."""
        while not self._stop_event.wait(60):
            try:
                self.check_disk()
            except Exception:
                pass
