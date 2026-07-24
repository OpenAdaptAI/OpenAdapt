// First-run onboarding (spec §5): empty-state hero "Record your first workflow",
// OS permission gate and the honest "get past the OS warning" copy. Mirrors
// the cloud seamless flow.
import { useEffect, useState } from "react";
import {
  CMD,
  engineTry,
  ffmpegRuntimeStatus,
  onFfmpegRuntimeStatus,
  openExternal,
  retryFfmpegRuntime,
} from "../lib/engine";
import type { FfmpegRuntimeStatus, PermissionStatus } from "../lib/types";
import { Button, Card, CardHead, Callout, Pill } from "../ui/primitives";
import { OsWarning } from "../ui/OsWarning";

const MAC =
  typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

// Deep links to the exact System Settings panes (macOS).
const SCREEN_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
const AX_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
const INPUT_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";

export function Onboarding({ onStart }: { onStart: () => void }) {
  const [perms, setPerms] = useState<PermissionStatus>({
    screen_recording: false,
    accessibility: false,
    input_monitoring: false,
  });
  const [checked, setChecked] = useState(false);
  const [requestingInput, setRequestingInput] = useState(false);
  const [videoRuntime, setVideoRuntime] = useState<FfmpegRuntimeStatus>({
    phase: "checking",
    source: "managed",
    runtime_version: "8.1.2-r1",
    target: "detecting",
  });

  async function refresh() {
    const p = await engineTry<PermissionStatus>(
      CMD.CHECK_PERMISSIONS,
      {},
      {
        screen_recording: false,
        accessibility: false,
        input_monitoring: false,
      },
    );
    setPerms(p);
    setChecked(true);
  }

  useEffect(() => {
    void refresh();
    void ffmpegRuntimeStatus().then(setVideoRuntime);
    let unlisten: (() => void) | undefined;
    void onFfmpegRuntimeStatus(setVideoRuntime).then((stop) => {
      unlisten = stop;
    });
    return () => unlisten?.();
  }, []);

  async function requestInputMonitoring() {
    setRequestingInput(true);
    try {
      await engineTry<PermissionStatus>(
        CMD.REQUEST_INPUT_MONITORING,
        {},
        perms,
      );
    } finally {
      await refresh();
      setRequestingInput(false);
    }
  }

  const permissionsReady =
    !MAC ||
    (perms.screen_recording &&
      perms.accessibility &&
      perms.input_monitoring);
  const videoReady = videoRuntime.phase === "ready";
  const ready = permissionsReady && videoReady;
  const videoBusy = ["checking", "downloading", "verifying"].includes(
    videoRuntime.phase,
  );

  return (
    <div className="content">
      <div className="hero">
        <div className="ladder" aria-hidden>
          ▁▂▄▆█
        </div>
        <p className="eyebrow">Welcome to OpenAdapt</p>
        <h1>Record your first workflow</h1>
        <p>
          Demonstrate a task once. OpenAdapt compiles it into a deterministic
          workflow you can watch run, correct, and promote — no scripting.
        </p>
        <div className="row" style={{ justifyContent: "center" }}>
          <Button variant="primary" disabled={!ready} onClick={onStart}>
            Start recording
          </Button>
          <Button variant="ghost" onClick={refresh}>
            Re-check permissions
          </Button>
        </div>
        {!permissionsReady && checked && (
          <p className="hint">Grant the permissions below to begin.</p>
        )}
        {permissionsReady && !videoReady && (
          <p className="hint">
            {videoBusy
              ? "Preparing the local video engine…"
              : "The local video engine needs attention below."}
          </p>
        )}
      </div>

      <div className="grid grid-2">
        {MAC && (
          <Card>
            <CardHead
              eyebrow="Required"
              title="Screen &amp; input permissions"
              sub="macOS blocks capture until these are granted."
            />
            <div className="stack">
              <div className="row">
                <Pill tone={perms.screen_recording ? "ok" : "warn"}>
                  {perms.screen_recording ? "granted" : "needed"}
                </Pill>
                <span className="spacer" />
                <span>Screen Recording</span>
                <Button size="sm" onClick={() => openExternal(SCREEN_PANE)}>
                  Open pane
                </Button>
              </div>
              <div className="row">
                <Pill tone={perms.accessibility ? "ok" : "warn"}>
                  {perms.accessibility ? "granted" : "needed"}
                </Pill>
                <span className="spacer" />
                <span>Accessibility</span>
                <Button size="sm" onClick={() => openExternal(AX_PANE)}>
                  Open pane
                </Button>
              </div>
              <div className="row">
                <Pill tone={perms.input_monitoring ? "ok" : "warn"}>
                  {perms.input_monitoring ? "granted" : "needed"}
                </Pill>
                <span className="spacer" />
                <span>Input Monitoring</span>
                {!perms.input_monitoring && (
                  <Button
                    size="sm"
                    disabled={requestingInput}
                    onClick={() => void requestInputMonitoring()}
                  >
                    {requestingInput ? "Requesting…" : "Request access"}
                  </Button>
                )}
                <Button size="sm" onClick={() => openExternal(INPUT_PANE)}>
                  Open pane
                </Button>
              </div>
            </div>
          </Card>
        )}

        <Card>
          <CardHead
            eyebrow="Automatic"
            title="Local video engine"
            sub="Downloaded once, verified, and cached on this machine."
          />
          <div className="stack">
            <div className="row">
              <Pill
                tone={
                  videoReady
                    ? "ok"
                    : videoRuntime.phase === "error" ||
                        videoRuntime.phase === "unavailable"
                      ? "warn"
                      : "neutral"
                }
              >
                {videoReady
                  ? "ready"
                  : videoBusy
                    ? "preparing"
                    : "needs attention"}
              </Pill>
              <span className="spacer" />
              <span>
                {videoBusy
                  ? "OpenAdapt is preparing video recording"
                  : videoReady
                    ? "Ready for video recording"
                    : "Video recording is not ready"}
              </span>
              {!videoBusy && !videoReady && (
                <Button
                  size="sm"
                  onClick={() =>
                    void retryFfmpegRuntime().then(setVideoRuntime)
                  }
                >
                  Retry
                </Button>
              )}
            </div>
            {videoRuntime.detail && !videoReady && (
              <Callout tone="info">{videoRuntime.detail}</Callout>
            )}
            <p className="hint">
              Video recording runs locally. Workflow data is not sent anywhere
              during setup.
            </p>
          </div>
        </Card>

        <Card>
          <CardHead eyebrow="Heads up" title="First launch" />
          <OsWarning />
          <div style={{ marginTop: "var(--space-4)" }}>
            <Callout tone="info">
              Nothing leaves this machine unless you push it. On the regulated
              (BYOC) lane, recordings and corrections stay entirely local.
            </Callout>
          </div>
        </Card>
      </div>
    </div>
  );
}
