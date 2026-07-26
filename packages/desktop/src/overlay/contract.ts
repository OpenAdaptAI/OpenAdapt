import type { ControlOverlayState } from "./state";
import { overlayAllowsInteraction } from "./state";
import {
  CONTROL_OVERLAY_FRAME_VERSION,
  CONTROL_OVERLAY_STATUS_BY_PHASE,
  CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE,
  type ControlOverlayFrameV2,
} from "./generated/contract";

export { CONTROL_OVERLAY_FRAME_VERSION } from "./generated/contract";

export interface OverlayFrameTiming {
  event_sequence: number;
  observed_at_unix_ms: number;
  observed_at_monotonic_ms: number;
}

export function controlOverlayStateId(state: ControlOverlayState): string {
  const controls = overlayAllowsInteraction(state.phase)
    ? state.controls
    : { pause: false, resume: false, stop: false };
  return [
    state.visible ? "visible" : "hidden",
    state.phase,
    state.mode,
    state.profile ?? "no-profile",
    state.currentStep ?? "no-step",
    state.totalSteps ?? "no-total",
    controls.pause ? "pause" : "no-pause",
    controls.resume ? "resume" : "no-resume",
    controls.stop ? "stop" : "no-stop",
    // V2 state identity binds the exact optional target projection. Desktop's
    // local status producer never guesses a target; Flow supplies it later.
    "no-target",
  ].join(":");
}

export function buildControlOverlayFrame(
  state: ControlOverlayState,
  timing: OverlayFrameTiming,
): ControlOverlayFrameV2 {
  const controls = overlayAllowsInteraction(state.phase)
    ? { ...state.controls }
    : { pause: false, resume: false, stop: false };
  return {
    schema_version: CONTROL_OVERLAY_FRAME_VERSION,
    state_id: controlOverlayStateId(state),
    event_sequence: timing.event_sequence,
    observed_at_unix_ms: timing.observed_at_unix_ms,
    observed_at_monotonic_ms: timing.observed_at_monotonic_ms,
    visible: state.visible,
    phase: state.phase,
    workflow_label: CONTROL_OVERLAY_WORKFLOW_LABEL_BY_MODE[state.mode],
    mode: state.mode,
    profile: state.profile,
    step: { current: state.currentStep, total: state.totalSteps },
    controls,
    status: CONTROL_OVERLAY_STATUS_BY_PHASE[state.phase],
    target_tracking: null,
    presentation: true,
  };
}
