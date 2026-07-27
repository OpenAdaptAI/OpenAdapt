#!/usr/bin/env python3
"""Generate Desktop's overlay projection from the canonical Types package."""

from __future__ import annotations

import argparse
import json
from importlib import metadata, resources
from pathlib import Path

from openadapt_types import (
    CONTROL_OVERLAY_FRAME_V2_SCHEMA,
    CONTROL_OVERLAY_STATUS_BY_PHASE,
    CONTROL_OVERLAY_TIMELINE_V2_SCHEMA,
    CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE,
    ControlOverlayDataClassification,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayTargetActionKind,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "overlay" / "generated" / "contract.ts"
SCHEMA_DIR = ROOT / "src" / "overlay" / "generated" / "schemas"
PINNED_TYPES_VERSION = "0.6.1"
SCHEMA_NAMES = (
    "control-overlay-frame-v2.json",
    "control-overlay-timeline-v2.json",
)


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _values(enum_type: type) -> list[str]:
    return [item.value for item in enum_type]


def render_typescript() -> str:
    statuses = {
        phase.value: status for phase, status in CONTROL_OVERLAY_STATUS_BY_PHASE.items()
    }
    labels = {
        mode.value: label.value
        for mode, label in CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE.items()
    }
    header = f"// Generated from openadapt-types {PINNED_TYPES_VERSION}. Do not edit by hand."
    frame_version = _json(CONTROL_OVERLAY_FRAME_V2_SCHEMA)
    timeline_version = _json(CONTROL_OVERLAY_TIMELINE_V2_SCHEMA)
    return f'''{header}

export const CONTROL_OVERLAY_FRAME_VERSION = {frame_version} as const;
export const CONTROL_OVERLAY_TIMELINE_VERSION = {timeline_version} as const;
export const CONTROL_OVERLAY_PHASES = {_json(_values(ControlOverlayPhase))} as const;
export const CONTROL_OVERLAY_MODES = {_json(_values(ControlOverlayMode))} as const;
export const CONTROL_OVERLAY_PROFILES = {_json(_values(ControlOverlayProfile))} as const;
export const CONTROL_OVERLAY_DATA_CLASSIFICATIONS = {
        _json(_values(ControlOverlayDataClassification))
    } as const;
export const CONTROL_OVERLAY_TARGET_ACTION_KINDS = {
        _json(_values(ControlOverlayTargetActionKind))
    } as const;
export const CONTROL_OVERLAY_STATUS_BY_PHASE = {_json(statuses)} as const;
export const CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE = {
        _json(labels)
    } as const;

export type OverlayPhase = (typeof CONTROL_OVERLAY_PHASES)[number];
export type OverlayMode = (typeof CONTROL_OVERLAY_MODES)[number];
export type OverlayProfile = (typeof CONTROL_OVERLAY_PROFILES)[number];
export type OverlayDataClassification = (typeof CONTROL_OVERLAY_DATA_CLASSIFICATIONS)[number];
export type OverlayTargetActionKind = (typeof CONTROL_OVERLAY_TARGET_ACTION_KINDS)[number];

export interface ControlOverlayControlsV2 {{
  pause: boolean;
  resume: boolean;
  stop: boolean;
}}

export interface ControlOverlayStepV2 {{
  current: number | null;
  total: number | null;
}}

export interface ControlOverlayMediaFrameBindingV2 {{
  kind: "media_frame";
  media_sha256: string;
  frame_index: number;
}}

export interface ControlOverlayObservationBindingV2 {{
  kind: "observation_hmac_sha256";
  observation_hmac_sha256: string;
}}

export interface ControlOverlayTargetTrackingV2 {{
  coordinate_space: "top_level_viewport_normalized";
  rect: {{ x: number; y: number; width: number; height: number }};
  source_viewport: {{
    width_css_px: number;
    height_css_px: number;
    device_pixel_ratio: number;
  }};
  binding: ControlOverlayMediaFrameBindingV2 | ControlOverlayObservationBindingV2;
  action_kind: OverlayTargetActionKind | null;
}}

export interface ControlOverlayFrameV2 {{
  schema_version: typeof CONTROL_OVERLAY_FRAME_VERSION;
  state_id: string;
  event_sequence: number;
  observed_at_unix_ms: number;
  observed_at_monotonic_ms: number;
  visible: boolean;
  phase: OverlayPhase;
  workflow_label: (typeof CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE)[OverlayMode];
  mode: OverlayMode;
  profile: OverlayProfile | null;
  step: ControlOverlayStepV2;
  controls: ControlOverlayControlsV2;
  status: (typeof CONTROL_OVERLAY_STATUS_BY_PHASE)[OverlayPhase];
  target_tracking: ControlOverlayTargetTrackingV2 | null;
  presentation: true;
}}

export interface ControlOverlayTimelineEventV2 {{
  at_ms: number;
  media_frame_index: number;
  frame: ControlOverlayFrameV2;
}}

export interface ControlOverlayTimelineV2 {{
  schema_version: typeof CONTROL_OVERLAY_TIMELINE_VERSION;
  data_classification: OverlayDataClassification;
  evidence_pack_id: string;
  media_sha256: string;
  media_frame_count: number;
  duration_ms: number;
  events: ControlOverlayTimelineEventV2[];
}}
'''


def rendered_files() -> dict[Path, str]:
    package_schemas = resources.files("openadapt_types.schemas")
    files = {OUTPUT: render_typescript()}
    for name in SCHEMA_NAMES:
        files[SCHEMA_DIR / name] = package_schemas.joinpath(name).read_text(encoding="utf-8")
    return files


def assert_control_overlay_contract_synced(root: Path = ROOT) -> None:
    installed = metadata.version("openadapt-types")
    if installed != PINNED_TYPES_VERSION:
        raise ValueError(
            f"openadapt-types {PINNED_TYPES_VERSION} is required to generate the overlay contract; "
            f"found {installed}"
        )
    for path, expected in rendered_files().items():
        actual_path = root / path.relative_to(ROOT)
        if not actual_path.is_file() or actual_path.read_text(encoding="utf-8") != expected:
            raise ValueError(
                f"{actual_path.relative_to(root)} is stale; run "
                "python scripts/sync_control_overlay_contract.py --write"
            )


def write_contract() -> None:
    installed = metadata.version("openadapt-types")
    if installed != PINNED_TYPES_VERSION:
        raise ValueError(
            f"refusing generation from openadapt-types {installed}; expected {PINNED_TYPES_VERSION}"
        )
    for path, text in rendered_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        write_contract()
    else:
        assert_control_overlay_contract_synced()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
