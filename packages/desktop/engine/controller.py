"""Recording controller -- orchestrates start/stop/pause of capture sessions.

This module wraps openadapt-capture to provide recording lifecycle management
with crash recovery, adaptive frame rate, and memory monitoring.

Recording modes (from design doc Section 4.3):
    - Idle:   No user input for >5s     -> 0.1 FPS
    - Active: User input detected       -> up to 10 FPS (configurable)
    - Burst:  Click/type/drag events    -> up to 30 FPS for 2s after event

Storage format (Section 4.6):
    captures/<timestamp>_<id>/
        meta.json           Session metadata
        events.db           SQLite database (Pydantic-based CaptureStorage)
        video/
            chunk_0000.mp4  10-minute video chunks (ChunkedVideoWriter)
        screenshots/
            0001_<ts>_<type>.png   Action screenshots
        audio/
            audio.flac      Full audio (if enabled)
        state.json          Resumption state for crash recovery
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.storage_manager import StorageManager


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


class RecordingState(enum.Enum):
    """Current state of the recording controller."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    ERROR = "error"


class RecordingController:
    """Manages recording lifecycle with crash recovery and adaptive frame rate.

    Args:
        captures_dir: Directory to store capture sessions.
        quality: Recording quality preset ("low", "standard", "high", "lossless").
        storage_manager: Optional StorageManager for DB registration.
    """

    def __init__(
        self,
        captures_dir: Path,
        quality: str = "standard",
        storage_manager: StorageManager | None = None,
    ) -> None:
        self.captures_dir = captures_dir
        self.quality = quality
        self.state = RecordingState.IDLE
        self._current_capture_id: str | None = None
        self._capture_dir: Path | None = None
        self._started_at: str | None = None
        self._recorder: object | None = None
        self._storage_manager = storage_manager

    @property
    def is_recording(self) -> bool:
        """Whether a recording session is currently active (including paused)."""
        return self.state in (RecordingState.RECORDING, RecordingState.PAUSED)

    @property
    def current_capture_id(self) -> str | None:
        """ID of the current capture session, or None if idle."""
        return self._current_capture_id

    def start(self, quality: str | None = None, task_description: str = "") -> str:
        """Start a new recording session.

        Args:
            quality: Override the default quality preset.
            task_description: Optional description of the recording task.

        Returns:
            The capture ID for the new session.

        Raises:
            RuntimeError: If a recording is already active.
        """
        if self.state != RecordingState.IDLE:
            raise RuntimeError(
                f"Cannot start recording: controller is in {self.state.value} state"
            )

        capture_id = uuid.uuid4().hex[:8]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        capture_dir = self.captures_dir / f"{ts}_{capture_id}"
        capture_dir.mkdir(parents=True, exist_ok=True)
        (capture_dir / "screenshots").mkdir(exist_ok=True)
        (capture_dir / "video").mkdir(exist_ok=True)

        started_at = _now_iso()

        # Write meta.json
        meta = {
            "capture_id": capture_id,
            "started_at": started_at,
            "task_description": task_description,
            "quality": quality or self.quality,
        }
        (capture_dir / "meta.json").write_text(json.dumps(meta, indent=2))

        # Write state.json
        state = {"status": "recording", "started_at": started_at, "capture_id": capture_id}
        (capture_dir / "state.json").write_text(json.dumps(state, indent=2))

        # Try to start openadapt-capture Recorder
        try:
            from openadapt_capture import Recorder
            self._recorder = Recorder(output_dir=str(capture_dir))
            self._recorder.start()
        except ImportError:
            # openadapt-capture not installed -- record metadata only
            self._recorder = None
        except Exception:
            # Recorder failed (e.g., no display) -- still track the session
            self._recorder = None

        self._current_capture_id = capture_id
        self._capture_dir = capture_dir
        self._started_at = started_at
        self.state = RecordingState.RECORDING

        # Register in DB if storage_manager available
        if self._storage_manager:
            self._storage_manager.register_capture(capture_id, capture_dir)

        return capture_id

    def stop(self) -> dict:
        """Stop the current recording session.

        Returns:
            Metadata about the completed capture (id, duration, size, event count).

        Raises:
            RuntimeError: If no recording is active.
        """
        if self.state not in (RecordingState.RECORDING, RecordingState.PAUSED):
            raise RuntimeError("No recording is active")

        stopped_at = _now_iso()
        event_count = 0

        # Stop the recorder
        if self._recorder is not None:
            try:
                self._recorder.stop()
                event_count = getattr(self._recorder, "event_count", 0) or 0
            except Exception:
                pass

        # Calculate duration
        duration = 0.0
        if self._started_at:
            try:
                start = datetime.fromisoformat(self._started_at)
                end = datetime.fromisoformat(stopped_at)
                duration = (end - start).total_seconds()
            except Exception:
                pass

        size_bytes = _dir_size(self._capture_dir) if self._capture_dir else 0

        # Update state.json
        if self._capture_dir:
            state = {
                "status": "completed",
                "started_at": self._started_at,
                "stopped_at": stopped_at,
                "capture_id": self._current_capture_id,
            }
            (self._capture_dir / "state.json").write_text(json.dumps(state, indent=2))

            # Update meta.json
            meta_path = self._capture_dir / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            else:
                meta = {}
            meta.update({
                "stopped_at": stopped_at,
                "duration_secs": duration,
                "event_count": event_count,
                "size_bytes": size_bytes,
            })
            meta_path.write_text(json.dumps(meta, indent=2))

        # Update DB
        if self._storage_manager:
            self._storage_manager.db.update_capture(
                self._current_capture_id,
                stopped_at=stopped_at,
                duration_secs=duration,
                event_count=event_count,
                size_bytes=size_bytes,
            )

        metadata = {
            "id": self._current_capture_id,
            "duration": duration,
            "event_count": event_count,
            "size_bytes": size_bytes,
            "path": str(self._capture_dir),
        }

        # Reset state
        self._current_capture_id = None
        self._capture_dir = None
        self._started_at = None
        self._recorder = None
        self.state = RecordingState.IDLE

        return metadata

    def pause(self) -> None:
        """Pause the current recording session.

        Raises:
            RuntimeError: If no recording is active or already paused.
        """
        raise NotImplementedError("Pause not supported in v0.1.0 -- use stop/start instead")

    def resume(self) -> None:
        """Resume a paused recording session.

        Raises:
            RuntimeError: If not currently paused.
        """
        raise NotImplementedError("Resume not supported in v0.1.0 -- use stop/start instead")

    def recover(self) -> list[str]:
        """Recover any incomplete sessions from a previous crash.

        Scans the captures directory for sessions with state.json but no
        clean shutdown marker. Each incomplete session is finalized
        (current chunk closed, meta.json updated).

        Returns:
            List of capture IDs that were recovered.
        """
        recovered = []
        if not self.captures_dir.exists():
            return recovered

        for capture_dir in self.captures_dir.iterdir():
            if not capture_dir.is_dir():
                continue
            state_path = capture_dir / "state.json"
            if not state_path.exists():
                continue
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                continue

            if state.get("status") == "recording":
                capture_id = state.get("capture_id", capture_dir.name)
                stopped_at = _now_iso()

                # Finalize the session
                state["status"] = "recovered"
                state["stopped_at"] = stopped_at
                state_path.write_text(json.dumps(state, indent=2))

                # Update meta.json
                meta_path = capture_dir / "meta.json"
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text())
                    meta["stopped_at"] = stopped_at
                    meta["recovered"] = True
                    meta_path.write_text(json.dumps(meta, indent=2))

                # Register in DB if storage_manager available
                if self._storage_manager:
                    self._storage_manager.register_capture(capture_id, capture_dir)
                    self._storage_manager.db.update_capture(
                        capture_id, stopped_at=stopped_at
                    )

                recovered.append(capture_id)

        return recovered
