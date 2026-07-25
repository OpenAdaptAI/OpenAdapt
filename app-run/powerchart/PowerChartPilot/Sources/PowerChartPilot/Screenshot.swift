import Foundation
import CoreGraphics

/// Screenshots via /usr/sbin/screencapture — window (`-l`) or full desktop.
enum Screenshot {
    @discardableResult
    static func captureWindow(_ windowID: CGWindowID, to url: URL) -> Bool {
        run(["-x", "-o", "-l\(windowID)", url.path], to: url)
    }

    @discardableResult
    static func captureScreen(to url: URL) -> Bool {
        // Full interactive desktop (main display). -x = no sound, -C includes cursor.
        run(["-x", "-C", url.path], to: url)
    }

    static func captureAsync(
        target: CaptureTarget,
        windowID: CGWindowID?,
        to url: URL
    ) {
        DispatchQueue.global(qos: .utility).async {
            switch target {
            case .window:
                if let id = windowID { _ = captureWindow(id, to: url) }
            case .screen:
                _ = captureScreen(to: url)
            }
        }
    }

    private static func run(_ args: [String], to url: URL) -> Bool {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = args
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            return false
        }
        return FileManager.default.fileExists(atPath: url.path)
    }
}
