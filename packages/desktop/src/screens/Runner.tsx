// Hosted runner status, operator control, and recent terminal outcomes.
import { useEffect, useState } from "react";
import { CMD, EVT, engineInvoke, engineTry, onEngineEvent } from "../lib/engine";
import { plainResultForTransaction, resultCopy } from "../lib/outcomes";
import type { RunnerRun, RunnerStatus } from "../lib/types";
import {
  Button,
  Callout,
  Card,
  CardHead,
  EmptyState,
  Pill,
  StatusDot,
} from "../ui/primitives";

const EMPTY: RunnerStatus = { enabled: false, state: "disabled", last_runs: [] };

function stateTone(state: RunnerStatus["state"]): "ok" | "warn" | "off" | "run" {
  switch (state) {
    case "polling":
      return "ok";
    case "running":
      return "run";
    case "reauth_required":
    case "incompatible":
    case "error":
      return "warn";
    default:
      return "off";
  }
}

function stateLabel(state: RunnerStatus["state"]): string {
  switch (state) {
    case "polling":
      return "Online. Waiting for work.";
    case "running":
      return "Running an admitted workflow";
    case "stopping":
      return "Stopping after the current run";
    case "reauth_required":
      return "Sign-in required";
    case "incompatible":
      return "Update required";
    case "error":
      return "Error";
    case "offline":
      return "Offline. Reconnecting.";
    default:
      return "Disabled";
  }
}

// Cloud runs carry Flow's transaction outcome. The plain label derives from
// it, so only a proven no-effect stop can read as "Stopped before saving".
function outcomeView(outcome?: string | null): {
  label: string;
  tone: "ok" | "warn" | "neutral" | "crit";
} {
  if (!outcome) return { label: "In progress", tone: "neutral" };
  const copy = resultCopy(plainResultForTransaction(outcome));
  return {
    label: copy.label,
    tone: copy.tone === "crit" ? "crit" : copy.tone,
  };
}

export function Runner() {
  const [status, setStatus] = useState<RunnerStatus>(EMPTY);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    engineTry<RunnerStatus>(CMD.RUNNER_STATUS, {}, EMPTY).then(setStatus);
    const unsub = onEngineEvent<RunnerStatus>(EVT.RUNNER_STATE, setStatus);
    return () => {
      unsub.then((u) => u()).catch(() => {});
    };
  }, []);

  async function toggle() {
    setBusy(true);
    try {
      const next = await engineInvoke<RunnerStatus>(
        status.enabled ? CMD.RUNNER_DISABLE : CMD.RUNNER_ENABLE,
        {},
      );
      setStatus(next);
    } catch {
      /* engine offline: leave state as-is */
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <h1>Let Cloud send runs to this computer</h1>
        </div>
      </div>

      <Callout tone="info" title="How this works">
        When you turn this on, OpenAdapt Cloud can send approved workflows here.
        This computer reaches out to Cloud, and Cloud can't reach in. Each
        workflow's approval is checked here before it runs, and only results with
        no PHI go back.
      </Callout>

      <Card>
        <CardHead
          title="This computer"
          sub="Connects out over HTTPS. It doesn't open an inbound port."
        />
        <div className="row">
          <StatusDot tone={stateTone(status.state)} />
          <span>{stateLabel(status.state)}</span>
          {status.runner_id && (
            <span className="page-sub mono">{status.runner_id}</span>
          )}
          <span className="spacer" />
          <Button onClick={toggle} disabled={busy || status.state === "stopping"}>
            {status.enabled ? "Stop accepting runs" : "Accept runs from Cloud"}
          </Button>
        </div>
        {status.state === "reauth_required" && (
          <Callout tone="warn" title="Sign-in required">
            OpenAdapt Cloud didn't accept this computer's sign-in. Sign in
            again from Settings, then accept runs again.
          </Callout>
        )}
        {status.state === "incompatible" && (
          <Callout tone="warn" title="Update required">
            Install a current, approved Desktop release. Runs resume after
            Desktop restarts.
          </Callout>
        )}
        {status.last_error &&
          status.state !== "reauth_required" &&
          status.state !== "incompatible" && (
            <p className="page-sub">{status.last_error}</p>
          )}
      </Card>

      <Card>
        <CardHead
          title="Last runs"
          sub="Runs from Cloud that this computer handled."
        />
        {status.last_runs.length === 0 ? (
          <EmptyState
            title="No dispatched runs yet"
            body="Runs launched from app.openadapt.ai will appear here."
          />
        ) : (
          <div className="list">
            {status.last_runs.map((run: RunnerRun) => (
              <div className="row" key={run.run_id}>
                <span className="mono">{run.run_id}</span>
                <span title={run.outcome ?? run.phase ?? "pending"}>
                  <Pill tone={outcomeView(run.outcome).tone}>
                    {run.outcome ? outcomeView(run.outcome).label : (run.phase ?? "In progress")}
                  </Pill>
                </span>
                {run.updated_at && (
                  <span className="page-sub">
                    {new Date(run.updated_at).toLocaleString()}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
