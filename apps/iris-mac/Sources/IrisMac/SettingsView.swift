import SwiftUI

struct SettingsView: View {
    @Environment(IrisAppState.self) private var appState
    @State private var showProviderKeys = false

    private var selectedSecretKinds: [NativeSecretKind] {
        var kinds: [NativeSecretKind] = []
        appendSecretKind(for: appState.settings.sttProvider, to: &kinds)
        appendSecretKind(for: appState.settings.llmProvider, to: &kinds)
        appendSecretKind(for: appState.settings.ttsProvider, to: &kinds)
        return kinds
    }

    private var missingSecretKinds: [NativeSecretKind] {
        selectedSecretKinds.filter { !secretConfigured($0) }
    }

    var body: some View {
        ContentPage {
            Text("Settings")
                .font(.largeTitle.weight(.semibold))

            SectionBlock(title: "Mac") {
                VStack(alignment: .leading, spacing: 12) {
                    Toggle(
                        "Launch at login",
                        isOn: Binding(
                            get: { appState.launchAtLoginEnabled },
                            set: { appState.setLaunchAtLogin($0) }
                        )
                    )
                    SettingsRow(title: "Login item", value: appState.launchAtLoginStatus)
                    Divider()
                    SettingsRow(title: "Microphone", value: appState.microphoneStatus)
                    Button("Request microphone access") {
                        Task {
                            await appState.requestMicrophonePermission()
                        }
                    }
                    .disabled(appState.microphoneAllowed)
                }
            }

            SectionBlock(title: "Updates") {
                VStack(alignment: .leading, spacing: 12) {
                    SettingsRow(title: "Current", value: appState.updateStatus.currentTag)
                    SettingsRow(title: "Latest", value: appState.updateStatus.latestTag ?? "Not checked")
                    SettingsRow(title: "Status", value: appState.updateStatus.displayText)
                    HStack {
                        Button(appState.isCheckingForUpdates ? "Checking..." : "Check for updates") {
                            Task {
                                await appState.checkForUpdates()
                            }
                        }
                        .disabled(appState.isCheckingForUpdates)
                        Button(appState.updateStatus.updateAvailable ? "Download update" : "Open releases") {
                            appState.openUpdateDownload()
                        }
                    }
                }
            }

            SectionBlock(title: "Codex") {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Workspace")
                        .foregroundStyle(.secondary)
                    TextField("Workspace", text: Bindable(appState.settings).workspacePath)
                        .textFieldStyle(.roundedBorder)
                    Text(appState.workspacePath)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                }
                Divider()
                SettingsRow(title: "Sandbox", value: appState.bridgeHealth.codex?.runtime?.sandboxMode ?? "Unknown")
                SettingsRow(title: "Bridge PID", value: appState.bridgeHealth.codex?.runtime?.pid.map(String.init) ?? "Unknown")
            }

            SectionBlock(title: "Local services") {
                TextField("API URL", text: Bindable(appState.settings).apiURL)
                    .textFieldStyle(.roundedBorder)
                TextField("Voice URL", text: Bindable(appState.settings).voiceURL)
                    .textFieldStyle(.roundedBorder)
                TextField("Bridge URL", text: Bindable(appState.settings).bridgeURL)
                    .textFieldStyle(.roundedBorder)
                Divider()
                SettingsRow(title: "API", value: appState.apiHealth.isRunning ? "Running" : "Unavailable")
                SettingsRow(title: "Voice", value: appState.voiceHealth.isRunning ? "Running" : "Unavailable")
                SettingsRow(title: "Speaker ID", value: appState.speakerIDHealth.isRunning ? "Running" : "Unavailable")
                SettingsRow(title: "Bridge", value: appState.bridgeHealth.isRunning ? "Running" : "Unavailable")
                SettingsRow(title: "Voice PID", value: appState.supervisor.voiceProcessID.map(String.init) ?? "External or stopped")
                SettingsRow(title: "Speaker ID PID", value: appState.supervisor.speakerIDProcessID.map(String.init) ?? "External or stopped")
                SettingsRow(title: "Settings", value: appState.settingsStatus)
                HStack {
                    Button("Apply settings") {
                        Task {
                            await appState.applySettings()
                        }
                    }
                    Button("Reset native defaults") {
                        Task {
                            await appState.resetSettings()
                        }
                    }
                }
            }

            SectionBlock(title: "Local API keys") {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(providerSummary)
                            .font(.callout)
                        Text("Keys are saved locally and injected into voice services when they start.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Manage") {
                        showProviderKeys = true
                    }
                }
            }
        }
        .sheet(isPresented: $showProviderKeys) {
            ProviderKeysSheet()
                .environment(appState)
                .preferredColorScheme(.light)
        }
    }

    private var providerSummary: String {
        if !missingSecretKinds.isEmpty {
            return "Missing \(missingSecretKinds.map(\.displayName).joined(separator: ", "))"
        }
        return "\(providerLabel(appState.settings.sttProvider)) STT · \(providerLabel(appState.settings.llmProvider)) brain · \(providerLabel(appState.settings.ttsProvider)) voice"
    }

    private func providerLabel(_ provider: String) -> String {
        switch provider.lowercased() {
        case "deepgram": "Deepgram"
        case "gemini": "Gemini"
        case "openai": "OpenAI"
        case "xai": "xAI"
        default: provider
        }
    }

    private func secretConfigured(_ kind: NativeSecretKind) -> Bool {
        switch kind {
        case .deepgramAPIKey: appState.deepgramAPIKeyConfigured
        case .geminiAPIKey: appState.geminiAPIKeyConfigured
        case .xaiAPIKey: appState.xaiAPIKeyConfigured
        case .openAIAPIKey: appState.openAIAPIKeyConfigured
        }
    }

    private func appendSecretKind(for provider: String, to kinds: inout [NativeSecretKind]) {
        guard let kind = NativeSecretKind(provider: provider), !kinds.contains(kind) else {
            return
        }
        kinds.append(kind)
    }
}

private struct ProviderKeysSheet: View {
    @Environment(IrisAppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var deepgramAPIKey = ""
    @State private var geminiAPIKey = ""
    @State private var xaiAPIKey = ""
    @State private var openAIAPIKey = ""

    private var selectedSecretKinds: [NativeSecretKind] {
        var kinds: [NativeSecretKind] = []
        appendSecretKind(for: appState.settings.sttProvider, to: &kinds)
        appendSecretKind(for: appState.settings.llmProvider, to: &kinds)
        appendSecretKind(for: appState.settings.ttsProvider, to: &kinds)
        return kinds
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Local API keys")
                        .font(.title2.weight(.semibold))
                    Text("Only the keys required by the selected providers are shown.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Done") {
                    dismiss()
                }
            }

            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                GridRow {
                    Text("STT")
                        .foregroundStyle(.secondary)
                    Picker("STT", selection: Bindable(appState.settings).sttProvider) {
                        Text("Deepgram").tag("deepgram")
                        Text("OpenAI").tag("openai")
                    }
                    .labelsHidden()
                }
                GridRow {
                    Text("LLM")
                        .foregroundStyle(.secondary)
                    Picker("LLM", selection: Bindable(appState.settings).llmProvider) {
                        Text("Gemini").tag("gemini")
                        Text("OpenAI").tag("openai")
                    }
                    .labelsHidden()
                }
                GridRow {
                    Text("TTS")
                        .foregroundStyle(.secondary)
                    Picker("TTS", selection: Bindable(appState.settings).ttsProvider) {
                        Text("xAI").tag("xai")
                        Text("Deepgram").tag("deepgram")
                        Text("OpenAI").tag("openai")
                    }
                    .labelsHidden()
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 12) {
                ForEach(selectedSecretKinds, id: \.self) { kind in
                    SecretField(
                        title: kind.displayName,
                        placeholder: secretConfigured(kind) ? "Configured" : kind.environmentName,
                        text: secretBinding(kind),
                        configured: secretConfigured(kind),
                        clear: { appState.clearSecret(kind) }
                    )
                }
            }

            HStack {
                Button("Save keys") {
                    appState.saveSecrets(
                        deepgram: deepgramAPIKey,
                        gemini: geminiAPIKey,
                        xai: xaiAPIKey,
                        openAI: openAIAPIKey
                    )
                    clearDrafts()
                }
                Button("Apply and restart services") {
                    Task {
                        appState.supervisor.stopAll()
                        await appState.applySettings()
                    }
                }
                Spacer()
            }
        }
        .padding(24)
        .frame(width: 640)
    }

    private func secretBinding(_ kind: NativeSecretKind) -> Binding<String> {
        switch kind {
        case .deepgramAPIKey: $deepgramAPIKey
        case .geminiAPIKey: $geminiAPIKey
        case .xaiAPIKey: $xaiAPIKey
        case .openAIAPIKey: $openAIAPIKey
        }
    }

    private func secretConfigured(_ kind: NativeSecretKind) -> Bool {
        switch kind {
        case .deepgramAPIKey: appState.deepgramAPIKeyConfigured
        case .geminiAPIKey: appState.geminiAPIKeyConfigured
        case .xaiAPIKey: appState.xaiAPIKeyConfigured
        case .openAIAPIKey: appState.openAIAPIKeyConfigured
        }
    }

    private func appendSecretKind(for provider: String, to kinds: inout [NativeSecretKind]) {
        guard let kind = NativeSecretKind(provider: provider), !kinds.contains(kind) else {
            return
        }
        kinds.append(kind)
    }

    private func clearDrafts() {
        deepgramAPIKey = ""
        geminiAPIKey = ""
        xaiAPIKey = ""
        openAIAPIKey = ""
    }
}

private struct SecretField: View {
    var title: String
    var placeholder: String
    @Binding var text: String
    var configured: Bool
    var clear: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Text(title)
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            SecureField(placeholder, text: $text)
                .textFieldStyle(.roundedBorder)
            Text(configured ? "Saved" : "Missing")
                .font(.caption.weight(.medium))
                .foregroundStyle(configured ? .green : .secondary)
                .frame(width: 54, alignment: .leading)
            Button("Clear", action: clear)
                .disabled(!configured)
        }
    }
}

private struct SettingsRow: View {
    var title: String
    var value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            Text(value)
                .textSelection(.enabled)
            Spacer()
        }
        .font(.callout)
        .padding(.vertical, 7)
    }
}
