"""Rebuild results/<app>/summary.json from the committed runs and print the tables.

    python benchmark/code_as_action/summarize.py

Medians are over all runs of an arm; "correct" is the read-back verdict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

KEYS = [
    "model_calls",
    "actions",
    "prompt_tokens",
    "output_tokens",
    "cost_usd",
    "model_seconds",
    "run_seconds",
    "screenshots_sent",
]
ROWS = [
    ("Correct (read back)", None),
    ("Model calls", "model_calls"),
    ("Actions", "actions"),
    ("Prompt tokens", "prompt_tokens"),
    ("Output tokens", "output_tokens"),
    ("Cost", "cost_usd"),
    ("Model time", "model_seconds"),
    ("Run time (under load)", "run_seconds"),
    ("Screenshots sent", "screenshots_sent"),
]


def _fmt(key: str, value: object) -> str:
    if value is None:
        return "n/a"
    v = float(value)  # type: ignore[arg-type]
    if key == "cost_usd":
        return f"${v:.3f}"
    if key.endswith("seconds"):
        return f"{v:.1f} s"
    return f"{v:,.0f}"


def table(summary: dict, arms: list[str]) -> str:
    head = "| median per run | " + " | ".join(arms) + " |"
    lines = [head, "| --- |" + " ---: |" * len(arms)]
    for label, key in ROWS:
        cells = []
        for arm in arms:
            s = summary[arm]
            if key is None:
                cells.append(f"{s['correct']} / {s['runs']}")
            else:
                cells.append(_fmt(key, s.get(f"median_{key}")))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    for app, arms in (
        ("mockmed", ["code", "selective", "every_turn"]),
        ("openemr", ["code", "every_turn"]),
    ):
        path = common.RESULTS / app / "runs.jsonl"
        if not path.is_file():
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        arms = [a for a in arms if any(r["arm"] == a for r in rows)]
        summary_path = common.RESULTS / app / "summary.json"
        old = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
        old["arms"] = common.summarize(rows, arms, KEYS)
        if app == "openemr":
            old["arms_by_case"] = {
                case: common.summarize(
                    [r for r in rows if r["mismatch_case"] == (case == "duplicate")],
                    arms,
                    KEYS,
                )
                for case in ("normal", "duplicate")
            }
        old.setdefault("environment", {})["spend_total_usd"] = common.spent()
        summary_path.write_text(json.dumps(old, indent=2, sort_keys=True) + "\n")
        print(f"## {app}\n")
        print(table(old["arms"], arms))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
