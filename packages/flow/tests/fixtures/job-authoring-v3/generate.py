"""Regenerate only the new schema-3 examples; legacy vectors are never touched."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openadapt_flow import bundle_validation as bundles
from openadapt_flow.ir import ActionKind, ApiBinding, Step
from tests.test_job_authoring_schema3 import authored


def main() -> None:
    cases = [("minimal", authored())]
    numeric = authored()
    numeric.name = "Synthetic Unicode é / 😀"
    numeric.steps.append(
        Step(
            id="read",
            intent="synthetic read-only numeric fixture",
            action=ActionKind.WAIT,
            api_binding=ApiBinding(
                method="GET",
                url_template="/records",
                body_template={
                    "integer": 1,
                    "float": 1.0,
                    "negative_zero": -0.0,
                    "small": 1e-7,
                    "large": 1e20,
                    "unicode_keys": {"😀": "astral", "\ue000": "BMP"},
                    "job_authoring": {
                        "job_id": "ordinary user payload",
                        "schema": "data",
                    },
                },
            ),
        )
    )
    cases.append(("numeric-unicode-payload", numeric))
    result = []
    for name, workflow in cases:
        wire = workflow.model_dump(mode="json", exclude={"manifest"})
        canonical = bundles.source_canonical_payload(wire, {})
        digest = bundles.compute_content_digest(workflow, {})
        assert hashlib.sha256(canonical).hexdigest() == digest
        result.append(
            {
                "case": name,
                "workflow": wire,
                "files": {},
                "canonical_utf8": canonical.decode("utf-8"),
                "content_digest": digest,
                "workflow_json": workflow.model_dump_json(),
            }
        )
    Path(__file__).with_name("vectors.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
