"""Tests for the hosted push + report_break egress verbs."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from engine import hosted
from engine.backends.protocol import UploadResult
from engine.flow_bridge import FlowResult
from engine.hosted import PhiBoundaryError, report_break, zip_dir

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_WORKFLOW_ID = "123e4567-e89b-12d3-a456-426614174000"
_INGEST_ID = "223e4567-e89b-42d3-a456-426614174000"
_ORG_ID = "323e4567-e89b-42d3-a456-426614174000"
_BUNDLE_VERSION_ID = "423e4567-e89b-42d3-a456-426614174000"
_RUNTIME_VALIDATION_ID = "523e4567-e89b-42d3-a456-426614174000"


def _push_document(
    *, status: str = "accepted_for_ingest", kind: str = "recording"
) -> dict:
    document = {
        "schema": "openadapt.push-result/v1",
        "status": status,
        "workflow_id": None,
        "artifact_ingest_id": None,
        "review": None,
        "attestation": None,
        "binding": {
            "kind": kind,
            "source_tree_sha256": _SHA_A,
            "derivative_tree_sha256": _SHA_B,
            "approved_archive_sha256": None,
            "artifact_sha256": None,
            "bundle_sha256": None,
            "source_recording_sha256": None,
            "sanitization_policy": "outbound-phi-v1",
            "certification_policy": None,
            "certification_evidence_sha256": None,
            "governed_authorization_template_sha256": None,
            "parameter_schema_sha256": None,
            "attested_run_report_sha256": None,
            "resolves_run_id": None,
            "organization_id": None,
            "bundle_version_id": None,
            "bundle_version": None,
            "runtime_validation_id": None,
        },
        "next_action": None,
        "dashboard_url": None,
        "delivery": {"attempted": False, "certainty": "not_attempted"},
        "error": None,
    }
    if status == "paused_for_review":
        document["review"] = {
            "id": _SHA_C,
            "scope": "local_non_authoritative",
            "sanitized_path": "/safe/derivative",
            "command": "openadapt-flow review-sanitized /safe/derivative",
        }
        document["next_action"] = "review_local"
    elif status == "accepted_for_ingest":
        document["artifact_ingest_id"] = _INGEST_ID
        document["review"] = {
            "id": _SHA_C,
            "scope": "local_non_authoritative",
            "sanitized_path": None,
            "command": None,
        }
        document["binding"]["approved_archive_sha256"] = _SHA_D
        document["binding"]["artifact_sha256"] = _SHA_D
        document["delivery"] = {"attempted": True, "certainty": "accepted"}
        if kind == "recording":
            document["next_action"] = "parameterize"
        else:
            document["workflow_id"] = _WORKFLOW_ID
            document["attestation"] = {
                "id": "challenge-1",
                "schema": "openadapt.runtime-validation/v3",
            }
            document["binding"].update(
                {
                    "bundle_sha256": _SHA_D,
                    "source_recording_sha256": _SHA_A,
                    "certification_policy": "regulated",
                    "certification_evidence_sha256": _SHA_B,
                    "parameter_schema_sha256": _SHA_C,
                    "attested_run_report_sha256": _SHA_D,
                    "organization_id": _ORG_ID,
                    "bundle_version_id": _BUNDLE_VERSION_ID,
                    "bundle_version": 3,
                    "runtime_validation_id": _RUNTIME_VALIDATION_ID,
                }
            )
            document["next_action"] = "open_dashboard"
            document["dashboard_url"] = (
                f"https://app.openadapt.ai/dashboard/workflows/{_WORKFLOW_ID}"
            )
    elif status == "failed":
        document["binding"]["kind"] = None
        document["binding"]["source_tree_sha256"] = None
        document["binding"]["derivative_tree_sha256"] = None
        document["binding"]["sanitization_policy"] = None
        document["delivery"] = {"attempted": None, "certainty": "not_accepted"}
        document["error"] = {
            "code": "push_failed",
            "message": "The artifact was not accepted for ingest.",
        }
    return document


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

    def test_refuses_symlink_members(self, tmp_path: Path) -> None:
        src = tmp_path / "rec"
        outside = tmp_path / "outside"
        src.mkdir()
        outside.write_bytes(b"raw-secret")
        (src / "escape").symlink_to(outside)

        with pytest.raises(ValueError, match="symlink"):
            zip_dir(src)


class TestPush:
    def test_push_success_persists_workflow_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from engine.db import IndexDB

        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")

        db = IndexDB(tmp_path / "index.db")
        db.initialize()
        db.insert_bundle("bnd1", str(rec), capture_id="rec1")

        monkeypatch.setattr(
            hosted,
            "_push_via_flow",
            lambda *args, **kwargs: {
                "success": True,
                "workflow_id": "wf_1",
                "dashboard_url": "https://app/dashboard/workflows/wf_1",
                "error": "",
            },
        )
        result = hosted.push(
            rec,
            kind="recording",
            host="https://app",
            db=db,
            bundle_id="bnd1",
        )
        assert result["success"] is True
        assert result["workflow_id"] == "wf_1"
        assert db.get_bundle("bnd1")["workflow_id"] == "wf_1"
        db.close()

    def test_push_default_latest_recording(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recordings = tmp_path / "recordings"
        (recordings / "old").mkdir(parents=True)
        (recordings / "new").mkdir()
        (recordings / "new" / "meta.json").write_text("{}")
        # Make "new" the most recent.
        import os
        import time

        os.utime(recordings / "new", (time.time() + 10, time.time() + 10))

        selected: dict[str, Path] = {}

        def push_via_flow(path: Path, **_kwargs):
            selected["path"] = path
            return {
                "success": True,
                "workflow_id": "wf_x",
                "dashboard_url": "",
                "error": "",
            }

        monkeypatch.setattr(hosted, "_push_via_flow", push_via_flow)
        result = hosted.push(None, recordings_dir=recordings, host="https://app")
        assert result["success"] is True
        assert selected["path"] == recordings / "new"

    def test_push_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            hosted.push(tmp_path / "nope")

    def test_direct_backend_bypass_fails_without_upload(self, tmp_path: Path) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        backend = _StubBackend(UploadResult(success=True))

        result = hosted.push(rec, backend=backend, prefer_flow=False)

        assert result["success"] is False
        assert "Direct Desktop ingest is disabled" in result["error"]
        assert backend.uploaded is None

    def test_missing_flow_push_never_falls_back_to_direct_ingest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        monkeypatch.setattr(
            hosted,
            "_push_via_flow",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("flow")),
        )

        result = hosted.push(rec)

        assert result["success"] is False
        assert result["workflow_id"] is None
        assert result["delivery_uncertain"] is True
        assert "Do not retry blindly" in result["error"]

    def test_push_exception_detail_is_not_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        secret = "/captures/Jane-Doe-12345/raw.sqlite"
        warnings: list[tuple[tuple, dict]] = []
        monkeypatch.setattr(
            hosted,
            "_push_via_flow",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
        )
        monkeypatch.setattr(
            "engine.hosted.logger.warning",
            lambda *args, **kwargs: warnings.append((args, kwargs)),
        )

        result = hosted.push(rec)

        assert result["delivery_uncertain"] is True
        assert secret not in repr(warnings)

    def test_flow_review_pause_is_not_reported_as_upload_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        document = _push_document(status="paused_for_review")
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *args, **kwargs: FlowResult(
                ok=True, returncode=0, stdout=json.dumps(document)
            ),
        )

        result = hosted.push(rec)

        assert result["success"] is False
        assert result["pending_review"] is True
        assert result["sanitized_path"] == "/safe/derivative"
        assert result["workflow_id"] is None

    def test_desktop_credential_reaches_flow_only_through_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        calls: list[dict] = []
        monkeypatch.setattr(
            hosted,
            "token_for_host",
            lambda host, explicit=None: explicit or "stored-secret",
        )

        def fake_push(*_args, **kwargs):
            calls.append(kwargs)
            return FlowResult(
                ok=True,
                returncode=0,
                stdout=json.dumps(_push_document()),
            )

        monkeypatch.setattr("engine.hosted.FlowBridge.push", fake_push)

        result = hosted.push(rec)

        assert result["success"] is True
        assert calls[0]["token"] is None
        assert calls[0]["env_overrides"] == {
            "OPENADAPT_INGEST_TOKEN": "stored-secret"
        }

    def test_credential_for_another_host_is_not_forwarded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        calls: list[dict] = []
        monkeypatch.setattr(
            hosted,
            "token_for_host",
            lambda host, explicit=None: explicit or "",
        )
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *_args, **kwargs: (
                calls.append(kwargs)
                or FlowResult(
                    ok=False,
                    returncode=1,
                    stdout=json.dumps(_push_document(status="failed")),
                )
            ),
        )

        hosted.push(rec, host="https://app.openadapt.ai")

        assert calls[0]["env_overrides"] is None

    def test_flow_success_requires_and_parses_hosted_workflow_identity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        workflow_id = _WORKFLOW_ID
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *args, **kwargs: FlowResult(
                ok=True,
                returncode=0,
                stdout=json.dumps(_push_document(kind="bundle")),
            ),
        )

        result = hosted.push(rec, kind="bundle")

        assert result["success"] is True
        assert result["workflow_id"] == workflow_id
        assert result["dashboard_url"].endswith(workflow_id)

    def test_flow_exit_zero_without_identity_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *args, **kwargs: FlowResult(
                ok=True, returncode=0, stdout=json.dumps(_push_document(status="failed"))
            ),
        )

        result = hosted.push(rec)

        assert result["success"] is False
        assert result["delivery_uncertain"] is True
        assert result["error_code"] == "invalid_ingest_response"

    def test_flow_none_workflow_identity_is_not_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        malformed = _push_document(kind="bundle")
        malformed["workflow_id"] = None
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *args, **kwargs: FlowResult(
                ok=True,
                returncode=0,
                stdout=json.dumps(malformed),
            ),
        )

        result = hosted.push(rec, kind="bundle")

        assert result["success"] is False
        assert result["workflow_id"] is None

    def test_flow_failure_uses_bounded_stdout_and_marks_delivery_uncertain(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rec = tmp_path / "rec"
        rec.mkdir()
        document = _push_document(status="failed")
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.push",
            lambda *args, **kwargs: FlowResult(
                ok=False,
                returncode=1,
                stdout=json.dumps(document),
                stderr="",
            ),
        )

        result = hosted.push(rec, token="secret-value")

        assert result["success"] is False
        assert result["delivery_uncertain"] is False
        assert result["error"] == "The artifact was not accepted for ingest."


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
        calls: list[dict] = []
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.report_break",
            lambda *a, **k: (
                calls.append(k)
                or FlowResult(
                    ok=True,
                    returncode=0,
                    stdout=(
                        "Break reported (run_id=r1, halt_id=h1, status=halt).\n"
                        "Teach: https://app.openadapt.ai/dashboard/runs/r1/teach\n"
                    ),
                )
            ),
        )
        result = report_break(run_dir, workflow_id="wf_1", token="oai_ingest_x")
        assert result["ok"] is True
        assert result["halt_id"] == "h1"
        assert result["teach_url"].endswith("/teach")
        assert calls[0]["env_overrides"] == {
            "OPENADAPT_INGEST_TOKEN": "oai_ingest_x"
        }

    def test_422_local_fallback(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.report_break",
            lambda *a, **k: FlowResult(
                ok=True,
                returncode=0,
                stdout="Break kept LOCAL-ONLY: server rejected PHI boundary\n",
            ),
        )
        result = report_break(
            run_dir, workflow_id="wf_1", token="oai_ingest_x", allow_local_fallback=True
        )
        assert result["ok"] is False
        assert result["local_teach"] is True

    def test_422_raises_without_fallback(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.report_break",
            lambda *a, **k: FlowResult(
                ok=True,
                returncode=0,
                stdout="Break kept LOCAL-ONLY: server rejected PHI boundary\n",
            ),
        )
        with pytest.raises(PhiBoundaryError):
            report_break(
                run_dir, workflow_id="wf_1", token="oai_ingest_x", allow_local_fallback=False
            )

    def test_not_logged_in(self, tmp_path: Path, fake_keyring) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        result = report_break(run_dir, workflow_id="wf_1")
        assert result["ok"] is False
        assert "Not logged in" in result["error"]

    def test_report_exception_detail_is_not_logged(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        self._write_report(run_dir, {"reason": "drift"})
        secret = "/runs/Jane-Doe-12345/report.json"
        warnings: list[tuple[tuple, dict]] = []
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.report_break",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
        )
        monkeypatch.setattr(
            "engine.hosted.logger.warning",
            lambda *args, **kwargs: warnings.append((args, kwargs)),
        )

        result = report_break(run_dir, workflow_id="wf_1", token="oai_ingest_x")

        assert result["delivery_uncertain"] is True
        assert secret not in repr(warnings)

    def test_free_text_is_not_sent_by_desktop(self, tmp_path: Path, monkeypatch) -> None:
        run_dir = tmp_path / "run"
        secret = "patient Jane Doe has record 12345"
        self._write_report(run_dir, {"reason": secret, "step_intent": secret})
        calls: list[tuple[tuple, dict]] = []
        monkeypatch.setattr(
            "engine.hosted.FlowBridge.report_break",
            lambda *args, **kwargs: (
                calls.append((args, kwargs))
                or FlowResult(
                    ok=True,
                    returncode=0,
                    stdout="Break reported (run_id=r1, halt_id=h1, status=halt).\n",
                )
            ),
        )

        result = report_break(run_dir, workflow_id="wf_1", token="token")

        assert result["ok"] is True
        assert secret not in repr(calls)
