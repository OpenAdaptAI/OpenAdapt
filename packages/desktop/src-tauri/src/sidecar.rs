// Python sidecar process management.
//
// The Python engine is packaged into a standalone executable using PyInstaller
// and bundled as a Tauri sidecar. Communication uses JSON messages over
// stdin/stdout (one JSON object per line).
//
// See Appendix B of DESIGN.md for the full IPC protocol specification.
//
// Lifecycle:
//   1. Tauri spawns the sidecar on app startup
//   2. Commands are sent as JSON lines to the sidecar's stdin
//   3. Responses and events are read from the sidecar's stdout
//   4. The sidecar is gracefully terminated on app quit
//   5. A watchdog monitors the sidecar and restarts it on crash

use serde::{Deserialize, Serialize};

/// A command message sent from Tauri to the Python sidecar.
#[derive(Debug, Serialize)]
pub struct SidecarCommand {
    pub id: String,
    pub cmd: String,
    pub params: serde_json::Value,
}

/// A response message received from the Python sidecar.
#[derive(Debug, Deserialize)]
pub struct SidecarResponse {
    pub id: Option<String>,
    pub status: Option<String>,
    pub data: Option<serde_json::Value>,
    pub error: Option<String>,
    pub event: Option<String>,
}

/// Spawn the Python sidecar process.
///
/// The sidecar binary name is `openadapt-engine` and is located in the
/// Tauri sidecar directory alongside the main application binary.
pub fn spawn(_app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    // TODO: Use tauri_plugin_shell to spawn the sidecar
    // TODO: Set up stdin/stdout communication channels
    // TODO: Start a background thread to read sidecar stdout for events
    // TODO: Start a watchdog thread to monitor sidecar health
    Ok(())
}

/// Send a command to the Python sidecar and await the response.
pub async fn send_command(_cmd: SidecarCommand) -> Result<SidecarResponse, String> {
    // TODO: Serialize command as JSON line, write to sidecar stdin
    // TODO: Wait for response with matching id from sidecar stdout
    // TODO: Handle timeout (default 30s, configurable per command)
    Err("Sidecar not running".into())
}

/// Gracefully shut down the Python sidecar.
pub fn shutdown() -> Result<(), Box<dyn std::error::Error>> {
    // TODO: Send shutdown command to sidecar
    // TODO: Wait for graceful exit (5s timeout)
    // TODO: Force kill if timeout exceeded
    Ok(())
}
