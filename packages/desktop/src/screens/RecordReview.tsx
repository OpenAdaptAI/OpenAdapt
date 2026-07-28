// Record & review — drive a recording, then review + compile it.
// Recording state comes from the engine via events + get_status; after stop the
// capture can be scrubbed (PHI gate) and compiled into a workflow.
import { useEffect, useId, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type {
  BrowserRuntimeStatus,
  EngineStatus,
  ExecutionTarget,
  PresentationExportResult,
  PresentationExportStatus,
} from "../lib/types";
import { overlayPresentationEnabled } from "../overlay/preferences";
import { ExecutionTargetForm } from "../ui/ExecutionTargetForm";
import { Button, Card, CardHead, Callout, Field, Pill } from "../ui/primitives";

function fmt(secs?: number | null) {
  if (secs == null) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

type CompilePhase = "idle" | "compiling" | "failed";

interface CompileResult {
  ok: boolean;
  workflow_id?: string;
  error?: string;
  recording_retained?: boolean;
}

interface RecordingResult {
  capture_id?: string;
  compile?: CompileResult;
}

interface CompileProgress {
  capture_id?: string;
  state: "compiling" | "compiled" | "failed";
  bundle_id?: string;
  error?: string;
  recording_retained?: boolean;
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
  const [phase, setPhase] = useState<CompilePhase>("idle");
  const [compileError, setCompileError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState<ExecutionTarget>({ backend: "web" });
  const [task, setTask] = useState("");
  const [runtime, setRuntime] = useState<BrowserRuntimeStatus | null>(null);
  const [presentationStatus, setPresentationStatus] =
    useState<PresentationExportStatus | null>(null);
  const [presentationResult, setPresentationResult] =
    useState<PresentationExportResult | null>(null);
  const [presentationError, setPresentationError] = useState<string | null>(null);
  const [exportingPresentation, setExportingPresentation] = useState(false);
  const fieldPrefix = useId();
  const targetRef = useRef(target);
  const onCompiledRef = useRef(onCompiled);
  const openedWorkflowRef = useRef<string | null>(null);
  targetRef.current = target;
  onCompiledRef.current = onCompiled;

  function openCompiledWorkflow(workflowId: string) {
    if (openedWorkflowRef.current === workflowId) return;
    openedWorkflowRef.current = workflowId;
    onCompiledRef.current(workflowId, targetRef.current);
  }

  async function refresh() {
    const s = await engineTry<EngineStatus>(CMD.GET_STATUS, {}, status);
    setStatus(s);
  }

  useEffect(() => {
    void refresh();
    const unsubs = [
      onEngineEvent(EVT.STATUS_UPDATE, (s: EngineStatus) => setStatus(s)),
      onEngineEvent(EVT.RECORDING_STOPPED, (d: { capture_id?: string }) => {
        if (d?.capture_id) {
          setLastCapture(d.capture_id);
          setCompileError(null);
          setPhase("compiling");
        }
      }),
      onEngineEvent(EVT.COMPILE_PROGRESS, (next: CompileProgress) => {
        if (next.capture_id) setLastCapture(next.capture_id);
        if (next.state === "compiling") {
          setCompileError(null);
          setPhase("compiling");
        } else if (next.state === "failed") {
          setCompileError(
            next.error ||
              "OpenAdapt could not compile this recording. The raw recording was retained.",
          );
          setPhase("failed");
        } else if (next.bundle_id) {
          openCompiledWorkflow(next.bundle_id);
        }
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

  useEffect(() => {
    setPresentationResult(null);
    setPresentationError(null);
    if (!lastCapture || !overlayPresentationEnabled()) {
      setPresentationStatus(null);
      return;
    }
    void engineTry<PresentationExportStatus>(
      CMD.GET_PRESENTATION_EXPORT_STATUS,
      { capture_id: lastCapture },
      { ready: false, reason: "The local engine could not inspect this recording." },
    ).then(setPresentationStatus);
  }, [lastCapture]);

  async function start() {
    setBusy(true);
    setRuntime(null);
    setLastCapture(null);
    setCompileError(null);
    setPhase("idle");
    openedWorkflowRef.current = null;
    try {
      const result = await engineInvoke<RecordingResult>(CMD.START_RECORDING, {
        target,
        ...(task.trim() ? { purpose: task.trim() } : {}),
      });
      applyCompileResult(result);
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    setBusy(true);
    try {
      const r = await engineInvoke<RecordingResult>(
        CMD.STOP_RECORDING,
        {},
      );
      if (r?.capture_id) setLastCapture(r.capture_id);
      applyCompileResult(r);
    } finally {
      setBusy(false);
    }
  }

  function applyCompileResult(result: RecordingResult) {
    const compiled = result.compile;
    if (!compiled) return;
    if (compiled.ok && compiled.workflow_id) {
      openCompiledWorkflow(compiled.workflow_id);
      return;
    }
    setCompileError(
      compiled.error ||
        "OpenAdapt could not compile this recording. The raw recording was retained.",
    );
    setPhase("failed");
  }

  async function retryCompile() {
    if (!lastCapture) return;
    setPhase("compiling");
    setCompileError(null);
    try {
      const r = await engineInvoke<CompileResult>(
        CMD.COMPILE_RECORDING,
        { capture_id: lastCapture },
      );
      if (r.ok && r.workflow_id) {
        openCompiledWorkflow(r.workflow_id);
      } else {
        setCompileError(
          r.error ||
            "OpenAdapt could not compile this recording. The raw recording was retained.",
        );
        setPhase("failed");
      }
    } catch {
      setCompileError(
        "OpenAdapt could not start compilation. The raw recording is still available.",
      );
      setPhase("failed");
    }
  }

  async function exportPresentation() {
    if (!lastCapture || !presentationStatus?.ready) return;
    setExportingPresentation(true);
    setPresentationError(null);
    try {
      const result = await engineInvoke<PresentationExportResult>(
        CMD.EXPORT_PRESENTATION_VIDEO,
        { capture_id: lastCapture },
      );
      setPresentationResult(result);
    } catch (error) {
      setPresentationError(String(error));
    } finally {
      setExportingPresentation(false);
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
            eyebrow="Build"
            title={
              phase === "compiling"
                ? "Building your workflow"
                : phase === "failed"
                  ? "Compilation needs attention"
                  : "Preparing your workflow"
            }
            sub={`capture ${lastCapture}`}
          />
          <div role="status" aria-live="polite">
            {phase === "compiling" && (
              <Callout tone="info" title="Compilation is in progress">
                OpenAdapt is converting the completed demonstration into a
                deterministic workflow. The raw recording stays unchanged.
              </Callout>
            )}
            {phase === "failed" && (
              <Callout tone="warn" title="The recording is safe">
                {compileError}
              </Callout>
            )}
          </div>
          <Callout tone="info" title="PHI stays local until you push">
            OpenAdapt scrubs the recording (fail-closed) before any upload. On
            the BYOC lane it is never uploaded — compile, replay, and teach all
            run here.
          </Callout>
          <div className="row" style={{ marginTop: "var(--space-4)" }}>
            <Button
              variant="primary"
              disabled={phase === "compiling"}
              onClick={retryCompile}
            >
              {phase === "compiling"
                ? "Compiling…"
                : phase === "failed"
                  ? "Retry compilation"
                  : "Compile to workflow"}
            </Button>
            <Button
              variant="ghost"
              onClick={() =>
                engineInvoke(CMD.SCRUB_CAPTURE, { capture_id: lastCapture })
              }
            >
              Scrub &amp; inspect PHI
            </Button>
            {presentationStatus?.ready && (
              <Button
                variant="ghost"
                disabled={exportingPresentation}
                onClick={exportPresentation}
              >
                {exportingPresentation
                  ? "Exporting presentation…"
                  : "Export presentation video"}
              </Button>
            )}
          </div>
          {presentationStatus && !presentationStatus.ready && (
            <p className="page-sub" style={{ marginTop: "var(--space-3)" }}>
              Presentation export will appear when this recording retains an
              exact OpenAdapt status timeline bound to its raw video.
            </p>
          )}
          {presentationResult && (
            <Callout tone="info" title="Presentation video exported">
              A separate MP4 was created at {presentationResult.path}. The raw
              recording and its SHA-256 were unchanged.
            </Callout>
          )}
          {presentationError && (
            <Callout tone="warn" title="Presentation export stopped safely">
              {presentationError}
            </Callout>
          )}
        </Card>
      )}
    </div>
  );
}
