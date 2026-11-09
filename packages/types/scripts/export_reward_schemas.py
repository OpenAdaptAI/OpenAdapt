"""Export the reward contract, certificate, and evidence receipt JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.reward import (
    RewardCertificateV1,
    RewardContractV1,
    RewardEvidenceReceiptV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"

MODELS = {
    "reward-contract-v1.json": RewardContractV1,
    "reward-certificate-v1.json": RewardCertificateV1,
    "reward-evidence-receipt-v1.json": RewardEvidenceReceiptV1,
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
