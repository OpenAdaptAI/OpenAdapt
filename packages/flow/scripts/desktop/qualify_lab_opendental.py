"""Open Dental commlog lifecycle on the Windows lab VM, native backend.

Open Dental is a real back-office Windows app (WinForms shell, WPF dialogs,
painted toolbars and grids) on MariaDB. It is a test target only: the trial's
demo database holds fictional patients, and nothing from Open Dental is
copied into this repository.

The workflow: Select Patient, search last name "Smith", pick John Smith (the
second of four Smiths; the first row is auto-selected), OK, Commlog, type a
note, Save. The database decides each run: the newest ``commlog`` row for
John Smith (PatNum 15) must carry this run's note, and no other patient may
gain a commlog. It is read over the lab's read-only MariaDB account.

Usage (from packages/flow, the lab ``.env`` loaded, the desktop lease held)::

    python scripts/desktop/qualify_lab_opendental.py --out ~/oa/scratch/winlab/runs/od-commlog
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import winrm_lab  # noqa: E402

OD_EXE = r"C:\Program Files (x86)\Open Dental\OpenDental.exe"
TARGET_PATNUM = 15  # John Smith in the trial's demo database
SEARCH = "Smith"
# Painted controls have no UIA element; their points are fixed in the
# maximized 1024x768 main window.
SELECT_PATIENT = (110, 61)
COMMLOG = (213, 61)
JOHN_ROW = (140, 85)  # second row of the Smith search


def _db():
    import pymysql  # noqa: PLC0415 - lab-only dependency

    e = os.environ
    return pymysql.connect(
        host=e["OAFLOW_OD_MYSQL_HOST"],
        port=int(e["OAFLOW_OD_MYSQL_PORT"]),
        user=e["OAFLOW_OD_MYSQL_RO_USER"],
        password=e["OAFLOW_OD_MYSQL_RO_PASSWORD"],
        database="demo",
        connect_timeout=5,
    )


def commlogs() -> list[dict[str, Any]]:
    with _db() as conn, conn.cursor() as cur:
        cur.execute("select CommlogNum, PatNum, Note from commlog order by CommlogNum")
        return [{"num": n, "patnum": p, "note": note} for n, p, note in cur.fetchall()]


def judge(before: list[dict], after: list[dict], token: str) -> str:
    """``correct`` | ``wrong_target`` | ``no_write`` | ``other_change``."""
    known = {r["num"] for r in before}
    new = [r for r in after if r["num"] not in known]
    changed = [r for r in after if r["num"] in known and r not in before]
    holders = [r["patnum"] for r in new if token in (r["note"] or "")]
    if changed or len(new) > len(holders):
        return "other_change"
    if holders == [TARGET_PATNUM]:
        return "correct"
    return "wrong_target" if holders else "no_write"


def fresh_app(lab: winrm_lab.WinRMLab, backend, *, timeout_s: float = 90.0) -> None:
    from openadapt_flow.ir import StructuralLocator  # noqa: PLC0415

    lab.stop_interactive("OAFlowOD")
    lab.run_ps(
        "Stop-Process -Name OpenDental -Force -ErrorAction SilentlyContinue",
        check=False,
    )
    time.sleep(2)
    lab.launch_interactive(f'"{OD_EXE}"', name="OAFlowOD")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if backend.locate_structural(StructuralLocator(automation_id="ToolBarMain")):
            time.sleep(3)  # the appointment view finishes painting
            return
        time.sleep(1)
    raise AssertionError("Open Dental did not come up")


def locate(backend, locator, *, timeout_s: float = 20.0):
    deadline = time.monotonic() + timeout_s
    while True:
        handle = backend.locate_structural(locator)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(0.25)


def point_of(backend, automation_id: str) -> tuple[int, int]:
    from openadapt_flow.ir import StructuralLocator  # noqa: PLC0415

    handle = locate(backend, StructuralLocator(automation_id=automation_id))
    assert handle is not None, f"UIA did not find {automation_id}"
    return int(handle.point[0]), int(handle.point[1])


def main() -> int:
    from openadapt_flow.adapters.desktop_recorder import (  # noqa: PLC0415
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.compiler import compile_recording  # noqa: PLC0415
    from openadapt_flow.ir import Workflow  # noqa: PLC0415
    from openadapt_flow.runtime import Replayer  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--healthy", type=int, default=3)
    args = parser.parse_args()
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    lab = winrm_lab.WinRMLab()
    endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
    backend = endpoint.backend()
    (Path.home() / "oa/scratch/winlab/agent_pin.txt").write_text(
        endpoint.pin_fingerprint or ""
    )
    meta: dict[str, Any] = {
        "app": "Open Dental 25.3.48 trial, demo database (fictional patients)",
        "workflow": "Select Patient > Smith > John Smith > Commlog > note > Save",
        "candidate_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
        ).stdout.strip(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }
    steps = meta["steps"]

    def save_meta() -> None:
        (out / "evidence.json").write_text(json.dumps(meta, indent=2, default=str))

    try:
        fresh_app(lab, backend)
        (out / "screens").mkdir(exist_ok=True)
        (out / "screens" / "01-app.png").write_bytes(backend.screenshot())
        identities: dict[str, Any] = {}

        def driver(rec) -> None:
            rec.click(*SELECT_PATIENT)
            lname = point_of(backend, "textLName")
            identities["textLName"] = backend.element_identity_at(*lname)
            rec.click(*lname)
            rec.type_text(SEARCH)
            time.sleep(1.5)  # "Refresh while typing" fills the grid
            rec.click(*JOHN_ROW)
            ok = point_of(backend, "butOK")
            rec.click(*ok)
            time.sleep(2)
            rec.click(*COMMLOG)
            note = point_of(backend, "textNote")
            identities["textNote"] = backend.element_identity_at(*note)
            rec.click(*note)
            rec.type_text("recorded demonstration commlog", param="note")
            save = point_of(backend, "butSave")
            identities["butSave"] = backend.element_identity_at(*save)
            rec.click(*save)
            time.sleep(2)

        before = commlogs()
        recording = record_desktop_demo(
            backend, out / "recording", driver, settle_timeout_s=6.0
        )
        steps["element_identity"] = identities
        steps["record"] = {
            "db": judge(before, commlogs(), "recorded demonstration commlog")
        }
        bundle = out / "bundle"
        workflow = compile_recording(recording, bundle, name="opendental-commlog-lab")
        steps["compile"] = {
            "coverage": structural_armed_coverage(workflow),
            "steps": len(workflow.steps),
            "intents": [s.intent for s in workflow.steps],
        }
        save_meta()

        runs = []
        for i in range(args.healthy):
            fresh_app(lab, backend)
            token = f"OaOd{i:03d}{int(time.time()) % 100000}"
            before = commlogs()
            report = Replayer(backend, use_structural=True).run(
                Workflow.load(bundle),
                params={"note": token},
                bundle_dir=bundle,
                run_dir=out / f"run-healthy-{i}",
            )
            stopped = [r for r in report.results if not r.ok]
            runs.append(
                {
                    "success": report.success,
                    "db": judge(before, commlogs(), token),
                    "rung_counts": report.rung_counts,
                    "model_calls": report.model_calls,
                    "total_ms": report.total_ms,
                    "actuations": [r.actuation for r in report.results],
                    "stop": (
                        {
                            "step_id": stopped[0].step_id,
                            "intent": stopped[0].intent,
                            "error": (stopped[0].error or "")[:400],
                        }
                        if stopped
                        else None
                    ),
                }
            )
            steps["replay_healthy"] = runs
            save_meta()
            print(i, runs[-1]["success"], runs[-1]["db"], runs[-1]["stop"], flush=True)
    except Exception:  # noqa: BLE001 - keep what ran
        (out / "traceback.txt").write_text(traceback.format_exc())
        raise
    finally:
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_meta()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
