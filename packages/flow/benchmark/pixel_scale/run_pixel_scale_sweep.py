#!/usr/bin/env python3
"""Display-scaling and text-rendering sweep for the pixel path.

Records each kiosk once at 100% scaling with ClearType on, compiles it, and
replays the same bundle at 100/125/150/175% scaling with ClearType on and off.
Each point runs twice:

- ``healthy``: the correct record stays active. A correct run books exactly one
  row for Ada Lovelace with the replay values.
- ``wrong_record``: the active record switches to Grace Hopper after the last
  value is typed and before the save. A correct run halts with no write.

For every point the result names the step that stopped the run and the check
that stopped it (layout drift, target resolution, identity, input
verification, or postcondition), with the rung each step resolved through.

The kiosks are the in-process renderings in ``kiosk_render.py``; see the
README for what this does and does not reproduce. No model is called: the
grounder rung is never installed.

Usage::

    python benchmark/pixel_scale/run_pixel_scale_sweep.py --output results_after.json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
# Run against this checkout, not an installed openadapt_flow. The harness is
# loaded by path: an unrelated installed ``benchmark`` package can shadow
# this repository's ``benchmark`` namespace.
for _path in (str(HERE.parents[1]), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from kiosk_render import (  # noqa: E402
    SPECS,
    KioskBackend,
    RenderProfile,
    ScaledKiosk,
    font_path,
)

SCHEMA = "openadapt.pixel-scale-sweep.v1"
SCALES = (1.0, 1.25, 1.5, 1.75)
CLEARTYPE = (True, False)
RECORD_PROFILE = RenderProfile(scale=1.0, cleartype=True)
# Negative control: fonts 25% larger with the layout unchanged. This is a font
# change, not a display-scale change, and a correct run halts.
CONTROLS = (RenderProfile(scale=1.0, cleartype=True, font_scale=1.25),)

DEMO = {
    "rdp_appointments": {
        "appointment_slot": "2026-08-12 09:30",
        "visit_type": "Cardiology follow-up",
        "request_id": "REQ-DEMO-001",
    },
    "canvas_notes": {"note": "followup in two weeks"},
}
DEMO["rdp_appointments_unambiguous_ids"] = DEMO["rdp_appointments"]
REPLAY = {
    "rdp_appointments": {
        "appointment_slot": "2026-08-14 10:45",
        "visit_type": "Intake consultation",
        "request_id": "REQ-LIVE-2048",
    },
    "canvas_notes": {"note": "recheck labs next visit"},
}
REPLAY["rdp_appointments_unambiguous_ids"] = REPLAY["rdp_appointments"]
FIELD_FOR_PARAM = {
    "appointment_slot": "slot",
    "visit_type": "visit_type",
    "request_id": "request_id",
    "note": "note",
}
# Explicit pixel identities, as in benchmark/rdp_ladder: the roster click binds
# to the Ada row, every later click binds to the active-record banner.
RDP_IDENTIFIER_REGIONS = {
    0: (50, 164, 490, 54),
    1: (545, 118, 680, 55),
    3: (545, 118, 680, 55),
    5: (545, 118, 680, 55),
    7: (545, 118, 680, 55),
}


def _record(kiosk_name: str, work: Path) -> tuple[Any, Path]:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder

    kiosk = ScaledKiosk(SPECS[kiosk_name](), RECORD_PROFILE)
    backend = KioskBackend(kiosk)
    rec_dir = work / f"{kiosk_name}_recording"
    bundle_dir = work / f"{kiosk_name}_bundle"
    rec = Recorder(
        backend,
        rec_dir,
        settle_interval_s=0.0,
        settle_stable_frames=2,
        settle_timeout_s=2.0,
    )
    rec.click(*kiosk.logical_center("row0"))
    for param, value in DEMO[kiosk_name].items():
        rec.click(*kiosk.logical_center(FIELD_FOR_PARAM[param]))
        rec.type_text(value, param=param)
    rec.click(*kiosk.logical_center("save"))
    rec.finish()
    if kiosk_name.startswith("rdp_appointments"):
        path = rec_dir / "events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines() if line]
        for index, region in RDP_IDENTIFIER_REGIONS.items():
            assert events[index]["kind"] == "click", events[index]
            events[index]["identifier_region"] = list(region)
        path.write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
            encoding="utf-8",
        )
    workflow = compile_recording(rec_dir, bundle_dir, name=f"pixel-scale-{kiosk_name}")
    return workflow, bundle_dir


def _gate(result: Any) -> str:
    """Name the check that stopped a failed step."""
    error = (result.error or "").lower()
    if "layout drift" in error:
        return "layout_drift"
    if result.identity is not None and result.identity.status != "verified":
        return "identity"
    if result.resolution is None:
        return "target_resolution"
    if result.input_verified is False:
        return "input_verification"
    if result.postconditions_ok is False:
        return "postcondition"
    return "other"


def _replay(
    kiosk_name: str,
    workflow: Any,
    bundle_dir: Path,
    profile: RenderProfile,
    *,
    wrong_record: bool,
    run_dir: Path,
    layout_drift: bool = True,
    render_scale: Optional[bool] = None,
) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer

    # A replay can heal the in-memory workflow it was given, even on a halted
    # run. Load the bundle afresh so every point replays the compiled bundle.
    workflow = Workflow.load(bundle_dir)
    params = REPLAY[kiosk_name]
    last_value = list(params.values())[-1]

    def after_type(kiosk: ScaledKiosk, text: str) -> None:
        if wrong_record and text == last_value:
            kiosk.select_patient(1)

    kiosk = ScaledKiosk(SPECS[kiosk_name](), profile)
    backend = KioskBackend(kiosk, after_type=after_type)
    started = time.monotonic()
    crash: Optional[str] = None
    report = None
    try:
        # ``render_scale`` is passed only when set, so the same harness also
        # measures a checkout from before the display-scale estimate existed.
        options = {} if render_scale is None else {"render_scale": render_scale}
        report = Replayer(
            backend,
            poll_interval_s=0.0,
            pixel_verify_enabled=True,
            layout_drift=layout_drift,
            **options,
        ).run(workflow, params=params, bundle_dir=bundle_dir, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 - a crash is an outcome
        crash = f"{type(exc).__name__}: {exc}"[:300]
    rows = kiosk.state.saved
    expected = {
        "patient_name": "Ada Lovelace",
        "patient_mrn": kiosk.spec.patients[0][1],
        **{FIELD_FOR_PARAM[k]: v for k, v in params.items()},
    }
    correct_write = rows == [expected]
    success = bool(report is not None and report.success)
    if crash is not None:
        outcome = "crash"
    elif wrong_record:
        outcome = (
            "halted"
            if not rows and not success
            else ("wrong_write" if rows else "false_completion")
        )
    elif success and correct_write:
        outcome = "pass"
    elif rows and not correct_write:
        outcome = "wrong_write"
    elif success:
        outcome = "false_completion"
    else:
        outcome = "halted"

    steps = []
    stop: Optional[dict[str, Any]] = None
    for result in report.results if report is not None else []:
        step = {
            "step_id": result.step_id,
            "intent": result.intent,
            "ok": result.ok,
            "rung": result.resolution.rung if result.resolution is not None else None,
            "identity": result.identity.status if result.identity is not None else None,
        }
        steps.append(step)
        if not result.ok and stop is None:
            stop = {
                "step_id": result.step_id,
                "intent": result.intent,
                "gate": _gate(result),
                "rung": step["rung"],
                "identity": step["identity"],
                "error": (result.error or "")[:400],
            }
    rungs: dict[str, int] = dict(report.rung_counts) if report is not None else {}
    return {
        "kiosk": kiosk_name,
        "scale": profile.scale,
        "cleartype": profile.cleartype,
        "font_scale": profile.font_scale,
        "control": profile.font_scale != 1.0,
        "label": profile.label,
        "campaign": "wrong_record" if wrong_record else "healthy",
        "layout_drift_check": layout_drift,
        "outcome": outcome,
        "rows_written": len(rows),
        "model_calls": int(report.model_calls) if report is not None else 0,
        "rung_counts": rungs,
        "render_scale": getattr(report, "render_scale", None),
        "stopped_at": stop,
        "steps": steps,
        "crash": crash,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def _git(*args: str) -> Optional[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def summarize(points: list[dict[str, Any]]) -> dict[str, Any]:
    controls = [p for p in points if p.get("control")]
    points = [p for p in points if not p.get("control")]
    healthy = [p for p in points if p["campaign"] == "healthy"]
    wrong = [p for p in points if p["campaign"] == "wrong_record"]
    return {
        "font_only_controls": len(controls),
        "font_only_controls_halted": sum(p["outcome"] == "halted" for p in controls),
        "healthy_points": len(healthy),
        "healthy_passed": sum(p["outcome"] == "pass" for p in healthy),
        "healthy_halted": sum(p["outcome"] == "halted" for p in healthy),
        "wrong_record_points": len(wrong),
        "wrong_record_halted": sum(p["outcome"] == "halted" for p in wrong),
        "wrong_writes": sum(p["outcome"] == "wrong_write" for p in points),
        "false_completions": sum(p["outcome"] == "false_completion" for p in points),
        "crashes": sum(p["outcome"] == "crash" for p in points),
        "model_calls": sum(p["model_calls"] for p in points),
    }


def run(
    kiosks: list[str],
    scales: tuple[float, ...],
    cleartype: tuple[bool, ...],
    work: Path,
    *,
    layout_drift: bool = True,
) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    for kiosk_name in kiosks:
        workflow, bundle_dir = _record(kiosk_name, work)
        profiles = [
            RenderProfile(scale=scale, cleartype=ct)
            for scale in scales
            for ct in cleartype
        ] + list(CONTROLS)
        for profile in profiles:
            for wrong_record in (False, True):
                tag = (
                    f"{kiosk_name}_{round(profile.scale * 100)}_"
                    f"{'ct' if profile.cleartype else 'noct'}"
                    f"_f{round(profile.font_scale * 100)}"
                )
                tag += "_wrong" if wrong_record else ""
                point = _replay(
                    kiosk_name,
                    workflow,
                    bundle_dir,
                    profile,
                    wrong_record=wrong_record,
                    run_dir=work / "runs" / tag,
                    layout_drift=layout_drift,
                )
                stop = point["stopped_at"]
                print(
                    f"{kiosk_name:17s} {profile.label:24s} "
                    f"{point['campaign']:12s} {point['outcome']:16s} "
                    + (
                        f"stop={stop['step_id']} gate={stop['gate']} rung={stop['rung']}"
                        if stop
                        else ""
                    ),
                    flush=True,
                )
                points.append(point)
    return {
        "schema_version": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": _git("rev-parse", "HEAD"),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "font": {"regular": font_path(False), "bold": font_path(True)},
        "record_profile": RECORD_PROFILE.label,
        "layout_drift_check": layout_drift,
        "summary": summarize(points),
        "points": points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kiosk", action="append", choices=sorted(SPECS))
    parser.add_argument("--scale", action="append", type=float)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--no-layout-drift",
        action="store_true",
        help="diagnostic: turn the layout-drift check off to expose the next gate",
    )
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="oaflow-pixel-scale-"))
    work.mkdir(parents=True, exist_ok=True)
    results = run(
        args.kiosk or sorted(SPECS),
        tuple(args.scale) if args.scale else SCALES,
        CLEARTYPE,
        work,
        layout_drift=not args.no_layout_drift,
    )
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(json.dumps(results["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
