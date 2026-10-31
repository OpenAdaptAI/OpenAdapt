"""Per-fault-class catch rates, before and after, from committed rows.

    python -m verification_coverage.summarize            # print the tables
    python -m verification_coverage.summarize --write    # also RESULTS.md

A bad save is **caught** when the engine's outcome is anything but
``verified``. A clean save (``ok``, ``concurrent_edit_other``) refused is a
**false alarm**. Ground truth comes from the fixture's own database, read
after the run; a save the agent itself got wrong counts as bad whatever the
injected fault was, and is reported in its own row.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINES = ("e1365", "main", "after")
ENGINE_LABEL = {
    "e1365": "#1365 engine",
    "main": "main (with #1375)",
    "after": "frame + gate + provenance",
}
CLEAN_FAULTS = ("ok", "concurrent_edit_other")


def load(app: str) -> tuple[list[dict[str, str]], list[dict]]:
    rows: list[dict[str, str]] = []
    metas: list[dict] = []
    for run in sorted((HERE / "results" / app).glob("*/items.csv")):
        meta = json.loads((run.parent / "meta.json").read_text())
        if not meta.get("complete"):
            continue
        metas.append(meta)
        rows += list(csv.DictReader(run.open()))
    return rows, metas


def _clean(row: dict[str, str]) -> bool:
    return row["ground_truth"] == "correct"


def table(rows: list[dict[str, str]]) -> list[str]:
    by_fault: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_fault[row["fault"]].append(row)
    head = (
        "| Fault class | n | "
        + " | ".join(ENGINE_LABEL[e] for e in ENGINES)
        + " | Held before the save |"
    )
    out = [head, "|" + "---|" * (len(ENGINES) + 3)]
    totals = {e: [0, 0] for e in ENGINES}
    alarms = {e: [0, 0] for e in ENGINES}
    for fault in sorted(by_fault, key=lambda f: (f in CLEAN_FAULTS, f)):
        items = by_fault[fault]
        clean = [r for r in items if _clean(r)]
        bad = [r for r in items if not _clean(r)]
        cells = []
        for engine in ENGINES:
            parts = []
            if bad:
                caught = sum(r[f"{engine}_outcome"] != "verified" for r in bad)
                totals[engine][0] += caught
                totals[engine][1] += len(bad)
                parts.append(f"{caught}/{len(bad)} caught")
            if clean:
                false = sum(r[f"{engine}_outcome"] != "verified" for r in clean)
                alarms[engine][0] += false
                alarms[engine][1] += len(clean)
                parts.append(f"{false}/{len(clean)} false alarms")
            cells.append(", ".join(parts))
        held = sum(r["presave_check"] == "hold" for r in items)
        out.append(
            f"| `{fault}` | {len(items)} | "
            + " | ".join(cells)
            + f" | {held}/{len(items)} |"
        )
    out.append(
        "| **All bad saves** | | "
        + " | ".join(f"**{c}/{n}**" for c, n in (totals[e] for e in ENGINES))
        + " | |"
    )
    out.append(
        "| **All clean saves** | | "
        + " | ".join(f"{c}/{n} false alarms" for c, n in (alarms[e] for e in ENGINES))
        + " | |"
    )
    return out


def render() -> str:
    lines = ["# Results", ""]
    for app, title in (("openemr", "OpenEMR"), ("openimis", "openIMIS")):
        rows, metas = load(app)
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines += ["No complete run yet.", ""]
            continue
        engines = metas[-1].get("engines", {})
        lines.append(
            f"{len(rows)} fresh agent runs (Claude Sonnet 5.5). Engines: "
            + ", ".join(
                f"{ENGINE_LABEL[e]} `{engines.get(e, '?')[:9]}`" for e in ENGINES
            )
            + "."
        )
        lines.append("")
        lines += table(rows)
        lines.append("")
        gate = HERE / "results" / app
        for summary in sorted(gate.glob("*/gate-summary.json")):
            data = json.loads(summary.read_text())
            lines.append(
                f"Fault gate on the {title} test copy ({summary.parent.name}):"
            )
            lines.append("")
            for name, s in data.items():
                lines.append(
                    f"- `{name}` read: {s['killed']} of {s['applicable']} faults killed"
                    + (
                        f"; survived: {', '.join(s['survived'])}"
                        if s["survived"]
                        else ""
                    )
                    + ("" if s["clean_pass"] else "; the clean save did not verify")
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    text = render()
    print(text)
    if args.write:
        (HERE / "RESULTS.md").write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
