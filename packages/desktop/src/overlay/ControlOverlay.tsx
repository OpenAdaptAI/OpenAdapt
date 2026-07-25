import { useEffect, useMemo, useRef, useState } from "react";
import { emit, listen } from "@tauri-apps/api/event";
import {
  CMD,
  EVT,
  engineInvoke,
  engineTry,
  inTauri,
  onEngineEvent,
  setControlOverlayInteractive,
  setControlOverlayPresentation,
  setControlOverlayVisible,
} from "../lib/engine";
import type {
  EngineStatus,
  ReplayProgress,
  RunnerStatus,
  RunStep,
  Workflow,
} from "../lib/types";
import { overlayPresentationEnabled } from "./preferences";
import { buildControlOverlayFrame } from "./contract";
import {
  EMPTY_OVERLAY_STATE,
  overlayAllowsInteraction,
  reduceControlOverlay,
  type ControlOverlayInput,
} from "./state";
import "./overlay.css";

function modeLabel(
  mode: "demonstration" | "replay" | "governed" | "managed",
  profile: "demo" | "standard" | "regulated" | null,
): string {
  if (profile) return `${profile} profile`;
  switch (mode) {
    case "demonstration":
      return "demonstration";
    case "governed":
      return "governed run";
    case "managed":
      return "managed run";
    default:
      return "local replay";
  }
}

export function ControlOverlay() {
  const [state, setState] = useState(EMPTY_OVERLAY_STATE);
  const [presentation, setPresentation] = useState(
    overlayPresentationEnabled,
  );
  const [busy, setBusy] = useState(false);
  const [controlError, setControlError] = useState(false);
  const [capturePolicyReady, setCapturePolicyReady] = useState(false);
  const frameSequence = useRef(0);

  function send(input: ControlOverlayInput) {
    setState((current) => reduceControlOverlay(current, input));
  }

  useEffect(() => {
    let cancelled = false;
    void setControlOverlayPresentation(presentation)
      .then(() => {
        if (!cancelled) setCapturePolicyReady(true);
      })
      .catch((error) => {
        if (!cancelled) {
          setCapturePolicyReady(false);
          console.error("Control overlay capture policy failed", error);
          void setControlOverlayVisible(false).catch(() => {});
        }
      });
    void engineTry<EngineStatus>(CMD.GET_STATUS, {}, {
      recording: false,
      paused: false,
    }).then((status) => send({ kind: "recording-status", status }));

    const unsubs = [
      onEngineEvent<EngineStatus>(EVT.STATUS_UPDATE, (status) =>
        send({ kind: "recording-status", status }),
      ),
      onEngineEvent(EVT.RECORDING_STARTED, () =>
        send({ kind: "recording-started" }),
      ),
      onEngineEvent(EVT.RECORDING_STOPPED, () =>
        send({ kind: "recording-stopped" }),
      ),
      onEngineEvent(EVT.RECORDING_ERROR, () =>
        send({ kind: "recording-error" }),
      ),
      onEngineEvent<ReplayProgress>(EVT.REPLAY_PROGRESS, (progress) => {
        send({ kind: "replay-progress", progress });
        if (progress.state === "running") {
          void engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []).then(
            (workflows) => {
              const ordinal = workflows.findIndex(
                (item) => item.id === progress.workflow_id,
              );
              const workflow = workflows[ordinal];
              if (workflow) {
                send({
                  kind: "workflow-metadata",
                  ordinal: ordinal + 1,
                  totalSteps: workflow.steps,
                });
              }
            },
          );
        }
      }),
      onEngineEvent<RunnerStatus>(EVT.RUNNER_STATE, (status) =>
        send({ kind: "runner-state", status }),
      ),
      onEngineEvent<RunStep | { line: string }>(EVT.LOG_LINE, (payload) => {
        // Only the bounded step ordinal is accepted. Never project the log
        // line, action, target, evidence, or typed value into the overlay.
        if ("index" in payload && Number.isInteger(payload.index)) {
          send({ kind: "step", index: payload.index });
        }
      }),
    ];

    if (inTauri()) {
      unsubs.push(
        listen<boolean>("overlay://presentation", (event) =>
          setPresentation(Boolean(event.payload)),
        ),
      );
    }

    return () => {
      cancelled = true;
      unsubs.forEach((promise) => promise.then((stop) => stop()).catch(() => {}));
    };
    // The native presentation event owns subsequent updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const interactive = overlayAllowsInteraction(state.phase);

  useEffect(() => {
    let cancelled = false;
    async function synchronizeNativeWindow() {
      if (!state.visible || !capturePolicyReady) {
        await setControlOverlayVisible(false);
        return;
      }
      try {
        // The native command hides the window before enforcing click-through.
        // Show it only after the OS acknowledges the required input policy.
        await setControlOverlayInteractive(interactive);
        if (!cancelled) await setControlOverlayVisible(true);
      } catch (error) {
        console.error("Control overlay input policy failed", error);
        if (!cancelled) await setControlOverlayVisible(false).catch(() => {});
      }
    }
    void synchronizeNativeWindow().catch((error) => {
      console.error("Control overlay window synchronization failed", error);
    });
    return () => {
      cancelled = true;
    };
  }, [capturePolicyReady, interactive, state.visible]);

  useEffect(() => {
    if (state.phase !== "verified") return;
    const timeout = window.setTimeout(() => send({ kind: "dismiss" }), 4000);
    return () => window.clearTimeout(timeout);
  }, [state.phase, state.visible]);

  const displayFrame = useMemo(
    () => {
      return buildControlOverlayFrame(state, presentation, {
        event_sequence: 0,
        observed_at_unix_ms: 0,
        observed_at_monotonic_ms: 0,
      });
    },
    [presentation, state],
  );

  useEffect(() => {
    if (!inTauri()) return;
    // Always broadcast the presentation-safe projection. A future deterministic
    // video compositor can consume this exact event without seeing the local
    // workflow label, even when the local overlay itself is content-protected.
    frameSequence.current += 1;
    const safeFrame = buildControlOverlayFrame(state, true, {
      event_sequence: frameSequence.current,
      observed_at_unix_ms: Date.now(),
      observed_at_monotonic_ms: Math.round(performance.now() * 1000) / 1000,
    });
    void emit("overlay://frame", safeFrame).catch(() => {});
  }, [state]);

  const stepLabel = useMemo(() => {
    if (state.currentStep !== null && state.totalSteps !== null) {
      return `Step ${state.currentStep} of ${state.totalSteps}`;
    }
    if (state.currentStep !== null) return `Step ${state.currentStep}`;
    if (state.totalSteps !== null) return `${state.totalSteps} steps`;
    return "";
  }, [state.currentStep, state.totalSteps]);

  async function control(action: "pause" | "resume" | "stop") {
    const supported =
      action === "pause"
        ? state.controls.pause
        : action === "resume"
          ? state.controls.resume
          : state.controls.stop;
    if (!supported || busy) return;
    setBusy(true);
    setControlError(false);
    send({ kind: "control-requested", action });
    try {
      const result = await engineInvoke<EngineStatus>(
        action === "pause"
          ? CMD.PAUSE_RECORDING
          : action === "resume"
            ? CMD.RESUME_RECORDING
            : CMD.STOP_RECORDING,
        {},
      );
      if (action !== "stop" && result && typeof result.recording === "boolean") {
        send({ kind: "recording-status", status: result });
      }
    } catch {
      setControlError(true);
      send({ kind: "control-failed" });
    } finally {
      setBusy(false);
    }
  }

  const showResume = state.phase === "paused";
  const pauseAvailable = showResume
    ? state.controls.resume
    : state.controls.pause;
  const controlHelp = pauseAvailable
    ? undefined
    : "This operation does not currently advertise lossless pause or resume support.";

  return (
    <section
      className={`control-overlay phase-${state.phase} ${presentation ? "presentation" : ""}`}
      aria-label="OpenAdapt automation controls"
    >
      <div className="overlay-main" data-tauri-drag-region>
        <div className="overlay-mark" aria-label="OpenAdapt">
          <span className="overlay-open">Open</span>
          <strong>Adapt</strong>
          {presentation && <span className="demo-badge">demo view</span>}
        </div>
        <div className="overlay-copy" data-tauri-drag-region>
          <div className="overlay-meta" data-tauri-drag-region>
            <strong>{displayFrame.workflow_label}</strong>
            <span>{modeLabel(state.mode, state.profile)}</span>
            {stepLabel && <span>{stepLabel}</span>}
          </div>
          <div className="overlay-status" role="status" aria-live="polite">
            <span className="overlay-pulse" aria-hidden="true" />
            {displayFrame.status}
          </div>
        </div>
      </div>

      {interactive && (
        <div className="overlay-controls" aria-label="Run controls">
          <button
            type="button"
            className="overlay-button"
            disabled={busy || !pauseAvailable}
            title={controlHelp}
            aria-describedby={!pauseAvailable ? "pause-unavailable" : undefined}
            aria-label={showResume ? "Resume OpenAdapt" : "Pause OpenAdapt"}
            onClick={() => control(showResume ? "resume" : "pause")}
          >
            {showResume ? "Resume" : "Pause"}
          </button>
          <button
            type="button"
            className="overlay-button stop"
            disabled={busy || !state.controls.stop}
            aria-describedby={
              !state.controls.stop ? "stop-unavailable" : undefined
            }
            title={
              state.controls.stop
                ? "Finalize this recording"
                : "Safe interruption is not available for this operation"
            }
            aria-label="Stop OpenAdapt"
            onClick={() => control("stop")}
          >
            Stop
          </button>
          <button
            type="button"
            className="overlay-dismiss"
            aria-label="Hide OpenAdapt control overlay"
            onClick={() => send({ kind: "dismiss" })}
          >
            ×
          </button>
        </div>
      )}
      {controlError && (
        <span className="sr-only" role="alert">
          The control could not be delivered. Open OpenAdapt Desktop for details.
        </span>
      )}
      <span className="sr-only" id="pause-unavailable">
        Lossless pause and resume are unavailable for this operation.
      </span>
      <span className="sr-only" id="stop-unavailable">
        Safe interruption is unavailable for this operation.
      </span>
    </section>
  );
}
