"""Rollback on a Windows lab VM, native or over RDP (opt-in, lab only).

The in-tree Patient Notes WinForms app (SQLite ground truth) on the WinRM lab:

1. record the task (search a patient, type over the note, Save) and its
   undo (the same patient, type the old note back, Save) through the
   transport under test;
2. compile both. The undo becomes the save's approved ``restore``
   compensation (``UndoRestore``): before the save the run keeps the note's
   value, and the undo types that value back. A later step that can't run
   stops the run after the save;
3. **after the save**: the rollback reads the whole database, starts the
   compensation from the run's start screen (``rollback.screen_fresh_session``),
   runs it on the same backend, and proves the database matches the read
   from before the run;
4. **screen covered**: the same stop, but Notepad covers the app before the
   compensation: the run ends ``RECONCILIATION_REQUIRED`` with the note there;
5. **empty before**: the note was empty before the run. The app reports
   "Saved" for an empty note but writes nothing, so the restore can't be
   proven: the run must end ``RECONCILIATION_REQUIRED``, never rolled back.

Every verdict is checked again against the guest's SQLite file. The lab
database is a test copy only these runs write (``exclusive``).

Usage (from packages/flow, with the lab ``.env`` loaded and ``winrm_lab`` on
the path)::

    python scripts/desktop/qualify_lab_rollback.py --transport native \\
        --out ~/oa/scratch/winlab/runs/rollback-native-1 --evidence ev.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import winrm_lab  # noqa: E402

GUEST = r"C:\oa"
PY = r"C:\Program Files\Python312-arm64\python.exe"
APP = rf"{GUEST}\patient_notes.ps1"
DB = rf"{GUEST}\patients.db"
SEARCH = "Neil"
NOTE = "BP 128/82; follow-up in 2 weeks"
#: The note patient 1 holds before every run; the undo writes it back.
ORIGINAL = "Allergies reviewed; none known"
CONTROLS = ("searchBox", "searchButton", "noteBox", "saveButton")
WIDTH, HEIGHT = 1280, 800


class GuestDatabase:
    """The guest's SQLite file, copied to the host on every connect."""

    def __init__(self, lab: winrm_lab.WinRMLab, workdir: Path) -> None:
        self.lab = lab
        self.workdir = workdir
        self.reads = 0

    def pull(self) -> Path:
        out = self.lab.run_ps(
            f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{DB}'))"
        ).stdout
        self.reads += 1
        path = self.workdir / f"patients-{self.reads}.db"
        path.write_bytes(base64.b64decode("".join(out.split())))
        return path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.pull().resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def rows(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY id")]
        finally:
            conn.close()


def _fresh_app(
    lab: winrm_lab.WinRMLab, *, settle_s: float = 8.0, note: str = ORIGINAL
) -> None:
    """A new app process on a reseeded database (with ``note``: patient 1
    already holds it)."""
    lab.stop_interactive("OAFlowApp")
    lab.stop_interactive("OAFlowForeign")
    lab.run_ps(
        "Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue", check=False
    )
    lab.run_ps(
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -like '*patient_notes.ps1*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
        check=False,
    )
    lab.run_ps(f"& '{PY}' '{GUEST}\\pn_db.py' seed --drift none")
    if note:
        b64 = base64.b64encode(note.encode("utf-8")).decode("ascii")
        lab.run_ps(f"& '{PY}' '{GUEST}\\pn_db.py' save 1 {b64}")
    lab.run_ps(
        f"Set-Content -Encoding ascii -Path '{GUEST}\\pn_env.json' "
        '-Value \'{"window": "maximized"}\''
    )
    lab.launch_interactive(
        f"powershell.exe -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File {APP}",
        name="OAFlowApp",
    )
    time.sleep(settle_s)


def _cover_app(lab: winrm_lab.WinRMLab) -> None:
    lab.launch_interactive(
        "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden "
        f"-File {GUEST}\\foreign_window.ps1",
        name="OAFlowForeign",
    )
    time.sleep(8)


def _locate(agent, locator, *, timeout_s: float = 45.0):
    deadline = time.monotonic() + timeout_s
    while True:
        handle = agent.locate_structural(locator)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(0.25)


def _points(agent) -> dict[str, tuple[int, int]]:
    from openadapt_flow.ir import StructuralLocator

    points = {}
    for aid in CONTROLS:
        handle = _locate(agent, StructuralLocator(automation_id=aid), timeout_s=120)
        assert handle is not None, f"UIA did not find {aid}"
        points[aid] = (int(handle.point[0]), int(handle.point[1]))
    return points


def _open_patient(rec, points) -> None:
    # Replace whatever the search box holds: a stop leaves the run's search.
    rec.click(*points["searchBox"])
    rec.press("End")
    rec.hotkey(["shift"], "Home")
    rec.type_text(SEARCH)
    rec.press("Enter")
    time.sleep(0.8)
    rec.click(*points["searchButton"])
    time.sleep(0.8)
    rec.click(*points["noteBox"])


def _replace_note(rec, text: str) -> None:
    """Select the whole note and type over it."""
    rec.hotkey(["ctrl"], "End")
    rec.hotkey(["ctrl", "shift"], "Home")
    rec.type_text(text)


def _note_is(value: Any, *, timeout_s: float = 20.0):
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={
            "first": ValueExpr(literal="Neil"),
            "last": ValueExpr(literal="Sorenson"),
        },
        field="note",
        value=value,
        risk="irreversible",
        timeout_s=timeout_s,
    )


def build_bundle(run_rec: Path, undo_rec: Path, bundle: Path):
    """The task with its save checked, a later step that stops the run, and
    the undo as the save's approved restore."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import (
        ActionKind,
        Postcondition,
        PostconditionKind,
        Step,
        UndoRestore,
        UndoStep,
        Workflow,
    )
    from openadapt_flow.runtime.effects.effect import ValueExpr

    task = compile_recording(run_rec, bundle / "task", name="patient-notes-rollback")
    undo = compile_recording(undo_rec, bundle / "undo", name="patient-notes-undo")
    save = task.steps[-1]
    save.risk = "irreversible"
    save.effects = [_note_is(NOTE)]
    undo_steps = [s.model_copy(deep=True) for s in undo.steps]
    # The demonstrated old note becomes the value the run keeps before its save.
    typed = [
        s
        for s in undo_steps
        if s.action == ActionKind.TYPE
        and (s.text == ORIGINAL or undo.params.get(s.param or "") == ORIGINAL)
    ]
    assert len(typed) == 1, "the undo recording didn't type the old note once"
    typed[0].text, typed[0].param = None, "undo_note"
    undo_steps[-1].risk = "irreversible"
    undo_steps[-1].effects = [_note_is(ValueExpr(param="undo_note"))]
    # A later step that never shows its result: the run stops after its save.
    stop = Step(
        id="sign_chart",
        intent="sign the chart",
        action=ActionKind.KEY,
        key="F2",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Chart signed", timeout_s=3.0
            )
        ],
    )
    workflow = task.model_copy(
        update={
            "steps": [*task.steps, stop],
            "undo_steps": [
                UndoStep(
                    id="undo-note",
                    undoes=save.id,
                    intent="write back the note the run replaced",
                    source="demonstration",
                    kind="restore",
                    restore=UndoRestore(
                        match={"first": "Neil", "last": "Sorenson"},
                        fields={"note": "undo_note"},
                    ),
                    steps=undo_steps,
                )
            ],
            "manifest": None,
        }
    )
    workflow.save(bundle)
    return Workflow.load(bundle)


def _rdp_backend():
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    transport = AardwolfTransport.from_credentials(
        os.environ["OAFLOW_WINLAB_HOST"],
        os.environ["OAFLOW_WINLAB_USER"],
        os.environ["OAFLOW_WINLAB_PASSWORD"],
        width=WIDTH,
        height=HEIGHT,
    )
    return FreeRDPBackend(transport, max_frame_age_s=5.0)


def run_case(
    name: str,
    *,
    backend,
    workflow,
    bundle: Path,
    out: Path,
    guest: GuestDatabase,
    lab: winrm_lab.WinRMLab,
    cover: bool = False,
) -> dict[str, Any]:
    from openadapt_flow.rollback import (
        ReplayerRunner,
        RollbackPlan,
        screen_fresh_session,
    )
    from openadapt_flow.rollback_state import DatabaseReader
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    run_dir = out / f"run-{name}"

    def verifier() -> SqlRecordVerifier:
        return SqlRecordVerifier(guest.connect, "SELECT * FROM patients")

    def replayer() -> Replayer:
        return Replayer(backend, effect_verifier=verifier(), use_structural=True)

    reader = DatabaseReader(
        dialect="sqlite",
        read_config={"sqlite_database": "guest"},
        connect=guest.connect,
    )
    plan = RollbackPlan(reader, exclusive=True)
    seeded = guest.rows()
    before = plan.arm({})
    assert before is not None, "the guest database couldn't be read"
    report = replayer().run(workflow, params={}, bundle_dir=bundle, run_dir=run_dir)
    at_stop = guest.rows()
    reset = screen_fresh_session(backend, workflow, bundle_dir=bundle, run_dir=run_dir)
    assert reset is not None, "the workflow has no start screen to check"
    if cover:

        def covered_reset() -> None:
            _cover_app(lab)
            reset()

        fresh = covered_reset
    else:
        fresh = reset
    report = plan.finish(
        report,
        workflow,
        before=before,
        run_dir=run_dir,
        bundle_dir=bundle,
        runner=ReplayerRunner(replayer, fresh),
    )
    after = guest.rows()
    rollback = report.rollback
    return {
        "run_stopped_after_save": bool(
            not report.success
            and next(r for r in at_stop if r["id"] == 1)["note"] == NOTE
        ),
        "rollback_status": rollback.status if rollback else None,
        "rollback_reason": (rollback.reason if rollback else "")[:300],
        "steps": [
            {"step": s.step_id, "decision": s.decision, "reason": s.reason[:200]}
            for s in (rollback.steps if rollback else [])
        ],
        "transaction_outcome": report.transaction_outcome,
        "database_back": after == seeded,
        "note_left": next(r for r in after if r["id"] == 1)["note"] == NOTE,
    }


def run_trial(transport: str, out: Path, row: dict[str, Any]) -> None:
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo

    row.update(
        {
            "transport": transport,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
            ).stdout.strip(),
        }
    )
    lab = winrm_lab.WinRMLab()
    tools = Path(winrm_lab.__file__).resolve().parent
    for name in ("pn_db.py", "patient_notes.ps1", "foreign_window.ps1"):
        local = next(p for p in (HERE / name, tools / name) if p.exists())
        lab.push_file(local, rf"{GUEST}\{name}")
    rdp = _rdp_backend() if transport == "rdp" else None
    if rdp is not None:
        rdp.screenshot()
        time.sleep(8)  # the session moves from the console to RDP
    endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
    agent = endpoint.backend()
    (Path.home() / "oa/scratch/winlab/agent_pin.txt").write_text(
        endpoint.pin_fingerprint or ""
    )
    backend = rdp if rdp is not None else agent
    work = Path(tempfile.mkdtemp(prefix="pn-db-", dir=out))
    guest = GuestDatabase(lab, work)
    try:
        _fresh_app(lab)
        points = _points(agent)

        def task(rec) -> None:
            _open_patient(rec, points)
            _replace_note(rec, NOTE)
            rec.click(*points["saveButton"])
            time.sleep(1.5)

        def undo(rec) -> None:
            _open_patient(rec, points)
            _replace_note(rec, ORIGINAL)
            rec.click(*points["saveButton"])
            time.sleep(1.5)

        task_rec = record_desktop_demo(backend, out / "recording-task", task)
        row["recorded"] = {
            "task_saved_note": next(r for r in guest.rows() if r["id"] == 1)["note"]
            == NOTE
        }
        _fresh_app(lab, note=NOTE)
        points = _points(agent)
        undo_rec = record_desktop_demo(backend, out / "recording-undo", undo)
        row["recorded"]["undo_restored_note"] = (
            next(r for r in guest.rows() if r["id"] == 1)["note"] == ORIGINAL
        )
        assert all(row["recorded"].values()), f"a demonstration failed: {row}"
        bundle = out / "bundle"
        workflow = build_bundle(task_rec, undo_rec, bundle)
        cases = {}
        for name, cover, before_note in (
            ("after-save", False, ORIGINAL),
            ("screen-covered", True, ORIGINAL),
            ("empty-before", False, ""),
        ):
            _fresh_app(lab, note=before_note)
            _points(agent)
            cases[name] = run_case(
                name,
                backend=backend,
                workflow=workflow,
                bundle=bundle,
                out=out,
                guest=guest,
                lab=lab,
                cover=cover,
            )
        row["cases"] = cases
    finally:
        if rdp is not None:
            rdp.close()
        lab.stop_interactive("OAFlowForeign")
    good, covered = cases["after-save"], cases["screen-covered"]
    empty = cases["empty-before"]
    row["accepted"] = bool(
        good["run_stopped_after_save"]
        and good["rollback_status"] == "rolled_back"
        and good["transaction_outcome"] == "ROLLED_BACK"
        and good["database_back"]
        and covered["run_stopped_after_save"]
        and covered["rollback_status"] == "not_rolled_back"
        and covered["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        and covered["note_left"]
        and empty["run_stopped_after_save"]
        and empty["rollback_status"] == "not_rolled_back"
        and empty["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        and empty["note_left"]
    )
    # A rollback that claims success with the note still there.
    row["false_rolled_back"] = any(
        c["rollback_status"] == "rolled_back" and not c["database_back"]
        for c in cases.values()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transport", choices=("native", "rdp"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    args.out.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {}
    try:
        run_trial(args.transport, args.out, row)
    except Exception as exc:  # noqa: BLE001 - recorded as a rejected trial
        row["accepted"] = False
        row["failure"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        (args.out / "traceback.txt").write_text(traceback.format_exc())
    row["completed_at"] = datetime.now(timezone.utc).isoformat()
    rows = json.loads(args.evidence.read_text()) if args.evidence.exists() else []
    rows.append(row)
    args.evidence.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    print(json.dumps(row, indent=2, sort_keys=True))
    return 0 if row.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
