// Product shell + routed screens, gated by first-run/auth.
// The top shell uses the same OpenAdapt | Product pattern as Cloud. It also
// carries the local engine, recording, sync, and attention state.
import { useEffect, useRef, useState } from "react";
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
  FirstWorkflowState,
  FirstWorkflowStateResponse,
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
  | {
      name: "record";
      firstWorkflow?: boolean;
      target?: ExecutionTarget;
      task?: string;
    }
  | {
      name: "qualify";
      id: string;
      firstWorkflow?: boolean;
      firstRunComplete?: boolean;
      target?: ExecutionTarget;
    }
  | {
      name: "watch";
      id: string;
      target?: ExecutionTarget;
      firstWorkflow?: boolean;
      firstRunComplete?: boolean;
      firstRunLocked?: boolean;
    }
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

export function routeForFirstWorkflow(
  state: FirstWorkflowState | null,
): Route | null {
  if (!state || state.stage === "complete") return null;
  if (state.stage === "record") {
    return {
      name: "record",
      firstWorkflow: true,
      target: state.target ?? undefined,
      task: state.task || undefined,
    };
  }
  if (
    (state.stage === "qualification" ||
      state.stage === "qualification_after_result") &&
    state.workflow_id
  ) {
    return {
      name: "qualify",
      id: state.workflow_id,
      firstWorkflow: true,
      firstRunComplete: state.stage === "qualification_after_result",
      target: state.target ?? undefined,
    };
  }
  if (
    (state.stage === "review" ||
      state.stage === "executing" ||
      state.stage === "reconciliation" ||
      state.stage === "result") &&
    state.workflow_id
  ) {
    return {
      name: "watch",
      id: state.workflow_id,
      target: state.target ?? undefined,
      firstWorkflow: true,
      firstRunComplete: state.stage === "result",
      firstRunLocked:
        state.stage === "executing" || state.stage === "reconciliation",
    };
  }
  return null;
}

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
  const [authoring, setAuthoring] = useState<{
    status: string;
    client_display?: string;
    prompt?: string;
    error?: string;
    allowed?: boolean;
    coach_only?: boolean;
  } | null>(null);
  const [authoringUrl, setAuthoringUrl] = useState("");
  const [firstRunPersistencePending, setFirstRunPersistencePending] =
    useState(false);
  const [firstWorkflowRunning, setFirstWorkflowRunning] = useState(false);
  const [workflowCompiling, setWorkflowCompiling] = useState(false);
  const [firstWorkflowState, setFirstWorkflowState] =
    useState<FirstWorkflowState | null>(null);
  const firstWorkflowStateRef = useRef<FirstWorkflowState | null>(null);
  const [firstWorkflowStageError, setFirstWorkflowStageError] = useState("");
  firstWorkflowStateRef.current = firstWorkflowState;

  async function saveFirstWorkflowStage(
    stage:
      | "record"
      | "review"
      | "qualification"
      | "qualification_after_result"
      | "complete",
    workflowId?: string,
  ): Promise<boolean> {
    setFirstWorkflowStageError("");
    const result = await engineTry<{
      ok: boolean;
      state?: FirstWorkflowState | null;
    }>(
      CMD.SET_FIRST_WORKFLOW_STAGE,
      {
        stage,
        ...(workflowId ? { workflow_id: workflowId } : {}),
      },
      { ok: false },
    );
    if (result.ok) {
      if ("state" in result) setFirstWorkflowState(result.state ?? null);
      return true;
    }
    setFirstWorkflowStageError(
      "Desktop couldn't save your place in setup. Check the local engine, then try again.",
    );
    return false;
  }

  async function refreshFirstWorkflowState(): Promise<FirstWorkflowState | null> {
    const result = await engineTry<FirstWorkflowStateResponse>(
      CMD.GET_FIRST_WORKFLOW_STATE,
      {},
      { ok: true, state: null },
    );
    setFirstWorkflowState(result.state);
    return result.state;
  }

  // Bootstrap: auth status, sidecar liveness, and the status channels.
  // Authoring bind is optional — a missing status must not leave the shell
  // on Loading, or first-workflow navigation never mounts.
  useEffect(() => {
    (async () => {
      try {
        setEngineUp(await sidecarRunning());
        const a = await engineTry<AuthStatus>(
          CMD.GET_AUTH_STATUS,
          {},
          { authenticated: false },
        );
        setAuth(a);
        const [wf, firstWorkflow] = await Promise.all([
          engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []),
          engineTry<FirstWorkflowStateResponse>(
            CMD.GET_FIRST_WORKFLOW_STATE,
            {},
            { ok: true, state: null },
          ),
        ]);
        const resumedRoute = routeForFirstWorkflow(firstWorkflow.state);
        setFirstWorkflowState(firstWorkflow.state);
        setOnboarded(wf.length > 0 || resumedRoute !== null);
        if (resumedRoute) setRoute(resumedRoute);
        const na = await engineTry<NeedsAttention>(
          CMD.GET_NEEDS_ATTENTION,
          {},
          { count: 0, open_halts: 0, failed_runs: 0 },
        );
        setBreaks(na.count);
        const ss = await engineTry<SyncState>(CMD.GET_SYNC_STATE, {}, sync);
        setSync(ss);
        const authoringStatus = await engineTry<{
          status: string;
          client_display?: string;
          prompt?: string;
          error?: string;
          allowed?: boolean;
          coach_only?: boolean;
        } | null>(CMD.AUTHORING_STATUS, {}, { status: "idle" });
        if (authoringStatus?.status && authoringStatus.status !== "idle") {
          setAuthoring(authoringStatus);
        }
      } finally {
        setCheckedAuth(true);
      }
    })();

    const unsubs = [
      onEngineEvent(EVT.SIDECAR_STATE, (d: { running: boolean }) =>
        setEngineUp(!!d?.running),
      ),
      onEngineEvent(EVT.STATUS_UPDATE, (s: { recording?: boolean }) =>
        setRecording(!!s?.recording),
      ),
      onEngineEvent(EVT.RECORDING_STARTED, () => setRecording(true)),
      onEngineEvent(EVT.RECORDING_STOPPED, () => {
        setRecording(false);
        if (firstWorkflowStateRef.current?.stage === "record") {
          setWorkflowCompiling(true);
        }
      }),
      onEngineEvent(
        EVT.COMPILE_PROGRESS,
        (progress: { state?: string; bundle_id?: string }) => {
          if (progress.state === "compiling") {
            setWorkflowCompiling(true);
            return;
          }
          if (
            progress.state === "compiled" ||
            progress.state === "failed" ||
            progress.state === "review_failed"
          ) {
            setWorkflowCompiling(false);
          }
          if (progress.state === "compiled" && progress.bundle_id) {
            void refreshFirstWorkflowState().then((state) => {
              const resumed = routeForFirstWorkflow(state);
              if (resumed) setRoute(resumed);
            });
          }
        },
      ),
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
      onEngineEvent(
        EVT.AUTHORING_STATE,
        (state: {
          status: string;
          client_display?: string;
          prompt?: string;
          error?: string;
          allowed?: boolean;
          coach_only?: boolean;
        }) => {
          setAuthoring(state);
        },
      ),
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
  const authoringNotice =
    authoring &&
    (authoring.status === "pending_allow" ||
      authoring.status === "replace_allow" ||
      authoring.status === "error" ||
      authoring.status === "bound") ? (
      <div
        className={`pairing-notice ${authoring.status === "error" ? "error" : ""}`}
        role={authoring.status === "error" ? "alert" : "status"}
      >
        <span>
          {authoring.status === "error"
            ? authoring.error || "The authoring bind could not be completed."
            : authoring.status === "bound"
              ? authoring.coach_only
                ? "This job is coach-only on this window. ChatGPT can suggest; you click."
                : authoring.allowed
                  ? "Pin the browser URL or use this window. Titles stay on this computer."
                  : "This computer is bound. Pin the window, then Allow ChatGPT to drive this job."
              : authoring.prompt ||
                (authoring.status === "replace_allow"
                  ? `A different ${authoring.client_display || "ChatGPT"} account is asking. Allow it to replace the current one?`
                  : `Allow ${authoring.client_display || "ChatGPT"} to drive this job`)}
        </span>
        {(authoring.status === "pending_allow" ||
          authoring.status === "replace_allow") && (
          <span className="allow-actions">
            <button
              type="button"
              onClick={() => {
                void engineTry(
                  CMD.AUTHORING_ALLOW,
                  { replace: authoring.status === "replace_allow" },
                  {},
                ).then(() =>
                  engineTry(
                    CMD.AUTHORING_STATUS,
                    {},
                    { status: "bound", allowed: true },
                  ).then((next) => setAuthoring(next)),
                );
              }}
            >
              Allow
            </button>
            <button
              type="button"
              onClick={() => {
                void engineTry(CMD.AUTHORING_DENY, {}, {}).then(() =>
                  engineTry(
                    CMD.AUTHORING_STATUS,
                    {},
                    { status: "bound", allowed: false },
                  ).then((next) => setAuthoring(next)),
                );
              }}
            >
              Not now
            </button>
          </span>
        )}
        {authoring.status === "bound" && (
          <span className="allow-actions">
            <input
              aria-label="Playwright URL"
              onChange={(event) => setAuthoringUrl(event.target.value)}
              placeholder="https://"
              type="url"
              value={authoringUrl}
            />
            <button
              type="button"
              onClick={() => {
                void engineTry(
                  CMD.AUTHORING_PIN_TARGET,
                  { backend: "web", url: authoringUrl },
                  {},
                ).then((next) =>
                  setAuthoring((current) =>
                    current
                      ? {
                          ...current,
                          coach_only:
                            typeof next === "object" &&
                            next !== null &&
                            "coach_only" in next
                              ? Boolean(
                                  (next as { coach_only?: boolean }).coach_only,
                                )
                              : current.coach_only,
                        }
                      : current,
                  ),
                );
              }}
            >
              Pin browser
            </button>
            <button
              type="button"
              onClick={() => {
                void engineTry(
                  CMD.AUTHORING_PIN_TARGET,
                  { use_frontmost: true },
                  {},
                ).then((next) =>
                  setAuthoring((current) =>
                    current
                      ? {
                          ...current,
                          coach_only:
                            typeof next === "object" &&
                            next !== null &&
                            "coach_only" in next
                              ? Boolean(
                                  (next as { coach_only?: boolean }).coach_only,
                                )
                              : true,
                        }
                      : current,
                  ),
                );
              }}
            >
              Use this window
            </button>
          </span>
        )}
        {authoring.status === "error" && (
          <button
            type="button"
            aria-label="Dismiss authoring notice"
            onClick={() => setAuthoring(null)}
          >
            ×
          </button>
        )}
      </div>
    ) : null;
  const firstWorkflowStageNotice = firstWorkflowStageError ? (
    <div className="pairing-notice error" role="alert">
      <span>{firstWorkflowStageError}</span>
      <button
        type="button"
        aria-label="Dismiss setup notice"
        onClick={() => setFirstWorkflowStageError("")}
      >
        ×
      </button>
    </div>
  ) : null;

  if (!checkedAuth) {
    return (
      <>
        {pairingNotice}
        {authoringNotice}
        {firstWorkflowStageNotice}
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
        {authoringNotice}
        {firstWorkflowStageNotice}
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
        {authoringNotice}
        {firstWorkflowStageNotice}
        <DesktopEntryShell>
          <Onboarding
            onStart={async () => {
              if (!(await saveFirstWorkflowStage("record"))) return;
              setOnboarded(true);
              setRoute({ name: "record", firstWorkflow: true });
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
  const navigationLocked =
    recording ||
    workflowCompiling ||
    firstWorkflowRunning ||
    firstRunPersistencePending;
  const navigationLockTitle = recording
    ? "Stop and save this recording before you leave."
    : workflowCompiling
      ? "Keep setup open while Desktop compiles the recording."
      : firstWorkflowRunning
        ? "Keep the supervised run open until it stops."
        : firstRunPersistencePending
          ? "Save this first run before you leave the result."
          : undefined;

  return (
    <>
      {pairingNotice}
      {authoringNotice}
      {firstWorkflowStageNotice}
      <div className="app">
        <header className="desktop-shell">
          <div className="desktop-shell-inner">
            <DesktopBrand
              onOpen={
                navigationLocked
                  ? undefined
                  : () => setRoute({ name: "library" })
              }
            />

            <nav className="desktop-nav" aria-label="Desktop navigation">
              {NAV.map((n) => (
                <button
                  aria-current={route.name === n.route ? "page" : undefined}
                  className={`nav-item ${route.name === n.route ? "active" : ""}`}
                  disabled={navigationLocked}
                  title={navigationLockTitle}
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
            onQualify={(id) => {
              if (
                firstWorkflowState?.workflow_id === id &&
                firstWorkflowState.stage !== "complete"
              ) {
                const afterResult = [
                  "result",
                  "qualification_after_result",
                ].includes(firstWorkflowState.stage);
                void saveFirstWorkflowStage(
                  afterResult ? "qualification_after_result" : "qualification",
                  id,
                ).then((saved) => {
                  if (!saved) return;
                  setRoute({
                    name: "qualify",
                    id,
                    firstWorkflow: true,
                    firstRunComplete: afterResult,
                    target: firstWorkflowState.target ?? undefined,
                  });
                });
                return;
              }
              setRoute({ name: "qualify", id });
            }}
            onWatch={(id) => {
              if (firstWorkflowState?.workflow_id === id) {
                const resumed = routeForFirstWorkflow(firstWorkflowState);
                if (resumed) {
                  setRoute(resumed);
                  return;
                }
              }
              setRoute({ name: "watch", id });
            }}
            onTeach={(id) => setRoute({ name: "teach", id })}
            onRecord={() => setRoute({ name: "record" })}
          />
        )}
        {route.name === "qualify" && (
          <Qualification
            workflowId={route.id}
            backLabel={
              route.firstWorkflow && !route.firstRunComplete
                ? "Back to supervised run"
                : undefined
            }
            reviewOnly={Boolean(
              route.firstWorkflow && !route.firstRunComplete,
            )}
            onBack={async () => {
              if (route.firstWorkflow) {
                if (route.firstRunComplete) {
                  if (!(await saveFirstWorkflowStage("complete", route.id))) {
                    return;
                  }
                  setRoute({ name: "library" });
                } else {
                  if (!(await saveFirstWorkflowStage("review", route.id))) {
                    return;
                  }
                  setRoute({
                    name: "watch",
                    id: route.id,
                    target: route.target,
                    firstWorkflow: true,
                  });
                }
                return;
              }
              setRoute({ name: "library" });
            }}
            onOpenWorkflow={async (id) => {
              if (route.firstWorkflow) {
                if (
                  !(await saveFirstWorkflowStage(
                    route.firstRunComplete
                      ? "qualification_after_result"
                      : "qualification",
                    id,
                  ))
                ) {
                  return;
                }
              }
              setRoute({
                name: "qualify",
                id,
                firstWorkflow: route.firstWorkflow,
                firstRunComplete: route.firstRunComplete,
                target: route.target,
              });
            }}
          />
        )}
        {route.name === "record" && (
          <RecordReview
            firstWorkflow={route.firstWorkflow}
            initialTarget={route.target}
            initialTask={route.task}
            onCompiled={(id, target) => {
              if (!route.firstWorkflow) {
                setRoute({ name: "watch", id, target });
                return;
              }
              void refreshFirstWorkflowState().then((state) => {
                const resumed = routeForFirstWorkflow(state);
                setRoute(
                  resumed ?? {
                    name: "watch",
                    id,
                    target,
                    firstWorkflow: true,
                  },
                );
              });
            }}
          />
        )}
        {route.name === "watch" && (
          <WatchRun
            workflowId={route.id}
            initialTarget={route.target}
            firstWorkflow={route.firstWorkflow}
            firstRunComplete={route.firstRunComplete}
            firstRunLocked={route.firstRunLocked}
            onPersistencePendingChange={setFirstRunPersistencePending}
            onRunningChange={setFirstWorkflowRunning}
            onFirstWorkflowStateChange={() => {
              void refreshFirstWorkflowState().then((state) => {
                const resumed = routeForFirstWorkflow(state);
                if (resumed) setRoute(resumed);
              });
            }}
            onReconcile={async (id) => {
              if (!(await saveFirstWorkflowStage("review", id))) return;
              setRoute({
                name: "watch",
                id,
                target: route.target,
                firstWorkflow: true,
              });
            }}
            onQualify={async (id, afterSavedResult = false, target) => {
              if (route.firstWorkflow) {
                const stage = afterSavedResult
                  ? "qualification_after_result"
                  : "qualification";
                if (!(await saveFirstWorkflowStage(stage, id))) {
                  return;
                }
              }
              setRoute({
                name: "qualify",
                id,
                firstWorkflow: route.firstWorkflow,
                firstRunComplete: afterSavedResult,
                target,
              });
            }}
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
