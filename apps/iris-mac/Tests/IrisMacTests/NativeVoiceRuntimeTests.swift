import AVFoundation
import XCTest
@testable import IrisMac

final class NativeVoiceRuntimeTests: XCTestCase {
    func testPCM16RoundTripBufferConversion() throws {
        let format = try XCTUnwrap(
            AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)
        )
        let input = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4))
        input.frameLength = 4
        let channel = try XCTUnwrap(input.floatChannelData?[0])
        channel[0] = -1
        channel[1] = -0.5
        channel[2] = 0
        channel[3] = 0.5

        let pcm = NativeVoiceRuntime.pcm16Data(from: input)
        XCTAssertEqual(pcm.count, 8)

        let output = try XCTUnwrap(NativeVoiceRuntime.audioBuffer(fromPCM16: pcm, sampleRate: 16_000, channels: 1))
        XCTAssertEqual(output.frameLength, 4)
        let outputChannel = try XCTUnwrap(output.floatChannelData?[0])
        XCTAssertEqual(outputChannel[0], -1, accuracy: 0.001)
        XCTAssertEqual(outputChannel[1], -0.5, accuracy: 0.001)
        XCTAssertEqual(outputChannel[2], 0, accuracy: 0.001)
        XCTAssertEqual(outputChannel[3], 0.5, accuracy: 0.001)
    }

    func testVoiceEventSoundEffectMappingIncludesAgentCompletionEvents() {
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "wake.accepted"), "wake")
        XCTAssertNil(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "wake.detected"))
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "speaker.identified"), "speaker")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "sound.recognition.detected"), "sound")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "tool.started"), "tool")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "tool.finished"), "done")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "agent.completion.injected"), "done")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "hub.completion.injected"), "done")
        XCTAssertEqual(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "tool.failed"), "error")
        XCTAssertNil(NativeVoiceRuntime.soundEffectID(forVoiceEvent: "transcript.final"))
    }

    func testVoiceEventSoundEffectsUseAudibleVolume() {
        let eventTypes = [
            "wake.accepted",
            "speaker.identified",
            "sound.recognition.detected",
            "tool.started",
            "tool.finished",
            "tool.failed",
            "assistant.audio.started",
            "assistant.audio.stopped"
        ]

        for eventType in eventTypes {
            XCTAssertGreaterThanOrEqual(
                NativeVoiceRuntime.soundEffectVolume(forVoiceEvent: eventType) ?? 0,
                0.74,
                eventType
            )
        }
    }

    @MainActor
    func testLocalAudioStatusProcessesTranscriptBeforeLaterWakeStopEvent() {
        let runtime = NativeVoiceRuntime(api: IrisAPI())
        runtime.applyLocalAudioStatus(LocalAudioRuntimeStatus(
            ok: true,
            running: true,
            sessionId: "voice_test",
            uptimeSeconds: 1,
            lastError: nil,
            recentEvents: [
                LocalAudioRuntimeEvent(type: "ready", text: nil, reason: nil, at: 1),
                LocalAudioRuntimeEvent(type: "transcript.final", text: "Iris can you hear me", reason: nil, at: 2),
                LocalAudioRuntimeEvent(type: "wake.stopped", text: nil, reason: "timeout", at: 3)
            ]
        ))

        XCTAssertEqual(runtime.liveTranscripts.map(\.text), ["Iris can you hear me"])
        XCTAssertEqual(runtime.lastEvent, "wake.stopped")
        XCTAssertEqual(runtime.status, "Listening")
    }

    @MainActor
    func testLocalAudioStatusIgnoresInternalAudioActivityEvents() {
        let runtime = NativeVoiceRuntime(api: IrisAPI())
        runtime.applyLocalAudioStatus(LocalAudioRuntimeStatus(
            ok: true,
            running: true,
            sessionId: "voice_test",
            uptimeSeconds: 1,
            lastError: nil,
            recentEvents: [
                LocalAudioRuntimeEvent(type: "ready", text: nil, reason: nil, at: 1),
                LocalAudioRuntimeEvent(type: "audio.activity", text: nil, reason: nil, at: 2),
                LocalAudioRuntimeEvent(type: "transcript.final", text: "Iris hello", reason: nil, at: 3),
                LocalAudioRuntimeEvent(type: "audio.activity", text: nil, reason: nil, at: 4)
            ]
        ))

        XCTAssertEqual(runtime.liveTranscripts.map(\.text), ["Iris hello"])
        XCTAssertEqual(runtime.lastEvent, "transcript.final")
        XCTAssertEqual(runtime.status, "Listening")
    }

    @MainActor
    func testLocalAudioStatusPreservesSpeakerLabelsAndIrisTurns() {
        let runtime = NativeVoiceRuntime(api: IrisAPI())
        runtime.applyLocalAudioStatus(LocalAudioRuntimeStatus(
            ok: true,
            running: true,
            sessionId: "voice_test",
            uptimeSeconds: 1,
            lastError: nil,
            recentEvents: [
                LocalAudioRuntimeEvent(
                    type: "transcript.final",
                    text: "hello",
                    reason: nil,
                    speaker: "SPEAKER_0",
                    at: 1
                ),
                LocalAudioRuntimeEvent(
                    type: "assistant.turn.stopped",
                    text: "Hi.",
                    reason: nil,
                    at: 2
                )
            ]
        ))

        XCTAssertEqual(runtime.liveTranscripts.map(\.text), ["Hi.", "hello"])
        XCTAssertEqual(runtime.liveTranscripts.map(\.speakerName), ["Iris", "Speaker 1"])
        XCTAssertEqual(runtime.lastEvent, "assistant.turn.stopped")
    }

    @MainActor
    func testLocalAudioStatusClearsLiveTranscriptWhenSessionChanges() {
        let runtime = NativeVoiceRuntime(api: IrisAPI())
        runtime.applyLocalAudioStatus(LocalAudioRuntimeStatus(
            ok: true,
            running: true,
            sessionId: "voice_old",
            uptimeSeconds: 1,
            lastError: nil,
            recentEvents: [
                LocalAudioRuntimeEvent(type: "transcript.interim", text: "So", reason: nil, at: 1)
            ]
        ))

        XCTAssertEqual(runtime.liveTranscripts.map(\.text), ["So"])

        runtime.applyLocalAudioStatus(LocalAudioRuntimeStatus(
            ok: true,
            running: true,
            sessionId: "voice_new",
            uptimeSeconds: 1,
            lastError: nil,
            recentEvents: [
                LocalAudioRuntimeEvent(type: "ready", text: nil, reason: nil, at: 2)
            ]
        ))

        XCTAssertEqual(runtime.liveTranscripts, [])
        XCTAssertEqual(runtime.sessionID, "voice_new")
        XCTAssertEqual(runtime.lastEvent, "ready")
    }
}
