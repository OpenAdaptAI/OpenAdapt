// IPC commands for Tauri <-> frontend communication.
//
// These commands are invoked from the WebView frontend via
// `import { invoke } from '@tauri-apps/api/core'`.
//
// Every engine-backed command forwards to the Python sidecar over the
// JSON-lines protocol (see `sidecar.rs` and DESIGN.md Appendix B). The frontend
// mostly uses the generic `engine_invoke(cmd, params)` bridge (so new engine
// verbs need no Rust change), plus the typed convenience commands below and a
// couple of native helpers (`open_external`, `sidecar_status`).
//
// Command / event names are catalogued in `src/lib/engine.ts` (`CMD` / `EVT`)
// so the engine (W1) and tray (W3) agents can align on the same wire.

use serde_json::{json, Value};
use tauri::State;

use crate::sidecar::SidecarHandle;

/// Generic bridge: forward an arbitrary engine command + params to the sidecar.
///
/// This is the primary path the frontend uses. It keeps the Rust shell agnostic
/// to the evolving engine verb set (login/push/compile/replay/teach/…).
#[tauri::command]
pub async fn engine_invoke(
    state: State<'_, SidecarHandle>,
    cmd: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let params = params.unwrap_or_else(|| json!({}));
    state.0.send_command(&cmd, params).await
}

/// Whether the engine sidecar is currently running (frontend degrades if not).
#[tauri::command]
pub fn sidecar_status(state: State<'_, SidecarHandle>) -> bool {
    state.0.is_running()
}

/// Open a URL in the user's default system browser.
///
/// Used for the login deep-link ("open Settings -> Ingest tokens"), "Open cloud
/// dashboard", and the OS System Settings permission panes. Implemented with the
/// platform opener so no extra plugin capability is required.
#[tauri::command]
pub fn open_external(url: String) -> Result<(), String> {
    // Only allow http(s), the app's custom scheme, and macOS System Settings deep
    // links — never arbitrary shell strings.
    let allowed = url.starts_with("https://")
        || url.starts_with("http://")
        || url.starts_with("openadapt://")
        || url.starts_with("x-apple.systempreferences:");
    if !allowed {
        return Err(format!("refusing to open non-web URL: {url}"));
    }

    #[cfg(target_os = "macos")]
    let result = std::process::Command::new("open").arg(&url).spawn();
    #[cfg(target_os = "windows")]
    let result = std::process::Command::new("cmd")
        .args(["/C", "start", "", &url])
        .spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let result = std::process::Command::new("xdg-open").arg(&url).spawn();

    result
        .map(|_| ())
        .map_err(|e| format!("failed to open URL: {e}"))
}

// --------------------------------------------------------------------------
// Typed convenience commands (forward to the sidecar).
// These mirror DESIGN.md Appendix B and keep the originally-declared surface.
// --------------------------------------------------------------------------

async fn forward(
    state: &State<'_, SidecarHandle>,
    cmd: &str,
    params: Value,
) -> Result<Value, String> {
    state.0.send_command(cmd, params).await
}

#[tauri::command]
pub async fn start_recording(
    state: State<'_, SidecarHandle>,
    quality: Option<String>,
) -> Result<Value, String> {
    forward(&state, "start_recording", json!({ "quality": quality })).await
}

#[tauri::command]
pub async fn stop_recording(state: State<'_, SidecarHandle>) -> Result<Value, String> {
    forward(&state, "stop_recording", json!({})).await
}

#[tauri::command]
pub async fn pause_recording(state: State<'_, SidecarHandle>) -> Result<Value, String> {
    forward(&state, "pause_recording", json!({})).await
}

#[tauri::command]
pub async fn get_status(state: State<'_, SidecarHandle>) -> Result<Value, String> {
    forward(&state, "get_status", json!({})).await
}

#[tauri::command]
pub async fn get_captures(
    state: State<'_, SidecarHandle>,
    limit: Option<u32>,
) -> Result<Value, String> {
    forward(&state, "get_captures", json!({ "limit": limit })).await
}

#[tauri::command]
pub async fn get_storage_usage(state: State<'_, SidecarHandle>) -> Result<Value, String> {
    forward(&state, "get_storage_usage", json!({})).await
}

#[tauri::command]
pub async fn set_config(
    state: State<'_, SidecarHandle>,
    key: String,
    value: Value,
) -> Result<Value, String> {
    forward(&state, "set_config", json!({ "key": key, "value": value })).await
}

#[tauri::command]
pub async fn upload_capture(
    state: State<'_, SidecarHandle>,
    capture_id: String,
    backend: String,
) -> Result<Value, String> {
    forward(
        &state,
        "upload_capture",
        json!({ "capture_id": capture_id, "backend": backend }),
    )
    .await
}

#[tauri::command]
pub async fn delete_capture(
    state: State<'_, SidecarHandle>,
    capture_id: String,
) -> Result<Value, String> {
    forward(
        &state,
        "delete_capture",
        json!({ "capture_id": capture_id }),
    )
    .await
}

#[tauri::command]
pub async fn scrub_capture(
    state: State<'_, SidecarHandle>,
    capture_id: String,
    level: Option<String>,
) -> Result<Value, String> {
    forward(
        &state,
        "scrub_capture",
        json!({ "capture_id": capture_id, "level": level }),
    )
    .await
}

#[tauri::command]
pub async fn approve_review(
    state: State<'_, SidecarHandle>,
    capture_id: String,
) -> Result<Value, String> {
    forward(
        &state,
        "approve_review",
        json!({ "capture_id": capture_id }),
    )
    .await
}

#[tauri::command]
pub async fn dismiss_review(
    state: State<'_, SidecarHandle>,
    capture_id: String,
) -> Result<Value, String> {
    forward(
        &state,
        "dismiss_review",
        json!({ "capture_id": capture_id }),
    )
    .await
}

#[tauri::command]
pub async fn get_pending_reviews(state: State<'_, SidecarHandle>) -> Result<Value, String> {
    forward(&state, "get_pending_reviews", json!({})).await
}
