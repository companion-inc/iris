import AVFoundation
import Foundation
import SwiftUI
import WebKit

@MainActor
@Observable
final class NativeVoiceRuntime {
    private let api: IrisAPI
    private var audioInput = NativeAudioInput()
    private let session = URLSession(configuration: .default)
    private var webSocket: URLSessionWebSocketTask?
    private var audioPlayer: AVAudioPlayer?
    private var soundEffectPlayer: AVAudioPlayer?
    private var soundEffectLastPlayed: [String: TimeInterval] = [:]
    private var pendingOutputAudio = Data()
    private var pendingOutputSampleRate = 48_000
    private var pendingOutputChannels = 1
    private(set) var sessionID: String?
    private(set) var isRunning = false
    private(set) var status = "Idle"
    private(set) var lastEvent = "None"
    fileprivate(set) var inputFrames = 0
    private(set) var outputFrames = 0
    private(set) var liveTranscripts: [TranscriptSegment] = []
    var captureView: NSView { audioInput.webView }

    init(api: IrisAPI) {
        self.api = api
    }

    func start() async {
        guard !isRunning else { return }
        do {
            status = "Starting microphone"
            let input = try await startAudioInputWithTimeout()
            let voiceSession = try await api.createVoiceSession(sampleRate: input.sampleRate, channels: input.channels)
            sessionID = voiceSession.sessionId
            status = "Connecting voice runtime"

            let task = session.webSocketTask(with: voiceSession.voiceUrl)
            webSocket = task
            task.resume()
            receiveLoop(task)

            isRunning = true
            status = "Listening"
        } catch {
            stop()
            audioInput = NativeAudioInput()
            status = error.localizedDescription
        }
    }

    func stop() {
        audioInput.stop()
        audioPlayer?.stop()
        audioPlayer = nil
        soundEffectPlayer?.stop()
        soundEffectPlayer = nil
        pendingOutputAudio.removeAll(keepingCapacity: false)
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        sessionID = nil
        isRunning = false
        status = "Idle"
    }

    func stopSpeaking() {
        audioPlayer?.stop()
        audioPlayer = nil
        pendingOutputAudio.removeAll(keepingCapacity: false)
        let payload = ["type": "control", "action": "stop_speaking"]
        if let data = try? JSONSerialization.data(withJSONObject: payload),
           let text = String(data: data, encoding: .utf8) {
            webSocket?.send(.string(text)) { _ in }
        }
    }

    func setStatus(_ nextStatus: String) {
        status = nextStatus
    }

    func playDebugTone() -> Bool {
        let sampleRate = 48_000
        let channels = 1
        let frameCount = sampleRate / 4
        var pcm = Data(capacity: frameCount * MemoryLayout<Int16>.size)
        for frame in 0..<frameCount {
            let phase = 2.0 * Double.pi * 880.0 * Double(frame) / Double(sampleRate)
            var sample = Int16((sin(phase) * 0.18 * Double(Int16.max)).rounded())
            pcm.append(Data(bytes: &sample, count: MemoryLayout<Int16>.size))
        }
        do {
            let player = try AVAudioPlayer(data: Self.wavData(fromPCM16: pcm, sampleRate: sampleRate, channels: channels))
            player.prepareToPlay()
            let played = player.play()
            if played {
                audioPlayer = player
                outputFrames += 1
                lastEvent = "debug.audio.play-test"
            }
            return played
        } catch {
            status = "Playback failed: \(error.localizedDescription)"
            return false
        }
    }

    func debugInjectVoiceEvent(_ type: String) -> Bool {
        guard !type.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        let object: [String: Any] = ["type": type]
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else {
            return false
        }
        handleMessageText(text)
        return true
    }

    private func startAudioInputWithTimeout() async throws -> NativeAudioInputConfig {
        try await audioInput.start(timeout: 8, onStatus: { [weak self] nextStatus in
            Task { @MainActor in
                self?.status = nextStatus
            }
        }) { [weak self] data, sampleRate, channels in
            Task { @MainActor in
                guard let self else { return }
                self.inputFrames += 1
                self.sendAudio(data, sampleRate: sampleRate, channels: channels)
            }
        }
    }

    fileprivate func sendAudio(_ data: Data, sampleRate: Int, channels: Int) {
        guard let webSocket else { return }
        let payload: [String: Any] = [
            "type": "audio",
            "sampleRate": sampleRate,
            "channels": channels,
            "audio": data.base64EncodedString()
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: body, encoding: .utf8) else {
            return
        }
        webSocket.send(.string(text)) { _ in }
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.webSocket === task else { return }
                switch result {
                case .success(.string(let text)):
                    self.handleMessageText(text)
                    self.receiveLoop(task)
                case .success(.data(let data)):
                    self.lastEvent = "binary \(data.count) bytes"
                    self.receiveLoop(task)
                case .failure(let error):
                    if self.isRunning {
                        self.status = "Voice disconnected: \(error.localizedDescription)"
                        self.stop()
                    }
                @unknown default:
                    self.receiveLoop(task)
                }
            }
        }
    }

    private func handleMessageText(_ text: String) {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else {
            lastEvent = "text"
            return
        }
        lastEvent = type
        if type == "audio" {
            playAudioEvent(object)
        } else if type == "transcript.final" || type == "transcript.interim" {
            handleTranscriptEvent(object, isInterim: type == "transcript.interim")
        } else if let effect = Self.soundEffect(forVoiceEvent: type) {
            playSoundEffect(effect)
        } else if type == "assistant.audio.started" {
            playSoundEffect(.assistantStart)
            pendingOutputAudio.removeAll(keepingCapacity: true)
            status = "Iris speaking"
        } else if type == "assistant.audio.stopped" {
            playPendingOutputAudio()
            playSoundEffect(.assistantStop)
            status = "Listening"
        }
    }

    nonisolated static func soundEffectID(forVoiceEvent type: String) -> String? {
        soundEffect(forVoiceEvent: type)?.id
    }

    nonisolated private static func soundEffect(forVoiceEvent type: String) -> NativeVoiceSoundEffect? {
        switch type {
        case "wake.accepted", "wake.detected":
            return .wake
        case "speaker.identified":
            return .speaker
        case "sound.recognition.detected":
            return .sound
        case "tool.called", "tool.started":
            return .tool
        case "tool.finished", "agent.completion.injected", "hub.completion.injected":
            return .done
        case "tool.failed":
            return .error
        default:
            return nil
        }
    }

    private func handleTranscriptEvent(_ object: [String: Any], isInterim: Bool) {
        guard let text = object["text"] as? String else { return }
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return }
        let speakerName = object["speaker"] as? String
        let idSeed = "\(sessionID ?? "voice"):\(isInterim ? "interim" : "final"):\(inputFrames):\(normalized)"
        let segment = TranscriptSegment(
            id: isInterim ? "live-interim-\(sessionID ?? "voice")" : "live-\(abs(idSeed.hashValue))",
            text: normalized,
            startedAt: Date(),
            speakerName: speakerName,
            emotionLabel: nil
        )
        if isInterim {
            liveTranscripts.removeAll { $0.id.hasPrefix("live-interim-") }
            liveTranscripts.insert(segment, at: 0)
        } else {
            liveTranscripts.removeAll { $0.id.hasPrefix("live-interim-") || $0.text == normalized }
            liveTranscripts.insert(segment, at: 0)
            if liveTranscripts.count > 12 {
                liveTranscripts = Array(liveTranscripts.prefix(12))
            }
        }
    }

    private func playAudioEvent(_ object: [String: Any]) {
        guard let encoded = object["audio"] as? String,
              let data = Data(base64Encoded: encoded),
              let sampleRate = object["sampleRate"] as? Int,
              let channels = object["channels"] as? Int else {
            return
        }
        pendingOutputSampleRate = sampleRate
        pendingOutputChannels = max(1, channels)
        pendingOutputAudio.append(data)
        outputFrames += 1
    }

    private func playPendingOutputAudio() {
        guard !pendingOutputAudio.isEmpty else { return }
        let wav = Self.wavData(
            fromPCM16: pendingOutputAudio,
            sampleRate: pendingOutputSampleRate,
            channels: pendingOutputChannels
        )
        pendingOutputAudio.removeAll(keepingCapacity: true)
        do {
            let player = try AVAudioPlayer(data: wav)
            player.prepareToPlay()
            player.play()
            audioPlayer = player
        } catch {
            status = "Playback failed: \(error.localizedDescription)"
        }
    }

    private func playSoundEffect(_ effect: NativeVoiceSoundEffect) {
        let now = Date().timeIntervalSince1970 * 1000
        let lastPlayed = soundEffectLastPlayed[effect.id] ?? 0
        guard now - lastPlayed >= effect.cooldownMs else { return }
        soundEffectLastPlayed[effect.id] = now

        let sampleRate = 48_000
        let pcm = effect.pcm(sampleRate: sampleRate)
        guard !pcm.isEmpty else { return }
        do {
            let player = try AVAudioPlayer(data: Self.wavData(fromPCM16: pcm, sampleRate: sampleRate, channels: 1))
            player.volume = effect.volume
            player.prepareToPlay()
            guard player.play() else { return }
            soundEffectPlayer = player
        } catch {
            lastEvent = "sound-effect.failed"
        }
    }

    nonisolated static func pcm16Data(from buffer: AVAudioPCMBuffer) -> Data {
        nativePCM16Data(from: buffer)
    }

    nonisolated static func audioBuffer(fromPCM16 data: Data, sampleRate: Double, channels: Int) -> AVAudioPCMBuffer? {
        guard channels > 0,
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: AVAudioChannelCount(channels), interleaved: false) else {
            return nil
        }
        let frameCount = data.count / (channels * MemoryLayout<Int16>.size)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameCount)),
              let floatChannels = buffer.floatChannelData else {
            return nil
        }
        buffer.frameLength = AVAudioFrameCount(frameCount)
        data.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            for frame in 0..<frameCount {
                for channel in 0..<channels {
                    floatChannels[channel][frame] = Float(samples[frame * channels + channel]) / Float(Int16.max)
                }
            }
        }
        return buffer
    }

    nonisolated private static func wavData(fromPCM16 data: Data, sampleRate: Int, channels: Int) -> Data {
        let channelCount = max(1, channels)
        let byteRate = sampleRate * channelCount * MemoryLayout<Int16>.size
        let blockAlign = UInt16(channelCount * MemoryLayout<Int16>.size)
        let dataSize = UInt32(data.count)
        let riffSize = UInt32(36 + data.count)
        var wav = Data(capacity: 44 + data.count)
        wav.append("RIFF".data(using: .ascii)!)
        wav.appendLittleEndian(riffSize)
        wav.append("WAVEfmt ".data(using: .ascii)!)
        wav.appendLittleEndian(UInt32(16))
        wav.appendLittleEndian(UInt16(1))
        wav.appendLittleEndian(UInt16(channelCount))
        wav.appendLittleEndian(UInt32(sampleRate))
        wav.appendLittleEndian(UInt32(byteRate))
        wav.appendLittleEndian(blockAlign)
        wav.appendLittleEndian(UInt16(16))
        wav.append("data".data(using: .ascii)!)
        wav.appendLittleEndian(dataSize)
        wav.append(data)
        return wav
    }
}

private struct NativeAudioInputConfig: Sendable {
    var sampleRate: Int
    var channels: Int
}

private enum NativeAudioInputError: LocalizedError {
    case startTimedOut
    case webCapture(String)

    var errorDescription: String? {
        switch self {
        case .startTimedOut:
            "Microphone start timed out"
        case .webCapture(let message):
            message
        }
    }
}

private enum NativeVoiceSoundEffect {
    case wake
    case speaker
    case sound
    case tool
    case done
    case error
    case assistantStart
    case assistantStop

    var id: String {
        switch self {
        case .wake:
            return "wake"
        case .speaker:
            return "speaker"
        case .sound:
            return "sound"
        case .tool:
            return "tool"
        case .done:
            return "done"
        case .error:
            return "error"
        case .assistantStart:
            return "assistant-start"
        case .assistantStop:
            return "assistant-stop"
        }
    }

    var cooldownMs: TimeInterval {
        switch self {
        case .wake:
            return 1200
        case .speaker:
            return 700
        case .sound:
            return 3000
        case .tool, .done, .error:
            return 800
        case .assistantStart, .assistantStop:
            return 500
        }
    }

    var volume: Float {
        switch self {
        case .wake:
            return 0.035
        case .speaker:
            return 0.03
        case .sound:
            return 0.018
        case .tool, .done:
            return 0.026
        case .error:
            return 0.028
        case .assistantStart, .assistantStop:
            return 0.024
        }
    }

    func pcm(sampleRate: Int) -> Data {
        switch self {
        case .wake:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(660, 0, 0.055), (880, 0.065, 0.07)]
            )
        case .speaker:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(520, 0, 0.045), (780, 0.05, 0.055), (1040, 0.105, 0.055)]
            )
        case .sound:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(740, 0, 0.04)]
            )
        case .tool:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(420, 0, 0.045), (560, 0.055, 0.045)]
            )
        case .done:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(600, 0, 0.045), (900, 0.055, 0.055)]
            )
        case .error:
            return NativeVoiceSoundEffect.toneSchedule(
                sampleRate: sampleRate,
                tones: [(220, 0, 0.075), (185, 0.09, 0.08)]
            )
        case .assistantStart:
            return NativeVoiceSoundEffect.toneSequence(
                sampleRate: sampleRate,
                tones: [(660, 0.045)],
                gap: 0
            )
        case .assistantStop:
            return NativeVoiceSoundEffect.toneSequence(
                sampleRate: sampleRate,
                tones: [(440, 0.05)],
                gap: 0
            )
        }
    }

    private static func toneSequence(
        sampleRate: Int,
        tones: [(frequency: Double, duration: Double)],
        gap: Double
    ) -> Data {
        var pcm = Data()
        for (index, tone) in tones.enumerated() {
            if index > 0, gap > 0 {
                appendSilence(to: &pcm, frames: Int(Double(sampleRate) * gap))
            }
            appendTone(to: &pcm, sampleRate: sampleRate, frequency: tone.frequency, duration: tone.duration)
        }
        return pcm
    }

    private static func toneSchedule(
        sampleRate: Int,
        tones: [(frequency: Double, offset: Double, duration: Double)]
    ) -> Data {
        var pcm = Data()
        var cursor = 0
        for tone in tones {
            let start = max(0, Int(Double(sampleRate) * tone.offset))
            if start > cursor {
                appendSilence(to: &pcm, frames: start - cursor)
                cursor = start
            }
            appendTone(to: &pcm, sampleRate: sampleRate, frequency: tone.frequency, duration: tone.duration)
            cursor += max(1, Int(Double(sampleRate) * tone.duration))
        }
        return pcm
    }

    private static func appendTone(
        to pcm: inout Data,
        sampleRate: Int,
        frequency: Double,
        duration: Double
    ) {
        let frames = max(1, Int(Double(sampleRate) * duration))
        for frame in 0..<frames {
            let envelope = min(1.0, min(Double(frame) / 240.0, Double(frames - frame) / 240.0))
            let phase = 2.0 * Double.pi * frequency * Double(frame) / Double(sampleRate)
            var sample = Int16((sin(phase) * envelope * 0.42 * Double(Int16.max)).rounded())
            pcm.append(Data(bytes: &sample, count: MemoryLayout<Int16>.size))
        }
    }

    private static func appendSilence(to pcm: inout Data, frames: Int) {
        guard frames > 0 else { return }
        pcm.append(Data(repeating: 0, count: frames * MemoryLayout<Int16>.size))
    }
}

@MainActor
private final class NativeAudioInput: NSObject, WKScriptMessageHandler, WKUIDelegate {
    let webView: WKWebView
    private var onAudio: (@Sendable (Data, Int, Int) -> Void)?
    private var onStatus: (@Sendable (String) -> Void)?
    private var startContinuation: CheckedContinuation<NativeAudioInputConfig, Error>?
    private var started = false

    override init() {
        let contentController = WKUserContentController()
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = contentController
        configuration.allowsAirPlayForMediaPlayback = false
        configuration.mediaTypesRequiringUserActionForPlayback = []
        self.webView = WKWebView(frame: .init(x: 0, y: 0, width: 1, height: 1), configuration: configuration)
        super.init()
        contentController.add(self, name: "irisAudio")
        webView.uiDelegate = self
    }

    func start(
        timeout: TimeInterval,
        onStatus: @escaping @Sendable (String) -> Void,
        onAudio: @escaping @Sendable (Data, Int, Int) -> Void
    ) async throws -> NativeAudioInputConfig {
        try await withCheckedThrowingContinuation { continuation in
            self.onStatus = onStatus
            self.onAudio = onAudio
            self.startContinuation = continuation
            self.started = false
            onStatus("Starting WebKit microphone")
            if let url = URL(string: "http://127.0.0.1:4747/debug/web-voice-capture") {
                webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData))
            }
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(timeout))
                guard let self, !self.started else { return }
                self.finish(.failure(NativeAudioInputError.startTimedOut))
            }
        }
    }

    func stop() {
        webView.evaluateJavaScript("window.irisStopCapture && window.irisStopCapture();") { _, _ in }
        onAudio = nil
        onStatus = nil
        started = false
        finish(.failure(NativeAudioInputError.webCapture("Microphone stopped")))
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "irisAudio", let object = message.body as? [String: Any], let type = object["type"] as? String else {
            return
        }
        switch type {
        case "status":
            if let text = object["text"] as? String {
                onStatus?(text)
            }
        case "started":
            started = true
            onStatus?("Microphone connected")
            finish(.success(NativeAudioInputConfig(sampleRate: 16_000, channels: 1)))
        case "audio":
            guard let encoded = object["audio"] as? String,
                  let data = Data(base64Encoded: encoded),
                  !data.isEmpty else {
                return
            }
            onAudio?(data, 16_000, 1)
        case "error":
            let text = object["text"] as? String ?? "WebKit microphone failed"
            onStatus?(text)
            finish(.failure(NativeAudioInputError.webCapture(text)))
        default:
            break
        }
    }

    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
    ) {
        if type == .microphone || type == .cameraAndMicrophone {
            decisionHandler(.grant)
        } else {
            decisionHandler(.deny)
        }
    }

    private func finish(_ result: Result<NativeAudioInputConfig, Error>) {
        guard let continuation = startContinuation else { return }
        startContinuation = nil
        switch result {
        case .success(let config):
            continuation.resume(returning: config)
        case .failure(let error):
            continuation.resume(throwing: error)
        }
    }
}

struct NativeVoiceCaptureHost: NSViewRepresentable {
    let view: NSView

    func makeNSView(context: Context) -> NSView {
        view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

private func nativePCM16Data(from buffer: AVAudioPCMBuffer) -> Data {
    guard let channelData = buffer.floatChannelData else { return Data() }
    let channels = Int(buffer.format.channelCount)
    let frames = Int(buffer.frameLength)
    var data = Data(capacity: frames * channels * MemoryLayout<Int16>.size)
    for frame in 0..<frames {
        for channel in 0..<channels {
            let sample = max(-1, min(1, channelData[channel][frame]))
            var intSample = Int16((sample * Float(Int16.max)).rounded())
            data.append(Data(bytes: &intSample, count: MemoryLayout<Int16>.size))
        }
    }
    return data
}

private extension Data {
    mutating func appendLittleEndian<T: FixedWidthInteger>(_ value: T) {
        var littleEndian = value.littleEndian
        append(Data(bytes: &littleEndian, count: MemoryLayout<T>.size))
    }
}
