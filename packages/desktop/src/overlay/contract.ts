import type { ControlOverlayState, OverlayPhase } from "./state";
import { overlayAllowsInteraction, overlayStatusText } from "./state";

export const CONTROL_OVERLAY_FRAME_VERSION =
  "openadapt.control-overlay-frame/v1" as const;

/**
 * Deterministic, PHI-safe frame for UI rendering or later video composition.
 * The only time fields are compositor alignment metadata. There is deliberately
 * no screenshot, action target, typed value, identity, evidence payload, URL,
 * or free-form runtime text in this contract.
 */
export interface ControlOverlayFrameV1 {
  schema_version: typeof CONTROL_OVERLAY_FRAME_VERSION;
  /** Stable semantic identity; repeated equivalent states share this ID. */
  state_id: string;
  /** Monotonic ordering for repeated visits to the same semantic state. */
  event_sequence: number;
  /** Wall-clock alignment for recordings produced outside the app. */
  observed_at_unix_ms: number;
  /** Monotonic alignment immune to wall-clock corrections. */
  observed_at_monotonic_ms: number;
  visible: boolean;
  phase: OverlayPhase;
  workflow_label: string;
  mode: "demonstration" | "replay" | "governed" | "managed";
  profile: "demo" | "standard" | "regulated" | null;
  step: {
    current: number | null;
    total: number | null;
  };
  controls: {
    pause: boolean;
    resume: boolean;
    stop: boolean;
  };
  status: string;
  presentation: boolean;
}

export interface OverlayFrameTiming {
  event_sequence: number;
  observed_at_unix_ms: number;
  observed_at_monotonic_ms: number;
}

function presentationWorkflowLabel(state: ControlOverlayState): string {
  switch (state.mode) {
    case "demonstration":
      return "Workflow demonstration";
    case "governed":
      return "Governed workflow";
    case "managed":
      return "Managed workflow";
    default:
      return "Workflow replay";
  }
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
  ].join(":");
}

export function buildControlOverlayFrame(
  state: ControlOverlayState,
  presentation: boolean,
  timing: OverlayFrameTiming,
): ControlOverlayFrameV1 {
  const workflowLabel = presentation
    ? presentationWorkflowLabel(state)
    : state.localWorkflowLabel;
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
    workflow_label: workflowLabel,
    mode: state.mode,
    profile: state.profile,
    step: { current: state.currentStep, total: state.totalSteps },
    controls,
    status: overlayStatusText(state),
    presentation,
  };
}
