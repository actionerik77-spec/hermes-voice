import Foundation
import SwiftUI

enum PillPhase: Equatable {
    case idle
    case listening
    case transcribing
    case thinking
    case streaming
    case speaking
    case error(String)

    var label: String {
        switch self {
        case .idle: return "ARMED"
        case .listening: return "" // no badge while recording — the waveform IS the signal
        case .transcribing: return "TRANSCRIBING"
        case .thinking: return "HERMES WORKING"
        case .streaming: return "HERMES REPLYING"
        case .speaking: return "HERMES SPEAKING"
        case .error: return "FAULT"
        }
    }
}

enum PillTier: Equatable {
    case idle // compact capsule
    case bar // slim activity bar: waveform / tool ticker
    case full // reply card, sized to the message
}

// All mutations are dispatched onto the main queue by callers.
final class AppState: ObservableObject {
    @Published var phase: PillPhase = .idle
    @Published var level: Float = 0
    @Published var transcript: String = ""
    @Published var reply: String = ""
    @Published var toolLog: [String] = [] // rolling last-3 tool calls
    @Published var statusLine: String = "press \\ to talk"
    @Published var hotkeyEnabled: Bool = true

    @Published var workingSeconds: Int = 0

    var workingClock: String {
        guard workingSeconds > 0 else { return "" }
        return String(format: "%d:%02d", workingSeconds / 60, workingSeconds % 60)
    }

    var lastToolLine: String { toolLog.last ?? "" }

    /// ▸ on start; the matching line flips to ✓ when the tool completes,
    /// so even sub-second calls stay visible in the log.
    func recordToolStart(name: String, context: String) {
        let ctx = context
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var line = "▸ \(name)"
        if !ctx.isEmpty {
            let clipped = ctx.count > 58 ? String(ctx.prefix(58)).trimmingCharacters(in: .whitespaces) + "…" : ctx
            line += " — \(clipped)"
        }
        toolLog.append(line)
        if toolLog.count > 3 { toolLog.removeFirst(toolLog.count - 3) }
    }

    func recordToolDone(name: String) {
        guard !name.isEmpty else { return }
        if let idx = toolLog.lastIndex(where: { $0.hasPrefix("▸ \(name)") }) {
            toolLog[idx] = "✓" + toolLog[idx].dropFirst()
        }
    }

    var tier: PillTier {
        switch phase {
        case .idle:
            return .idle
        case .error:
            return .full
        case .listening, .transcribing, .thinking:
            return .bar
        case .streaming, .speaking:
            // Tool activity stays in the slim bar; the pill opens up only once
            // reply text exists to show.
            return reply.isEmpty ? .bar : .full
        }
    }

    func reset() {
        phase = .idle
        level = 0
        toolLog = []
        workingSeconds = 0
        statusLine = "press \\ to talk"
    }
}
