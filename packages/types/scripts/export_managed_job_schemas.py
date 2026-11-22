"""Export the managed inbox, outbox, and MCP JSON Schemas into the package."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types.managed_job import (
    ManagedInboxV1,
    ManagedMcpToolCatalogV1,
    ManagedOutboxV1,
    ManagedToolResultV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "managed-inbox-v1.json": ManagedInboxV1,
    "managed-outbox-v1.json": ManagedOutboxV1,
    "managed-tool-result-v1.json": ManagedToolResultV1,
    "managed-mcp-tools-v1.json": ManagedMcpToolCatalogV1,
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
