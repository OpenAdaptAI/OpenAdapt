// IPC commands for Tauri <-> frontend communication.
//
// These commands are invoked from the WebView frontend via
// `window.__TAURI__.invoke('command_name', { params })`.
//
// Each command delegates to the Python sidecar via JSON-over-stdin/stdout IPC.
// See Appendix B of DESIGN.md for the full IPC protocol specification.

use serde::{Deserialize, Serialize};

/// Status response returned by most commands.
#[derive(Debug, Serialize, Deserialize)]
pub struct StatusResponse {
    pub recording: bool,
    pub paused: bool,
    pub duration_secs: Option<f64>,
    pub capture_id: Option<String>,
}

/// Capture metadata for the captures list.
#[derive(Debug, Serialize, Deserialize)]
pub struct CaptureInfo {
    pub id: String,
    pub started_at: String,
    pub duration_secs: f64,
    pub size_bytes: u64,
    pub review_status: String,
}

/// Storage usage information.
#[derive(Debug, Serialize, Deserialize)]
pub struct StorageUsage {
    pub used_bytes: u64,
    pub max_bytes: u64,
    pub capture_count: u32,
}

/// Start a new recording session.
#[tauri::command]
pub async fn start_recording(quality: Option<String>) -> Result<StatusResponse, String> {
    // TODO: Send start_recording command to Python sidecar
    let _ = quality;
    Err("Not implemented".into())
}

/// Stop the current recording session.
#[tauri::command]
pub async fn stop_recording() -> Result<StatusResponse, String> {
    // TODO: Send stop_recording command to Python sidecar
    Err("Not implemented".into())
}

/// Pause the current recording session.
#[tauri::command]
pub async fn pause_recording() -> Result<StatusResponse, String> {
    // TODO: Send pause_recording command to Python sidecar
    Err("Not implemented".into())
}

/// Get the current recording status.
#[tauri::command]
pub async fn get_status() -> Result<StatusResponse, String> {
    // TODO: Send get_status command to Python sidecar
    Err("Not implemented".into())
}

/// Get a list of recent captures.
#[tauri::command]
pub async fn get_captures(limit: Option<u32>) -> Result<Vec<CaptureInfo>, String> {
    // TODO: Send get_captures command to Python sidecar
    let _ = limit;
    Err("Not implemented".into())
}

/// Get current storage usage information.
#[tauri::command]
pub async fn get_storage_usage() -> Result<StorageUsage, String> {
    // TODO: Send get_storage_usage command to Python sidecar
    Err("Not implemented".into())
}

/// Update a configuration setting.
#[tauri::command]
pub async fn set_config(key: String, value: String) -> Result<(), String> {
    // TODO: Send set_config command to Python sidecar
    let _ = (key, value);
    Err("Not implemented".into())
}

/// Upload a capture to a configured storage backend.
///
/// The capture must be in `reviewed` or `dismissed` state (cleared for egress).
/// See Section 5 of DESIGN.md for the review state machine.
#[tauri::command]
pub async fn upload_capture(capture_id: String, backend: String) -> Result<(), String> {
    // TODO: Send upload_capture command to Python sidecar
    // TODO: Verify capture is cleared for egress before sending
    let _ = (capture_id, backend);
    Err("Not implemented".into())
}

/// Delete a capture from local storage.
#[tauri::command]
pub async fn delete_capture(capture_id: String) -> Result<(), String> {
    // TODO: Send delete_capture command to Python sidecar
    let _ = capture_id;
    Err("Not implemented".into())
}

/// Run PII scrubbing on a capture.
///
/// Creates a parallel `.scrubbed/` directory with redacted copies.
/// See Section 5.4 of DESIGN.md for scrubbed copy format.
#[tauri::command]
pub async fn scrub_capture(capture_id: String, level: Option<String>) -> Result<(), String> {
    // TODO: Send scrub_capture command to Python sidecar
    let _ = (capture_id, level);
    Err("Not implemented".into())
}

/// Approve a scrubbed capture for egress (state: scrubbed -> reviewed).
#[tauri::command]
pub async fn approve_review(capture_id: String) -> Result<(), String> {
    // TODO: Send approve_review command to Python sidecar
    let _ = capture_id;
    Err("Not implemented".into())
}

/// Dismiss a capture without scrubbing (state: captured -> dismissed).
///
/// This allows raw data to be uploaded. The user must acknowledge the
/// PII warning before this is allowed.
#[tauri::command]
pub async fn dismiss_review(capture_id: String) -> Result<(), String> {
    // TODO: Send dismiss_review command to Python sidecar
    let _ = capture_id;
    Err("Not implemented".into())
}

/// Get a list of captures pending review.
#[tauri::command]
pub async fn get_pending_reviews() -> Result<Vec<CaptureInfo>, String> {
    // TODO: Send get_pending_reviews command to Python sidecar
    Err("Not implemented".into())
}
