// The approver key: what makes "Approve and save" count as a person's answer.
//
// On a Mac the engine keeps the key behind Touch ID (or the login password):
// setup is this one block and one prompt, and each approval prompts again.
// Elsewhere the key is set up once at a terminal, and this shows the steps.
// The key itself never reaches the window; only its status does.
import { useCallback, useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { ApproverKeyStatus } from "../lib/types";
import { Button, Card, CardHead, Callout } from "./primitives";
import { Icon } from "./simple";

export function useApproverKey(): {
  status: ApproverKeyStatus | null;
  refresh: () => Promise<void>;
} {
  const [status, setStatus] = useState<ApproverKeyStatus | null>(null);
  const refresh = useCallback(async () => {
    const next = await engineTry<ApproverKeyStatus | null>(CMD.GET_APPROVER_KEY, {}, null);
    setStatus(next && next.ok ? next : null);
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { status, refresh };
}

async function setUp(replace: boolean): Promise<ApproverKeyStatus> {
  try {
    return await engineInvoke<ApproverKeyStatus>(CMD.SET_UP_APPROVER_KEY, { replace });
  } catch (failure) {
    return {
      ok: false,
      error: `OpenAdapt couldn't set up the approver key. Nothing changed. (${String(failure)})`,
    };
  }
}

/** The one-screen setup shown in place of Approve until the key exists. */
export function ApproverKeySetup({ onReady }: { onReady: () => void | Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function start() {
    setBusy(true);
    setError("");
    const result = await setUp(false);
    setBusy(false);
    if (!result.ok || !result.set_up) {
      setError(result.error || "OpenAdapt couldn't set up the approver key. Nothing changed.");
      return;
    }
    await onReady();
  }

  return (
    <section className="approver-setup" aria-labelledby="approver-setup-title">
      <h3 id="approver-setup-title">Set up your approver key</h3>
      <p className="simple-sub">
        On this Mac, each approval asks for your Touch ID or login password, so
        only a person can approve a save. Set it up once.
      </p>
      {error && (
        <p className="notice warn" role="alert">
          <Icon name="attention" />
          <span>{error}</span>
        </p>
      )}
      <div className="check-actions">
        <Button variant="primary" disabled={busy} onClick={() => void start()}>
          {busy ? "Waiting for Touch ID…" : "Set up with Touch ID"}
        </Button>
      </div>
    </section>
  );
}

/** Settings: see, replace or remove this computer's approver key. */
export function ApproverKeyCard() {
  const { status, refresh } = useApproverKey();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);

  if (!status) return null;

  async function replace() {
    setBusy(true);
    setError("");
    const result = await setUp(status?.set_up ?? false);
    setBusy(false);
    if (!result.ok) setError(result.error || "The approver key didn't change.");
    await refresh();
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      const result = await engineInvoke<ApproverKeyStatus>(CMD.REMOVE_APPROVER_KEY, {});
      if (!result.ok) setError(result.error || "The approver key wasn't removed.");
    } catch (failure) {
      setError(`The approver key wasn't removed. (${String(failure)})`);
    }
    setBusy(false);
    setConfirmRemove(false);
    await refresh();
  }

  return (
    <Card>
      <CardHead
        title="Approver key"
        sub={
          !status.supported
            ? "Approvals from this computer need a key set up once at a terminal."
            : status.set_up
              ? "Set up. Each approval asks for your Touch ID or login password."
              : "Not set up. Approvals from this computer are refused until it is."
        }
      />
      {!status.supported && status.instructions && (
        <p className="simple-sub">{status.instructions}</p>
      )}
      {status.supported && (
        <div className="row">
          {status.set_up && status.fingerprint && (
            <span className="page-sub mono">{status.fingerprint}</span>
          )}
          <Button variant={status.set_up ? "ghost" : "primary"} disabled={busy} onClick={() => void replace()}>
            {status.set_up ? "Replace key" : "Set up with Touch ID"}
          </Button>
          {status.stored &&
            (confirmRemove ? (
              <>
                <Button variant="danger" disabled={busy} onClick={() => void remove()}>
                  Remove key
                </Button>
                <Button disabled={busy} onClick={() => setConfirmRemove(false)}>
                  Keep it
                </Button>
              </>
            ) : (
              <Button variant="danger" disabled={busy} onClick={() => setConfirmRemove(true)}>
                Remove key…
              </Button>
            ))}
        </div>
      )}
      {confirmRemove && (
        <Callout tone="warn" title="Remove the approver key?">
          Approvals from this computer are refused until you set up a new key.
        </Callout>
      )}
      {error && (
        <Callout tone="warn" title="The approver key didn't change">
          {error}
        </Callout>
      )}
    </Card>
  );
}
