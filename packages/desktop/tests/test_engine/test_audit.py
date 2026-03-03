"""Tests for the audit logger."""

from __future__ import annotations

import json
from pathlib import Path

from engine.audit import AuditLogger


class TestAuditLogger:
    """Tests for audit logging functionality."""

    def test_log_creates_file(self, tmp_path: Path) -> None:
        """Logging should create the audit log file."""
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path=log_path)
        logger.log("test_event", key="value")
        assert log_path.exists()

    def test_log_writes_valid_jsonl(self, tmp_path: Path) -> None:
        """Each log entry should be valid JSON on its own line."""
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path=log_path)
        logger.log("event_one", data=1)
        logger.log("event_two", data=2)

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert "ts" in entry
            assert "event" in entry

    def test_log_includes_timestamp(self, tmp_path: Path) -> None:
        """Each log entry should include an ISO 8601 timestamp."""
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path=log_path)
        logger.log("test_event")

        entry = json.loads(log_path.read_text().strip())
        assert "ts" in entry
        # ISO 8601 timestamps contain "T" between date and time
        assert "T" in entry["ts"]

    def test_log_startup(self, tmp_path: Path) -> None:
        """Startup log should include storage mode and backend lists."""
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path=log_path)
        logger.log_startup(storage_mode="enterprise", active_backends=["s3"])

        entry = json.loads(log_path.read_text().strip())
        assert entry["event"] == "startup"
        assert entry["storage_mode"] == "enterprise"
        assert "s3" in entry["backends"]
        assert "hf" in entry["excluded"]

    def test_disabled_logger_writes_nothing(self, tmp_path: Path) -> None:
        """A disabled logger should not create any files."""
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path=log_path, enabled=False)
        logger.log("test_event")
        assert not log_path.exists()
