import { useEffect, useMemo, useRef, useState } from "react";
import { emit } from "@tauri-apps/api/event";
import {
  CMD,
  EVT,
  engineInvoke,
  engineTry,
  ensureControlOverlayCaptureExcluded,
  inTauri,
  onEngineEvent,
  setControlOverlayInteractive,
  setControlOverlayLayout,
  setControlOverlayVisible,
} from "../lib/engine";
import type {
  EngineStatus,
  ReplayProgress,
  RunnerStatus,
  RunStep,
  Workflow,
} from "../lib/types";
import {
  AUTH_PAUSE_COPY,
  EMPTY_COACH,
  coachHoldsPause,
  overlayLayoutFor,
  type CoachOperatorResponse,
} from "./coach";
import { buildControlOverlayFrame } from "./contract";
import {
  EMPTY_OVERLAY_STATE,
  overlayAllowsInteraction,
  overlayExpands,
  overlayPlainStatus,
  overlaySafetyLabel,
  overlaySecondaryItems,
  overlayShowsExecutionRail,
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
  const [busy, setBusy] = useState(false);
  const [controlError, setControlError] = useState(false);
  const [capturePolicyReady, setCapturePolicyReady] = useState(false);
  const [nowUnixMs, setNowUnixMs] = useState(Date.now);
  const frameSequence = useRef(0);

  function send(input: ControlOverlayInput) {
    setState((current) => reduceControlOverlay(current, input));
  }

  useEffect(() => {
    let cancelled = false;
    void ensureControlOverlayCaptureExcluded()
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
        send({
          kind: "replay-progress",
          progress,
          observedAtUnixMs: Date.now(),
        });
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
      onEngineEvent(EVT.COACH, (payload) =>
        send({ kind: "coach", payload }),
      ),
      onEngineEvent<RunStep | { line: string }>(EVT.LOG_LINE, (payload) => {
        // Only the bounded step ordinal is accepted. Never project the log
        // line, action, target, evidence, or typed value into the overlay.
        if ("index" in payload && Number.isInteger(payload.index)) {
          send({ kind: "step", index: payload.index });
        }
      }),
    ];

    return () => {
      cancelled = true;
      unsubs.forEach((promise) => promise.then((stop) => stop()).catch(() => {}));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const interactive = overlayAllowsInteraction(state.phase);
  const expanded = overlayExpands(state.phase);
  const coach = state.coach;
  const layout = overlayLayoutFor(
    state.visible,
    interactive,
    expanded,
    coach,
  );
  const stage = layout === "stage";

  useEffect(() => {
    if (!state.visible || state.startedAtUnixMs === null || expanded) return;
    setNowUnixMs(Date.now());
    const timer = window.setInterval(() => setNowUnixMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [expanded, state.startedAtUnixMs, state.visible]);

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

  useEffect(() => {
    void setControlOverlayLayout(layout).catch((error) => {
      console.error("Control overlay layout failed", error);
    });
  }, [layout]);

  useEffect(() => {
    if (!inTauri()) return;
    // Always broadcast the presentation-safe projection. A future deterministic
    // video compositor can consume this exact event without seeing the local
    // workflow label, even when the local overlay itself is content-protected.
    frameSequence.current += 1;
    const safeFrame = buildControlOverlayFrame(state, {
      event_sequence: frameSequence.current,
      observed_at_unix_ms: Date.now(),
      observed_at_monotonic_ms: Math.round(performance.now() * 1000) / 1000,
    });
    void emit("overlay://frame", safeFrame).catch(() => {});
  }, [state]);

  useEffect(() => {
    if (!inTauri()) return;
    // Local operator channel. Never persist, compose, ingest, or seal this.
    void emit("overlay://coach", coach ?? EMPTY_COACH).catch(() => {});
  }, [coach]);

  const stepLabel = useMemo(() => {
    if (state.currentStep !== null && state.totalSteps !== null) {
      return `Step ${state.currentStep} of ${state.totalSteps}`;
    }
    if (state.currentStep !== null) return `Step ${state.currentStep}`;
    if (state.totalSteps !== null) return `${state.totalSteps} steps`;
    return "Step pending";
  }, [state.currentStep, state.totalSteps]);
  const secondaryItems = useMemo(
    () => overlaySecondaryItems(state, nowUnixMs),
    [nowUnixMs, state],
  );

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
      if (action === "resume" && coachHoldsPause(coach)) {
        send({ kind: "coach", payload: { operator_response: "continue" } });
        await engineInvoke(CMD.SET_COACH, { operator_response: "continue" });
      }
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

  async function respond(response: CoachOperatorResponse) {
    if (!interactive || busy) return;
    setBusy(true);
    setControlError(false);
    send({ kind: "coach", payload: { operator_response: response } });
    try {
      await engineInvoke(CMD.SET_COACH, { operator_response: response });
    } catch {
      setControlError(true);
    } finally {
      setBusy(false);
    }
  }

  const showResume = state.phase === "paused";
  const coachPaused = coachHoldsPause(coach);
  const pauseAvailable = showResume
    ? state.controls.resume
    : state.controls.pause;
  const controlHelp = pauseAvailable
    ? undefined
    : "This operation does not currently advertise lossless pause or resume support.";
  const authPause =
    coachPaused &&
    (coach?.turn === "auth" ||
      coach?.pause_reason === "auth" ||
      coach?.pause_reason === "secret_field");
  const feedbackPause =
    coachPaused &&
    (coach?.turn === "feedback" ||
      coach?.pause_reason === "wrong_step" ||
      coach?.pause_reason === "skip" ||
      coach?.pause_reason === "done");
  const target = stage ? coach?.target : null;
  const continueLabel = coachPaused
    ? "Continue"
    : showResume
      ? state.pausePrompt
        ? "Continue"
        : "Resume"
      : "Pause";

  return (
    <div className={`control-overlay-host${stage ? " stage" : ""}`}>
      {target && (
        <div
          className="overlay-target-ring"
          aria-hidden="true"
          style={{
            left: `${target.rect.x * 100}%`,
            top: `${target.rect.y * 100}%`,
            width: `${target.rect.width * 100}%`,
            height: `${target.rect.height * 100}%`,
          }}
        />
      )}
      <section
        className={`control-overlay phase-${state.phase} ${expanded ? "expanded" : "compact"}`}
        aria-label="OpenAdapt automation controls"
      >
        <div className="overlay-main" data-tauri-drag-region>
          <div className="overlay-copy" data-tauri-drag-region>
            <div className="overlay-primary" role="status" aria-live="polite">
              <span className="overlay-pulse" aria-hidden="true" />
              <strong>
                {state.pausePrompt || overlayPlainStatus(state.phase)}
              </strong>
              <span className="overlay-step">{stepLabel}</span>
              {state.profile && (
                <span className="overlay-profile">{state.profile}</span>
              )}
              {coach?.turn === "your_turn" && !interactive && (
                <span className="overlay-turn">Your turn</span>
              )}
              <span className="overlay-safety">
                {overlaySafetyLabel(state.phase)}
              </span>
            </div>
            {coach?.hint && !interactive && (
              <div className="overlay-hint" data-tauri-drag-region>
                {coach.hint}
              </div>
            )}
            {overlayShowsExecutionRail(state.phase) && (
              <div className="overlay-rail" aria-label="Resolve, act, verify">
                <span>Resolve</span>
                <i aria-hidden="true" />
                <span>Act</span>
                <i aria-hidden="true" />
                <span>Verify</span>
              </div>
            )}
            {secondaryItems.length > 0 && (
              <div className="overlay-secondary" data-tauri-drag-region>
                {secondaryItems.map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            )}
            {authPause && (
              <p className="overlay-pause-copy">{AUTH_PAUSE_COPY}</p>
            )}
            {feedbackPause && (
              <p className="overlay-pause-copy">Was this the right step?</p>
            )}
            {expanded && !coachPaused && (
              <div className="overlay-details" data-tauri-drag-region>
                <strong>{state.localWorkflowLabel}</strong>
                <span>{modeLabel(state.mode, state.profile)}</span>
              </div>
            )}
          </div>
        </div>

        {interactive && (
          <div className="overlay-controls" aria-label="Run controls">
            {feedbackPause && (
              <>
                <button
                  type="button"
                  className="overlay-button"
                  disabled={busy}
                  aria-label="Mark the last step as wrong"
                  onClick={() => respond("wrong")}
                >
                  That was wrong
                </button>
                <button
                  type="button"
                  className="overlay-button"
                  disabled={busy}
                  aria-label="Skip this suggested step"
                  onClick={() => respond("skip")}
                >
                  Skip
                </button>
              </>
            )}
            <button
              type="button"
              className="overlay-button"
              disabled={busy || !pauseAvailable}
              title={controlHelp}
              aria-describedby={!pauseAvailable ? "pause-unavailable" : undefined}
              aria-label={
                coachPaused
                  ? "Continue OpenAdapt"
                  : showResume
                    ? state.pausePrompt
                      ? "Continue OpenAdapt"
                      : "Resume OpenAdapt"
                    : "Pause OpenAdapt"
              }
              onClick={() =>
                coachPaused ? respond("continue") : control(showResume ? "resume" : "pause")
              }
            >
              {continueLabel}
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
    </div>
  );
}
