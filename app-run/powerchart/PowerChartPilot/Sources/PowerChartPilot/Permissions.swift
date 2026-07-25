import AppKit
import ApplicationServices
import CoreGraphics
import IOKit.hid

/// The three TCC permissions the app needs, with prompts and deep links.
/// - Accessibility: post CGEvents during replay
/// - Input Monitoring: listen-only event tap during recording
/// - Screen Recording: window screenshots (via /usr/sbin/screencapture)
enum Permissions {
    struct Status {
        var accessibility: Bool
        var screenRecording: Bool
        var inputMonitoring: Bool
        var allGranted: Bool { accessibility && screenRecording && inputMonitoring }
    }

    static func check() -> Status {
        Status(
            accessibility: AXIsProcessTrusted(),
            screenRecording: CGPreflightScreenCaptureAccess(),
            inputMonitoring: IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)
                == kIOHIDAccessTypeGranted
        )
    }

    static func requestAccessibility() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    }

    static func requestScreenRecording() {
        _ = CGRequestScreenCaptureAccess()
    }

    static func requestInputMonitoring() {
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
    }

    static func openSettings(pane: String) {
        // x-apple.systempreferences deep links (Privacy & Security panes).
        let urls = [
            "accessibility":
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "screen":
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            "input":
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
        ]
        if let s = urls[pane], let url = URL(string: s) {
            NSWorkspace.shared.open(url)
        }
    }
}
