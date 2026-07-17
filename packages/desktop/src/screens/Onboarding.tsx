// First-run onboarding (spec §5): empty-state hero "Record your first workflow",
// OS permission gate (screen recording + accessibility), and the honest
// "get past the OS warning" copy. Mirrors the cloud seamless flow.
import { useEffect, useState } from "react";
import { CMD, engineTry, openExternal } from "../lib/engine";
import type { PermissionStatus } from "../lib/types";
import { Button, Card, CardHead, Callout, Pill } from "../ui/primitives";
import { OsWarning } from "../ui/OsWarning";

const MAC =
  typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

// Deep links to the exact System Settings panes (macOS).
const SCREEN_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
const AX_PANE =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";

export function Onboarding({ onStart }: { onStart: () => void }) {
  const [perms, setPerms] = useState<PermissionStatus>({
    screen_recording: false,
    accessibility: false,
  });
  const [checked, setChecked] = useState(false);

  async function refresh() {
    const p = await engineTry<PermissionStatus>(
      CMD.CHECK_PERMISSIONS,
      {},
      { screen_recording: false, accessibility: false },
    );
    setPerms(p);
    setChecked(true);
  }

  useEffect(() => {
    void refresh();
  }, []);

  const ready = perms.screen_recording && perms.accessibility;

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
        {!ready && checked && (
          <p className="hint">Grant the permissions below to begin.</p>
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
            </div>
          </Card>
        )}

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
