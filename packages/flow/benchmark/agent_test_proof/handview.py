"""Print each run's raw table change for a hand check, independently of the grader.

    python handview.py <run dir> [<run dir> ...]

It reads only the raw tab-separated dumps (``table_before_raw.tsv``,
``table_after_raw.tsv``, every column of the table) and the scenario's task
values, not the grader's JSON rows or verdict, and prints the rows that
appeared, disappeared or changed, with the agent's claim. The person checking
writes their own verdict per run; ``compare.py`` sets those against the
grader's outcomes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def table(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    lines = path.read_text(errors="replace").splitlines()
    if not lines:
        return [], {}
    head = lines[0].split("\t")
    rows = {}
    for ln in lines[1:]:
        vals = ln.split("\t")
        row = dict(zip(head, vals))
        rows[vals[0]] = row
    return head, rows


def main(dirs: list[str]) -> None:
    for d in dirs:
        out = Path(d)
        for run in sorted((out / "runs").glob("*.json")):
            r = json.loads(run.read_text())
            ev = out / "evidence" / f"{r['scenario']}-r{r['rep']}"
            _, before = table(ev / "table_before_raw.tsv")
            head, after = table(ev / "table_after_raw.tsv")
            print(f"=== {out.name} {r['scenario']} r{r['rep']} fault={r['fault']} claim={r['claim']['status']}")
            print(f"    task: {json.dumps({k: v for k, v in r.get('task', {}).items()})}" if r.get("task") else "", end="")
            for k in after:
                if k not in before:
                    print(f"    NEW {k}: " + " | ".join(f"{c}={after[k][c]}" for c in head if after[k][c] not in ("", "NULL", "0"))[:900])
            for k in before:
                if k not in after:
                    print(f"    GONE {k}: " + " | ".join(f"{c}={before[k][c]}" for c in head[:12]))
                elif before[k] != after[k]:
                    diff = [f"{c}: {before[k].get(c)!r} -> {after[k].get(c)!r}" for c in head if before[k].get(c) != after[k].get(c)]
                    print(f"    CHANGED {k}: " + "; ".join(diff)[:600])
            print(f"    agent said: {r['claim'].get('line', '')[:200]}")


if __name__ == "__main__":
    main(sys.argv[1:])
