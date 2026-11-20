// The main window: closing it hides it, and every way back shows it again.
//
// The app keeps running after the main window is closed, because the tray icon
// and the hidden control overlay stay alive. Before 0.30.5 closing destroyed the
// window, so nothing could bring it back: a second launch from the Start menu,
// "Open OpenAdapt" in the tray menu and a click on the macOS Dock icon all
// looked for a window that no longer existed and did nothing.
//
// Closing now hides the window instead. `show_main` is the one way back, used
// by the single-instance handler (a second launch), the tray menu and the Dock.

use tauri::{AppHandle, Manager, Window, WindowEvent};

pub const MAIN_WINDOW: &str = "main";

/// What a close request does to a window.
#[derive(Debug, PartialEq, Eq)]
pub enum CloseAction {
    /// Keep the window and hide it, so it can be shown again.
    Hide,
    /// Let the window close.
    Close,
}

/// Only the main window is kept; any other window closes as usual.
pub fn close_action(label: &str) -> CloseAction {
    if label == MAIN_WINDOW {
        CloseAction::Hide
    } else {
        CloseAction::Close
    }
}

/// Hide the main window when the person closes it.
pub fn on_window_event(window: &Window, event: &WindowEvent) {
    if let WindowEvent::CloseRequested { api, .. } = event {
        if close_action(window.label()) == CloseAction::Hide {
            api.prevent_close();
            let _ = window.hide();
        }
    }
}

/// Bring the main window back to the front.
pub fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn closing_the_main_window_hides_it() {
        assert_eq!(close_action("main"), CloseAction::Hide);
    }

    #[test]
    fn other_windows_close_as_usual() {
        assert_eq!(close_action("control-overlay"), CloseAction::Close);
        assert_eq!(close_action(""), CloseAction::Close);
    }
}
