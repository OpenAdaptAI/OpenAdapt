// OpenAdapt Desktop - Tauri entry point
//
// This is the main entry point for the Tauri application shell.
// It initializes the system tray, registers IPC commands, and
// spawns the Python sidecar process for the recording engine.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod sidecar;
mod tray;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            tray::setup_tray(app)?;
            // TODO: spawn Python sidecar on startup
            // sidecar::spawn(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
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
        .run(tauri::generate_context!())
        .expect("error while running openadapt-desktop");
}
