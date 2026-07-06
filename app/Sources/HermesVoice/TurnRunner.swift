import Foundation

// One voice turn = one `airmic chat --audio <wav> --speak` subprocess emitting
// NDJSON events. airmic owns STT (Parakeet shared cache), N1 session injection,
// reply streaming, and ElevenLabs TTS with the playback lock.
struct TurnEvent {
    let event: String
    let text: String?
    let message: String?
    let played: Bool?
    let name: String?
    let status: String?
    let context: String?
}

final class TurnRunner {
    private var process: Process?
    private var watchdog: Timer?
    private var lastLineAt = Date()
    // The FileHandle readability callback runs on a private background queue
    // while cancel()/run() touch state from main — every parse-buffer access
    // is serialized here (2026-07-03 audit: Data is not thread-safe).
    private let parseQueue = DispatchQueue(label: "hermesvoice.turnrunner.parse")
    var isRunning: Bool { process?.isRunning ?? false }

    // Per-turn parse state: each run() gets its OWN box so a dying previous
    // child can never bleed bytes into the next turn's stream.
    private final class LineBuffer { var data = Data() }

    func run(audioURL: URL, speak: Bool = true, onEvent: @escaping (TurnEvent) -> Void, onExit: @escaping (Int32) -> Void) {
        cancel()

        // Stall watchdog: airmic emits heartbeats (waiting/tts_chunk/deltas)
        // constantly during a healthy turn. 150 s of total silence means the
        // child is wedged (dead websocket, stuck join) — kill it so the pill
        // recovers instead of holding a dead call forever.
        lastLineAt = Date()
        watchdog = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self, let proc = self.process, proc.isRunning else { return }
            if Date().timeIntervalSince(self.lastLineAt) > 150 {
                onEvent(TurnEvent(event: "error", text: nil, message: "turn stalled — no events for 150s; recovered", played: nil, name: nil, status: nil, context: nil))
                self.cancel()
            }
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: NSHomeDirectory() + "/.local/bin/airmic")
        var args = ["chat", "--audio", audioURL.path, "--allow-busy"]
        if speak { args.append("--speak") } // menu "Voice Replies (TTS)" off → text-only turn
        proc.arguments = args
        // Config lives in ~/.hermes/voicepill/voicepill.env (written by
        // setup.sh) and is loaded by airmic itself — no env plumbing here.

        let pipe = Pipe()
        proc.standardOutput = pipe
        // Never an undrained Pipe(): once the child writes ~64 KB of stderr the
        // OS buffer fills and the whole turn wedges on a blocked write(2).
        proc.standardError = FileHandle.nullDevice

        let fh = pipe.fileHandleForReading
        let lineBuffer = LineBuffer()

        fh.readabilityHandler = { [weak self] handle in
            guard let self else { return }
            let chunk = handle.availableData
            if chunk.isEmpty { return }
            DispatchQueue.main.async { self.lastLineAt = Date() }
            self.parseQueue.async { Self.ingest(chunk, into: lineBuffer, onEvent: onEvent) }
        }

        proc.terminationHandler = { [weak self] p in
            // Drain the tail BEFORE reporting exit: a fast-exiting turn's final
            // complete/spoken line can still be sitting in the pipe.
            fh.readabilityHandler = nil
            let tail = (try? fh.readDataToEndOfFile()) ?? Data()
            let finish = { DispatchQueue.main.async { onExit(p.terminationStatus) } }
            if let self {
                self.parseQueue.async {
                    if !tail.isEmpty { Self.ingest(tail, into: lineBuffer, onEvent: onEvent) }
                    finish()
                }
            } else {
                finish()
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            fh.readabilityHandler = nil
            DispatchQueue.main.async {
                onEvent(TurnEvent(event: "error", text: nil, message: "airmic launch failed: \(error.localizedDescription)", played: nil, name: nil, status: nil, context: nil))
                onExit(-1)
            }
        }
    }

    private static func ingest(_ chunk: Data, into buf: LineBuffer, onEvent: @escaping (TurnEvent) -> Void) {
        buf.data.append(chunk)
        while let nl = buf.data.firstIndex(of: 0x0A) {
            let line = buf.data.subdata(in: buf.data.startIndex..<nl)
            buf.data.removeSubrange(buf.data.startIndex...nl)
            guard !line.isEmpty,
                  let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let name = obj["event"] as? String else { continue }
            let ev = TurnEvent(
                event: name,
                text: obj["text"] as? String,
                message: obj["message"] as? String,
                played: obj["played"] as? Bool,
                name: obj["name"] as? String,
                status: obj["status"] as? String,
                context: obj["context"] as? String
            )
            DispatchQueue.main.async { onEvent(ev) }
        }
    }

    func cancel() {
        watchdog?.invalidate()
        watchdog = nil
        process?.terminate()
        process = nil
    }
}
