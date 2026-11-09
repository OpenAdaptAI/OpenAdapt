"""OpenAdapt's read-back around any executor (``--readback``).

The same check the scoreboard's claims run used: ``openadapt-flow
verify-run`` reads the store (``GET /api/db``) with a baseline taken before
the executor started, and judges the executor's write against a typed effect.
With ``--readback``, an item counts as claimed only when the executor claimed
success and verify-run confirmed the save.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

#: The scoreboard claims run's verify.yaml (``scoreboard/claims.py``).
VERIFY_YAML = """\
schema_version: openadapt.verify/v1
verifier:
  kind: rest
  base_url: {base}
  records_path: /api/db
effects:
  - kind: record_written
    match:
      patient_id: {{param: patient_id}}
      type: {{literal: Triage}}
      note: {{param: note}}
    expected_count: 1
    count_new_only: true
"""


def _verify(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "verify-run", *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


class ReadBack:
    """One verify-run baseline and check per item."""

    def __init__(self, store_base: str, work: Path) -> None:
        self.verify_yaml = work / "verify.yaml"
        self.verify_yaml.write_text(VERIFY_YAML.format(base=store_base))

    def before(self, item_dir: Path, params: dict[str, str]) -> None:
        self.params = item_dir / "params.json"
        self.params.write_text(json.dumps({"patient_id": "p1", **params}))
        self.baseline = item_dir / "baseline.json"
        self.baseline.unlink(missing_ok=True)
        cap = _verify(
            "--verify", str(self.verify_yaml),
            "--params", str(self.params),
            "--capture-baseline", str(self.baseline),
        )  # fmt: skip
        if cap.returncode != 0:
            raise SystemExit(f"verify-run baseline failed: {cap.stderr[-500:]}")

    def after(
        self, item_dir: Path, executor: str, claimed: bool, final_text: str, ref: str
    ) -> dict[str, Any]:
        record = item_dir / "run-record.json"
        record.write_text(
            json.dumps(
                {
                    "executor_kind": "agent",
                    "executor_id": executor,
                    "executor_build": {"benchmark": "silent-saves"},
                    "claimed_success": claimed,
                    "final_text": final_text,
                }
            )
        )
        share = item_dir / "verify"
        ver = _verify(
            "--verify", str(self.verify_yaml),
            "--params", str(self.params),
            "--run-record", str(record),
            "--authorization-ref", ref,
            "--baseline", str(self.baseline),
            "--out", str(share),
        )  # fmt: skip
        receipt_file = share / "effect-receipt.json"
        receipt = (
            json.loads(receipt_file.read_text())["receipt"]
            if receipt_file.is_file()
            else {}
        )
        return {
            "readback_verdict": receipt.get("effect_verdict", "none"),
            "readback_outcome": receipt.get("outcome", "refused"),
            "readback_confirmed": receipt.get("outcome") == "verified",
            "readback_stderr": "" if receipt else ver.stderr.strip()[-300:],
        }
