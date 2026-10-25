"""Run the independent suite's 17 cases on the stand-in with the #1505 frame.

The stand-in is the suite's SQLite copy of the pinned OpenEMR fixture's dump.
``framed`` gets the frame narrowed to the tables the New Patient save writes;
everything else is the suite's own code (verification-coverage/independent).
The synthesized check is not run (it is #1509's subject and slow).
"""

import json
import sys
from pathlib import Path

import yaml
from verification_coverage.independent import run as suite
from verification_coverage.independent import standin

SCOPE = (
    r"(?i)^\s*(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from)"
    r"\s+`?(?P<scope>[A-Za-z0-9_]+)"
)
narrow = "--narrow" in sys.argv
args = [a for a in sys.argv[1:] if a != "--narrow"]
work = Path(args[0])
out = Path(args[1])
orig_framed = suite.framed_doc


def framed_doc(db):
    doc = orig_framed(db)
    if narrow:
        att = doc["frame"]["attribution"]
        att.update(
            scope_field="comments",
            scope_encoding="base64",
            scope_pattern=SCOPE,
            writes=["patient_data", "history_data"],
        )
        att["read"]["sql_query"] = att["read"]["sql_query"].replace(
            "SELECT id, user, patient_id FROM",
            "SELECT id, user, patient_id, comments FROM",
        )
    return doc


suite.framed_doc = framed_doc
work.mkdir(parents=True, exist_ok=True)
if not (work / "baseline.sqlite").exists():
    standin.build(Path(args[2]), work / "baseline.sqlite")
    standin.keep_tables(work / "baseline.sqlite", suite.SLICE)
synth = {
    "doc": suite.shipped_doc(Path("DEMO_DB")),
    "demo_db": "DEMO_DB",
    "params": list(suite.SHIPPED_PARAMS),
}
cases = yaml.safe_load((Path(suite.__file__).parent / "faults.yaml").read_text())
rows = []
for case in cases["cases"]:
    row = suite.run_case(case, work, synth)
    row.pop("synthesized", None)
    for name in ("shipped", "framed"):
        row[name]["score"] = suite.score(case["expect"], row[name]["verified"])
    rows.append(row)
    print(case["id"], row["framed"]["outcome"], row["framed"]["score"], flush=True)
out.mkdir(parents=True, exist_ok=True)
(out / "rows.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
