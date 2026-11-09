"""Export the plain-result JSON Schemas into the package."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.plain_results import PlainOutcomeV1, PlainProgressV1

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "plain-outcome-v1.json": PlainOutcomeV1,
    "plain-progress-v1.json": PlainProgressV1,
}


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMAS.items():
        (SCHEMA_DIR / filename).write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
