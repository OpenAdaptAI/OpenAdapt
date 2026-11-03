"""Native Windows lifecycle qualification on a WinRM lab VM (opt-in, lab only).

One trial on the in-tree Patient Notes WinForms app (SQLite ground truth):

1. start ``win_agent`` on the signed-in desktop (pinned TLS + bearer token);
2. record a demonstration through ``WindowsBackend`` (UIA locators armed per
   click), including a click in the app's own "Confirm save" dialog;
3. compile it and require full structural coverage;
4. replay on a fresh app and database, then check the database directly;
5. read each control's UIA identity through ``element_identity_at``;
6. replay with an unexpected "Session notice" dialog after Search: the run
   must stop and the database must not change;
7. refuse a stale target (same locator, new window) and an ambiguous one
   (two identical windows) with the database unchanged.

The result row is PHI-free (synthetic data, bounded verdicts) and is appended
to ``--evidence``. Screens, receipts and the report stay in ``--out``.

Usage (from packages/flow, with the lab ``.env`` loaded)::

    python scripts/desktop/qualify_lab_native.py --trial 1 \
        --out ~/oa/scratch/winlab/runs/native-1 --evidence evidence.json
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

GUEST = r"C:\oa"
PY = r"C:\Program Files\Python312-arm64\python.exe"
APP = rf"{GUEST}\patient_notes.ps1"
DEMO_SEARCH = "Neil"
DEMO_NOTE = "BP 128/82; follow-up in 2 weeks"
CONTROLS = ("searchBox", "searchButton", "noteBox", "saveButton")


def _db_rows(lab: winrm_lab.WinRMLab) -> list[dict]:
    out = lab.run_ps(f"& '{PY}' '{GUEST}\\pn_db.py' all").stdout
    for line in reversed(out.splitlines()):
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    raise AssertionError("Patient Notes DB returned no JSON rows")


def _app_env(lab: winrm_lab.WinRMLab, **knobs: Any) -> None:
    config = {"window": "maximized", **knobs}
    lab.run_ps(
        f"Set-Content -Encoding ascii -Path '{GUEST}\\pn_env.json' "
        f"-Value '{json.dumps(config)}'"
    )


def _fresh_app(lab: winrm_lab.WinRMLab, *, settle_s: float = 8.0, **knobs) -> None:
    lab.stop_interactive("OAFlowApp")
    lab.run_ps(
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -like '*patient_notes.ps1*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
        check=False,
    )
    lab.run_ps(f"& '{PY}' '{GUEST}\\pn_db.py' seed --drift none")
    _app_env(lab, **knobs)
    lab.launch_interactive(
        f"powershell.exe -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File {APP}",
        name="OAFlowApp",
    )
    time.sleep(settle_s)


def _extra_window(lab: winrm_lab.WinRMLab, *, settle_s: float = 8.0) -> None:
    lab.launch_interactive(
        f"powershell.exe -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File {APP}",
        name="OAFlowApp2",
    )
    time.sleep(settle_s)


def _locate(backend, locator, *, timeout_s: float = 45.0):
    deadline = time.monotonic() + timeout_s
    while True:
        handle = backend.locate_structural(locator)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(0.25)


def _ready(backend) -> tuple[int, int]:
    """Wait until the app's search box is reachable; return its point."""
    from openadapt_flow.ir import StructuralLocator

    handle = _locate(backend, StructuralLocator(automation_id="searchBox"))
    if handle is None:
        raise AssertionError("Patient Notes did not come up")
    return int(handle.point[0]), int(handle.point[1])


def _save_png(backend, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(backend.screenshot())


def run_trial(trial: int, out: Path, row: dict[str, Any]) -> dict[str, Any]:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.backend import StructuralResolutionRefused
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import StructuralLocator, Workflow
    from openadapt_flow.runtime import Replayer

    row.update(
        {
            "trial": trial,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
            ).stdout.strip(),
            "steps": {},
        }
    )
    steps = row["steps"]
    lab = winrm_lab.WinRMLab()
    endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
    backend = endpoint.backend()
    # The per-run pin, for lab tools that look at the screen afterwards.
    (Path.home() / "oa/scratch/winlab/agent_pin.txt").write_text(
        endpoint.pin_fingerprint or ""
    )
    caps = backend.agent_capabilities()
    steps["agent"] = {
        "tls_pinned": endpoint.url.startswith("https://"),
        "typed_contract": sorted(c for c in caps if c.endswith("_v1")),
        "legacy_exec_exposed": "legacy_exec" in caps,
    }
    for name in ("pn_db.py", "patient_notes.ps1"):
        lab.push_file(HERE / name, rf"{GUEST}\{name}")

    # Record, with the owned "Confirm save" dialog in the flow.
    _fresh_app(lab, confirm_save=True)
    _ready(backend)
    _save_png(backend, out / "screens" / "01-app.png")
    points = {}
    for aid in CONTROLS:
        handle = _locate(backend, StructuralLocator(automation_id=aid))
        assert handle is not None, f"UIA did not find {aid}"
        points[aid] = (int(handle.point[0]), int(handle.point[1]))
    identities = {
        aid: backend.element_identity_at(*point) for aid, point in points.items()
    }
    steps["element_identity"] = {
        aid: (
            {k: value[k] for k in ("role", "automation_id", "window", "process")}
            | {"ancestry_depth": len(value["ancestry"])}
            if value
            else None
        )
        for aid, value in identities.items()
    }

    def driver(rec) -> None:
        rec.click(*points["searchBox"])
        rec.type_text(DEMO_SEARCH)
        rec.press("Enter")
        time.sleep(0.6)
        rec.click(*points["searchButton"])
        time.sleep(0.6)
        rec.click(*points["noteBox"])
        rec.type_text(DEMO_NOTE)
        rec.click(*points["saveButton"])
        time.sleep(1.0)
        yes = _locate(backend, StructuralLocator(role="button", name="Yes"))
        assert yes is not None, "UIA did not find the Confirm save dialog's Yes"
        steps["owned_dialog"] = {
            "yes_identity": (
                lambda v: (
                    {k: v[k] for k in ("role", "name", "window", "process")}
                    | {"ancestry": v["ancestry"]}
                    if v
                    else None
                )
            )(backend.element_identity_at(*yes.point))
        }
        rec.click(int(yes.point[0]), int(yes.point[1]))
        time.sleep(1.0)

    recording = record_desktop_demo(backend, out / "recording", driver)
    recorded_rows = _db_rows(lab)
    steps["record"] = {
        "demo_wrote_note": next(r for r in recorded_rows if r["id"] == 1)["note"]
        == DEMO_NOTE
    }
    bundle = out / "bundle"
    workflow = compile_recording(recording, bundle, name="patient-notes-lab")
    coverage = structural_armed_coverage(workflow)
    steps["compile"] = {"coverage": coverage, "steps": len(workflow.steps)}

    # Healthy replay on a fresh app and database.
    _fresh_app(lab, confirm_save=True)
    _ready(backend)
    before = _db_rows(lab)
    report = Replayer(backend, use_structural=True).run(
        Workflow.load(bundle), params={}, bundle_dir=bundle, run_dir=out / "run-healthy"
    )
    after = _db_rows(lab)
    _save_png(backend, out / "screens" / "02-after-healthy.png")
    target = next(r for r in after if r["id"] == 1)
    steps["replay_healthy"] = {
        "success": report.success,
        "rung_counts": report.rung_counts,
        "model_calls": report.model_calls,
        "total_ms": report.total_ms,
        "all_empty_before": all(r["note"] == "" for r in before),
        "target_note_written": target["note"] == DEMO_NOTE,
        "siblings_untouched": all(r["note"] == "" for r in after if r["id"] != 1),
        "actuations": [r.actuation for r in report.results],
        "errors": [r.error for r in report.results if r.error],
    }

    # Unexpected window: an owned "Session notice" appears after Search.
    _fresh_app(lab, confirm_save=True, interrupt="after_search")
    _ready(backend)
    before = _db_rows(lab)
    report = Replayer(backend, use_structural=True).run(
        Workflow.load(bundle),
        params={},
        bundle_dir=bundle,
        run_dir=out / "run-unexpected",
    )
    _save_png(backend, out / "screens" / "03-unexpected.png")
    after = _db_rows(lab)
    stopped = [r for r in report.results if not r.ok]
    steps["unexpected_window"] = {
        "success": report.success,
        "stopped_step": stopped[0].step_id if stopped else None,
        "stopped_error": (stopped[0].error or "")[:300] if stopped else None,
        "safety_halt": bool(stopped and stopped[0].safety_halt),
        "database_unchanged": after == before,
    }

    # Unexpected window from another process: Notepad maximized over the app
    # before the run. UIA can still reach the covered controls, so this is
    # where a structural replay must not act through someone else's window.
    _fresh_app(lab, confirm_save=True)
    _ready(backend)
    before = _db_rows(lab)
    search_point = _ready(backend)
    lab.launch_interactive("notepad.exe", name="OAFlowForeign")
    time.sleep(4)
    lab.run_ps(
        "Add-Type -Namespace W -Name U -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool ShowWindow(System.IntPtr h, int c);'; "
        "Get-Process notepad | ForEach-Object { [W.U]::ShowWindow($_.MainWindowHandle, 3) } | Out-Null",
        check=False,
    )
    time.sleep(2)
    _save_png(backend, out / "screens" / "04-foreign-window.png")
    cover = backend.element_identity_at(*search_point)
    if not cover or cover.get("process") != "notepad":
        raise AssertionError(
            "foreign-window setup did not cover the app: "
            f"{(cover or {}).get('process')!r} at the search box"
        )
    report = Replayer(backend, use_structural=True).run(
        Workflow.load(bundle),
        params={},
        bundle_dir=bundle,
        run_dir=out / "run-foreign",
    )
    after = _db_rows(lab)
    stopped = [r for r in report.results if not r.ok]
    steps["foreign_window"] = {
        "success": report.success,
        "stopped_step": stopped[0].step_id if stopped else None,
        "stopped_error": (stopped[0].error or "")[:300] if stopped else None,
        "database_unchanged": after == before,
        "actuations": [r.actuation for r in report.results],
    }
    lab.stop_interactive("OAFlowForeign")
    lab.run_ps(
        "Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue", check=False
    )

    # Stale target, then ambiguity, both with the database unchanged.
    _fresh_app(lab)
    _ready(backend)
    rows = _db_rows(lab)
    save = StructuralLocator(automation_id="saveButton")
    stale = _locate(backend, save)
    _fresh_app(lab)
    _ready(backend)
    fresh = _locate(backend, save)
    stale_refused = False
    try:
        backend.act_structural(save, stale)
    except StructuralResolutionRefused as exc:
        stale_refused = "stale" in str(exc)
    steps["stale_refusal"] = {
        "fingerprint_changed": bool(
            stale and fresh and stale.target_fingerprint != fresh.target_fingerprint
        ),
        "refused": stale_refused,
        "database_unchanged": _db_rows(lab) == rows,
    }
    _extra_window(lab)
    ambiguous = False
    try:
        backend.locate_structural(save)
    except StructuralResolutionRefused as exc:
        ambiguous = "ambiguous" in str(exc)
    steps["ambiguity_refusal"] = {
        "refused": ambiguous,
        "database_unchanged": _db_rows(lab) == rows,
    }
    lab.stop_interactive("OAFlowApp2")
    healthy = steps["replay_healthy"]
    row["accepted"] = bool(
        healthy["success"]
        and healthy["target_note_written"]
        and healthy["siblings_untouched"]
        and steps["unexpected_window"]["success"] is False
        and steps["unexpected_window"]["database_unchanged"]
        and steps["foreign_window"]["success"] is False
        and steps["foreign_window"]["database_unchanged"]
        and steps["stale_refusal"]["refused"]
        and steps["ambiguity_refusal"]["refused"]
    )
    row["silent_incorrect_success"] = bool(
        healthy["success"]
        and not (healthy["target_note_written"] and healthy["siblings_untouched"])
    )
    return row


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
