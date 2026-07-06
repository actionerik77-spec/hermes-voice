import AppKit
import AVFoundation
import Foundation

// HermesVoice — SWORD voice pill for Hermes (Hermes on N1).
// \ toggles: press = listen, press again = send. airmic owns the backend lane.

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let state = AppState()
    private let hotkey = HotkeyManager()
    private var ttsMuted = UserDefaults.standard.bool(forKey: "ttsMuted")
    private let recorder = Recorder()
    private let runner = TurnRunner()
    private var panelController: PillPanelController?
    private var statusItem: NSStatusItem?
    private var collapseTimer: Timer?
    private var partialTimer: Timer?
    private var partialBusy = false
    private var turnGeneration = 0
    private var currentAck: NSSound? // held so playback isn't deallocated mid-line
    private var workTimer: Timer?
    private var midTurnFadeTimer: Timer?
    private var turnComplete = false
    private var hotkeyMenuItem: NSMenuItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        panelController = PillPanelController(state: state)
        setupStatusItem()

        if CommandLine.arguments.contains("--ui-smoke") {
            runUiSmoke()
            return // no hotkey/mic in smoke mode (a live instance may own them)
        }

        recorder.onLevel = { [weak self] level in
            self?.state.level = level
        }

        do {
            try hotkey.register { [weak self] in self?.handleHotkey() }
            log("REGISTERED_HOTKEY=backslash")
        } catch {
            log("HOTKEY_REGISTER_FAILED=\(error.localizedDescription)")
            state.phase = .error("hotkey: \(error.localizedDescription)")
            state.statusLine = "backslash hotkey failed to register"
            // Keep the menu checkmark honest — the key is NOT live.
            state.hotkeyEnabled = false
            hotkeyMenuItem?.state = .off
        }

        AVCaptureDevice.requestAccess(for: .audio) { granted in
            log("MIC_ACCESS=\(granted)")
        }
    }

    // Scripted walk through every tier with synthetic data; used for visual QA.
    private func runUiSmoke() {
        var tick = 0
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] timer in
            guard let self else { return timer.invalidate() }
            tick += 1
            let t = Double(tick) * 0.05
            switch t {
            case ..<3.5:
                if self.state.phase != .listening {
                    self.state.phase = .listening
                    self.state.statusLine = "listening — press \\ to send"
                }
                self.state.level = Float(abs(sin(t * 5.3)) * 0.85)
                // live partial transcript growing word by word
                let words = "hermes run the demo verify battery and give me the tally".split(separator: " ")
                let n = min(words.count, Int(t / 3.2 * Double(words.count)))
                self.state.transcript = words.prefix(n).joined(separator: " ")
            case ..<4.5:
                self.state.phase = .transcribing
                self.state.level = 0
                self.state.statusLine = "parakeet transcribing"
            case ..<9.0:
                self.state.phase = .thinking
                let stage = Int((t - 4.5) / 1.1) + 1
                var log: [String] = []
                if stage >= 1 {
                    log.append((stage >= 2 ? "✓" : "▸") + " exec_shell — python -m tracex.verify --battery full")
                }
                if stage >= 3 {
                    log.append((stage >= 4 ? "✓" : "▸") + " read_file — tracex/verify/report.json")
                }
                self.state.toolLog = log
                self.state.statusLine = "sent to HERMES — working"
            case ..<11.0:
                self.state.phase = .streaming
                let full = "Verify battery complete. 8 of 8 lanes accepted with zero failures and zero repair rounds. The transform contract is published and the perception stack keys off it cleanly.\n\nLane 05 re-issued its retained-transform contract against 03's published one per the SA-1 ruling. Wave 2 is halfway — remaining lanes are 05, 22, 04+20, and 07. Full tally is on screen in the session."
                let n = min(full.count, Int((t - 9.0) / 2.0 * Double(full.count)))
                self.state.reply = String(full.prefix(n))
            case ..<15.0:
                self.state.phase = .speaking
                self.state.statusLine = "ElevenLabs voice synthesizing"
            default:
                timer.invalidate()
                self.state.reset()
                self.state.transcript = ""
                self.state.reply = ""
                log("UI_SMOKE_DONE")
            }
        }
    }

    private func handleHotkey() {
        guard state.hotkeyEnabled else { return }
        log("HOTKEY_PRESSED phase=\(state.phase.label)")

        switch state.phase {
        case .idle, .error:
            startListening()
        case .listening:
            finishListeningAndSend()
        default:
            // Barge-in: kill the running turn (SIGTERM silences airmic playback),
            // interrupt Hermes server-side even mid-tool-call, listen immediately.
            interruptAndListen()
        }
    }

    private func interruptAndListen() {
        runner.cancel()
        killStrayTurns()
        fireAirmic("interrupt") // fire and forget; session.interrupt is idempotent
        startListening()
    }

    // Short-lived airmic side commands: null devices (an undrained Pipe() can
    // fill and block) and a termination handler so the child is always reaped.
    private func fireAirmic(_ command: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: NSHomeDirectory() + "/.local/bin/airmic")
        proc.arguments = [command]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        proc.terminationHandler = { _ in }
        try? proc.run()
    }

    // Belt for the 2026-07-03 zombie-turn incident: a chat child that dodges
    // runner.cancel() (pid tracking lost across pill restarts, etc.) keeps
    // narrating a dead reply. Only the pill spawns these, so pkill is safe.
    private func killStrayTurns() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        proc.arguments = ["-f", "airmic.py chat --audio"]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()
    }

    private func returnToIdleHeardNothing() {
        stopWorkClock()
        state.phase = .idle
        state.transcript = ""
        state.reply = ""
        state.statusLine = "heard nothing — \\ to talk"
        scheduleCollapse(after: 2)
    }

    private func startListening() {
        collapseTimer?.invalidate()
        midTurnFadeTimer?.invalidate() // stale fade must never clear the NEW turn's reply
        stopWorkClock()
        turnGeneration += 1
        state.transcript = ""
        state.reply = ""
        state.toolLog = []
        do {
            try recorder.start()
            state.phase = .listening
            state.statusLine = "listening — press \\ to send"
            startPartialTranscription()
        } catch {
            state.phase = .error("mic: \(error.localizedDescription)")
            state.statusLine = "mic unavailable — check permission"
            scheduleCollapse(after: 6)
        }
    }

    // MARK: live partial transcript (Parakeet on the growing buffer)

    private func startPartialTranscription() {
        partialTimer?.invalidate()
        partialTimer = Timer.scheduledTimer(withTimeInterval: 1.2, repeats: true) { [weak self] _ in
            self?.runPartialTranscription()
        }
    }

    private func stopPartialTranscription() {
        partialTimer?.invalidate()
        partialTimer = nil
    }

    private func runPartialTranscription() {
        guard state.phase == .listening, !partialBusy else { return }
        let gen = turnGeneration
        let snap = FileManager.default.temporaryDirectory
            .appendingPathComponent("hermesvoice-partial-\(ProcessInfo.processInfo.processIdentifier).wav")
        guard recorder.snapshot(to: snap) != nil else { return }
        partialBusy = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let out = snap.appendingPathExtension("txt")
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: NSHomeDirectory() + "/.local/bin/parakeet-stt")
            proc.arguments = [snap.path, out.path]
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            let text = ((try? String(contentsOf: out, encoding: .utf8)) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                self.partialBusy = false
                // Stale guard: never let an old snapshot overwrite a newer turn.
                if gen == self.turnGeneration, self.state.phase == .listening, !text.isEmpty {
                    self.state.transcript = text
                }
            }
        }
    }

    private func finishListeningAndSend() {
        stopPartialTranscription()
        let wav = FileManager.default.temporaryDirectory
            .appendingPathComponent("hermesvoice-\(Int(Date().timeIntervalSince1970)).wav")
        guard let url = recorder.stop(to: wav) else {
            returnToIdleHeardNothing() // silence isn't an error
            return
        }
        state.phase = .transcribing
        state.statusLine = "parakeet transcribing"
        turnComplete = false

        let gen = turnGeneration
        runner.run(audioURL: url, speak: !ttsMuted) { [weak self] ev in
            guard let self, gen == self.turnGeneration else { return }
            self.handleTurnEvent(ev)
        } onExit: { [weak self] code in
            guard let self else { return }
            try? FileManager.default.removeItem(at: url)
            // A newer turn owns the pill (barge-in) — this exit is history.
            guard gen == self.turnGeneration else { return }
            // A clean exit without complete/error would otherwise leak the
            // repeating 1 s work clock forever.
            self.stopWorkClock()
            if case .error = self.state.phase { return }
            if code != 0 {
                self.state.phase = .error("turn exited \(code)")
                self.state.statusLine = "airmic exited \(code)"
            }
            self.scheduleCollapse(after: 5)
        }
    }

    private func handleTurnEvent(_ ev: TurnEvent) {
        switch ev.event {
        case "transcript":
            state.transcript = ev.text ?? ""
            state.phase = .thinking
            state.statusLine = "sent to HERMES — working"
        case "hangup":
            stopWorkClock()
            state.phase = .idle
            state.transcript = ""
            state.reply = ""
            state.statusLine = "call ended — next \\ starts fresh"
            playNamedAck("copy")
            scheduleCollapse(after: 3)
        case "attach":
            state.phase = .thinking
            state.statusLine = "waking HERMES session"
        case "tool":
            if ev.status == "start" {
                state.recordToolStart(name: ev.name ?? "tool", context: ev.context ?? "")
            } else {
                state.recordToolDone(name: ev.name ?? "")
            }
            // Text on screen + tools running again = that was a mid-turn message.
            // Let it breathe a few seconds, then fall back to the tool ticker.
            if case .streaming = state.phase, !state.reply.isEmpty {
                scheduleMidTurnFade()
            }
        case "target":
            break
        case "submitted":
            state.phase = .thinking
            playAck()
            startWorkClock()
        case "waiting":
            state.statusLine = "HERMES still working"
        case "delta":
            midTurnFadeTimer?.invalidate()
            if case .streaming = state.phase {} else { state.phase = .streaming }
            state.reply += ev.text ?? ""
            state.statusLine = "HERMES replying"
        case "complete":
            midTurnFadeTimer?.invalidate()
            turnComplete = true
            if let final = ev.text, !final.isEmpty { state.reply = final }
            // The final card must persist even if a mid-turn fade just dropped
            // the phase to thinking.
            state.phase = .speaking
            let clock = state.workingClock
            stopWorkClock()
            state.statusLine = clock.isEmpty ? "reply complete" : "reply complete · worked \(clock)"
        case "speaking":
            // Streaming TTS starts mid-turn; only flip the badge once the turn
            // is actually done — mid-turn speech shouldn't disturb the ticker.
            if turnComplete { state.phase = .speaking }
            state.statusLine = "ElevenLabs voice synthesizing"
        case "spoken":
            state.statusLine = ev.played == true ? "spoken — ElevenLabs" : "speech skipped"
            scheduleCollapse(after: 4)
        case "midturn_complete":
            // Hermes settled an interrupted segment but is still working
            // (interrupted-turn auto-continue) — the turn is NOT over.
            state.statusLine = "segment done — HERMES continuing"
        case "empty":
            // `\` pressed, nothing said — drop back to idle, no error card.
            returnToIdleHeardNothing()
        case "error":
            stopWorkClock()
            state.phase = .error(ev.message ?? "unknown")
            state.statusLine = ev.message ?? "error"
            scheduleCollapse(after: 8)
        default:
            break
        }
    }

    // Local stopwatch — no backend streaming needed, just "how long has Hermes
    // been working" ticking in the pill.
    private func startWorkClock() {
        workTimer?.invalidate()
        state.workingSeconds = 0
        workTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.state.workingSeconds += 1
        }
    }

    private func stopWorkClock() {
        workTimer?.invalidate()
        workTimer = nil
    }

    // Instant acknowledgment in Hermes's own voice the moment a prompt submits —
    // pre-generated clips in ~/.hermes/voicepill/acks ("On it.", "Copy.",
    // "Executing now.", "Implementing now.", "Executing violently.").
    private func playAck() {
        let dir = NSHomeDirectory() + "/.hermes/voicepill/acks"
        guard let files = try? FileManager.default.contentsOfDirectory(atPath: dir).filter({ $0.hasSuffix(".wav") }),
              let pick = files.randomElement() else { return }
        currentAck?.stop()
        currentAck = NSSound(contentsOfFile: dir + "/" + pick, byReference: true)
        currentAck?.play()
    }

    private func playNamedAck(_ name: String) {
        let path = NSHomeDirectory() + "/.hermes/voicepill/acks/\(name).wav"
        currentAck?.stop()
        currentAck = NSSound(contentsOfFile: path, byReference: true)
        currentAck?.play()
    }

    // Mid-turn messages are transient: visible (and spoken) for a few seconds,
    // then the pill returns to the slim tool ticker until the FINAL reply.
    private func scheduleMidTurnFade() {
        midTurnFadeTimer?.invalidate()
        midTurnFadeTimer = Timer.scheduledTimer(withTimeInterval: 4.5, repeats: false) { [weak self] _ in
            guard let self else { return }
            DispatchQueue.main.async {
                if case .streaming = self.state.phase {
                    self.state.reply = ""
                    self.state.phase = .thinking
                    self.state.statusLine = "HERMES still working"
                }
            }
        }
    }

    private func scheduleCollapse(after seconds: TimeInterval) {
        collapseTimer?.invalidate()
        collapseTimer = Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { [weak self] _ in
            DispatchQueue.main.async { self?.state.reset() }
        }
    }

    // MARK: menu bar

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "◭"
        let menu = NSMenu()

        let toggle = NSMenuItem(title: "Backslash Hotkey Enabled", action: #selector(toggleHotkey), keyEquivalent: "")
        toggle.target = self
        toggle.state = state.hotkeyEnabled ? .on : .off
        hotkeyMenuItem = toggle
        menu.addItem(toggle)

        let tts = NSMenuItem(title: "Voice Replies (TTS)", action: #selector(toggleTTS), keyEquivalent: "")
        tts.target = self
        tts.state = ttsMuted ? .off : .on
        menu.addItem(tts)

        let cancel = NSMenuItem(title: "Cancel Current Turn", action: #selector(cancelTurn), keyEquivalent: "")
        cancel.target = self
        menu.addItem(cancel)

        let hangup = NSMenuItem(title: "Hang Up — New Session", action: #selector(hangUp), keyEquivalent: "")
        hangup.target = self
        menu.addItem(hangup)

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Press \\ to talk to HERMES", action: nil, keyEquivalent: ""))
        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit HermesVoice", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)

        item.menu = menu
        statusItem = item
    }

    @objc private func toggleHotkey(_ sender: NSMenuItem) {
        state.hotkeyEnabled.toggle()
        if state.hotkeyEnabled {
            do {
                try hotkey.register { [weak self] in self?.handleHotkey() }
            } catch {
                // Registration failed (another app owns the key) — never show
                // an "enabled" checkmark for a hotkey that can't fire.
                state.hotkeyEnabled = false
                state.statusLine = "hotkey re-register failed — key in use?"
                log("HOTKEY_REREGISTER_FAILED=\(error.localizedDescription)")
            }
        } else {
            hotkey.unregister() // releases '\' so it types normally again
        }
        sender.state = state.hotkeyEnabled ? .on : .off
    }

    @objc private func toggleTTS(_ sender: NSMenuItem) {
        ttsMuted.toggle()
        UserDefaults.standard.set(ttsMuted, forKey: "ttsMuted")
        sender.state = ttsMuted ? .off : .on
        // Muting while Hermes is narrating a finished reply → shut it up now.
        // (Mid-stream turns are left alone so the reply text isn't lost.)
        if ttsMuted, turnComplete, case .speaking = state.phase {
            turnGeneration += 1 // deliberate kill: the SIGTERM exit(15) must not paint a FAULT card
            runner.cancel()
            killStrayTurns()
            state.statusLine = "voice replies off"
        }
    }

    @objc private func hangUp() {
        turnGeneration += 1 // deliberate kill: stale onExit must not paint a FAULT card
        runner.cancel()
        killStrayTurns()
        fireAirmic("hangup")
        stopWorkClock()
        state.reset()
        state.transcript = ""
        state.reply = ""
        state.statusLine = "call ended — next \\ starts fresh"
        playNamedAck("copy")
    }

    @objc private func cancelTurn() {
        stopPartialTranscription()
        stopWorkClock()
        turnGeneration += 1
        runner.cancel()
        killStrayTurns()
        _ = recorder.stop(to: FileManager.default.temporaryDirectory.appendingPathComponent("hermesvoice-discard.wav"))
        state.reset()
    }
}

func log(_ line: String) {
    let dir = NSHomeDirectory() + "/Library/Logs/HermesVoice"
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    let path = dir + "/hermesvoice.log"
    let stamp = ISO8601DateFormatter().string(from: Date())
    let entry = "\(stamp) \(line)\n"
    if let handle = FileHandle(forWritingAtPath: path) {
        handle.seekToEndOfFile()
        handle.write(entry.data(using: .utf8)!)
        handle.closeFile()
    } else {
        try? entry.write(toFile: path, atomically: true, encoding: .utf8)
    }
    FileHandle.standardError.write(entry.data(using: .utf8)!)
}

// --hotkey-smoke: register, report, exit (machine-verifiable without a human)
if CommandLine.arguments.contains("--hotkey-smoke") {
    let mgr = HotkeyManager()
    do {
        try mgr.register {}
        print("{\"hotkey\": \"backslash\", \"registered\": true}")
        mgr.unregister()
        exit(0)
    } catch {
        print("{\"registered\": false, \"error\": \"\(error.localizedDescription)\"}")
        exit(1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
