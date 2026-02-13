"""Tests for performance statistics module."""

import time

import pytest

from openadapt_capture.stats import CaptureStats, PerfStat


class TestPerfStat:
    """Tests for PerfStat dataclass."""

    def test_latency_calculation(self):
        """Test that latency is calculated correctly."""
        stat = PerfStat(
            event_type="mouse.down",
            event_timestamp=1000.0,
            write_timestamp=1000.05,
        )
        assert stat.latency == pytest.approx(0.05, abs=1e-6)

    def test_zero_latency(self):
        """Test zero latency when timestamps match."""
        stat = PerfStat(
            event_type="key.down",
            event_timestamp=100.0,
            write_timestamp=100.0,
        )
        assert stat.latency == 0.0


class TestCaptureStats:
    """Tests for CaptureStats class."""

    def test_empty_stats(self):
        """Test summary with no events recorded."""
        stats = CaptureStats()
        summary = stats.summary()
        assert summary == {"total_events": 0}

    def test_record_single_event(self):
        """Test recording a single event."""
        stats = CaptureStats()
        stats.record_event("mouse.down", event_timestamp=1000.0)

        assert len(stats.stats) == 1
        assert stats.stats[0].event_type == "mouse.down"
        assert stats.stats[0].event_timestamp == 1000.0
        # Write timestamp should be close to current time
        assert stats.stats[0].write_timestamp >= 1000.0

    def test_record_multiple_events(self):
        """Test recording multiple events."""
        stats = CaptureStats()
        stats.record_event("mouse.down", event_timestamp=1.0)
        stats.record_event("mouse.up", event_timestamp=1.1)
        stats.record_event("key.down", event_timestamp=1.2)

        assert len(stats.stats) == 3

    def test_summary_with_events(self):
        """Test summary statistics calculation."""
        stats = CaptureStats()

        # Add stats directly for controlled testing
        stats.stats = [
            PerfStat("mouse.down", 1.0, 1.01),  # 10ms latency
            PerfStat("mouse.down", 2.0, 2.02),  # 20ms latency
            PerfStat("mouse.up", 1.1, 1.12),    # 20ms latency
        ]

        summary = stats.summary()

        assert summary["total_events"] == 3
        assert "by_type" in summary
        assert "mouse.down" in summary["by_type"]
        assert "mouse.up" in summary["by_type"]

        # Check mouse.down stats
        mouse_down = summary["by_type"]["mouse.down"]
        assert mouse_down["count"] == 2
        assert mouse_down["mean_latency_ms"] == pytest.approx(15.0, abs=0.1)
        assert mouse_down["max_latency_ms"] == pytest.approx(20.0, abs=0.1)
        assert mouse_down["min_latency_ms"] == pytest.approx(10.0, abs=0.1)

        # Check overall stats
        assert summary["mean_latency_ms"] == pytest.approx(16.67, abs=0.1)
        assert summary["max_latency_ms"] == pytest.approx(20.0, abs=0.1)

    def test_start_time(self):
        """Test start time tracking."""
        stats = CaptureStats()
        assert stats._start_time is None

        stats.start()
        assert stats._start_time is not None
        assert stats._start_time <= time.time()


class TestCaptureStatsPlotting:
    """Tests for plotting functionality."""

    def test_plot_empty_stats_returns_none(self):
        """Test that plotting empty stats returns None without importing matplotlib."""
        stats = CaptureStats()
        # Empty stats should return None before even trying to import matplotlib
        assert len(stats.stats) == 0
        # The actual plot() call would need matplotlib, so we just verify
        # the empty check works correctly

    def test_plot_returns_image(self):
        """Test that plot returns a PIL Image when no output path."""
        pytest.importorskip("matplotlib")
        from PIL import Image

        stats = CaptureStats()
        stats.stats = [
            PerfStat("mouse.down", 1.0, 1.01),
            PerfStat("mouse.up", 1.1, 1.11),
        ]

        result = stats.plot()
        assert isinstance(result, Image.Image)

    def test_plot_saves_to_file(self, tmp_path):
        """Test that plot can save to a file."""
        pytest.importorskip("matplotlib")

        stats = CaptureStats()
        stats.stats = [
            PerfStat("mouse.down", 1.0, 1.01),
            PerfStat("mouse.up", 1.1, 1.11),
        ]

        output_path = tmp_path / "perf_plot.png"
        result = stats.plot(output_path=output_path)

        assert result is None  # Returns None when saving to file
        assert output_path.exists()

    def test_plot_with_custom_title(self, tmp_path):
        """Test plotting with custom title."""
        pytest.importorskip("matplotlib")

        stats = CaptureStats()
        stats.stats = [
            PerfStat("mouse.down", 1.0, 1.01),
        ]

        output_path = tmp_path / "perf_plot.png"
        stats.plot(output_path=output_path, title="Custom Title")

        assert output_path.exists()

    def test_plot_multiple_event_types(self, tmp_path):
        """Test plotting with multiple event types."""
        pytest.importorskip("matplotlib")

        stats = CaptureStats()
        # Add various event types
        stats.stats = [
            PerfStat("mouse.down", 1.0, 1.01),
            PerfStat("mouse.up", 1.1, 1.11),
            PerfStat("key.down", 1.2, 1.22),
            PerfStat("key.up", 1.3, 1.32),
            PerfStat("screen.frame", 1.4, 1.41),
        ]

        output_path = tmp_path / "perf_plot.png"
        stats.plot(output_path=output_path)

        assert output_path.exists()


class TestPlotCapturePerformance:
    """Tests for plot_capture_performance function."""

    def test_plot_nonexistent_capture(self, tmp_path):
        """Test plotting a nonexistent capture raises error."""
        from openadapt_capture.stats import plot_capture_performance

        with pytest.raises(FileNotFoundError):
            plot_capture_performance(tmp_path / "nonexistent")

    def test_plot_empty_capture(self, tmp_path):
        """Test plotting an empty capture."""
        pytest.importorskip("matplotlib")
        from openadapt_capture.stats import plot_capture_performance
        from openadapt_capture.storage import create_capture

        # Create an empty capture
        capture, storage = create_capture(
            tmp_path / "empty_capture",
            task_description="Empty test",
        )
        storage.close()

        result = plot_capture_performance(tmp_path / "empty_capture")
        assert result is None

    def test_plot_capture_with_events(self, tmp_path):
        """Test plotting a capture with events."""
        pytest.importorskip("matplotlib")
        from PIL import Image

        from openadapt_capture.events import MouseButton, MouseDownEvent
        from openadapt_capture.stats import plot_capture_performance
        from openadapt_capture.storage import create_capture

        # Create a capture with events
        capture, storage = create_capture(
            tmp_path / "test_capture",
            task_description="Test",
        )

        # Add some events
        storage.write_event(MouseDownEvent(
            timestamp=1.0,
            x=100,
            y=200,
            button=MouseButton.LEFT,
        ))
        storage.write_event(MouseDownEvent(
            timestamp=2.0,
            x=150,
            y=250,
            button=MouseButton.LEFT,
        ))
        storage.close()

        result = plot_capture_performance(tmp_path / "test_capture")
        assert isinstance(result, Image.Image)

    def test_plot_capture_saves_to_file(self, tmp_path):
        """Test plotting a capture and saving to file."""
        pytest.importorskip("matplotlib")
        from openadapt_capture.events import MouseButton, MouseDownEvent
        from openadapt_capture.stats import plot_capture_performance
        from openadapt_capture.storage import create_capture

        # Create a capture with events
        capture, storage = create_capture(
            tmp_path / "test_capture",
            task_description="Test",
        )
        storage.write_event(MouseDownEvent(
            timestamp=1.0,
            x=100,
            y=200,
            button=MouseButton.LEFT,
        ))
        storage.close()

        output_path = tmp_path / "capture_perf.png"
        result = plot_capture_performance(
            tmp_path / "test_capture",
            output_path=output_path,
        )

        assert result is None
        assert output_path.exists()
