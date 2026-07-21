// Python sidecar process management.
//
// The Python engine is packaged into a standalone executable using PyInstaller
// and bundled as a Tauri sidecar (declared in `tauri.conf.json`
// `bundle.externalBin` as `binaries/openadapt-engine`). Communication uses JSON
// messages over stdin/stdout, one JSON object per line (see DESIGN.md Appendix B
// and the desktop-tray architecture spec §3d/§4b):
//
//   Command  (shell -> engine):  {"id": "req-1", "cmd": "get_status", "params": {}}
//   Response (engine -> shell):  {"id": "req-1", "status": "ok"|"error", "data"|"error": ...}
//   Event    (engine -> shell):  {"event": "recording_started", "data": {...}}
//
// Lifecycle:
//   1. Tauri spawns the sidecar on app startup (guarded: a missing frozen
//      binary must NOT crash the shell — the UI still renders "engine offline").
//   2. Commands are written as JSON lines to the sidecar's stdin.
//   3. A background task reads the sidecar's stdout: responses are routed back to
//      the awaiting command by `id`; events are re-emitted to the WebView as
//      `engine://<event>`.
//   4. A watchdog restarts the sidecar a bounded number of times on crash.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// The frozen sidecar binary base name (see `tauri.conf.json` externalBin).
const SIDECAR_NAME: &str = "openadapt-engine";
/// Fast IPC queries should fail quickly; workflow operations may include a
/// first-use browser download and legitimate long-running local execution.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30);
const WORKFLOW_COMMAND_TIMEOUT: Duration = Duration::from_secs(15 * 60);
/// Max automatic respawns before giving up (avoids a crash loop).
const MAX_RESTARTS: u32 = 3;

/// A response or event line received from the Python sidecar.
#[derive(Debug, Clone, Deserialize)]
pub struct SidecarResponse {
    pub id: Option<String>,
    pub status: Option<String>,
    pub data: Option<Value>,
    pub error: Option<String>,
    pub event: Option<String>,
}

/// A command message serialized to the sidecar's stdin.
#[derive(Debug, Serialize)]
struct SidecarCommand<'a> {
    id: String,
    cmd: &'a str,
    params: Value,
}

/// Shared sidecar state, held behind an `Arc` in Tauri managed state.
#[derive(Default)]
pub struct SidecarInner {
    child: Mutex<Option<CommandChild>>,
    pending: Mutex<HashMap<String, oneshot::Sender<SidecarResponse>>>,
    running: AtomicBool,
    seq: AtomicU64,
}

/// Managed-state wrapper so Tauri commands can reach the sidecar.
pub struct SidecarHandle(pub Arc<SidecarInner>);

impl SidecarInner {
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    fn next_id(&self) -> String {
        let n = self.seq.fetch_add(1, Ordering::SeqCst);
        format!("req-{n}")
    }

    /// Send a command to the sidecar and await its correlated response.
    ///
    /// Returns the `data` payload on success, or a human-readable error string.
    /// If the sidecar is not running (e.g. the frozen binary is absent during
    /// development), this returns an `engine offline` error rather than panicking
    /// so the frontend can degrade gracefully.
    pub async fn send_command(&self, cmd: &str, params: Value) -> Result<Value, String> {
        if !self.is_running() {
            return Err("engine offline: sidecar not running".into());
        }

        let id = self.next_id();
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id.clone(), tx);

        let payload = SidecarCommand {
            id: id.clone(),
            cmd,
            params,
        };
        let mut line = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
        line.push('\n');

        {
            let mut guard = self.child.lock().unwrap();
            match guard.as_mut() {
                Some(child) => {
                    if let Err(e) = child.write(line.as_bytes()) {
                        self.pending.lock().unwrap().remove(&id);
                        return Err(format!("failed to write to sidecar: {e}"));
                    }
                }
                None => {
                    self.pending.lock().unwrap().remove(&id);
                    return Err("engine offline: sidecar not running".into());
                }
            }
        }

        let timeout = match cmd {
            "compile_recording" | "replay_workflow" | "run_workflow" | "teach_fix" => {
                WORKFLOW_COMMAND_TIMEOUT
            }
            _ => COMMAND_TIMEOUT,
        };
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(resp)) => match resp.status.as_deref() {
                Some("ok") => Ok(resp.data.unwrap_or(Value::Null)),
                _ => Err(resp.error.unwrap_or_else(|| "unknown engine error".into())),
            },
            Ok(Err(_)) => Err("sidecar dropped the response channel".into()),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(format!("engine command '{cmd}' timed out"))
            }
        }
    }
}

/// Spawn the Python sidecar and wire up the stdout reader + watchdog.
///
/// Guarded: if the frozen binary cannot be resolved or spawned, we log and
/// return — the shell keeps running so the app builds and renders without a
/// sidecar present (common during frontend development).
pub fn spawn(app: &AppHandle, inner: Arc<SidecarInner>) {
    spawn_with_attempt(app.clone(), inner, 0);
}

fn spawn_with_attempt(app: AppHandle, inner: Arc<SidecarInner>, attempt: u32) {
    let command = match app.shell().sidecar(SIDECAR_NAME) {
        Ok(cmd) => cmd,
        Err(e) => {
            eprintln!("[sidecar] '{SIDECAR_NAME}' not available (frontend-only mode): {e}");
            let _ = app.emit("engine://sidecar_state", json!({ "running": false }));
            return;
        }
    };

    let (mut rx, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("[sidecar] failed to spawn '{SIDECAR_NAME}': {e}");
            let _ = app.emit("engine://sidecar_state", json!({ "running": false }));
            return;
        }
    };

    *inner.child.lock().unwrap() = Some(child);
    inner.running.store(true, Ordering::SeqCst);
    let _ = app.emit("engine://sidecar_state", json!({ "running": true }));

    let reader_inner = inner.clone();
    let reader_app = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    if let Ok(text) = String::from_utf8(bytes) {
                        for raw in text.split('\n') {
                            let line = raw.trim();
                            if line.is_empty() {
                                continue;
                            }
                            route_line(&reader_app, &reader_inner, line);
                        }
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    if let Ok(text) = String::from_utf8(bytes) {
                        let line = text.trim();
                        if !line.is_empty() {
                            eprintln!("[sidecar:stderr] {line}");
                        }
                    }
                }
                CommandEvent::Error(err) => {
                    eprintln!("[sidecar] stream error: {err}");
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[sidecar] terminated: {payload:?}");
                    break;
                }
                _ => {}
            }
        }

        // Stream ended: mark stopped, fail any in-flight requests, maybe respawn.
        reader_inner.running.store(false, Ordering::SeqCst);
        *reader_inner.child.lock().unwrap() = None;
        {
            let mut pending = reader_inner.pending.lock().unwrap();
            pending.clear();
        }
        let _ = reader_app.emit("engine://sidecar_state", json!({ "running": false }));

        if attempt + 1 < MAX_RESTARTS {
            eprintln!("[sidecar] restarting (attempt {})", attempt + 2);
            tokio::time::sleep(Duration::from_secs(1)).await;
            spawn_with_attempt(reader_app, reader_inner, attempt + 1);
        } else {
            eprintln!("[sidecar] giving up after {MAX_RESTARTS} restarts");
        }
    });
}

/// Route a single JSON line: correlate responses by id, re-emit events.
fn route_line(app: &AppHandle, inner: &Arc<SidecarInner>, line: &str) {
    let resp: SidecarResponse = match serde_json::from_str(line) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[sidecar] invalid JSON line ({e}): {line}");
            return;
        }
    };

    if let Some(event) = resp.event.as_deref() {
        // Forward engine events to the WebView as `engine://<event>`.
        let _ = app.emit(&format!("engine://{event}"), resp.data.clone());
        return;
    }

    if let Some(id) = resp.id.as_deref() {
        if let Some(tx) = inner.pending.lock().unwrap().remove(id) {
            let _ = tx.send(resp);
        }
    }
}

/// Gracefully shut down the Python sidecar (best-effort on app quit).
pub fn shutdown(inner: &Arc<SidecarInner>) {
    inner.running.store(false, Ordering::SeqCst);
    if let Some(mut child) = inner.child.lock().unwrap().take() {
        // Ask the engine to exit cleanly, then drop the child (kills on drop).
        let _ = child.write(b"{\"id\":\"shutdown\",\"cmd\":\"shutdown\",\"params\":{}}\n");
        let _ = child.kill();
    }
}
