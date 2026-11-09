"""CLI: structural (DOM) ACTION rung vs the visual ladder under render drift.

Reproduces the desktop-benchmark shape that reframed the thesis from
"vision-only" to "deterministic compiled automation with visual FALLBACK"
(structural 21/21 vs compiled visual replay 6/21). The reusable, tested logic
lives in :mod:`openadapt_flow.validation.structural_action`; this script is a
thin runner that prints the ratios and writes ``structural_action.json``.

Usage:  python benchmark/structural_action/structural_action_probe.py [n] [--show]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from openadapt_flow.validation.structural_action import run_probe


def main(argv: list[str]) -> int:
    n = next((int(a) for a in argv if a.isdigit()), 21)
    report = run_probe(n=n, headless="--show" not in argv)
    out = Path(__file__).with_name("structural_action.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"structural (DOM) rung : {report['structural_ratio']} acted correctly")
    print(f"visual ladder only    : {report['visual_ratio']} acted correctly")
    drifted = [t for t in report["targets"] if t["drifted"]]
    d_struct = sum(t["structural_ok"] for t in drifted)
    d_vis = sum(t["visual_ok"] for t in drifted)
    print(
        f"under drift ({len(drifted)} targets): structural {d_struct}/{len(drifted)}"
        f" vs visual {d_vis}/{len(drifted)}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
