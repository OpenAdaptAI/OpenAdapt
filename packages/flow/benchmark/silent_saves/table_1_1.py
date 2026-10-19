"""The README's fault set 1.1.0 tables, from each run's summary.json.

python -m silent_saves.table_1_1           # print the tables
python -m silent_saves.table_1_1 --check   # fail if README.md differs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "1.1.0"
RUN = "2026-10-19"
#: (label, run directory, use the run's own claim instead of the read-back gate)
ARMS = (
    ("1. Agent alone", "claude-computer-use+readback", True),
    ("1 + read-back (same runs)", "claude-computer-use+readback", False),
    ("2. Agent's first run under OpenAdapt", "compile-agent", False),
    ("3. Program", "openadapt-program", False),
    ("3r. Program with self-repair", "self-repair", False),
)
BEGIN = "<!-- table_1_1: begin -->"
END = "<!-- table_1_1: end -->"


def _load(run: str, alone: bool) -> Optional[dict[str, Any]]:
    path = RESULTS / run / RUN / "summary.json"
    if not path.is_file():
        return None
    s = json.loads(path.read_text())
    return s.get("without_readback", s) if alone else s


def _f(r: dict[str, Any]) -> str:
    return f"{r['k']} of {r['n']}" if r["n"] else "-"


def tables() -> str:
    head = (
        "| Arm | Verified saves | Silent wrong saves | False stops | "
        "Items left for a person | Human minutes per 100 items | Cost per verified item |\n"
        "|---|---|---|---|---|---|---|"
    )
    dp = (
        "| Arm | Detection: server-fault bad saves reported as success | "
        "Prevention: trap items where a wrong record landed | "
        "Stopped when no correct save existed |\n|---|---|---|---|"
    )
    rows, rows2 = [head], [dp]
    for label, run, alone in ARMS:
        s = _load(run, alone)
        if s is None:
            rows.append(f"| {label} | not run | | | | | |")
            rows2.append(f"| {label} | not run | | |")
            continue
        h = s["human"]
        cost = s["cost_per_verified_item_usd"]
        events = f" ({h['events']} scripted events)" if h["events"] else ""
        rows.append(
            f"| {label} | {_f(s['verified_saves'])} | {_f(s['silent_wrong_saves'])} | "
            f"{_f(s['false_stops'])} | {h['items_left_for_a_person']} | "
            f"{h['minutes_per_100_items']}{events} | "
            f"{'-' if cost is None else f'${cost:.4f}'} |"
        )
        p = s["prevention"]
        rows2.append(
            f"| {label} | {_f(s['detection']['silent_wrong'])} | "
            f"{_f(p['wrong_saves_landed'])} | {_f(p['stopped_when_no_correct_save'])} |"
        )
    return "\n".join(rows) + "\n\n" + "\n".join(rows2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = tables()
    if args.check:
        readme = (HERE / "README.md").read_text()
        inside = readme.split(BEGIN, 1)[-1].split(END, 1)[0].strip()
        if inside != text.strip():
            print("README.md's 1.1.0 tables are out of date", file=sys.stderr)
            return 1
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
