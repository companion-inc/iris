import Foundation

enum NativeSecretKind: String, CaseIterable {
    case deepgramAPIKey = "deepgram-api-key"
    case geminiAPIKey = "gemini-api-key"
    case xaiAPIKey = "xai-api-key"
    case openAIAPIKey = "openai-api-key"

    var environmentName: String {
        switch self {
        case .deepgramAPIKey: "DEEPGRAM_API_KEY"
        case .geminiAPIKey: "GEMINI_API_KEY"
        case .xaiAPIKey: "XAI_API_KEY"
        case .openAIAPIKey: "OPENAI_API_KEY"
        }
    }

    var displayName: String {
        switch self {
        case .deepgramAPIKey: "Deepgram"
        case .geminiAPIKey: "Gemini"
        case .xaiAPIKey: "xAI"
        case .openAIAPIKey: "OpenAI"
        }
    }

    init?(provider: String) {
        switch provider.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "deepgram": self = .deepgramAPIKey
        case "gemini": self = .geminiAPIKey
        case "xai": self = .xaiAPIKey
        case "openai": self = .openAIAPIKey
        default: return nil
        }
    }
}

protocol NativeSecretStoring {
    func read(_ kind: NativeSecretKind) -> String?
    func write(_ value: String, for kind: NativeSecretKind) throws
    func delete(_ kind: NativeSecretKind) throws
}

final class NativeSecrets: NativeSecretStoring {
    private let defaults: UserDefaults
    private let keyPrefix: String

    init(defaults: UserDefaults = .standard, service: String = "providerKeys") {
        self.defaults = defaults
        self.keyPrefix = "\(service)."
    }

    func read(_ kind: NativeSecretKind) -> String? {
        nonEmpty(defaults.string(forKey: key(kind)))
    }

    func write(_ value: String, for kind: NativeSecretKind) throws {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else {
            try delete(kind)
            return
        }
        defaults.set(normalized, forKey: key(kind))
    }

    func delete(_ kind: NativeSecretKind) throws {
        defaults.removeObject(forKey: key(kind))
    }

    func configured(_ kind: NativeSecretKind) -> Bool {
        read(kind) != nil
    }

    func environment() -> [String: String] {
        var result: [String: String] = [:]
        for kind in NativeSecretKind.allCases {
            if let value = read(kind) {
                result[kind.environmentName] = value
            }
        }
        return result
    }

    private func key(_ kind: NativeSecretKind) -> String {
        "\(keyPrefix)\(kind.rawValue)"
    }

    private func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }
}
