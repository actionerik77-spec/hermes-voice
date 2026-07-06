import AppKit
import Combine
import SwiftUI

// Borderless non-activating floating NSPanel — the FluidVoice pill-window recipe.
// Draggable anywhere on its surface; remembers its spot; resizes anchored to its
// own bottom-center so it grows upward in place, with a smooth animated frame.
final class PillPanelController {
    private let panel: NSPanel
    private let hosting: NSHostingView<PillView>
    private let state: AppState
    private var cancellable: AnyCancellable?
    private var moveObserver: NSObjectProtocol?
    private var lastSize: NSSize = .zero
    private var lastTierKey = -1
    private var programmaticMove = false

    private let anchorURL = URL(fileURLWithPath: NSHomeDirectory() + "/.hermes/voicepill/hermesvoice_anchor.json")

    init(state: AppState) {
        self.state = state
        hosting = NSHostingView(rootView: PillView(state: state))

        panel = NSPanel(
            contentRect: .zero,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false // SwiftUI draws the glow
        panel.hidesOnDeactivate = false
        panel.animationBehavior = .none
        panel.isMovableByWindowBackground = true
        panel.contentView = hosting

        moveObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification, object: panel, queue: .main
        ) { [weak self] _ in
            guard let self, !self.programmaticMove else { return }
            self.saveAnchor()
        }

        cancellable = state.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                DispatchQueue.main.async { self?.layout() }
            }

        layout()
        panel.orderFrontRegardless()
    }

    deinit {
        if let moveObserver { NotificationCenter.default.removeObserver(moveObserver) }
    }

    // MARK: anchor persistence (bottom-center point of the pill)

    private func saveAnchor() {
        let f = panel.frame
        let anchor = ["x": f.midX, "y": f.minY]
        if let data = try? JSONSerialization.data(withJSONObject: anchor) {
            try? FileManager.default.createDirectory(
                at: anchorURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: anchorURL)
        }
    }

    private func loadAnchor() -> NSPoint? {
        guard let data = try? Data(contentsOf: anchorURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Double],
              let x = obj["x"], let y = obj["y"] else { return nil }
        return NSPoint(x: x, y: y)
    }

    private func currentAnchor() -> NSPoint {
        if lastSize != .zero {
            let f = panel.frame
            return NSPoint(x: f.midX, y: f.minY)
        }
        if let saved = loadAnchor() {
            return saved
        }
        let frame = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame ?? .zero
        return NSPoint(x: frame.midX, y: frame.minY + 28)
    }

    // MARK: layout

    private func layout() {
        let size = hosting.fittingSize
        guard size.width > 0, size.height > 0 else { return }
        let grewOrShrunk = abs(size.width - lastSize.width) > 1 || abs(size.height - lastSize.height) > 1
        guard grewOrShrunk else { return }

        let anchor = currentAnchor()
        var origin = NSPoint(x: anchor.x - size.width / 2, y: anchor.y)

        // Clamp to the screen the pill currently lives on.
        let screen = panel.screen ?? NSScreen.main ?? NSScreen.screens.first
        if let visible = screen?.visibleFrame {
            origin.x = max(visible.minX + 4, min(origin.x, visible.maxX - size.width - 4))
            origin.y = max(visible.minY + 4, min(origin.y, visible.maxY - size.height - 4))
        }

        let firstLayout = lastSize == .zero
        lastSize = size

        // Animate the window only on tier transitions (open/close feel);
        // text-driven growth inside a tier resizes INSTANTLY so streaming
        // reply text never sits clipped waiting for a 0.3 s window animation.
        let tierKey: Int
        switch state.tier {
        case .idle: tierKey = 0
        case .bar: tierKey = 1
        case .full: tierKey = 2
        }
        let tierChanged = tierKey != lastTierKey
        lastTierKey = tierKey

        programmaticMove = true
        panel.setFrame(NSRect(origin: origin, size: size), display: true, animate: tierChanged && !firstLayout)
        // setFrame(animate:) runs its own short animation; release the guard after it.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.programmaticMove = false
        }
    }
}
