"""The fault gate on the suite's shipped and framed checks, on the stand-in."""

import json
import sys
from pathlib import Path

from verification_coverage.independent import run as suite

work, out = Path(sys.argv[1]), Path(sys.argv[2])
from openadapt_flow.fault_gate import AdmissionPolicy, CoverageRecord  # noqa: E402

report = {}
for framed in (False, True):
    rec = CoverageRecord.model_validate(suite.gate_shipped(work, framed))
    policy = AdmissionPolicy(coverage="check_coverage.json")
    name = "framed" if framed else "shipped"
    report[name] = {
        "killed": rec.killed,
        "applicable": rec.applicable,
        "clean_pass": rec.clean_pass,
        "admission": policy.problem(rec, rec.check_sha256) or "met",
        "classes": getattr(rec, "class_results", lambda: None)(),
        "cases": [
            {k: c[k] for k in ("fault", "detail", "result", "outcome")}
            for c in rec.cases
        ],
    }
    print(name, rec.killed, "/", rec.applicable, report[name]["admission"], flush=True)
out.mkdir(parents=True, exist_ok=True)
(out / "gate.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
