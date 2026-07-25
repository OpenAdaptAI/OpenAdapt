import AppKit
import SwiftUI

/// Borderless floating panel: 3 → 2 → 1 → Go, then dismisses.
final class CountdownController: ObservableObject {
    @Published var digit: Int? = nil
    private var panel: NSPanel?
    private var hosting: NSHostingView<CountdownView>?

    func run(completion: @escaping () -> Void) {
        DispatchQueue.main.async { [self] in
            showPanel()
            digit = 3
            tick(from: 3, completion: completion)
        }
    }

    private func tick(from n: Int, completion: @escaping () -> Void) {
        digit = n
        if n == 0 {
            digit = nil
            hidePanel()
            completion()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [self] in
            tick(from: n - 1, completion: completion)
        }
    }

    private func showPanel() {
        if panel != nil { return }
        let view = CountdownView(controller: self)
        let host = NSHostingView(rootView: view)
        hosting = host
        let p = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 220, height: 220),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        p.isOpaque = false
        p.backgroundColor = .clear
        p.level = .floating
        p.hasShadow = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.contentView = host
        p.center()
        p.orderFrontRegardless()
        panel = p
    }

    private func hidePanel() {
        panel?.orderOut(nil)
        panel = nil
        hosting = nil
    }
}

struct CountdownView: View {
    @ObservedObject var controller: CountdownController

    var body: some View {
        ZStack {
            Circle()
                .fill(.black.opacity(0.72))
            if let d = controller.digit, d > 0 {
                Text("\(d)")
                    .font(.system(size: 96, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                    .transition(.scale.combined(with: .opacity))
            } else if controller.digit == 0 {
                Text("Go")
                    .font(.system(size: 56, weight: .bold, design: .rounded))
                    .foregroundStyle(.green)
            }
        }
        .frame(width: 200, height: 200)
        .animation(.easeInOut(duration: 0.2), value: controller.digit)
    }
}

/// Small always-on-top banner while recording: "Recording… Esc to stop".
final class RecordingBannerController {
    private var panel: NSPanel?

    func show() {
        DispatchQueue.main.async { [self] in
            hide()
            let label = NSTextField(labelWithString: "● Recording  ·  Esc to stop")
            label.font = .systemFont(ofSize: 13, weight: .semibold)
            label.textColor = .white
            label.alignment = .center
            label.frame = NSRect(x: 12, y: 8, width: 220, height: 20)

            let p = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 244, height: 36),
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            p.isOpaque = false
            p.backgroundColor = NSColor.systemRed.withAlphaComponent(0.92)
            p.level = .statusBar
            p.hasShadow = true
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.contentView = NSView(frame: p.contentRect(forFrameRect: p.frame))
            p.contentView?.addSubview(label)
            if let screen = NSScreen.main {
                let f = screen.visibleFrame
                p.setFrameOrigin(
                    NSPoint(x: f.midX - 122, y: f.maxY - 56)
                )
            }
            p.orderFrontRegardless()
            panel = p
        }
    }

    func hide() {
        panel?.orderOut(nil)
        panel = nil
    }
}
