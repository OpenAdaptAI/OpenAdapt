"""Per fault class: caught, missed, false alarm, for each check.

    python -m verification_coverage.independent.summarize ROWS [ROWS ...]

ROWS is a ``rows.json`` (stand-in) or ``rows.jsonl`` (live). Prints Markdown.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

CLASSES = (
    "clean",
    "concurrent_legitimate",
    "wrong_record",
    "unrelated_change",
    "misread_source",
    "missing_side_record",
    "duplicate",
)
CHECKS = ("shipped", "framed", "synthesized", "presave")


def load(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def cell(rows: list[dict[str, Any]], check: str) -> str:
    scores = [
        r["presave_score"] if check == "presave" else r[check]["score"]
        for r in rows
        if (check == "presave" and "presave_score" in r)
        or isinstance(r.get(check), dict)
    ]
    if not scores:
        return "n/a"
    n = len(scores)
    if rows[0]["expect"] == "clean":
        bad = scores.count("false_alarm")
        return f"{n - bad}/{n} ok" + (f", **{bad} false alarm**" if bad else "")
    caught = scores.count("caught")
    missed = n - caught
    return f"{caught}/{n} caught" + (f", **{missed} missed**" if missed else "")


def table(rows: list[dict[str, Any]]) -> str:
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if "class" in r:
            by_class[r["class"]].append(r)
    checks = [c for c in CHECKS if any(c == "presave" or c in r for r in rows)]
    out = ["| class | cases | " + " | ".join(checks) + " |"]
    out.append("|---|---|" + "---|" * len(checks))
    for cls in CLASSES:
        group = by_class.get(cls)
        if not group:
            continue
        out.append(
            f"| {cls} | {len(group)} | "
            + " | ".join(cell(group, c) for c in checks)
            + " |"
        )
    return "\n".join(out)


def cases(rows: list[dict[str, Any]]) -> str:
    checks = [c for c in CHECKS if c != "presave" and any(c in r for r in rows)]
    out = ["| case | expect | " + " | ".join(checks) + " | pre-save |"]
    out.append("|---|---|" + "---|" * (len(checks) + 1))
    for r in rows:
        if "class" not in r:
            out.append(f"| {r['id']} | error | {r.get('error', '')[:120]} |")
            continue
        out.append(
            f"| {r['id']} | {r['expect']} | "
            + " | ".join(
                f"{r[c]['outcome']} ({r[c]['score']})"
                if isinstance(r.get(c), dict)
                else "n/a"
                for c in checks
            )
            + f" | {r.get('presave')} |"
        )
    return "\n".join(out)


def main() -> int:
    for arg in sys.argv[1:]:
        rows = load(Path(arg))
        print(f"### {arg}\n\n{table(rows)}\n\n{cases(rows)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
