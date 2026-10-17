"""Export the portable process-capability JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.authentication import (
    AuthenticationCapturePolicyV1,
    AuthenticationReceiptV1,
    AuthenticationTaskContractV1,
)
from openadapt_types.process_capability import (
    ArtifactRefV1,
    CodeCapabilityAdmissionEnvelopeV1,
    CodeCapabilityManifestV1,
    ProcessEvidenceReceiptV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"

MODELS = {
    "authentication-capture-policy-v1.json": AuthenticationCapturePolicyV1,
    "authentication-task-v1.json": AuthenticationTaskContractV1,
    "authentication-receipt-v1.json": AuthenticationReceiptV1,
    "artifact-ref-v1.json": ArtifactRefV1,
    "code-capability-manifest-v1.json": CodeCapabilityManifestV1,
    "code-capability-admission-v1.json": CodeCapabilityAdmissionEnvelopeV1,
    "process-evidence-receipt-v1.json": ProcessEvidenceReceiptV1,
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
