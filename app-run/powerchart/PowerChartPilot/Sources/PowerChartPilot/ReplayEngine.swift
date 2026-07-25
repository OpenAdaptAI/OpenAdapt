import AppKit
import CoreGraphics
import Foundation

/// Turn-by-turn replayer. Each step waits for explicit user confirmation
/// (Run / Skip / Abort) unless auto-run is enabled. Coordinates are resolved
/// from recorded window *fractions* against the live window bounds, so a
/// session recorded on one screen replays correctly on another.
final class ReplayEngine: ObservableObject {
    enum State: Equatable {
        case idle
        case waitingConfirm(step: Int)
        case running(step: Int)
        case finished(outcome: String)
    }

    @Published var state: State = .idle
    @Published var log: [String] = []
    @Published var autoRun = false
    @Published var autoDelay = 1.0
    @Published var results: [ReplayStepResult] = []

    private(set) var session: SessionRecord?
    private var target: TargetWindow?
    private var replayDir: URL?
    private var startedAt = Date()

    var events: [RecordedEvent] { session?.events ?? [] }

    var currentStep: Int? {
        switch state {
        case .waitingConfirm(let s), .running(let s): return s
        default: return nil
        }
    }

    // MARK: control

    func begin(session: SessionRecord, target: TargetWindow) {
        self.session = session
        self.target = target
        self.results = []
        self.startedAt = Date()
        self.log = []

        let stamp = Self.timestamp()
        let dir = session.dir.appendingPathComponent("replays/\(stamp)")
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        self.replayDir = dir

        appendLog("Target: \(target.owner) — \(target.title)")
        appendLog(
            String(
                format: "Window %.0f×%.0f (recorded %.0f×%.0f)",
                target.bounds.width, target.bounds.height,
                session.meta?.window?.w ?? 0, session.meta?.window?.h ?? 0
            )
        )
        if session.events.isEmpty {
            state = .finished(outcome: "error:no events")
        } else {
            state = .waitingConfirm(step: 0)
        }
    }

    func confirmRun() {
        guard case .waitingConfirm(let step) = state else { return }
        runStep(step)
    }

    func skip() {
        guard case .waitingConfirm(let step) = state else { return }
        let ev = events[step]
        results.append(
            ReplayStepResult(
                i: ev.i, action: ev, decision: "skipped",
                absX: nil, absY: nil, afterFrame: nil, timestamp: Date()
            )
        )
        appendLog("[\(step)] skipped — \(ev.summary)")
        advance(from: step)
    }

    func abort() {
        if case .waitingConfirm(let step) = state {
            let ev = events[step]
            results.append(
                ReplayStepResult(
                    i: ev.i, action: ev, decision: "aborted",
                    absX: nil, absY: nil, afterFrame: nil, timestamp: Date()
                )
            )
        }
        finish(outcome: "aborted")
    }

    // MARK: execution

    private func runStep(_ step: Int) {
        guard let target = refreshedTarget() else {
            finish(outcome: "error:target window lost")
            return
        }
        state = .running(step: step)
        let ev = events[step]
        WindowLocator.activate(pid: target.pid)

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            // Give the target app a beat to come frontmost.
            Thread.sleep(forTimeInterval: 0.35)
            var absPoint: CGPoint?

            switch ev.kind {
            case "click", "double_click":
                if let p = resolvePoint(ev, in: target.bounds) {
                    absPoint = p
                    postClick(at: p, clicks: ev.kind == "double_click" ? 2 : 1)
                }
            case "type":
                if let text = ev.text { postText(text) }
            case "key":
                if let code = Self.keyCode(ev.key) { postKey(code) }
            case "scroll":
                let p = resolvePoint(ev, in: target.bounds)
                    ?? CGPoint(
                        x: target.bounds.midX, y: target.bounds.midY
                    )
                absPoint = p
                postScroll(at: p, dy: Int32(clamping: Int(ev.dy ?? -3)))
            default:
                break
            }

            Thread.sleep(forTimeInterval: 0.6)
            var afterName: String?
            if let dir = replayDir {
                let name = String(format: "after_%03d.png", step)
                if Screenshot.captureWindow(
                    target.windowID, to: dir.appendingPathComponent(name)
                ) {
                    afterName = name
                }
            }

            DispatchQueue.main.async { [self] in
                results.append(
                    ReplayStepResult(
                        i: ev.i, action: ev, decision: "ran",
                        absX: absPoint.map { Double($0.x) },
                        absY: absPoint.map { Double($0.y) },
                        afterFrame: afterName, timestamp: Date()
                    )
                )
                appendLog("[\(step)] ran — \(ev.summary)")
                advance(from: step)
            }
        }
    }

    private func advance(from step: Int) {
        let next = step + 1
        if next >= events.count {
            finish(outcome: "completed")
            return
        }
        state = .waitingConfirm(step: next)
        if autoRun {
            DispatchQueue.main.asyncAfter(deadline: .now() + autoDelay) { [self] in
                if case .waitingConfirm(let s) = state, s == next, autoRun {
                    runStep(s)
                }
            }
        }
    }

    private func finish(outcome: String) {
        state = .finished(outcome: outcome)
        appendLog("Replay \(outcome)")
        guard let dir = replayDir, let session else { return }
        let report = ReplayReport(
            sessionName: session.dir.lastPathComponent,
            startedAt: startedAt,
            finishedAt: Date(),
            targetWindow: target.map { WindowLocator.refresh($0)?.snapshot ?? $0.snapshot },
            steps: results,
            outcome: outcome
        )
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        enc.dateEncodingStrategy = .iso8601
        if let data = try? enc.encode(report) {
            try? data.write(to: dir.appendingPathComponent("report.json"))
        }
    }

    // MARK: coordinate resolution (the cross-machine hardening)

    private func refreshedTarget() -> TargetWindow? {
        guard let t = target else { return nil }
        if let fresh = WindowLocator.refresh(t) {
            target = fresh
            return fresh
        }
        return nil
    }

    /// Prefer fractions; fall back to recorded points scaled by the ratio of
    /// live window size to recorded window size (legacy recordings).
    private func resolvePoint(_ ev: RecordedEvent, in live: CGRect) -> CGPoint? {
        if let fx = ev.fx, let fy = ev.fy {
            return CGPoint(
                x: live.origin.x + fx * live.width,
                y: live.origin.y + fy * live.height
            )
        }
        guard let x = ev.x, let y = ev.y else { return nil }
        let rec = session?.meta?.window
        let sx = (rec?.w ?? live.width) > 0 ? live.width / (rec?.w ?? live.width) : 1
        let sy = (rec?.h ?? live.height) > 0 ? live.height / (rec?.h ?? live.height) : 1
        return CGPoint(
            x: live.origin.x + x * sx,
            y: live.origin.y + y * sy
        )
    }

    // MARK: CGEvent posting

    private func postClick(at p: CGPoint, clicks: Int) {
        let move = CGEvent(
            mouseEventSource: nil, mouseType: .mouseMoved,
            mouseCursorPosition: p, mouseButton: .left
        )
        move?.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.06)
        for n in 1...clicks {
            let down = CGEvent(
                mouseEventSource: nil, mouseType: .leftMouseDown,
                mouseCursorPosition: p, mouseButton: .left
            )
            let up = CGEvent(
                mouseEventSource: nil, mouseType: .leftMouseUp,
                mouseCursorPosition: p, mouseButton: .left
            )
            down?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            up?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            down?.post(tap: .cghidEventTap)
            up?.post(tap: .cghidEventTap)
            Thread.sleep(forTimeInterval: 0.08)
        }
    }

    private func postText(_ text: String) {
        for ch in text {
            let units = Array(String(ch).utf16)
            let down = CGEvent(
                keyboardEventSource: nil, virtualKey: 0, keyDown: true
            )
            down?.keyboardSetUnicodeString(
                stringLength: units.count, unicodeString: units
            )
            down?.post(tap: .cghidEventTap)
            let up = CGEvent(
                keyboardEventSource: nil, virtualKey: 0, keyDown: false
            )
            up?.keyboardSetUnicodeString(
                stringLength: units.count, unicodeString: units
            )
            up?.post(tap: .cghidEventTap)
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    private func postKey(_ code: CGKeyCode) {
        CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true)?
            .post(tap: .cghidEventTap)
        CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)?
            .post(tap: .cghidEventTap)
    }

    private func postScroll(at p: CGPoint, dy: Int32) {
        let move = CGEvent(
            mouseEventSource: nil, mouseType: .mouseMoved,
            mouseCursorPosition: p, mouseButton: .left
        )
        move?.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.04)
        let scroll = CGEvent(
            scrollWheelEvent2Source: nil, units: .line,
            wheelCount: 1, wheel1: dy, wheel2: 0, wheel3: 0
        )
        scroll?.post(tap: .cghidEventTap)
    }

    private func appendLog(_ s: String) {
        log.append(s)
        if log.count > 400 { log.removeFirst(100) }
    }

    private static func timestamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.string(from: Date())
    }

    static func keyCode(_ name: String?) -> CGKeyCode? {
        switch name {
        case "return": return 36
        case "escape": return 53
        case "tab": return 48
        case "delete": return 51
        default: return nil
        }
    }
}
