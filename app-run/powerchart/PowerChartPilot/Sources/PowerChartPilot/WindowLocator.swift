import AppKit
import CoreGraphics

struct TargetWindow: Identifiable, Hashable {
    let windowID: CGWindowID
    let pid: pid_t
    let owner: String
    let title: String
    let bounds: CGRect          // Quartz coords: origin top-left of main display

    var id: CGWindowID { windowID }

    var snapshot: WindowSnapshot {
        WindowSnapshot(
            title: title, owner: owner,
            x: bounds.origin.x, y: bounds.origin.y,
            w: bounds.width, h: bounds.height
        )
    }
}

enum WindowLocator {
    /// On-screen, layer-0 windows whose owning app name contains `ownerContains`.
    /// Reading other apps' window titles requires Screen Recording permission.
    static func windows(ownerContains: String) -> [TargetWindow] {
        guard
            let raw = CGWindowListCopyWindowInfo(
                [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
            ) as? [[String: Any]]
        else { return [] }

        let needle = ownerContains.lowercased()
        var out: [TargetWindow] = []
        for info in raw {
            guard
                let owner = info[kCGWindowOwnerName as String] as? String,
                owner.lowercased().contains(needle),
                (info[kCGWindowLayer as String] as? Int) == 0,
                let num = info[kCGWindowNumber as String] as? UInt32,
                let pid = info[kCGWindowOwnerPID as String] as? pid_t,
                let b = info[kCGWindowBounds as String] as? [String: CGFloat]
            else { continue }
            let rect = CGRect(
                x: b["X"] ?? 0, y: b["Y"] ?? 0,
                width: b["Width"] ?? 0, height: b["Height"] ?? 0
            )
            // Skip tiny helper windows (tooltips, 1px panels).
            guard rect.width > 200, rect.height > 150 else { continue }
            let title = info[kCGWindowName as String] as? String ?? ""
            out.append(
                TargetWindow(
                    windowID: num, pid: pid, owner: owner, title: title, bounds: rect
                )
            )
        }
        // Largest window first — typically the main Citrix / PowerChart surface.
        return out.sorted { $0.bounds.width * $0.bounds.height > $1.bounds.width * $1.bounds.height }
    }

    static func best(ownerContains: String, titleContains: String? = nil) -> TargetWindow? {
        let all = windows(ownerContains: ownerContains)
        if let t = titleContains, !t.isEmpty {
            if let hit = all.first(where: { $0.title.lowercased().contains(t.lowercased()) }) {
                return hit
            }
        }
        return all.first
    }

    /// Re-read live bounds for a window id (window may have moved/resized).
    static func refresh(_ target: TargetWindow) -> TargetWindow? {
        guard
            let raw = CGWindowListCopyWindowInfo(
                [.optionIncludingWindow], target.windowID
            ) as? [[String: Any]],
            let info = raw.first,
            let b = info[kCGWindowBounds as String] as? [String: CGFloat]
        else { return nil }
        let rect = CGRect(
            x: b["X"] ?? 0, y: b["Y"] ?? 0,
            width: b["Width"] ?? 0, height: b["Height"] ?? 0
        )
        let title = info[kCGWindowName as String] as? String ?? target.title
        return TargetWindow(
            windowID: target.windowID, pid: target.pid,
            owner: target.owner, title: title, bounds: rect
        )
    }

    static func activate(pid: pid_t) {
        NSRunningApplication(processIdentifier: pid)?
            .activate(options: [.activateIgnoringOtherApps])
    }
}
