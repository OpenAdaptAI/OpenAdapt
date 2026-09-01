import type {
  EngineStatus,
  ReplayProgress,
  RunnerStatus,
  TargetBackend,
} from "../lib/types";
import {
  coachHoldsPause,
  EMPTY_COACH,
  normalizeCoachPayload,
  type ControlOverlayCoachV1,
} from "./coach";
import {
  CONTROL_OVERLAY_STATUS_BY_PHASE,
  type OverlayMode,
  type OverlayPhase,
  type OverlayProfile,
} from "./generated/contract";

export type { OverlayPhase } from "./generated/contract";

export interface OverlayControls {
  pause: boolean;
  resume: boolean;
  stop: boolean;
}

/**
 * Privacy-minimized state projected into the always-on-top window.
 *
 * It deliberately excludes screenshots, target descriptions, typed values,
 * identity evidence, report bodies, and exception text. This is a display and
 * control contract, not another runtime.
 */
export interface ControlOverlayState {
  visible: boolean;
  phase: OverlayPhase;
  localWorkflowLabel: string;
  mode: OverlayMode;
  profile: OverlayProfile | null;
  currentStep: number | null;
  totalSteps: number | null;
  controls: OverlayControls;
  surface: TargetBackend | "configured" | null;
  startedAtUnixMs: number | null;
  elapsedSeconds: number | null;
  evidenceClasses: string[];
  modelCalls: number | null;
  externalNetworkCalls: "none" | "observed" | "unknown" | null;
  pausePrompt: string | null;
  /** Local-only. Never projected into overlay://frame. */
  coach: ControlOverlayCoachV1 | null;
}

export type ControlOverlayInput =
  | { kind: "recording-status"; status: EngineStatus }
  | { kind: "recording-started" }
  | { kind: "recording-stopped" }
  | { kind: "recording-error" }
  | {
      kind: "replay-progress";
      progress: ReplayProgress;
      observedAtUnixMs?: number;
    }
  | { kind: "runner-state"; status: RunnerStatus }
  | { kind: "workflow-metadata"; ordinal: number; totalSteps: number }
  | { kind: "step"; index: number; total?: number | null }
  | { kind: "control-requested"; action: "pause" | "resume" | "stop" }
  | { kind: "control-failed" }
  | { kind: "dismiss" }
  | { kind: "coach"; payload: unknown };

export const EMPTY_OVERLAY_STATE: ControlOverlayState = {
  visible: false,
  phase: "idle",
  localWorkflowLabel: "Local workflow",
  mode: "demonstration",
  profile: null,
  currentStep: null,
  totalSteps: null,
  controls: { pause: false, resume: false, stop: false },
  surface: null,
  startedAtUnixMs: null,
  elapsedSeconds: null,
  evidenceClasses: [],
  modelCalls: null,
  externalNetworkCalls: null,
  pausePrompt: null,
  coach: null,
};

/** Only states with no in-flight observation or actuation may receive input. */
export function overlayAllowsInteraction(phase: OverlayPhase): boolean {
  return (
    phase === "paused" ||
    phase === "verified" ||
    phase === "completed_unverified" ||
    phase === "halted" ||
    phase === "failed" ||
    phase === "rolled_back"
  );
}

function recordingState(
  status: EngineStatus,
  coach: ControlOverlayState["coach"] = null,
): ControlOverlayState {
  if (!status.recording) return EMPTY_OVERLAY_STATE;
  const capabilities = status.controls ?? {
    pause: false,
    resume: false,
    stop: true,
  };
  const holdsPause = coachHoldsPause(coach);
  return {
    ...EMPTY_OVERLAY_STATE,
    visible: true,
    phase: status.paused || holdsPause ? "paused" : "recording",
    localWorkflowLabel: "New demonstration",
    mode: "demonstration",
    controls: holdsPause
      ? { pause: false, resume: true, stop: true }
      : capabilities,
    elapsedSeconds:
      typeof status.duration_secs === "number" ? status.duration_secs : null,
    pausePrompt:
      status.paused && typeof status.pause_prompt === "string"
        ? status.pause_prompt
        : null,
    coach,
  };
}

function applyCoach(
  state: ControlOverlayState,
  payload: unknown,
): ControlOverlayState {
  const coach = normalizeCoachPayload(payload, state.coach ?? EMPTY_COACH);
  if (!state.visible) {
    return { ...state, coach };
  }
  if (coachHoldsPause(coach)) {
    return {
      ...state,
      visible: true,
      phase: "paused",
      controls: { pause: false, resume: true, stop: true },
      coach,
    };
  }
  if (
    state.phase === "paused" &&
    state.mode === "demonstration" &&
    !coachHoldsPause(coach) &&
    !state.pausePrompt
  ) {
    return {
      ...state,
      phase: "recording",
      controls: { pause: false, resume: false, stop: true },
      coach,
    };
  }
  return { ...state, coach };
}

function terminalPhase(progress: ReplayProgress): OverlayPhase {
  switch (progress.outcome) {
    case "VERIFIED":
      return "verified";
    case "COMPLETED_UNVERIFIED":
    case "success":
      return "completed_unverified";
    case "HALTED":
    case "halt":
      return "halted";
    case "FAILED":
      return "failed";
    case "ROLLED_BACK":
      return "rolled_back";
    case "unknown":
      return "failed";
  }
  if (progress.state === "running") return "executing";
  if (progress.state === "halted") return "halted";
  if (progress.state === "completed_unverified") return "completed_unverified";
  if (progress.state === "rolled_back") return "rolled_back";
  if (progress.state === "failed" || progress.state === "error") return "failed";
  // ``done`` is a legacy transport state shared by VERIFIED and old generic
  // success. Without the precise outcome contract it remains unverified.
  return "completed_unverified";
}

export function reduceControlOverlay(
  state: ControlOverlayState,
  input: ControlOverlayInput,
): ControlOverlayState {
  switch (input.kind) {
    case "recording-status":
      if (
        (state.phase === "pausing" && !input.status.paused) ||
        (state.phase === "resuming" && input.status.paused) ||
        (state.phase === "stopping" && input.status.recording)
      ) {
        return state;
      }
      if (!input.status.recording) {
        if (
          state.phase === "recording" ||
          state.phase === "paused" ||
          state.phase === "pausing" ||
          state.phase === "resuming" ||
          state.phase === "stopping"
        ) {
          return EMPTY_OVERLAY_STATE;
        }
        if (state.phase !== "idle") return state;
      }
      return recordingState(input.status, state.coach);
    case "recording-started":
      return recordingState(
        {
          recording: true,
          paused: false,
          controls: { pause: false, resume: false, stop: true },
        },
        state.coach,
      );
    case "recording-stopped":
      return EMPTY_OVERLAY_STATE;
    case "recording-error":
      return {
        ...state,
        visible: true,
        phase: "failed",
        controls: { pause: false, resume: false, stop: false },
      };
    case "replay-progress": {
      const { progress } = input;
      const mode =
        progress.mode === "governed"
          ? "governed"
          : progress.mode === "managed"
            ? "managed"
            : "replay";
      return {
        ...state,
        visible: true,
        phase: terminalPhase(progress),
        mode,
        surface: progress.backend,
        profile: progress.profile ?? state.profile,
        startedAtUnixMs:
          progress.state === "running"
            ? state.startedAtUnixMs ?? input.observedAtUnixMs ?? null
            : state.startedAtUnixMs,
        elapsedSeconds:
          typeof progress.duration_s === "number"
            ? progress.duration_s
            : state.elapsedSeconds,
        evidenceClasses: Array.isArray(progress.evidence_classes)
          ? [...progress.evidence_classes]
          : state.evidenceClasses,
        modelCalls:
          typeof progress.model_calls === "number"
            ? progress.model_calls
            : state.modelCalls,
        externalNetworkCalls:
          progress.external_network_calls ?? state.externalNetworkCalls,
        currentStep:
          typeof progress.current_step === "number"
            ? progress.current_step
            : progress.state === "running"
              ? null
              : state.currentStep,
        totalSteps:
          typeof progress.total_steps === "number"
            ? progress.total_steps
            : state.totalSteps,
        controls: { pause: false, resume: false, stop: false },
      };
    }
    case "runner-state":
      if (input.status.state === "running") {
        return {
          ...state,
          visible: true,
          phase: "executing",
          mode: "managed",
          surface: state.surface,
          localWorkflowLabel: "Managed workflow",
          controls: { pause: false, resume: false, stop: false },
        };
      }
      if (state.mode !== "managed" || state.phase !== "executing") return state;
      return {
        ...state,
        visible: true,
        phase:
          input.status.last_runs[0]?.outcome === "VERIFIED"
            ? "verified"
            : input.status.last_runs[0]?.outcome === "RECONCILIATION_REQUIRED"
              ? "failed"
              : input.status.last_runs[0]?.outcome === "HALTED" ||
                  input.status.last_runs[0]?.outcome === "HALTED_BEFORE_EFFECT" ||
                input.status.last_runs[0]?.outcome === "halted-needs-attention"
              ? "halted"
              : input.status.state === "error"
                ? "failed"
                : input.status.last_runs[0]?.outcome === "ROLLED_BACK"
                  ? "rolled_back"
                  : "completed_unverified",
      };
    case "workflow-metadata":
      return {
        ...state,
        // A workflow name is user-authored and may contain identity data. The
        // overlay uses a stable local ordinal instead of heuristically claiming
        // that free-form text has been scrubbed.
        localWorkflowLabel: `Local workflow ${Math.max(1, input.ordinal)}`,
        totalSteps: input.totalSteps || state.totalSteps,
      };
    case "step":
      if (!state.visible || input.index < 0) return state;
      return {
        ...state,
        currentStep: input.index + 1,
        totalSteps: input.total ?? state.totalSteps,
      };
    case "control-requested":
      return {
        ...state,
        visible: true,
        phase:
          input.action === "stop"
            ? "stopping"
            : input.action === "pause"
              ? "pausing"
              : "resuming",
        controls: { pause: false, resume: false, stop: false },
      };
    case "control-failed":
      return {
        ...state,
        visible: true,
        phase: "failed",
        controls: { pause: false, resume: false, stop: false },
      };
    case "dismiss":
      return { ...state, visible: false, coach: null };
    case "coach":
      return applyCoach(state, input.payload);
  }
}

export function overlayStatusText(state: ControlOverlayState): string {
  return CONTROL_OVERLAY_STATUS_BY_PHASE[state.phase];
}

export function overlayExpands(phase: OverlayPhase): boolean {
  return (
    phase === "paused" ||
    phase === "verified" ||
    phase === "completed_unverified" ||
    phase === "halted" ||
    phase === "failed" ||
    phase === "rolled_back"
  );
}

const SURFACE_LABEL: Record<TargetBackend | "configured", string> = {
  web: "Browser",
  windows: "Windows",
  macos: "macOS",
  linux: "Linux",
  rdp: "RDP",
  citrix: "Citrix",
  configured: "Configured surface",
};

export function overlaySafetyLabel(phase: OverlayPhase): string {
  switch (phase) {
    case "verified":
      return "VERIFIED";
    case "completed_unverified":
      return "UNVERIFIED";
    case "halted":
      return "HALTED";
    case "failed":
      return "FAILED";
    case "rolled_back":
      return "ROLLED BACK";
    case "paused":
      return "PAUSED";
    case "recording":
    case "observing":
      return "LOCAL CAPTURE";
    default:
      return "CHECKS ACTIVE";
  }
}

export function overlayPlainStatus(phase: OverlayPhase): string {
  switch (phase) {
    case "idle":
      return "Ready";
    case "observing":
      return "Resolving target";
    case "recording":
      return "Recording demonstration";
    case "executing":
      return "Running workflow";
    case "pausing":
      return "Pausing safely";
    case "paused":
      return "Paused safely";
    case "resuming":
      return "Resuming safely";
    case "stopping":
      return "Stopping safely";
    case "verifying":
      return "Verifying result";
    case "verified":
      return "Outcome verified";
    case "completed_unverified":
      return "Needs verification";
    case "halted":
      return "Stopped safely";
    case "failed":
      return "Run failed";
    case "rolled_back":
      return "Change rolled back";
  }
}

export function overlayShowsExecutionRail(phase: OverlayPhase): boolean {
  return !["idle", "observing", "recording"].includes(phase);
}

function compactDuration(seconds: number): string {
  const rounded = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function overlaySecondaryItems(
  state: ControlOverlayState,
  nowUnixMs: number,
): string[] {
  const items: string[] = [];
  if (state.surface) items.push(SURFACE_LABEL[state.surface]);
  const elapsed =
    state.elapsedSeconds ??
    (state.startedAtUnixMs === null
      ? null
      : Math.max(0, (nowUnixMs - state.startedAtUnixMs) / 1000));
  if (elapsed !== null) items.push(compactDuration(elapsed));

  const effectTiers = state.evidenceClasses
    .map((item) => /^effect_tier_([1-4])$/.exec(item)?.[1] ?? null)
    .filter((item): item is string => item !== null)
    .map(Number);
  if (effectTiers.length) {
    const tier = Math.min(...effectTiers);
    const phrase = {
      1: "independent system interface",
      2: "separate read-only session",
      3: "persisted-state reacquisition",
      4: "immediate screen confirmation",
    }[tier];
    items.push(`Effect evidence: ${phrase} (Tier ${tier})`);
  }
  if (state.modelCalls !== null) {
    items.push(
      state.modelCalls === 0
        ? "0 model calls"
        : `${state.modelCalls} model ${state.modelCalls === 1 ? "call" : "calls"}`,
    );
  }
  if (state.externalNetworkCalls !== null) {
    items.push(
      state.externalNetworkCalls === "none"
        ? "No external network calls"
        : state.externalNetworkCalls === "observed"
          ? "External network calls observed"
          : "External network-call status unknown",
    );
  }
  return items;
}
