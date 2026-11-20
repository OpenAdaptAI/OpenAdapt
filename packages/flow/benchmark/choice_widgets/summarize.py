"""Print the before/after tables in results/ as Markdown (used for README.md).

Usage::

    python -m benchmark.choice_widgets.summarize
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
CASES = (
    "demo",
    "heldout",
    "reorder_late",
    "missing",
    "duplicate",
    "disabled",
    "surprise_confirm",
    "surprise_modal",
)


def _fixture(name: str) -> dict[str, dict[str, str]]:
    data = json.loads((RESULTS / name).read_text())
    return data["summary"]["table"]


def main() -> int:
    before, after = _fixture("fixture_before.json"), _fixture("fixture_after.json")
    print("| Widget | " + " | ".join(CASES) + " |")
    print("| --- |" + " --- |" * len(CASES))
    for kind in after:
        cells = [f"{before[kind][c]} → {after[kind][c]}" for c in CASES]
        print(f"| {kind} | " + " | ".join(cells) + " |")
    for label, name in (("before", "fixture_before.json"), ("after", "fixture_after.json")):
        totals = json.loads((RESULTS / name).read_text())["summary"]["totals"]
        print(f"\n{label}: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    for app in ("openemr", "openmrs"):
        for label in ("before", "after"):
            path = RESULTS / f"{app}_{label}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            cases = {c: v["score"] for c, v in data.get("cases", {}).items()}
            print(f"\n{app} {label}: pipeline={data.get('pipeline')} cases={cases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
