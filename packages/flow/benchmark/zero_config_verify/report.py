"""Print the RESULTS.md tables from results.json.

python -m benchmark.zero_config_verify.report
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _cell(per: dict, fault: str, correct: bool) -> str:
    c = per.get(fault)
    if not c:
        return "-"
    if correct:
        return f"{c.get('false_reject', 0)} FR / {c['runs']}"
    return f"{c.get('false_accept', 0)} FA / {c['runs']}"


def main() -> None:
    data = json.loads((HERE / "results.json").read_text())
    for app, out in data["apps"].items():
        print(f"### {app}{' (held out)' if out['held_out'] else ''}\n")
        names = ["hand-written", "hand-written+complete"] + [
            a for a, e in out["arms"].items() if e["runnable"]
        ]
        tallies = {
            "hand-written": out["hand_written"]["tally"],
            "hand-written+complete": out["hand_written_complete"]["tally"],
            **{a: e["tally"] for a, e in out["arms"].items() if e["runnable"]},
        }
        faults: list[tuple[str, bool]] = []
        for row in out["trials"]:
            key = (row["fault"], row["ground_truth_correct"])
            if key not in faults:
                faults.append(key)
        print("| fault | truth | " + " | ".join(f"`{n}`" for n in names) + " |")
        print("|---|---|" + "---|" * len(names))
        for fault, correct in faults:
            cells = [_cell(tallies[n]["per_fault"], fault, correct) for n in names]
            print(
                f"| {fault} | {'correct' if correct else 'wrong'} | "
                + " | ".join(cells)
                + " |"
            )
        print(
            "| **total** | | "
            + " | ".join(
                f"**{t['false_accept']} FA / {t['wrong_runs']}, "
                f"{t['false_reject']} FR / {t['correct_runs']}**"
                for t in (tallies[n]["total"] for n in names)
            )
            + " |"
        )
        print(
            "| indeterminate | | "
            + " | ".join(str(tallies[n]["total"]["indeterminate"]) for n in names)
            + " |\n"
        )
        print("| arm | class | generator said | runnable | kill check |")
        print("|---|---|---|---|---|")
        for arm, e in out["arms"].items():
            kc = e["kill_check"]
            verdict = (
                "ready" if kc["ready"] else "NOT READY: " + "; ".join(kc["failed"])
            )
            print(
                f"| `{arm}` | {e['class']} | {e['generator_status']} | "
                f"{'yes' if e['runnable'] else 'no'} | {verdict} |"
            )
        print()
    print("verdict:", json.dumps(data["verdict"]))


if __name__ == "__main__":
    main()
