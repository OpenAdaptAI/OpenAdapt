// Record & review — drive a recording, then review + compile it.
// Recording state comes from the engine via events + get_status; after stop the
// capture can be scrubbed (PHI gate) and compiled into a workflow.
import { useEffect, useId, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type {
  BrowserRuntimeStatus,
  CapabilityReport,
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
  state: "compiling" | "compiled" | "failed" | "review_failed";
  bundle_id?: string;
  error?: string;
  recording_retained?: boolean;
}

function firstTargetIssue(target: ExecutionTarget): string | null {
  switch (target.backend) {
    case "web":
      if (!target.url?.trim()) {
        return "Enter the page URL for the app you want to record.";
      }
      try {
        const url = new URL(target.url);
        return url.protocol === "http:" || url.protocol === "https:"
          ? null
          : "Enter a complete HTTP or HTTPS page URL.";
      } catch {
        return "Enter a complete HTTP or HTTPS page URL.";
      }
    case "windows":
      return target.agent_url?.trim()
        ? null
        : "Enter the Windows connection for the computer that owns the app.";
    case "macos":
      return target.macos_app?.trim()
        ? null
        : "Enter the name of the Mac app you want to record.";
    case "linux":
      if (!target.linux_app?.trim()) {
        return "Enter the Linux application name.";
      }
      return target.linux_window_title?.trim()
        ? null
        : "Enter the exact Linux window title.";
    case "rdp":
      return target.rdp_host?.trim() || target.rdp_window?.trim()
        ? null
        : "Choose the RDP host or the local client window.";
    case "citrix":
      return target.rdp_readiness_text?.trim()
        ? null
        : "Enter stable text from the app screen you plan to record.";
  }
}

function firstCapabilityIssue(
  target: ExecutionTarget,
  capabilities: CapabilityReport | null,
  loaded: boolean,
): string | null {
  if (!loaded) return "Checking whether this app is ready to record.";
  const capability = capabilities?.surfaces?.[target.backend];
  if (!capability) {
    return "Desktop couldn't check this app. Check the local engine, then try again.";
  }
  if (capability.state === "available") return null;
  return (
    [capability.detail, capability.remediation].filter(Boolean).join(" ") ||
    "This app isn't ready to record on this computer."
  );
}

export function RecordReview({
  onCompiled,
  firstWorkflow = false,
  initialTarget,
  initialTask = "",
}: {
  onCompiled: (id: string, target: ExecutionTarget) => void;
  firstWorkflow?: boolean;
  initialTarget?: ExecutionTarget;
  initialTask?: string;
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
  const [target, setTarget] = useState<ExecutionTarget>(
    initialTarget ?? { backend: "web" },
  );
  const [task, setTask] = useState(initialTask);
  const [capabilities, setCapabilities] = useState<CapabilityReport | null>(null);
  const [capabilitiesLoaded, setCapabilitiesLoaded] = useState(!firstWorkflow);
  const [recordingError, setRecordingError] = useState<string | null>(null);
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
  const capabilityGenerationRef = useRef(0);
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

  async function refreshCapabilities() {
    const generation = ++capabilityGenerationRef.current;
    setCapabilitiesLoaded(false);
    const next = await engineTry<CapabilityReport | null>(
      CMD.GET_CAPABILITIES,
      {},
      null,
    );
    if (generation !== capabilityGenerationRef.current) return;
    setCapabilities(next);
    setCapabilitiesLoaded(true);
  }

  useEffect(() => {
    if (!firstWorkflow) return;
    void refreshCapabilities();
    return () => {
      capabilityGenerationRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstWorkflow]);

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
        } else if (next.state === "failed" || next.state === "review_failed") {
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
      onEngineEvent(EVT.RECORDING_ERROR, (next: { error?: string }) => {
        setRecordingError(
          next.error || "Desktop couldn't start this recording. Check the setup and try again.",
        );
        setBusy(false);
        void refresh();
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
    setRecordingError(null);
    setRuntime(null);
    setLastCapture(null);
    setCompileError(null);
    setPhase("idle");
    openedWorkflowRef.current = null;
    try {
      const result = await engineInvoke<RecordingResult>(CMD.START_RECORDING, {
        target,
        ...(firstWorkflow ? { first_workflow: true } : {}),
        ...(task.trim() ? { purpose: task.trim() } : {}),
      });
      applyCompileResult(result);
    } catch (error) {
      setRecordingError(
        error instanceof Error
          ? error.message
          : typeof error === "string"
            ? error
            : "Desktop couldn't start this recording. Check the setup and try again.",
      );
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
    } catch (error) {
      setRecordingError(
        error instanceof Error
          ? error.message
          : typeof error === "string"
            ? error
            : "Desktop couldn't stop this recording cleanly. Try again before closing Desktop.",
      );
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
  const targetIssue = firstWorkflow ? firstTargetIssue(target) : null;
  const capabilityIssue = firstWorkflow
    ? firstCapabilityIssue(target, capabilities, capabilitiesLoaded)
    : null;
  const taskMissing = firstWorkflow && !task.trim();
  const firstWorkflowReady = !taskMissing && !targetIssue && !capabilityIssue;

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">
            {firstWorkflow ? "First workflow" : "Author"}
          </p>
          <h1>
            {firstWorkflow
              ? "Show OpenAdapt one small task"
              : "Record & review"}
          </h1>
        </div>
      </div>

      {firstWorkflow && (
        <Callout title="Pick a task you can verify yourself">
          A good first task takes less than a minute. Use test data and choose
          a result that is easy to see.
        </Callout>
      )}

      <Card>
        <CardHead
          eyebrow="Target"
          title={
            firstWorkflow
              ? "Choose the app and task"
              : "What are you demonstrating?"
          }
          sub={
            firstWorkflow
              ? "Open the app first. Select its surface here, then describe the result you want."
              : "The same target contract follows this recording into compile and execution."
          }
        />
        <ExecutionTargetForm
          target={target}
          onChange={setTarget}
          idPrefix={`${fieldPrefix}-record-target`}
          disabled={recording || busy}
          capabilities={firstWorkflow ? capabilities : undefined}
        />
        {firstWorkflow && !targetIssue && capabilityIssue && (
          <Button
            size="sm"
            disabled={!capabilitiesLoaded}
            onClick={() => void refreshCapabilities()}
          >
            {capabilitiesLoaded ? "Check again" : "Checking…"}
          </Button>
        )}
        <Field
          label={firstWorkflow ? "Task to record" : "Task description"}
          hint={
            firstWorkflow
              ? "Required for your first workflow. Keep it short and specific."
              : "Optional local description stored with this recording."
          }
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
            placeholder={
              firstWorkflow
                ? "Find a test record and check one value"
                : undefined
            }
          />
        </Field>
        {firstWorkflow && (taskMissing || targetIssue) && (
          <Callout tone="info" title="Finish the task setup">
            {taskMissing
              ? "Describe the task you want to record."
              : targetIssue}
          </Callout>
        )}
        {recordingError && (
          <div role="alert">
            <Callout tone="warn" title="Recording needs attention">
              {recordingError}
              {!recording && (
                <div style={{ marginTop: "var(--space-3)" }}>
                  <Button size="sm" onClick={() => void start()}>
                    Try recording again
                  </Button>
                </div>
              )}
            </Callout>
          </div>
        )}
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
            <Button
              variant="primary"
              disabled={busy || (firstWorkflow && !firstWorkflowReady)}
              onClick={start}
            >
              {firstWorkflow ? "Record this task" : "Start recording"}
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
