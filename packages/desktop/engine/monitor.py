"""Health monitoring -- memory watchdog, disk usage tracker, process supervision.

Monitors the engine's resource usage and takes corrective action:

Memory monitoring (from design doc Section 4.2):
    - Tracks RSS every 30 seconds via psutil.Process().memory_info().rss
    - If RSS exceeds threshold (default 500 MB), triggers graceful restart
      of the recording process (finish current chunk, start new process)
    - Uses pympler.tracker.SummaryTracker in debug mode to detect leak sources

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

from engine.config import EngineConfig


class HealthMonitor:
    """Monitors engine health and takes corrective action.

    Args:
        config: Engine configuration.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._rss_threshold_bytes = 500 * 1024 * 1024  # 500 MB

    def start(self) -> None:
        """Start background monitoring threads.

        Starts:
            - Memory monitor (checks RSS every 30s)
            - Disk monitor (checks usage every 60s)
            - Process watchdog (monitors recording subprocess)
        """
        # TODO: Start memory monitoring thread
        # TODO: Start disk usage monitoring thread
        # TODO: Start process watchdog thread
        raise NotImplementedError

    def stop(self) -> None:
        """Stop all monitoring threads."""
        # TODO: Signal threads to stop and join them
        raise NotImplementedError

    def check_memory(self) -> dict:
        """Check current memory usage of the engine process.

        Returns:
            Dict with rss_bytes, rss_mb, threshold_mb, over_threshold.
        """
        # TODO: Use psutil.Process().memory_info().rss
        raise NotImplementedError

    def check_disk(self) -> dict:
        """Check current disk usage of capture storage.

        Returns:
            Dict with used_bytes, max_bytes, usage_percent, warning.
        """
        # TODO: Calculate size of captures/ + archive/
        raise NotImplementedError

    def get_health_status(self) -> dict:
        """Get comprehensive health status.

        Returns:
            Dict with memory, disk, recording_process, uptime information.
        """
        # TODO: Aggregate memory, disk, and process status
        raise NotImplementedError
