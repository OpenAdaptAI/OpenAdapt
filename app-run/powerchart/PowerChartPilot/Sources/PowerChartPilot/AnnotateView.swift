import AppKit
import SwiftUI

/// Flexible annotation workbench — ActionDock pattern from health-priority
/// (Comment / Flag / Grade / Pin region / Better / Re-record) plus insert,
/// delete, and reorder of steps for clawagents fine-tuning.
struct AnnotateView: View {
    @State private var sessions: [SessionRecord] = []
    @State private var session: SessionRecord?
    @State private var selectedStep: Int?
    @State private var mode: AnnotationKind?
    @State private var draftBody = ""
    @State private var draftFlag = FlagTypes.all[0]
    @State private var draftGrade = 3
    @State private var draftProposed = ""
    @State private var draftRegion: RegionPin?
    @State private var pickingRegion = false
    @State private var status = ""
    @State private var insertLabel = ""

    var body: some View {
        HSplitView {
            // Sessions + steps
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Picker("Session", selection: $session) {
                        Text("Choose…").tag(SessionRecord?.none)
                        ForEach(sessions) { s in
                            Text(
                                "\(s.name) · \(s.events.count) steps · \(s.annotations.count) notes"
                            )
                            .tag(SessionRecord?.some(s))
                        }
                    }
                    Button {
                        sessions = SessionStore.list()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }

                if let session {
                    List(selection: $selectedStep) {
                        ForEach(session.events) { ev in
                            stepRow(session: session, ev: ev)
                        }
                        .onMove(perform: moveSteps)
                    }
                    .onDeleteCommand(perform: deleteSelected)
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "tray")
                            .font(.largeTitle)
                            .foregroundStyle(.secondary)
                        Text("No session").font(.headline)
                        Text("Record a demo first, then annotate here.")
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 280)
            .padding()

            // Detail + dock
            VStack(spacing: 10) {
                if let session, let idx = selectedStep,
                    let ev = session.events.first(where: { $0.i == idx }) {
                    stepDetail(session: session, ev: ev)
                } else {
                    Text("Select a step to annotate, pin a region, or insert a new one.")
                        .foregroundStyle(.secondary)
                        .frame(maxHeight: .infinity)
                }

                actionDock
                if !status.isEmpty {
                    Text(status).font(.caption).foregroundStyle(.green)
                }
            }
            .frame(minWidth: 420)
            .padding()
        }
        .onAppear { sessions = SessionStore.list() }
        .onChange(of: session?.id) { _ in
            selectedStep = session?.events.first?.i
            mode = nil
            pickingRegion = false
        }
        .sheet(item: $mode) { kind in
            annotationSheet(kind)
        }
    }

    // MARK: rows

    @ViewBuilder
    private func stepRow(session: SessionRecord, ev: RecordedEvent) -> some View {
        let n = session.annotations.filter { $0.stepIndex == ev.i }.count
        HStack(alignment: .top, spacing: 8) {
            Text("\(ev.i)")
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 28, alignment: .trailing)
            VStack(alignment: .leading, spacing: 2) {
                Text(ev.summary).lineLimit(2)
                if n > 0 {
                    Text("\(n) annotation\(n == 1 ? "" : "s")")
                        .font(.caption2)
                        .foregroundStyle(.blue)
                }
                if ev.inserted == true {
                    Text("inserted")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
        }
        .tag(ev.i)
    }

    // MARK: detail

    @ViewBuilder
    private func stepDetail(session: SessionRecord, ev: RecordedEvent) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Step \(ev.i): \(ev.summary)").font(.headline)

            if pickingRegion {
                Text("Drag on the screenshot to pin a region (Esc cancels via Cancel).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                RegionPickerView(
                    imageURL: frameURL(session: session, ev: ev),
                    region: $draftRegion
                ) { pin in
                    draftRegion = pin
                }
                .frame(maxHeight: 280)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                HStack {
                    Button("Cancel pin") {
                        pickingRegion = false
                        draftRegion = nil
                    }
                    Button("Save pin + comment") {
                        mode = .pin
                    }
                    .disabled(draftRegion == nil)
                    .keyboardShortcut(.return, modifiers: [])
                }
            } else if let url = frameURL(session: session, ev: ev),
                let img = NSImage(contentsOf: url) {
                ZStack(alignment: .topLeading) {
                    Image(nsImage: img)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxHeight: 240)
                    // Draw existing pin regions
                    ForEach(
                        session.annotations.filter {
                            $0.stepIndex == ev.i && $0.region != nil
                        }
                    ) { ann in
                        if let r = ann.region {
                            GeometryReader { geo in
                                let rect = CGRect(
                                    x: r.fx * geo.size.width,
                                    y: r.fy * geo.size.height,
                                    width: r.fw * geo.size.width,
                                    height: r.fh * geo.size.height
                                )
                                Rectangle()
                                    .stroke(Color.orange, lineWidth: 2)
                                    .background(Color.orange.opacity(0.12))
                                    .frame(width: rect.width, height: rect.height)
                                    .position(x: rect.midX, y: rect.midY)
                            }
                            .frame(maxHeight: 240)
                        }
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            }

            // Existing annotations for this step
            let anns = session.annotations.filter { $0.stepIndex == ev.i }
            if !anns.isEmpty {
                GroupBox("Annotations") {
                    ForEach(anns) { a in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(a.kind.uppercased())
                                    .font(.caption2).bold()
                                    .foregroundStyle(.secondary)
                                Text(a.body)
                                if let g = a.grade {
                                    Text("Grade \(g)/5").font(.caption)
                                }
                                if let f = a.flagType {
                                    Text("Flag: \(f)").font(.caption)
                                }
                                if let r = a.region {
                                    Text(r.summary).font(.caption2)
                                        .foregroundStyle(.orange)
                                }
                            }
                            Spacer()
                            Button(role: .destructive) {
                                removeAnnotation(a)
                            } label: {
                                Image(systemName: "trash")
                            }
                            .buttonStyle(.borderless)
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
            Spacer(minLength: 0)
        }
    }

    // MARK: action dock (health-priority style)

    private var actionDock: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Text("Annotate selected step")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    ForEach(
                        [
                            AnnotationKind.comment, .flag, .grade, .pin,
                            .better, .rerecord,
                        ]
                    ) { k in
                        Button(k.label) {
                            if k == .pin {
                                pickingRegion = true
                                draftRegion = nil
                            } else {
                                mode = k
                                draftBody = ""
                                draftProposed = ""
                            }
                        }
                        .disabled(selectedStep == nil && k != .comment)
                    }
                }
                Divider()
                HStack {
                    TextField("Insert step label…", text: $insertLabel)
                        .frame(maxWidth: 220)
                    Button("Insert below") { insertStep(after: selectedStep) }
                        .disabled(session == nil)
                    Button("Insert note") {
                        insertNote(at: selectedStep)
                    }
                    .disabled(session == nil)
                    Spacer()
                    Button("Export for clawagents") {
                        if let s = session,
                            SessionStore.exportFineTune(s) != nil {
                            status = "Exported samples.jsonl + clawagents_manifest.json"
                        }
                    }
                    .disabled(session == nil)
                }
            }
            .padding(4)
        }
    }

    // MARK: sheets

    @ViewBuilder
    private func annotationSheet(_ kind: AnnotationKind) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(kind.label).font(.title2.bold())
            switch kind {
            case .flag:
                Picker("Flag type", selection: $draftFlag) {
                    ForEach(FlagTypes.all, id: \.self) { Text($0).tag($0) }
                }
            case .grade:
                Stepper("Grade: \(draftGrade)/5", value: $draftGrade, in: 1...5)
            case .better:
                Text("Preferred action the agent should have taken:")
                    .font(.caption)
                TextEditor(text: $draftProposed)
                    .frame(height: 80)
                    .border(.quaternary)
            case .rerecord:
                Text(
                    "Mark this step for partial re-record. clawagents can use this as a repair target."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            case .pin:
                if let r = draftRegion {
                    Text("Region: \(r.summary)").font(.caption)
                }
            default:
                EmptyView()
            }
            Text("Details")
            TextEditor(text: $draftBody)
                .frame(minHeight: 100)
                .border(.quaternary)
            HStack {
                Button("Cancel") { mode = nil }
                Spacer()
                Button("Save") { saveAnnotation(kind) }
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(draftBody.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        && kind != .rerecord && kind != .better)
            }
        }
        .padding()
        .frame(width: 440)
    }

    // MARK: mutations

    private func saveAnnotation(_ kind: AnnotationKind) {
        guard var s = session else { return }
        var body = draftBody.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.isEmpty {
            switch kind {
            case .rerecord: body = "Re-record requested"
            case .better: body = draftProposed
            case .pin: body = "Pinned region"
            default: return
            }
        }
        var region = draftRegion
        if kind == .pin, region == nil { return }
        if kind != .pin { region = nil }

        let ann = StepAnnotation.make(
            stepIndex: selectedStep,
            kind: kind,
            body: body,
            flagType: kind == .flag ? draftFlag : nil,
            grade: kind == .grade ? draftGrade : nil,
            region: region,
            proposedAction: kind == .better ? draftProposed : nil
        )
        s.annotations.append(ann)
        SessionStore.save(&s)
        session = s
        refreshList(keeping: s.dir)
        mode = nil
        pickingRegion = false
        draftRegion = nil
        status = "Saved \(kind.label.lowercased())"
    }

    private func removeAnnotation(_ a: StepAnnotation) {
        guard var s = session else { return }
        s.annotations.removeAll { $0.id == a.id }
        SessionStore.save(&s)
        session = s
        refreshList(keeping: s.dir)
    }

    private func insertStep(after index: Int?) {
        guard var s = session else { return }
        let at = (index ?? s.events.count - 1) + 1
        let label = insertLabel.isEmpty ? "Manual step" : insertLabel
        let ev = RecordedEvent(
            i: at, kind: "manual", t: Double(at),
            x: nil, y: nil, fx: nil, fy: nil,
            text: label, key: nil, dy: nil, frame: nil,
            label: label, inserted: true
        )
        s.events.insert(ev, at: min(at, s.events.count))
        SessionStore.save(&s)
        session = s
        selectedStep = at
        insertLabel = ""
        refreshList(keeping: s.dir)
        status = "Inserted step \(at)"
    }

    private func insertNote(at index: Int?) {
        guard var s = session else { return }
        let at = index ?? s.events.count
        let ev = RecordedEvent(
            i: at, kind: "note", t: Double(at),
            x: nil, y: nil, fx: nil, fy: nil,
            text: insertLabel.isEmpty ? "Note" : insertLabel,
            key: nil, dy: nil, frame: nil,
            label: nil, inserted: true
        )
        s.events.insert(ev, at: min(at, s.events.count))
        SessionStore.save(&s)
        session = s
        selectedStep = at
        insertLabel = ""
        refreshList(keeping: s.dir)
    }

    private func deleteSelected() {
        guard var s = session, let idx = selectedStep,
            let pos = s.events.firstIndex(where: { $0.i == idx })
        else { return }
        s.events.remove(at: pos)
        // Drop annotations for deleted step; shift later ones.
        s.annotations = s.annotations.compactMap { a in
            guard let si = a.stepIndex else { return a }
            if si == idx { return nil }
            var copy = a
            if si > idx { copy.stepIndex = si - 1 }
            return copy
        }
        SessionStore.save(&s)
        session = s
        selectedStep = s.events.first?.i
        refreshList(keeping: s.dir)
    }

    private func moveSteps(from: IndexSet, to: Int) {
        guard var s = session else { return }
        s.events.move(fromOffsets: from, toOffset: to)
        SessionStore.save(&s)
        session = s
        refreshList(keeping: s.dir)
    }

    private func refreshList(keeping dir: URL) {
        sessions = SessionStore.list()
        session = sessions.first { $0.dir == dir }
    }

    private func frameURL(session: SessionRecord, ev: RecordedEvent) -> URL? {
        guard let frame = ev.frame else { return nil }
        return session.dir.appendingPathComponent("frames/\(frame)")
    }
}
