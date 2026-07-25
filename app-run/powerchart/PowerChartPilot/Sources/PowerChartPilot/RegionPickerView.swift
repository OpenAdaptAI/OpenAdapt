import AppKit
import SwiftUI

/// Click-drag a rectangle on a step screenshot (health-priority DesignPin
/// adapted to native frames). Emits a RegionPin in frame-fraction coords.
struct RegionPickerView: View {
    let imageURL: URL?
    @Binding var region: RegionPin?
    var onPicked: ((RegionPin) -> Void)?

    @State private var dragStart: CGPoint?
    @State private var dragCurrent: CGPoint?
    @State private var imageSize: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            ZStack {
                if let imageURL, let ns = NSImage(contentsOf: imageURL) {
                    Image(nsImage: ns)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .background(
                            GeometryReader { g in
                                Color.clear.preference(
                                    key: SizeKey.self, value: g.size
                                )
                            }
                        )
                        .onPreferenceChange(SizeKey.self) { imageSize = $0 }
                        .gesture(
                            DragGesture(minimumDistance: 4)
                                .onChanged { val in
                                    if dragStart == nil {
                                        dragStart = val.startLocation
                                    }
                                    dragCurrent = val.location
                                }
                                .onEnded { val in
                                    defer {
                                        dragStart = nil
                                        dragCurrent = nil
                                    }
                                    guard imageSize.width > 0, imageSize.height > 0
                                    else { return }
                                    let a = val.startLocation
                                    let b = val.location
                                    let x0 = min(a.x, b.x)
                                    let y0 = min(a.y, b.y)
                                    let x1 = max(a.x, b.x)
                                    let y1 = max(a.y, b.y)
                                    // Fit-centered image rect inside geo.
                                    let fit = fittedRect(
                                        imageSize: imageSize, in: geo.size
                                    )
                                    let fx = (x0 - fit.minX) / fit.width
                                    let fy = (y0 - fit.minY) / fit.height
                                    let fw = (x1 - x0) / fit.width
                                    let fh = (y1 - y0) / fit.height
                                    let pin = RegionPin(
                                        fx: clamp01(fx),
                                        fy: clamp01(fy),
                                        fw: max(0.01, min(1, fw)),
                                        fh: max(0.01, min(1, fh)),
                                        targetHint: nil
                                    )
                                    region = pin
                                    onPicked?(pin)
                                }
                        )

                    // Existing / live selection overlay
                    if let r = liveRect(in: geo.size) ?? storedRect(in: geo.size) {
                        Rectangle()
                            .stroke(Color.accentColor, lineWidth: 2)
                            .background(Color.accentColor.opacity(0.15))
                            .frame(width: r.width, height: r.height)
                            .position(x: r.midX, y: r.midY)
                    }
                } else {
                    Text("No screenshot for this step")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
    }

    private func liveRect(in container: CGSize) -> CGRect? {
        guard let a = dragStart, let b = dragCurrent else { return nil }
        return CGRect(
            x: min(a.x, b.x), y: min(a.y, b.y),
            width: abs(a.x - b.x), height: abs(a.y - b.y)
        )
    }

    private func storedRect(in container: CGSize) -> CGRect? {
        guard let region, imageSize.width > 0 else { return nil }
        let fit = fittedRect(imageSize: imageSize, in: container)
        return CGRect(
            x: fit.minX + region.fx * fit.width,
            y: fit.minY + region.fy * fit.height,
            width: region.fw * fit.width,
            height: region.fh * fit.height
        )
    }

    private func fittedRect(imageSize: CGSize, in container: CGSize) -> CGRect {
        let scale = min(
            container.width / max(imageSize.width, 1),
            container.height / max(imageSize.height, 1)
        )
        let w = imageSize.width * scale
        let h = imageSize.height * scale
        return CGRect(
            x: (container.width - w) / 2,
            y: (container.height - h) / 2,
            width: w, height: h
        )
    }

    private func clamp01(_ v: Double) -> Double { min(1, max(0, v)) }
}

private struct SizeKey: PreferenceKey {
    static var defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
        value = nextValue()
    }
}
