"""Count a validation run: correct writes confirmed, faults stopped, by kind.

    python benchmark/generate_contracts/summarize.py APP WORKDIR

A case passes when the verifier says CONFIRMED. The oracle's label says what
the write really did: ``correct`` (or no write needed), or harm. A correct
write that is not CONFIRMED is a false stop; a harm that is CONFIRMED is a
miss. Fault scope (in or out of what the verifier claims) comes from the
pack's ``faults.yaml``.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from demo_lib import FCG

GOOD = {
    "correct"
}  # "nothing" (no write where one was needed) and "harm" must be stopped


def main() -> None:
    app, work = sys.argv[1], Path(sys.argv[2])
    scope = {
        f["id"]: f.get("scope", "in")
        for f in yaml.safe_load(
            (FCG / "apps" / app / "faults" / "faults.yaml").read_text()
        )
    }
    rows = [
        json.loads(x)
        for x in (work / "validate.jsonl").read_text().splitlines()
        if x.strip()
    ]
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    misses, false_stops = [], []
    for r in rows:
        k = r["kind"]
        # A recheck that refutes a confirmed write turns it into a stop (late).
        ok = r["verdict"]["outcome"] == "CONFIRMED" and r.get("recheck", {}).get("outcome", "CONFIRMED") == "CONFIRMED"
        label = str(r["oracle"].get("label"))
        harm = label not in GOOD
        sc = scope.get(r["case"].split("-", 1)[1], "in")
        if harm:
            by_kind[k][f"harm_{sc}"] += 1
            if ok:
                by_kind[k][f"harm_{sc}_confirmed"] += 1
                misses.append((r["case"], sc, label))
        else:
            by_kind[k]["correct"] += 1
            if ok:
                by_kind[k]["correct_confirmed"] += 1
            else:
                false_stops.append(
                    (
                        r["case"],
                        r["verdict"]["outcome"],
                        "; ".join(r["verdict"]["reasons"])[:160],
                    )
                )
    print(
        "| Kind | Correct writes confirmed | In-scope harms stopped | Out-of-scope harms stopped |"
    )
    print("| --- | --- | --- | --- |")
    for k, c in sorted(by_kind.items()):
        hi, ho = c["harm_in"], c["harm_out"]
        print(f"| `{k}` | {c['correct_confirmed']}/{c['correct']} | {hi - c['harm_in_confirmed']}/{hi} | "
              f"{ho - c['harm_out_confirmed']}/{ho} |")  # fmt: skip
    print()
    for m in misses:
        print("MISS", *m)
    for f in false_stops:
        print("FALSE STOP", *f)


if __name__ == "__main__":
    main()
