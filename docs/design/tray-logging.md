# OpenAdapt Tray: Logging & Action Storage

This document supplements the main `openadapt-tray` design document with detailed specifications for logging, action history, telemetry integration, and storage considerations.

## Table of Contents

1. [Local Logging](#local-logging)
2. [Action History](#action-history)
3. [Telemetry Integration](#telemetry-integration)
4. [Privacy Considerations](#privacy-considerations)
5. [Storage Locations](#storage-locations)
6. [Integration with Existing Packages](#integration-with-existing-packages)

---

## Local Logging

### Platform-Specific Log Paths

The tray application stores logs in platform-appropriate locations following OS conventions:

| Platform | Log Directory |
|----------|---------------|
| macOS | `~/Library/Application Support/OpenAdapt/logs/` |
| Windows | `%APPDATA%/OpenAdapt/logs/` |
| Linux | `~/.local/share/openadapt/logs/` |

### Log File Naming

```
openadapt-tray.log          # Current log file
openadapt-tray.log.1        # Previous rotation (newest)
openadapt-tray.log.2        # Older rotation
...
openadapt-tray.log.5        # Oldest rotation
```

### Log Rotation Policy

| Setting | Value | Rationale |
|---------|-------|-----------|
| **Max File Size** | 10 MB | Prevents disk space issues |
| **Max Backup Count** | 5 files | ~50 MB total log storage |
| **Rotation Trigger** | Size-based | Predictable disk usage |
| **Compression** | gzip for backups | Reduces storage footprint |

### Log Retention Policy

- **Active logs**: Rotated based on size (10 MB threshold)
- **Rotated logs**: Kept for 30 days or 5 rotations, whichever comes first
- **Crash logs**: Retained for 90 days for debugging
- **Automatic cleanup**: Old logs purged on app startup

### Log Levels

| Environment | Level | Description |
|-------------|-------|-------------|
| **Production** | `INFO` | Normal operations, errors, warnings |
| **Debug** | `DEBUG` | Verbose output including state changes |
| **Trace** | `TRACE` | Extremely verbose, including IPC messages |

### Log Level Configuration

```python
# Environment variable override
OPENADAPT_TRAY_LOG_LEVEL=DEBUG

# Or via config.json
{
    "logging": {
        "level": "INFO",
        "console": false,
        "file": true
    }
}
```

### Log Format

```
2024-01-15 10:30:45.123 | INFO     | tray.main:start_recording:42 - Recording session started
2024-01-15 10:30:45.456 | DEBUG    | tray.menu:update_state:78 - Menu state updated: recording=True
2024-01-15 10:31:12.789 | ERROR    | tray.capture:on_error:156 - Capture failed: Permission denied
```

Format specification:
```
{timestamp} | {level:8} | {module}:{function}:{line} - {message}
```

### Implementation Example

```python
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import sys

def get_log_directory() -> Path:
    """Get platform-appropriate log directory."""
    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:  # Linux and others
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))

    log_dir = base / "OpenAdapt" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure logging for the tray application."""
    logger = logging.getLogger("openadapt.tray")
    logger.setLevel(getattr(logging, level.upper()))

    # File handler with rotation
    log_file = get_log_directory() / "openadapt-tray.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "{asctime} | {levelname:8} | {name}:{funcName}:{lineno} - {message}",
        style="{",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(file_handler)

    return logger
```

---

## Action History

### Overview

The tray app maintains a local history of user interactions for:
- Auditing user actions
- Supporting undo/redo functionality
- Debugging session issues
- Syncing state with other OpenAdapt components

### Tracked Actions

| Action Type | Data Captured | Purpose |
|-------------|---------------|---------|
| `recording.start` | timestamp, task_name, settings | Session tracking |
| `recording.stop` | timestamp, duration, frame_count | Session completion |
| `recording.pause` | timestamp | Session state |
| `recording.resume` | timestamp | Session state |
| `training.start` | timestamp, model_type, demo_ids | Training tracking |
| `training.complete` | timestamp, duration, success | Training outcomes |
| `training.cancel` | timestamp, reason | Training interruptions |
| `settings.changed` | key, old_value, new_value | Configuration audit |
| `app.start` | timestamp, version, os_info | Lifecycle tracking |
| `app.stop` | timestamp, exit_reason | Lifecycle tracking |
| `error.occurred` | timestamp, error_type, context | Error tracking |

### Storage Format

Action history is stored in a local SQLite database for efficient querying and reliable storage.

#### Database Schema

```sql
-- Action history table
CREATE TABLE action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,              -- ISO 8601 format
    action_type TEXT NOT NULL,            -- e.g., 'recording.start'
    session_id TEXT,                      -- Groups related actions
    data TEXT,                            -- JSON blob for action-specific data
    synced INTEGER DEFAULT 0,             -- Sync status with capture DB
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Index for common queries
CREATE INDEX idx_action_timestamp ON action_history(timestamp);
CREATE INDEX idx_action_type ON action_history(action_type);
CREATE INDEX idx_session_id ON action_history(session_id);
CREATE INDEX idx_synced ON action_history(synced);

-- Session metadata table
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,                  -- UUID
    task_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT DEFAULT 'active',         -- active, completed, cancelled, error
    frame_count INTEGER DEFAULT 0,
    duration_seconds REAL,
    capture_db_id TEXT                    -- Reference to openadapt-capture DB
);
```

#### Example Records

```json
{
    "id": 1,
    "timestamp": "2024-01-15T10:30:45.123Z",
    "action_type": "recording.start",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "data": {
        "task_name": "Fill out expense report",
        "settings": {
            "capture_screenshots": true,
            "capture_audio": false,
            "fps": 1
        }
    },
    "synced": 0
}
```

### Sync with openadapt-capture Database

The tray app synchronizes action history with the capture package's database to maintain a unified record:

```python
from pathlib import Path
import sqlite3
from typing import Optional
import json

class ActionHistorySync:
    """Sync tray action history with capture database."""

    def __init__(self, tray_db_path: Path, capture_db_path: Optional[Path] = None):
        self.tray_db = tray_db_path
        self.capture_db = capture_db_path

    def sync_session(self, session_id: str) -> bool:
        """Sync a completed session to capture database."""
        if not self.capture_db or not self.capture_db.exists():
            return False

        with sqlite3.connect(self.tray_db) as tray_conn:
            # Get unsynced actions for this session
            actions = tray_conn.execute(
                """
                SELECT id, timestamp, action_type, data
                FROM action_history
                WHERE session_id = ? AND synced = 0
                ORDER BY timestamp
                """,
                (session_id,)
            ).fetchall()

        if not actions:
            return True

        with sqlite3.connect(self.capture_db) as capture_conn:
            # Insert into capture database's session_events table
            for action_id, timestamp, action_type, data in actions:
                capture_conn.execute(
                    """
                    INSERT INTO session_events (timestamp, event_type, data, source)
                    VALUES (?, ?, ?, 'tray')
                    """,
                    (timestamp, action_type, data)
                )
            capture_conn.commit()

        # Mark as synced
        with sqlite3.connect(self.tray_db) as tray_conn:
            tray_conn.executemany(
                "UPDATE action_history SET synced = 1 WHERE id = ?",
                [(a[0],) for a in actions]
            )
            tray_conn.commit()

        return True
```

### Retention Policy

| Data Type | Retention Period | Rationale |
|-----------|------------------|-----------|
| Action history | 90 days | Debugging and audit trail |
| Session metadata | 1 year | Long-term usage patterns |
| Synced records | 30 days (then delete) | Reduce redundancy |

---

## Telemetry Integration

### Reference Design

For detailed telemetry implementation, see the comprehensive telemetry design at [docs/design/telemetry-design.md](./telemetry-design.md).

### GlitchTip/Sentry Integration

The tray app uses the shared `openadapt-telemetry` module for crash reporting and error tracking.

```python
# Initialize telemetry in tray app
from openadapt_telemetry import get_telemetry

def init_app():
    """Initialize the tray application."""
    telemetry = get_telemetry()
    telemetry.initialize(
        package_name="openadapt-tray",
        package_version=__version__,
    )
```

### Error and Crash Reporting

```python
from openadapt_telemetry import get_telemetry, track_errors

class TrayApp:
    @track_errors(reraise=True)
    def start_recording(self, task_name: str) -> None:
        """Start a recording session."""
        try:
            # Recording logic...
            pass
        except PermissionError as e:
            get_telemetry().capture_exception(e, tags={
                "action": "start_recording",
                "platform": platform.system(),
            })
            raise
```

### Anonymous Usage Analytics (Opt-In)

Usage analytics are strictly opt-in and collect only aggregate, non-identifying data.

#### Events Tracked

| Event | Data Collected | Purpose |
|-------|----------------|---------|
| `tray.app_start` | timestamp, version, os, internal_flag | App lifecycle |
| `tray.app_stop` | timestamp, uptime_seconds, exit_reason | App lifecycle |
| `tray.recording_session` | duration_seconds, success, frame_count | Usage patterns |
| `tray.training_initiated` | model_type, demo_count | Feature usage |
| `tray.error` | error_type (no message), context | Error patterns |

#### Event Implementation

```python
from openadapt_telemetry import get_telemetry

def track_recording_session(duration: float, success: bool, frame_count: int):
    """Track recording session metrics (opt-in only)."""
    telemetry = get_telemetry()

    if not telemetry.is_analytics_enabled():
        return

    telemetry.capture_event(
        "tray.recording_session",
        {
            "duration_seconds": round(duration, 1),
            "success": success,
            "frame_count_bucket": bucket_count(frame_count),  # 0-10, 10-50, 50-100, 100+
        }
    )

def bucket_count(count: int) -> str:
    """Bucket counts to avoid exact numbers (privacy)."""
    if count <= 10:
        return "0-10"
    elif count <= 50:
        return "10-50"
    elif count <= 100:
        return "50-100"
    else:
        return "100+"
```

---

## Privacy Considerations

### Core Principles

1. **Local-First**: All data stored locally by default
2. **No PII**: Never collect personally identifiable information
3. **No Content**: Never collect screenshots, recordings, or user input
4. **Explicit Consent**: Cloud sync and analytics require opt-in
5. **Transparency**: Users can inspect all stored data

### What Is Never Collected or Transmitted

| Data Type | Reason |
|-----------|--------|
| Screenshots | Highly sensitive, potential PII |
| Recorded actions | Contains user behavior data |
| Typed text | PII and sensitive content |
| File paths with usernames | PII leakage |
| IP addresses | Location identification |
| Hardware identifiers | Device fingerprinting |
| Window titles | May contain sensitive info |

### Opt-In/Opt-Out Settings

```json
// config.json
{
    "telemetry": {
        "crash_reporting": true,       // Enabled by default, can disable
        "anonymous_analytics": false,  // Disabled by default, opt-in
        "cloud_sync": false            // Disabled by default, opt-in
    }
}
```

### Settings UI Integration

The tray app settings menu should include clear telemetry controls:

```
Settings > Privacy
├── [x] Send crash reports (helps improve stability)
├── [ ] Share anonymous usage statistics
├── [ ] Sync settings across devices
└── [View collected data...] -> Opens local data directory
```

### Data Inspection

Users can inspect all locally stored data:

```python
def open_data_directory():
    """Open the OpenAdapt data directory in file explorer."""
    import subprocess
    import platform

    data_dir = get_data_directory()

    if platform.system() == "Darwin":
        subprocess.run(["open", str(data_dir)])
    elif platform.system() == "Windows":
        subprocess.run(["explorer", str(data_dir)])
    else:
        subprocess.run(["xdg-open", str(data_dir)])
```

### Data Deletion

Users can delete all local data:

```python
def clear_all_data(keep_config: bool = True):
    """Delete all OpenAdapt local data."""
    data_dir = get_data_directory()

    for item in data_dir.iterdir():
        if keep_config and item.name == "config.json":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    logger.info("All local data cleared")
```

---

## Storage Locations

### Directory Structure

```
macOS:   ~/Library/Application Support/OpenAdapt/
Windows: %APPDATA%/OpenAdapt/
Linux:   ~/.local/share/openadapt/

Contents:
├── logs/                    # Application logs
│   ├── openadapt-tray.log   # Current tray app log
│   ├── openadapt-tray.log.1 # Rotated logs
│   └── crash/               # Crash dumps
├── config.json              # User settings and preferences
├── history.db               # Action history (SQLite)
├── cache/                   # Temporary files
│   ├── icons/               # Cached tray icons
│   └── temp/                # Temporary processing files
└── state/                   # Persistent state
    └── session.json         # Current session state (for crash recovery)
```

### Storage Path Resolution

```python
import os
import platform
from pathlib import Path
from typing import Dict

def get_storage_paths() -> Dict[str, Path]:
    """Get all storage paths for the current platform."""

    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "OpenAdapt"
    elif platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
        base = Path(appdata) / "OpenAdapt"
    else:  # Linux and others
        xdg_data = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        base = Path(xdg_data) / "openadapt"

    paths = {
        "base": base,
        "logs": base / "logs",
        "crash_logs": base / "logs" / "crash",
        "config": base / "config.json",
        "history_db": base / "history.db",
        "cache": base / "cache",
        "state": base / "state",
    }

    # Ensure directories exist
    for key, path in paths.items():
        if key not in ("config", "history_db"):  # Don't create files
            path.mkdir(parents=True, exist_ok=True)

    return paths
```

### Config File Schema

```json
{
    "$schema": "https://openadapt.ai/schemas/tray-config-v1.json",
    "version": 1,
    "logging": {
        "level": "INFO",
        "console": false,
        "file": true,
        "max_size_mb": 10,
        "backup_count": 5
    },
    "telemetry": {
        "crash_reporting": true,
        "anonymous_analytics": false,
        "cloud_sync": false
    },
    "recording": {
        "default_fps": 1,
        "capture_audio": false,
        "capture_screenshots": true,
        "auto_pause_on_idle": true,
        "idle_threshold_seconds": 30
    },
    "ui": {
        "show_notifications": true,
        "start_minimized": false,
        "start_on_login": false
    },
    "advanced": {
        "capture_db_path": null,
        "ml_model_path": null
    }
}
```

---

## Integration with Existing Packages

### Shared Telemetry Module

The tray app uses the shared `openadapt-telemetry` module (see [telemetry-design.md](./telemetry-design.md)) for consistent telemetry across all OpenAdapt packages.

```python
# pyproject.toml
[project]
dependencies = [
    "openadapt-telemetry>=0.1.0",
]
```

### Coordination with openadapt-capture

The tray app coordinates with `openadapt-capture` for recording functionality:

```python
from openadapt_capture import RecordingSession, CaptureConfig
from openadapt_tray.history import ActionHistory

class TrayRecordingController:
    """Bridge between tray UI and capture backend."""

    def __init__(self):
        self.history = ActionHistory()
        self.current_session: Optional[RecordingSession] = None

    def start_recording(self, task_name: str, config: CaptureConfig) -> str:
        """Start a new recording session."""
        import uuid

        session_id = str(uuid.uuid4())

        # Log to action history
        self.history.log_action(
            action_type="recording.start",
            session_id=session_id,
            data={"task_name": task_name, "config": config.to_dict()}
        )

        # Start capture backend
        self.current_session = RecordingSession(
            session_id=session_id,
            task_name=task_name,
            config=config,
            on_error=self._on_capture_error,
        )
        self.current_session.start()

        return session_id

    def stop_recording(self) -> dict:
        """Stop the current recording session."""
        if not self.current_session:
            return {"error": "No active session"}

        result = self.current_session.stop()

        # Log completion
        self.history.log_action(
            action_type="recording.stop",
            session_id=self.current_session.session_id,
            data={
                "duration": result.duration,
                "frame_count": result.frame_count,
                "success": result.success,
            }
        )

        # Sync with capture database
        self.history.sync_session(self.current_session.session_id)

        self.current_session = None
        return result.to_dict()

    def _on_capture_error(self, error: Exception):
        """Handle capture errors."""
        get_telemetry().capture_exception(error)
        self.history.log_action(
            action_type="error.occurred",
            session_id=self.current_session.session_id if self.current_session else None,
            data={"error_type": type(error).__name__}
        )
```

### Surfacing Training Logs from openadapt-ml

The tray app can display training progress and logs from the ML package:

```python
from openadapt_ml import TrainingJob, TrainingStatus
from openadapt_tray.notifications import show_notification

class TrayTrainingController:
    """Bridge between tray UI and ML training backend."""

    def __init__(self):
        self.history = ActionHistory()
        self.current_job: Optional[TrainingJob] = None

    def start_training(self, model_type: str, demo_ids: list[str]) -> str:
        """Start a training job."""
        job_id = str(uuid.uuid4())

        self.history.log_action(
            action_type="training.start",
            data={
                "job_id": job_id,
                "model_type": model_type,
                "demo_count": len(demo_ids),
            }
        )

        self.current_job = TrainingJob(
            job_id=job_id,
            model_type=model_type,
            demo_ids=demo_ids,
            on_progress=self._on_training_progress,
            on_complete=self._on_training_complete,
            on_error=self._on_training_error,
        )
        self.current_job.start()

        return job_id

    def _on_training_progress(self, progress: float, message: str):
        """Handle training progress updates."""
        # Update tray icon or menu with progress
        pass

    def _on_training_complete(self, result: TrainingStatus):
        """Handle training completion."""
        self.history.log_action(
            action_type="training.complete",
            data={
                "job_id": self.current_job.job_id,
                "duration": result.duration,
                "success": result.success,
            }
        )

        show_notification(
            title="Training Complete",
            message=f"Model trained successfully in {result.duration:.1f}s"
        )

        # Track telemetry (anonymous)
        get_telemetry().capture_event(
            "tray.training_complete",
            {"model_type": self.current_job.model_type, "success": True}
        )

    def _on_training_error(self, error: Exception):
        """Handle training errors."""
        get_telemetry().capture_exception(error)

        self.history.log_action(
            action_type="training.error",
            data={
                "job_id": self.current_job.job_id if self.current_job else None,
                "error_type": type(error).__name__,
            }
        )

        show_notification(
            title="Training Failed",
            message="An error occurred during training. Check logs for details."
        )
```

### Log Aggregation View

The tray app can provide a unified view of logs from all OpenAdapt components:

```python
from pathlib import Path
from typing import Iterator, NamedTuple
from datetime import datetime

class LogEntry(NamedTuple):
    timestamp: datetime
    level: str
    source: str  # tray, capture, ml, etc.
    message: str

def aggregate_logs(max_entries: int = 1000) -> Iterator[LogEntry]:
    """Aggregate logs from all OpenAdapt components."""

    log_sources = {
        "tray": get_storage_paths()["logs"] / "openadapt-tray.log",
        "capture": get_capture_log_path(),  # From openadapt-capture
        "ml": get_ml_log_path(),            # From openadapt-ml
    }

    entries = []

    for source, log_path in log_sources.items():
        if not log_path.exists():
            continue

        with open(log_path, "r") as f:
            for line in f:
                try:
                    entry = parse_log_line(line, source)
                    if entry:
                        entries.append(entry)
                except Exception:
                    continue

    # Sort by timestamp and return most recent
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return iter(entries[:max_entries])
```

---

## Summary

This document defines the logging and storage architecture for the OpenAdapt tray application:

1. **Local Logging**: Platform-specific paths with rotation and retention policies
2. **Action History**: SQLite-based storage for user interactions, synced with capture database
3. **Telemetry**: Integration with shared telemetry module for crash reporting and opt-in analytics
4. **Privacy**: Local-first approach with no PII collection and clear opt-in/opt-out controls
5. **Storage**: Organized directory structure following OS conventions
6. **Integration**: Seamless coordination with capture, ML, and telemetry packages

For telemetry implementation details, refer to the comprehensive [telemetry design document](./telemetry-design.md).
