// Shadow mode UI: the notice a person must acknowledge, the Settings card, the
// header indicator with its Pause control, and the prompt banner. Shadow mode
// is off by default, and OpenAdapt never sends its data off this computer. It
// offers drafts; it never runs anything.
import { useCallback, useEffect, useState } from "react";
import {
  CMD,
  EVT,
  engineInvoke,
  engineTry,
  onEngineEvent,
  setShadowTrayStatus,
} from "../lib/engine";
import {
  SHADOW_UNKNOWN,
  formatWhen,
  isPrompt,
  parseDomains,
  readShadowStatus,
  type ShadowDecision,
  type ShadowNotice,
  type ShadowPrompt,
  type ShadowStatus,
} from "../lib/shadowMode";
import { Button, Callout, Card, CardHead, Field, Pill, StatusDot } from "./primitives";

/** How often the status is re-read, in case an event was missed. */
const REFRESH_MS = 30_000;

/** Shadow mode's status, kept current from engine events. */
export function useShadowMode({ syncTray = false }: { syncTray?: boolean } = {}) {
  const [status, setStatus] = useState<ShadowStatus>(SHADOW_UNKNOWN);

  const refresh = useCallback(async () => {
    const raw = await engineTry(CMD.SHADOW_STATUS, {}, null);
    // A failed read keeps what we knew; it doesn't flip the indicator to off.
    if (raw) setStatus(readShadowStatus(raw));
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    // Outside the desktop shell there are no events; the status read above
    // still answers, so a failed subscription is not an error.
    const quiet = (p: Promise<() => void>) => p.catch(() => () => {});
    const unlisteners = [
      quiet(onEngineEvent(EVT.SHADOW_STATE, (payload) => setStatus(readShadowStatus(payload)))),
      quiet(
        onEngineEvent(EVT.SHADOW_PROMPT, (payload) => {
          if (!isPrompt(payload)) return;
          setStatus((s) => ({
            ...s,
            pending: [...s.pending.filter((p) => p.motif_id !== payload.motif_id), payload],
          }));
        }),
      ),
      // The engine restarted: what we knew may be stale.
      quiet(onEngineEvent(EVT.SIDECAR_STATE, () => void refresh())),
    ];
    return () => {
      clearInterval(timer);
      for (const u of unlisteners) void u.then((fn) => fn());
    };
  }, [refresh]);

  useEffect(() => {
    if (syncTray) void setShadowTrayStatus(status.known ? status.active : null, status.paused);
  }, [syncTray, status.known, status.active, status.paused]);

  /** Pause or resume. Returns the error text, or null. */
  const togglePause = useCallback(async (): Promise<string | null> => {
    try {
      const result = await engineInvoke(status.paused ? CMD.SHADOW_UNPAUSE : CMD.SHADOW_PAUSE);
      setStatus(readShadowStatus(result));
      return null;
    } catch (e) {
      return String(e);
    }
  }, [status.paused]);

  return { status, setStatus, refresh, togglePause };
}

/** The header indicator and its Pause control. Shows only while shadow mode is on. */
export function ShadowModeIndicator({
  status,
  onTogglePause,
}: {
  status: ShadowStatus;
  onTogglePause?: () => void;
}) {
  if (!status.active) return null;
  return (
    <span
      className="shadow-mode-on"
      role="status"
      title={
        status.paused
          ? "Shadow mode is paused. It isn't recording."
          : "Shadow mode is on. Pause it before you view sensitive material."
      }
    >
      <StatusDot tone={status.paused ? "off" : "run"} />
      <strong>{status.paused ? "Shadow mode paused" : "Shadow mode on"}</strong>
      {onTogglePause && (
        <Button size="sm" variant="ghost" onClick={onTogglePause}>
          {status.paused ? "Resume" : "Pause"}
        </Button>
      )}
    </span>
  );
}

/** One offer per repeated task: Review draft or Ignore. Generic text only. */
export function ShadowPromptBanner({
  status,
  onDecided,
}: {
  status: ShadowStatus;
  onDecided?: (decision: ShadowDecision) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!status.active || (status.pending.length === 0 && !note && !error)) return null;

  async function decide(prompt: ShadowPrompt, action: string) {
    setBusy(prompt.motif_id);
    setError(null);
    try {
      const result = await engineInvoke<ShadowDecision>(CMD.SHADOW_DECIDE, {
        motif_id: prompt.motif_id,
        action,
      });
      setNote(action === "review_draft" ? (result.next ?? "The draft is saved.") : null);
      onDecided?.(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="shadow-prompts" aria-label="Repeated tasks">
      {status.pending.map((prompt) => (
        <Callout key={prompt.motif_id} title={prompt.title}>
          <p>{prompt.body}</p>
          <div className="row">
            {prompt.actions.map((a) => (
              <Button
                key={a.id}
                size="sm"
                variant={a.id === "review_draft" ? "primary" : "ghost"}
                disabled={busy === prompt.motif_id}
                onClick={() => void decide(prompt, a.id)}
              >
                {a.label}
              </Button>
            ))}
          </div>
        </Callout>
      ))}
      {note && (
        <Callout title="Draft saved on this computer">
          <p>{note}</p>
        </Callout>
      )}
      {error && (
        <Callout tone="warn" title="Shadow mode couldn't save that answer">
          {error}
        </Callout>
      )}
    </div>
  );
}

/** The notice. Shadow mode turns on only from its acknowledge button. */
export function ShadowNoticeDialog({
  notice,
  busy,
  onAcknowledge,
  onDecline,
}: {
  notice: ShadowNotice;
  busy?: boolean;
  onAcknowledge: () => void;
  onDecline: () => void;
}) {
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="shadow-notice-title" className="shadow-notice">
      <h3 id="shadow-notice-title">{notice.title}</h3>
      <p>{notice.intro}</p>
      {notice.sections.map((section) => (
        <section key={section.heading}>
          <h4>{section.heading}</h4>
          <ul>
            {section.items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      ))}
      <p className="hint">Notice version {notice.version}</p>
      <div className="row">
        <Button variant="primary" disabled={busy} onClick={onAcknowledge}>
          {notice.acknowledge_label}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onDecline}>
          {notice.decline_label}
        </Button>
      </div>
    </div>
  );
}

/** The Settings card: status, the notice, work domains, and delete-and-off. */
export function ShadowModeCard() {
  const { status, setStatus, togglePause } = useShadowMode();
  const [notice, setNotice] = useState<ShadowNotice | null>(null);
  const [domains, setDomains] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const savedDomains = status.work_domains.join("\n");
  useEffect(() => {
    setDomains(savedDomains);
  }, [savedDomains]);

  async function run(action: () => Promise<unknown>, done?: string) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await action();
      if (result && typeof result === "object" && "available" in result) {
        setStatus(readShadowStatus(result));
      }
      if (result && typeof result === "object" && (result as { key_deleted?: unknown }).key_deleted === false) {
        setError(
          "The buffer is deleted, but the system keychain didn't delete its key. OpenAdapt tries again the next time it starts.",
        );
      }
      if (done) setMessage(done);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const openNotice = () =>
    run(async () => setNotice(await engineInvoke<ShadowNotice>(CMD.SHADOW_NOTICE)));

  const acknowledge = () =>
    run(async () => {
      if (!notice) return null;
      const result = await engineInvoke(CMD.SHADOW_ENABLE, {
        notice_sha256: notice.sha256,
        acknowledged: true,
      });
      setNotice(null);
      return result;
    });

  const turnOff = () =>
    run(
      () => engineInvoke(CMD.SHADOW_TURN_OFF),
      "Shadow mode is off. Its buffer, key, and drafts are erased.",
    );

  const purge = () =>
    run(
      () => engineInvoke(CMD.SHADOW_PURGE_AND_DISABLE),
      "Shadow mode is off. Everything it kept on this computer is erased.",
    );

  const pause = () =>
    run(async () => {
      const failed = await togglePause();
      if (failed) throw new Error(failed);
      return null;
    });

  const saveDomains = () =>
    run(
      () => engineInvoke(CMD.SHADOW_SET_WORK_DOMAINS, { domains: parseDomains(domains) }),
      "Work sites saved.",
    );

  return (
    <Card>
      <CardHead
        title="Shadow mode"
        sub="Off unless you turn it on. When on, OpenAdapt notices a task you repeat and offers to make a draft. It never runs anything. Its encrypted buffer on this computer is cleared at least every 24 hours, and when you turn it off or quit."
      />
      {!status.known ? (
        <p className="page-sub">Checking shadow mode…</p>
      ) : !status.available ? (
        <Callout title="Not available in this version">
          This version of the OpenAdapt engine doesn't include shadow mode.
        </Callout>
      ) : (
        <>
          <div className="row">
            <Pill tone={status.active && !status.paused ? "run" : "neutral"}>
              {status.active ? (status.paused ? "Paused" : "On") : "Off"}
            </Pill>
            {status.active && (
              <span className="page-sub">
                Turned on {formatWhen(status.acknowledged_at)}. Watches your recordings in
                Desktop{status.paused ? ", but is paused and records nothing" : ""}. The buffer is
                encrypted with a key{" "}
                {status.keys_in_keychain ? "in your system keychain" : "held in memory only"}. Both
                are erased by {formatWhen(status.epoch_ends_at)}, or sooner when you turn shadow
                mode off or quit OpenAdapt.
                {status.location ? ` Kept in ${status.location}.` : ""}
              </span>
            )}
          </div>

          {notice ? (
            <ShadowNoticeDialog
              notice={notice}
              busy={busy}
              onAcknowledge={() => void acknowledge()}
              onDecline={() => setNotice(null)}
            />
          ) : status.active ? (
            <>
              <p className="hint">Pause shadow mode before you view sensitive material.</p>
              <div className="row">
                <Button variant="primary" disabled={busy} onClick={() => void pause()}>
                  {status.paused ? "Resume" : "Pause"}
                </Button>
                <Button variant="ghost" disabled={busy} onClick={() => void turnOff()}>
                  Turn off
                </Button>
                <Button variant="danger" disabled={busy} onClick={() => void purge()}>
                  Delete everything and turn off
                </Button>
              </div>
            </>
          ) : (
            <div className="row">
              <Button variant="ghost" disabled={busy} onClick={() => void openNotice()}>
                Read the notice to turn on
              </Button>
            </div>
          )}

          <Field
            label="Work sites"
            htmlFor="shadow-work-domains"
            hint="Shadow mode keeps browser steps only on these sites, one per line, such as erp.example.com. On any other site it notes only that a browser was in use."
          >
            <textarea
              id="shadow-work-domains"
              rows={3}
              value={domains}
              onChange={(e) => setDomains(e.target.value)}
            />
          </Field>
          <div className="row">
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => void saveDomains()}>
              Save work sites
            </Button>
          </div>
        </>
      )}
      {message && <p className="page-sub">{message}</p>}
      {error && (
        <Callout tone="warn" title="Shadow mode didn't change">
          {error}
        </Callout>
      )}
    </Card>
  );
}
