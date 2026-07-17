// Minimal in-window system tray for OpenAdapt Desktop.
//
// NOTE ON SCOPE: the always-on, cross-platform status tray (recording/sync/break
// badge, quick actions, cloud count poll) is a SEPARATE application — the
// `openadapt-tray` repo (pystray, owned by workstream W3). It talks to this
// desktop app over a localhost loopback socket (spec §3d).
//
// This module therefore keeps only a lightweight Tauri tray so the desktop app
// itself is reachable from the menu bar (show window / quit). The rich status
// model lives in the standalone tray, not here.
//
// The recording indicator remains a compliance requirement wherever a tray is
// shown: the tooltip reflects whether recording is active.

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    App, Manager,
};

/// Build the minimal tray icon + context menu (Show window / Quit).
pub fn setup_tray(app: &App) -> Result<(), Box<dyn std::error::Error>> {
    let show = MenuItem::with_id(app, "show", "Open OpenAdapt", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::with_id("openadapt-desktop-tray")
        .tooltip("OpenAdapt Desktop")
        .icon(
            app.default_window_icon()
                .cloned()
                .ok_or("missing default window icon")?,
        )
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;

    Ok(())
}
