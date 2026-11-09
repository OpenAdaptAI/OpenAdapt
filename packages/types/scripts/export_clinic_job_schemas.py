"""Export the clinic inbox, outbox, and MCP JSON Schemas into the package."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.clinic_job import (
    ClinicInboxV1,
    ClinicMcpToolCatalogV1,
    ClinicOutboxV1,
    ClinicToolResultV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "clinic-inbox-v1.json": ClinicInboxV1,
    "clinic-outbox-v1.json": ClinicOutboxV1,
    "clinic-tool-result-v1.json": ClinicToolResultV1,
    "clinic-mcp-tools-v1.json": ClinicMcpToolCatalogV1,
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
