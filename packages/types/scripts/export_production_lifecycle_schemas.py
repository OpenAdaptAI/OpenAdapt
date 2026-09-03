"""Export the simple Production admission registry JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.production_lifecycle import (
    ProductionAdmissionRegistryStateV1,
    ProductionLifecycleAdmissionBindingV2,
    ProductionLifecycleTargetV2,
    QualificationReleaseReferenceV2,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
MODELS = {
    "production-admission-registry-state-v1.json": ProductionAdmissionRegistryStateV1,
    "production-lifecycle-admission-binding-v2.json": (
        ProductionLifecycleAdmissionBindingV2
    ),
    "production-lifecycle-target-v2.json": ProductionLifecycleTargetV2,
    "qualification-release-reference-v2.json": QualificationReleaseReferenceV2,
}


def main() -> None:
    for filename, model in MODELS.items():
        (SCHEMA_DIR / filename).write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
