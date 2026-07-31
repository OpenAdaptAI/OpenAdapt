// Product shell + routed screens, gated by first-run/auth.
// The top shell uses the same OpenAdapt | Product pattern as Cloud. It also
// carries the local engine, recording, sync, and attention state.
import { useEffect, useState } from "react";
import {
  CMD,
  EVT,
  engineTry,
  onEngineEvent,
  sidecarRunning,
} from "./lib/engine";
import type {
  AuthStatus,
  ExecutionTarget,
  NeedsAttention,
  SyncState,
  Workflow,
} from "./lib/types";
import { StatusDot, Pill } from "./ui/primitives";
import { Login } from "./screens/Login";
import { Onboarding } from "./screens/Onboarding";
import { WorkflowLibrary } from "./screens/WorkflowLibrary";
import { RecordReview } from "./screens/RecordReview";
import { WatchRun } from "./screens/WatchRun";
import { Teach } from "./screens/Teach";
import { Runner } from "./screens/Runner";
import { Settings } from "./screens/Settings";
import { DecisionPortal } from "./screens/DecisionPortal";
import { deliverAttentionNotification } from "./lib/attentionNotification";
import { Qualification } from "./screens/Qualification";
import {
  clearLocalSession,
  localSessionEnabled,
  rememberLocalSession,
} from "./localSession";

type Route =
  | { name: "library" }
  | { name: "record" }
  | { name: "qualify"; id: string }
  | { name: "watch"; id: string; target?: ExecutionTarget }
  | { name: "teach"; id: string }
  | { name: "runner" }
  | { name: "portal" }
  | { name: "settings" };

type PairingState = {
  status: "connecting" | "connected" | "error";
  error?: string;
};

const NAV: { route: Route["name"]; label: string; glyph: string }[] = [
  { route: "library", label: "Workflows", glyph: "▤" },
  { route: "record", label: "Record", glyph: "●" },
  { route: "runner", label: "Runner", glyph: "⇅" },
  { route: "portal", label: "Phone", glyph: "▯" },
  { route: "settings", label: "Settings", glyph: "⚙" },
];

function DesktopBrand({ onOpen }: { onOpen?: () => void }) {
  const content = (
    <>
      <span className="brand-open">Open</span>
      <span className="brand-adapt">Adapt</span>
      <span className="brand-product">Desktop</span>
    </>
  );
  if (!onOpen) {
    return <div className="product-brand product-brand-static">{content}</div>;
  }
  return (
    <button
      aria-label="Open OpenAdapt Desktop workflows"
      className="product-brand"
      onClick={onOpen}
      type="button"
    >
      {content}
    </button>
  );
}

function DesktopEntryShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="desktop-entry-shell">
      <header className="desktop-shell">
        <div className="desktop-shell-inner">
          <DesktopBrand />
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [checkedAuth, setCheckedAuth] = useState(false);
  const [localSession, setLocalSession] = useState(localSessionEnabled);
  const [onboarded, setOnboarded] = useState(false);
  const [route, setRoute] = useState<Route>({ name: "library" });

  const [engineUp, setEngineUp] = useState(false);
  const [recording, setRecording] = useState(false);
  const [sync, setSync] = useState<SyncState>({ state: "synced", queued: 0 });
  const [breaks, setBreaks] = useState(0);
  const [pairing, setPairing] = useState<PairingState | null>(null);

  // Bootstrap: auth status, sidecar liveness, and the status channels.
  useEffect(() => {
    (async () => {
      setEngineUp(await sidecarRunning());
      const a = await engineTry<AuthStatus>(
        CMD.GET_AUTH_STATUS,
        {},
        { authenticated: false },
      );
      setAuth(a);
      setCheckedAuth(true);
      const wf = await engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []);
      setOnboarded(wf.length > 0);
      const na = await engineTry<NeedsAttention>(
        CMD.GET_NEEDS_ATTENTION,
        {},
        { count: 0, open_halts: 0, failed_runs: 0 },
      );
      setBreaks(na.count);
      const ss = await engineTry<SyncState>(CMD.GET_SYNC_STATE, {}, sync);
      setSync(ss);
    })();

    const unsubs = [
      onEngineEvent(EVT.SIDECAR_STATE, (d: { running: boolean }) =>
        setEngineUp(!!d?.running),
      ),
      onEngineEvent(EVT.STATUS_UPDATE, (s: { recording?: boolean }) =>
        setRecording(!!s?.recording),
      ),
      onEngineEvent(EVT.RECORDING_STARTED, () => setRecording(true)),
      onEngineEvent(EVT.RECORDING_STOPPED, () => setRecording(false)),
      onEngineEvent(EVT.SYNC_STATE, (s: SyncState) => setSync(s)),
      onEngineEvent(EVT.BREAK_COUNT, (d: { count: number }) =>
        setBreaks(d?.count ?? 0),
      ),
      // Generic only: the payload is re-derived from the count before any
      // operating-system notification is shown (see attentionNotification.ts).
      onEngineEvent(EVT.ATTENTION_NOTIFICATION, (payload: unknown) => {
        void deliverAttentionNotification(payload);
      }),
      onEngineEvent(EVT.PAIRING_STATE, (state: PairingState) => {
        setPairing(state);
        if (state.status === "connected") {
          void engineTry<AuthStatus>(
            CMD.GET_AUTH_STATUS,
            {},
            { authenticated: false },
          ).then((next) => {
            setAuth(next);
            setCheckedAuth(true);
          });
        }
      }),
    ];
    return () => unsubs.forEach((p) => p.then((u) => u()).catch(() => {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pairingNotice = pairing ? (
    <div
      className={`pairing-notice ${pairing.status}`}
      role={pairing.status === "error" ? "alert" : "status"}
    >
      <span>
        {pairing.status === "connecting" &&
          "Connecting this computer to your OpenAdapt Cloud workspace…"}
        {pairing.status === "connected" &&
          "Connected securely. The access credential is saved in this computer’s protected password storage."}
        {pairing.status === "error" &&
          (pairing.error || "The secure connection could not be completed.")}
      </span>
      {pairing.status !== "connecting" && (
        <button
          type="button"
          aria-label="Dismiss connection notice"
          onClick={() => setPairing(null)}
        >
          ×
        </button>
      )}
    </div>
  ) : null;

  if (!checkedAuth) {
    return (
      <>
        {pairingNotice}
        <DesktopEntryShell>
          <div className="center-stage"><span className="page-sub">Loading…</span></div>
        </DesktopEntryShell>
      </>
    );
  }

  if (!auth?.authenticated && !localSession) {
    return (
      <>
        {pairingNotice}
        <DesktopEntryShell>
          <Login
            onLocal={() => {
              rememberLocalSession();
              setLocalSession(true);
            }}
            onAuthed={(s) => {
              setAuth(s);
            }}
          />
        </DesktopEntryShell>
      </>
    );
  }

  if (!onboarded) {
    return (
      <>
        {pairingNotice}
        <DesktopEntryShell>
          <Onboarding
            onStart={() => {
              setOnboarded(true);
              setRoute({ name: "record" });
            }}
          />
        </DesktopEntryShell>
      </>
    );
  }

  const syncTone =
    sync.state === "offline"
      ? "off"
      : sync.state === "pushing"
        ? "run"
        : sync.state === "paused"
          ? "warn"
          : "ok";

  return (
    <>
      {pairingNotice}
      <div className="app">
        <header className="desktop-shell">
          <div className="desktop-shell-inner">
            <DesktopBrand onOpen={() => setRoute({ name: "library" })} />

            <nav className="desktop-nav" aria-label="Desktop navigation">
              {NAV.map((n) => (
                <button
                  aria-current={route.name === n.route ? "page" : undefined}
                  className={`nav-item ${route.name === n.route ? "active" : ""}`}
                  key={n.route}
                  onClick={() => setRoute({ name: n.route } as Route)}
                  type="button"
                >
                  <span className="glyph" aria-hidden="true">{n.glyph}</span>
                  <span>{n.label}</span>
                  {n.route === "library" && breaks > 0 ? (
                    <Pill tone="warn">{breaks}</Pill>
                  ) : null}
                </button>
              ))}
            </nav>

            <div className="desktop-shell-spacer" />

            <div className="desktop-status" aria-label="Desktop status">
              <span title={engineUp ? "Local engine ready" : "Local engine offline"}>
                <StatusDot tone={engineUp ? "ok" : "off"} />
                <strong>{engineUp ? "Engine ready" : "Engine offline"}</strong>
              </span>
              {recording ? (
                <span className="recording" title="A demonstration is being recorded">
                  <StatusDot tone="warn" />
                  <strong>Recording</strong>
                </span>
              ) : null}
              <span title={`Cloud sync: ${sync.state}`}>
                <StatusDot tone={syncTone} />
                <strong>{sync.state}</strong>
                {sync.queued ? <small>{sync.queued}</small> : null}
              </span>
            </div>
          </div>
        </header>

        <main>
        {route.name === "library" && (
          <WorkflowLibrary
            onQualify={(id) => setRoute({ name: "qualify", id })}
            onWatch={(id) => setRoute({ name: "watch", id })}
            onTeach={(id) => setRoute({ name: "teach", id })}
            onRecord={() => setRoute({ name: "record" })}
          />
        )}
        {route.name === "qualify" && (
          <Qualification
            workflowId={route.id}
            onBack={() => setRoute({ name: "library" })}
            onOpenWorkflow={(id) => setRoute({ name: "qualify", id })}
          />
        )}
        {route.name === "record" && (
          <RecordReview
            onCompiled={(id, target) =>
              setRoute({ name: "watch", id, target })
            }
          />
        )}
        {route.name === "watch" && (
          <WatchRun
            workflowId={route.id}
            initialTarget={route.target}
            onTeach={(id) => setRoute({ name: "teach", id })}
          />
        )}
        {route.name === "teach" && (
          <Teach
            workflowId={route.id}
            onDone={() => setRoute({ name: "library" })}
          />
        )}
        {route.name === "runner" && <Runner />}
        {route.name === "portal" && <DecisionPortal />}
        {route.name === "settings" && (
          <Settings
            auth={auth ?? { authenticated: false }}
            onConnectCloud={() => {
              clearLocalSession();
              setLocalSession(false);
            }}
            onSignedOut={() => setAuth({ authenticated: false })}
          />
        )}
        </main>
      </div>
    </>
  );
}
