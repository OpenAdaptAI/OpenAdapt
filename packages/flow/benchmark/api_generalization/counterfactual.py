"""Counterfactual input edit: drop the clicked submit button's name=value from the captured save.

Not a product result. The frozen compiler can't explain a submit button's
own name and value (``_FormParser`` skips submit inputs), which refuses every
form whose save button has a name. This copies a recording, removes that one
pair from the captured save request, and asks the same frozen compiler again,
to find the next thing that would block the API step. The compiler is not
changed; only its input is.

    python -m benchmark.api_generalization.counterfactual --app trac --recording <rec> --out <rec-cf> --field submit
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def strip_submit(rec: Path, out: Path, field: str) -> int:
    shutil.copytree(rec, out)
    lines = (out / "network.jsonl").read_text().splitlines()
    n = 0
    fixed = [lines[0]]
    for raw in lines[1:]:
        d = json.loads(raw)
        if d.get("method") == "POST" and d.get("form") is not None:
            before = len(d["form"])
            d["form"] = [p for p in d["form"] if p[0] != field]
            n += before - len(d["form"])
        fixed.append(json.dumps(d))
    (out / "network.jsonl").write_text("\n".join(fixed) + "\n")
    (out / "COUNTERFACTUAL.txt").write_text(f"removed {n} '{field}' pair(s) from captured POST forms\n")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--field", required=True)
    a = ap.parse_args()
    print(json.dumps({"removed": strip_submit(Path(a.recording), Path(a.out), a.field)}))


if __name__ == "__main__":
    main()
