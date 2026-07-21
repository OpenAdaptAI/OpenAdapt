"""Snapshot-safe, OPT-IN Parallels desktop end-to-end proof.

Proves the DESKTOP path reaches web parity on a REAL Windows app: record ->
compile -> replay through ``WindowsBackend``, and asserts the DETERMINISTIC
``structural`` (UIA) rung resolves every click by ``AutomationId`` and the run
completes.

Target app: the in-tree **Patient Notes -- Benchmark Harness** WinForms app
(``scripts/desktop/patient_notes.ps1``), NOT the Windows Calculator
-----------------------------------------------------------------------------
The earlier version of this test drove the built-in **UWP Calculator**
(``calc.exe``) and asserted per-key ``AutomationId``\\s (``num7Button`` ...). On
**Windows 11 ARM** the modern Calculator is a packaged UWP app hosted under
``ApplicationFrameHost`` whose keypad does NOT surface as a findable top-level
window through the UIA path :meth:`WindowsBackend.locate_structural` walks, so
``locate_structural`` returns None and the test fails EVEN THOUGH the desktop
stack works. That made the "e2e" a flaky Calculator test, not a repeatable
proof of the structural rung.

This test instead drives a **classic Win32/WinForms** app that ships in this
repo and that the desktop benchmark already uses. Its controls are plain
``System.Windows.Forms`` controls created with explicit ``.Name`` /
``.AccessibleName`` (``searchBox``, ``searchButton``, ``noteBox``,
``saveButton``), so WinForms exposes each as a first-class element with a stable
``AutomationId`` in the top-level window's UIA tree -- exactly the elements
:meth:`WindowsBackend.locate_structural` searches for from the root. This was
verified live on the Win11-ARM VM:
``locate_structural(automation_id='searchBox') -> StructuralHandle(...)`` with
confidence 1.0. The four controls this test clicks are all TextBox
(``EditControl``) / Button (``ButtonControl``) controls with stable
``AutomationId``\\s -- it deliberately does NOT click the ``DataGridView`` rows,
whose WinForms UIA tree is only partially populated (rows often carry no
``AutomationId``; see ``docs/desktop/LIMITS.md``), so every recorded click is
structurally armable and the ``armed_coverage == 1.0`` / structural-rung
assertions are meaningful and repeatable rather than flaky.

Safety / isolation (the user's VM is sacred):
    * OPT-IN ONLY. Skipped unless ``OAFLOW_PARALLELS_E2E=1`` -- it is collected
      but never runs on CI or any machine without the env var, so ``pytest
      --ignore=tests/e2e`` (macOS CI) and a plain run both stay green.
    * SNAPSHOT-FIRST, RESTORE-BASE, DELETE-OWNED. A fresh per-trial snapshot is
      taken before anything touches the guest. ``finally`` switches to the
      explicitly named preserved base, proves it is current, and deletes only
      the exact snapshot id this trial created. No snapshot accumulates.
    * A host-free-space preflight runs before VM mutation. The harness never
      deletes the VM or a pre-existing snapshot, and never runs unless the
      maintainer explicitly opts in. CI never does.

Override the VM via ``OAFLOW_PARALLELS_VM_UUID`` (defaults to the documented
"Windows 11" VM).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

RUN_E2E = os.environ.get("OAFLOW_PARALLELS_E2E") == "1"

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in live Parallels e2e; set OAFLOW_PARALLELS_E2E=1 to run",
    ),
    pytest.mark.timeout(900),
]

# The reliable target: classic WinForms controls with stable AutomationIds
# (verified on Win11 ARM). searchBox/noteBox are TextBox (EditControl);
# searchButton/saveButton are Button (ButtonControl). Every one is resolvable
# from the UIA root by AutomationId, so every recorded click arms structurally
# and every replayed click resolves on the structural rung. The DataGridView
# rows are intentionally NOT clicked (partial WinForms UIA tree; see module
# docstring / docs/desktop/LIMITS.md).
WINFORMS_CONTROLS = ("searchBox", "searchButton", "noteBox", "saveButton")

# Demonstration values. The replay must write this exact note to patient 1 and
# the independent SQLite oracle must prove that no sibling record changed.
DEMO_SEARCH = "Neil"
DEMO_NOTE = "BP 128/82; follow-up in 2 weeks"

EVIDENCE_PATH_ENV = "OAFLOW_WINDOWS_UIA_EVIDENCE"
MATRIX_ID_ENV = "OAFLOW_WINDOWS_UIA_MATRIX_ID"
BASE_SNAPSHOT_ENV = "OAFLOW_PARALLELS_BASE_SNAPSHOT_ID"
HOST_STORAGE_PATH_ENV = "OAFLOW_PARALLELS_STORAGE_PATH"
CANDIDATE_COMMIT_ENV = "OAFLOW_WINDOWS_UIA_CANDIDATE_COMMIT"


def _failure_category(error: Exception) -> str:
    """Return a bounded, PHI-free failure taxonomy label."""
    message = str(error).lower()
    if "typed input could not be verified" in message:
        return "input_verification_over_halt"
    if "uia_unavailable" in message or "ui automation is unavailable" in message:
        return "uia_provider_unavailable"
    if "ambiguous" in message:
        return "ambiguity_refusal_failure"
    if "stale_target" in message:
        return "stale_target_refusal_failure"
    if "restore snapshot" in message or "suspend" in message:
        return "environment_restore_failure"
    if isinstance(error, AssertionError):
        return "oracle_assertion_failure"
    return "unexpected_runtime_failure"


def _append_evidence_row(path: Path, row: dict[str, Any]) -> None:
    """Persist one qualification row and recompute auditable aggregates.

    The path is explicitly outside pytest's temporary tree. It intentionally
    stores only synthetic task labels and bounded verdicts, never screenshots,
    typed values, bearer tokens, certificates, raw exception messages, or DB
    rows. Existing rejected attempts are retained rather than overwritten.
    """
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "openadapt.windows-uia.v1":
            raise RuntimeError("refusing to append to an unknown evidence schema")
    else:
        payload = {
            "schema_version": "openadapt.windows-uia.v1",
            "task": {
                "name": "patient-notes-record-compile-governed-replay",
                "application": "in-tree WinForms Patient Notes benchmark harness",
                "workflow": "record, compile, replay, and verify one isolated note write",
            },
            "environment": {
                "substrate": "Parallels Desktop Windows 11 ARM",
                "vm_uuid": row["environment"]["vm_uuid"],
                "base_snapshot_id": row["environment"]["base_snapshot_id"],
                "transport": "per-run TLS certificate pin plus bearer token",
                "agent_contract": "typed_input_v1 + uia_v1; legacy_exec disabled",
            },
            "oracle": {
                "source": "independent SQLite pn_db.py all query",
                "success": "target record contains the exact synthetic note and every sibling note remains empty",
                "refusal_invariant": "stale and ambiguous UIA actions leave all database rows unchanged",
            },
            "minimum_evidence_standard": {
                "required_accepted_trials": 3,
                "clean_snapshot_per_trial": True,
            },
            "caveats": [
                "This qualifies the named in-tree WinForms workflow and exact VM snapshot, not arbitrary Windows applications.",
                "Native UIA receipts prove delivery to a re-resolved unique fingerprint; the SQLite oracle, not the receipt, verifies the business outcome.",
                "Screenshot artifacts remain local and are excluded from this PHI-free evidence file.",
            ],
            "runs": [],
        }
    runs = payload.setdefault("runs", [])
    key = (row["matrix_id"], row["trial"])
    if any((item.get("matrix_id"), item.get("trial")) == key for item in runs):
        raise RuntimeError(f"duplicate Windows UIA evidence row: {key!r}")
    runs.append(row)
    categories: dict[str, int] = {}
    for item in runs:
        item_categories = item.get("failure_categories")
        if not isinstance(item_categories, list) or not item_categories:
            item_categories = [str(item.get("failure_category") or "none")]
        for category in item_categories:
            label = str(category)
            categories[label] = categories.get(label, 0) + 1
    accepted = [item for item in runs if item.get("accepted") is True]
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["summary"] = {
        "run_count": len(runs),
        "accepted_count": len(accepted),
        "rejected_count": len(runs) - len(accepted),
        "accepted_task_success_count": sum(
            item.get("report_success") is True for item in accepted
        ),
        "silent_incorrect_success_count": sum(
            item.get("silent_incorrect_success") is True for item in runs
        ),
        "over_halt_count": sum(item.get("over_halt") is True for item in runs),
        "accepted_native_receipt_count": sum(
            int(item.get("native_receipt_count", 0)) for item in accepted
        ),
        "accepted_stale_refusal_count": sum(
            item.get("stale_refusal_passed") is True for item in accepted
        ),
        "accepted_ambiguity_refusal_count": sum(
            item.get("ambiguity_refusal_passed") is True for item in accepted
        ),
        "failure_taxonomy": categories,
    }
    matrix_summaries: dict[str, dict[str, Any]] = {}
    for item in runs:
        matrix_id = str(item.get("matrix_id") or "unknown")
        summary = matrix_summaries.setdefault(
            matrix_id,
            {
                "run_count": 0,
                "accepted_count": 0,
                "task_success_count": 0,
                "silent_incorrect_success_count": 0,
                "over_halt_count": 0,
                "native_receipt_count": 0,
                "stale_refusal_count": 0,
                "ambiguity_refusal_count": 0,
                "candidate_commits": [],
            },
        )
        summary["run_count"] += 1
        summary["accepted_count"] += item.get("accepted") is True
        summary["task_success_count"] += item.get("report_success") is True
        summary["silent_incorrect_success_count"] += (
            item.get("silent_incorrect_success") is True
        )
        summary["over_halt_count"] += item.get("over_halt") is True
        summary["native_receipt_count"] += int(item.get("native_receipt_count", 0))
        summary["stale_refusal_count"] += item.get("stale_refusal_passed") is True
        summary["ambiguity_refusal_count"] += (
            item.get("ambiguity_refusal_passed") is True
        )
        commit = item.get("candidate_commit")
        if commit and commit not in summary["candidate_commits"]:
            summary["candidate_commits"].append(commit)
    payload["matrix_summaries"] = matrix_summaries
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _deploy_app(vm) -> None:
    """Push the WinForms target-app scripts into the guest (C:/oa).

    ``session1_launch.py`` is already deployed by ``vm.launch_agent``; this adds
    the SQLite ground-truth CLI and the WinForms UI script the launcher runs.
    """
    import openadapt_flow.backends.parallels_vm as pv

    src = Path(pv._SCRIPT_DIR)
    vm.exec_cmd(f"if not exist {pv.GUEST_DIR} mkdir {pv.GUEST_DIR}")
    for name in ("pn_db.py", "patient_notes.ps1"):
        vm.push_file(str((src / name).resolve()), f"{pv.GUEST_DIR}/{name}")


def _seed_db(vm) -> None:
    """(Re)create the app's SQLite DB with the fixed clean roster."""
    import openadapt_flow.backends.parallels_vm as pv

    vm.exec(
        [pv.GUEST_PY, f"{pv.GUEST_DIR}/pn_db.py", "seed", "--drift", "none"],
        timeout=60,
    )


def _launch_app(vm, *, settle_s: float = 6.0) -> None:
    """(Re)launch the WinForms app in the interactive session (session 1).

    Kills any prior instance first so replay starts from a fresh, clean window.
    The app is a PowerShell ``-STA`` process; ``session1_launch.py`` runs it on
    the real desktop via ``CreateProcessAsUser`` (session-0 cannot host the UI).
    """
    import openadapt_flow.backends.parallels_vm as pv

    vm.exec_cmd("taskkill /F /IM powershell.exe 2>nul & echo ok")
    time.sleep(1)
    vm.exec(
        [
            pv.GUEST_PY,
            f"{pv.GUEST_DIR}/session1_launch.py",
            f"{pv.GUEST_DIR}/patient_notes.ps1",
        ]
    )
    time.sleep(settle_s)


def _launch_additional_app(vm, *, settle_s: float = 6.0) -> None:
    """Launch a second identical window without closing the first."""
    import openadapt_flow.backends.parallels_vm as pv

    vm.exec(
        [
            pv.GUEST_PY,
            f"{pv.GUEST_DIR}/session1_launch.py",
            f"{pv.GUEST_DIR}/patient_notes.ps1",
        ]
    )
    time.sleep(settle_s)


def _control_points(backend, ids) -> dict:
    """Live UIA center point for each WinForms control (via the agent).

    This is the first half of the proof: ``locate_structural`` MUST resolve
    every control by its ``AutomationId``. On a UWP app that surfaces nothing
    findable this returns None and fails loudly (the old Calculator failure);
    on the WinForms target it returns a real center point per control.
    """
    from openadapt_flow.ir import StructuralLocator

    points = {}
    for aid in ids:
        handle = _locate_with_retry(
            backend, StructuralLocator(automation_id=aid), timeout_s=5.0
        )
        assert handle is not None, (
            f"UIA could not locate WinForms control {aid!r} by AutomationId -- "
            "the target app is not exposing a stable UIA tree"
        )
        points[aid] = (int(handle.point[0]), int(handle.point[1]))
    return points


def _locate_with_retry(backend, locator, *, timeout_s: float):
    """Retry bounded UIA not-found during a window/provider settle only.

    Ambiguity and other structural refusals propagate immediately; only a plain
    ``None`` (element not surfaced yet) is retried until the short deadline.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        handle = backend.locate_structural(locator)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(0.25)


def _db_rows(vm) -> list[dict]:
    """Read the authoritative SQLite state, independent of pixels/UIA."""
    import openadapt_flow.backends.parallels_vm as pv

    completed = vm.exec(
        [pv.GUEST_PY, f"{pv.GUEST_DIR}/pn_db.py", "all"],
        timeout=60,
        check=True,
    )
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    raise AssertionError(
        f"Patient Notes DB command returned no JSON rows: {completed.stdout!r}"
    )


@pytest.mark.parametrize("trial", [1, 2, 3])
def test_desktop_record_compile_replay_structural(tmp_path, trial: int) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.backends.parallels_vm import (
        DEFAULT_VM_UUID,
        ParallelsError,
        ParallelsVM,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    uuid = os.environ.get("OAFLOW_PARALLELS_VM_UUID", DEFAULT_VM_UUID)
    base_snapshot_id = os.environ.get(BASE_SNAPSHOT_ENV)
    candidate_commit = os.environ.get(CANDIDATE_COMMIT_ENV)
    vm = ParallelsVM(uuid)
    token = secrets.token_hex(16)
    started_at = datetime.now(timezone.utc).isoformat()
    evidence_row: dict[str, Any] = {
        "matrix_id": os.environ.get(MATRIX_ID_ENV, "manual"),
        "candidate_commit": candidate_commit,
        "trial": trial,
        "started_at": started_at,
        "environment": {
            "vm_uuid": uuid,
            "base_snapshot_id": base_snapshot_id,
        },
        "accepted": False,
        "report_success": False,
        "oracle_passed": False,
        "silent_incorrect_success": False,
        "over_halt": False,
        "native_receipt_count": 0,
        "legacy_exec_refusal_passed": False,
        "malformed_input_refusal_passed": False,
        "stale_refusal_passed": False,
        "ambiguity_refusal_passed": False,
        "clean_restore_passed": False,
        "base_snapshot_current_after": False,
        "trial_snapshot_deleted": False,
        "failure_category": None,
        "failure_categories": [],
    }

    snap_id: str | None = None
    vm_touched = False
    active_error: Exception | None = None
    try:
        if not base_snapshot_id:
            raise RuntimeError(
                f"{BASE_SNAPSHOT_ENV} is required for snapshot-safe qualification"
            )
        if (
            candidate_commit is None
            or re.fullmatch(r"[0-9a-f]{40}", candidate_commit) is None
        ):
            raise RuntimeError(
                f"{CANDIDATE_COMMIT_ENV} must name one exact candidate commit"
            )
        storage_path = os.environ.get(HOST_STORAGE_PATH_ENV, os.getcwd())
        evidence_row["host_free_bytes_before"] = vm.require_host_free_space(
            storage_path=storage_path
        )
        vm_touched = True
        vm.ensure_running()
        # Snapshot before any guest deployment/recording. This id is retained
        # in memory and is the only snapshot the trial may later delete.
        snap_id = vm.snapshot(
            f"oaflow-e2e-t{trial}-{int(time.time())}",
            description=f"openadapt-flow typed UIA qualification trial {trial}/3",
        )
        evidence_row["trial_snapshot_id"] = snap_id
        endpoint = vm.launch_agent(token=token)
        # launch_agent auto-provisions the per-run TLS cert into the guest and
        # returns the pin fingerprint, so the client is encrypted + pinned end to
        # end with no manual step (docs/phi_in_transit.md). ``endpoint.backend()``
        # wires https:// + pin_fingerprint + require_tls + token in one call.
        backend = endpoint.backend()
        assert endpoint.url.startswith("https://")
        assert backend.probe(), "win_agent screenshot probe failed"
        capabilities = backend.agent_capabilities()
        assert {"typed_input_v1", "uia_v1"}.issubset(capabilities)
        assert "legacy_exec" not in capabilities, (
            "qualification agent exposed arbitrary Python execution"
        )
        # The deployed agent must refuse the old arbitrary-code route and
        # malformed typed requests in every trial, over the same pinned+token
        # channel used for the workflow.
        legacy = backend._session.post(
            f"{endpoint.url}/execute_windows",
            json={"command": "print('must not execute')"},
            **backend._request_kwargs(),
        )
        assert legacy.status_code == 404
        evidence_row["legacy_exec_refusal_passed"] = True
        malformed = backend._session.post(
            f"{endpoint.url}/input",
            json={"action": "click", "x": 1, "y": 2, "unknown": True},
            **backend._request_kwargs(),
        )
        assert malformed.status_code == 400
        assert malformed.json()["code"] == "invalid_schema"
        evidence_row["malformed_input_refusal_passed"] = True

        # Deploy + seed + launch the RELIABLE WinForms target in session 1.
        _deploy_app(vm)
        _seed_db(vm)
        _launch_app(vm)

        # Half 1 of the proof: UIA resolves every control by AutomationId.
        points = _control_points(backend, WINFORMS_CONTROLS)

        # RECORD the demonstration live -> arms a UIA locator per click. Every
        # click lands on a control that carries a stable AutomationId, so the
        # compiled bundle is fully structurally armed (web parity).
        def driver(rec) -> None:
            rec.click(*points["searchBox"])
            rec.type_text(DEMO_SEARCH)
            rec.press("Enter")
            time.sleep(0.4)
            rec.click(*points["searchButton"])
            time.sleep(0.4)
            rec.click(*points["noteBox"])
            rec.type_text(DEMO_NOTE)
            rec.click(*points["saveButton"])
            time.sleep(0.4)

        recording = record_desktop_demo(backend, tmp_path / "recording", driver)

        bundle_dir = tmp_path / "bundle"
        workflow = compile_recording(recording, bundle_dir, name="patient-notes-e2e")
        coverage = structural_armed_coverage(workflow)
        assert coverage["armed_coverage"] == 1.0, (
            "desktop bundle is not fully structurally armed (web parity gap): "
            f"{coverage}"
        )
        n_clicks = coverage["click_steps"]
        assert n_clicks == len(WINFORMS_CONTROLS), (
            f"expected {len(WINFORMS_CONTROLS)} click steps, got {n_clicks}: {coverage}"
        )

        # REPLAY from a fresh app + authoritative DB baseline; the structural
        # rung must drive it and the recording's earlier write cannot leak in.
        _seed_db(vm)
        _launch_app(vm)
        before_rows = _db_rows(vm)
        assert all(row["note"] == "" for row in before_rows)
        loaded = Workflow.load(bundle_dir)
        report = Replayer(backend, use_structural=True).run(
            loaded,
            params={},
            bundle_dir=bundle_dir,
            run_dir=tmp_path / "run",
        )
        evidence_row["report_success"] = report.success
        evidence_row["rung_counts"] = report.rung_counts
        evidence_row["model_calls"] = report.model_calls
        evidence_row["total_ms"] = report.total_ms

        # Half 2 of the proof: the structural (UIA) rung resolved EVERY click.
        assert report.rung_counts.get("structural", 0) >= n_clicks, (
            f"structural (UIA) rung did not fire for every click: {report.rung_counts}"
        )
        assert report.success, [r.model_dump() for r in report.results]
        by_step = {step.id: step for step in loaded.steps}
        click_results = [
            result
            for result in report.results
            if by_step[result.step_id].action.value in {"click", "double_click"}
        ]
        assert len(click_results) == n_clicks
        assert [result.actuation for result in click_results] == ["uia"] * n_clicks
        assert [result.delivery_receipt.operation for result in click_results] == [
            "uia_focus",
            "uia_invoke",
            "uia_focus",
            "uia_invoke",
        ]
        for result in click_results:
            receipt = result.delivery_receipt
            assert receipt is not None
            assert receipt.native is True
            assert receipt.outcome_verified is False
            assert result.resolution is not None
            assert result.resolution.structural_handle is not None
            assert result.resolution.structural_handle.candidate_count == 1
        evidence_row["native_receipt_count"] = len(click_results)

        after_rows = _db_rows(vm)
        patient_one = next(row for row in after_rows if row["id"] == 1)
        assert patient_one["note"] == DEMO_NOTE
        assert all(row["note"] == "" for row in after_rows if row["id"] != 1), (
            "independent DB oracle found a wrong-record write"
        )
        evidence_row["oracle_passed"] = True
        # No click was admitted on an identity MISMATCH.
        for result in report.results:
            if result.identity is not None:
                assert result.identity.status != "mismatch"

        # Live refusal 1: a target replaced between resolve and act keeps the
        # same locator but gets a new UIA RuntimeId/process fingerprint. The old
        # handle must be refused before Invoke/Focus, with DB state unchanged.
        from openadapt_flow.backend import StructuralResolutionRefused
        from openadapt_flow.ir import StructuralLocator

        save_locator = StructuralLocator(automation_id="saveButton")
        stale_handle = _locate_with_retry(backend, save_locator, timeout_s=5.0)
        assert stale_handle is not None
        _launch_app(vm)
        fresh_handle = _locate_with_retry(backend, save_locator, timeout_s=5.0)
        assert fresh_handle is not None
        assert fresh_handle.target_fingerprint != stale_handle.target_fingerprint
        with pytest.raises(StructuralResolutionRefused, match="stale_target"):
            backend.act_structural(save_locator, stale_handle)
        assert _db_rows(vm) == after_rows
        evidence_row["stale_refusal_passed"] = True

        # Live refusal 2: two identical top-level app windows expose the same
        # AutomationIds and title. Exact locator matching is intentionally
        # ambiguous and must never fall through to a pixel click.
        _launch_additional_app(vm)
        with pytest.raises(StructuralResolutionRefused, match="ambiguous"):
            backend.locate_structural(save_locator)
        assert _db_rows(vm) == after_rows
        evidence_row["ambiguity_refusal_passed"] = True
        evidence_row["accepted"] = True
    except Exception as exc:
        active_error = exc
        evidence_row["failure_category"] = _failure_category(exc)
        evidence_row["failure_categories"].append(evidence_row["failure_category"])
        evidence_row["exception_type"] = type(exc).__name__
        evidence_row["silent_incorrect_success"] = bool(
            evidence_row["report_success"] and not evidence_row["oracle_passed"]
        )
        evidence_row["over_halt"] = bool(
            not evidence_row["report_success"]
            and evidence_row["failure_category"] == "input_verification_over_halt"
        )
        raise
    finally:
        # Restore the explicit preserved base. Only after proving it current may
        # the harness delete the exact per-trial snapshot id it just created.
        # A failure makes the row rejected and stops cleanup before any broader
        # mutation; no names, wildcards, or child-recursive deletion are used.
        cleanup_errors: list[Exception] = []
        if base_snapshot_id and snap_id is not None:
            try:
                vm.restore_base_and_delete_owned_snapshot(
                    base_snapshot_id=base_snapshot_id,
                    owned_snapshot_id=snap_id,
                )
                evidence_row["trial_snapshot_deleted"] = True
            except Exception as exc:
                cleanup_errors.append(exc)
        elif base_snapshot_id and vm_touched:
            try:
                vm.revert(base_snapshot_id)
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            state = vm.status()
            if state in {"running", "paused"}:
                vm.suspend()
                state = vm.status()
            if state != "suspended":
                raise ParallelsError(
                    f"preserved base did not finish suspended (state={state!r})"
                )
            if base_snapshot_id:
                snapshots = vm.list_snapshots()
                evidence_row["base_snapshot_current_after"] = any(
                    item.snapshot_id == base_snapshot_id and item.current
                    for item in snapshots
                )
                if not evidence_row["base_snapshot_current_after"]:
                    raise ParallelsError("preserved base is not current after trial")
        except Exception as exc:
            cleanup_errors.append(exc)
        evidence_row["clean_restore_passed"] = not cleanup_errors
        if cleanup_errors:
            evidence_row["accepted"] = False
            evidence_row["primary_failure_category"] = evidence_row["failure_category"]
            evidence_row["failure_category"] = "environment_restore_failure"
            if "environment_restore_failure" not in evidence_row["failure_categories"]:
                evidence_row["failure_categories"].append("environment_restore_failure")
        evidence_row["completed_at"] = datetime.now(timezone.utc).isoformat()
        evidence_path_value = os.environ.get(EVIDENCE_PATH_ENV)
        evidence_error: Exception | None = None
        if evidence_path_value:
            try:
                _append_evidence_row(Path(evidence_path_value), evidence_row)
            except Exception as exc:
                evidence_error = exc
                evidence_row["accepted"] = False
        if cleanup_errors and active_error is None:
            details = "; ".join(repr(error) for error in cleanup_errors)
            raise RuntimeError(
                f"trial {trial} failed to restore snapshot/suspended state: {details}"
            ) from cleanup_errors[0]
        if cleanup_errors:
            for cleanup_error in cleanup_errors:
                active_error.add_note(f"cleanup error: {cleanup_error!r}")
        if evidence_error is not None and active_error is None:
            raise RuntimeError(
                f"trial {trial} failed to persist durable evidence"
            ) from evidence_error
        if evidence_error is not None:
            active_error.add_note(f"evidence error: {evidence_error!r}")
