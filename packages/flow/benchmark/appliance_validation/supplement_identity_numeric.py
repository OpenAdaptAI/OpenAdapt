"""Definitive identity-veto pass over the FULL collapse surface, incl. the
purely-numeric MRN class (``100512`` vs ``1OO512``) the primary run omitted.

The primary run used ``vlm_identity_probe.COLLAPSE_PAIRS`` (alphanumeric only).
This pass uses ``pixel_identity_probe.COLLAPSE_PAIRS`` -- digit-flanked,
alpha-flanked, AND purely-numeric O0/l1 pairs, the last being the 9th
wrong-patient reopening (OCR reads them byte-identically). Each identifier is
rendered as a magnified crop with the SAME probe renderer and compared through
the shipped ``RemoteIdentityVLM`` client against the real MLX service. This is
the wrong-patient false-accept number that decides the safety-critical tier.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

from openadapt_flow.runtime.remote_vlm import IdentityVerdict, appliance_from_env
from openadapt_flow.validation.pixel_identity_probe import COLLAPSE_PAIRS
from openadapt_flow.validation.vlm_identity_probe import RECORD_COND, render_crop

from run_validation import MODEL, CALL_TIMEOUT_S
from service_manager import start_service


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--out",
                    default="benchmark/appliance_validation/results_identity_full.json")
    ap.add_argument("--log",
                    default="benchmark/appliance_validation/service_identity_full.log")
    args = ap.parse_args()

    handle, load_s = start_service(model=MODEL, token="test", port=args.port,
                                   log_path=args.log)
    os.environ["OPENADAPT_FLOW_VLM_URL"] = handle.base_url
    os.environ["OPENADAPT_FLOW_VLM_TOKEN"] = handle.token
    os.environ["OPENADAPT_FLOW_VLM_TIMEOUT"] = str(CALL_TIMEOUT_S)
    identity = appliance_from_env().identity_vlm

    rows = []
    try:
        for p in COLLAPSE_PAIRS:
            pa = render_crop(p.target, RECORD_COND)
            pb = render_crop(p.sibling, RECORD_COND)
            t0 = time.time()
            verdict = identity.compare(pa, pb)  # wrong patient -> expect veto
            dt = time.time() - t0
            sod = identity.same_or_different(pa, pb)
            rows.append({
                "label": p.label, "glyph_class": p.glyph_class, "flank": p.flank,
                "target": p.target, "sibling": p.sibling,
                "verdict": verdict.value, "sod": sod,
                "vetoed": verdict is not IdentityVerdict.VERIFY,
                "false_accept": verdict is IdentityVerdict.VERIFY,
                "latency_s": dt, "note": p.note,
            })
    finally:
        handle.stop()

    fa = [r for r in rows if r["false_accept"]]
    by_flank = defaultdict(lambda: [0, 0])
    for r in rows:
        by_flank[r["flank"]][0] += 0 if r["false_accept"] else 1  # detected
        by_flank[r["flank"]][1] += 1
    lat = [r["latency_s"] for r in rows]
    out = {
        "model": MODEL,
        "corpus": "pixel_identity_probe.COLLAPSE_PAIRS (digit/alpha/numeric O0+l1)",
        "n_wrong_patient": len(rows),
        "false_accepts": len(fa),
        "false_accept_rate": len(fa) / len(rows) if rows else None,
        "detection_rate": (len(rows) - len(fa)) / len(rows) if rows else None,
        "detection_by_flank": {k: {"detected": v[0], "n": v[1]}
                               for k, v in by_flank.items()},
        "false_accept_cases": fa,
        "latency_s": {"median": statistics.median(lat), "max": max(lat)},
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in
                      ["n_wrong_patient", "false_accepts", "false_accept_rate",
                       "detection_rate", "detection_by_flank"]}, indent=2))
    print("[*] wrote", args.out)


if __name__ == "__main__":
    main()
