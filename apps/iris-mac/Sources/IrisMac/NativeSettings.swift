import Foundation

@MainActor
@Observable
final class NativeSettings {
    static let defaultWorkspacePath = "\(NSHomeDirectory())/Iris/Workspace"
    static let defaultAPIURL = "http://127.0.0.1:4747"
    static let defaultVoiceURL = "http://127.0.0.1:4748"
    static let defaultBridgeURL = "http://127.0.0.1:4750"

    var workspacePath: String {
        didSet {
            defaults.set(workspacePath, forKey: Keys.workspacePath)
        }
    }

    var apiURL: String {
        didSet {
            defaults.set(apiURL, forKey: Keys.apiURL)
        }
    }

    var voiceURL: String {
        didSet {
            defaults.set(voiceURL, forKey: Keys.voiceURL)
        }
    }

    var bridgeURL: String {
        didSet {
            defaults.set(bridgeURL, forKey: Keys.bridgeURL)
        }
    }

    var sttProvider: String {
        didSet {
            defaults.set(sttProvider, forKey: Keys.sttProvider)
        }
    }

    var llmProvider: String {
        didSet {
            defaults.set(llmProvider, forKey: Keys.llmProvider)
        }
    }

    var ttsProvider: String {
        didSet {
            defaults.set(ttsProvider, forKey: Keys.ttsProvider)
        }
    }

    private let defaults: UserDefaults

    var defaultsStore: UserDefaults {
        defaults
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        workspacePath = defaults.string(forKey: Keys.workspacePath) ?? Self.defaultWorkspacePath
        apiURL = defaults.string(forKey: Keys.apiURL) ?? Self.defaultAPIURL
        voiceURL = defaults.string(forKey: Keys.voiceURL) ?? Self.defaultVoiceURL
        bridgeURL = defaults.string(forKey: Keys.bridgeURL) ?? Self.defaultBridgeURL
        sttProvider = defaults.string(forKey: Keys.sttProvider) ?? "deepgram"
        llmProvider = defaults.string(forKey: Keys.llmProvider) ?? "gemini"
        ttsProvider = defaults.string(forKey: Keys.ttsProvider) ?? "xai"
    }

    func reset() {
        workspacePath = Self.defaultWorkspacePath
        apiURL = Self.defaultAPIURL
        voiceURL = Self.defaultVoiceURL
        bridgeURL = Self.defaultBridgeURL
        sttProvider = "deepgram"
        llmProvider = "gemini"
        ttsProvider = "xai"
    }

    func resolvedEndpoints() throws -> NativeSettingsEndpoints {
        let api = try endpointURL(apiURL, name: "API URL")
        let voice = try endpointURL(voiceURL, name: "Voice URL")
        let bridge = try endpointURL(bridgeURL, name: "Bridge URL")
        let workspace = URL(fileURLWithPath: expandedPath(workspacePath), isDirectory: true)
        return NativeSettingsEndpoints(apiURL: api, voiceURL: voice, bridgeURL: bridge, workspaceURL: workspace)
    }

    private func endpointURL(_ rawValue: String, name: String) throws -> URL {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              url.host != nil else {
            throw NativeSettingsError.invalidURL(name)
        }
        return url
    }

    private func expandedPath(_ rawValue: String) -> String {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed == "~" {
            return NSHomeDirectory()
        }
        if trimmed.hasPrefix("~/") {
            return NSHomeDirectory() + "/" + trimmed.dropFirst(2)
        }
        return trimmed.isEmpty ? Self.defaultWorkspacePath : trimmed
    }

    enum Keys {
        static let workspacePath = "workspacePath"
        static let apiURL = "apiURL"
        static let voiceURL = "voiceURL"
        static let bridgeURL = "bridgeURL"
        static let sttProvider = "sttProvider"
        static let llmProvider = "llmProvider"
        static let ttsProvider = "ttsProvider"
    }
}

struct NativeSettingsEndpoints: Equatable {
    var apiURL: URL
    var voiceURL: URL
    var bridgeURL: URL
    var workspaceURL: URL
}

enum NativeSettingsError: LocalizedError {
    case invalidURL(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL(let name):
            return "\(name) must be an http:// or https:// URL."
        }
    }
}
