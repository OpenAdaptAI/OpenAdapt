// First-launch usage-stats question and the "update available" notice.
//
// Usage stats: nothing is sent until the person picks Share. The question
// appears only while no answer is saved and nothing in the environment
// (DO_NOT_TRACK, OPENADAPT_TELEMETRY_ENABLED) decides. Settings can change it.
//
// Updates: the engine checks the Desktop releases at most once a day with an
// anonymous request and reports through UPDATE_STATUS. Installing stays a
// person's step: the notice links to the release page.
import { useEffect, useRef, useState } from "react";
import { CMD, EVT, engineInvoke, engineTry, onEngineEvent, openExternal } from "../lib/engine";
import type { PrivacySettings, UpdateStatus } from "../lib/types";
import { Button, Callout } from "./primitives";

export const USAGE_STATS_TITLE = "Help improve OpenAdapt";
export const USAGE_STATS_BODY =
  "Share anonymous usage stats, like which features are used and whether they work. No screen content, keystrokes, file paths, or task text. You can change this in Settings.";

// `engineUp` re-reads once the local engine starts, so a question or notice
// isn't lost when the window opens before the engine does.
export function UsageStatsPrompt({ engineUp }: { engineUp: boolean }) {
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void engineTry<PrivacySettings | null>(CMD.GET_PRIVACY_SETTINGS, {}, null).then(
      (settings) => setAsk(settings?.ask_usage_stats === true),
    );
  }, [engineUp]);

  if (!ask) return null;

  async function answer(share: boolean) {
    setBusy(true);
    setError(null);
    try {
      const result = await engineInvoke<{ ok: boolean; error?: string }>(
        CMD.SET_USAGE_STATS,
        { share },
      );
      if (!result.ok) {
        setError(result.error || "Your choice wasn't saved.");
        return;
      }
      setAsk(false);
    } catch {
      setError("Your choice wasn't saved. Nothing is shared until it is.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shadow-prompts" aria-label="Usage stats">
      <Callout title={USAGE_STATS_TITLE}>
        <p>{USAGE_STATS_BODY}</p>
        <div className="row">
          <Button
            size="sm"
            variant="primary"
            disabled={busy}
            onClick={() => void answer(true)}
          >
            Share
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void answer(false)}>
            No thanks
          </Button>
        </div>
        {error && <p className="hint">{error}</p>}
      </Callout>
    </div>
  );
}

export function UpdateNotice({ engineUp }: { engineUp: boolean }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  // Bumped on every live UPDATE_STATUS event so a late initial GET cannot
  // clobber a newer available-release notice.
  const statusEpoch = useRef(0);

  useEffect(() => {
    // Outside the Tauri shell there is no event bus; the listener just stays off.
    const unlisten = onEngineEvent(EVT.UPDATE_STATUS, (next: UpdateStatus) => {
      statusEpoch.current += 1;
      setStatus(next);
    }).catch(() => null);
    return () => void unlisten.then((fn) => fn?.());
  }, []);

  useEffect(() => {
    const epochAtRequest = statusEpoch.current;
    void engineTry<UpdateStatus | null>(CMD.GET_UPDATE_STATUS, {}, null).then((first) => {
      if (!first) return;
      if (statusEpoch.current !== epochAtRequest) return;
      setStatus(first);
    });
  }, [engineUp]);

  if (!status?.enabled || !status.available || !status.latest_version) return null;
  if (dismissed === status.latest_version) return null;

  return (
    <div className="pairing-notice" role="status">
      <span>
        OpenAdapt {status.latest_version} is available. You have{" "}
        {status.current_version}.
      </span>
      <span className="allow-actions">
        <button type="button" onClick={() => void openExternal(status.url)}>
          Download
        </button>
      </span>
      <button
        type="button"
        aria-label="Dismiss update notice"
        onClick={() => setDismissed(status.latest_version)}
      >
        ×
      </button>
    </div>
  );
}
