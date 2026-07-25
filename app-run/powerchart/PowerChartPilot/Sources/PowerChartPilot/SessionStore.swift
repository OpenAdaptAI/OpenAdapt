import Foundation

struct SessionRecord: Identifiable, Hashable {
    let dir: URL
    var meta: SessionMeta?
    var events: [RecordedEvent]
    var annotations: [StepAnnotation]

    var id: String { dir.path }
    var name: String { meta?.name ?? dir.lastPathComponent }

    static func == (lhs: SessionRecord, rhs: SessionRecord) -> Bool {
        lhs.dir == rhs.dir
    }
    func hash(into hasher: inout Hasher) { hasher.combine(dir) }
}

/// Sessions live in ~/Documents/PowerChartPilot/sessions/<stamp>-<name>/
enum SessionStore {
    static var root: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("PowerChartPilot/sessions")
    }

    static func newSessionDir(name: String) -> URL {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss"
        let safe = name.replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: " ", with: "_")
        let dir = root.appendingPathComponent(
            "\(f.string(from: Date()))-\(safe.isEmpty ? "session" : safe)"
        )
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        return dir
    }

    static func list() -> [SessionRecord] {
        guard
            let dirs = try? FileManager.default.contentsOfDirectory(
                at: root, includingPropertiesForKeys: nil
            )
        else { return [] }
        return dirs
            .filter { $0.hasDirectoryPath }
            .compactMap { load(dir: $0) }
            .sorted {
                ($0.meta?.createdAt ?? .distantPast)
                    > ($1.meta?.createdAt ?? .distantPast)
            }
    }

    static func load(dir: URL) -> SessionRecord? {
        let eventsURL = dir.appendingPathComponent("events.jsonl")
        guard FileManager.default.fileExists(atPath: eventsURL.path) else {
            return nil
        }
        let dec = JSONDecoder()
        var events: [RecordedEvent] = []
        if let text = try? String(contentsOf: eventsURL, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                if let ev = try? dec.decode(
                    RecordedEvent.self, from: Data(line.utf8)
                ) {
                    events.append(ev)
                }
            }
        }
        var annotations: [StepAnnotation] = []
        let annDec = JSONDecoder()
        annDec.dateDecodingStrategy = .iso8601
        if let text = try? String(
            contentsOf: dir.appendingPathComponent("annotations.jsonl"),
            encoding: .utf8
        ) {
            for line in text.split(separator: "\n") {
                if let a = try? annDec.decode(
                    StepAnnotation.self, from: Data(line.utf8)
                ) {
                    annotations.append(a)
                }
            }
        }
        var meta: SessionMeta?
        let metaDec = JSONDecoder()
        metaDec.dateDecodingStrategy = .iso8601
        if let data = try? Data(
            contentsOf: dir.appendingPathComponent("meta.json")
        ) {
            meta = try? metaDec.decode(SessionMeta.self, from: data)
        }
        return SessionRecord(
            dir: dir, meta: meta, events: events, annotations: annotations
        )
    }

    static func delete(_ session: SessionRecord) {
        try? FileManager.default.removeItem(at: session.dir)
    }

    /// Persist events + annotations + refresh meta counts.
    static func save(_ session: inout SessionRecord) {
        let enc = JSONEncoder()
        var lines: [String] = []
        for (idx, var ev) in session.events.enumerated() {
            ev.i = idx
            session.events[idx] = ev
            if let data = try? enc.encode(ev),
                let s = String(data: data, encoding: .utf8) {
                lines.append(s)
            }
        }
        try? lines.joined(separator: "\n").appending("\n")
            .write(
                to: session.dir.appendingPathComponent("events.jsonl"),
                atomically: true, encoding: .utf8
            )

        let annEnc = JSONEncoder()
        annEnc.dateEncodingStrategy = .iso8601
        var annLines: [String] = []
        for a in session.annotations {
            if let data = try? annEnc.encode(a),
                let s = String(data: data, encoding: .utf8) {
                annLines.append(s)
            }
        }
        try? annLines.joined(separator: "\n").appending("\n")
            .write(
                to: session.dir.appendingPathComponent("annotations.jsonl"),
                atomically: true, encoding: .utf8
            )

        // Patch meta counts if present.
        if var meta = session.meta {
            meta.eventCount = session.events.count
            meta.annotationCount = session.annotations.count
            session.meta = meta
            let metaEnc = JSONEncoder()
            metaEnc.outputFormatting = [.prettyPrinted, .sortedKeys]
            metaEnc.dateEncodingStrategy = .iso8601
            if let data = try? metaEnc.encode(meta) {
                try? data.write(to: session.dir.appendingPathComponent("meta.json"))
            }
        }
    }

    /// Export for clawagents_py fine-tuning. Includes annotations, region
    /// pins, grades, flags, and proposed better actions.
    @discardableResult
    static func exportFineTune(_ session: SessionRecord) -> URL? {
        var decisions: [Int: String] = [:]
        let replaysDir = session.dir.appendingPathComponent("replays")
        if let replays = try? FileManager.default.contentsOfDirectory(
            at: replaysDir, includingPropertiesForKeys: nil
        ) {
            let dec = JSONDecoder()
            dec.dateDecodingStrategy = .iso8601
            for r in replays.sorted(by: { $0.path < $1.path }) {
                if let data = try? Data(
                    contentsOf: r.appendingPathComponent("report.json")
                ),
                    let report = try? dec.decode(ReplayReport.self, from: data) {
                    for step in report.steps {
                        decisions[step.i] = step.decision
                    }
                }
            }
        }

        let annsByStep = Dictionary(grouping: session.annotations) {
            $0.stepIndex ?? -1
        }

        var lines: [String] = []
        for ev in session.events {
            var action: [String: Any] = ["kind": ev.kind]
            if let fx = ev.fx { action["fx"] = fx }
            if let fy = ev.fy { action["fy"] = fy }
            if let text = ev.text { action["text"] = text }
            if let key = ev.key { action["key"] = key }
            if let dy = ev.dy { action["dy"] = dy }
            if let label = ev.label { action["label"] = label }
            if ev.inserted == true { action["inserted"] = true }

            var obj: [String: Any] = [
                "session": session.dir.lastPathComponent,
                "step": ev.i,
                "t": ev.t,
                "action": action,
                "schema": "powerchart_pilot.v1",
            ]
            if let frame = ev.frame {
                obj["image"] = "frames/\(frame)"
            }
            if let w = session.meta?.window {
                obj["window"] = [
                    "title": w.title, "owner": w.owner, "w": w.w, "h": w.h,
                ]
            }
            if let ct = session.meta?.captureTarget {
                obj["capture_target"] = ct
            }
            if let d = decisions[ev.i] {
                obj["human_decision"] = d
            }
            let stepAnns = annsByStep[ev.i] ?? []
            if !stepAnns.isEmpty {
                obj["annotations"] = stepAnns.map { annJSON($0) }
            }
            if let data = try? JSONSerialization.data(
                withJSONObject: obj, options: [.sortedKeys]
            ),
                let s = String(data: data, encoding: .utf8) {
                lines.append(s)
            }
        }

        // Session-level annotations (no step).
        for a in annsByStep[-1] ?? [] {
            let obj: [String: Any] = [
                "session": session.dir.lastPathComponent,
                "step": NSNull(),
                "schema": "powerchart_pilot.v1",
                "annotations": [annJSON(a)],
            ]
            if let data = try? JSONSerialization.data(
                withJSONObject: obj, options: [.sortedKeys]
            ),
                let s = String(data: data, encoding: .utf8) {
                lines.append(s)
            }
        }

        let out = session.dir.appendingPathComponent("samples.jsonl")
        do {
            try lines.joined(separator: "\n").appending("\n")
                .write(to: out, atomically: true, encoding: .utf8)
            // Also write a clawagents-friendly manifest.
            let manifest: [String: Any] = [
                "schema": "powerchart_pilot.v1",
                "session": session.dir.lastPathComponent,
                "samples": "samples.jsonl",
                "events": "events.jsonl",
                "annotations": "annotations.jsonl",
                "event_count": session.events.count,
                "annotation_count": session.annotations.count,
                "clawagents_hint":
                    "Load samples.jsonl as multimodal (image, action, feedback) pairs.",
            ]
            if let data = try? JSONSerialization.data(
                withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys]
            ) {
                try? data.write(
                    to: session.dir.appendingPathComponent("clawagents_manifest.json")
                )
            }
            return out
        } catch {
            return nil
        }
    }

    private static func annJSON(_ a: StepAnnotation) -> [String: Any] {
        var d: [String: Any] = [
            "id": a.id,
            "kind": a.kind,
            "body": a.body,
        ]
        if let step = a.stepIndex { d["step_index"] = step }
        if let flag = a.flagType { d["flag_type"] = flag }
        if let g = a.grade { d["grade"] = g }
        if let p = a.proposedAction { d["proposed_action"] = p }
        if let r = a.region {
            var rd: [String: Any] = [
                "fx": r.fx, "fy": r.fy, "fw": r.fw, "fh": r.fh,
            ]
            if let h = r.targetHint { rd["target_hint"] = h }
            d["region"] = rd
        }
        return d
    }
}
