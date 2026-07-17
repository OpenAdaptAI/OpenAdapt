// Runner — EXPERIMENTAL outbound dispatch lane (spec §2, P0 desktop lane).
// Shows the enabled toggle, connection state, and recent dispatched runs.
// The loop is outbound-only HTTPS long-poll; enabling it is the operator's
// standing consent for cloud-dispatched governed runs on this machine.
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
    case "error":
      return "warn";
    default:
      return "off";
  }
}

function stateLabel(state: RunnerStatus["state"]): string {
  switch (state) {
    case "polling":
      return "online — polling for dispatches";
    case "running":
      return "running a dispatched workflow";
    case "reauth_required":
      return "sign-in required";
    case "error":
      return "error";
    case "offline":
      return "offline — reconnecting";
    default:
      return "disabled";
  }
}

function outcomeTone(outcome?: string | null): "ok" | "warn" | "neutral" {
  if (outcome === "confirmed") return "ok";
  if (outcome === "halted-needs-attention" || outcome === "uncertain") return "warn";
  if (outcome === "refused" || outcome === "failed") return "warn";
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

      <Callout tone="warn" title="Experimental">
        The runner lane lets app.openadapt.ai dispatch governed runs to this
        machine over outbound HTTPS only. Every dispatch is revalidated locally
        against the sealed bundle digest before anything executes, and only
        PHI-free evidence (digests, counts, step ids) leaves this machine.
      </Callout>

      <Card>
        <CardHead
          eyebrow="Connection"
          title="This machine as a runner"
          sub="Outbound long-poll to the control plane; no inbound ports."
        />
        <div className="row">
          <StatusDot tone={stateTone(status.state)} />
          <span>{stateLabel(status.state)}</span>
          {status.runner_id && (
            <span className="page-sub mono">{status.runner_id}</span>
          )}
          <span className="spacer" />
          <Button onClick={toggle} disabled={busy}>
            {status.enabled ? "Disable runner" : "Enable runner"}
          </Button>
        </div>
        {status.state === "reauth_required" && (
          <Callout tone="warn" title="Sign-in required">
            The control plane rejected this runner's credentials. Sign in again
            from Settings, then re-enable the runner.
          </Callout>
        )}
        {status.last_error && status.state !== "reauth_required" && (
          <p className="page-sub">{status.last_error}</p>
        )}
      </Card>

      <Card>
        <CardHead
          eyebrow="History"
          title="Last runs"
          sub="Dispatched runs executed (or refused) on this machine."
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
