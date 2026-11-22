"""Count each phase's receipts in a state folder: signed, verified, and the
approval block's approver kind. Counts only."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def tally(state_root: Path) -> dict[str, Any]:
    from openadapt_flow.receipt_record import local_registry, verify_run_receipt

    registry = local_registry()
    out: dict[str, Counter] = {}
    for run in sorted((Path(state_root) / "runs").iterdir()):
        report = json.loads((run / "report.json").read_text())
        phase = report.get("workflow_name", "?")
        c = out.setdefault(phase, Counter())
        c["runs"] += 1
        if not (run / "receipt.json").exists():
            c["not_signed"] += 1
            continue
        got = verify_run_receipt(run, registry)
        c["verified" if got.ok else "verify_failed"] += 1
        if got.ok:
            approval = got.statement["predicate"].get("approval") or {}
            c[f"approver_{approval.get('approver_kind', 'absent')}"] += 1
            bound = report.get("approval", {}).get("approvals") or []
            if bound and all(a.get("request_sha256") == report.get("plan_sha256") for a in bound):
                c["approval_bound_to_plan_digest"] += 1
    return {k: dict(v) for k, v in sorted(out.items())}
