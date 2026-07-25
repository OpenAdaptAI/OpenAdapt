// Watch-it-run — trigger a replay and watch the compile/replay rail + live log.
// Consumes replay_progress / log_line events; falls back to get_run_report.
import { useEffect, useId, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type {
  BrowserRuntimeStatus,
  ExecutionResponse,
  ExecutionTarget,
  ReplayProgress,
  RunReport,
  RunStep,
} from "../lib/types";
import {
  Button,
  Card,
  CardHead,
  Callout,
  Field,
} from "../ui/primitives";
import { ExecutionTargetForm } from "../ui/ExecutionTargetForm";
import { ReplayMonitor } from "../ui/ReplayMonitor";

type ExecuteMode = "replay" | "run";
type RunIssue = {
  title: string;
  message: string;
  preActionRefusal: boolean;
};

function issueForReport(report: RunReport): RunIssue | null {
  if (report.outcome === "COMPLETED_UNVERIFIED") {
    return {
      title: "Execution completed without sufficient verification",
      message:
        "The workflow finished, but its required evidence did not prove the intended business effect.",
      preActionRefusal: false,
    };
  }
  if (report.outcome === "FAILED") {
    return {
      title: "Execution failed",
      message:
        "A runtime or infrastructure failure prevented a verified outcome.",
      preActionRefusal: false,
    };
  }
  if (report.outcome === "ROLLED_BACK") {
    return {
      title: "Execution was rolled back",
      message:
        "The configured compensating action completed, so the requested effect was not reported as verified.",
      preActionRefusal: false,
    };
  }
  if (report.outcome === "unknown") {
    return {
      title: "Execution outcome needs verification",
      message:
        report.error || "Desktop could not classify the execution outcome.",
      preActionRefusal: false,
    };
  }
  return null;
}

export function WatchRun({
  workflowId,
  initialTarget,
  onTeach,
}: {
  workflowId: string;
  initialTarget?: ExecutionTarget;
  onTeach: (id: string) => void;
}) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [running, setRunning] = useState(false);
  const [runtime, setRuntime] = useState<BrowserRuntimeStatus | null>(null);
  const [runIssue, setRunIssue] = useState<RunIssue | null>(null);
  const [target, setTarget] = useState<ExecutionTarget>(
    initialTarget ?? { backend: "web" },
  );
  const [deploymentConfig, setDeploymentConfig] = useState("");
  const stepsRef = useRef<RunStep[]>([]);
  const fieldPrefix = useId();

  async function load() {
    const next = await engineTry<RunReport | null>(
      CMD.GET_RUN_REPORT,
      { workflow_id: workflowId },
      null,
    );
    if (next) {
      setReport(next);
      stepsRef.current = next.steps ?? [];
      setRunIssue(issueForReport(next));
    }
  }

  useEffect(() => {
    void load();
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
    return () => unsubs.forEach((promise) => promise.then((u) => u()).catch(() => {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  async function execute(mode: ExecuteMode) {
    setRunning(true);
    setRunIssue(null);
    stepsRef.current = [];
    setReport((current) => (current ? { ...current, steps: [] } : current));
    try {
      const response = await engineInvoke<ExecutionResponse>(
        mode === "run" ? CMD.RUN_WORKFLOW : CMD.REPLAY_WORKFLOW,
        {
          workflow_id: workflowId,
          target,
          ...(deploymentConfig.trim()
            ? { deployment_config: deploymentConfig.trim() }
            : {}),
        },
      );
      if (response.outcome === "refused") {
        setRunIssue({
          title: "Execution was refused before action",
          message: response.error,
          preActionRefusal: response.pre_action_refusal,
        });
      } else {
        setReport(response);
        stepsRef.current = response.steps ?? [];
        setRunIssue(issueForReport(response));
      }
    } catch (error) {
      setRunIssue({
        title: "Engine connection ended unexpectedly",
        message:
          error instanceof Error
            ? error.message
            : "The engine connection ended unexpectedly.",
        preActionRefusal: false,
      });
    } finally {
      setRunning(false);
    }
  }

  const total = report?.total_steps ?? 0;
  const steps = report?.steps ?? [];

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Execute</p>
          <h1>{report?.workflow_name ?? "Watch it run"}</h1>
        </div>
        <div className="row">
          <Button disabled={running} onClick={() => execute("replay")}>
            {running ? "Running…" : "Replay"}
          </Button>
          <Button
            variant="primary"
            disabled={running}
            onClick={() => execute("run")}
          >
            Run with safety gates
          </Button>
        </div>
      </div>

      <Card>
        <CardHead
          eyebrow="Target"
          title="Where should this workflow run?"
          sub="Choose the application surface. OpenAdapt uses the same compiled workflow and fail-closed verification on every target."
        />
        <ExecutionTargetForm
          target={target}
          onChange={(next) => {
            setTarget(next);
            setRuntime(null);
          }}
          idPrefix={fieldPrefix}
          disabled={running}
        />

        <details className="advanced-target">
          <summary>Advanced deployment details</summary>
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
                disabled={running}
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
        </details>
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
              ? "Correct the configuration before trying again; Flow was not invoked."
              : "Do not retry until you verify the target application or system of record and inspect the retained run evidence; the prior dispatch may have delivered an action."}
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
          <CardHead eyebrow="Halted" title="This run stopped safely" />
          <Callout tone="warn" title={report.halt.step_intent}>
            {report.halt.reason}
            {report.halt.resolver_rung
              ? ` (resolver: ${report.halt.resolver_rung})`
              : ""}
          </Callout>
          <div className="row" style={{ marginTop: "var(--space-4)" }}>
            <Button variant="primary" onClick={() => onTeach(workflowId)}>
              Teach the fix
            </Button>
          </div>
        </Card>
      )}

      {report?.metrics && (
        <Card>
          <CardHead eyebrow="Run report" title="Metrics" />
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
    </div>
  );
}
