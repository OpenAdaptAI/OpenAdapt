#!/usr/bin/env python3
"""Qualify a repaired session notice on a real Windows app, through UI Automation.

The app is the lab's Patient Notes WinForms app (``scripts/desktop/
patient_notes.ps1``) on the WinRM lab VM, with its SQLite database in the
guest. The script:

1. starts ``win_agent`` on the VM's desktop (pinned TLS, bearer token);
2. records the healthy workflow through ``WindowsBackend``: search for a
   patient, type a note (a parameter), save. Every click carries the patient
   header as its identity band; the save gets a SQL record check on the
   patient's note;
3. runs it with ``interrupt=after_search``: the app shows an owned "Session
   notice" dialog after the search, and the run stops before any save;
4. runs ``SelfRepair``. The repair agent (it proposes clicking "OK", found by
   OCR) and the approver are scripted. The app, UI Automation, the screen, the
   input and the database are real. The fault probe seeds a look-alike patient
   (``--drift siblings``) and must stop before the save;
5. reads the database: exactly the patient's note changed, to the run's note.

The record check reads a fresh copy of the guest's database file over WinRM
on every read, through ``SqlRecordVerifier``.

Usage (from packages/flow, the lab ``.env`` loaded; tell the coordinator
first: one driver uses the VM desktop at a time)::

    python benchmark/repair_every_backend/qualify_windows.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FLOW = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DESKTOP = FLOW / "scripts" / "desktop"
for path in (FLOW, HERE, DESKTOP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qualify_lab_native as lab_native  # noqa: E402
import qualify_macos as mac  # noqa: E402
import winrm_lab  # noqa: E402

RESULTS = HERE / "results"
#: Where the app files pushed to the guest come from (the lab's copies, which
#: carry the ``interrupt`` knob, until they land here).
LAB_FILES = Path(os.environ.get("OAFLOW_WINLAB_SCRIPTS") or DESKTOP)
GUEST = lab_native.GUEST
PY = lab_native.PY
SEARCH = "Neil"
PATIENT = {"first": "Neil", "last": "Sorenson"}
DEMO_NOTE = "Follow up in two weeks"
RUN_NOTE = "Repaired run follow up in three weeks"


def _app(lab: Any, backend: Any, *, drift: str = "none", **knobs: Any) -> None:
    """A fresh app on a freshly seeded database (``drift`` picks the seed)."""
    lab.stop_interactive("OAFlowApp")
    lab.run_ps(
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -like '*patient_notes.ps1*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
        check=False,
    )
    lab.run_ps(f"& '{PY}' '{GUEST}\\pn_db.py' seed --drift {drift}")
    lab_native._app_env(lab, **knobs)
    lab.launch_interactive(
        f"powershell.exe -STA -ExecutionPolicy Bypass -WindowStyle Hidden "
        f"-File {lab_native.APP}",
        name="OAFlowApp",
    )
    time.sleep(8.0)
    lab_native._ready(backend)


def _stop_app(lab: Any) -> None:
    lab.stop_interactive("OAFlowApp")


def _db_copy(lab: Any) -> sqlite3.Connection:
    """A read-only connection to a fresh copy of the guest's database."""
    out = lab.run_ps(
        f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{GUEST}\\patients.db'))"
    ).stdout.strip()
    handle, path = tempfile.mkstemp(suffix=".db")
    with os.fdopen(handle, "wb") as f:
        f.write(base64.b64decode(out))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _notes(lab: Any) -> dict[int, str]:
    with _db_copy(lab) as conn:
        return {
            r["id"]: r["note"] or "" for r in conn.execute("SELECT * FROM patients")
        }


def _record(lab: Any, backend: Any, root: Path) -> tuple[Path, dict]:
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import StructuralLocator, Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    _app(lab, backend)
    points = {}
    for aid in ("searchBox", "searchButton", "noteBox", "saveButton"):
        handle = lab_native._locate(backend, StructuralLocator(automation_id=aid))
        assert handle is not None, f"UIA did not find {aid}"
        points[aid] = (int(handle.point[0]), int(handle.point[1]))

    def driver(rec: Any) -> None:
        rec.click(*points["searchBox"])
        rec.type_text(SEARCH)
        rec.click(*points["searchButton"])
        time.sleep(0.8)
        rec.click(*points["noteBox"])
        rec.type_text(DEMO_NOTE, param="note")
        rec.click(*points["saveButton"])
        time.sleep(1.0)

    recording = record_desktop_demo(backend, root / "recording", driver)
    _stop_app(lab)
    # Each click's identity band (``--identifier`` in a browser), from its own
    # recorded frame: the "Patient: ..." header once a patient is shown, the
    # window's title (which app this is) before that.
    from openadapt_flow.vision import ocr

    events_path = recording / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    marked = []
    for event in events:
        if event.get("kind") != "click":
            continue
        frame = recording / "frames" / f"{int(event['i']):04d}_before.png"
        lines = ocr(frame.read_bytes())
        band = next(
            (ln for ln in lines if ln.text.strip().startswith("Patient:")), None
        )
        if band is None:
            band = next((ln for ln in lines if "Patient Notes" in ln.text), None)
        if band is not None:
            x, y, w, h = band.region
            event["identifier_region"] = [max(0, x - 4), max(0, y - 3), w + 8, h + 6]
            marked.append(band.text.split(":")[0])
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    bundle = root / "bundle"
    workflow = compile_recording(recording, bundle, name="patient-notes-repair")
    save = workflow.steps[-1]
    note = ValueExpr(param="note")
    save.effects = [
        # The patient's row carries the run's note, exactly once (the save
        # updates an existing row, so it is not a new one).
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={**PATIENT, "note": note},
            expected_count=1,
            composite_key={**PATIENT, "note": note},
            risk="irreversible",
            timeout_s=10.0,
        )
    ]
    save.risk = "irreversible"
    for step in workflow.steps[:-1]:
        step.risk = "reversible"
    (bundle / "manifest.json").unlink(missing_ok=True)
    workflow.save(bundle, encrypt=bool(workflow.encrypted))
    workflow = Workflow.load(bundle)
    report = evaluate_policy(workflow, load_policy("clinical-write"))
    return bundle, {
        "steps": [f"{s.id}:{s.action.value}:{s.intent}" for s in workflow.steps],
        "identity_bands": marked,
        "certified_clinical_write": report.passed,
        "certification": report.render() if not report.passed else "passed",
    }


class OkAgent:
    """Scripted repair agent: proposes clicking "OK". No model calls."""

    dialect = "generic"
    agent_id = "qualification:scripted-repair-agent"
    requires_model_key = False

    def propose(self, request: Any) -> Any:
        from openadapt_flow.repair.self_repair import RepairProposal
        from openadapt_flow.vision import ocr

        for line in ocr(request.live_png):
            if mac._squash(line.text) == "ok":
                x, y, w, h = line.region
                return RepairProposal(
                    actions=[{"kind": "click", "x": x + w // 2, "y": y + h // 2}],
                    note="dismiss the session notice",
                )
        return None


def _repair(
    lab: Any, backend: Any, root: Path, bundle: Path, case: str, *, profile: str
):
    from openadapt_flow.repair.self_repair import (
        AppSession,
        ReplayTarget,
        ScreenAccess,
        SelfRepair,
        SelfRepairRefused,
    )
    from openadapt_flow.runtime.effects import SqlRecordVerifier

    def open_session(fault: Optional[str]) -> Any:
        _app(
            lab,
            backend,
            drift="siblings" if fault == "look_alike" else "none",
            interrupt="after_search",
        )
        return AppSession(backend=backend, close=lambda: _stop_app(lab))

    target = ReplayTarget(
        open_session,
        effect_verifier=SqlRecordVerifier(
            lambda: _db_copy(lab),
            "SELECT id, first, last, dob, note FROM patients "
            "WHERE first = :first AND last = :last",
            query_params=dict(PATIENT),
            poll_interval_s=1.0,
            timeout_s=15.0,
        ),
        policy="clinical-write",
        profile=profile,
        params={"note": RUN_NOTE},
        execution_target_kind="windows",
    )
    decisions: list[float] = []
    run_root = root / case
    run_root.mkdir()
    repair = SelfRepair(
        OkAgent(),
        target,
        store=run_root / "repair-store",
        screen_access=ScreenAccess(
            enabled_by="qualification-operator", mode="allow_unscrubbed"
        ),
        fault_probes=("look_alike",),
        approval_wait_s=300,
        sleep=mac._approver(run_root, decisions),
    )
    try:
        result = repair.run(bundle, run_root / "item")
    except SelfRepairRefused as exc:
        return {
            "status": "refused",
            "reason": str(exc)[:2000],
            "stages": [],
            "person_decisions": len(decisions),
            "notes": _notes(lab),
        }
    evidence = result.evidence
    return {
        "status": result.status,
        "reason": evidence.reason,
        "person_decisions": len(decisions),
        "approver": mac.APPROVER,
        "stages": [
            {"stage": s.stage, "passed": s.passed, "detail": s.detail}
            for s in evidence.steps
        ],
        "notes": _notes(lab),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    root = Path(tempfile.mkdtemp(prefix="oa-repair-uia-"))
    saved_env = dict(os.environ)
    lab = winrm_lab.WinRMLab()
    endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
    backend = endpoint.backend()
    for name in ("pn_db.py", "patient_notes.ps1"):
        lab.push_file(LAB_FILES / name, rf"{GUEST}\{name}")
    try:
        mac._approver_roster(root)
        bundle, compiled = _record(lab, backend, root)
        checked = _repair(
            lab, backend, root, bundle, "with-record-check", profile="standard"
        )
        demo = _repair(lab, backend, root, bundle, "demo-profile", profile="demo")
    finally:
        _stop_app(lab)
        os.environ.clear()
        os.environ.update(saved_env)
    stages = json.dumps(checked["stages"])
    target_id = 1
    checks = {
        "notice_stopped_the_run": checked["status"] != "not_needed",
        "bundle_certified": compiled["certified_clinical_write"],
        "repair_resumed": checked["status"] == "resumed",
        "resume_verified": any(
            s["stage"] == "resume" and s["passed"] for s in checked["stages"]
        ),
        "only_the_patients_note_changed": {
            k: v for k, v in checked["notes"].items() if v
        }
        == {target_id: RUN_NOTE},
        "click_admitted_on_element_identity": "admitted by element_and_record_check"
        in stages,
        "fault_probe_stopped_before_save": any(
            s["stage"] == "campaign" and "fault" in s["detail"] and s["passed"]
            for s in checked["stages"]
        ),
        "demo_profile_not_repaired": demo["status"] != "resumed",
        "demo_profile_wrote_nothing": not any(demo["notes"].values()),
    }
    report = {
        "schema": "openadapt.repair-every-backend-qualification/v1",
        "kind": "uia",
        "backend": "WindowsBackend (UI Automation, win_agent)",
        "app": "scripts/desktop/patient_notes.ps1 (WinForms, SQLite), Windows 11 VM",
        "at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=FLOW
        ).stdout.strip(),
        "git_dirty": [
            line
            for line in subprocess.run(
                ["git", "status", "--porcelain", "--", "."],
                capture_output=True,
                text=True,
                cwd=FLOW,
            ).stdout.splitlines()
            if "repair_every_backend/results/" not in line
        ],
        "app_files": {
            name: hashlib.sha256((LAB_FILES / name).read_bytes()).hexdigest()
            for name in ("pn_db.py", "patient_notes.ps1")
        },
        "compiled": compiled,
        "repair_with_record_check": checked,
        "repair_under_demo_profile": demo,
        "checks": checks,
        "passed": all(checks.values()),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"windows-uia-{datetime.now(timezone.utc):%Y%m%d}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"uia: {'PASSED' if report['passed'] else 'FAILED'} -> {out}")
    for name, ok in checks.items():
        print(f"  {'ok ' if ok else 'BAD'} {name}")
    if args.keep:
        print(f"work folder: {root}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
