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
use tauri::{
    AppHandle, LogicalSize, Manager, PhysicalPosition, PhysicalSize, Position, Size, State,
};
use url::Url;

use crate::sidecar::SidecarHandle;

const OVERLAY_COMPACT_WIDTH: f64 = 440.0;
const OVERLAY_COMPACT_HEIGHT: f64 = 104.0;
const OVERLAY_PAUSED_WIDTH: f64 = 560.0;
const OVERLAY_PAUSED_HEIGHT: f64 = 168.0;

pub(crate) fn place_control_overlay(app: &AppHandle) {
    let Some(main) = app.get_webview_window("main") else {
        return;
    };
    let Some(overlay) = app.get_webview_window("control-overlay") else {
        return;
    };
    let monitor = main
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| app.primary_monitor().ok().flatten());
    let (Some(monitor), Ok(overlay_size)) = (monitor, overlay.outer_size()) else {
        return;
    };
    let monitor_position = monitor.position();
    let monitor_size = monitor.size();
    let margin = (24.0 * monitor.scale_factor()).round() as i32;
    let x = monitor_position.x + margin;
    let y = monitor_position.y + monitor_size.height as i32 - overlay_size.height as i32 - margin;
    let _ = overlay.set_position(Position::Physical(PhysicalPosition::new(x, y)));
}

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

/// Whether this build registered the updater plugin. `main.rs` registers it
/// only when the build carries an update feed (`plugins.updater`), and no build
/// does until signed releases ship. Settings shows its update check only when
/// this is true, because without the plugin the check can only fail.
pub struct UpdaterAvailable(pub bool);

#[tauri::command]
pub fn updater_available(state: State<'_, UpdaterAvailable>) -> bool {
    state.0
}

/// Show or hide the compact control overlay without focusing it.
///
/// The overlay is created once at startup as an always-on-top window with
/// `focus: false`. State changes only call show/hide; they never activate the
/// window or move keyboard focus away from the application being automated.
#[tauri::command]
pub fn set_control_overlay_visible(app: AppHandle, visible: bool) -> Result<(), String> {
    let window = app
        .get_webview_window("control-overlay")
        .ok_or_else(|| "control overlay window is unavailable".to_string())?;
    if visible {
        window.show()
    } else {
        window.hide()
    }
    .map_err(|error| format!("could not update control overlay visibility: {error}"))
}

/// Apply the overlay's OS-level pointer/focus policy.
///
/// Active observation and actuation states must be click-through and
/// non-focusable. The window is hidden before entering that mode, so a partial
/// platform failure cannot leave a visible input-intercepting overlay. The
/// frontend shows it again only after this command acknowledges the policy.
#[tauri::command]
pub fn set_control_overlay_interactive(app: AppHandle, interactive: bool) -> Result<(), String> {
    let window = app
        .get_webview_window("control-overlay")
        .ok_or_else(|| "control overlay window is unavailable".to_string())?;

    if !interactive {
        window
            .hide()
            .map_err(|error| format!("could not hide control overlay safely: {error}"))?;
        window
            .set_ignore_cursor_events(true)
            .map_err(|error| format!("could not make control overlay click-through: {error}"))?;
        window
            .set_focusable(false)
            .map_err(|error| format!("could not make control overlay non-focusable: {error}"))?;
    } else {
        window
            .set_focusable(true)
            .map_err(|error| format!("could not enable control overlay focus: {error}"))?;
        window
            .set_ignore_cursor_events(false)
            .map_err(|error| format!("could not enable control overlay input: {error}"))?;
    }
    Ok(())
}

/// Enforce OS-level capture exclusion before the overlay becomes visible.
#[tauri::command]
pub fn ensure_control_overlay_capture_excluded(app: AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("control-overlay")
        .ok_or_else(|| "control overlay window is unavailable".to_string())?;
    window
        .set_content_protected(true)
        .map_err(|error| format!("this platform could not exclude the overlay: {error}"))
}

/// Resize the overlay between a compact card, a pause card, and a click-through
/// monitor stage used only to paint a bound target ring. Capture exclusion is
/// never disabled here.
#[tauri::command]
pub fn set_control_overlay_layout(app: AppHandle, layout: String) -> Result<(), String> {
    let window = app
        .get_webview_window("control-overlay")
        .ok_or_else(|| "control overlay window is unavailable".to_string())?;
    match layout.as_str() {
        "stage" => {
            let Some(main) = app.get_webview_window("main") else {
                return Err("main window is unavailable".to_string());
            };
            let monitor = main
                .current_monitor()
                .ok()
                .flatten()
                .or_else(|| app.primary_monitor().ok().flatten())
                .ok_or_else(|| "no monitor is available for the overlay stage".to_string())?;
            let size = monitor.size();
            let position = monitor.position();
            window
                .set_size(Size::Physical(PhysicalSize::new(size.width, size.height)))
                .map_err(|error| format!("could not stage the control overlay: {error}"))?;
            window
                .set_position(Position::Physical(PhysicalPosition::new(
                    position.x, position.y,
                )))
                .map_err(|error| format!("could not place the overlay stage: {error}"))?;
        }
        "paused" => {
            window
                .set_size(Size::Logical(LogicalSize::new(
                    OVERLAY_PAUSED_WIDTH,
                    OVERLAY_PAUSED_HEIGHT,
                )))
                .map_err(|error| format!("could not size the paused overlay: {error}"))?;
            place_control_overlay(&app);
        }
        _ => {
            window
                .set_size(Size::Logical(LogicalSize::new(
                    OVERLAY_COMPACT_WIDTH,
                    OVERLAY_COMPACT_HEIGHT,
                )))
                .map_err(|error| format!("could not size the compact overlay: {error}"))?;
            place_control_overlay(&app);
        }
    }
    Ok(())
}

/// Open a URL in the user's default system browser.
///
/// Used for the login deep-link ("open Settings -> Ingest tokens"), "Open cloud
/// dashboard", and the OS System Settings permission panes. Implemented with the
/// platform opener so no extra plugin capability is required.
fn validated_external_url(value: &str) -> Result<String, String> {
    let parsed = Url::parse(value).map_err(|_| "refusing to open an invalid URL".to_string())?;
    let allowed_scheme = matches!(parsed.scheme(), "https" | "http" | "openadapt")
        || cfg!(target_os = "macos") && parsed.scheme() == "x-apple.systempreferences";
    if !allowed_scheme {
        return Err("refusing to open a URL with an unsupported scheme".to_string());
    }
    if matches!(parsed.scheme(), "https" | "http")
        && (parsed.host_str().is_none()
            || !parsed.username().is_empty()
            || parsed.password().is_some())
    {
        return Err("refusing to open a web URL with invalid authority".to_string());
    }
    if value.chars().any(char::is_control) {
        return Err("refusing to open a URL with control characters".to_string());
    }
    Ok(parsed.to_string())
}

#[tauri::command]
pub fn open_external(url: String) -> Result<(), String> {
    open_external_with(&url, |safe_url| {
        // The Rust API does not enforce plugin scopes: our validation is
        // mandatory. No general opener capability is exposed to the webview.
        tauri_plugin_opener::open_url(safe_url, None::<&str>)
    })
}

fn open_external_with<E>(
    url: &str,
    launch: impl FnOnce(String) -> Result<(), E>,
) -> Result<(), String> {
    let safe_url = validated_external_url(url)?;
    launch(safe_url).map_err(|_| "failed to open URL".to_string())
}

#[cfg(test)]
mod external_url_tests {
    use super::{open_external_with, validated_external_url};

    #[test]
    fn shared_url_model_dispatches_each_accepted_native_case_once() {
        let model: serde_json::Value =
            serde_json::from_str(include_str!("../../tests/fixtures/system-url-launch.json"))
                .unwrap();
        for case in model["cases"].as_array().unwrap() {
            let url = case["url"].as_str().unwrap();
            let allowed = case["native"].as_bool().unwrap_or_else(|| {
                assert_eq!(case["native"].as_str(), Some("macos"));
                cfg!(target_os = "macos")
            });
            let mut calls = 0;
            let result = open_external_with(url, |value| {
                calls += 1;
                assert_eq!(value, url);
                Ok::<(), String>(())
            });
            assert_eq!(result.is_ok(), allowed, "{url:?}");
            assert_eq!(calls, usize::from(allowed), "{url:?}");
        }
    }

    #[test]
    fn only_validated_urls_reach_the_default_handler() {
        for value in [
            "https://example.test/",
            "http://localhost/",
            "openadapt://pair",
        ] {
            open_external_with(value, |url| {
                assert_eq!(url, value);
                Ok::<(), String>(())
            })
            .unwrap();
        }
        for value in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://u:p@example.test/",
            "https://example.test/\n",
        ] {
            assert!(open_external_with(value, |_| -> Result<(), String> {
                panic!("invalid URL reached the default handler")
            })
            .is_err());
        }
    }

    #[test]
    fn platform_settings_scheme_keeps_its_existing_platform_boundary() {
        let value = "x-apple.systempreferences:com.apple.preference.security";
        assert_eq!(
            validated_external_url(value).is_ok(),
            cfg!(target_os = "macos")
        );
    }

    #[test]
    fn launcher_error_does_not_disclose_url_contents() {
        assert_eq!(
            open_external_with("https://example.test/?state=synthetic", Err::<(), _>).unwrap_err(),
            "failed to open URL"
        );
    }

    #[test]
    fn accepts_clean_web_url() {
        assert_eq!(
            validated_external_url("https://app.openadapt.ai/dashboard").unwrap(),
            "https://app.openadapt.ai/dashboard"
        );
    }

    #[test]
    fn rejects_shell_and_authority_tricks() {
        for value in [
            "javascript:alert(1)",
            "https://user:secret@example.com/",
            "https://example.com/\nnext",
            "https://example.com\" & calc.exe",
            "https://example.com|calc.exe",
            "https://example.com^calc.exe",
        ] {
            assert!(validated_external_url(value).is_err(), "accepted {value}");
        }
    }
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
