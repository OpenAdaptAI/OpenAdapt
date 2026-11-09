"""Supplementary pass: measure state-verifier + grounder at a MODEL-FRIENDLY
image resolution, to separate two distinct failures found in the primary run.

The primary run (run_validation.py) rendered screens at device_scale_factor=2
(Retina, ~1800x1200 / 2240x3726). At that size the 4-bit MLX Qwen3-VL-4B build
emits an EMPTY / degenerate generation, so every state check parsed to
"uncertain" and every ground call returned no point -- both the safe (halt)
direction, but the FEATURE never fired. Downscaling the same screens to ~900px
wide makes the model answer. This pass quantifies the true-/false-rescue rates
and grounder hit-rate under a resolution the model can actually process, so the
report can state plainly: (a) at native 2x the tiers are non-functional
(safe-halt), and (b) at a model-friendly size, here are the real numbers.

Still driven entirely through the shipped clients (RemoteStateVerifier /
RemoteGrounder) against the real MLX service.
"""

from __future__ import annotations

import io
import json
import os
import statistics
import time
from pathlib import Path

from PIL import Image

from openadapt_flow.runtime.remote_vlm import appliance_from_env
from openadapt_flow.validation.dense_surface import (
    RECORD_CONDITION,
    build_dense_table,
    render_frame,
)

from run_validation import MODEL, CALL_TIMEOUT_S, render_screen
from service_manager import start_service
from state_fixtures import FIXTURES

STATE_DSF = 1          # render screens at 1x (~900px wide) so the model can read
STATE_W, STATE_H = 900, 600


def _downscale(png: bytes, target_w: int) -> tuple[bytes, float]:
    """Downscale a PNG to ``target_w`` wide; return (png, scale_factor)."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.width <= target_w:
        return png, 1.0
    scale = target_w / im.width
    new = im.resize((target_w, int(im.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    new.save(out, format="PNG")
    return out.getvalue(), scale


def run_state(verifier) -> dict:
    rows = []
    for fx in FIXTURES:
        png = render_screen(fx.html, device_scale_factor=STATE_DSF,
                            width=STATE_W, height=STATE_H)
        t0 = time.time()
        ans = verifier.verify(png, fx.expected_state)
        dt = time.time() - t0
        rows.append({"fid": fx.fid, "kind": fx.kind, "truth": fx.truth,
                     "expected_state": fx.expected_state, "answer": ans,
                     "holds": ans == "yes", "latency_s": dt, "note": fx.note})
    true_c = [r for r in rows if r["truth"] == "yes"]
    false_c = [r for r in rows if r["truth"] == "no"]
    tr = [r for r in true_c if r["holds"]]
    fr = [r for r in false_c if r["holds"]]
    return {
        "render": f"dsf={STATE_DSF} {STATE_W}x{STATE_H}",
        "n_true_cases": len(true_c), "n_false_cases": len(false_c),
        "true_rescue_rate": len(tr) / len(true_c) if true_c else None,
        "false_rescue_rate": len(fr) / len(false_c) if false_c else None,
        "false_rescue_cases": [r for r in false_c if r["holds"]],
        "answers": {r["fid"]: r["answer"] for r in rows},
        "rows": rows,
    }


def run_grounder(grounder, *, target_w: int = 1000, tol_px: int = 40,
                 n_targets: int = 6) -> dict:
    table = build_dense_table(seed=1, n_rows=18)
    frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
    png, scale = _downscale(frame.png, target_w)
    indices = sorted(frame.points.keys())
    step = max(1, len(indices) // n_targets)
    chosen = indices[::step][:n_targets]
    rows = []
    for i in chosen:
        _, open_point, _, _ = frame.points[i]
        row = table.rows[i]
        tx, ty = open_point[0] * scale, open_point[1] * scale
        intent = (f"the blue 'Open' button in the row for patient {row.name} "
                  f"(MRN {row.mrn})")
        t0 = time.time()
        match = grounder.locate(png, intent)
        dt = time.time() - t0
        if match is None:
            rows.append({"row": i, "name": row.name, "proposed": None,
                         "truth": [tx, ty], "error_px": None, "hit": False,
                         "latency_s": dt})
            continue
        px, py = match.point
        err = ((px - tx) ** 2 + (py - ty) ** 2) ** 0.5
        rows.append({"row": i, "name": row.name, "proposed": [px, py],
                     "truth": [tx, ty], "error_px": err, "hit": err <= tol_px,
                     "latency_s": dt})
    hits = [r for r in rows if r["hit"]]
    errs = [r["error_px"] for r in rows if r["error_px"] is not None]
    return {
        "render_scaled_to_w": target_w, "scale_factor": scale,
        "scaled_image_wh": list(Image.open(io.BytesIO(png)).size),
        "tol_px": tol_px, "n_targets": len(rows),
        "hit_rate": len(hits) / len(rows) if rows else None,
        "median_error_px": statistics.median(errs) if errs else None,
        "returned_proposal_rate": len(errs) / len(rows) if rows else None,
        "rows": rows,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--out",
                    default="benchmark/appliance_validation/results_supplement.json")
    ap.add_argument("--log",
                    default="benchmark/appliance_validation/service_supplement.log")
    args = ap.parse_args()

    handle, load_s = start_service(model=MODEL, token="test", port=args.port,
                                   log_path=args.log)
    print(f"[*] service ready; load={load_s:.1f}s")
    os.environ["OPENADAPT_FLOW_VLM_URL"] = handle.base_url
    os.environ["OPENADAPT_FLOW_VLM_TOKEN"] = handle.token
    os.environ["OPENADAPT_FLOW_VLM_TIMEOUT"] = str(CALL_TIMEOUT_S)
    appliance = appliance_from_env()
    out = {"model": MODEL, "note": "model-friendly resolution supplement"}
    try:
        print("[state @ 1x] ...")
        out["state_downscaled"] = run_state(appliance.state_verifier)
        st = out["state_downscaled"]
        print(f"   true-rescue={st['true_rescue_rate']} "
              f"false-rescue={st['false_rescue_rate']} answers={st['answers']}")
        print("[grounder @ ~1000px] ...")
        out["grounder_downscaled"] = run_grounder(appliance.grounder)
        gr = out["grounder_downscaled"]
        print(f"   hit-rate={gr['hit_rate']} "
              f"proposal-rate={gr['returned_proposal_rate']} "
              f"median-err={gr['median_error_px']}")
    finally:
        handle.stop()
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"[*] wrote {args.out}")


if __name__ == "__main__":
    main()
