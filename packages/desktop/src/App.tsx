// App shell: left-rail nav + routed screens, gated by first-run/auth.
// Rail carries the two orthogonal status channels (recording, sync) plus the
// needs-attention break count, mirrored from the engine over events (spec §3d).
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

type Route =
  | { name: "library" }
  | { name: "record" }
  | { name: "watch"; id: string }
  | { name: "teach"; id: string }
  | { name: "runner" }
  | { name: "settings" };

const NAV: { route: Route["name"]; label: string; glyph: string }[] = [
  { route: "library", label: "Workflows", glyph: "▤" },
  { route: "record", label: "Record", glyph: "●" },
  { route: "runner", label: "Runner", glyph: "⇅" },
  { route: "settings", label: "Settings", glyph: "⚙" },
];

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [checkedAuth, setCheckedAuth] = useState(false);
  const [onboarded, setOnboarded] = useState(false);
  const [route, setRoute] = useState<Route>({ name: "library" });

  const [engineUp, setEngineUp] = useState(false);
  const [recording, setRecording] = useState(false);
  const [sync, setSync] = useState<SyncState>({ state: "synced", queued: 0 });
  const [breaks, setBreaks] = useState(0);

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
    ];
    return () => unsubs.forEach((p) => p.then((u) => u()).catch(() => {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!checkedAuth) {
    return <div className="center-stage"><span className="page-sub">Loading…</span></div>;
  }

  if (!auth?.authenticated) {
    return (
      <Login
        onAuthed={(s) => {
          setAuth(s);
        }}
      />
    );
  }

  if (!onboarded) {
    return (
      <Onboarding
        onStart={() => {
          setOnboarded(true);
          setRoute({ name: "record" });
        }}
      />
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
    <div className="app">
      <nav className="rail">
        <div className="wordmark">
          <span className="open">Open</span>
          <span className="adapt">Adapt</span>
        </div>

        {NAV.map((n) => (
          <button
            key={n.route}
            className={`nav-item ${route.name === n.route ? "active" : ""}`}
            onClick={() => setRoute({ name: n.route } as Route)}
          >
            <span className="glyph">{n.glyph}</span>
            {n.label}
            {n.route === "library" && breaks > 0 && (
              <>
                <span className="spacer" />
                <Pill tone="warn">{breaks}</Pill>
              </>
            )}
          </button>
        ))}

        <div className="nav-spacer" />

        <div className="rail-status">
          <div className="row">
            <StatusDot tone={engineUp ? "ok" : "off"} />
            <span>{engineUp ? "engine ready" : "engine offline"}</span>
          </div>
          <div className="row">
            <StatusDot tone={recording ? "warn" : "off"} />
            <span>{recording ? "recording" : "idle"}</span>
          </div>
          <div className="row">
            <StatusDot tone={syncTone} />
            <span>
              {sync.state}
              {sync.queued ? ` · ${sync.queued} queued` : ""}
            </span>
          </div>
        </div>
      </nav>

      <main>
        {route.name === "library" && (
          <WorkflowLibrary
            onWatch={(id) => setRoute({ name: "watch", id })}
            onTeach={(id) => setRoute({ name: "teach", id })}
            onRecord={() => setRoute({ name: "record" })}
          />
        )}
        {route.name === "record" && (
          <RecordReview
            onCompiled={(id) => setRoute({ name: "watch", id })}
          />
        )}
        {route.name === "watch" && (
          <WatchRun
            workflowId={route.id}
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
        {route.name === "settings" && (
          <Settings
            auth={auth}
            onSignedOut={() => setAuth({ authenticated: false })}
          />
        )}
      </main>
    </div>
  );
}
