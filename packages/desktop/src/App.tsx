// Product shell + routed screens.
//
// Three tabs carry the operator's whole day: Tasks (each task's one next
// step), Needs you (one inbox for every stop), and Results (what got done and
// checked). Settings and every expert tool sit behind the gear menu, so no
// engine capability is removed; it is one click deeper.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CMD,
  EVT,
  engineTry,
  onEngineEvent,
  onShadowPauseToggleRequested,
  onShadowSettingsRequested,
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
import { Button, StatusDot, Pill } from "./ui/primitives";
import { GearMenu, PageHead } from "./ui/simple";
import { Login } from "./screens/Login";
import { Onboarding } from "./screens/Onboarding";
import { Welcome } from "./screens/Welcome";
import { Tasks } from "./screens/Tasks";
import { NeedsYou } from "./screens/NeedsYou";
import { Stops } from "./screens/Stops";
import { Results } from "./screens/Results";
import { CheckIt } from "./screens/CheckIt";
import { RunIt } from "./screens/RunIt";
import { Sample } from "./screens/Sample";
import { Advanced } from "./screens/Advanced";
import { WorkflowLibrary } from "./screens/WorkflowLibrary";
import { RecordReview } from "./screens/RecordReview";
import { WatchRun } from "./screens/WatchRun";
import { Teach } from "./screens/Teach";
import { Runner } from "./screens/Runner";
import { Settings } from "./screens/Settings";
import { DecisionPortal } from "./screens/DecisionPortal";
import { deliverAttentionNotification } from "./lib/attentionNotification";
import { Qualification } from "./screens/Qualification";
import { clearLocalSession, rememberLocalSession } from "./localSession";
import { ItemApprovalDialog } from "./ui/ItemApproval";
import {
  ShadowModeIndicator,
  ShadowPromptBanner,
  useShadowMode,
} from "./ui/ShadowMode";

type Route =
  | { name: "tasks" }
  | { name: "needs" }
  | { name: "stops" }
  | { name: "results" }
  | { name: "sample" }
  | { name: "check"; id: string; firstWorkflow?: boolean }
  | { name: "run"; id: string }
  | { name: "advanced" }
  | { name: "setup" }
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

const NAV: { route: "tasks" | "needs" | "results"; label: string }[] = [
  { route: "tasks", label: "Workflows" },
  { route: "needs", label: "Needs you" },
  { route: "results", label: "Results" },
];

/** Screens that belong under one of the three tabs, for the active marker. */
function tabFor(route: Route): "tasks" | "needs" | "results" | null {
  switch (route.name) {
    case "tasks":
    case "check":
    case "run":
    case "sample":
    case "record":
    case "watch":
    case "teach":
      return "tasks";
    case "needs":
    case "stops":
      return "needs";
    case "results":
      return "results";
    default:
      return null;
  }
}

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
    (state.stage === "review" ||
      state.stage === "qualification" ||
      state.stage === "qualification_after_result") &&
    state.workflow_id
  ) {
    // The short Check it path replaces the read-only first run and the
    // review-only loop. Nothing has run yet at these stages.
    return { name: "check", id: state.workflow_id, firstWorkflow: true };
  }
  if (
    (state.stage === "executing" ||
      state.stage === "reconciliation" ||
      state.stage === "result") &&
    state.workflow_id
  ) {
    // A first run that started stays on its run screen: after an uncertain
    // run, a person checks the target before anything runs again.
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

// Shown when the engine didn't answer the library question at start. An
// engine that didn't answer is not an empty library, so Desktop must not send
// someone with tasks back to first run, or claim anything about their setup.
function EngineOffline({ onRetry, busy }: { onRetry: () => void; busy: boolean }) {
  return (
    <div className="content simple">
      <PageHead
        title="Waiting for the local engine"
        sub="Desktop can't reach the OpenAdapt engine on this computer, so it can't show your workflows yet. It tries again when the engine starts. If this doesn't clear, quit and reopen OpenAdapt."
      />
      <div className="row">
        <Button variant="primary" disabled={busy} onClick={onRetry}>
          {busy ? "Trying…" : "Try again"}
        </Button>
      </div>
    </div>
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
  // null while the engine hasn't answered whether any task exists yet.
  const [onboarded, setOnboarded] = useState<boolean | null>(null);
  const onboardedRef = useRef<boolean | null>(null);
  onboardedRef.current = onboarded;
  const [entryRetrying, setEntryRetrying] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [route, setRoute] = useState<Route>({ name: "tasks" });
  // Shadow mode is off unless the person turned it on from Settings. While on,
  // the header and the tray both say so.
  const { status: shadow, togglePause: toggleShadowPause } = useShadowMode({ syncTray: true });
  useEffect(() => {
    const unlisten = onShadowPauseToggleRequested(() => void toggleShadowPause());
    return () => void unlisten.then((fn) => fn()).catch(() => {});
  }, [toggleShadowPause]);
  useEffect(() => {
    const unlisten = onShadowSettingsRequested(() => setRoute({ name: "settings" }));
    return () => void unlisten.then((fn) => fn()).catch(() => {});
  }, []);
  const [screenBusy, setScreenBusy] = useState(false);

  const [engineUp, setEngineUp] = useState(false);
  const [recording, setRecording] = useState(false);
  // Unknown until the engine reports it: never assume Cloud has a copy.
  const [sync, setSync] = useState<SyncState | null>(null);
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

  // Which entry screen to show: first run or the tasks. It needs a real
  // answer from the engine for both questions.
  async function loadEntry(): Promise<boolean> {
    const [wf, firstWorkflow] = await Promise.all([
      engineTry<Workflow[] | null>(CMD.GET_WORKFLOWS, {}, null),
      engineTry<FirstWorkflowStateResponse | null>(
        CMD.GET_FIRST_WORKFLOW_STATE,
        {},
        null,
      ),
    ]);
    if (!Array.isArray(wf) || !firstWorkflow) return false;
    // A person may have moved on while this was in flight.
    if (onboardedRef.current === true) return true;
    const resumedRoute = routeForFirstWorkflow(firstWorkflow.state);
    setFirstWorkflowState(firstWorkflow.state);
    setOnboarded(wf.length > 0 || resumedRoute !== null);
    if (resumedRoute) setRoute(resumedRoute);
    return true;
  }

  async function retryEntry() {
    if (onboardedRef.current !== null) return;
    setEntryRetrying(true);
    try {
      if (await loadEntry()) {
        // These were fallbacks too while the engine was down.
        setAuth(
          await engineTry<AuthStatus>(
            CMD.GET_AUTH_STATUS,
            {},
            { authenticated: false },
          ),
        );
        await refreshAttention();
        setSync(await engineTry<SyncState | null>(CMD.GET_SYNC_STATE, {}, null));
      }
    } finally {
      setEntryRetrying(false);
    }
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
        await loadEntry();
        const na = await engineTry<NeedsAttention>(
          CMD.GET_NEEDS_ATTENTION,
          {},
          { count: 0, open_halts: 0, failed_runs: 0 },
        );
        setBreaks(na.count);
        const ss = await engineTry<SyncState | null>(CMD.GET_SYNC_STATE, {}, sync);
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
      onEngineEvent(EVT.SIDECAR_STATE, (d: { running: boolean }) => {
        setEngineUp(!!d?.running);
        if (d?.running && onboardedRef.current === null) void retryEntry();
      }),
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
                ? `This job is coach-only on this window. ${authoring.client_display || "Your assistant"} can suggest; you click.`
                : authoring.allowed
                  ? "Pin the browser URL or use this window. OpenAdapt doesn't send window titles to your assistant."
                  : `This computer is bound. Pin the window, then Allow ${authoring.client_display || "your assistant"} to drive this job.`
              : authoring.prompt ||
                (authoring.status === "replace_allow"
                  ? `A different ${authoring.client_display || "assistant"} account is asking. Allow it to replace the current one?`
                  : `Allow ${authoring.client_display || "your assistant"} to drive this job`)}
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

  // A first workflow leaves the read-only first-run path when it opens Check
  // it. That path is only left before anything ran; a run that started stays
  // on its own screen until a person has checked the target.
  const leaveFirstWorkflowPath = useCallback(
    async (workflowId: string): Promise<boolean> => {
      const current = firstWorkflowStateRef.current;
      if (!current || current.workflow_id !== workflowId) return true;
      if (current.stage === "complete") return true;
      if (current.stage === "executing" || current.stage === "reconciliation") {
        return false;
      }
      if (current.stage === "result") {
        if (!(await saveFirstWorkflowStage("qualification_after_result", workflowId))) {
          return false;
        }
      }
      return saveFirstWorkflowStage("complete", workflowId);
    },
    // saveFirstWorkflowStage only reads setters and the engine.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    if (route.name !== "check" || !route.firstWorkflow) return;
    void leaveFirstWorkflowPath(route.id).then((left) => {
      if (left) {
        setRoute({ name: "check", id: route.id });
        return;
      }
      const resumed = routeForFirstWorkflow(firstWorkflowStateRef.current);
      setRoute(resumed && resumed.name === "watch" ? resumed : { name: "tasks" });
    });
  }, [route, leaveFirstWorkflowPath]);

  async function openCheck(workflowId: string) {
    const current = firstWorkflowState;
    if (
      current?.workflow_id === workflowId &&
      (current.stage === "executing" || current.stage === "reconciliation")
    ) {
      const resumed = routeForFirstWorkflow(current);
      if (resumed) setRoute(resumed);
      return;
    }
    setRoute({ name: "check", id: workflowId, firstWorkflow: true });
  }

  async function startFirstTask() {
    rememberLocalSession();
    const current = firstWorkflowStateRef.current;
    if (current?.stage === "executing" || current?.stage === "reconciliation") {
      // An uncertain run keeps its lock: check its target first.
      const resumed = routeForFirstWorkflow(current);
      if (resumed) setRoute(resumed);
      setFirstWorkflowStageError(
        "Check the target of your last run before you record another workflow.",
      );
      return;
    }
    if (!(await saveFirstWorkflowStage("record"))) return;
    setOnboarded(true);
    setShowLogin(false);
    setRoute({ name: "record", firstWorkflow: true });
  }

  function openSample() {
    rememberLocalSession();
    setOnboarded(true);
    setShowLogin(false);
    setRoute({ name: "sample" });
  }

  async function refreshAttention() {
    const na = await engineTry<NeedsAttention>(
      CMD.GET_NEEDS_ATTENTION,
      {},
      { count: breaks, open_halts: 0, failed_runs: 0 },
    );
    setBreaks(na.count);
  }

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

  if (showLogin) {
    return (
      <>
        {pairingNotice}
        {authoringNotice}
        {firstWorkflowStageNotice}
        <DesktopEntryShell>
          <Login
            onLocal={() => {
              rememberLocalSession();
              setShowLogin(false);
            }}
            onAuthed={(s) => {
              setAuth(s);
              setShowLogin(false);
            }}
          />
        </DesktopEntryShell>
      </>
    );
  }

  if (onboarded === null) {
    return (
      <>
        {pairingNotice}
        {authoringNotice}
        {firstWorkflowStageNotice}
        <DesktopEntryShell>
          <EngineOffline busy={entryRetrying} onRetry={() => void retryEntry()} />
        </DesktopEntryShell>
      </>
    );
  }

  // Nothing shown yet: the welcome screen. It needs no account and no
  // permission; Cloud sign-in is one click away on it.
  if (!onboarded) {
    return (
      <>
        {pairingNotice}
        {authoringNotice}
        {firstWorkflowStageNotice}
        <DesktopEntryShell>
          <Welcome
            onSample={openSample}
            onShow={() => void startFirstTask()}
            onConnectCloud={() => setShowLogin(true)}
          />
        </DesktopEntryShell>
      </>
    );
  }

  const navigationLocked =
    recording ||
    workflowCompiling ||
    firstWorkflowRunning ||
    firstRunPersistencePending ||
    screenBusy;
  const navigationLockTitle = recording
    ? "Stop and save this recording before you leave."
    : workflowCompiling
      ? "Keep this screen open while OpenAdapt builds the workflow from your recording."
      : firstWorkflowRunning
        ? "Keep the run open until it stops."
        : firstRunPersistencePending
          ? "Save this first run before you leave the result."
          : screenBusy
            ? "Keep this screen open until OpenAdapt finishes."
            : undefined;
  const activeTab = tabFor(route);
  const cloudConnected = Boolean(auth?.authenticated);
  const syncTone =
    sync?.state === "synced"
      ? "ok"
      : sync?.state === "pushing"
        ? "run"
        : sync?.state === "paused"
          ? "warn"
          : "off";
  const syncLabel = sync?.state === "local" ? "Local only" : sync?.state;

  return (
    <>
      {pairingNotice}
      {authoringNotice}
      {firstWorkflowStageNotice}
      <ItemApprovalDialog />
      <div className="app">
        <header className="desktop-shell">
          <div className="desktop-shell-inner">
            <DesktopBrand
              onOpen={
                navigationLocked
                  ? undefined
                  : () => setRoute({ name: "tasks" })
              }
            />

            <nav className="desktop-nav" aria-label="Desktop navigation">
              {NAV.map((n) => (
                <button
                  aria-current={activeTab === n.route ? "page" : undefined}
                  className={`nav-item ${activeTab === n.route ? "active" : ""}`}
                  disabled={navigationLocked}
                  title={navigationLockTitle}
                  key={n.route}
                  onClick={() => setRoute({ name: n.route } as Route)}
                  type="button"
                >
                  <span>{n.label}</span>
                  {n.route === "needs" && breaks > 0 ? (
                    <Pill tone="warn">{breaks}</Pill>
                  ) : null}
                </button>
              ))}
            </nav>

            <div className="desktop-shell-spacer" />

            <div className="desktop-status" aria-label="Desktop status">
              {!engineUp && (
                <span title="The local OpenAdapt engine isn't running">
                  <StatusDot tone="off" />
                  <strong>Engine stopped</strong>
                </span>
              )}
              {recording ? (
                <span className="recording" title="OpenAdapt is recording your workflow">
                  <StatusDot tone="warn" />
                  <strong>Recording</strong>
                </span>
              ) : null}
              <ShadowModeIndicator
                status={shadow}
                onTogglePause={() => void toggleShadowPause()}
              />
              {cloudConnected && sync && sync.state !== "synced" && (
                <span title={`Cloud: ${syncLabel}`}>
                  <StatusDot tone={syncTone} />
                  <strong>{syncLabel}</strong>
                  {sync.queued ? <small>{sync.queued}</small> : null}
                </span>
              )}
            </div>

            <GearMenu
              disabled={navigationLocked}
              title={navigationLockTitle}
              items={[
                {
                  label: "Settings",
                  sub: "Account, privacy, updates",
                  onSelect: () => setRoute({ name: "settings" }),
                },
                {
                  label: "Advanced",
                  sub: "Exact checks, every version, runner, phone",
                  onSelect: () => setRoute({ name: "advanced" }),
                },
              ]}
            />
          </div>
        </header>

        <main>
        <ShadowPromptBanner status={shadow} />
        {route.name === "tasks" && (
          <Tasks
            attention={breaks}
            onShow={() => void startFirstTask()}
            onSample={() => setRoute({ name: "sample" })}
            onCheck={(id) => void openCheck(id)}
            onRun={(id) => setRoute({ name: "run", id })}
            onNeedsYou={() => setRoute({ name: "needs" })}
            onWatch={(id) => {
              if (firstWorkflowState?.workflow_id === id) {
                const resumed = routeForFirstWorkflow(firstWorkflowState);
                if (resumed && resumed.name === "watch") {
                  setRoute(resumed);
                  return;
                }
              }
              setRoute({ name: "watch", id });
            }}
            onExactChecks={(id) => setRoute({ name: "qualify", id })}
            onTeach={(id) => setRoute({ name: "teach", id })}
          />
        )}
        {route.name === "needs" && (
          <NeedsYou
            onTeach={(id) => setRoute({ name: "teach", id })}
            onChanged={() => void refreshAttention()}
            onReviewStops={() => setRoute({ name: "stops" })}
          />
        )}
        {route.name === "stops" && (
          <Stops
            onDone={() => {
              void refreshAttention();
              setRoute({ name: "needs" });
            }}
          />
        )}
        {route.name === "results" && (
          <Results onNeedsYou={() => setRoute({ name: "needs" })} />
        )}
        {route.name === "sample" && (
          <Sample
            onShow={() => void startFirstTask()}
            onTasks={() => setRoute({ name: "tasks" })}
            onRunningChange={setScreenBusy}
          />
        )}
        {route.name === "check" && !route.firstWorkflow && (
          <CheckIt
            workflowId={route.id}
            onExactChecks={(id) => setRoute({ name: "qualify", id })}
            onRun={(id) => setRoute({ name: "run", id })}
          />
        )}
        {route.name === "check" && route.firstWorkflow && (
          <div className="content simple">
            <p className="simple-sub">Opening Check it…</p>
          </div>
        )}
        {route.name === "run" && (
          <RunIt
            workflowId={route.id}
            onCheck={(id) => void openCheck(id)}
            onNeedsYou={() => {
              void refreshAttention();
              setRoute({ name: "needs" });
            }}
            onResults={() => setRoute({ name: "results" })}
            onRunningChange={setScreenBusy}
          />
        )}
        {route.name === "advanced" && (
          <Advanced
            onExactChecks={(id) => setRoute({ name: "qualify", id })}
            onLibrary={() => setRoute({ name: "library" })}
            onRecord={() => setRoute({ name: "record" })}
            onRunner={() => setRoute({ name: "runner" })}
            onPhone={() => setRoute({ name: "portal" })}
            onSetup={() => setRoute({ name: "setup" })}
          />
        )}
        {route.name === "setup" && <Onboarding />}
        {route.name === "library" && (
          <WorkflowLibrary
            onQualify={(id) => setRoute({ name: "qualify", id })}
            onWatch={(id) => {
              if (firstWorkflowState?.workflow_id === id) {
                const resumed = routeForFirstWorkflow(firstWorkflowState);
                if (resumed && resumed.name === "watch") {
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
            onBack={() => setRoute({ name: "tasks" })}
            onOpenWorkflow={(id) => setRoute({ name: "qualify", id })}
          />
        )}
        {route.name === "record" && (
          <RecordReview
            firstWorkflow={route.firstWorkflow}
            initialTarget={route.target}
            initialTask={route.task}
            onCompiled={(id) => {
              if (!route.firstWorkflow) {
                void openCheck(id);
                return;
              }
              void refreshFirstWorkflowState().then((state) => {
                const resumed = routeForFirstWorkflow(state);
                setRoute(resumed ?? { name: "check", id, firstWorkflow: true });
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
              setRoute({ name: "check", id, firstWorkflow: true });
            }}
            onQualify={(id) => void openCheck(id)}
            onTeach={(id) => setRoute({ name: "teach", id })}
          />
        )}
        {route.name === "teach" && (
          <Teach
            workflowId={route.id}
            onDone={() => setRoute({ name: "tasks" })}
          />
        )}
        {route.name === "runner" && <Runner />}
        {route.name === "portal" && <DecisionPortal />}
        {route.name === "settings" && (
          <Settings
            auth={auth ?? { authenticated: false }}
            onConnectCloud={() => {
              clearLocalSession();
              setShowLogin(true);
            }}
            onSignedOut={() => setAuth({ authenticated: false })}
          />
        )}
        </main>
      </div>
    </>
  );
}
