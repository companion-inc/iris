import Foundation

struct HealthStatus: Decodable, Equatable {
    var ok: Bool?
    var service: String?
    var environment: String?
    var error: String?

    var isRunning: Bool {
        ok == true
    }
}

struct TranscriptSegment: Decodable, Identifiable, Equatable {
    var id: String
    var text: String
    var startedAt: Date?
    var speakerName: String?
    var emotionLabel: String?
}

struct TranscriptListResponse: Decodable {
    var segments: [TranscriptSegment]
}

struct VoiceSession: Decodable, Identifiable, Equatable {
    var id: String
    var status: String
    var startedAt: Date?
    var segments: [TranscriptSegment]?
}

struct VoiceSessionListResponse: Decodable {
    var sessions: [VoiceSession]
}

struct VoiceSessionStart: Decodable, Equatable {
    var voiceUrl: URL
    var sessionId: String
    var expiresAt: Date?
}

struct NativeVoiceDebugStatus: Sendable {
    var ok: Bool
    var running: Bool
    var status: String
    var lastEvent: String
    var inputFrames: Int
    var outputFrames: Int
    var sessionID: String?
    var liveTranscriptCount: Int
    var liveTranscripts: [NativeVoiceDebugTranscript]
    var microphoneAllowed: Bool
    var microphoneStatus: String
    var error: String?

    var jsonObject: [String: Any] {
        [
            "ok": ok,
            "running": running,
            "status": status,
            "lastEvent": lastEvent,
            "inputFrames": inputFrames,
            "outputFrames": outputFrames,
            "sessionId": sessionID ?? NSNull(),
            "liveTranscriptCount": liveTranscriptCount,
            "liveTranscripts": liveTranscripts.map(\.jsonObject),
            "microphoneAllowed": microphoneAllowed,
            "microphoneStatus": microphoneStatus,
            "error": error ?? NSNull()
        ]
    }
}

struct NativeVoiceDebugTranscript: Sendable {
    var id: String
    var text: String
    var speakerName: String?

    var jsonObject: [String: Any] {
        [
            "id": id,
            "text": text,
            "speakerName": speakerName ?? NSNull()
        ]
    }
}

struct IrisDevice: Decodable, Identifiable, Equatable {
    var id: String
    var kind: String
    var product: String?
    var model: String?
    var name: String?
    var status: String
    var deviceSerial: String?
    var firmwareVersion: String?
    var lastSeenAt: Date?

    var displayName: String {
        name ?? model ?? product ?? id
    }
}

struct BridgeHealth: Decodable, Equatable {
    var ok: Bool?
    var service: String?
    var agentId: String?
    var codex: CodexStatus?

    var isRunning: Bool {
        ok == true
    }
}

struct CodexStatus: Decodable, Equatable {
    var active: Bool?
    var runtime: CodexRuntime?
}

struct CodexRuntime: Decodable, Equatable {
    var cwd: String?
    var sandboxMode: String?
    var pid: Int?
}

enum IrisTab: String, CaseIterable, Identifiable {
    case home = "Home"
    case voice = "Voice"
    case devices = "Devices"
    case transcripts = "Transcripts"
    case settings = "Settings"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .home: "house"
        case .voice: "mic"
        case .devices: "sensor"
        case .transcripts: "doc.text"
        case .settings: "gearshape"
        }
    }
}
