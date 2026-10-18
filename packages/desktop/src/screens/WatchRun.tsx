// Watch-it-run — trigger a replay and watch the compile/replay rail + live log.
// Consumes replay_progress / log_line events; falls back to get_run_report.
import { useEffect, useId, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type {
  BrowserRuntimeStatus,
  ExecutionResponse,
  ExecutionTarget,
  QualificationProject,
  QualificationResponse,
  ReplayProgress,
  RunReport,
  RunPersistenceRetryResponse,
  RunStep,
} from "../lib/types";
import {
  Button,
  Card,
  CardHead,
  Callout,
  Field,
  Pill,
} from "../ui/primitives";
import { ExecutionTargetForm } from "../ui/ExecutionTargetForm";
import { ReplayMonitor } from "../ui/ReplayMonitor";
import { plainResultForReport, resultCopy } from "../lib/outcomes";

type ExecuteMode = "replay" | "run";
type RunIssue = {
  title: string;
  message: string;
  preActionRefusal: boolean;
  nothingWritten?: boolean;
};

// The plain result comes from the transaction outcome (see lib/outcomes.ts).
// Only a result that proves no effect may say nothing was written; every
// other stop tells the person to check the record before anything runs again.
function issueForReport(report: RunReport): RunIssue | null {
  const plain = plainResultForReport(report);
  if (plain.key === "done_checked") return null;
  const copy = resultCopy(plain);
  return {
    title: copy.label,
    // "Check the record" already says what to do; its Needs-you next step
    // doesn't apply on this screen.
    message:
      plain.key === "check_record"
        ? copy.sentence
        : [copy.sentence, copy.next].filter(Boolean).join(" "),
    preActionRefusal: false,
    nothingWritten: plain.nothingWritten,
  };
}

const CONTRACT_LABELS = {
  authorization: "Allowed to act",
  identity: "Right record",
  postcondition: "Screen checks",
  effect: "Read-back",
} as const;

function reviewRisk(
  project: QualificationProject,
  actionId: string,
): string {
  const classification =
    project.controls.actions[actionId]?.classification;
  if (!classification || classification.classification === "unknown") {
    return "not reviewed";
  }
  const label = classification.classification.replaceAll("_", " ");
  return classification.operator_confirmed
    ? label
    : `${label}, not reviewed`;
}

function riskTone(risk: string): "neutral" | "warn" | "crit" {
  if (risk.startsWith("irreversible")) return "crit";
  if (
    risk.startsWith("consequential") ||
    risk.startsWith("state changing") ||
    risk === "not reviewed"
  ) {
    return "warn";
  }
  return "neutral";
}

function firstReplayActionIsSafe(
  project: QualificationProject,
  actionId: string,
): boolean {
  const classification =
    project.controls.actions[actionId]?.classification;
  return Boolean(
    classification?.operator_confirmed &&
      classification.classification === "read_only",
  );
}

function reviewAuthority(
  project: QualificationProject | null,
  workflowId: string,
): string | null {
  const revision = project?.project?.revision;
  const digest = project?.graph.bundle.provenance.content_digest;
  if (
    project?.workflow_id !== workflowId ||
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision < 1 ||
    typeof digest !== "string" ||
    !/^[a-f0-9]{64}$/.test(digest)
  ) {
    return null;
  }
  return `${workflowId}:${revision}:${digest}`;
}

export function WatchRun({
  workflowId,
  initialTarget,
  firstWorkflow = false,
  firstRunComplete = false,
  firstRunLocked = false,
  onPersistencePendingChange,
  onRunningChange,
  onFirstWorkflowStateChange,
  onReconcile,
  onQualify,
  onTeach,
}: {
  workflowId: string;
  initialTarget?: ExecutionTarget;
  firstWorkflow?: boolean;
  firstRunComplete?: boolean;
  firstRunLocked?: boolean;
  onPersistencePendingChange?: (pending: boolean) => void;
  onRunningChange?: (running: boolean) => void;
  onFirstWorkflowStateChange?: () => void;
  onReconcile?: (id: string) => void;
  onQualify: (
    id: string,
    afterSavedResult?: boolean,
    target?: ExecutionTarget,
  ) => void;
  onTeach: (id: string) => void;
}) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [review, setReview] = useState<QualificationProject | null>(null);
  const [reviewLoaded, setReviewLoaded] = useState(false);
  const [reviewedAuthority, setReviewedAuthority] = useState<string | null>(null);
  const [completedFirstRun, setCompletedFirstRun] = useState(firstRunComplete);
  const [running, setRunning] = useState(false);
  const [commandInFlight, setCommandInFlight] = useState(false);
  const [runtime, setRuntime] = useState<BrowserRuntimeStatus | null>(null);
  const [runIssue, setRunIssue] = useState<RunIssue | null>(null);
  const [persistenceIssue, setPersistenceIssue] = useState("");
  const [retryingPersistence, setRetryingPersistence] = useState(false);
  const [target, setTarget] = useState<ExecutionTarget>(
    initialTarget ?? { backend: "web" },
  );
  const [deploymentConfig, setDeploymentConfig] = useState("");
  const stepsRef = useRef<RunStep[]>([]);
  const reportGenerationRef = useRef(0);
  const reviewGenerationRef = useRef(0);
  const fieldPrefix = useId();
  const executionBusy = running || commandInFlight;

  async function load(generation: number) {
    const next = await engineTry<RunReport | null>(
      CMD.GET_RUN_REPORT,
      { workflow_id: workflowId },
      null,
    );
    if (generation !== reportGenerationRef.current) return;
    if (next) {
      setReport(next);
      stepsRef.current = next.steps ?? [];
      setRunIssue(issueForReport(next));
    }
  }

  async function loadReview() {
    const generation = ++reviewGenerationRef.current;
    setReview(null);
    setReviewLoaded(false);
    setReviewedAuthority(null);
    const next = await engineTry<QualificationResponse | null>(
      CMD.GET_QUALIFICATION,
      { workflow_id: workflowId },
      null,
    );
    if (generation !== reviewGenerationRef.current) return;
    if (next && next.ok && "graph" in next) {
      setReview(next);
    }
    setReviewLoaded(true);
  }

  useEffect(() => {
    setCompletedFirstRun(firstRunComplete);
    const generation = ++reportGenerationRef.current;
    void load(generation);
    void loadReview();
    const unsubs = [
      onEngineEvent(EVT.LOG_LINE, (step: RunStep | { line: string }) => {
        if (!("index" in step)) return;
        const next = [
          ...stepsRef.current.filter((item) => item.index !== step.index),
          step,
        ];
        next.sort((a, b) => a.index - b.index);
        stepsRef.current = next;
        setReport((current) =>
          current ? { ...current, steps: next } : current,
        );
      }),
      onEngineEvent(EVT.REPLAY_PROGRESS, (progress: ReplayProgress) => {
        if (progress.workflow_id !== workflowId) return;
        setRunning(progress.state === "running");
      }),
      onEngineEvent(EVT.BROWSER_RUNTIME, (status: BrowserRuntimeStatus) => {
        if (status.workflow_id === workflowId) setRuntime(status);
      }),
    ];
    return () => {
      reportGenerationRef.current += 1;
      reviewGenerationRef.current += 1;
      unsubs.forEach((promise) => promise.then((u) => u()).catch(() => {}));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, firstRunComplete]);

  async function execute(mode: ExecuteMode) {
    reportGenerationRef.current += 1;
    setCommandInFlight(true);
    setRunning(true);
    setRunIssue(null);
    setPersistenceIssue("");
    stepsRef.current = [];
    setReport((current) =>
      current
        ? {
            ...current,
            ok: false,
            outcome: "unknown",
            error: undefined,
            steps: [],
            halt: null,
            metrics: null,
            outcome_details: null,
          }
        : current,
    );
    try {
      const currentReviewAuthority = reviewAuthority(review, workflowId);
      const firstAdmission =
        firstWorkflow && mode === "replay" && currentReviewAuthority
          ? {
              workflow_id: workflowId,
              project_revision: review?.project?.revision,
              bundle_content_digest:
                review?.graph.bundle.provenance.content_digest,
            }
          : null;
      const response = await engineInvoke<ExecutionResponse>(
        mode === "run"
          ? CMD.RUN_WORKFLOW
          : firstWorkflow
            ? CMD.REPLAY_FIRST_WORKFLOW
            : CMD.REPLAY_WORKFLOW,
        {
          workflow_id: workflowId,
          target,
          ...(firstAdmission ? { admission: firstAdmission } : {}),
          ...(deploymentConfig.trim()
            ? { deployment_config: deploymentConfig.trim() }
            : {}),
        },
      );
      if (response.outcome === "refused") {
        setRunIssue({
          title: "OpenAdapt didn't start this run",
          message: response.error,
          preActionRefusal: response.pre_action_refusal,
        });
      } else {
        setReport(response);
        stepsRef.current = response.steps ?? [];
        setRunIssue(issueForReport(response));
        if (firstWorkflow) setCompletedFirstRun(true);
        if (
          firstWorkflow &&
          response.persistence?.state === "persisted"
        ) {
          onFirstWorkflowStateChange?.();
        }
      }
    } catch (error) {
      setRunIssue({
        title: "Check the record",
        message:
          error instanceof Error
            ? error.message
            : "The engine connection ended unexpectedly.",
        preActionRefusal: false,
      });
    } finally {
      setCommandInFlight(false);
      setRunning(false);
    }
  }

  async function retryPersistence() {
    if (!report) return;
    setRetryingPersistence(true);
    setPersistenceIssue("");
    try {
      const response = await engineInvoke<RunPersistenceRetryResponse>(
        CMD.RETRY_RUN_PERSISTENCE,
        { workflow_id: workflowId, run_id: report.run_id },
      );
      if (!response.ok || !response.report) {
        setPersistenceIssue(
          response.error || "Desktop could not save this run in local history.",
        );
        return;
      }
      setReport(response.report);
      stepsRef.current = response.report.steps ?? [];
      if (firstWorkflow) onFirstWorkflowStateChange?.();
    } catch {
      setPersistenceIssue("Desktop could not save this run in local history.");
    } finally {
      setRetryingPersistence(false);
    }
  }

  const total = report?.total_steps ?? 0;
  const steps = report?.steps ?? [];
  const currentReview = review?.workflow_id === workflowId ? review : null;
  const currentReviewAuthority = reviewAuthority(currentReview, workflowId);
  const reviewActions =
    currentReview?.graph.nodes.filter((node) => node.kind === "action") || [];
  const reviewParameters = currentReview?.controls.parameters || [];
  const reviewReady = Boolean(currentReview && reviewActions.length > 0);
  const blockedFirstReplayActions = currentReview
    ? reviewActions.filter(
        (action) => !firstReplayActionIsSafe(currentReview, action.id),
      )
    : [];
  const firstReplaySafe =
    reviewReady &&
    currentReviewAuthority !== null &&
    blockedFirstReplayActions.length === 0;
  const reviewed =
    currentReviewAuthority !== null &&
    reviewedAuthority === currentReviewAuthority;
  const firstRunFinished =
    completedFirstRun &&
    report &&
    report.persistence?.state === "persisted" &&
    (report.outcome === "VERIFIED" ||
      report.outcome === "COMPLETED_UNVERIFIED" ||
      report.outcome === "success");
  const firstRunPersistencePending = Boolean(
    firstWorkflow &&
      completedFirstRun &&
      (!report || report.persistence?.state !== "persisted"),
  );

  useEffect(() => {
    onPersistencePendingChange?.(firstRunPersistencePending);
    return () => onPersistencePendingChange?.(false);
  }, [firstRunPersistencePending, onPersistencePendingChange]);

  useEffect(() => {
    onRunningChange?.(executionBusy);
    return () => onRunningChange?.(false);
  }, [executionBusy, onRunningChange]);

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          {firstWorkflow && <p className="eyebrow">First supervised run</p>}
          <h1>
            {firstWorkflow
              ? "Review the workflow, then watch it run"
              : report?.workflow_name ?? "Watch it run"}
          </h1>
        </div>
        {!firstWorkflow && (
          <div className="row">
            <Button disabled={executionBusy} onClick={() => execute("replay")}>
              {executionBusy ? "Running…" : "Replay the recording"}
            </Button>
            <Button
              variant="primary"
              disabled={executionBusy}
              onClick={() => execute("run")}
            >
              Run it
            </Button>
          </div>
        )}
      </div>

      {firstWorkflow && (
        <Card>
          <CardHead
            title="Check the steps"
            sub="Read the steps and inputs before OpenAdapt touches the app."
          />
          {reviewReady && currentReview ? (
            <>
              <div className="metrics">
                <div className="metric">
                  <span className="label">Actions</span>
                  <span className="metric-value tnum">
                    {reviewActions.length}
                  </span>
                </div>
                <div className="metric">
                  <span className="label">Detected inputs</span>
                  <span className="metric-value tnum">
                    {reviewParameters.length}
                  </span>
                </div>
              </div>

              <h3 style={{ marginTop: "var(--space-5)" }}>Compiled steps</h3>
              <ol className="first-workflow-steps">
                {reviewActions.map((action) => {
                  const risk = reviewRisk(currentReview, action.id);
                  return (
                    <li key={action.id}>
                      <span>{action.title}</span>
                      <Pill tone={riskTone(risk)}>{risk}</Pill>
                    </li>
                  );
                })}
              </ol>

              <h3>Detected inputs</h3>
              {reviewParameters.length ? (
                <ul className="first-workflow-inputs">
                  {reviewParameters.map((parameter) => (
                    <li key={parameter.name}>
                      <span className="mono">{parameter.name}</span>
                      <span className="page-sub">
                        {parameter.secret ? "protected input" : parameter.type}
                        {parameter.required ? ", required" : ", optional"}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="page-sub">
                  OpenAdapt didn't detect a reusable input in this recording.
                </p>
              )}

              {firstReplaySafe ? (
                <label className="check-row first-workflow-confirmation">
                  <input
                    type="checkbox"
                    checked={reviewed}
                    onChange={(event) =>
                      setReviewedAuthority(
                        event.target.checked ? currentReviewAuthority : null,
                      )
                    }
                  />
                  <span>
                    I reviewed these steps. The workflow uses test data, and I will
                    keep the target app in view.
                  </span>
                </label>
              ) : (
                <Callout
                  tone="warn"
                  title="Review these actions before the first run"
                >
                  One or more actions still need risk review or can cause a
                  consequential change. Open the qualification review before
                  OpenAdapt acts.
                  <div style={{ marginTop: "var(--space-3)" }}>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => onQualify(workflowId, false, target)}
                    >
                      Review before running
                    </Button>
                  </div>
                </Callout>
              )}
            </>
          ) : !reviewLoaded ? (
            <Callout tone="info" title="Loading the compiled review">
              OpenAdapt is reading the retained workflow graph and input schema.
            </Callout>
          ) : (
            <Callout tone="warn" title="Open the compiled review">
              Desktop couldn't read the workflow steps. Retry the local review
              before the first run.
              <div style={{ marginTop: "var(--space-3)" }}>
                <Button size="sm" onClick={() => void loadReview()}>
                  Retry review
                </Button>
              </div>
            </Callout>
          )}
        </Card>
      )}

      <Card>
        <CardHead
          title={
            firstWorkflow
              ? "Recorded application target"
              : "Where should this workflow run?"
          }
          sub={
            firstWorkflow
              ? "The supervised run uses the same application target as the recording."
              : "Pick the app it runs in. The steps and checks are the same everywhere."
          }
        />
        <ExecutionTargetForm
          target={target}
          onChange={(next) => {
            setTarget(next);
            setRuntime(null);
            setReviewedAuthority(null);
          }}
          idPrefix={fieldPrefix}
          disabled={executionBusy || firstWorkflow}
        />

        {!firstWorkflow && <details className="advanced-target">
          <summary>Settings for the person who sets this up</summary>
          <div className="advanced-target-body">
            <Field
              label="Deployment config"
              hint="Optional local YAML/JSON file for policy, effect verification, credentials, and other advanced Flow settings."
              htmlFor={`${fieldPrefix}-deployment-config`}
              hintId={`${fieldPrefix}-deployment-config-hint`}
            >
              <input
                id={`${fieldPrefix}-deployment-config`}
                className="input mono"
                value={deploymentConfig}
                disabled={executionBusy}
                aria-describedby={`${fieldPrefix}-deployment-config-hint ${fieldPrefix}-deployment-config-note`}
                onChange={(event) => setDeploymentConfig(event.target.value)}
                placeholder="/path/to/deployment.yaml"
                spellCheck={false}
              />
            </Field>
            <p
              className="page-sub"
              id={`${fieldPrefix}-deployment-config-note`}
            >
              Desktop merges target details into a short-lived private config.
              Secret and selector values stay off the process command line and
              out of Desktop logs.
            </p>
          </div>
        </details>}
        {firstWorkflow && !completedFirstRun && !firstRunLocked && firstReplaySafe && (
          <div style={{ marginTop: "var(--space-5)" }}>
            <p className="page-sub">
              Keep the target app visible and watch each step.
            </p>
            <Button
              variant="primary"
              disabled={executionBusy || !reviewed}
              onClick={() => execute("replay")}
            >
              {executionBusy ? "Running…" : "Run once while I watch"}
            </Button>
          </div>
        )}
        {firstWorkflow && firstRunLocked && (
          <Callout tone="warn" title="Check the target before another attempt">
            The prior run might have reached the application. Check the target
            and restore the test state if it changed. Desktop won't dispatch
            this workflow again until you confirm that check.
            <div style={{ marginTop: "var(--space-3)" }}>
              <Button
                size="sm"
                onClick={() => {
                  setReviewedAuthority(null);
                  onReconcile?.(workflowId);
                }}
              >
                I checked the target. Review again
              </Button>
            </div>
          </Callout>
        )}
      </Card>

      <Card>
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
        {runIssue && (
          <Callout
            tone="warn"
            title={runIssue.title}
          >
            {runIssue.message}{" "}
            {runIssue.preActionRefusal
              ? "Correct the configuration before trying again. OpenAdapt didn't start the run."
              : runIssue.nothingWritten === false
                ? "Look at the record and the run evidence below before you run it again."
                : runIssue.nothingWritten
                  ? ""
                  : "The run may have saved something. Look at the record before you run it again."}
          </Callout>
        )}
        {report?.persistence && report.persistence.state !== "persisted" && (
          <Callout
            tone={report.persistence.state === "failed" ? "crit" : "warn"}
            title="Local run history needs attention"
          >
            {report.persistence.message}
            {persistenceIssue && <div>{persistenceIssue}</div>}
            {report.persistence.retryable && (
              <div style={{ marginTop: "var(--space-3)" }}>
                <Button
                  size="sm"
                  disabled={retryingPersistence}
                  onClick={() => void retryPersistence()}
                >
                  {retryingPersistence ? "Saving…" : "Retry local history save"}
                </Button>
              </div>
            )}
          </Callout>
        )}
        <ReplayMonitor
          workflowName={report?.workflow_name ?? workflowId}
          steps={steps}
          total={total || steps.length}
        />
      </Card>

      {report?.halt && (
        <Card>
          <CardHead
            title="Where it stopped"
            sub={resultCopy(plainResultForReport(report)).label}
          />
          <Callout tone="warn" title={report.halt.step_intent}>
            {report.halt.reason}
            {report.halt.resolver_rung
              ? ` (resolver: ${report.halt.resolver_rung})`
              : ""}
          </Callout>
          <div className="row" style={{ marginTop: "var(--space-4)" }}>
            <Button
              variant="primary"
              disabled={
                firstWorkflow && report.persistence?.state !== "persisted"
              }
              onClick={() => onTeach(workflowId)}
            >
              Show OpenAdapt the right way
            </Button>
          </div>
        </Card>
      )}

      {report?.outcome_details && (
        <Card>
          <CardHead
            title="What was checked"
            sub="What this run had to prove, what passed, and anything it reached outside this computer."
          />
          <div className="row">
            <Pill tone={report.outcome === "VERIFIED" ? "ok" : "warn"}>
              {resultCopy(plainResultForReport(report)).label}
            </Pill>
            <Pill tone={report.outcome_details.execution_completed ? "ok" : "warn"}>
              {report.outcome_details.execution_completed
                ? "Ran to the end"
                : "Stopped before the end"}
            </Pill>
            <Pill
              tone={
                report.outcome_details.production_eligible ? "ok" : "neutral"
              }
            >
              {report.outcome_details.production_eligible
                ? "Ready for real items"
                : "Not ready for real items yet"}
            </Pill>
            {report.outcome_details.profile && (
              <span className="technical-text" title="Profile">
                {report.outcome_details.profile}
              </span>
            )}
          </div>
          <div className="metrics" style={{ marginTop: "var(--space-4)" }}>
            {(
              Object.keys(CONTRACT_LABELS) as Array<
                keyof typeof CONTRACT_LABELS
              >
            ).map((contract) => (
              <div className="metric" key={contract}>
                <span className="label">{CONTRACT_LABELS[contract]}</span>
                <span className="metric-value tnum">
                  {report.outcome_details?.passed_contracts[contract]}/
                  {report.outcome_details?.required_contracts[contract]}
                </span>
              </div>
            ))}
            <div className="metric">
              <span className="label">Model calls</span>
              <span className="metric-value tnum">
                {report.outcome_details.model_calls}
              </span>
            </div>
            <div className="metric">
              <span className="label">External network</span>
              <span className="metric-value">
                {report.outcome_details.external_network_calls}
              </span>
            </div>
            <div className="metric">
              <span className="label">Compensating actions</span>
              <span className="metric-value tnum">
                {report.outcome_details.compensation_actions}
              </span>
            </div>
          </div>
          {report.outcome_details.evidence_classes.length > 0 && (
            <div className="row" style={{ marginTop: "var(--space-4)" }}>
              {report.outcome_details.evidence_classes.map((evidence) => (
                <Pill key={evidence} tone="neutral">
                  {evidence.replaceAll("_", " ")}
                </Pill>
              ))}
            </div>
          )}
        </Card>
      )}

      {report?.metrics && (
        <Card>
          <CardHead title="Run details" />
          <div className="metrics">
            <div className="metric">
              <span className="label">Steps</span>
              <span className="metric-value tnum">{report.total_steps}</span>
            </div>
            {report.metrics.duration_s != null && (
              <div className="metric">
                <span className="label">Duration</span>
                <span className="metric-value tnum">
                  {report.metrics.duration_s.toFixed(1)}s
                </span>
              </div>
            )}
            {report.metrics.cost_usd != null && (
              <div className="metric">
                <span className="label">Cost</span>
                <span className="metric-value tnum">
                  ${report.metrics.cost_usd.toFixed(3)}
                </span>
              </div>
            )}
          </div>
        </Card>
      )}

      {firstRunFinished && (
        <Card>
          <CardHead
            title="Next: check it for everyday use"
            sub="Your supervised run is saved. Now define which record may change and what result proves success."
          />
          <p className="page-sub">
            The qualification review also applies the policy for each
            consequential action.
          </p>
          <Button
            variant="primary"
            onClick={() => onQualify(workflowId, true, target)}
          >
            Review identity, effects, and policy
          </Button>
        </Card>
      )}
    </div>
  );
}
