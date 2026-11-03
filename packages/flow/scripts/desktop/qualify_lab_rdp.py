"""RDP lifecycle qualification on the WinRM lab VM (opt-in, lab only).

The same Patient Notes task as ``qualify_lab_native.py``, but every frame
and every input goes over a real RDP connection (``FreeRDPBackend`` on the
pure-Python ``aardwolf`` client): no UI Automation in the run itself.

1. connect over RDP as the lab account (its desktop session moves to RDP);
2. record the demonstration through the RDP backend (pixels only; the
   in-guest agent is used once, before recording, to find where controls
   are, as a person would see them);
3. compile, replay on a fresh app and database, check the database;
4. replay with an unexpected owned dialog after Search, and with Notepad
   pinned over the app: each run must stop and leave the database as it was.

Usage (from packages/flow, with the lab ``.env`` loaded)::

    python scripts/desktop/qualify_lab_rdp.py --trial 1 \
        --out ~/oa/scratch/winlab/runs/rdp-1 --evidence rdp-evidence.json
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

import qualify_lab_native as native  # noqa: E402
import winrm_lab  # noqa: E402

WIDTH, HEIGHT = 1280, 800


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


def _replay(backend, bundle: Path, run_dir: Path):
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    return Replayer(backend).run(
        Workflow.load(bundle), params={}, bundle_dir=bundle, run_dir=run_dir
    )


def _stopped(report) -> dict[str, Any]:
    stopped = [r for r in report.results if not r.ok]
    return {
        "success": report.success,
        "stopped_step": stopped[0].step_id if stopped else None,
        "stopped_error": (stopped[0].error or "")[:300] if stopped else None,
        "safety_halt": bool(stopped and stopped[0].safety_halt),
        "rung_counts": report.rung_counts,
        "model_calls": report.model_calls,
    }


def run_trial(trial: int, out: Path, row: dict[str, Any]) -> None:
    from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import StructuralLocator

    row.update(
        {
            "trial": trial,
            "transport": "aardwolf RDP, 1280x800, NLA (NTLM)",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
            ).stdout.strip(),
            "steps": {},
        }
    )
    steps = row["steps"]
    lab = winrm_lab.WinRMLab()
    for name in ("pn_db.py", "patient_notes.ps1", "foreign_window.ps1"):
        lab.push_file(HERE / name, rf"{native.GUEST}\{name}")
    rdp = _rdp_backend()
    try:
        rdp.screenshot()
        time.sleep(8)  # the session moves from the console to RDP
        steps["rdp"] = {"viewport": list(rdp.viewport)}
        # The agent runs in the same (now remote) session; it only finds
        # where the controls are before recording.
        endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
        agent = endpoint.backend()
        (Path.home() / "oa/scratch/winlab/agent_pin.txt").write_text(
            endpoint.pin_fingerprint or ""
        )

        native._fresh_app(lab, confirm_save=True)
        native._ready(agent)
        points = {}
        for aid in native.CONTROLS:
            handle = native._locate(agent, StructuralLocator(automation_id=aid))
            assert handle is not None, f"UIA did not find {aid}"
            points[aid] = (int(handle.point[0]), int(handle.point[1]))
        native._save_png(rdp, out / "screens" / "01-app-over-rdp.png")

        def driver(rec) -> None:
            rec.click(*points["searchBox"])
            rec.type_text(native.DEMO_SEARCH)
            rec.press("Enter")
            time.sleep(1.0)
            rec.click(*points["searchButton"])
            time.sleep(1.0)
            rec.click(*points["noteBox"])
            rec.type_text(native.DEMO_NOTE)
            rec.click(*points["saveButton"])
            time.sleep(1.5)
            yes = native._locate(agent, StructuralLocator(role="button", name="Yes"))
            assert yes is not None, "Confirm save dialog did not open"
            rec.click(int(yes.point[0]), int(yes.point[1]))
            time.sleep(1.5)

        recording = record_desktop_demo(rdp, out / "recording", driver)
        rows = native._db_rows(lab)
        steps["record"] = {
            "demo_wrote_note": next(r for r in rows if r["id"] == 1)["note"]
            == native.DEMO_NOTE
        }
        bundle = out / "bundle"
        workflow = compile_recording(recording, bundle, name="patient-notes-rdp")
        steps["compile"] = {"steps": len(workflow.steps)}

        native._fresh_app(lab, confirm_save=True)
        native._ready(agent)
        before = native._db_rows(lab)
        report = _replay(rdp, bundle, out / "run-healthy")
        after = native._db_rows(lab)
        native._save_png(rdp, out / "screens" / "02-after-healthy.png")
        target = next(r for r in after if r["id"] == 1)
        steps["replay_healthy"] = _stopped(report) | {
            "target_note_written": target["note"] == native.DEMO_NOTE,
            "siblings_untouched": all(r["note"] == "" for r in after if r["id"] != 1),
            "all_empty_before": all(r["note"] == "" for r in before),
            "total_ms": report.total_ms,
        }

        native._fresh_app(lab, confirm_save=True, interrupt="after_search")
        native._ready(agent)
        before = native._db_rows(lab)
        report = _replay(rdp, bundle, out / "run-unexpected")
        native._save_png(rdp, out / "screens" / "03-unexpected.png")
        steps["unexpected_window"] = _stopped(report) | {
            "database_unchanged": native._db_rows(lab) == before
        }

        native._fresh_app(lab, confirm_save=True)
        native._ready(agent)
        before = native._db_rows(lab)
        lab.launch_interactive(
            "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden "
            f"-File {native.GUEST}\\foreign_window.ps1",
            name="OAFlowForeign",
        )
        time.sleep(8)
        native._save_png(rdp, out / "screens" / "04-foreign-window.png")
        report = _replay(rdp, bundle, out / "run-foreign")
        steps["foreign_window"] = _stopped(report) | {
            "database_unchanged": native._db_rows(lab) == before
        }
        lab.stop_interactive("OAFlowForeign")
        lab.run_ps(
            "Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue",
            check=False,
        )
    finally:
        rdp.close()

    healthy = steps["replay_healthy"]
    row["accepted"] = bool(
        healthy["success"]
        and healthy["target_note_written"]
        and healthy["siblings_untouched"]
        and steps["unexpected_window"]["success"] is False
        and steps["unexpected_window"]["database_unchanged"]
        and steps["foreign_window"]["success"] is False
        and steps["foreign_window"]["database_unchanged"]
    )
    row["silent_incorrect_success"] = bool(
        healthy["success"]
        and not (healthy["target_note_written"] and healthy["siblings_untouched"])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    args.out.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {"trial": args.trial}
    try:
        run_trial(args.trial, args.out, row)
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
