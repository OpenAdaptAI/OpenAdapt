// Recording setup: operating-system permissions and the local video engine
// that desktop-app recordings need. It is no longer a first-run gate: the
// sample and browser tasks need neither, and the recording screen checks each
// app type just in time. It stays reachable under Advanced for IT.
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

const MAC =
  typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

// Deep links to the exact System Settings panes (macOS).
const SCREEN_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
const AX_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
const INPUT_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";

export function Onboarding({ onStart }: { onStart?: () => void }) {
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
        <h1>Recording setup</h1>
        <p>
          Recording a desktop app, a remote desktop, or Citrix needs these
          permissions and the local video engine. Browser tasks and the sample
          app don't need either.
        </p>
        <div className="row" style={{ justifyContent: "center" }}>
          {onStart && (
            <Button variant="primary" disabled={!ready} onClick={onStart}>
              Choose an app and task
            </Button>
          )}
          <Button variant="ghost" onClick={refresh}>
            Re-check permissions
          </Button>
        </div>
        {!permissionsReady && checked && (
          <p className="hint">Grant the permissions below to record desktop apps.</p>
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
              title="Screen and input permissions"
              sub="macOS blocks desktop-app recording until these are granted."
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
            title="Local video engine"
            sub="OpenAdapt downloads its video recorder once and checks it before use."
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

      </div>
    </div>
  );
}
