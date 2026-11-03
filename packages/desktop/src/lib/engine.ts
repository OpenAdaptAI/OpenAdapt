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
import type { FfmpegRuntimeStatus } from "./types";

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
  GET_FIRST_WORKFLOW_STATE: "get_first_workflow_state",
  SET_FIRST_WORKFLOW_STAGE: "set_first_workflow_stage",
  GET_CAPTURES: "get_captures",
  GET_STORAGE_USAGE: "get_storage_usage",
  GET_PRESENTATION_EXPORT_STATUS: "get_presentation_export_status",
  EXPORT_PRESENTATION_VIDEO: "export_presentation_video",
  // the loop: compile -> replay/run -> teach
  COMPILE_RECORDING: "compile_recording",
  REPLAY_WORKFLOW: "replay_workflow",
  REPLAY_FIRST_WORKFLOW: "replay_first_workflow",
  RUN_WORKFLOW: "run_workflow",
  GET_RUN_REPORT: "get_run_report",
  RETRY_RUN_PERSISTENCE: "retry_run_persistence",
  TEACH_FIX: "teach_fix",
  TEACH_BRANCH: "teach_branch",
  // simple operator surface: items, the Needs-you inbox, Results, the sample
  GET_TASK_INPUTS: "get_task_inputs",
  RUN_ITEM: "run_item",
  LIST_ATTENTION_ITEMS: "list_attention_items",
  RESOLVE_ATTENTION_ITEM: "resolve_attention_item",
  GET_ATTENTION_SCREENSHOT: "get_attention_screenshot",
  GET_RESULTS_SUMMARY: "get_results_summary",
  // A person approves each save. While a run waits before a save, these read
  // and answer it (they're served while the run is still going).
  GET_ITEM_APPROVAL: "get_item_approval",
  ANSWER_ITEM_APPROVAL: "answer_item_approval",
  // The approver key that signs those answers (Touch ID on a Mac). Served
  // while a run waits, so a person can set it up from the approval screen.
  // The stop-review queue: every stop as one card (stop_queue.py).
  LIST_STOP_CARDS: "list_stop_cards",
  ANSWER_STOP_CARDS: "answer_stop_cards",
  ANSWER_STOP_APPROVAL: "answer_stop_approval",
  GET_APPROVER_KEY: "get_approver_key",
  SET_UP_APPROVER_KEY: "set_up_approver_key",
  REMOVE_APPROVER_KEY: "remove_approver_key",
  // "Only ask a person about problems": read the switch and turn it off.
  // Turning it on happens in OpenAdapt Cloud, never here.
  GET_APPROVAL_MODE: "get_approval_mode",
  TURN_OFF_APPROVAL_MODE: "turn_off_approval_mode",
  RUN_SAMPLE: "run_sample",
  GET_QUALIFICATION: "get_qualification",
  EDIT_PROGRAM_SECTIONS: "edit_program_sections",
  // Check it pre-filled from the recording; answered by a person or their agent.
  GET_CHECK_SETUP: "get_check_setup",
  ANSWER_CHECK_SETUP: "answer_check_setup",
  INITIALIZE_QUALIFICATION: "initialize_qualification",
  PROPOSE_QUALIFICATION_FROM_DEMO: "propose_qualification_from_demo",
  SET_QUALIFICATION_RISK: "set_qualification_risk",
  ARM_QUALIFICATION_IDENTITY: "arm_qualification_identity",
  SET_QUALIFICATION_IDENTITY: "set_qualification_identity",
  BIND_QUALIFICATION_EFFECT: "bind_qualification_effect",
  SET_QUALIFICATION_EFFECT_VERIFICATION:
    "set_qualification_effect_verification",
  SET_QUALIFICATION_MINIMUM_EFFECT_TIER:
    "set_qualification_minimum_effect_tier",
  AUTHOR_QUALIFICATION_BUSINESS_DECISION:
    "author_qualification_business_decision",
  SET_QUALIFICATION_JUDGMENT_CASES: "set_qualification_judgment_cases",
  ADD_QUALIFICATION_CASE: "add_qualification_case",
  RUN_QUALIFICATION_CASE: "run_qualification_case",
  IMPORT_QUALIFICATION_RESULTS: "import_qualification_results",
  CERTIFY_QUALIFICATION: "certify_qualification",
  VERSION_QUALIFICATION_WORKFLOW: "version_qualification_workflow",
  SEAL_QUALIFICATION_WORKFLOW: "seal_qualification_workflow",
  EXPORT_QUALIFICATION_WORKFLOW: "export_qualification_workflow",
  DEPLOY_QUALIFICATION_WORKFLOW: "deploy_qualification_workflow",
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
  CLAIM_RUNNER_URI: "claim_runner_uri",
  AUTHORING_ALLOW: "authoring_allow",
  AUTHORING_DENY: "authoring_deny",
  AUTHORING_STATUS: "authoring_status",
  AUTHORING_PIN_TARGET: "authoring_pin_target",
  // config / settings (lane, phi_mode, hosted host)
  GET_CONFIG: "get_config",
  SET_CONFIG: "set_config",
  // governed policy (Tier-2/3) resolved by the cloud control plane and fetched +
  // cached fail-closed by the engine (openadapt_desktop/policy.py, docs/POLICY_SYNC.md).
  // The desktop consumes it read-only; the canonical write path is the cloud API.
  GET_EFFECTIVE_POLICY: "get_effective_policy",
  // OS permissions (screen recording / accessibility / input monitoring)
  CHECK_PERMISSIONS: "check_permissions",
  REQUEST_INPUT_MONITORING: "request_input_monitoring",
  // capability-aware surface availability (openadapt_desktop/capabilities.py)
  GET_CAPABILITIES: "get_capabilities",
  // review / egress gate (existing engine surface)
  SCRUB_CAPTURE: "scrub_capture",
  APPROVE_REVIEW: "approve_review",
  DISMISS_REVIEW: "dismiss_review",
  GET_PENDING_REVIEWS: "get_pending_reviews",
  // hosted runner (strict register / poll / callback transport)
  RUNNER_STATUS: "runner_status",
  RUNNER_ENABLE: "runner_enable",
  RUNNER_DISABLE: "runner_disable",
  // mobile attended-decision portal (openadapt_desktop/portal). Desktop owns lifecycle,
  // ingress posture, QR pairing, and generic notifications — never a decision.
  PORTAL_STATUS: "portal_status",
  PORTAL_START: "portal_start",
  PORTAL_STOP: "portal_stop",
  PORTAL_CREATE_PAIRING: "portal_create_pairing",
  PORTAL_PAIRING_STATUS: "portal_pairing_status",
  PORTAL_APPROVE_PAIRING: "portal_approve_pairing",
  PORTAL_CANCEL_PAIRING: "portal_cancel_pairing",
  PORTAL_DEVICES: "portal_devices",
  PORTAL_REVOKE_DEVICE: "portal_revoke_device",
  PORTAL_NOTIFICATION: "portal_notification",
  // Local demonstration coach. Payload is never an evidence frame.
  SET_COACH: "set_coach",
  GET_COACH: "get_coach",
  CLEAR_COACH: "clear_coach",
  // Shadow mode: off by default, local only. Turning it on needs the notice
  // hash and acknowledged=true. Turning it off deletes everything it kept.
  SHADOW_STATUS: "shadow_status",
  SHADOW_NOTICE: "shadow_notice",
  SHADOW_ENABLE: "shadow_enable",
  SHADOW_TURN_OFF: "shadow_turn_off",
  SHADOW_PAUSE: "shadow_pause",
  SHADOW_UNPAUSE: "shadow_unpause",
  SHADOW_PURGE_AND_DISABLE: "shadow_purge_and_disable",
  SHADOW_SET_WORK_DOMAINS: "shadow_set_work_domains",
  SHADOW_DECIDE: "shadow_decide",
  SHADOW_SCAN: "shadow_scan",
} as const;

/** Events the engine emits (delivered as Tauri events `engine://<name>`). */
export const EVT = {
  RECORDING_STARTED: "recording_started",
  RECORDING_STOPPED: "recording_stopped",
  RECORDING_ERROR: "recording_error",
  STATUS_UPDATE: "status_update",
  COMPILE_PROGRESS: "compile_progress",
  REPLAY_PROGRESS: "replay_progress",
  BROWSER_RUNTIME: "browser_runtime",
  LOG_LINE: "log_line",
  SYNC_STATE: "sync_state",
  BREAK_COUNT: "break_count",
  SIDECAR_STATE: "sidecar_state",
  PAIRING_STATE: "pairing_state",
  AUTHORING_STATE: "authoring_state",
  RUNNER_STATE: "runner_state",
  PORTAL_STATE: "portal_state",
  // Carries only {title, body, open_count, route}; see attentionNotification.ts.
  ATTENTION_NOTIFICATION: "attention_notification",
  // Local-only coach channel. Never persist this payload into evidence.
  COACH: "coach",
  // {state: "browser" | "running" | "done" | "failed", detail}
  SAMPLE_PROGRESS: "sample_progress",
  // {workflow_id, state: "watching" | "closed"}: a run that may stop before a
  // save for a person's approval started or ended. Never carries a value.
  ITEM_APPROVAL: "item_approval",
  // {workflow_id, question, answered_by}: a Check it question was answered,
  // in this window or by the person's agent over the local socket.
  CHECK_SETUP: "check_setup",
  // Shadow mode status (see src/lib/shadowMode.ts). Never carries an event.
  SHADOW_STATE: "shadow_state",
  // {motif_id, count, title, body, actions}: generic text only.
  SHADOW_PROMPT: "shadow_prompt",
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

/**
 * Did this build register the updater plugin? Only a build with an update
 * feed does. False outside Tauri, and false if the shell can't say.
 */
export async function updaterAvailable(): Promise<boolean> {
  if (!inTauri()) return false;
  try {
    return (await invoke<boolean>("updater_available")) === true;
  } catch {
    return false;
  }
}

/** Show/hide the always-on-top control overlay without requesting focus. */
export async function setControlOverlayVisible(visible: boolean): Promise<void> {
  if (!inTauri()) return;
  await invoke("set_control_overlay_visible", { visible });
}

/**
 * Apply OS-level click-through and focus behavior for the overlay.
 * False is required throughout observation and actuation.
 */
export async function setControlOverlayInteractive(
  interactive: boolean,
): Promise<void> {
  if (!inTauri()) return;
  await invoke("set_control_overlay_interactive", { interactive });
}

/** Enforce capture exclusion before the native overlay is shown. */
export async function ensureControlOverlayCaptureExcluded(): Promise<void> {
  if (!inTauri()) return;
  await invoke("ensure_control_overlay_capture_excluded");
}

/** Compact card, taller pause card, or full-monitor click-through stage. */
export async function setControlOverlayLayout(
  layout: "compact" | "paused" | "stage",
): Promise<void> {
  if (!inTauri()) return;
  await invoke("set_control_overlay_layout", { layout });
}

/** Publish the post-composition preference; raw capture stays excluded. */
const FFMPEG_BROWSER_FALLBACK: FfmpegRuntimeStatus = {
  phase: "ready",
  source: "managed",
  runtime_version: "browser-preview",
  target: "browser-preview",
};

/** Status of the separately provisioned local video runtime. */
export async function ffmpegRuntimeStatus(): Promise<FfmpegRuntimeStatus> {
  if (!inTauri()) return FFMPEG_BROWSER_FALLBACK;
  try {
    return await invoke<FfmpegRuntimeStatus>("ffmpeg_status");
  } catch (error) {
    return {
      phase: "error",
      source: "managed",
      runtime_version: "unknown",
      target: "unknown",
      detail: String(error),
    };
  }
}

/** Retry automatic first-use runtime provisioning. */
export async function retryFfmpegRuntime(): Promise<FfmpegRuntimeStatus> {
  if (!inTauri()) return FFMPEG_BROWSER_FALLBACK;
  return invoke<FfmpegRuntimeStatus>("retry_ffmpeg_provisioning");
}

/** Subscribe to native runtime provisioning progress. */
export function onFfmpegRuntimeStatus(
  handler: (payload: FfmpegRuntimeStatus) => void,
): Promise<UnlistenFn> {
  if (!inTauri()) return Promise.resolve(() => {});
  return listen<FfmpegRuntimeStatus>("runtime://ffmpeg-status", (event) =>
    handler(event.payload),
  );
}

/** Open a URL in the system browser (login deep-link, cloud dashboard, panes). */
export async function openExternal(url: string): Promise<void> {
  if (!inTauri()) {
    window.open(url, "_blank");
    return;
  }
  await invoke("open_external", { url });
}

/**
 * Show whether shadow mode is on in the menu-bar tray. A no-op outside Tauri
 * or in a shell without the command.
 */
/** Update the tray's shadow line. `active` is null until the engine answers. */
export async function setShadowTrayStatus(
  active: boolean | null,
  paused = false,
): Promise<void> {
  if (!inTauri()) return;
  try {
    await invoke("set_shadow_tray_status", { active, paused });
  } catch {
    // An older shell has no shadow tray line. The window still shows it.
  }
}

/** The tray's "Shadow mode settings…" item. Returns an unlisten fn. */
export function onShadowSettingsRequested(handler: () => void): Promise<UnlistenFn> {
  if (!inTauri()) return Promise.resolve(() => {});
  return listen("tray://open-shadow-settings", () => handler());
}

/** The tray's "Pause shadow mode" / "Resume shadow mode" item. */
export function onShadowPauseToggleRequested(handler: () => void): Promise<UnlistenFn> {
  if (!inTauri()) return Promise.resolve(() => {});
  return listen("tray://toggle-shadow-pause", () => handler());
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
