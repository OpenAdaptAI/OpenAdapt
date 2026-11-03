"""Patient Notes over real RDP to the UTM Windows lab, rdp_avc_real's protocol.

Same workflow, conditions, counts and judging as
``benchmark/rdp_avc_real`` (Patient Notes arm, 70 runs), so the false-stop
numbers compare; the difference is the transport. There, the stock mstsc
client on a Windows client machine drove a Windows Server 2022 session with
H.264/AVC444. Here, the headless pure-Python ``aardwolf`` client on a Mac
drives a Windows 11 ARM session (no AVC444), through ``FreeRDPBackend``.

Record once at 1280x800 / 32 bpp, compile, then replay each condition. Each
run gets a fresh note value; the database, read outside the session over
WinRM, decides the outcome with rdp_avc_real's classes.

Usage (from packages/flow, the lab ``.env`` loaded, the desktop lease held)::

    python benchmark/rdp_utm_lab/run_pn.py --out ~/oa/scratch/winlab/runs/rdp-pn
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP_SCRIPTS = HERE.parent.parent / "scripts" / "desktop"
sys.path.insert(0, str(DESKTOP_SCRIPTS))

import qualify_lab_native as native  # noqa: E402
import winrm_lab  # noqa: E402

TARGET_ID = 1
# rdp_avc_real's Patient Notes conditions and counts.
CONDITIONS = [
    {"name": "healthy", "n": 20},
    {"name": "reorder", "n": 10, "drift": "reorder"},
    {"name": "bpp16", "n": 10, "bpp": 16},
    {"name": "decoy", "n": 10, "drift": "decoy"},
    {"name": "siblings", "n": 10, "drift": "siblings"},
    {"name": "desktop1440", "n": 10, "desktop": (1440, 900)},
]
# Conditions under which the target stays unambiguous: a stop is a false stop.
MUST_SAVE = {"healthy", "reorder", "decoy", "bpp16", "desktop1440"}
RECORD_DESKTOP = (1280, 800)


def classify(db_outcome: str, success: bool, must_save: bool) -> str:
    """rdp_avc_real's outcome classes, unchanged."""
    if db_outcome == "correct":
        return "verified_save" if success else "stopped_after_save"
    if db_outcome == "wrong_target":
        return "silent_wrong" if success else "flagged_wrong"
    if success:
        return "false_completion"
    return "false_stop" if must_save else "stop_on_lookalike"


def _rdp(desktop: tuple[int, int], bpp: int):
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    transport = AardwolfTransport.from_credentials(
        os.environ["OAFLOW_WINLAB_HOST"],
        os.environ["OAFLOW_WINLAB_USER"],
        os.environ["OAFLOW_WINLAB_PASSWORD"],
        width=desktop[0],
        height=desktop[1],
        color_depth=None if bpp == 32 else bpp,
    )
    backend = FreeRDPBackend(transport, max_frame_age_s=5.0)
    backend.screenshot()
    time.sleep(6)  # the desktop session moves to this connection
    return backend


def _load1() -> float:
    return os.getloadavg()[0]


def _hold_for_load(limit: float) -> float:
    """Wait until the host's 1-minute load is under ``limit``; return the wait."""
    started = time.monotonic()
    while _load1() >= limit:
        time.sleep(15)
    return round(time.monotonic() - started, 1)


def _seed_drift(lab, drift: str) -> None:
    lab.run_ps(f"& '{native.PY}' '{native.GUEST}\\pn_db.py' seed --drift {drift}")


def main() -> int:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import StructuralLocator
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", help="comma-separated condition names")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse --out's recording and bundle and skip the runs already in "
        "trials.json (the workflow under test stays the same)",
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=35.0,
        help="hold each run until the host's 1-minute load is below this",
    )
    args = parser.parse_args()
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    lab = winrm_lab.WinRMLab()
    for name in ("pn_db.py", "patient_notes.ps1"):
        lab.push_file(DESKTOP_SCRIPTS / name, rf"{native.GUEST}\{name}")

    meta = {
        "protocol": "benchmark/rdp_avc_real, Patient Notes arm",
        "transport": "aardwolf 0.2.14 (pure-Python RDP), Mac host -> UTM Windows 11 ARM",
        "codec": "RDP bitmap codecs negotiated by aardwolf; not H.264/AVC444",
        "candidate_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
        ).stdout.strip(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))

    session = (RECORD_DESKTOP, 32)
    rdp = _rdp(*session)
    endpoint = lab.launch_agent(token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"])
    agent = endpoint.backend()
    (Path.home() / "oa/scratch/winlab/agent_pin.txt").write_text(
        endpoint.pin_fingerprint or ""
    )

    def fresh(drift: str) -> None:
        native._fresh_app(lab)
        if drift != "none":
            # Restart on the drifted roster so the grid shows it.
            lab.stop_interactive("OAFlowApp")
            lab.run_ps(
                "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                "Where-Object { $_.CommandLine -like '*patient_notes.ps1*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
                check=False,
            )
            _seed_drift(lab, drift)
            lab.launch_interactive(
                "powershell.exe -STA -ExecutionPolicy Bypass -WindowStyle Hidden "
                f"-File {native.APP}",
                name="OAFlowApp",
            )
        native._ready(agent)

    try:
        fresh("none")
        points = {}
        for aid in ("searchBox", "searchButton", "noteBox", "saveButton"):
            handle = native._locate(agent, StructuralLocator(automation_id=aid))
            assert handle is not None, aid
            points[aid] = (int(handle.point[0]), int(handle.point[1]))
        # The grid's first data row, below its header (fixed WinForms layout).
        sx, sy = points["searchBox"]
        points["firstRow"] = (sx - 160, sy + 91)
        (out / "points.json").write_text(json.dumps(points))
        native._save_png(rdp, out / "record_start.png")

        if args.resume and (out / "bundle" / "workflow.json").exists():
            from openadapt_flow.ir import Workflow

            workflow = Workflow.load(out / "bundle")
        else:
            rec = Recorder(
                rdp,
                out / "recording",
                settle_interval_s=0.3,
                settle_stable_frames=2,
                settle_timeout_s=6.0,
            )
            rec.click(*points["searchBox"])
            rec.type_text("Neil")
            rec.click(*points["searchButton"])
            rec.click(*points["firstRow"])
            rec.click(*points["noteBox"])
            rec.type_text("recorded demonstration note", param="note")
            rec.click(*points["saveButton"])
            rec.finish()
            workflow = compile_recording(
                out / "recording", out / "bundle", name="utm-rdp-patient-notes"
            )

        trials: list[dict] = (
            json.loads((out / "trials.json").read_text())
            if args.resume and (out / "trials.json").exists()
            else []
        )
        done = len(trials)
        counter = 0
        only = set(args.only.split(",")) if args.only else None
        for cond in CONDITIONS:
            name = cond["name"]
            if only and name not in only:
                continue
            if counter + cond["n"] <= done:
                counter += cond["n"]  # every run of it finished earlier
                continue
            want = (tuple(cond.get("desktop", RECORD_DESKTOP)), cond.get("bpp", 32))
            if want != session:
                rdp.close()
                time.sleep(3)
                rdp = _rdp(*want)
                session = want
            for i in range(cond["n"]):
                counter += 1
                if counter <= done:
                    continue  # finished in an earlier invocation
                drift = cond.get("drift", "none")
                held_s = _hold_for_load(args.max_load)
                fresh(drift)
                load_start = round(_load1(), 1)
                token = f"OaUtm{len(trials):03d}{name[:4]}"
                run_dir = out / f"run_{len(trials):03d}_{name}"
                first = rdp.screenshot()
                started = time.monotonic()
                error = None
                try:
                    report = Replayer(rdp, poll_interval_s=0.3).run(
                        workflow,
                        params={"note": token},
                        bundle_dir=out / "bundle",
                        run_dir=run_dir,
                    )
                    success, model_calls = bool(report.success), report.model_calls
                    stopped = [r for r in report.results if not r.ok]
                    stop = (
                        {
                            "step_id": stopped[0].step_id,
                            "intent": stopped[0].intent,
                            "error": (stopped[0].error or "")[:400],
                            "safety_halt": stopped[0].safety_halt,
                        }
                        if stopped
                        else None
                    )
                    rungs = dict(report.rung_counts)
                except Exception as exc:  # noqa: BLE001 - a crash is a stop
                    success, model_calls, stop, rungs = False, 0, None, {}
                    error = repr(exc)[:400]
                elapsed = time.monotonic() - started
                load_end = round(_load1(), 1)
                rows = native._db_rows(lab)
                holders = [r["id"] for r in rows if r["note"] == token]
                db_outcome = (
                    "correct"
                    if holders == [TARGET_ID]
                    else "wrong_target"
                    if holders
                    else "no_write"
                )
                must_save = name in MUST_SAVE
                trial = {
                    "trial": len(trials) + 1,
                    "condition": name,
                    "drift": drift,
                    "bpp": want[1],
                    "desktop": list(want[0]),
                    "success": success,
                    "error": error,
                    "stop": stop,
                    "model_calls": model_calls,
                    "rung_counts": rungs,
                    "db_outcome": db_outcome,
                    "note_holders": holders,
                    "class": classify(db_outcome, success, must_save),
                    "must_save": must_save,
                    "elapsed_s": round(elapsed, 2),
                    "held_for_load_s": held_s,
                    "load1_start": load_start,
                    "load1_end": load_end,
                    "first_frame_sha256": hashlib.sha256(first).hexdigest(),
                }
                trials.append(trial)
                (out / "trials.json").write_text(json.dumps(trials, indent=2))
                print(
                    trial["trial"],
                    name,
                    trial["class"],
                    round(elapsed, 1),
                    (stop or {}).get("error", "")[:120],
                    flush=True,
                )
    except Exception:  # noqa: BLE001 - keep what ran
        (out / "traceback.txt").write_text(traceback.format_exc())
        raise
    finally:
        rdp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
