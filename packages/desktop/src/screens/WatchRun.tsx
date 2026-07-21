// Watch-it-run — trigger a replay and watch the compile/replay rail + live log.
// Consumes replay_progress / log_line events; falls back to get_run_report.
import { useEffect, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry, onEngineEvent, EVT } from "../lib/engine";
import type { BrowserRuntimeStatus, RunReport, RunStep } from "../lib/types";
import { Button, Card, CardHead, Callout } from "../ui/primitives";
import { ReplayMonitor } from "../ui/ReplayMonitor";

export function WatchRun({
  workflowId,
  onTeach,
}: {
  workflowId: string;
  onTeach: (id: string) => void;
}) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [running, setRunning] = useState(false);
  const [runtime, setRuntime] = useState<BrowserRuntimeStatus | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const stepsRef = useRef<RunStep[]>([]);

  async function load() {
    const r = await engineTry<RunReport | null>(
      CMD.GET_RUN_REPORT,
      { workflow_id: workflowId },
      null,
    );
    if (r) {
      setReport(r);
      stepsRef.current = r.steps ?? [];
    }
  }

  useEffect(() => {
    void load();
    const unsubs = [
      onEngineEvent(EVT.LOG_LINE, (step: RunStep) => {
        const next = [...stepsRef.current.filter((s) => s.index !== step.index), step];
        next.sort((a, b) => a.index - b.index);
        stepsRef.current = next;
        setReport((r) => (r ? { ...r, steps: next } : r));
      }),
      onEngineEvent(EVT.REPLAY_PROGRESS, (r: RunReport) => {
        setReport(r);
        stepsRef.current = r.steps ?? [];
        if (r.halt || (r.steps?.length ?? 0) >= r.total_steps) setRunning(false);
      }),
      onEngineEvent(EVT.BROWSER_RUNTIME, (status: BrowserRuntimeStatus) => {
        if (status.workflow_id === workflowId) setRuntime(status);
      }),
    ];
    return () => unsubs.forEach((p) => p.then((u) => u()).catch(() => {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  async function replay() {
    setRunning(true);
    setRunError(null);
    stepsRef.current = [];
    setReport((r) => (r ? { ...r, steps: [] } : r));
    try {
      const r = await engineInvoke<RunReport>(CMD.REPLAY_WORKFLOW, {
        workflow_id: workflowId,
      });
      if (r.ok === false) {
        throw new Error(r.error || "Replay could not start.");
      }
      if (r) {
        setReport(r);
        stepsRef.current = r.steps ?? [];
      }
    } catch (error) {
      setRunError(error instanceof Error ? error.message : String(error));
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
        <Button variant="primary" disabled={running} onClick={replay}>
          {running ? "Replaying…" : "Replay"}
        </Button>
      </div>

      <Card>
        {runtime && runtime.state !== "ready" && (
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
        {runError && (
          <Callout tone="warn" title="Replay did not start">
            {runError} Select Replay to retry; no workflow action was sent.
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
