// Record & review — drive a recording, then review + compile it.
// Recording state comes from the engine via events + get_status; after stop the
// capture can be scrubbed (PHI gate) and compiled into a workflow.
import { useEffect, useId, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type {
  BrowserRuntimeStatus,
  EngineStatus,
  ExecutionTarget,
} from "../lib/types";
import { ExecutionTargetForm } from "../ui/ExecutionTargetForm";
import { Button, Card, CardHead, Callout, Field, Pill } from "../ui/primitives";

function fmt(secs?: number | null) {
  if (secs == null) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function RecordReview({
  onCompiled,
}: {
  onCompiled: (id: string, target: ExecutionTarget) => void;
}) {
  const [status, setStatus] = useState<EngineStatus>({
    recording: false,
    paused: false,
    duration_secs: 0,
    capture_id: null,
    controls: { pause: false, resume: false, stop: false },
  });
  const [lastCapture, setLastCapture] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "compiling">("idle");
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState<ExecutionTarget>({ backend: "web" });
  const [task, setTask] = useState("");
  const [runtime, setRuntime] = useState<BrowserRuntimeStatus | null>(null);
  const fieldPrefix = useId();

  async function refresh() {
    const s = await engineTry<EngineStatus>(CMD.GET_STATUS, {}, status);
    setStatus(s);
  }

  useEffect(() => {
    void refresh();
    const unsubs = [
      onEngineEvent(EVT.STATUS_UPDATE, (s: EngineStatus) => setStatus(s)),
      onEngineEvent(EVT.RECORDING_STOPPED, (d: { capture_id?: string }) => {
        if (d?.capture_id) setLastCapture(d.capture_id);
      }),
      onEngineEvent(EVT.BROWSER_RUNTIME, (next: BrowserRuntimeStatus) => {
        if (next.workflow_id === "recording") setRuntime(next);
      }),
    ];
    const t = setInterval(refresh, 1000);
    return () => {
      clearInterval(t);
      unsubs.forEach((p) => p.then((u) => u()).catch(() => {}));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function start() {
    setBusy(true);
    setRuntime(null);
    try {
      await engineInvoke(CMD.START_RECORDING, {
        target,
        ...(task.trim() ? { purpose: task.trim() } : {}),
      });
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    setBusy(true);
    try {
      const r = await engineInvoke<{ capture_id?: string }>(
        CMD.STOP_RECORDING,
        {},
      );
      if (r?.capture_id) setLastCapture(r.capture_id);
    } finally {
      setBusy(false);
    }
  }
  async function compile() {
    if (!lastCapture) return;
    setPhase("compiling");
    try {
      const r = await engineInvoke<{ workflow_id?: string }>(
        CMD.COMPILE_RECORDING,
        { capture_id: lastCapture },
      );
      if (r?.workflow_id) onCompiled(r.workflow_id, target);
    } finally {
      setPhase("idle");
    }
  }

  const recording = status.recording;

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Author</p>
          <h1>Record &amp; review</h1>
        </div>
      </div>

      <Card>
        <CardHead
          eyebrow="Target"
          title="What are you demonstrating?"
          sub="The same target contract follows this recording into compile and execution."
        />
        <ExecutionTargetForm
          target={target}
          onChange={setTarget}
          idPrefix={`${fieldPrefix}-record-target`}
          disabled={recording || busy}
        />
        <Field
          label="Task description"
          hint="Optional local description stored with this recording."
          htmlFor={`${fieldPrefix}-record-task`}
          hintId={`${fieldPrefix}-record-task-hint`}
        >
          <input
            id={`${fieldPrefix}-record-task`}
            className="input"
            value={task}
            disabled={recording || busy}
            aria-describedby={`${fieldPrefix}-record-task-hint`}
            onChange={(event) => setTask(event.target.value)}
          />
        </Field>
        <p className="page-sub">
          Target selectors are handed to OpenAdapt Flow through a short-lived
          private file; they never appear in the process command line or
          Desktop logs.
        </p>
        {target.backend === "web" && runtime && runtime.state !== "ready" && (
          <Callout
            tone={runtime.state === "error" ? "warn" : "info"}
            title={
              runtime.state === "installing"
                ? "Preparing the browser"
                : runtime.state === "error"
                  ? "Browser setup needs attention"
                  : "Checking the browser"
            }
          >
            {runtime.detail}
          </Callout>
        )}

        <div className="row">
          {recording ? (
            <>
              <span className="rec-dot" />
              <strong>Recording</strong>
              <Pill tone={status.paused ? "warn" : "run"}>
                {status.paused ? "paused" : "live"}
              </Pill>
            </>
          ) : (
            <>
              <Pill tone="neutral">idle</Pill>
              <span className="page-sub">Not recording</span>
            </>
          )}
          <span className="spacer" />
          <span className="mono tnum">{fmt(status.duration_secs)}</span>
        </div>

        <div className="row" style={{ marginTop: "var(--space-5)" }}>
          {!recording ? (
            <Button variant="primary" disabled={busy} onClick={start}>
              Start recording
            </Button>
          ) : (
            <>
              <Button
                variant="ghost"
                disabled={
                  busy ||
                  (status.paused
                    ? !status.controls?.resume
                    : !status.controls?.pause)
                }
                title={
                  status.paused
                    ? status.controls?.resume
                      ? undefined
                      : "Resume is not available for this recorder"
                    : status.controls?.pause
                      ? undefined
                      : "Pause is not available for this recorder; stop finalizes the recording safely"
                }
                onClick={() =>
                  engineInvoke(
                    status.paused ? CMD.RESUME_RECORDING : CMD.PAUSE_RECORDING,
                    {},
                  )
                }
              >
                {status.paused ? "Resume" : "Pause"}
              </Button>
              <Button
                variant="danger"
                disabled={busy || status.controls?.stop === false}
                onClick={stop}
              >
                Stop
              </Button>
            </>
          )}
        </div>
      </Card>

      {lastCapture && !recording && (
        <Card>
          <CardHead
            eyebrow="Review"
            title="Compile this recording"
            sub={`capture ${lastCapture}`}
          />
          <Callout tone="info" title="PHI stays local until you push">
            OpenAdapt scrubs the recording (fail-closed) before any upload. On
            the BYOC lane it is never uploaded — compile, replay, and teach all
            run here.
          </Callout>
          <div className="row" style={{ marginTop: "var(--space-4)" }}>
            <Button
              variant="primary"
              disabled={phase === "compiling"}
              onClick={compile}
            >
              {phase === "compiling" ? "Compiling…" : "Compile to workflow"}
            </Button>
            <Button
              variant="ghost"
              onClick={() =>
                engineInvoke(CMD.SCRUB_CAPTURE, { capture_id: lastCapture })
              }
            >
              Scrub &amp; inspect PHI
            </Button>
          </div>
        </Card>
      )}
    </div>
  );
}
