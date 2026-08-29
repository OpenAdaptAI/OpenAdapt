// Hosted runner status, operator control, and recent terminal outcomes.
import { useEffect, useState } from "react";
import { CMD, EVT, engineInvoke, engineTry, onEngineEvent } from "../lib/engine";
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

function outcomeTone(outcome?: string | null): "ok" | "warn" | "neutral" {
  if (outcome === "VERIFIED") return "ok";
  if (
    outcome === "HALTED_BEFORE_EFFECT" ||
    outcome === "RECONCILIATION_REQUIRED"
  )
    return "warn";
  if (outcome === "FAILED_PLATFORM" || outcome === "REJECTED_POLICY") return "warn";
  return "neutral";
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
          <p className="eyebrow">Execute</p>
          <h1>Runner</h1>
        </div>
      </div>

      <Callout tone="info" title="Hosted execution">
        OpenAdapt Cloud sends admitted workflows to this computer over outbound
        HTTPS. Flow checks the exact product and workflow admissions here before
        it runs. Only approved, PHI-free evidence returns to Cloud.
      </Callout>

      <Card>
        <CardHead
          eyebrow="Connection"
          title="This machine as a runner"
          sub="Connects through outbound HTTPS. It doesn't open an inbound port."
        />
        <div className="row">
          <StatusDot tone={stateTone(status.state)} />
          <span>{stateLabel(status.state)}</span>
          {status.runner_id && (
            <span className="page-sub mono">{status.runner_id}</span>
          )}
          <span className="spacer" />
          <Button onClick={toggle} disabled={busy || status.state === "stopping"}>
            {status.enabled ? "Disable runner" : "Enable runner"}
          </Button>
        </div>
        {status.state === "reauth_required" && (
          <Callout tone="warn" title="Sign-in required">
            The control plane rejected this runner's credentials. Sign in again
            from Settings, then re-enable the runner.
          </Callout>
        )}
        {status.state === "incompatible" && (
          <Callout tone="warn" title="Update required">
            Install an admitted Desktop release that includes the current Flow
            hosted runner. The runner will resume after Desktop restarts.
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
          eyebrow="History"
          title="Last runs"
          sub="Hosted runs handled by this computer."
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
                <Pill tone={outcomeTone(run.outcome)}>
                  {run.outcome ?? run.phase ?? "pending"}
                </Pill>
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
