#!/usr/bin/env python3
"""Qualify record, compile, a governed run and a repair on Linux (AT-SPI).

The app is ``tests/native_apps/chart_gtk.py``: a GTK3 window with a patient
banner, a "New note" form and a "Save note" button that inserts one row into
a SQLite ``notes`` table. Its "new version" shows an in-window "Update
available" notice on the first "New note". It is the Linux counterpart of
``Chart.swift`` in ``qualify_macos.py``.

The script drives ``LinuxBackend`` over a live AT-SPI registry and X11
display (Xvfb in CI):

1. records the healthy workflow on the old version and compiles it; the save
   gets a SQL record check (``SqlRecordVerifier``) and the patient banner as
   its identity band;
2. runs the bundle governed (``standard`` profile, run gate, durable, settled)
   on the old version: the clean run must be ``VERIFIED`` with one new row;
   another patient's chart (planted fault) and a look-alike chart (same name,
   one MRN character different) must stop before the save and write nothing;
3. runs it on the new version, where it stops at the notice before any save,
   and runs ``SelfRepair``. The repair agent is scripted (it proposes clicking
   "Later", found by OCR) and so is the approver (it approves every request,
   through the decision portal's own code). The new click must be admitted
   on ``element_and_record_check``: its AT-SPI element identity (role, name,
   ancestry, window, process) plus the record check;
4. checks the refusals: with no verifier, or under ``demo``, the fix is not
   admitted and nothing is written.

Usage, in the Linux qualification container (Xvfb, openbox, session D-Bus,
AT-SPI)::

    benchmark/linux_container/run.sh build
    OA_LINUX_SETUP=1 benchmark/linux_container/run.sh \
        python benchmark/repair_every_backend/qualify_linux.py

or on any X11 session with a window manager, a session D-Bus started after
``DISPLAY`` is set, and ``GTK_MODULES=atk-bridge`` (the CI job's setup).

Writes ``benchmark/repair_every_backend/results/linux-atspi-<date>.json``
(or ``--output``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
for path in (FLOW, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from qualify_macos import (  # noqa: E402
    APPROVER,
    LaterAgent,
    _approver,
    _approver_roster,
    _find_text,
    _rows,
)

APP_SOURCE = FLOW / "tests" / "native_apps" / "chart_gtk.py"
RESULTS = HERE / "results"
APP = "chart"
TITLE = "Chart"
# No 0/O/1/l/I in the MRNs: pixel identity abstains on those glyphs.
PATIENT = ("rc79284", "MRN RC79284 Okafor, Philip born 1966-02-17")
OTHER = ("rc48572", "MRN RC48572 Adeyemi, Grace born 1971-03-29")
# The same name and birth date; one MRN character differs.
LOOK_ALIKE = ("rc79285", "MRN RC79285 Okafor, Philip born 1966-02-17")
DEMO_NOTE = "FOLLOW UP IN TWO WEEKS"
RUN_NOTE = "GOVERNED RUN FOLLOW UP IN THREE WEEKS"
REPAIR_NOTE = "REPAIRED RUN FOLLOW UP IN FOUR WEEKS"
FAULTS = {"other_patient": OTHER, "look_alike": LOOK_ALIKE}


class App:
    """One launched chart process; killed by exact PID."""

    def __init__(self, db: Path, patient: tuple[str, str], mode: str):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OPENADAPT_", "OAFLOW_"))
        }
        self.proc = subprocess.Popen(
            [sys.executable, str(APP_SOURCE), str(db), TITLE, *patient, mode],
            env=env,
        )

    def close(self) -> None:
        from openadapt_flow.backends.linux_backend import AtspiLinuxClient

        self.proc.kill()
        self.proc.wait(timeout=10)
        # The registry drops a killed app's window a moment later; the next
        # run would otherwise see two "Chart" windows and refuse.
        client = AtspiLinuxClient()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and client.find_windows(APP, TITLE):
            time.sleep(0.3)


def _backend() -> Any:
    from openadapt_flow.backends.linux_backend import AtspiLinuxClient, LinuxBackend

    client = AtspiLinuxClient()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        windows = client.find_windows(APP, TITLE)
        if len(windows) == 1:
            client.focus_window(windows[0])
            if client.window_is_active(windows[0]):
                break
        time.sleep(0.3)
    backend = LinuxBackend(
        client, app=APP, window_title=TITLE, allow_physical_input=True
    )
    time.sleep(0.5)
    backend.screenshot()
    return backend


def _point(backend: Any, role: str, name: str) -> tuple[int, int]:
    """The center of the one AT-SPI element with ``role`` and ``name``."""
    from openadapt_flow.ir import StructuralLocator

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        handle = backend.locate_structural(
            StructuralLocator(role=role, name=name, window_name=TITLE)
        )
        if handle is not None:
            return handle.point
        time.sleep(0.2)
    raise RuntimeError(f"no {role} {name!r} in the {TITLE!r} window")


def _record(root: Path, db: Path) -> tuple[Path, dict]:
    """Record the healthy workflow on the old version and compile it."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    app = App(db, PATIENT, "plain")
    try:
        backend = _backend()
        recording = root / "recording"
        recorder = Recorder(
            backend, recording, settle_interval_s=0.05, settle_timeout_s=3.0
        )
        recorder.click(*_point(backend, "button", "New note"))
        _bx, _by, banner = _find_text(backend, PATIENT[1])
        recorder.click(*_point(backend, "textbox", "Note"))
        recorder.type_text(DEMO_NOTE, param="note")
        recorder.click(*_point(backend, "button", "Save note"))
        recorder.finish()
    finally:
        app.close()
    events_path = recording / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    pad = 6
    for event in events:
        if event.get("kind") == "click":
            event["identifier_region"] = [
                banner[0] - pad,
                banner[1] - pad,
                banner[2] + 2 * pad,
                banner[3] + 2 * pad,
            ]
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    bundle = root / "bundle"
    workflow = compile_recording(recording, bundle, name="chart-note-linux")
    save = workflow.steps[-1]
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient_id": PATIENT[0], "note": ValueExpr(param="note")},
            composite_key={"patient_id": PATIENT[0], "note": ValueExpr(param="note")},
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            timeout_s=5.0,
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
        "steps": [
            {
                "id": s.id,
                "action": s.action.value,
                "intent": s.intent,
                "structural": s.anchor.structural.model_dump(exclude_none=True)
                if s.anchor is not None and s.anchor.structural is not None
                else None,
            }
            for s in workflow.steps
        ],
        "save_identity_armed": bool(
            workflow.steps[-1].anchor and workflow.steps[-1].anchor.identifier_crop
        ),
        "certified_clinical_write": report.passed,
        "certification": report.render() if not report.passed else "passed",
    }


def _target(
    db: Path, *, mode: str, note: str, verifier: bool = True, profile: str = "standard"
) -> Any:
    from openadapt_flow.repair.self_repair import AppSession, ReplayTarget
    from openadapt_flow.runtime.effects import SqlRecordVerifier

    def open_session(fault: Optional[str]) -> Any:
        app = App(db, FAULTS.get(fault or "", PATIENT), mode)
        try:
            backend = _backend()
        except BaseException:
            app.close()
            raise
        return AppSession(backend=backend, close=app.close)

    return ReplayTarget(
        open_session,
        effect_verifier=SqlRecordVerifier(
            lambda: sqlite3.connect(db),
            "SELECT id, patient_id, note FROM notes WHERE patient_id = :patient_id",
            query_params={"patient_id": PATIENT[0]},
            poll_interval_s=0.1,
        )
        if verifier
        else None,
        policy="clinical-write",
        profile=profile,
        params={"note": note},
        execution_target_kind="linux",
    )


def _governed(root: Path, bundle: Path, case: str, fault: Optional[str]) -> dict:
    """One governed run of the unrepaired bundle on the old version."""
    from openadapt_flow.ir import Workflow

    db = root / f"sor-{case}.db"
    _rows(db)
    target = _target(db, mode="plain", note=RUN_NOTE)
    session = target.open(fault)
    started = time.monotonic()
    try:
        report = target.execute(
            Workflow.load(bundle), bundle, session, root / f"run-{case}"
        )
    except Exception as exc:  # noqa: BLE001 - a refusal is a result here
        return {"status": "refused", "reason": str(exc)[:2000], "rows": _rows(db)}
    finally:
        session.close()
    return {
        "status": report.execution_outcome,
        "success": report.success,
        "duration_s": round(time.monotonic() - started, 1),
        "steps": [
            {
                "step": r.step_id,
                "ok": r.ok,
                "actuation": r.actuation,
                "identity": r.identity.model_dump(mode="json", exclude_none=True)
                if r.identity is not None
                else None,
                "effect_verified": r.effect_verified,
                "failure_category": r.failure_category,
                "error": (r.error or "")[:500],
            }
            for r in report.results
        ],
        "rows": _rows(db),
    }


def _repair(
    root: Path,
    bundle: Path,
    case: str,
    *,
    verifier: bool = True,
    profile: str = "standard",
) -> dict[str, Any]:
    from openadapt_flow.repair.self_repair import (
        ScreenAccess,
        SelfRepair,
        SelfRepairRefused,
    )

    db = root / f"sor-{case}.db"
    _rows(db)
    decisions: list[float] = []
    run_root = root / case
    run_root.mkdir()
    repair = SelfRepair(
        LaterAgent(),
        _target(
            db,
            mode="update_notice",
            note=REPAIR_NOTE,
            verifier=verifier,
            profile=profile,
        ),
        store=run_root / "repair-store",
        screen_access=ScreenAccess(
            enabled_by="qualification-operator", mode="allow_unscrubbed"
        ),
        fault_probes=("other_patient",),
        approval_wait_s=120,
        sleep=_approver(run_root, decisions),
    )
    started = time.monotonic()
    try:
        result = repair.run(bundle, run_root / "item")
    except SelfRepairRefused as exc:
        return {
            "status": "refused",
            "reason": str(exc)[:2000],
            "person_decisions": len(decisions),
            "stages": [],
            "rows": _rows(db),
        }
    evidence = getattr(result, "evidence", None)
    return {
        "status": result.status,
        "reason": evidence.reason if evidence is not None else "",
        "duration_s": round(time.monotonic() - started, 1),
        "person_decisions": len(decisions),
        "approver": APPROVER,
        "stages": [
            {"stage": s.stage, "passed": s.passed, "detail": s.detail}
            for s in (evidence.steps if evidence is not None else [])
        ],
        "rows": _rows(db),
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=FLOW
    ).stdout.strip()


def qualify(root: Path) -> dict[str, Any]:
    _approver_roster(root)
    demo_db = root / "sor-demo.db"
    _rows(demo_db)
    bundle, compiled = _record(root / "record", demo_db)
    clean = _governed(root, bundle, "clean", None)
    other = _governed(root, bundle, "other-patient", "other_patient")
    look_alike = _governed(root, bundle, "look-alike", "look_alike")
    checked = _repair(root, bundle, "repair-with-record-check")
    no_verifier = _repair(root, bundle, "repair-no-verifier", verifier=False)
    demo = _repair(root, bundle, "repair-demo-profile", profile="demo")
    checks = {
        "demonstration_saved_one_row": _rows(demo_db) == [(PATIENT[0], DEMO_NOTE)],
        "bundle_certified": compiled["certified_clinical_write"],
        "save_identity_armed": compiled["save_identity_armed"],
        "steps_carry_atspi_locators": all(
            s["structural"] for s in compiled["steps"] if s["action"] == "click"
        ),
        "governed_run_verified": clean["status"] == "VERIFIED",
        "governed_run_one_new_row": clean["rows"] == [(PATIENT[0], RUN_NOTE)],
        "other_patient_stopped_before_save": other["status"] != "VERIFIED"
        and other["rows"] == [],
        "look_alike_stopped_before_save": look_alike["status"] != "VERIFIED"
        and look_alike["rows"] == [],
        "repair_resumed": checked["status"] == "resumed",
        "repair_resume_verified": any(
            s["stage"] == "resume" and s["passed"] for s in checked["stages"]
        ),
        "repair_exactly_one_new_row": checked["rows"] == [(PATIENT[0], REPAIR_NOTE)],
        "click_admitted_on_atspi_identity": any(
            s["stage"] == "patch"
            and "admitted by element_and_record_check" in s["detail"]
            for s in checked["stages"]
        ),
        "repair_fault_probe_stopped_before_save": any(
            s["stage"] == "campaign" and s["passed"] for s in checked["stages"]
        ),
        "one_person_decision": checked["person_decisions"] == 1,
        "no_verifier_refused": no_verifier["status"] == "refused",
        "no_verifier_wrote_nothing": no_verifier["rows"] == [],
        "demo_profile_click_not_admitted": demo["status"] != "resumed"
        and "none covers this workflow" in json.dumps(demo),
        "demo_profile_wrote_nothing": demo["rows"] == [],
    }
    return {
        "schema": "openadapt.linux-atspi-qualification/v1",
        "backend": "LinuxBackend (AT-SPI, X11)",
        "app": "tests/native_apps/chart_gtk.py (GTK3, SQLite)",
        "display": os.environ.get("DISPLAY"),
        "at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": [
            line
            for line in _git("status", "--porcelain", "--", ".").splitlines()
            if "repair_every_backend/results/" not in line
        ],
        "compiled": compiled,
        "governed_clean": clean,
        "governed_other_patient": other,
        "governed_look_alike": look_alike,
        "repair_with_record_check": checked,
        "repair_without_verifier": no_verifier,
        "repair_under_demo_profile": demo,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the work folder")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="oa-linux-atspi-"))
    saved_env = dict(os.environ)
    try:
        report = qualify(root)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        if args.keep:
            print(f"work folder: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    out = (
        args.output or RESULTS / f"linux-atspi-{datetime.now(timezone.utc):%Y%m%d}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"linux atspi: {'PASSED' if report['passed'] else 'FAILED'} -> {out}")
    for name, ok in report["checks"].items():
        print(f"  {'ok ' if ok else 'BAD'} {name}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
