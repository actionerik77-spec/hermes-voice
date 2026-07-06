import AppKit
import SwiftUI

// SWORD palette
let swordBg = Color(red: 0.02, green: 0.024, blue: 0.04)
let swordCyan = Color(red: 0, green: 0.83, blue: 1)
let swordCyan2 = Color(red: 0.47, green: 0.94, blue: 1)
let swordInk = Color(red: 0.937, green: 0.973, blue: 1)
let swordMuted = Color(red: 0.62, green: 0.706, blue: 0.788)
let swordRed = Color(red: 1, green: 0.302, blue: 0.427)
let swordGreen = Color(red: 0.337, green: 0.941, blue: 0.639)

struct PillView: View {
    @ObservedObject var state: AppState

    private var tierKey: Int {
        switch state.tier {
        case .idle: return 0
        case .bar: return 1
        case .full: return 2
        }
    }

    var body: some View {
        Group {
            switch state.tier {
            case .idle:
                compact.transition(.scale(scale: 0.9, anchor: .bottom).combined(with: .opacity))
            case .bar:
                bar.transition(.scale(scale: 0.88, anchor: .bottom).combined(with: .opacity))
            case .full:
                full.transition(.scale(scale: 0.94, anchor: .bottom).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.45, dampingFraction: 0.82), value: tierKey)
        .padding(22) // room for the glow inside the borderless panel
    }

    // MARK: idle

    private var compact: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(state.hotkeyEnabled ? swordCyan : swordMuted)
                .frame(width: 6, height: 6)
            Text("HERMES")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(1.5)
                .foregroundColor(swordMuted)
            Text("\\")
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundColor(swordCyan.opacity(0.85))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(capsuleBackground(borderOpacity: 0.22))
        .opacity(0.88)
    }

    // MARK: activity bar (listening / transcribing / thinking / pre-reply speaking)

    private var bar: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 10) {
                switch state.phase {
                case .listening:
                    PulsingDot(color: swordRed)
                    SwordWaveform(level: state.level)
                        .frame(maxWidth: .infinity)
                case .speaking:
                    PulsingDot(color: swordGreen)
                    SwordWaveform(level: 0.35)
                        .frame(maxWidth: .infinity)
                        .opacity(0.75)
                default:
                    PulsingDot(color: swordCyan)
                    VStack(alignment: .leading, spacing: 3) {
                        toolLogStack
                        ShimmerBar().frame(height: 3)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                workClock
                Text("◭")
                    .font(.system(size: 12))
                    .foregroundColor(swordCyan.opacity(0.7))
            }
            if !state.transcript.isEmpty {
                // Live while listening (grows word by word), dim context afterward.
                Text(state.transcript)
                    .font(.system(size: state.phase == .listening ? 11.5 : 10.5, design: .monospaced))
                    .foregroundColor(state.phase == .listening ? swordInk.opacity(0.92) : swordMuted.opacity(0.85))
                    .lineLimit(state.phase == .listening ? 4 : 1)
                    .fixedSize(horizontal: false, vertical: true)
                    .animation(.easeOut(duration: 0.18), value: state.transcript)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 11)
        .frame(width: 440)
        .background(capsuleBackground(borderOpacity: 0.38))
    }

    private var workClock: some View {
        Group {
            if !state.workingClock.isEmpty {
                Text(state.workingClock)
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundColor(swordCyan2.opacity(0.8))
            }
        }
    }

    // Rolling last-3 tool calls: newest bright, older dimmed, ▸ flips to ✓.
    private var toolLogStack: some View {
        VStack(alignment: .leading, spacing: 2) {
            let log = state.toolLog
            ForEach(Array(log.enumerated()), id: \.offset) { idx, line in
                Text(line)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundColor(
                        line.hasPrefix("✓")
                            ? swordGreen.opacity(idx == log.count - 1 ? 0.85 : 0.5)
                            : swordCyan2.opacity(idx == log.count - 1 ? 0.95 : 0.55)
                    )
                    .lineLimit(1)
            }
        }
        .animation(.easeOut(duration: 0.15), value: state.toolLog)
    }

    // MARK: full reply card

    private var full: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                phaseBadge
                Spacer(minLength: 0)
                workClock
                Text("◭")
                    .font(.system(size: 12))
                    .foregroundColor(swordCyan.opacity(0.7))
            }

            if !state.transcript.isEmpty {
                Text(state.transcript)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(swordMuted)
                    .lineLimit(2)
            }

            if case .error(let message) = state.phase {
                Text(message)
                    .font(.system(size: 12))
                    .foregroundColor(swordRed.opacity(0.95))
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !state.reply.isEmpty {
                // Sized to the text up to a cap, scrollable past it; follows the
                // tail while Hermes is still streaming.
                ScrollViewReader { proxy in
                    ScrollView(.vertical, showsIndicators: true) {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(state.reply)
                                .font(.system(size: 12, design: .monospaced))
                                .foregroundColor(swordInk)
                                .lineSpacing(3)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Color.clear.frame(height: 1).id("replyEnd")
                        }
                    }
                    .frame(height: replyViewHeight)
                    .onChange(of: state.reply) { _ in
                        if case .streaming = state.phase {
                            proxy.scrollTo("replyEnd", anchor: .bottom)
                        }
                    }
                }
            }

            // Mid-turn messages keep the live tool log visible under the text.
            if !state.toolLog.isEmpty, state.phase == .streaming || state.phase == .thinking {
                toolLogStack
            }

            Text(state.statusLine.isEmpty ? state.lastToolLine : state.statusLine)
                .font(.system(size: 9, design: .monospaced))
                .tracking(1.2)
                .foregroundColor(swordMuted.opacity(0.7))
                .lineLimit(1)
        }
        .padding(.horizontal, 17)
        .padding(.vertical, 13)
        .frame(width: 460, alignment: .leading)
        .background(capsuleBackground(borderOpacity: 0.4))
    }

    // Measured text height so the window (sized via fittingSize) stays
    // deterministic with a flexible ScrollView inside.
    private var replyViewHeight: CGFloat {
        let width: CGFloat = 426 // card width minus horizontal padding
        let font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let attr = NSAttributedString(string: state.reply, attributes: [.font: font])
        let rect = attr.boundingRect(
            with: NSSize(width: width, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading]
        )
        let estimated = ceil(rect.height * 1.26) + 10 // lineSpacing allowance
        return min(max(24, estimated), 380)
    }

    private var phaseBadge: some View {
        HStack(spacing: 6) {
            switch state.phase {
            case .speaking:
                PulsingDot(color: swordGreen)
            case .error:
                Circle().fill(swordRed).frame(width: 6, height: 6)
            default:
                Circle().fill(swordCyan).frame(width: 6, height: 6)
            }
            Text(state.phase.label)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(2)
                .foregroundColor(badgeColor)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(Capsule().fill(badgeColor.opacity(0.12)))
        .overlay(Capsule().strokeBorder(badgeColor.opacity(0.4), lineWidth: 0.5))
    }

    private var badgeColor: Color {
        switch state.phase {
        case .listening, .error: return swordRed
        case .speaking: return swordGreen
        default: return swordCyan
        }
    }

    private func capsuleBackground(borderOpacity: Double) -> some View {
        RoundedRectangle(cornerRadius: 21, style: .continuous)
            .fill(swordBg.opacity(0.94))
            .overlay(
                RoundedRectangle(cornerRadius: 21, style: .continuous)
                    .strokeBorder(swordCyan.opacity(borderOpacity), lineWidth: 1)
            )
            .shadow(color: swordCyan.opacity(0.18), radius: 14)
            .shadow(color: .black.opacity(0.5), radius: 10, y: 4)
    }
}

struct PulsingDot: View {
    let color: Color
    @State private var on = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 7, height: 7)
            .shadow(color: color.opacity(0.8), radius: on ? 5 : 1)
            .opacity(on ? 1 : 0.35)
            .animation(.easeInOut(duration: 0.55).repeatForever(autoreverses: true), value: on)
            .onAppear { on = true }
    }
}

// SWORD waveform: mirrored capsule bars, cyan gradient, glow, no text badge.
struct SwordWaveform: View {
    let level: Float
    private let barCount = 26

    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<barCount, id: \.self) { i in
                Capsule()
                    .fill(
                        LinearGradient(
                            colors: [swordCyan2, swordCyan, swordCyan.opacity(0.5)],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                    )
                    .frame(width: 3, height: barHeight(i))
                    .shadow(color: swordCyan.opacity(0.45), radius: 2.5)
                    .animation(.easeOut(duration: 0.08), value: level)
            }
        }
        .frame(height: 30)
    }

    private func barHeight(_ index: Int) -> CGFloat {
        let center = Double(barCount - 1) / 2
        let dist = abs(Double(index) - center) / center
        let weight = 1 - 0.7 * dist * dist
        let amplified = pow(Double(max(0.02, level)), 0.5)
        // level-coupled jitter makes the comb dance per audio tick
        let jitter = 0.72 + 0.5 * abs(sin(Double(index) * 2.4 + Double(level) * 11))
        return CGFloat(4 + amplified * weight * 24 * jitter)
    }
}

struct ShimmerBar: View {
    @State private var x: CGFloat = -1

    var body: some View {
        GeometryReader { geo in
            RoundedRectangle(cornerRadius: 2)
                .fill(swordCyan.opacity(0.12))
                .overlay(
                    RoundedRectangle(cornerRadius: 2)
                        .fill(
                            LinearGradient(
                                colors: [.clear, swordCyan.opacity(0.8), .clear],
                                startPoint: .leading, endPoint: .trailing
                            )
                        )
                        .frame(width: geo.size.width * 0.35)
                        .offset(x: x * geo.size.width)
                )
                .clipShape(RoundedRectangle(cornerRadius: 2))
                .onAppear {
                    withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) {
                        x = 1
                    }
                }
        }
    }
}
