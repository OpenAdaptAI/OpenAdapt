"""Tests for the hosted push + report_break egress verbs."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from engine import hosted
from engine.backends.protocol import UploadResult
from engine.hosted import (
    PhiBoundaryError,
    build_break_descriptor,
    report_break,
    zip_dir,
)

from .conftest import FakeResponse


class _StubBackend:
    name = "hosted_ingest"

    def __init__(self, result: UploadResult) -> None:
        self._result = result
        self.uploaded: Path | None = None
        self.metadata: dict | None = None

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        self.uploaded = archive_path
        self.metadata = metadata
        return self._result


class TestZipDir:
    def test_zips_recursively(self, tmp_path: Path) -> None:
        src = tmp_path / "rec"
        (src / "frames").mkdir(parents=True)
        (src / "meta.json").write_text("{}")
        (src / "frames" / "0001.png").write_bytes(b"x")
        out = zip_dir(src)
        assert out.suffix == ".zip"
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "meta.json" in names
        assert "frames/0001.png" in names


class TestPush:
    def test_push_success_persists_workflow_id(self, tmp_path: Path) -> None:
        from engine.db import IndexDB

        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")

        db = IndexDB(tmp_path / "index.db")
        db.initialize()
        db.insert_bundle("bnd1", str(rec), capture_id="rec1")

        backend = _StubBackend(
            UploadResult(success=True, remote_url="https://app/dashboard/workflows/wf_1",
                         metadata={"workflow_id": "wf_1"})
        )
        result = hosted.push(
            rec, kind="recording", host="https://app", backend=backend,
            prefer_flow=False, db=db, bundle_id="bnd1",
        )
        assert result["success"] is True
        assert result["workflow_id"] == "wf_1"
        assert db.get_bundle("bnd1")["workflow_id"] == "wf_1"
        db.close()

    def test_push_default_latest_recording(self, tmp_path: Path) -> None:
        recordings = tmp_path / "recordings"
        (recordings / "old").mkdir(parents=True)
        (recordings / "new").mkdir()
        (recordings / "new" / "meta.json").write_text("{}")
        # Make "new" the most recent.
        import os
        import time

        os.utime(recordings / "new", (time.time() + 10, time.time() + 10))

        backend = _StubBackend(UploadResult(success=True, metadata={"workflow_id": "wf_x"}))
        result = hosted.push(
            None, recordings_dir=recordings, backend=backend, prefer_flow=False, host="https://app"
        )
        assert result["success"] is True
        assert backend.uploaded is not None
        assert backend.metadata["capture_id"] == "new"

    def test_push_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            hosted.push(tmp_path / "nope", prefer_flow=False)


class TestBreakDescriptor:
    def test_phi_free_fields_only(self) -> None:
        report = {
            "workflow_id": "ignored",
            "step_intent": "click Submit",
            "reason": "element not found",
            "resolver_rung": "template",
            "drift_signature": "sig123",
            "metrics": {"steps": 5, "duration_s": 12.3},
            # PHI that must never be forwarded:
            "field_values": {"ssn": "123-45-6789"},
            "report_body": "raw",
            "dom": "<html>",
            "screenshots": ["a.png"],
        }
        d = build_break_descriptor(
            report, workflow_id="wf_1", deployment_kind="byoc", org_id="org_1"
        )
        assert d["workflow_id"] == "wf_1"
        assert d["deployment_kind"] == "byoc"
        assert d["metrics"] == {"steps": 5, "duration_s": 12.3}
        for forbidden in ("field_values", "report_body", "dom", "screenshots"):
            assert forbidden not in d


class TestReportBreak:
    def _write_report(self, run_dir: Path, halt: dict) -> None:
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(json.dumps({"halt": halt}))

    def test_no_halt_returns_error(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "report.json").write_text(json.dumps({"status": "ok"}))
        result = report_break(run_dir, token="oai_ingest_x")
        assert result["ok"] is False
        assert "No halt" in result["error"]

    def test_success(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift", "step_intent": "click"})
        monkeypatch.setattr(
            "engine.hosted.httpx.post",
            lambda *a, **k: FakeResponse(202, {
                "ok": True, "run_id": "r1", "halt_id": "h1",
                "status": "halt", "teach_url": "/dashboard/runs/r1/teach",
            }),
        )
        result = report_break(run_dir, workflow_id="wf_1", token="oai_ingest_x")
        assert result["ok"] is True
        assert result["halt_id"] == "h1"
        assert result["teach_url"].endswith("/teach")

    def test_422_local_fallback(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        monkeypatch.setattr(
            "engine.hosted.httpx.post", lambda *a, **k: FakeResponse(422, {})
        )
        result = report_break(run_dir, workflow_id="wf_1", token="oai_ingest_x",
                              allow_local_fallback=True)
        assert result["ok"] is False
        assert result["local_teach"] is True

    def test_422_raises_without_fallback(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        monkeypatch.setattr(
            "engine.hosted.httpx.post", lambda *a, **k: FakeResponse(422, {})
        )
        with pytest.raises(PhiBoundaryError):
            report_break(run_dir, workflow_id="wf_1", token="oai_ingest_x",
                         allow_local_fallback=False)

    def test_not_logged_in(self, tmp_path: Path, fake_keyring) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        result = report_break(run_dir, workflow_id="wf_1")
        assert result["ok"] is False
        assert "Not logged in" in result["error"]
