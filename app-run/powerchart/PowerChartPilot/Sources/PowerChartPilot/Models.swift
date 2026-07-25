import Foundation

/// Capture scope chosen before the 3-2-1 countdown.
enum CaptureTarget: String, Codable, CaseIterable, Identifiable {
    case window
    case screen
    var id: String { rawValue }
    var label: String {
        switch self {
        case .window: return "Selected window"
        case .screen: return "Entire screen"
        }
    }
}

/// One recorded UI action. Field names match the Python runner's events.jsonl
/// (i, kind, x, y, text, dy). fx/fy are fractions of the capture surface.
struct RecordedEvent: Codable, Identifiable, Hashable {
    var i: Int
    var kind: String            // click | double_click | type | key | scroll | note | manual
    var t: Double
    var x: Double?
    var y: Double?
    var fx: Double?
    var fy: Double?
    var text: String?
    var key: String?
    var dy: Double?
    var frame: String?
    /// Human label for inserted/manual steps (not replayed as CGEvents).
    var label: String?
    /// true when the user inserted this step during annotation (not auto-recorded).
    var inserted: Bool?

    var id: Int { i }

    var isReplayable: Bool {
        switch kind {
        case "click", "double_click", "type", "key", "scroll": return true
        default: return false
        }
    }

    var summary: String {
        if let label, !label.isEmpty { return label }
        switch kind {
        case "click", "double_click":
            let pos: String
            if let fx, let fy {
                pos = String(format: "%.1f%%, %.1f%%", fx * 100, fy * 100)
            } else if let x, let y {
                pos = String(format: "%.0f, %.0f pt", x, y)
            } else {
                pos = "?"
            }
            return "\(kind == "double_click" ? "Double-click" : "Click") @ \(pos)"
        case "type":
            return "Type \"\(text ?? "")\""
        case "key":
            return "Press \(key ?? "?")"
        case "scroll":
            return "Scroll dy=\(Int(dy ?? 0))"
        case "note":
            return "Note: \(text ?? "")"
        case "manual":
            return "Manual: \(text ?? label ?? "step")"
        default:
            return kind
        }
    }
}

/// Region on a step screenshot, in fractions of the frame (0..1) — portable
/// across window sizes, same idea as DesignPinLayer's xPct/yPct.
struct RegionPin: Codable, Hashable {
    var fx: Double
    var fy: Double
    var fw: Double
    var fh: Double
    var targetHint: String?

    var summary: String {
        if let targetHint, !targetHint.isEmpty { return targetHint }
        return String(
            format: "region %.0f%%,%.0f%% %.0f×%.0f%%",
            fx * 100, fy * 100, fw * 100, fh * 100
        )
    }
}

/// Button-based annotation kinds — mirrors health-priority ActionDock modes.
enum AnnotationKind: String, Codable, CaseIterable, Identifiable {
    case comment
    case flag
    case grade
    case pin
    case better
    case rerecord
    case note

    var id: String { rawValue }

    var label: String {
        switch self {
        case .comment: return "Comment"
        case .flag: return "Flag"
        case .grade: return "Grade"
        case .pin: return "Pin region"
        case .better: return "Better action"
        case .rerecord: return "Re-record"
        case .note: return "Note"
        }
    }
}

struct StepAnnotation: Codable, Identifiable, Hashable {
    var id: String
    /// Index into events.jsonl; nil = session-level annotation.
    var stepIndex: Int?
    var kind: String
    var body: String
    var flagType: String?
    var grade: Int?                 // 1–5
    var region: RegionPin?
    var proposedAction: String?     // free-text "better action" for clawagents
    var createdAt: Date

    static func make(
        stepIndex: Int?,
        kind: AnnotationKind,
        body: String,
        flagType: String? = nil,
        grade: Int? = nil,
        region: RegionPin? = nil,
        proposedAction: String? = nil
    ) -> StepAnnotation {
        StepAnnotation(
            id: UUID().uuidString,
            stepIndex: stepIndex,
            kind: kind.rawValue,
            body: body,
            flagType: flagType,
            grade: grade,
            region: region,
            proposedAction: proposedAction,
            createdAt: Date()
        )
    }
}

struct WindowSnapshot: Codable, Hashable {
    var title: String
    var owner: String
    var x: Double
    var y: Double
    var w: Double
    var h: Double
}

struct SessionMeta: Codable {
    var name: String
    var createdAt: Date
    var host: String
    var osVersion: String
    var appVersion: String
    var targetOwner: String
    var captureTarget: String?      // window | screen
    var window: WindowSnapshot?
    var screenW: Double
    var screenH: Double
    var eventCount: Int
    var annotationCount: Int?
    var notes: String?
}

struct ReplayStepResult: Codable {
    var i: Int
    var action: RecordedEvent
    var decision: String
    var absX: Double?
    var absY: Double?
    var afterFrame: String?
    var timestamp: Date
}

struct ReplayReport: Codable {
    var sessionName: String
    var startedAt: Date
    var finishedAt: Date?
    var targetWindow: WindowSnapshot?
    var steps: [ReplayStepResult]
    var outcome: String
}

enum FlagTypes {
    static let all = [
        "wrong_target",
        "timing",
        "missing_step",
        "extra_step",
        "ocr_issue",
        "auth_dialog",
        "layout_drift",
        "other",
    ]
}
