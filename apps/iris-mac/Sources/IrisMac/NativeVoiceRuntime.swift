import AVFoundation
import Foundation
import SwiftUI

@MainActor
@Observable
final class NativeVoiceRuntime {
    private let api: IrisAPI
    private var audioPlayer: AVAudioPlayer?
    private var soundEffectPlayers: [AVAudioPlayer] = []
    private var soundEffectLastPlayed: [String: TimeInterval] = [:]
    private(set) var sessionID: String?
    private(set) var isRunning = false
    private(set) var status = "Idle"
    private(set) var lastEvent = "None"
    fileprivate(set) var inputFrames = 0
    private(set) var outputFrames = 0
    private(set) var liveTranscripts: [TranscriptSegment] = []
    private var seenLocalAudioEventKeys = Set<String>()

    init(api: IrisAPI) {
        self.api = api
    }

    func start() async {
        guard !isRunning else { return }
        do {
            status = "Starting Pipecat audio"
            liveTranscripts.removeAll()
            seenLocalAudioEventKeys.removeAll()
            let voiceSession = try await api.createVoiceSession(sampleRate: 16_000, channels: 1)
            sessionID = voiceSession.sessionId
            let localStatus = try await api.startLocalAudio(voiceUrl: voiceSession.voiceUrl)
            isRunning = true
            applyLocalAudioStatus(localStatus)
            if status == "Idle" {
                status = "Listening"
            }
        } catch {
            stop()
            status = error.localizedDescription
        }
    }

    func stop() {
        audioPlayer?.stop()
        audioPlayer = nil
        soundEffectPlayers.forEach { $0.stop() }
        soundEffectPlayers.removeAll()
        if isRunning {
            Task { [api] in
                _ = try? await api.stopLocalAudio(reason: "swift_ui_stop")
            }
        }
        sessionID = nil
        isRunning = false
        status = "Idle"
        liveTranscripts.removeAll()
        seenLocalAudioEventKeys.removeAll()
    }

    func stopSpeaking() {
        lastEvent = "local-audio.stop-speaking.requested"
        Task { [api] in
            do {
                let status = try await api.stopLocalAudioSpeaking(reason: "swift_ui_stop_speaking")
                await MainActor.run {
                    self.applyLocalAudioStatus(status)
                    self.lastEvent = status.recentEvents?.last?.type ?? "local-audio.stop-speaking.sent"
                }
            } catch {
                await MainActor.run {
                    self.lastEvent = "local-audio.stop-speaking.failed"
                    self.status = error.localizedDescription
                }
            }
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

    func refreshLocalAudioStatus() async {
        guard let localStatus = await api.localAudioStatus() else {
            if isRunning {
                status = "Voice unavailable"
            }
            return
        }
        applyLocalAudioStatus(localStatus)
    }

    private func handleMessageText(_ text: String) {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else {
            lastEvent = "text"
            return
        }
        guard type != "audio.activity" else { return }
        lastEvent = type
        if type == "transcript.final" || type == "transcript.interim" {
            handleTranscriptEvent(object, isInterim: type == "transcript.interim")
        } else if let effect = Self.soundEffect(forVoiceEvent: type) {
            playSoundEffect(effect)
        } else if type == "assistant.audio.started" {
            playSoundEffect(.assistantStart)
            status = "Iris speaking"
        } else if type == "assistant.audio.stopped" {
            playSoundEffect(.assistantStop)
            status = "Listening"
        }
    }

    nonisolated static func soundEffectID(forVoiceEvent type: String) -> String? {
        soundEffect(forVoiceEvent: type)?.id
    }

    nonisolated private static func soundEffect(forVoiceEvent type: String) -> NativeVoiceSoundEffect? {
        switch type {
        case "wake.accepted":
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
        let timestamp = object["at"] as? Double
        let speakerName = Self.displaySpeakerName(
            speaker: object["speaker"] as? String,
            displayName: object["speakerDisplayName"] as? String
        )
        handleTranscriptText(text, isInterim: isInterim, speakerName: speakerName, timestamp: timestamp)
    }

    private func handleTranscriptText(_ text: String, isInterim: Bool, speakerName: String?, timestamp: Double?) {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return }
        let startedAt = timestamp.map { Date(timeIntervalSince1970: $0) } ?? Date()
        let idSeed = "\(sessionID ?? "voice"):\(isInterim ? "interim" : "final"):\(timestamp ?? startedAt.timeIntervalSince1970):\(normalized)"
        let segment = TranscriptSegment(
            id: isInterim ? "live-interim-\(sessionID ?? "voice")" : "live-\(abs(idSeed.hashValue))",
            text: normalized,
            startedAt: startedAt,
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
            retainSoundEffectPlayer(player)
        } catch {
            lastEvent = "sound-effect.failed"
        }
    }

    private func retainSoundEffectPlayer(_ player: AVAudioPlayer) {
        soundEffectPlayers.removeAll { !$0.isPlaying }
        soundEffectPlayers.append(player)
        let lifetime = max(0.25, player.duration + 0.25)
        Task { [weak player] in
            try? await Task.sleep(nanoseconds: UInt64(lifetime * 1_000_000_000))
            guard let player else { return }
            soundEffectPlayers.removeAll { $0 === player || !$0.isPlaying }
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

    func applyLocalAudioStatus(_ localStatus: LocalAudioRuntimeStatus) {
        isRunning = localStatus.running
        if let nextSessionID = localStatus.sessionId, nextSessionID != sessionID {
            sessionID = nextSessionID
            liveTranscripts.removeAll()
            seenLocalAudioEventKeys.removeAll()
        } else {
            sessionID = localStatus.sessionId ?? sessionID
        }
        for event in localStatus.recentEvents ?? [] {
            let key = localAudioEventKey(event)
            guard !seenLocalAudioEventKeys.contains(key) else { continue }
            seenLocalAudioEventKeys.insert(key)
            handleLocalAudioEvent(event)
        }
        if seenLocalAudioEventKeys.count > 200 {
            seenLocalAudioEventKeys = Set(seenLocalAudioEventKeys.suffix(100))
        }
        if let error = localStatus.lastError, !error.isEmpty {
            status = error
        } else if localStatus.running {
            status = "Listening"
        } else {
            status = "Idle"
        }
    }

    private func handleLocalAudioEvent(_ event: LocalAudioRuntimeEvent) {
        guard event.type != "audio.activity" else { return }
        lastEvent = event.type
        switch event.type {
        case "transcript.final", "transcript.interim":
            guard let text = event.text else { return }
            handleTranscriptText(
                text,
                isInterim: event.type == "transcript.interim",
                speakerName: Self.displaySpeakerName(
                    speaker: event.speaker,
                    displayName: event.speakerDisplayName
                ),
                timestamp: event.at
            )
        case "assistant.turn.stopped":
            guard let text = event.text else { return }
            handleTranscriptText(
                text,
                isInterim: false,
                speakerName: "Iris",
                timestamp: event.at
            )
        case "assistant.audio.started":
            playSoundEffect(.assistantStart)
            status = "Iris speaking"
        case "assistant.audio.stopped":
            playSoundEffect(.assistantStop)
            status = "Listening"
        default:
            if let effect = Self.soundEffect(forVoiceEvent: event.type) {
                playSoundEffect(effect)
            }
        }
    }

    private func localAudioEventKey(_ event: LocalAudioRuntimeEvent) -> String {
        "\(event.at ?? 0):\(event.type):\(event.text ?? ""):\(event.reason ?? ""):\(event.speaker ?? ""):\(event.speakerDisplayName ?? "")"
    }

    private static func displaySpeakerName(speaker: String?, displayName: String?) -> String? {
        if let displayName = displayName?.trimmingCharacters(in: .whitespacesAndNewlines),
           !displayName.isEmpty {
            return displayName
        }
        guard let speaker = speaker?.trimmingCharacters(in: .whitespacesAndNewlines),
              !speaker.isEmpty else {
            return nil
        }
        if speaker.hasPrefix("SPEAKER_"),
           let index = Int(speaker.dropFirst("SPEAKER_".count)) {
            return "Speaker \(index + 1)"
        }
        return speaker
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
            return 0.12
        case .speaker:
            return 0.09
        case .sound:
            return 0.07
        case .tool, .done:
            return 0.09
        case .error:
            return 0.1
        case .assistantStart, .assistantStop:
            return 0.08
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
