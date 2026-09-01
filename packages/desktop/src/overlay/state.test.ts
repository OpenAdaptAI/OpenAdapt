import { expect, it } from "vitest";
import type { ReplayProgress } from "../lib/types";
import {
  EMPTY_OVERLAY_STATE,
  overlayExpands,
  overlaySecondaryItems,
  reduceControlOverlay,
} from "./state";
import {
  buildControlOverlayFrame,
  CONTROL_OVERLAY_FRAME_VERSION,
} from "./contract";
import { AUTH_PAUSE_COPY } from "./coach";

it("shows the authoring pause card copy without sending titles", () => {
  const state = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-status",
    status: {
      recording: true,
      paused: true,
      pause_prompt: "Type in the application. Continue here when done.",
      controls: { pause: false, resume: true, stop: true },
    },
  });

  expect(state.phase).toBe("paused");
  expect(state.pausePrompt).toBe(
    "Type in the application. Continue here when done.",
  );
  expect(JSON.stringify(state)).not.toContain("title");
});

it("projects only advertised recording controls", () => {
  const state = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-status",
    status: {
      recording: true,
      paused: false,
      capture_id: "capture-local",
      controls: { pause: false, resume: false, stop: true },
    },
  });

  expect(state.phase).toBe("recording");
  expect(state.controls).toEqual({ pause: false, resume: false, stop: true });
  expect(JSON.stringify(state)).not.toContain("capture-local");
});

it("acknowledges cooperative stop before the recorder finalizes", () => {
  const recording = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-started",
  });
  const stopping = reduceControlOverlay(recording, {
    kind: "control-requested",
    action: "stop",
  });

  expect(stopping).toMatchObject({
    visible: true,
    phase: "stopping",
    controls: { pause: false, resume: false, stop: false },
  });
});

it("preserves precise terminal outcomes and bounded step progress", () => {
  const running: ReplayProgress = {
    workflow_id: "workflow-1",
    state: "running",
    backend: "citrix",
    mode: "governed",
    total_steps: 7,
  };
  const active = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "replay-progress",
    progress: running,
    observedAtUnixMs: 1_000,
  });
  const halted = reduceControlOverlay(active, {
    kind: "replay-progress",
    progress: {
      ...running,
      state: "halted",
      outcome: "HALTED",
      profile: "regulated",
      current_step: 3,
    },
  });

  expect(active).toMatchObject({
    phase: "executing",
    mode: "governed",
    totalSteps: 7,
    currentStep: null,
    surface: "citrix",
    startedAtUnixMs: 1_000,
  });
  expect(halted).toMatchObject({
    phase: "halted",
    profile: "regulated",
    currentStep: 3,
    totalSteps: 7,
  });
});

it("keeps active execution compact and expands only safe boundaries", () => {
  expect(overlayExpands("executing")).toBe(false);
  expect(overlayExpands("verifying")).toBe(false);
  expect(overlayExpands("paused")).toBe(true);
  expect(overlayExpands("halted")).toBe(true);
  expect(overlayExpands("verified")).toBe(true);
});

it("shows only authoritative compact execution details in plain language", () => {
  const details = overlaySecondaryItems(
    {
      ...EMPTY_OVERLAY_STATE,
      surface: "citrix",
      startedAtUnixMs: 1_000,
      evidenceClasses: ["identity", "effect_tier_2"],
      modelCalls: 0,
      externalNetworkCalls: "none",
    },
    66_000,
  );

  expect(details).toEqual([
    "Citrix",
    "1:05",
    "Effect evidence: separate read-only session (Tier 2)",
    "0 model calls",
    "No external network calls",
  ]);
});

it("keeps workflow identifiers and names out of shared overlay state", () => {
  const running = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "replay-progress",
    progress: {
      workflow_id: "workflow-1",
      state: "running",
      backend: "web",
    },
  });
  const projected = reduceControlOverlay(running, {
    kind: "workflow-metadata",
    ordinal: 3,
    totalSteps: 5,
  });
  expect(projected.localWorkflowLabel).toBe("Local workflow 3");
  expect(JSON.stringify(projected)).not.toContain("workflow-1");
});

it("requires the precise outcome contract before displaying verified", () => {
  const legacyDone = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "replay-progress",
    progress: {
      workflow_id: "workflow-1",
      state: "done",
      backend: "web",
    },
  });
  const verified = reduceControlOverlay(legacyDone, {
    kind: "replay-progress",
    progress: {
      workflow_id: "workflow-1",
      state: "done",
      outcome: "VERIFIED",
      backend: "web",
    },
  });
  const unverified = reduceControlOverlay(verified, {
    kind: "replay-progress",
    progress: {
      workflow_id: "workflow-1",
      state: "completed_unverified",
      outcome: "COMPLETED_UNVERIFIED",
      backend: "web",
    },
  });
  const rolledBack = reduceControlOverlay(unverified, {
    kind: "replay-progress",
    progress: {
      workflow_id: "workflow-1",
      state: "rolled_back",
      outcome: "ROLLED_BACK",
      backend: "web",
    },
  });

  expect(legacyDone.phase).toBe("completed_unverified");
  expect(verified.phase).toBe("verified");
  expect(unverified.phase).toBe("completed_unverified");
  expect(rolledBack.phase).toBe("rolled_back");
});

it("keeps governed hosted-run failures out of the completed state", () => {
  const running = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "runner-state",
    status: { enabled: true, state: "running", last_runs: [] },
  });
  const halted = reduceControlOverlay(running, {
    kind: "runner-state",
    status: {
      enabled: true,
      state: "polling",
      last_runs: [{ run_id: "run-halted", outcome: "HALTED_BEFORE_EFFECT" }],
    },
  });
  const runningAgain = reduceControlOverlay(halted, {
    kind: "runner-state",
    status: { enabled: true, state: "running", last_runs: [] },
  });
  const reconcile = reduceControlOverlay(runningAgain, {
    kind: "runner-state",
    status: {
      enabled: true,
      state: "polling",
      last_runs: [{ run_id: "run-uncertain", outcome: "RECONCILIATION_REQUIRED" }],
    },
  });

  expect(halted.phase).toBe("halted");
  expect(reconcile.phase).toBe("failed");
});

it("exports a deterministic presentation frame without the local workflow label", () => {
  const local = {
    ...EMPTY_OVERLAY_STATE,
    visible: true,
    phase: "executing" as const,
    localWorkflowLabel: "Patient Jane Doe",
    mode: "governed" as const,
    profile: "regulated" as const,
    currentStep: 2,
    totalSteps: 5,
  };
  const frame = buildControlOverlayFrame(local, {
    event_sequence: 4,
    observed_at_unix_ms: 1785000000123,
    observed_at_monotonic_ms: 1234.5,
  });

  expect(frame).toEqual({
    schema_version: CONTROL_OVERLAY_FRAME_VERSION,
    state_id:
      "visible:executing:governed:regulated:2:5:no-pause:no-resume:no-stop:no-target",
    event_sequence: 4,
    observed_at_unix_ms: 1785000000123,
    observed_at_monotonic_ms: 1234.5,
    visible: true,
    phase: "executing",
    workflow_label: "Governed workflow",
    mode: "governed",
    profile: "regulated",
    step: { current: 2, total: 5 },
    controls: { pause: false, resume: false, stop: false },
    status: "Executing with verification gates",
    target_tracking: null,
    presentation: true,
  });
  expect(JSON.stringify(frame)).not.toContain("Jane Doe");
});

it("keeps coach hints off the closed overlay://frame projection", () => {
  const recording = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-started",
  });
  const coached = reduceControlOverlay(recording, {
    kind: "coach",
    payload: {
      hint: "Open the claim screen",
      turn: "your_turn",
      pack_url: "https://openadapt.ai/j/secret",
    },
  });
  const frame = buildControlOverlayFrame(coached, {
    event_sequence: 1,
    observed_at_unix_ms: 1,
    observed_at_monotonic_ms: 1,
  });
  const encoded = JSON.stringify(frame);

  expect(coached.phase).toBe("recording");
  expect(coached.coach?.hint).toBe("Open the claim screen");
  expect(encoded).not.toContain("Open the claim screen");
  expect(encoded).not.toContain("your_turn");
  expect(encoded).not.toContain("openadapt.ai");
  expect(encoded).not.toContain("overlay://coach");
  expect(frame.target_tracking).toBeNull();
  expect(frame.status).not.toBe("VERIFIED");
});

it("latches a coach auth pause even while capture still reports recording", () => {
  const recording = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-started",
  });
  const paused = reduceControlOverlay(recording, {
    kind: "coach",
    payload: { turn: "auth", pause_reason: "auth" },
  });
  const stillPaused = reduceControlOverlay(paused, {
    kind: "recording-status",
    status: {
      recording: true,
      paused: false,
      controls: { pause: false, resume: false, stop: true },
    },
  });

  expect(paused.phase).toBe("paused");
  expect(stillPaused.phase).toBe("paused");
  expect(stillPaused.controls.resume).toBe(true);
  expect(AUTH_PAUSE_COPY).toContain("Type in the application");
});

it("omits a ghost ring from overlay state when the rect has no binding", () => {
  const recording = reduceControlOverlay(EMPTY_OVERLAY_STATE, {
    kind: "recording-started",
  });
  const unbound = reduceControlOverlay(recording, {
    kind: "coach",
    payload: {
      hint: "Open the claim screen",
      target: {
        coordinate_space: "top_level_viewport_normalized",
        rect: { x: 0.2, y: 0.2, width: 0.1, height: 0.1 },
      },
    },
  });
  expect(unbound.coach?.target).toBeNull();
});
