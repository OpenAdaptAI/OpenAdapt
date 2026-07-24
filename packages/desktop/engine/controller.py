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
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from engine.db import IndexDB
    from engine.flow_bridge import FlowBridge
    from engine.storage_manager import StorageManager


def _load_capture_recorder():
    """Load the native recorder or fail before claiming a recording started."""

    if getattr(sys, "frozen", False):
        from engine.managed_vision import ensure_managed_vision_runtime

        ensure_managed_vision_runtime()
    try:
        from openadapt_capture import Recorder
    except ImportError as exc:
        raise RuntimeError(
            "OpenAdapt Capture could not be imported; reinstall OpenAdapt Desktop"
        ) from exc
    if Recorder is None:
        raise RuntimeError("OpenAdapt Capture's native recorder is unavailable on this system")
    return Recorder


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
        flow_bridge: FlowBridge | None = None,
        db: IndexDB | None = None,
        bundles_dir: Path | None = None,
        auto_compile: bool = False,
    ) -> None:
        self.captures_dir = captures_dir
        self.quality = quality
        self.state = RecordingState.IDLE
        self._current_capture_id: str | None = None
        self._capture_dir: Path | None = None
        self._started_at: str | None = None
        self._recorder: object | None = None
        self._storage_manager = storage_manager
        # Loop wiring: after a recording stops, compile it into an
        # openadapt-flow bundle and track it locally (spec W1).
        self._flow_bridge = flow_bridge
        self._db = db
        self._bundles_dir = bundles_dir
        self._auto_compile = auto_compile

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
            raise RuntimeError(f"Cannot start recording: controller is in {self.state.value} state")

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

        state_path = capture_dir / "state.json"
        state = {"status": "starting", "started_at": started_at, "capture_id": capture_id}
        state_path.write_text(json.dumps(state, indent=2))

        recorder = None
        try:
            Recorder = _load_capture_recorder()
            recorder = Recorder(str(capture_dir), task_description=task_description)
            recorder.__enter__()
            if not recorder.wait_for_ready(timeout=60):
                raise RuntimeError("OpenAdapt Capture did not become ready within 60 seconds")
            if not recorder.is_recording:
                raise RuntimeError("OpenAdapt Capture stopped before recording became ready")

            self._recorder = recorder
            self._current_capture_id = capture_id
            self._capture_dir = capture_dir
            self._started_at = started_at
            self.state = RecordingState.RECORDING
            state["status"] = "recording"
            state_path.write_text(json.dumps(state, indent=2))

            if self._storage_manager:
                self._storage_manager.register_capture(capture_id, capture_dir)
        except Exception as exc:
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    logger.exception("Recorder stop failed after start error")
                try:
                    recorder.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    logger.exception("Recorder exit failed after start error")
            state.update({"status": "failed", "failed_at": _now_iso(), "error": str(exc)})
            try:
                state_path.write_text(json.dumps(state, indent=2))
            except Exception:
                logger.exception("Could not persist failed recording state")
            self._current_capture_id = None
            self._capture_dir = None
            self._started_at = None
            self._recorder = None
            self.state = RecordingState.IDLE
            raise RuntimeError(f"Could not start native recording: {exc}") from exc

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

        recorder = self._recorder
        if recorder is None:
            raise RuntimeError("Recording state is active but the native recorder is missing")

        try:
            recorder.stop()
            recorder.__exit__(None, None, None)
            event_count = getattr(recorder, "event_count", 0) or 0
        except Exception as exc:
            try:
                recorder.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                logger.exception("Recorder cleanup failed after stop error")
            if self._capture_dir:
                state = {
                    "status": "failed",
                    "started_at": self._started_at,
                    "failed_at": _now_iso(),
                    "capture_id": self._current_capture_id,
                    "error": str(exc),
                }
                (self._capture_dir / "state.json").write_text(json.dumps(state, indent=2))
            self._current_capture_id = None
            self._capture_dir = None
            self._started_at = None
            self._recorder = None
            self.state = RecordingState.IDLE
            raise RuntimeError(f"Could not stop native recording cleanly: {exc}") from exc

        capture_id = self._current_capture_id
        capture_dir = self._capture_dir
        started_at = self._started_at
        try:
            duration = 0.0
            if started_at:
                try:
                    start = datetime.fromisoformat(started_at)
                    end = datetime.fromisoformat(stopped_at)
                    duration = (end - start).total_seconds()
                except Exception:
                    pass

            size_bytes = _dir_size(capture_dir) if capture_dir else 0

            if capture_dir:
                state = {
                    "status": "completed",
                    "started_at": started_at,
                    "stopped_at": stopped_at,
                    "capture_id": capture_id,
                }
                (capture_dir / "state.json").write_text(json.dumps(state, indent=2))

                meta_path = capture_dir / "meta.json"
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text())
                else:
                    meta = {}
                meta.update(
                    {
                        "stopped_at": stopped_at,
                        "duration_secs": duration,
                        "event_count": event_count,
                        "size_bytes": size_bytes,
                    }
                )
                meta_path.write_text(json.dumps(meta, indent=2))

            if self._storage_manager:
                self._storage_manager.db.update_capture(
                    capture_id,
                    stopped_at=stopped_at,
                    duration_secs=duration,
                    event_count=event_count,
                    size_bytes=size_bytes,
                )

            metadata = {
                "id": capture_id,
                "duration": duration,
                "event_count": event_count,
                "size_bytes": size_bytes,
                "path": str(capture_dir),
            }

            if self._auto_compile and self._flow_bridge is not None and capture_dir:
                compiled = self.compile_capture(capture_id, capture_dir)
                if compiled:
                    metadata["bundle_id"] = compiled.get("bundle_id")
                    metadata["bundle_path"] = compiled.get("bundle_path")

            return metadata
        except Exception as exc:
            if capture_dir:
                failed_state = {
                    "status": "failed",
                    "started_at": started_at,
                    "failed_at": _now_iso(),
                    "capture_id": capture_id,
                    "error": str(exc),
                }
                try:
                    (capture_dir / "state.json").write_text(json.dumps(failed_state, indent=2))
                except Exception:
                    logger.exception("Could not persist failed recording finalization")
            raise RuntimeError(f"Could not finalize native recording: {exc}") from exc
        finally:
            self._current_capture_id = None
            self._capture_dir = None
            self._started_at = None
            self._recorder = None
            self.state = RecordingState.IDLE

    def compile_capture(self, capture_id: str, capture_dir: Path) -> dict | None:
        """Compile a finished recording into an openadapt-flow bundle directory.

        Wraps ``openadapt-flow compile`` via :class:`~engine.flow_bridge.FlowBridge`
        and records the bundle in the ``bundles`` table when a DB is wired.

        Args:
            capture_id: The capture/recording id.
            capture_dir: The recording directory to compile.

        Returns:
            ``{"bundle_id", "bundle_path", "ok"}`` on success, or None if flow is
            unavailable / the compile failed.
        """
        if self._flow_bridge is None:
            return None
        bundles_dir = self._bundles_dir or (self.captures_dir.parent / "bundles")
        bundles_dir.mkdir(parents=True, exist_ok=True)
        bundle_id = uuid.uuid4().hex[:8]
        bundle_path = bundles_dir / f"{capture_id}_{bundle_id}"
        try:
            result = self._flow_bridge.compile(Path(capture_dir), bundle_path)
        except Exception as exc:
            logger.warning("Compile failed for {cid}: {e}", cid=capture_id, e=exc)
            return None
        if not result.ok:
            logger.warning(
                "Compile returned nonzero for {cid}: {err}", cid=capture_id, err=result.stderr[:200]
            )
            return None
        if self._db is not None:
            try:
                self._db.insert_bundle(bundle_id, str(bundle_path), capture_id=capture_id)
            except Exception as exc:
                logger.warning("Could not record bundle {bid}: {e}", bid=bundle_id, e=exc)
        return {"bundle_id": bundle_id, "bundle_path": str(bundle_path), "ok": True}

    def pause(self) -> None:
        """Pause the current recording session.

        Raises:
            RuntimeError: If no recording is active or already paused.
        """
        raise NotImplementedError("Pause is not supported; use stop/start instead")

    def resume(self) -> None:
        """Resume a paused recording session.

        Raises:
            RuntimeError: If not currently paused.
        """
        raise NotImplementedError("Resume is not supported; use stop/start instead")

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
                    self._storage_manager.db.update_capture(capture_id, stopped_at=stopped_at)

                recovered.append(capture_id)

        return recovered
