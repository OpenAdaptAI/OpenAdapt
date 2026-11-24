"""Print the per-app table from results/<app>/result.json (Markdown)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def row(r: dict) -> str:
    h = r.get("heldout") or {}
    adm_faults = [x for x in r["rounds"] if x.get("admitted")]
    caught = (
        f"{adm_faults[0]['faults_caught']}/{adm_faults[0]['faults_applicable']}"
        if adm_faults
        else "-"
    )
    held = f"{h['faults_caught']}/{h['faults_applicable']}" if h else "-"
    clean_adm = (
        f"{len(adm_faults[0]['false_stops'])}/{adm_faults[0]['clean']}"
        if adm_faults
        else "-"
    )
    clean_held = f"{len(h['false_stops'])}/{h['clean']}" if h else "-"
    return (
        f"| {r['app']}{' (untuned)' if r['untuned'] else ''} | {r['describe']} | {r['setup_minutes']:.1f} | "
        f"{r['person_decisions']} | {r['drafts_admitted']} / {r['drafts_rejected']} | {caught} | {held} | "
        f"{clean_adm} | {clean_held} | {len(r.get('model_calls') or [])} |"
    )


def main(names: list[str]) -> int:
    print(
        "| App | Description | Setup minutes | Person decisions | Drafts admitted / rejected | "
        "Planted faults caught (admission) | Planted faults caught (held out) | "
        "False stops (admission clean writes) | False stops (held-out clean writes) | Model calls |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name in names or sorted(
        p.name for p in RESULTS.iterdir() if (p / "result.json").is_file()
    ):
        print(row(json.loads((RESULTS / name / "result.json").read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
