"""Six real Windows campaign cases on the hosted WinForms/SQLite fixture.

The primary cases retain UIA. The existing additional pixels-only pass remains
informational. One trial per case does not request or weaken certification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_windows_hosted_replay import (
    CONTROLS, DEMO_NOTE, DEMO_SEARCH, TARGET_ID, _Host, _points,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OAFLOW_WINDOWS_HOSTED_E2E") != "1" or sys.platform != "win32",
        reason="opt-in hosted Windows campaign; set OAFLOW_WINDOWS_HOSTED_E2E=1 on Windows",
    ),
    pytest.mark.timeout(900),
]
_SESSIONS: dict[str, Any] = {}


@dataclass(frozen=True)
class _Result:
    status: int
    stdout: str
    stderr: str


class _LocalGuest:
    """Existing guest-channel API restricted to this test's owned directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.operations: list[dict[str, Any]] = []
        principal = self.run_ps(
            "[Security.Principal.WindowsIdentity]::GetCurrent().Name",
            operation="read current principal",
        ).stdout.strip()
        if not principal or len(principal) > 320 or "\n" in principal:
            raise RuntimeError("current principal metadata unavailable")
        self.user = principal
        self.principal_sha256 = hashlib.sha256(principal.encode()).hexdigest()

    def run_ps(self, script: str, *, check: bool = True,
               timeout_s: float = 60.0, operation: str = "hosted metadata") -> _Result:
        assert 0 < timeout_s <= 60 and len(script) <= 65536
        encoded = base64.b64encode(
            ("$ErrorActionPreference = 'Stop'\n" + script).encode("utf-16-le")
        ).decode("ascii")
        try:
            done = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired:
            self.operations.append({"operation": operation, "status": "TIMEOUT"})
            raise RuntimeError("owned PowerShell operation timed out") from None
        self.operations.append({"operation": operation, "status": done.returncode})
        result = _Result(done.returncode, done.stdout, done.stderr)
        if check and result.status:
            # Do not include scripts, principal, commands or raw outputs in errors.
            raise RuntimeError(f"{operation} failed with status {result.status}")
        return result

    def push_file(self, local: Path, remote: str) -> None:
        from openadapt_flow.qualify import qualification_pixel_fault_task

        source = local.resolve(strict=True)
        destination = Path(remote)
        assert destination.parent.resolve(strict=True) == self.root
        assert not destination.exists() and not destination.is_symlink()
        helper = Path(qualification_pixel_fault_task.__file__).with_name(
            "qualification_pixel_fault_overlay.ps1"
        ).resolve(strict=True)
        assert source == helper or source.name == "request.json"
        payload = source.read_bytes()
        if source == helper:
            assert destination.name.startswith("qualification_pixel_fault_overlay_")
            assert destination.suffix == ".ps1" and len(payload) <= 131072
        else:
            assert source.name == "request.json" and len(payload) <= 4096
            request = json.loads(payload)
            assert request["schema"] == "openadapt.qualification-pixel-fault/v1"
            assert destination.name == f"qualification_pixel_fault_{request['nonce']}.json"
        with destination.open("xb") as stream:
            stream.write(payload)


class _CampaignHost:
    def __init__(self, host: _Host, backend: Any, guest: _LocalGuest) -> None:
        self.host, self.backend, self.guest = host, backend, guest
        self.sessions: list[dict[str, Any]] = []
        self.oracles: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.host.launch_app()
        _points(self.backend)
        assert all(row["note"] == "" for row in self.host.rows())

    def open(self):
        from openadapt_flow.qualify.windows_pixel_fault_controller import (
            attach_windows_qualification_pixel_fault_controller,
        )

        _points(self.backend)
        controller = attach_windows_qualification_pixel_fault_controller(
            self.backend, self.guest, guest_dir=str(self.guest.root)
        )
        evidence: dict[str, Any] = {"closed": False, "tasks": []}
        self.sessions.append(evidence)
        closed = False

        def close() -> None:
            nonlocal closed
            assert not closed, "session close must not be retried"
            closed = True
            handles = tuple(controller._pixel_tasks._tasks)
            evidence["tasks"] = [{
                "nonce": h.nonce,
                "ready": h.ready_status == f"ready:{h.nonce}:{controller.session_identity_sha256}",
                "request_sha256": h.request_sha256,
                "session_identity_sha256": h.session_binding_sha256,
            } for h in handles]
            try:
                controller.close()
                for handle in handles:
                    # Only names from this controller's actual creation receipts.
                    name = handle.task.task_name.replace("'", "''")
                    self.guest.run_ps(
                        f"if (Get-ScheduledTask -TaskName '{name}' "
                        "-ErrorAction SilentlyContinue) { throw 'owned task remains' }",
                        operation="verify owned task disposal",
                    )
                    assert all(not Path(path).exists() for path in (
                        handle.helper_path, handle.request_path, handle.status_path,
                    ))
                evidence["closed"] = True
            finally:
                if getattr(self.backend, "qualification_pixel_fault_controller", None) is controller:
                    delattr(self.backend, "qualification_pixel_fault_controller")

        return self.backend, close

    def before_trial(self, context: Any) -> None:
        context.state["sqlite_before"] = self.host.rows()
        assert all(row["note"] == "" for row in context.state["sqlite_before"])

    def oracle(self, context: Any, report: Any) -> dict[str, Any]:
        from openadapt_flow.qualification_faults import fault_detector_contract_error

        before = {row["id"]: row for row in context.state["sqlite_before"]}
        after = {row["id"]: row for row in self.host.rows()}
        expected = {key: dict(row) for key, row in before.items()}
        expected[TARGET_ID]["note"] = DEMO_NOTE
        exact, untouched = after == expected, after == before
        passed = exact if context.oracle_kind == "exact_record" else untouched
        result = {
            "schema_version": "openadapt.qualify-readback/v1",
            "oracle_kind": context.oracle_kind,
            "read_path": "independent SQLite CLI, complete row comparison",
            "independent": True, "passed": passed,
            "observed": ("exact_record" if exact else "record_mismatch")
            if context.oracle_kind == "exact_record"
            else ("no_mutation" if untouched else "unexpected_mutation"),
            "effect_verdict": "CONFIRMED" if exact else "REFUTED",
            "landed": "exact" if exact else ("none" if untouched else "some"),
            "silent_incorrect_success": bool(report.success and not exact),
            "changed_ids": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)),
            "records_before": len(before), "records_after": len(after),
        }
        mutations = report.qualification_fault_mutations
        result["fault_receipt_count"] = len(mutations)
        result["fault_detector_error"] = (
            fault_detector_contract_error(report, mutations[0])
            if len(mutations) == 1 else None
        )
        result["execution_outcome"] = report.execution_outcome
        result["model_calls"] = report.model_calls
        self.oracles.append({
            "case_id": context.case_id, "trial": context.trial,
            "informational": bool(context.state.get("pixels_only")), **result,
        })
        return result


def open_hosted_session(config):
    """Actual app.session hook, registered only during this owned test."""
    return _SESSIONS[config.app.name].open()


def test_windows_hosted_six_case_campaign(tmp_path) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo, structural_armed_coverage,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.qualify.campaign import QualifyHooks, qualify
    from openadapt_flow.qualify.config import CASE_IDS, parse_verify_config

    root = tmp_path / "hosted-campaign"
    root.mkdir()
    guest_root = root / "guest"
    guest_root.mkdir()
    host = _Host(root)
    owner = None
    name = f"hosted-campaign-{uuid.uuid4().hex}"
    evidence: dict[str, Any] = {
        "primary_planned": 6, "informational_planned": 6,
        "certification_requested": False, "status": "UNRUN",
    }
    try:
        try:
            backend = host.start_agent()
        except Exception:
            raise AssertionError("hosted agent startup failed") from None
        assert {"typed_input_v1", "uia_v1"}.issubset(backend.agent_capabilities())
        assert "legacy_exec" not in backend.agent_capabilities()
        host.launch_app()
        points = _points(backend)
        observed = backend.qualification_environment_identity()
        assert observed is not None
        executable = shutil.which("powershell.exe")
        assert executable is not None
        assert observed[0] == "powershell"
        assert observed[1] == "sha256:" + hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        evidence["environment"] = {
            "application_identity": observed[0], "application_version": observed[1],
            "session_sha256": observed[2], "environment_sha256": observed[3],
        }
        before_demo = {row["id"]: row for row in host.rows()}

        def driver(rec) -> None:
            rec.click(*points["searchBox"])
            rec.type_text(DEMO_SEARCH, param="search")
            rec.press("Enter")
            time.sleep(0.4)
            rec.click(*points["searchButton"])
            time.sleep(0.4)
            rec.click(*points["noteBox"])
            rec.type_text(DEMO_NOTE, param="note")
            rec.click(*points["saveButton"])
            time.sleep(0.4)

        recording = record_desktop_demo(backend, root / "recording", driver)
        expected = {key: dict(row) for key, row in before_demo.items()}
        expected[TARGET_ID]["note"] = DEMO_NOTE
        assert {row["id"]: row for row in host.rows()} == expected
        bundle = root / "bundle"
        workflow = compile_recording(recording, bundle, name="hosted-campaign")
        coverage = structural_armed_coverage(workflow)
        assert coverage["armed_coverage"] == 1.0
        assert coverage["click_steps"] == len(CONTROLS)
        saves = [step for step in workflow.steps if step.anchor is not None
                 and step.anchor.structural is not None
                 and step.anchor.structural.automation_id == "saveButton"]
        assert len(saves) == 1 and saves[0].identity_armed
        guest = _LocalGuest(guest_root)
        owner = _CampaignHost(host, backend, guest)
        _SESSIONS[name] = owner
        config = parse_verify_config({
            "schema_version": "openadapt.verify/v1",
            "app": {
                "name": name, "target": "windows", "surface": "local",
                "application_identity": "powershell", "version": observed[1],
                "session": f"{__name__}:open_hosted_session",
                "required_capabilities": ["pixel_frame_capture", "pixel_input", "uia_v1"],
            },
            "minimum_readback": "system_of_record", "save_step": saves[0].id,
            "verifier": {
                "kind": "sql", "sqlite_database": str(root / "patients.db"),
                "sql_query": "SELECT id, first, last, dob, note FROM patients ORDER BY id",
            },
            "record": {
                "name": "patient note", "match": ["id"], "duplicate_key": "id",
                "fields": {"id": {"literal": str(TARGET_ID)}, "note": "note"},
            },
            "inputs": {"params": {"search": DEMO_SEARCH, "note": DEMO_NOTE}},
            "campaign": {"trials": 1, "cases": list(CASE_IDS)},
        }, env={})
        owner.reset()
        evidence["status"] = "RUNNING"
        result = qualify(
            bundle, config, out=root / "campaign", trials=1, certify=None,
            hooks=QualifyHooks(reset=owner.reset, before_trial=owner.before_trial, oracle=owner.oracle),
            log=lambda _message: None,
        )
        evidence.update(status="RETURNED", qualification_passed=result.passed, certified=result.certified)
        evidence["refusal_codes"] = [item.code.value for item in result.report.refusals]
        assert not result.certified
        assert {row["case_id"] for row in result.rows} == set(CASE_IDS)
        assert len(result.rows) == 6
        assert all(row["model_calls"] == 0 for row in result.rows)
        assert all(not row["fault_declined"] for row in result.rows)
        assert result.reports["representative"][0].rung_counts.get("structural", 0) >= len(CONTROLS)
        for case_id in CASE_IDS[1:]:
            report = result.reports[case_id][0]
            assert report.execution_outcome == "HALTED"
            assert len(report.qualification_fault_mutations) == 1
        primary = [item for item in owner.oracles if not item["informational"]]
        assert len(primary) == 6 and all(item["passed"] for item in primary)
        assert all(item["fault_detector_error"] is None for item in primary)
        assert all(session["closed"] for session in owner.sessions)
        assert result.passed, "one-trial campaign did not qualify; gates remain unchanged"
        evidence["status"] = "PASSED"
    except BaseException as exc:
        evidence.update(status="FAILED", exception_type=type(exc).__name__)
        raise
    finally:
        _SESSIONS.pop(name, None)
        try:
            host.close()
        finally:
            if owner is not None:
                evidence.update(oracles=owner.oracles, sessions=owner.sessions,
                                operations=owner.guest.operations,
                                principal_sha256=owner.guest.principal_sha256)
            evidence["primary_completed"] = sum(
                not item["informational"] for item in evidence.get("oracles", [])
            )
            evidence["informational_completed"] = sum(
                item["informational"] for item in evidence.get("oracles", [])
            )
            (root / "campaign-evidence.json").write_text(json.dumps(evidence, indent=2))
