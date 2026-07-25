import type {
  EngineStatus,
  ReplayProgress,
  RunnerStatus,
} from "../lib/types";

export type OverlayPhase =
  | "idle"
  | "observing"
  | "recording"
  | "pausing"
  | "paused"
  | "resuming"
  | "stopping"
  | "executing"
  | "verifying"
  | "verified"
  | "completed_unverified"
  | "halted"
  | "failed"
  | "rolled_back";

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
  mode: "demonstration" | "replay" | "governed" | "managed";
  profile: "demo" | "standard" | "regulated" | null;
  currentStep: number | null;
  totalSteps: number | null;
  controls: OverlayControls;
}

export type ControlOverlayInput =
  | { kind: "recording-status"; status: EngineStatus }
  | { kind: "recording-started" }
  | { kind: "recording-stopped" }
  | { kind: "recording-error" }
  | { kind: "replay-progress"; progress: ReplayProgress }
  | { kind: "runner-state"; status: RunnerStatus }
  | { kind: "workflow-metadata"; ordinal: number; totalSteps: number }
  | { kind: "step"; index: number; total?: number | null }
  | { kind: "control-requested"; action: "pause" | "resume" | "stop" }
  | { kind: "control-failed" }
  | { kind: "dismiss" };

export const EMPTY_OVERLAY_STATE: ControlOverlayState = {
  visible: false,
  phase: "idle",
  localWorkflowLabel: "Local workflow",
  mode: "demonstration",
  profile: null,
  currentStep: null,
  totalSteps: null,
  controls: { pause: false, resume: false, stop: false },
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

function recordingState(status: EngineStatus): ControlOverlayState {
  if (!status.recording) return EMPTY_OVERLAY_STATE;
  const capabilities = status.controls ?? {
    pause: false,
    resume: false,
    stop: true,
  };
  return {
    ...EMPTY_OVERLAY_STATE,
    visible: true,
    phase: status.paused ? "paused" : "recording",
    localWorkflowLabel: "New demonstration",
    mode: "demonstration",
    controls: capabilities,
  };
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
      return recordingState(input.status);
    case "recording-started":
      return recordingState({
        recording: true,
        paused: false,
        controls: { pause: false, resume: false, stop: true },
      });
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
        profile: progress.profile ?? state.profile,
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
            : input.status.last_runs[0]?.outcome === "HALTED" ||
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
      return { ...state, visible: false };
  }
}

export function overlayStatusText(state: ControlOverlayState): string {
  switch (state.phase) {
    case "recording":
      return "Watching your demonstration";
    case "observing":
      return "Observing the application";
    case "pausing":
      return "Pausing at a safe boundary";
    case "paused":
      return "Execution paused";
    case "resuming":
      return "Resuming at a safe boundary";
    case "stopping":
      return "Stopping at a safe boundary";
    case "executing":
      return "Executing with verification gates";
    case "verifying":
      return "Verifying the intended result";
    case "verified":
      return "Outcome verified";
    case "completed_unverified":
      return "Completed without sufficient verification";
    case "halted":
      return "Halted instead of guessing";
    case "failed":
      return "Execution failed";
    case "rolled_back":
      return "Compensating action completed";
    default:
      return "Ready";
  }
}
