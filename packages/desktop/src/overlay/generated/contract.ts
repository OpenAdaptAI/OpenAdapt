// Generated from openadapt-types 0.10.0. Do not edit by hand.

export const CONTROL_OVERLAY_FRAME_VERSION = "openadapt.control-overlay-frame/v2" as const;
export const CONTROL_OVERLAY_TIMELINE_VERSION = "openadapt.control-overlay-timeline/v2" as const;
export const CONTROL_OVERLAY_PHASES = ["idle","observing","recording","executing","pausing","paused","resuming","stopping","verifying","verified","completed_unverified","halted","failed","rolled_back"] as const;
export const CONTROL_OVERLAY_MODES = ["demonstration","replay","governed","managed"] as const;
export const CONTROL_OVERLAY_PROFILES = ["demo","standard","regulated"] as const;
export const CONTROL_OVERLAY_DATA_CLASSIFICATIONS = ["synthetic","sanitized_public"] as const;
export const CONTROL_OVERLAY_TARGET_ACTION_KINDS = ["click","double_click","right_click","drag","type","select","toggle","invoke","expand_collapse","scroll","hover"] as const;
export const CONTROL_OVERLAY_STATUS_BY_PHASE = {"idle":"Ready","observing":"Observing the application","recording":"Watching your demonstration","executing":"Executing with verification gates","pausing":"Pausing at a safe boundary","paused":"Execution paused","resuming":"Resuming at a safe boundary","stopping":"Stopping at a safe boundary","verifying":"Verifying the intended result","verified":"Outcome verified","completed_unverified":"Completed without sufficient verification","halted":"Halted instead of guessing","failed":"Execution failed","rolled_back":"Compensating action completed"} as const;
export const CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE = {"demonstration":"Workflow demonstration","replay":"Workflow replay","governed":"Governed workflow","managed":"Managed workflow"} as const;

export type OverlayPhase = (typeof CONTROL_OVERLAY_PHASES)[number];
export type OverlayMode = (typeof CONTROL_OVERLAY_MODES)[number];
export type OverlayProfile = (typeof CONTROL_OVERLAY_PROFILES)[number];
export type OverlayDataClassification = (typeof CONTROL_OVERLAY_DATA_CLASSIFICATIONS)[number];
export type OverlayTargetActionKind = (typeof CONTROL_OVERLAY_TARGET_ACTION_KINDS)[number];

export interface ControlOverlayControlsV2 {
  pause: boolean;
  resume: boolean;
  stop: boolean;
}

export interface ControlOverlayStepV2 {
  current: number | null;
  total: number | null;
}

export interface ControlOverlayMediaFrameBindingV2 {
  kind: "media_frame";
  media_sha256: string;
  frame_index: number;
}

export interface ControlOverlayObservationBindingV2 {
  kind: "observation_hmac_sha256";
  observation_hmac_sha256: string;
}

export interface ControlOverlayTargetTrackingV2 {
  coordinate_space: "top_level_viewport_normalized";
  rect: { x: number; y: number; width: number; height: number };
  source_viewport: {
    width_css_px: number;
    height_css_px: number;
    device_pixel_ratio: number;
  };
  binding: ControlOverlayMediaFrameBindingV2 | ControlOverlayObservationBindingV2;
  action_kind: OverlayTargetActionKind | null;
}

export interface ControlOverlayFrameV2 {
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
}

export interface ControlOverlayTimelineEventV2 {
  at_ms: number;
  media_frame_index: number;
  frame: ControlOverlayFrameV2;
}

export interface ControlOverlayTimelineV2 {
  schema_version: typeof CONTROL_OVERLAY_TIMELINE_VERSION;
  data_classification: OverlayDataClassification;
  evidence_pack_id: string;
  media_sha256: string;
  media_frame_count: number;
  duration_ms: number;
  events: ControlOverlayTimelineEventV2[];
}
