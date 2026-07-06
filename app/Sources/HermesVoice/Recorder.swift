import AVFoundation
import Foundation

// AVAudioEngine mic capture -> mono 16 kHz Int16 buffer + RMS level, following
// the FluidVoice pipeline shape (downmix + linear resample on the tap thread).
final class Recorder {
    private var engine: AVAudioEngine?
    private var samples: [Int16] = []
    private let lock = NSLock()
    private(set) var isRecording = false
    var onLevel: ((Float) -> Void)?

    func start() throws {
        stopEngineOnly()
        lock.lock(); samples.removeAll(); lock.unlock()

        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "HermesVoice", code: 1, userInfo: [NSLocalizedDescriptionKey: "mic format unavailable (permission not granted yet?)"])
        }
        let sourceRate = format.sampleRate

        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self, let channelData = buffer.floatChannelData else { return }
            let frames = Int(buffer.frameLength)
            let channels = Int(buffer.format.channelCount)
            if frames == 0 { return }

            // Downmix to mono.
            var mono = [Float](repeating: 0, count: frames)
            for ch in 0..<channels {
                let data = channelData[ch]
                for i in 0..<frames { mono[i] += data[i] }
            }
            if channels > 1 {
                let inv = 1 / Float(channels)
                for i in 0..<frames { mono[i] *= inv }
            }

            // RMS -> normalized level for the waveform.
            var sumsq: Float = 0
            for s in mono { sumsq += s * s }
            let rms = sqrtf(sumsq / Float(frames))
            let db = 20 * log10f(max(rms, 1e-7))
            let norm = max(0, min(1, (db + 55) / 55))
            DispatchQueue.main.async { self.onLevel?(norm) }

            // Linear resample to 16 kHz Int16.
            let ratio = 16_000.0 / sourceRate
            let outCount = max(1, Int(Double(frames) * ratio))
            var out = [Int16](repeating: 0, count: outCount)
            for i in 0..<outCount {
                let pos = Double(i) / ratio
                let idx = Int(pos)
                let frac = Float(pos - Double(idx))
                let a = mono[min(idx, frames - 1)]
                let b = mono[min(idx + 1, frames - 1)]
                let v = a + (b - a) * frac
                out[i] = Int16(max(-1, min(1, v)) * 32767)
            }
            self.lock.lock()
            self.samples.append(contentsOf: out)
            self.lock.unlock()
        }

        engine.prepare()
        try engine.start()
        self.engine = engine
        isRecording = true
    }

    /// Stops capture and writes a 16 kHz mono WAV. Returns nil if nothing was captured.
    func stop(to url: URL) -> URL? {
        stopEngineOnly()
        lock.lock(); let captured = samples; samples.removeAll(); lock.unlock()
        guard captured.count > 1600 else { return nil } // <0.1s = nothing usable
        return Self.writeWav(captured, to: url)
    }

    /// Writes the buffer captured SO FAR without stopping — feeds live partial
    /// transcription while the user is still speaking.
    func snapshot(to url: URL) -> URL? {
        guard isRecording else { return nil }
        lock.lock(); let captured = samples; lock.unlock()
        guard captured.count > 8000 else { return nil } // need ~0.5s before first partial
        return Self.writeWav(captured, to: url)
    }

    private static func writeWav(_ captured: [Int16], to url: URL) -> URL? {
        var data = Data()
        let byteCount = captured.count * 2
        func append(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        func append16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: Array("RIFF".utf8)); append(UInt32(36 + byteCount))
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8)); append(16); append16(1); append16(1)
        append(16_000); append(32_000); append16(2); append16(16)
        data.append(contentsOf: Array("data".utf8)); append(UInt32(byteCount))
        captured.withUnsafeBytes { data.append(contentsOf: $0) }

        do {
            try data.write(to: url)
            return url
        } catch {
            return nil
        }
    }

    private func stopEngineOnly() {
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        isRecording = false
        DispatchQueue.main.async { self.onLevel?(0) }
    }
}
