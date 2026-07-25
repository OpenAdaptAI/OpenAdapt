import AppKit
import SwiftUI

struct ContentView: View {
    @StateObject private var recorder = RecorderEngine()
    @StateObject private var replayer = ReplayEngine()
    @StateObject private var countdown = CountdownController()
    private let banner = RecordingBannerController()

    var body: some View {
        TabView {
            PermissionsView()
                .tabItem { Label("Setup", systemImage: "checkmark.shield") }
            RecordView(
                recorder: recorder,
                countdown: countdown,
                banner: banner
            )
            .tabItem { Label("Record", systemImage: "record.circle") }
            AnnotateView()
                .tabItem { Label("Annotate", systemImage: "pin.circle") }
            ReplayView(replayer: replayer)
                .tabItem { Label("Replay", systemImage: "play.circle") }
            SessionsView()
                .tabItem { Label("Sessions", systemImage: "tray.full") }
        }
        .frame(minWidth: 860, minHeight: 600)
    }
}

// MARK: - Setup

struct PermissionsView: View {
    @State private var status = Permissions.check()
    private let timer = Timer.publish(every: 2, on: .main, in: .common)
        .autoconnect()

    var body: some View {
        Form {
            Section("Required permissions") {
                permissionRow(
                    "Accessibility (post clicks/keys during replay)",
                    granted: status.accessibility,
                    request: { Permissions.requestAccessibility() },
                    pane: "accessibility"
                )
                permissionRow(
                    "Input Monitoring (listen during recording + Esc to stop)",
                    granted: status.inputMonitoring,
                    request: { Permissions.requestInputMonitoring() },
                    pane: "input"
                )
                permissionRow(
                    "Screen Recording (window / full-screen screenshots)",
                    granted: status.screenRecording,
                    request: { Permissions.requestScreenRecording() },
                    pane: "screen"
                )
            }
            Section {
                Text(
                    status.allGranted
                        ? "All set. Record → Annotate (pin / flag / insert) → Replay → Export for clawagents."
                        : "Grant each permission, then relaunch if a row stays red."
                )
                .foregroundStyle(status.allGranted ? .green : .secondary)
            }
        }
        .formStyle(.grouped)
        .padding()
        .onReceive(timer) { _ in status = Permissions.check() }
    }

    @ViewBuilder
    private func permissionRow(
        _ title: String, granted: Bool,
        request: @escaping () -> Void, pane: String
    ) -> some View {
        HStack {
            Image(
                systemName: granted
                    ? "checkmark.circle.fill" : "xmark.circle.fill"
            )
            .foregroundStyle(granted ? .green : .red)
            Text(title)
            Spacer()
            if !granted {
                Button("Request") { request() }
                Button("Open Settings") { Permissions.openSettings(pane: pane) }
            }
        }
    }
}

// MARK: - Record (countdown + window/screen + Esc)

struct RecordView: View {
    @ObservedObject var recorder: RecorderEngine
    @ObservedObject var countdown: CountdownController
    let banner: RecordingBannerController

    @State private var ownerFilter = "Citrix Viewer"
    @State private var sessionName = "powerchart-open-patient"
    @State private var notes = ""
    @State private var captureTarget: CaptureTarget = .window
    @State private var detected: TargetWindow?
    @State private var windows: [TargetWindow] = []
    @State private var sessionDir: URL?
    @State private var savedTo: String?
    @State private var countingDown = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox("Capture target") {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Record", selection: $captureTarget) {
                        ForEach(CaptureTarget.allCases) { t in
                            Text(t.label).tag(t)
                        }
                    }
                    .pickerStyle(.segmented)
                    .disabled(recorder.isRecording || countingDown)

                    if captureTarget == .window {
                        HStack {
                            TextField("App name contains…", text: $ownerFilter)
                                .frame(maxWidth: 200)
                            Button("Refresh windows") {
                                windows = WindowLocator.windows(
                                    ownerContains: ownerFilter
                                )
                                detected = windows.first
                            }
                        }
                        if windows.isEmpty {
                            Text("No matching windows — open Citrix/PowerChart, then Refresh.")
                                .foregroundStyle(.secondary)
                        } else {
                            Picker("Window", selection: $detected) {
                                ForEach(windows) { w in
                                    Text(
                                        w.title.isEmpty
                                            ? "\(w.owner) (untitled)"
                                            : "\(w.owner): \(w.title)"
                                    )
                                    .tag(TargetWindow?.some(w))
                                }
                            }
                        }
                    } else {
                        Text("Entire screen will be captured. Clicks anywhere (except this app) are recorded.")
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(4)
            }

            GroupBox("Session") {
                HStack {
                    TextField("Session name", text: $sessionName)
                        .frame(maxWidth: 260)
                    TextField("Notes (optional)", text: $notes)
                }
                .padding(4)
            }

            HStack(spacing: 12) {
                if recorder.isRecording {
                    Button(role: .destructive) {
                        finishRecording()
                    } label: {
                        Label("Stop & Save (or press Esc)", systemImage: "stop.circle.fill")
                    }
                    .controlSize(.large)
                } else if countingDown {
                    ProgressView("Starting…")
                } else {
                    Button {
                        beginWithCountdown()
                    } label: {
                        Label("Start Recording", systemImage: "record.circle.fill")
                    }
                    .controlSize(.large)
                    .disabled(!canStart)
                }
                Text(recorder.statusText).foregroundStyle(.secondary)
                if let savedTo {
                    Text("Saved: \(savedTo)").foregroundStyle(.green)
                }
            }

            Text("Flow: choose window or screen → 3-2-1 countdown → demonstrate → Esc to stop → Annotate tab for pins / flags / inserts.")
                .font(.caption)
                .foregroundStyle(.secondary)

            GroupBox("Recorded events (\(recorder.events.count))") {
                List(recorder.events) { ev in
                    HStack {
                        Text("\(ev.i)").monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 30, alignment: .trailing)
                        Text(ev.summary)
                        Spacer()
                        Text(String(format: "t+%.1fs", ev.t))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
                .frame(minHeight: 200)
            }
            Spacer()
        }
        .padding()
        .onAppear {
            windows = WindowLocator.windows(ownerContains: ownerFilter)
            detected = windows.first
            recorder.onStoppedByEscape = { finishRecording() }
        }
    }

    private var canStart: Bool {
        switch captureTarget {
        case .screen: return true
        case .window: return detected != nil
        }
    }

    private func beginWithCountdown() {
        savedTo = nil
        let dir = SessionStore.newSessionDir(name: sessionName)
        sessionDir = dir
        countingDown = true

        // Hide our main window so it isn't in the way / screenshots.
        NSApp.windows.first { $0.isKeyWindow }?.miniaturize(nil)

        countdown.run { [self] in
            countingDown = false
            let ok = recorder.start(
                target: captureTarget == .window ? detected : nil,
                captureTarget: captureTarget,
                sessionDir: dir
            )
            if ok {
                banner.show()
            } else {
                banner.hide()
                NSApp.activate(ignoringOtherApps: true)
            }
        }
    }

    private func finishRecording() {
        banner.hide()
        recorder.stop()
        if recorder.save(name: sessionName, notes: notes) != nil {
            savedTo = sessionDir?.lastPathComponent
        }
        NSApp.activate(ignoringOtherApps: true)
        for w in NSApp.windows where w.isMiniaturized {
            w.deminiaturize(nil)
        }
    }
}

// MARK: - Replay

struct ReplayView: View {
    @ObservedObject var replayer: ReplayEngine
    @State private var sessions: [SessionRecord] = []
    @State private var selected: SessionRecord?
    @State private var ownerFilter = "Citrix Viewer"

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Picker("Session", selection: $selected) {
                        Text("Choose…").tag(SessionRecord?.none)
                        ForEach(sessions) { s in
                            Text("\(s.name) (\(s.events.count) steps)")
                                .tag(SessionRecord?.some(s))
                        }
                    }
                    Button {
                        sessions = SessionStore.list()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
                TextField("Target app contains…", text: $ownerFilter)

                Button {
                    guard
                        let s = selected,
                        let target = WindowLocator.best(ownerContains: ownerFilter)
                    else { return }
                    replayer.begin(session: s, target: target)
                } label: {
                    Label("Start Turn-by-Turn Replay", systemImage: "play.fill")
                }
                .controlSize(.large)
                .disabled(selected == nil)

                List(replayer.events.filter(\.isReplayable)) { ev in
                    HStack {
                        stepIcon(ev.i)
                        Text("\(ev.i)").monospacedDigit()
                            .frame(width: 26, alignment: .trailing)
                            .foregroundStyle(.secondary)
                        Text(ev.summary).lineLimit(1)
                    }
                    .listRowBackground(
                        replayer.currentStep == ev.i
                            ? Color.accentColor.opacity(0.18) : Color.clear
                    )
                }
            }
            .frame(minWidth: 300)
            .padding()

            VStack(spacing: 12) {
                switch replayer.state {
                case .idle:
                    Text("Pick a session and start replay.")
                        .foregroundStyle(.secondary)
                        .frame(maxHeight: .infinity)
                case .waitingConfirm(let step):
                    confirmPanel(step: step)
                case .running(let step):
                    ProgressView("Running step \(step)…")
                        .frame(maxHeight: .infinity)
                case .finished(let outcome):
                    VStack(spacing: 8) {
                        Image(
                            systemName: outcome == "completed"
                                ? "checkmark.seal.fill"
                                : "exclamationmark.triangle.fill"
                        )
                        .font(.largeTitle)
                        .foregroundStyle(
                            outcome == "completed" ? .green : .orange
                        )
                        Text("Replay \(outcome)")
                    }
                    .frame(maxHeight: .infinity)
                }

                GroupBox("Log") {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(
                                Array(replayer.log.enumerated()), id: \.offset
                            ) { _, line in
                                Text(line).font(.caption).monospaced()
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 140)
                }
            }
            .frame(minWidth: 360)
            .padding()
        }
        .onAppear { sessions = SessionStore.list() }
    }

    @ViewBuilder
    private func confirmPanel(step: Int) -> some View {
        let all = replayer.events
        if step < all.count {
            let ev = all[step]
            VStack(spacing: 10) {
                Text("Step \(step + 1) of \(all.count)").font(.headline)
                Text(ev.summary).font(.title3)
                if !ev.isReplayable {
                    Text("Non-replayable (note/manual) — skip or abort.")
                        .foregroundStyle(.orange)
                }
                if let frameName = ev.frame,
                    let session = replayer.session,
                    let img = NSImage(
                        contentsOf: session.dir.appendingPathComponent(
                            "frames/\(frameName)"
                        )
                    ) {
                    Image(nsImage: img)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxHeight: 240)
                }
                HStack(spacing: 12) {
                    Button {
                        if ev.isReplayable {
                            replayer.confirmRun()
                        } else {
                            replayer.skip()
                        }
                    } label: {
                        Label(
                            ev.isReplayable ? "Run Step" : "Skip note",
                            systemImage: "play.fill"
                        )
                    }
                    .keyboardShortcut(.return, modifiers: [])
                    .controlSize(.large)
                    Button("Skip") { replayer.skip() }
                    Button(role: .destructive) { replayer.abort() } label: {
                        Text("Abort")
                    }
                }
                Toggle("Auto-run remaining steps", isOn: $replayer.autoRun)
                if replayer.autoRun {
                    HStack {
                        Text("Delay")
                        Slider(value: $replayer.autoDelay, in: 0.3...3.0)
                            .frame(width: 160)
                        Text(String(format: "%.1fs", replayer.autoDelay))
                            .monospacedDigit()
                    }
                    .onAppear {
                        if case .waitingConfirm = replayer.state {
                            replayer.confirmRun()
                        }
                    }
                }
            }
            .frame(maxHeight: .infinity)
        } else {
            Text("Invalid step").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func stepIcon(_ i: Int) -> some View {
        if let r = replayer.results.first(where: { $0.i == i }) {
            switch r.decision {
            case "ran":
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case "skipped":
                Image(systemName: "arrow.right.circle").foregroundStyle(.orange)
            default:
                Image(systemName: "xmark.circle").foregroundStyle(.red)
            }
        } else {
            Image(systemName: "circle").foregroundStyle(.quaternary)
        }
    }
}

// MARK: - Sessions

struct SessionsView: View {
    @State private var sessions: [SessionRecord] = []
    @State private var exported: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Saved sessions").font(.headline)
                Spacer()
                if let exported {
                    Text("Exported: \(exported)").foregroundStyle(.green)
                }
                Button {
                    sessions = SessionStore.list()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                Button("Open Folder") {
                    NSWorkspace.shared.open(SessionStore.root)
                }
            }
            List(sessions) { s in
                HStack {
                    VStack(alignment: .leading) {
                        Text(s.name)
                        Text(
                            "\(s.events.count) events · \(s.annotations.count) annotations · "
                                + (s.meta?.captureTarget ?? "window")
                                + " · \(s.meta?.host ?? "?")"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Export clawagents JSONL") {
                        if SessionStore.exportFineTune(s) != nil {
                            exported = s.name
                        }
                    }
                    Button {
                        NSWorkspace.shared.activateFileViewerSelecting([s.dir])
                    } label: {
                        Image(systemName: "folder")
                    }
                    Button(role: .destructive) {
                        SessionStore.delete(s)
                        sessions = SessionStore.list()
                    } label: {
                        Image(systemName: "trash")
                    }
                }
            }
        }
        .padding()
        .onAppear { sessions = SessionStore.list() }
    }
}
