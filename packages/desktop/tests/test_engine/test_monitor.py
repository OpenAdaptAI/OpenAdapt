"""Tests for the health monitor."""

from __future__ import annotations

import time

from engine.config import EngineConfig
from engine.monitor import HealthMonitor


class TestHealthMonitor:
    """Tests for HealthMonitor operations."""

    def test_check_memory_returns_dict(self, config: EngineConfig) -> None:
        """check_memory should return a dict with required keys."""
        monitor = HealthMonitor(config)
        mem = monitor.check_memory()
        assert "rss_bytes" in mem
        assert "rss_mb" in mem
        assert "threshold_mb" in mem
        assert "over_threshold" in mem
        assert mem["rss_bytes"] > 0

    def test_check_disk_returns_dict(self, config: EngineConfig) -> None:
        """check_disk should return a dict with required keys."""
        monitor = HealthMonitor(config)
        disk = monitor.check_disk()
        assert "used_bytes" in disk
        assert "max_bytes" in disk
        assert "usage_percent" in disk
        assert "warning" in disk

    def test_start_stop_threads(self, config: EngineConfig) -> None:
        """Threads should start and stop cleanly."""
        monitor = HealthMonitor(config)
        monitor.start()
        assert len(monitor._threads) == 2
        assert all(t.is_alive() for t in monitor._threads)

        monitor.stop()
        assert all(not t.is_alive() for t in monitor._threads)

    def test_get_health_status(self, config: EngineConfig) -> None:
        """Health status should contain all sections."""
        monitor = HealthMonitor(config)
        health = monitor.get_health_status()
        assert "memory" in health
        assert "disk" in health
        assert "uptime_secs" in health
        assert "monitoring" in health

    def test_uptime_increases(self, config: EngineConfig) -> None:
        """Uptime should increase over time."""
        monitor = HealthMonitor(config)
        monitor._start_time = time.monotonic() - 10  # Pretend started 10s ago
        health = monitor.get_health_status()
        assert health["uptime_secs"] >= 10
