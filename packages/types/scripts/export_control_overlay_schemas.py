"""Export the versioned control-overlay JSON Schemas into the package."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_types import (
    ControlOverlayFrameV1,
    ControlOverlayFrameV2,
    ControlOverlayTimelineV1,
    ControlOverlayTimelineV2,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "control-overlay-frame-v1.json": ControlOverlayFrameV1,
    "control-overlay-timeline-v1.json": ControlOverlayTimelineV1,
    "control-overlay-frame-v2.json": ControlOverlayFrameV2,
    "control-overlay-timeline-v2.json": ControlOverlayTimelineV2,
}


def rendered_schemas() -> dict[str, str]:
    return {
        filename: json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for filename, model in SCHEMAS.items()
    }


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered_schemas().items():
        (SCHEMA_DIR / filename).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
