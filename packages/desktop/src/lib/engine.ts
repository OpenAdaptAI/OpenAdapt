// Engine bridge — the single place the frontend talks to the Python engine.
//
// Everything goes through the Tauri `engine_invoke(cmd, params)` command, which
// forwards to the frozen `openadapt-engine` sidecar over JSON-lines
// (see src-tauri/src/sidecar.rs). Engine events arrive as Tauri events named
// `engine://<event>`.
//
// CMD / EVT below are the SHARED WIRE CONTRACT. The engine (W1) implements these
// command handlers and emits these events; the tray (W3) mirrors the same names
// over its loopback socket (spec §3d). Keep this list authoritative.

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/** Commands the frontend sends to the engine (Tauri cmd === engine IPC cmd). */
export const CMD = {
  // recording lifecycle
  START_RECORDING: "start_recording",
  STOP_RECORDING: "stop_recording",
  PAUSE_RECORDING: "pause_recording",
  RESUME_RECORDING: "resume_recording",
  GET_STATUS: "get_status",
  // library / captures / workflows
  GET_WORKFLOWS: "get_workflows",
  GET_CAPTURES: "get_captures",
  GET_STORAGE_USAGE: "get_storage_usage",
  // the loop: compile -> replay/run -> teach
  COMPILE_RECORDING: "compile_recording",
  REPLAY_WORKFLOW: "replay_workflow",
  RUN_WORKFLOW: "run_workflow",
  GET_RUN_REPORT: "get_run_report",
  TEACH_FIX: "teach_fix",
  // cloud sync / push
  PUSH_WORKFLOW: "push_workflow",
  GET_SYNC_STATE: "get_sync_state",
  PAUSE_SYNC: "pause_sync",
  RESUME_SYNC: "resume_sync",
  GET_NEEDS_ATTENTION: "get_needs_attention",
  // auth (both providers live in the engine: engine.auth — spec §3a)
  LOGIN_BROWSER: "login_browser",
  LOGIN_PASTE: "login_paste",
  LOGOUT: "logout",
  GET_AUTH_STATUS: "get_auth_status",
  // config / settings (lane, phi_mode, hosted host)
  GET_CONFIG: "get_config",
  SET_CONFIG: "set_config",
  // OS permissions (screen recording / accessibility)
  CHECK_PERMISSIONS: "check_permissions",
  // review / egress gate (existing engine surface)
  SCRUB_CAPTURE: "scrub_capture",
  APPROVE_REVIEW: "approve_review",
  DISMISS_REVIEW: "dismiss_review",
  GET_PENDING_REVIEWS: "get_pending_reviews",
  // runner lane (EXPERIMENTAL — outbound /api/runners/* long-poll)
  RUNNER_STATUS: "runner_status",
  RUNNER_ENABLE: "runner_enable",
  RUNNER_DISABLE: "runner_disable",
} as const;

/** Events the engine emits (delivered as Tauri events `engine://<name>`). */
export const EVT = {
  RECORDING_STARTED: "recording_started",
  RECORDING_STOPPED: "recording_stopped",
  RECORDING_ERROR: "recording_error",
  STATUS_UPDATE: "status_update",
  COMPILE_PROGRESS: "compile_progress",
  REPLAY_PROGRESS: "replay_progress",
  LOG_LINE: "log_line",
  SYNC_STATE: "sync_state",
  BREAK_COUNT: "break_count",
  SIDECAR_STATE: "sidecar_state",
  RUNNER_STATE: "runner_state",
} as const;

export type EngineEvent = (typeof EVT)[keyof typeof EVT];

/**
 * Invoke an engine command. Returns the engine's `data` payload.
 * Throws a string error if the sidecar is offline or the command failed — most
 * callers wrap this and degrade gracefully (the app renders without an engine).
 */
export async function engineInvoke<T = unknown>(
  cmd: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  return invoke<T>("engine_invoke", { cmd, params });
}

/** Best-effort variant: returns `fallback` instead of throwing (offline-safe). */
export async function engineTry<T>(
  cmd: string,
  params: Record<string, unknown>,
  fallback: T,
): Promise<T> {
  try {
    return await engineInvoke<T>(cmd, params);
  } catch {
    return fallback;
  }
}

/** Is the engine sidecar process currently running? */
export async function sidecarRunning(): Promise<boolean> {
  try {
    return await invoke<boolean>("sidecar_status");
  } catch {
    return false;
  }
}

/** Open a URL in the system browser (login deep-link, cloud dashboard, panes). */
export async function openExternal(url: string): Promise<void> {
  try {
    await invoke("open_external", { url });
  } catch (e) {
    // Fall back to a plain anchor navigation when running outside Tauri (dev).
    window.open(url, "_blank");
    void e;
  }
}

/** Subscribe to an engine event. Returns an unlisten fn. */
export function onEngineEvent<T = unknown>(
  event: EngineEvent,
  handler: (payload: T) => void,
): Promise<UnlistenFn> {
  return listen<T>(`engine://${event}`, (e) => handler(e.payload));
}

/** True when running inside a Tauri webview (vs. a plain browser dev preview). */
export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
