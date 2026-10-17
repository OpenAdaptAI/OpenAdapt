"""Export the approver identity, approver policy, approval record and
autonomy eligibility JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.approval import (
    ApprovalRecordV1,
    ApproverIdentityV1,
    ApproverPolicyV1,
    AutonomyEligibilityV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"

MODELS = {
    "approver-identity-v1.json": ApproverIdentityV1,
    "approver-policy-v1.json": ApproverPolicyV1,
    "approval-record-v1.json": ApprovalRecordV1,
    "autonomy-eligibility-v1.json": AutonomyEligibilityV1,
}


def main() -> None:
    for filename, model in MODELS.items():
        target = SCHEMA_DIR / filename
        target.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
