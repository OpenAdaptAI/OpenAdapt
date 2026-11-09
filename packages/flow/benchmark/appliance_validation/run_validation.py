"""End-to-end validation of the on-prem VLM appliance against a REAL MLX model.

This harness proves the three wired tiers (identity veto, drift-oracle state
verifier, grounder) actually work when a real model is served -- exercised
through the SAME production wiring the runtime uses: the fail-safe clients in
``openadapt_flow.runtime.remote_vlm`` talking HTTP to the real service
(``openadapt_flow.services.vlm_service``) with ``VLM_BACKEND=mlx``.

It does NOT edit or re-implement any shipped runtime; it only measures it.

Run (from repo root, in the validation venv, with ANTHROPIC_API_KEY unset)::

    python benchmark/appliance_validation/run_validation.py \
        --port 8077 --out benchmark/appliance_validation/results.json

Outputs a JSON results file and prints a per-tier verdict.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# --- shipped production wiring (the path under test) -----------------------
from openadapt_flow.runtime.remote_vlm import (
    IdentityVerdict,
    RemoteGrounder,
    RemoteIdentityVLM,
    RemoteStateVerifier,
    RemoteVLMClient,
    appliance_from_env,
)

# --- validation-only rendering (reused, not modified) ----------------------
from openadapt_flow.validation.vlm_identity_probe import (
    COLLAPSE_PAIRS,
    DRIFT_CONDS,
    RECORD_COND,
    SAME_VALUES,
    render_crop,
)
from openadapt_flow.validation.dense_surface import (
    RECORD_CONDITION,
    build_dense_table,
    render_frame,
)

from service_manager import start_service
from state_fixtures import FIXTURES

MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"

# Generous per-call timeout: we are measuring the FEATURE (real inference), not
# the 2s fail-safe timeout. A short timeout would degrade every call to the safe
# ABSTAIN/uncertain outcome and measure nothing about the model.
CALL_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Render cache (Playwright is slow; render each (value, cond) once)
# ---------------------------------------------------------------------------
_crop_cache: dict[tuple[str, str], bytes] = {}


def crop(value: str, cond) -> bytes:
    key = (value, cond.name)
    if key not in _crop_cache:
        _crop_cache[key] = render_crop(value, cond)
    return _crop_cache[key]


def render_screen(html: str, *, device_scale_factor: int = 2,
                  width: int = 900, height: int = 600) -> bytes:
    """Render a full app-like screen to PNG (drift-oracle fixtures)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=device_scale_factor,
        )
        page.set_content(html, wait_until="networkidle")
        png = page.screenshot()
        browser.close()
    return png


# ---------------------------------------------------------------------------
# Validation 1 -- identity veto (SAFETY-CRITICAL)
# ---------------------------------------------------------------------------
@dataclass
class IdRow:
    suite: str
    collision_class: str
    code_a: str
    code_b: str
    truth: str  # "different" (wrong patient) | "same" (same patient)
    verdict: str  # remote IdentityVerdict.value
    sod: str  # same_or_different() -> "same"/"different"
    latency_s: float
    note: str

    @property
    def vetoed(self) -> bool:
        # veto == anything but a confident VERIFY
        return self.verdict != IdentityVerdict.VERIFY.value


def run_identity(identity: RemoteIdentityVLM) -> dict:
    rows: list[IdRow] = []

    def call(a_png: bytes, b_png: bytes) -> tuple[str, str, float]:
        t0 = time.time()
        verdict = identity.compare(a_png, b_png)
        dt = time.time() - t0
        sod = identity.same_or_different(a_png, b_png)
        return verdict.value, sod, dt

    # --- WRONG-patient: target vs one-glyph-collapsible sibling (truth different)
    for cls, a, b, note in COLLAPSE_PAIRS:
        pa, pb = crop(a, RECORD_COND), crop(b, RECORD_COND)
        v, sod, dt = call(pa, pb)
        rows.append(IdRow("wrong_patient", cls, a, b, "different", v, sod, dt, note))

    # --- SAME-patient clean re-render of a NON-confusable id (truth same)
    for val in SAME_VALUES:
        pa, pb = crop(val, RECORD_COND), crop(val, RECORD_COND)
        v, sod, dt = call(pa, pb)
        rows.append(IdRow("same_clean", "identical", val, val, "same", v, sod, dt,
                          "same value, clean re-render"))

    # --- SAME-patient, the exact confusable target vs itself (over-veto stress)
    for cls, a, b, note in COLLAPSE_PAIRS:
        pa = crop(a, RECORD_COND)
        v, sod, dt = call(pa, pa)
        rows.append(IdRow("same_confusable", cls, a, a, "same", v, sod, dt,
                          "confusable target vs identical re-render"))

    # --- SAME-patient under render drift (theme/font/scale) (truth same)
    for val in SAME_VALUES:
        pa = crop(val, RECORD_COND)
        for cond in DRIFT_CONDS:
            pb = crop(val, cond)
            v, sod, dt = call(pa, pb)
            rows.append(IdRow("same_drift", cond.name, val, val, "same", v, sod, dt,
                              f"same value under {cond.name}"))

    wrong = [r for r in rows if r.truth == "different"]
    same = [r for r in rows if r.truth == "same"]
    # False-accept: wrong-patient pair that did NOT veto (verdict == verify)
    false_accepts = [r for r in wrong if not r.vetoed]
    detected = [r for r in wrong if r.vetoed]
    over_vetoed = [r for r in same if r.vetoed]

    lat = [r.latency_s for r in rows]
    return {
        "n_wrong_patient": len(wrong),
        "n_same_patient": len(same),
        "false_accepts": len(false_accepts),
        "false_accept_rate": len(false_accepts) / len(wrong) if wrong else None,
        "detection_rate": len(detected) / len(wrong) if wrong else None,
        "over_veto": len(over_vetoed),
        "over_veto_rate": len(over_vetoed) / len(same) if same else None,
        "false_accept_cases": [asdict(r) for r in false_accepts],
        "over_veto_cases": [asdict(r) for r in over_vetoed],
        "latency_s": {
            "median": statistics.median(lat),
            "p95": sorted(lat)[int(0.95 * (len(lat) - 1))],
            "max": max(lat),
        },
        "rows": [asdict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Validation 2 -- drift-oracle false-rescue (residual risk)
# ---------------------------------------------------------------------------
def run_state(verifier: RemoteStateVerifier) -> dict:
    rows = []
    for fx in FIXTURES:
        png = render_screen(fx.html)
        t0 = time.time()
        ans = verifier.verify(png, fx.expected_state)  # "yes"/"no"/"uncertain"
        dt = time.time() - t0
        rows.append({
            "fid": fx.fid, "kind": fx.kind, "truth": fx.truth,
            "expected_state": fx.expected_state, "answer": ans,
            "holds": ans == "yes", "latency_s": dt, "note": fx.note,
        })

    true_cases = [r for r in rows if r["truth"] == "yes"]
    false_cases = [r for r in rows if r["truth"] == "no"]
    true_rescue = [r for r in true_cases if r["holds"]]        # correct "yes"
    false_rescue = [r for r in false_cases if r["holds"]]      # WRONG "yes" (risk)
    lat = [r["latency_s"] for r in rows]
    return {
        "n_true_cases": len(true_cases),
        "n_false_cases": len(false_cases),
        "true_rescue_rate": len(true_rescue) / len(true_cases) if true_cases else None,
        "false_rescue_rate": len(false_rescue) / len(false_cases) if false_cases else None,
        "false_rescue_cases": [r for r in false_cases if r["holds"]],
        "latency_s": {
            "median": statistics.median(lat),
            "max": max(lat),
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Validation 3 -- grounder availability
# ---------------------------------------------------------------------------
def run_grounder(grounder: RemoteGrounder, *, tol_px: int = 60,
                 n_targets: int = 6) -> dict:
    table = build_dense_table(seed=1, n_rows=18)
    frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
    vw, vh = frame.viewport

    # Pick a spread of rows that have a real patient name; target the Open button
    # (a discrete, describable control) and use its DOM centre as ground truth.
    indices = sorted(frame.points.keys())
    step = max(1, len(indices) // n_targets)
    chosen = indices[::step][:n_targets]

    rows = []
    for i in chosen:
        name_point, open_point, _, _ = frame.points[i]
        row = table.rows[i]
        intent = (
            f"the blue 'Open' button in the row for patient {row.name} "
            f"(MRN {row.mrn})"
        )
        t0 = time.time()
        match = grounder.locate(frame.png, intent)
        dt = time.time() - t0
        if match is None:
            rows.append({"row": i, "name": row.name, "mrn": row.mrn,
                         "intent": intent, "proposed": None, "truth": open_point,
                         "error_px": None, "hit": False, "latency_s": dt})
            continue
        px, py = match.point
        tx, ty = open_point
        err = ((px - tx) ** 2 + (py - ty) ** 2) ** 0.5
        rows.append({"row": i, "name": row.name, "mrn": row.mrn, "intent": intent,
                     "proposed": [px, py], "truth": [tx, ty],
                     "error_px": err, "hit": err <= tol_px, "latency_s": dt})

    hits = [r for r in rows if r["hit"]]
    errs = [r["error_px"] for r in rows if r["error_px"] is not None]
    lat = [r["latency_s"] for r in rows]
    return {
        "viewport": [vw, vh],
        "tol_px": tol_px,
        "n_targets": len(rows),
        "hit_rate": len(hits) / len(rows) if rows else None,
        "median_error_px": statistics.median(errs) if errs else None,
        "returned_proposal_rate": len(errs) / len(rows) if rows else None,
        "latency_s": {"median": statistics.median(lat), "max": max(lat)},
        "rows": rows,
    }


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--out", default="benchmark/appliance_validation/results.json")
    ap.add_argument("--log", default="benchmark/appliance_validation/service.log")
    ap.add_argument("--skip-grounder", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("[*] starting real MLX-backed VLM service ...")
    handle, load_s = start_service(
        model=MODEL, token="test", port=args.port, log_path=args.log
    )
    print(f"[*] service ready; model load = {load_s:.1f}s")

    results: dict = {
        "model": MODEL,
        "host": "Apple M2 Max (arm64, MLX)",
        "model_load_seconds": load_s,
        "call_timeout_s": CALL_TIMEOUT_S,
    }
    try:
        # Health/readiness sanity through plain HTTP (the client's transport).
        import httpx
        h = httpx.get(f"{handle.base_url}/health", timeout=5).json()
        rdy = httpx.get(f"{handle.base_url}/ready", timeout=5).json()
        results["health"] = h
        results["ready"] = rdy
        print(f"[*] /health={h}  /ready={rdy}")

        # Build the appliance through the SAME env path production uses.
        import os
        os.environ["OPENADAPT_FLOW_VLM_URL"] = handle.base_url
        os.environ["OPENADAPT_FLOW_VLM_TOKEN"] = handle.token
        os.environ["OPENADAPT_FLOW_VLM_TIMEOUT"] = str(CALL_TIMEOUT_S)
        appliance = appliance_from_env()
        assert appliance is not None, "appliance_from_env returned None"

        print("[1] identity veto (safety-critical) ...")
        results["identity"] = run_identity(appliance.identity_vlm)
        idr = results["identity"]
        print(f"    false-accepts={idr['false_accepts']}/{idr['n_wrong_patient']} "
              f"(rate {idr['false_accept_rate']}); "
              f"detection={idr['detection_rate']}; "
              f"over-veto={idr['over_veto']}/{idr['n_same_patient']}")

        print("[2] drift-oracle state verifier ...")
        results["state"] = run_state(appliance.state_verifier)
        st = results["state"]
        print(f"    true-rescue={st['true_rescue_rate']}  "
              f"false-rescue={st['false_rescue_rate']}")

        if not args.skip_grounder:
            print("[3] grounder availability ...")
            results["grounder"] = run_grounder(appliance.grounder)
            gr = results["grounder"]
            print(f"    hit-rate={gr['hit_rate']}  "
                  f"median-err-px={gr['median_error_px']}")

        results["peak_rss_mb"] = handle.rss_mb()
        print(f"[*] service RSS after run = {results['peak_rss_mb']} MiB")
    finally:
        handle.stop()

    out_path.write_text(json.dumps(results, indent=2))
    print(f"[*] wrote {out_path}")


if __name__ == "__main__":
    main()
