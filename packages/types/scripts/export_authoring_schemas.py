"""Export the authoring MCP JSON Schemas into the package."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.authoring import (
    AuthoringBindV1,
    AuthoringCommandV1,
    AuthoringObserveV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "authoring-observe-v1.json": AuthoringObserveV1,
    "authoring-command-v1.json": AuthoringCommandV1,
    "authoring-bind-v1.json": AuthoringBindV1,
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
