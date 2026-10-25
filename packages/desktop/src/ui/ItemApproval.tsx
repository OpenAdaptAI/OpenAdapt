// Approve each save: the screen a person sees when a run stops before saving.
//
// Flow stops every Desktop run before each save (its `each_item` gate),
// takes a screenshot of the filled form, and waits. The engine tells the
// window when a run that may wait starts (`item_approval` event); while one
// runs, this polls for a waiting request and shows the item's own values
// next to the filled form. The person approves, or says why it shouldn't
// save. Flow checks the answer again, re-reads the form and checks the
// record before it saves.
//
// Desktop has no person-level sign-in, so every approval is recorded without
// a name; this computer's approver key signs it. On a Mac without that key
// yet, the screen offers its one-step setup instead of Approve. The screen
// never offers Approve without the filled form to compare.
import { useCallback, useEffect, useRef, useState } from "react";
import { DECLINED } from "../lib/approvalMode";
import { CMD, EVT, engineInvoke, engineTry, onEngineEvent } from "../lib/engine";
import { humanizeInput, sentenceCase } from "../lib/outcomes";
import type { ItemApprovalAnswer, ItemApprovalState } from "../lib/types";
import { ApproverKeySetup, useApproverKey } from "./ApproverKey";
import { Button } from "./primitives";
import { Icon } from "./simple";

const POLL_MS = 1500;

type DeclineReason = keyof typeof DECLINED;

function clock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Polls the engine while a run that may wait is going. */
export function useItemApproval(): {
  state: ItemApprovalState | null;
  refresh: () => Promise<void>;
} {
  const [state, setState] = useState<ItemApprovalState | null>(null);
  const watching = useRef(0);

  const refresh = useCallback(async () => {
    const next = await engineTry<ItemApprovalState | null>(CMD.GET_ITEM_APPROVAL, {}, null);
    setState(next && next.ok && next.waiting ? next : null);
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const start = () => {
      if (timer) return;
      void refresh();
      timer = setInterval(() => void refresh(), POLL_MS);
    };
    const unlisten = onEngineEvent<{ state: "watching" | "closed" }>(
      EVT.ITEM_APPROVAL,
      (payload) => {
        if (payload?.state === "watching") {
          watching.current += 1;
          start();
        } else if (payload?.state === "closed") {
          watching.current = Math.max(0, watching.current - 1);
          if (watching.current === 0) {
            stop();
            setState(null);
          }
        }
      },
    ).catch(() => null);
    return () => {
      stop();
      void unlisten.then((fn) => fn?.());
    };
  }, [refresh]);

  return { state, refresh };
}

export function ItemApprovalPanel({
  state,
  onAnswered,
}: {
  state: ItemApprovalState;
  onAnswered: () => void | Promise<void>;
}) {
  const request = state.request;
  const [busy, setBusy] = useState(false);
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState<DeclineReason | "">("");
  const [error, setError] = useState("");
  const [sent, setSent] = useState<"approved" | "declined" | null>(null);
  const { status: keyStatus, refresh: refreshKey } = useApproverKey();
  // The engine said the key is missing on a Mac that can set it up here.
  const [needsKey, setNeedsKey] = useState(false);
  const showSetup =
    needsKey || Boolean(keyStatus?.supported && keyStatus.set_up === false);

  useEffect(() => {
    setBusy(false);
    setDeclining(false);
    setReason("");
    setError("");
    setSent(null);
  }, [request?.request_id]);

  if (!request) return null;
  const until = clock(request.answer_by);
  const canApprove = Boolean(request.screenshot);
  const practice = state.kind === "practice";

  async function answer(decision: "approve" | "decline") {
    if (!request) return;
    setBusy(true);
    setError("");
    try {
      const response = await engineInvoke<ItemApprovalAnswer>(CMD.ANSWER_ITEM_APPROVAL, {
        workflow_id: state.workflow_id,
        decision,
        request_id: request.request_id,
        request_sha256: request.request_sha256,
        ...(decision === "decline" ? { reason } : {}),
      });
      if (!response.ok) {
        if (response.setup === "in_app") {
          setNeedsKey(true);
          return;
        }
        setError(response.error || "OpenAdapt couldn't send your answer. Nothing was saved.");
        return;
      }
      setSent(response.decision ?? (decision === "approve" ? "approved" : "declined"));
      await onAnswered();
    } catch (failure) {
      setError(`OpenAdapt couldn't send your answer. Nothing was saved. (${String(failure)})`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="approval-backdrop">
      <section
        className="approval-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="item-approval-title"
      >
        <header className="approval-head">
          <h2 id="item-approval-title">
            {practice ? "Approve the practice save?" : "Approve this save?"}
          </h2>
          <p className="simple-sub">
            OpenAdapt filled in the form and stopped before saving. Compare your
            values with the form, then approve.
            {until ? ` It waits until ${until}, then stops without saving.` : ""}
          </p>
        </header>
        <div className="approval-body">
          <div className="approval-values">
            <h3>{practice ? "Your test values" : "Your values"}</h3>
            {request.values.length ? (
              <dl>
                {request.values.map((item) => (
                  <div key={item.name}>
                    <dt>{sentenceCase(humanizeInput(item.name))}</dt>
                    <dd>{item.value === "" ? <em>Left empty</em> : item.value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="simple-sub">This workflow takes no values.</p>
            )}
            <p className="simple-sub approval-step">
              Next step: {request.step_intent || "the save"}
            </p>
          </div>
          <figure className="approval-form">
            <h3>The form</h3>
            {request.screenshot ? (
              <img src={request.screenshot} alt="The filled form, before saving" />
            ) : (
              <p className="notice warn" role="alert">
                <Icon name="attention" />
                <span>
                  OpenAdapt couldn't show the filled form, so you can't approve
                  this save. Choose Don't save.
                </span>
              </p>
            )}
          </figure>
        </div>
        {error && (
          <p className="notice warn" role="alert">
            <Icon name="attention" />
            <span>{error}</span>
          </p>
        )}
        {sent ? (
          <p className="notice ok" role="status">
            <Icon name="check" />
            <span>
              {sent === "approved"
                ? "Approved. OpenAdapt checks the form again, then saves."
                : "OpenAdapt won't save it."}
            </span>
          </p>
        ) : showSetup ? (
          <ApproverKeySetup
            onReady={async () => {
              setNeedsKey(false);
              await refreshKey();
            }}
          />
        ) : declining ? (
          <fieldset className="approval-decline">
            <legend>Why shouldn't it save?</legend>
            {(Object.keys(DECLINED) as DeclineReason[]).map((code) => (
              <label key={code} className="record-check">
                <input
                  type="radio"
                  name="decline-reason"
                  value={code}
                  checked={reason === code}
                  onChange={() => setReason(code)}
                />
                <span>{sentenceCase(DECLINED[code])}</span>
              </label>
            ))}
            <div className="check-actions">
              <Button
                variant="primary"
                disabled={busy || !reason}
                onClick={() => void answer("decline")}
              >
                {busy ? "Sending…" : "Don't save"}
              </Button>
              <Button disabled={busy} onClick={() => setDeclining(false)}>
                Back
              </Button>
            </div>
          </fieldset>
        ) : (
          <div className="check-actions">
            <Button
              variant="primary"
              disabled={busy || !canApprove}
              onClick={() => void answer("approve")}
            >
              {busy ? "Sending…" : "Approve and save"}
            </Button>
            <Button disabled={busy} onClick={() => setDeclining(true)}>
              Don't save
            </Button>
          </div>
        )}
        <p className="fine-print">
          OpenAdapt records this approval without your name, because this
          computer doesn't know who is signed in.
        </p>
      </section>
    </div>
  );
}

/** Shown on top of any screen whenever a run waits for a person. */
export function ItemApprovalDialog() {
  const { state, refresh } = useItemApproval();
  if (!state) return null;
  return <ItemApprovalPanel state={state} onAnswered={refresh} />;
}
