import AppKit
import CoreGraphics
import Foundation

/// Global listen-only event tap. Esc stops recording. Capture can be a single
/// window or the entire screen. Coordinates are stored as fractions of the
/// capture surface so they port across machines.
final class RecorderEngine: ObservableObject {
    @Published var isRecording = false
    @Published var events: [RecordedEvent] = []
    @Published var statusText = "Idle"
    @Published var captureTarget: CaptureTarget = .window

    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var target: TargetWindow?
    private var sessionDir: URL?
    private var startTime = Date()
    private var nextIndex = 0
    private var captureBounds: CGRect = .zero

    private var pendingText = ""
    private var pendingTextFrame: String?
    private var lastKeyAt = Date.distantPast

    private var pendingScrollDY = 0.0
    private var pendingScrollAt = Date.distantPast
    private var pendingScrollLoc = CGPoint.zero
    private var pendingScrollFrame: String?

    private let typeGap: TimeInterval = 1.5
    private let scrollGap: TimeInterval = 0.5

    var onStoppedByEscape: (() -> Void)?

    // MARK: lifecycle

    func start(
        target: TargetWindow?,
        captureTarget: CaptureTarget,
        sessionDir: URL
    ) -> Bool {
        stop(silent: true)
        self.captureTarget = captureTarget
        self.target = target
        self.sessionDir = sessionDir
        self.startTime = Date()
        self.nextIndex = 0
        events = []
        pendingText = ""
        pendingScrollDY = 0

        switch captureTarget {
        case .window:
            guard let target else {
                statusText = "No window selected"
                return false
            }
            captureBounds = target.bounds
        case .screen:
            // Quartz main-display bounds (origin top-left of primary).
            if let screen = NSScreen.main {
                let f = screen.frame
                captureBounds = CGRect(x: 0, y: 0, width: f.width, height: f.height)
            } else {
                captureBounds = CGRect(x: 0, y: 0, width: 1920, height: 1080)
            }
        }

        try? FileManager.default.createDirectory(
            at: sessionDir.appendingPathComponent("frames"),
            withIntermediateDirectories: true
        )

        let mask: CGEventMask =
            (1 << CGEventType.leftMouseDown.rawValue)
            | (1 << CGEventType.keyDown.rawValue)
            | (1 << CGEventType.scrollWheel.rawValue)

        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard
            let tap = CGEvent.tapCreate(
                tap: .cghidEventTap,
                place: .headInsertEventTap,
                options: .listenOnly,
                eventsOfInterest: mask,
                callback: { _, type, event, refcon in
                    guard let refcon else { return Unmanaged.passUnretained(event) }
                    let me = Unmanaged<RecorderEngine>.fromOpaque(refcon)
                        .takeUnretainedValue()
                    me.handle(type: type, event: event)
                    return Unmanaged.passUnretained(event)
                },
                userInfo: refcon
            )
        else {
            statusText = "Could not create event tap — check Input Monitoring"
            return false
        }
        self.tap = tap
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        self.runLoopSource = source
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        isRecording = true
        let where_ = captureTarget == .screen
            ? "entire screen"
            : (target?.owner ?? "window")
        statusText = "Recording \(where_) — press Esc to stop"

        let initial = sessionDir.appendingPathComponent("frames/initial.png")
        Screenshot.captureAsync(
            target: captureTarget,
            windowID: target?.windowID,
            to: initial
        )
        return true
    }

    func stop(silent: Bool = false) {
        flushPendingText()
        flushPendingScroll()
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let src = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), src, .commonModes)
        }
        tap = nil
        runLoopSource = nil
        if isRecording {
            isRecording = false
            if !silent {
                statusText = "Stopped — \(events.count) events"
            }
        }
    }

    @discardableResult
    func save(name: String, notes: String?) -> SessionMeta? {
        guard let dir = sessionDir else { return nil }
        flushPendingText()
        flushPendingScroll()

        let enc = JSONEncoder()
        var lines: [String] = []
        for ev in events {
            if let data = try? enc.encode(ev),
                let s = String(data: data, encoding: .utf8) {
                lines.append(s)
            }
        }
        do {
            try lines.joined(separator: "\n").appending("\n")
                .write(
                    to: dir.appendingPathComponent("events.jsonl"),
                    atomically: true, encoding: .utf8
                )
        } catch {
            statusText = "Save failed: \(error.localizedDescription)"
            return nil
        }

        // Empty annotations file ready for the Annotate tab.
        let annURL = dir.appendingPathComponent("annotations.jsonl")
        if !FileManager.default.fileExists(atPath: annURL.path) {
            try? "".write(to: annURL, atomically: true, encoding: .utf8)
        }

        let screen = NSScreen.main?.frame ?? .zero
        let meta = SessionMeta(
            name: name,
            createdAt: startTime,
            host: Host.current().localizedName ?? "unknown",
            osVersion: ProcessInfo.processInfo.operatingSystemVersionString,
            appVersion: "1.1",
            targetOwner: target?.owner
                ?? (captureTarget == .screen ? "Entire Screen" : "unknown"),
            captureTarget: captureTarget.rawValue,
            window: target.map {
                WindowLocator.refresh($0)?.snapshot ?? $0.snapshot
            },
            screenW: screen.width,
            screenH: screen.height,
            eventCount: events.count,
            annotationCount: 0,
            notes: notes
        )
        let metaEnc = JSONEncoder()
        metaEnc.outputFormatting = [.prettyPrinted, .sortedKeys]
        metaEnc.dateEncodingStrategy = .iso8601
        if let data = try? metaEnc.encode(meta) {
            try? data.write(to: dir.appendingPathComponent("meta.json"))
        }
        statusText = "Saved \(events.count) events → \(dir.lastPathComponent)"
        return meta
    }

    // MARK: event handling

    private func handle(type: CGEventType, event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return
        }

        // Esc always stops recording (even if focused on our app).
        if type == .keyDown {
            let code = event.getIntegerValueField(.keyboardEventKeycode)
            if code == 53 { // Escape
                DispatchQueue.main.async { [self] in
                    guard isRecording else { return }
                    stop()
                    onStoppedByEscape?()
                }
                return
            }
        }

        refreshBoundsIfNeeded()
        let loc = event.location

        switch type {
        case .leftMouseDown:
            guard captureBounds.contains(loc), !isOwnWindow(loc) else { return }
            flushPendingText()
            flushPendingScroll()
            let clickState = event.getIntegerValueField(.mouseEventClickState)
            let frame = captureFrame()
            let rel = relative(loc)
            DispatchQueue.main.async { [self] in
                if clickState >= 2, let last = events.last,
                    last.kind == "click",
                    abs((last.x ?? -99) - rel.x) < 6,
                    abs((last.y ?? -99) - rel.y) < 6 {
                    events[events.count - 1].kind = "double_click"
                    statusText = "Double-click recorded"
                } else {
                    append(
                        kind: clickState >= 2 ? "double_click" : "click",
                        rel: rel, frame: frame
                    )
                }
            }

        case .keyDown:
            if captureTarget == .window {
                guard
                    let pid = target?.pid,
                    NSWorkspace.shared.frontmostApplication?.processIdentifier == pid
                else { return }
            } else if isOwnWindow(CGPoint(x: 0, y: 0)) == false {
                // Screen mode: skip if our app is frontmost.
                if NSWorkspace.shared.frontmostApplication?.bundleIdentifier
                    == Bundle.main.bundleIdentifier {
                    return
                }
            }
            flushPendingScroll()
            let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
            if let special = specialKeyName(keyCode) {
                // Don't record Esc as a typed key — it stops recording.
                if special == "escape" { return }
                flushPendingText()
                let frame = captureFrame()
                DispatchQueue.main.async { [self] in
                    append(kind: "key", key: special, frame: frame)
                }
                return
            }
            var chars = [UniChar](repeating: 0, count: 8)
            var len = 0
            event.keyboardGetUnicodeString(
                maxStringLength: 8, actualStringLength: &len,
                unicodeString: &chars
            )
            guard len > 0 else { return }
            let s = String(utf16CodeUnits: chars, count: len)
            guard !s.isEmpty, s.unicodeScalars.allSatisfy({ $0.value >= 32 }) else {
                return
            }
            DispatchQueue.main.async { [self] in
                if Date().timeIntervalSince(lastKeyAt) > typeGap {
                    flushPendingText()
                }
                if pendingText.isEmpty {
                    pendingTextFrame = captureFrame()
                }
                pendingText += s
                lastKeyAt = Date()
                statusText = "Typing: \(pendingText)"
            }

        case .scrollWheel:
            guard captureBounds.contains(loc), !isOwnWindow(loc) else { return }
            flushPendingText()
            let dy = Double(event.getIntegerValueField(.scrollWheelEventDeltaAxis1))
            guard dy != 0 else { return }
            DispatchQueue.main.async { [self] in
                if Date().timeIntervalSince(pendingScrollAt) > scrollGap {
                    flushPendingScroll()
                }
                if pendingScrollDY == 0 {
                    pendingScrollFrame = captureFrame()
                    pendingScrollLoc = loc
                }
                pendingScrollDY += dy
                pendingScrollAt = Date()
                statusText = "Scroll: \(Int(pendingScrollDY))"
            }

        default:
            break
        }
    }

    // MARK: helpers

    private func refreshBoundsIfNeeded() {
        if captureTarget == .window, let t = target,
            let fresh = WindowLocator.refresh(t) {
            target = fresh
            captureBounds = fresh.bounds
        }
    }

    private func relative(_ p: CGPoint) -> CGPoint {
        CGPoint(x: p.x - captureBounds.origin.x, y: p.y - captureBounds.origin.y)
    }

    private func isOwnWindow(_ quartzPoint: CGPoint) -> Bool {
        guard let screen = NSScreen.screens.first else { return false }
        let appKitY = screen.frame.height - quartzPoint.y
        let pt = NSPoint(x: quartzPoint.x, y: appKitY)
        for w in NSApp.windows where w.isVisible {
            if w.frame.contains(pt) { return true }
        }
        return false
    }

    private func captureFrame() -> String {
        let name = String(format: "step_%03d.png", nextIndex + pendingCount())
        if let dir = sessionDir {
            Screenshot.captureAsync(
                target: captureTarget,
                windowID: target?.windowID,
                to: dir.appendingPathComponent("frames/\(name)")
            )
        }
        return name
    }

    private func pendingCount() -> Int {
        (pendingText.isEmpty ? 0 : 1) + (pendingScrollDY == 0 ? 0 : 1)
    }

    private func append(
        kind: String,
        rel: CGPoint? = nil,
        text: String? = nil,
        key: String? = nil,
        dy: Double? = nil,
        frame: String?,
        label: String? = nil,
        inserted: Bool? = nil
    ) {
        var ev = RecordedEvent(
            i: nextIndex, kind: kind,
            t: Date().timeIntervalSince(startTime),
            x: rel.map { Double($0.x) }, y: rel.map { Double($0.y) },
            fx: nil, fy: nil,
            text: text, key: key, dy: dy, frame: frame,
            label: label, inserted: inserted
        )
        if let rel, captureBounds.width > 0, captureBounds.height > 0 {
            ev.fx = Double(rel.x / captureBounds.width)
            ev.fy = Double(rel.y / captureBounds.height)
        }
        events.append(ev)
        nextIndex += 1
        statusText = ev.summary
    }

    private func flushPendingText() {
        guard !pendingText.isEmpty else { return }
        let text = pendingText
        let frame = pendingTextFrame
        pendingText = ""
        pendingTextFrame = nil
        let work = { [self] in append(kind: "type", text: text, frame: frame) }
        if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) }
    }

    private func flushPendingScroll() {
        guard pendingScrollDY != 0 else { return }
        let dy = pendingScrollDY
        let loc = pendingScrollLoc
        let frame = pendingScrollFrame
        pendingScrollDY = 0
        pendingScrollFrame = nil
        let rel = relative(loc)
        let work = { [self] in
            append(kind: "scroll", rel: rel, dy: dy, frame: frame)
        }
        if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) }
    }

    private func specialKeyName(_ code: Int64) -> String? {
        switch code {
        case 36, 76: return "return"
        case 53: return "escape"
        case 48: return "tab"
        case 51: return "delete"
        default: return nil
        }
    }
}
