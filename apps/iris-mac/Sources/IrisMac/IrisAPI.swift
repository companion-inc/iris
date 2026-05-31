import Foundation

actor IrisAPI {
    private var apiURL: URL
    private var voiceURL: URL
    private var bridgeURL: URL
    private let speakerIDURL = URL(string: "http://127.0.0.1:4749")!
    private let decoder: JSONDecoder

    init(
        apiURL: URL = URL(string: "http://127.0.0.1:4747")!,
        voiceURL: URL = URL(string: "http://127.0.0.1:4748")!,
        bridgeURL: URL = URL(string: "http://127.0.0.1:4750")!
    ) {
        self.apiURL = apiURL
        self.voiceURL = voiceURL
        self.bridgeURL = bridgeURL
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        self.decoder = decoder
    }

    func configure(apiURL: URL, voiceURL: URL, bridgeURL: URL) {
        self.apiURL = apiURL
        self.voiceURL = voiceURL
        self.bridgeURL = bridgeURL
    }

    func apiHealth() async -> HealthStatus {
        await decode(HealthStatus.self, from: apiURL.appending(path: "health")) ?? HealthStatus(ok: false, error: "API unavailable")
    }

    func voiceHealth() async -> HealthStatus {
        await decode(HealthStatus.self, from: voiceURL.appending(path: "health")) ?? HealthStatus(ok: false, error: "Voice unavailable")
    }

    func bridgeHealth() async -> BridgeHealth {
        await decode(BridgeHealth.self, from: bridgeURL.appending(path: "health")) ?? BridgeHealth(ok: false)
    }

    func speakerIDHealth() async -> HealthStatus {
        await decode(HealthStatus.self, from: speakerIDURL.appending(path: "health")) ?? HealthStatus(ok: false, error: "Speaker ID unavailable")
    }

    func transcripts(limit: Int = 40) async -> [TranscriptSegment] {
        var components = URLComponents(url: apiURL.appending(path: "v1/transcripts"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        guard let url = components?.url else { return [] }
        let response = await decode(TranscriptListResponse.self, from: url)
        return response?.segments ?? []
    }

    func voiceSessions(limit: Int = 20) async -> [VoiceSession] {
        var components = URLComponents(url: apiURL.appending(path: "v1/voice-sessions"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        guard let url = components?.url else { return [] }
        let response = await decode(VoiceSessionListResponse.self, from: url)
        return response?.sessions ?? []
    }

    func createVoiceSession(sampleRate: Int, channels: Int) async throws -> VoiceSessionStart {
        var request = URLRequest(url: apiURL.appending(path: "v1/voice/sessions"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "sampleRate": sampleRate,
            "channels": channels,
            "initialAwake": false
        ])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try decoder.decode(VoiceSessionStart.self, from: data)
    }

    private func decode<T: Decodable>(_ type: T.Type, from url: URL) async -> T? {
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                return nil
            }
            return try decoder.decode(T.self, from: data)
        } catch {
            return nil
        }
    }
}
