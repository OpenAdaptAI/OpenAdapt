// OpenAdapt Desktop - Tauri entry point
//
// This is the main entry point for the Tauri application shell.
// It initializes the (minimal) system tray, registers IPC commands, spawns the
// Python sidecar for the recording/compile/replay engine, and exposes the
// generic `engine_invoke` bridge the frontend uses to drive the loop
// (record -> compile -> replay -> teach) and auth (login/paste) over the
// sidecar's JSON-lines protocol.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod pairing;
mod sidecar;
mod tray;

use std::sync::Arc;

use sidecar::{SidecarHandle, SidecarInner};
use tauri::Manager;

fn main() {
    let engine = Arc::new(SidecarInner::default());
    let engine_for_exit = engine.clone();
    let pairing_links = Arc::new(pairing::PairingLinkState::default());

    tauri::Builder::default()
        // Must be first: on Windows/Linux it forwards a second process's
        // statically configured deep link into the deep-link plugin event.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(SidecarHandle(engine.clone()))
        .setup(move |app| {
            tray::setup_tray(app)?;

            // Show the main window (config keeps it hidden until the frontend is
            // ready so there is no white flash).
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }

            // Spawn the frozen Python engine sidecar. Guarded: if the binary is
            // absent (frontend-only dev) the app still runs; the UI shows an
            // "engine offline" state.
            sidecar::spawn(&app.handle().clone(), engine.clone());
            pairing::setup(app, engine.clone(), pairing_links.clone())?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            // generic bridge + native helpers
            commands::engine_invoke,
            commands::sidecar_status,
            commands::open_external,
            // typed convenience commands (forward to the sidecar)
            commands::start_recording,
            commands::stop_recording,
            commands::pause_recording,
            commands::get_status,
            commands::get_captures,
            commands::get_storage_usage,
            commands::set_config,
            commands::upload_capture,
            commands::delete_capture,
            commands::scrub_capture,
            commands::approve_review,
            commands::dismiss_review,
            commands::get_pending_reviews,
        ])
        .build(tauri::generate_context!())
        .expect("error while building openadapt-desktop")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                sidecar::shutdown(&engine_for_exit);
            }
        });
}
