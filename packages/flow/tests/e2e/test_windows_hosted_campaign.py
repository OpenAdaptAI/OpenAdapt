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
    CONTROLS,
    DEMO_NOTE,
    DEMO_SEARCH,
    TARGET_ID,
    _Host,
    _points,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OAFLOW_WINDOWS_HOSTED_E2E") != "1" or sys.platform != "win32",
        reason="opt-in hosted Windows campaign; set OAFLOW_WINDOWS_HOSTED_E2E=1 on Windows",
    ),
    pytest.mark.timeout(900),
]
_SESSIONS: dict[str, Any] = {}
_SAFE_TASK_PHASES = {
    "preflight",
    "build_action",
    "build_principal",
    "build_settings",
    "register_definition",
    "verify_definition_count",
    "verify_description",
    "verify_action_count",
    "verify_action_executable",
    "verify_action_arguments",
    "verify_principal_user",
    "verify_principal_logon",
    "verify_principal_runlevel",
    "start_instance",
    "observe_instance",
    "readiness_poll",
    "transport_result",
    "unknown",
}
_SAFE_TASK_REGISTRATION = {
    "not_attempted",
    "preexisting",
    "unknown",
    "absent",
    "ambiguous",
    "mismatch",
    "registered_unverified",
    "verified_owned",
    "started_owned",
    "owned_handle",
}
_SAFE_TASK_CLEANUP = {
    "not_attempted",
    "not_needed",
    "unknown",
    "removed_exact_owned_definition",
    "left_running_instance_unproven",
    "left_ambiguous_instances",
    "left_mismatched_definition",
    "failed_or_unknown",
}
_SAFE_CONTROLLER_DECLINES = {
    "session_binding_unavailable",
    "session_identity_changed",
    "fault_request_unavailable",
    "task_creation_failed",
    "task_readiness_failed",
    "task_readiness_unconfirmed",
}


def _safe_task_diagnostic(value: Any) -> dict[str, Any] | None:
    phase = value.get("phase") if isinstance(value, dict) else None
    registration = value.get("registration") if isinstance(value, dict) else None
    cleanup = value.get("cleanup") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"phase", "registration", "cleanup", "exit_status"}
        or not isinstance(phase, str)
        or phase not in _SAFE_TASK_PHASES
        or not isinstance(registration, str)
        or registration not in _SAFE_TASK_REGISTRATION
        or not isinstance(cleanup, str)
        or cleanup not in _SAFE_TASK_CLEANUP
        or (
            value.get("exit_status") is not None
            and type(value["exit_status"]) is not int
        )
    ):
        return None
    return {
        "phase": value["phase"],
        "registration": value["registration"],
        "cleanup": value["cleanup"],
        "exit_status": value["exit_status"],
    }


def _safe_environment_identity(value: Any) -> dict[str, str] | None:
    """Keep only the four bounded fields in an atomic live identity tuple."""

    if (
        not isinstance(value, tuple)
        or len(value) != 4
        or any(not isinstance(item, str) or not item for item in value)
    ):
        return None
    application, version, session_sha256, environment_sha256 = value
    return {
        "application_identity": application,
        "application_version": version,
        "session_sha256": session_sha256,
        "environment_sha256": environment_sha256,
    }


def _normalized_identity(text: str) -> str:
    return "".join(character.casefold() for character in text if character.isalnum())


def _live_patient_header(
    backend: Any, identity_parts: tuple[str, ...]
) -> tuple[int, int, int, int] | None:
    """Return the live UIA label region only when its pixels show the target."""
    from openadapt_flow.ir import StructuralLocator
    from openadapt_flow.vision.ocr import ocr

    handle = backend.locate_structural(StructuralLocator(automation_id="patientHeader"))
    region = handle.region if handle is not None else None
    if region is None:
        return None
    x, y, width, height = region
    viewport_width, viewport_height = backend.viewport
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > viewport_width
        or y + height > viewport_height
    ):
        return None
    observed = " ".join(line.text for line in ocr(backend.screenshot(), region=region))
    normalized = _normalized_identity(observed)
    if not all(_normalized_identity(part) in normalized for part in identity_parts):
        return None
    return region


def _prime_patient_context(host: _Host, backend: Any):
    """Select the real target through the app, then clear only its query text."""
    rows_before = host.rows()
    target = next((row for row in rows_before if row["id"] == TARGET_ID), None)
    if target is None:
        raise AssertionError("fixture target record is unavailable")
    identity_parts = tuple(
        str(target[field]) for field in ("first", "last", "dob") if target[field]
    )
    if len(identity_parts) != 3 or any(row["note"] for row in rows_before):
        raise AssertionError(
            "fixture target identity or blank-note precondition failed"
        )

    points = _points(backend)
    backend.click(*points["searchBox"])
    backend.type_text(DEMO_SEARCH)
    backend.press("Enter")
    deadline = time.monotonic() + 30
    region = None
    while time.monotonic() < deadline:
        region = _live_patient_header(backend, identity_parts)
        if region is not None:
            break
        time.sleep(0.1)
    if region is None:
        raise AssertionError("live patient identity header was not verified")

    # The selected record and its visible identity remain in place while the
    # query is cleared, so the recorded search still starts with that identity
    # visibly present before the first recorded action.
    backend.click(*points["searchBox"])
    backend.press("ControlOrMeta+a")
    backend.press("Backspace")
    if _live_patient_header(backend, identity_parts) != region:
        raise AssertionError("patient identity header changed while clearing query")
    if host.rows() != rows_before:
        raise AssertionError("read-only patient selection changed the fixture records")
    return region, identity_parts, points


def _assert_recorded_patient_identity(
    recording: Path,
    identifier_region: tuple[int, int, int, int],
    identity_parts: tuple[str, ...],
) -> None:
    """Check the marked region in each recorded click's exact before-frame."""
    from openadapt_flow import sealed_set
    from openadapt_flow.vision.ocr import ocr

    events = [
        json.loads(line)
        for line in sealed_set.read_text(recording / "events.jsonl").splitlines()
        if line.strip()
    ]
    clicks = [event for event in events if event.get("kind") == "click"]
    if len(clicks) != len(CONTROLS):
        raise AssertionError("recorded click denominator changed")
    for event in clicks:
        frame = sealed_set.read_bytes(
            recording / "frames" / f"{int(event['i']):04d}_before.png"
        )
        observed = " ".join(line.text for line in ocr(frame, region=identifier_region))
        normalized = _normalized_identity(observed)
        if not all(_normalized_identity(part) in normalized for part in identity_parts):
            raise AssertionError("recorded click before-frame lacks target identity")


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

    def run_ps(
        self,
        script: str,
        *,
        check: bool = True,
        timeout_s: float = 60.0,
        operation: str = "hosted metadata",
    ) -> _Result:
        assert 0 < timeout_s <= 60 and len(script) <= 65536
        encoded = base64.b64encode(
            ("$ErrorActionPreference = 'Stop'\n" + script).encode("utf-16-le")
        ).decode("ascii")
        try:
            done = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        helper = (
            Path(qualification_pixel_fault_task.__file__)
            .with_name("qualification_pixel_fault_overlay.ps1")
            .resolve(strict=True)
        )
        assert source == helper or source.name == "request.json"
        payload = source.read_bytes()
        if source == helper:
            assert destination.name.startswith("qualification_pixel_fault_overlay_")
            assert destination.suffix == ".ps1" and len(payload) <= 131072
        else:
            assert source.name == "request.json" and len(payload) <= 4096
            request = json.loads(payload)
            assert request["schema"] == "openadapt.qualification-pixel-fault/v1"
            assert (
                destination.name == f"qualification_pixel_fault_{request['nonce']}.json"
            )
        with destination.open("xb") as stream:
            stream.write(payload)


class _CampaignHost:
    def __init__(
        self,
        host: _Host,
        backend: Any,
        guest: _LocalGuest,
        expected_identity: tuple[str, str, str, str],
        identifier_region: tuple[int, int, int, int],
        patient_identity_parts: tuple[str, ...],
    ) -> None:
        self.host, self.prototype, self.guest = host, backend, guest
        self.expected_identity = expected_identity
        self.identifier_region = identifier_region
        self.patient_identity_parts = patient_identity_parts
        self._backends: list[Any] = []
        self._controllers: list[Any] = []
        self.sessions: list[dict[str, Any]] = []
        self.oracles: list[dict[str, Any]] = []

    def _restore_pinned_app_foreground(self, backend: Any) -> None:
        """Foreground only the uniquely resolved window owned by this app PID."""
        from openadapt_flow.backends.win32_window_client import Win32WindowClient

        app = self.host.app
        if app is None or app.poll() is not None:
            raise RuntimeError("owned hosted application process is unavailable")
        executable = shutil.which("powershell.exe")
        if executable is None:
            raise RuntimeError("pinned hosted application executable is unavailable")

        client = Win32WindowClient()
        windows = client.find_windows(owner=Path(executable).stem, title=None)
        owned = [window for window in windows if window.pid == app.pid]
        if len(owned) != 1:
            raise RuntimeError("owned hosted application window is not unique")
        window = owned[0]
        current = client.find_pinned_window(
            owner=Path(executable).stem, window_id=window.window_id
        )
        if len(current) != 1 or current[0].pid != app.pid:
            raise RuntimeError("owned hosted application window changed")
        if app.poll() is not None:
            raise RuntimeError("owned hosted application process exited")
        client.activate(app.pid)
        if app.poll() is not None:
            raise RuntimeError("owned hosted application process exited")
        current = client.find_pinned_window(
            owner=Path(executable).stem, window_id=window.window_id
        )
        if len(current) != 1 or current[0].pid != app.pid:
            raise RuntimeError("owned hosted application window changed")
        if client.key_window_id(app.pid) != window.window_id:
            raise RuntimeError("owned hosted application did not take foreground")
        if app.poll() is not None:
            raise RuntimeError("owned hosted application process exited")

        identity = backend.qualification_environment_identity()
        if identity is None or identity[:2] != self.expected_identity[:2]:
            raise RuntimeError("live foreground identity differs from pinned app")

    def reset(self) -> None:
        self.host.launch_app()
        region, identity_parts, _points_by_id = _prime_patient_context(
            self.host, self.prototype
        )
        if (
            region != self.identifier_region
            or identity_parts != self.patient_identity_parts
        ):
            raise AssertionError(
                "trial reset changed the marked patient identity region"
            )
        if any(row["note"] != "" for row in self.host.rows()):
            raise AssertionError("trial reset did not restore blank patient notes")

    def open(self):
        import requests

        from openadapt_flow.backends.windows_backend import WindowsBackend
        from openadapt_flow.qualify.windows_pixel_fault_controller import (
            attach_windows_qualification_pixel_fault_controller,
        )

        # These are this fixture's ephemeral in-memory connection settings.
        # No lab environment, token file or certificate file is read here.
        prototype = self.prototype
        if not prototype._auth_token or not prototype._pin_fingerprint:
            raise RuntimeError("owned agent connection binding unavailable")
        client = requests.Session()
        controller = None
        evidence: dict[str, Any] = {
            "opened": False,
            "closed": False,
            "tasks": [],
            "controller_bound_identity": None,
            "identity_before_open_return": None,
            "fault_declined": None,
            "task_diagnostic": None,
        }
        self.sessions.append(evidence)
        try:
            backend = WindowsBackend(
                prototype.server_url,
                auth_token=prototype._auth_token,
                pin_fingerprint=prototype._pin_fingerprint,
                require_tls=prototype._require_tls,
                type_interval_s=prototype._type_interval_s,
                timeout_s=prototype._timeout_s,
                screenshot_max_retries=prototype._screenshot_max_retries,
                screenshot_retry_delay_s=prototype._screenshot_retry_delay_s,
                session=client,
            )
            assert backend is not prototype
            assert all(backend is not prior for prior in self._backends)
            assert all(client is not prior._session for prior in self._backends)
            _points(backend)
            self._restore_pinned_app_foreground(backend)
            controller = attach_windows_qualification_pixel_fault_controller(
                backend, self.guest, guest_dir=str(self.guest.root)
            )
            if controller._environment_identity[:2] != self.expected_identity[:2]:
                raise RuntimeError("controller identity differs from pinned app")
            evidence["controller_bound_identity"] = _safe_environment_identity(
                controller._environment_identity
            )
            observe_identity = getattr(
                backend, "qualification_environment_identity", None
            )
            try:
                live_identity = (
                    observe_identity() if callable(observe_identity) else None
                )
            except Exception:  # noqa: BLE001 - diagnostics never change admission
                live_identity = None
            evidence["identity_before_open_return"] = _safe_environment_identity(
                live_identity
            )
            assert all(controller is not prior for prior in self._controllers)
            self._backends.append(backend)
            self._controllers.append(controller)
            evidence["opened"] = True
        except BaseException:
            try:
                if controller is not None:
                    controller.close()
                evidence["closed"] = True
            finally:
                client.close()
                evidence["http_closed"] = True
            raise
        closed = False

        def close() -> None:
            nonlocal closed
            assert not closed, "session close must not be retried"
            closed = True
            handles = tuple(controller._pixel_tasks._tasks)
            evidence["tasks"] = [
                {
                    "nonce": h.nonce,
                    "ready": h.ready_status
                    == f"ready:{h.nonce}:{controller.session_identity_sha256}",
                    "request_sha256": h.request_sha256,
                    "session_identity_sha256": h.session_binding_sha256,
                }
                for h in handles
            ]
            controller_decline = controller.last_decline_code
            evidence["fault_declined"] = (
                controller_decline
                if isinstance(controller_decline, str)
                and controller_decline in _SAFE_CONTROLLER_DECLINES
                else None
            )
            evidence["task_diagnostic"] = _safe_task_diagnostic(
                controller.last_task_diagnostic
            )
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
                    assert all(
                        not Path(path).exists()
                        for path in (
                            handle.helper_path,
                            handle.request_path,
                            handle.status_path,
                        )
                    )
                evidence["closed"] = True
            finally:
                client.close()
                evidence["http_closed"] = True

        return backend, close

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
            "independent": True,
            "passed": passed,
            "observed": ("exact_record" if exact else "record_mismatch")
            if context.oracle_kind == "exact_record"
            else ("no_mutation" if untouched else "unexpected_mutation"),
            "effect_verdict": "CONFIRMED" if exact else "REFUTED",
            "landed": "exact" if exact else ("none" if untouched else "some"),
            "silent_incorrect_success": bool(report.success and not exact),
            "changed_ids": sorted(
                k for k in set(before) | set(after) if before.get(k) != after.get(k)
            ),
            "records_before": len(before),
            "records_after": len(after),
        }
        mutations = report.qualification_fault_mutations
        result["fault_receipt_count"] = len(mutations)
        result["fault_detector_error"] = (
            fault_detector_contract_error(report, mutations[0])
            if len(mutations) == 1
            else None
        )
        result["execution_outcome"] = report.execution_outcome
        result["model_calls"] = report.model_calls
        self.oracles.append(
            {
                "case_id": context.case_id,
                "trial": context.trial,
                "informational": bool(context.state.get("pixels_only")),
                **result,
            }
        )
        return result


def open_hosted_session(config):
    """Actual app.session hook, registered only during this owned test."""
    return _SESSIONS[config.app.name].open()


def test_windows_hosted_six_case_campaign(tmp_path) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.qualify.campaign import QualifyHooks, qualify
    from openadapt_flow.qualify.config import CASE_IDS, parse_verify_config
    from tests.e2e.hosted_policy_observer import observe_policy

    root = tmp_path / "hosted-campaign"
    root.mkdir()
    guest_root = root / "guest"
    guest_root.mkdir()
    host = _Host(root)
    owner = None
    backend = None
    name = f"hosted-campaign-{uuid.uuid4().hex}"
    capture_telemetry = root / "agent-capture-telemetry.json"
    evidence: dict[str, Any] = {
        "primary_planned": 6,
        "informational_planned": 6,
        "certification_requested": False,
        "status": "UNRUN",
    }
    try:
        try:
            backend = host.start_agent(capture_telemetry_sidecar=capture_telemetry)
        except Exception:
            raise AssertionError("hosted agent startup failed") from None
        assert {"typed_input_v1", "uia_v1"}.issubset(backend.agent_capabilities())
        assert "legacy_exec" not in backend.agent_capabilities()
        host.launch_app()
        identifier_region, patient_identity_parts, points = _prime_patient_context(
            host, backend
        )
        observed = backend.qualification_environment_identity()
        assert observed is not None
        executable = shutil.which("powershell.exe")
        assert executable is not None
        assert observed[0] == "powershell"
        assert (
            observed[1]
            == "sha256:" + hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        )
        evidence["environment"] = {
            "application_identity": observed[0],
            "application_version": observed[1],
            "session_sha256": observed[2],
            "environment_sha256": observed[3],
        }
        evidence["initial_pinned_expected"] = {
            "application_identity": observed[0],
            "application_version": observed[1],
        }
        before_demo = {row["id"]: row for row in host.rows()}
        assert all(row["note"] == "" for row in before_demo.values())

        def driver(rec) -> None:
            rec.click(*points["searchBox"])
            rec.type_text(DEMO_SEARCH, param="search")
            time.sleep(0.4)
            rec.click(*points["searchButton"])
            time.sleep(0.4)
            rec.click(*points["noteBox"])
            rec.type_text(DEMO_NOTE, param="note")
            rec.click(*points["saveButton"])
            time.sleep(0.4)

        recording = record_desktop_demo(
            backend,
            root / "recording",
            driver,
            identifier_region=identifier_region,
        )
        _assert_recorded_patient_identity(
            recording, identifier_region, patient_identity_parts
        )
        expected = {key: dict(row) for key, row in before_demo.items()}
        expected[TARGET_ID]["note"] = DEMO_NOTE
        assert {row["id"]: row for row in host.rows()} == expected
        bundle = root / "bundle"
        workflow = compile_recording(recording, bundle, name="hosted-campaign")
        coverage = structural_armed_coverage(workflow)
        assert coverage["armed_coverage"] == 1.0
        assert coverage["click_steps"] == len(CONTROLS)
        click_steps = [
            step
            for step in workflow.steps
            if step.anchor is not None and step.anchor.structural is not None
        ]
        assert len(click_steps) == len(CONTROLS)
        assert all(step.identity_armed for step in click_steps)
        assert all(step.anchor.identifier_crop for step in click_steps)
        saves = [
            step
            for step in workflow.steps
            if step.anchor is not None
            and step.anchor.structural is not None
            and step.anchor.structural.automation_id == "saveButton"
        ]
        assert len(saves) == 1 and saves[0].identity_armed
        guest = _LocalGuest(guest_root)
        owner = _CampaignHost(
            host,
            backend,
            guest,
            observed,
            identifier_region,
            patient_identity_parts,
        )
        _SESSIONS[name] = owner
        config = parse_verify_config(
            {
                "schema_version": "openadapt.verify/v1",
                "app": {
                    "name": name,
                    "target": "windows",
                    "surface": "local",
                    "application_identity": "powershell",
                    "version": observed[1],
                    "session": f"{__name__}:open_hosted_session",
                    "required_capabilities": [
                        "pixel_frame_capture",
                        "pixel_input",
                        "uia_v1",
                    ],
                },
                "minimum_readback": "system_of_record",
                "save_step": saves[0].id,
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(root / "patients.db"),
                    "sql_query": "SELECT id, first, last, dob, note FROM patients ORDER BY id",
                },
                "record": {
                    "name": "patient note",
                    "match": ["id"],
                    "duplicate_key": "id",
                    "fields": {"id": {"literal": str(TARGET_ID)}, "note": "note"},
                },
                "inputs": {"params": {"search": DEMO_SEARCH, "note": DEMO_NOTE}},
                "campaign": {"trials": 1, "cases": list(CASE_IDS)},
            },
            env={},
        )
        owner.reset()
        evidence["status"] = "RUNNING"
        with observe_policy(evidence):
            result = qualify(
                bundle,
                config,
                out=root / "campaign",
                trials=1,
                certify=None,
                hooks=QualifyHooks(
                    reset=owner.reset,
                    before_trial=owner.before_trial,
                    oracle=owner.oracle,
                ),
                log=lambda _message: None,
            )
        evidence.update(
            status="RETURNED",
            qualification_passed=result.passed,
            certified=result.certified,
        )
        evidence["refusal_codes"] = [item.code.value for item in result.report.refusals]
        assert not result.certified
        assert {row["case_id"] for row in result.rows} == set(CASE_IDS)
        assert len(result.rows) == 6
        assert all(row["model_calls"] == 0 for row in result.rows)
        assert all(not row["fault_declined"] for row in result.rows)
        assert result.reports["representative"][0].rung_counts.get(
            "structural", 0
        ) >= len(CONTROLS)
        for case_id in CASE_IDS[1:]:
            report = result.reports[case_id][0]
            assert report.execution_outcome == "HALTED"
            assert len(report.qualification_fault_mutations) == 1
        primary = [item for item in owner.oracles if not item["informational"]]
        assert len(primary) == 6 and all(item["passed"] for item in primary)
        assert all(item["fault_detector_error"] is None for item in primary)
        assert all(
            session["closed"] and session["http_closed"] for session in owner.sessions
        )
        assert result.passed, (
            "one-trial campaign did not qualify; gates remain unchanged"
        )
        evidence["status"] = "PASSED"
    except BaseException as exc:
        evidence.update(status="FAILED", exception_type=type(exc).__name__)
        raise
    finally:
        _SESSIONS.pop(name, None)
        try:
            try:
                host.close()
            finally:
                if backend is not None:
                    backend._session.close()
        finally:
            if owner is not None:
                evidence.update(
                    oracles=owner.oracles,
                    sessions=owner.sessions,
                    operations=owner.guest.operations,
                    principal_sha256=owner.guest.principal_sha256,
                )
            evidence["primary_completed"] = sum(
                not item["informational"] for item in evidence.get("oracles", [])
            )
            evidence["informational_completed"] = sum(
                item["informational"] for item in evidence.get("oracles", [])
            )
            try:
                from tests.e2e.win_agent_capture_telemetry import safe_projection

                evidence["frame_guard_diagnostics"] = safe_projection(capture_telemetry)
            except Exception:  # noqa: BLE001 - telemetry never changes test outcome
                evidence["frame_guard_diagnostics"] = {
                    "status": "unavailable",
                    "code": "capture_telemetry_projection_failed",
                }
            (root / "campaign-evidence.json").write_text(json.dumps(evidence, indent=2))
