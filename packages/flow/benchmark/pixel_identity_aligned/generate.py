#!/usr/bin/env python3
"""Regenerate the jitter-robust pixel-identity evidence.

Thin runner over :mod:`openadapt_flow.validation.pixel_identity_aligned` (the
battery lives in the package so the unit suite can assert its invariants). Runs
the full cross-render jitter battery through the SAME production tier the
runtime uses (``runtime.identity.verify_pixel_identity``) and writes:

- ``evidence.json`` -- machine-readable summary + the distance gap the VERIFY
  gate sits in;
- ``EVIDENCE.md`` -- the human-readable writeup.

Self-contained (``cv2`` + ``numpy``, no browser, no system fonts). Zero model
calls, zero network. Usage::

    python benchmark/pixel_identity_aligned/generate.py
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.validation import pixel_identity_aligned as battery


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    result = battery.run_battery(enable_verify=True)
    evidence = {
        "summary": result["summary"],
        "distance_stats": battery.distance_stats(),
        "n_trials": len(result["rows"]),
    }
    (out_dir / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (out_dir / "EVIDENCE.md").write_text(battery.render_markdown())

    s = evidence["summary"]
    print(f"trials: {evidence['n_trials']}")
    print(f"false-accept (MUST be 0): {s['false_accept']} / {s['n_diff']}")
    print(f"false-mismatch: {s['false_mismatch']} / {s['n_same']}")
    print(f"same match rate (matching render): {s['same_match_rate_matching_render']:.0%}")
    return 0 if s["false_accept"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
