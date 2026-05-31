import Foundation
import AppKit

@MainActor
@Observable
final class IrisAppState {
    static let releaseAPIURL = URL(string: "https://api.github.com/repos/companion-inc/iris/releases/latest")!
    static let releaseDownloadsURL = URL(string: "https://github.com/companion-inc/iris/releases/latest")!

    var selectedTab: IrisTab = .home
    var apiHealth = HealthStatus(ok: false, service: "iris-api")
    var voiceHealth = HealthStatus(ok: false, service: "iris-voice")
    var speakerIDHealth = HealthStatus(ok: false, service: "iris-speaker-id")
    var bridgeHealth = BridgeHealth(ok: false)
    var transcripts: [TranscriptSegment] = []
    var voiceSessions: [VoiceSession] = []
    var devices: [IrisDevice] = []
    var homeClearedAt: Date?
    var isRefreshing = false
    var launchAtLoginEnabled = false
    var launchAtLoginStatus = "Unknown"
    var microphoneAllowed = false
    var microphoneStatus = "Unknown"
    var settingsStatus = "Native defaults active"
    var updateStatus = AppUpdateStatus(currentTag: IrisAppState.currentReleaseTag)
    var isCheckingForUpdates = false
    var deepgramAPIKeyConfigured = false
    var geminiAPIKeyConfigured = false
    var xaiAPIKeyConfigured = false
    var openAIAPIKeyConfigured = false
    var nativeVoiceStatus: String { voiceRuntime.status }
    var nativeVoiceRunning: Bool { voiceRuntime.isRunning }
    var nativeVoiceLastEvent: String { voiceRuntime.lastEvent }
    var nativeVoiceInputFrames: Int { voiceRuntime.inputFrames }
    var nativeVoiceOutputFrames: Int { voiceRuntime.outputFrames }
    var liveTranscripts: [TranscriptSegment] {
        var seen = Set<String>()
        return (voiceRuntime.liveTranscripts + transcripts).filter { segment in
            let key = "\(segment.text)|\(segment.startedAt?.timeIntervalSince1970 ?? 0)"
            guard !seen.contains(key) else { return false }
            seen.insert(key)
            return true
        }
    }

    let settings = NativeSettings()
    let supervisor: ProcessSupervisor
    let apiServer: SwiftLocalAPIServer
    private(set) var bridgeServer: SwiftCodexBridgeServer
    let voiceRuntime: NativeVoiceRuntime
    let secrets: NativeSecrets
    private let api: IrisAPI
    private let transcriptStore = NativeTranscriptStore()
    private let deviceStore = NativeDeviceStore()
    private var bridgeWorkspaceURL: URL

    init() {
        self.supervisor = ProcessSupervisor()
        self.apiServer = SwiftLocalAPIServer()
        self.secrets = NativeSecrets(defaults: settings.defaultsStore)
        let endpoints = (try? settings.resolvedEndpoints()) ?? NativeSettingsEndpoints(
            apiURL: URL(string: NativeSettings.defaultAPIURL)!,
            voiceURL: URL(string: NativeSettings.defaultVoiceURL)!,
            bridgeURL: URL(string: NativeSettings.defaultBridgeURL)!,
            workspaceURL: URL(fileURLWithPath: NativeSettings.defaultWorkspacePath, isDirectory: true)
        )
        self.api = IrisAPI(apiURL: endpoints.apiURL, voiceURL: endpoints.voiceURL, bridgeURL: endpoints.bridgeURL)
        self.voiceRuntime = NativeVoiceRuntime(api: api)
        self.bridgeWorkspaceURL = endpoints.workspaceURL
        self.bridgeServer = SwiftCodexBridgeServer(
            workspace: endpoints.workspaceURL,
            completionSink: { [weak apiServer] completion in
                await apiServer?.recordAgentBridgeCompletion(completion)
            }
        )
        apiServer.setNativeVoiceStatusProvider { [weak voiceRuntime] in
            if let voiceRuntime {
                await voiceRuntime.refreshLocalAudioStatus()
            }
            return await MainActor.run {
                guard let voiceRuntime else {
                    let permissionStatus = MicrophonePermission.statusDescription
                    return NativeVoiceDebugStatus(
                        ok: false,
                        running: false,
                        status: "Unavailable",
                        lastEvent: "None",
                        inputFrames: 0,
                        outputFrames: 0,
                        sessionID: nil,
                        liveTranscriptCount: 0,
                        liveTranscripts: [],
                        microphoneAllowed: MicrophonePermission.isGranted,
                        microphoneStatus: permissionStatus,
                        error: "native voice unavailable"
                    )
                }
                let effectiveMicrophoneAllowed = voiceRuntime.isRunning || MicrophonePermission.isGranted
                let effectiveMicrophoneStatus = voiceRuntime.isRunning ? "Allowed" : MicrophonePermission.statusDescription
                return NativeVoiceDebugStatus(
                    ok: true,
                    running: voiceRuntime.isRunning,
                    status: voiceRuntime.status,
                    lastEvent: voiceRuntime.lastEvent,
                    inputFrames: voiceRuntime.inputFrames,
                    outputFrames: voiceRuntime.outputFrames,
                    sessionID: voiceRuntime.sessionID,
                    liveTranscriptCount: voiceRuntime.liveTranscripts.count,
                    liveTranscripts: voiceRuntime.liveTranscripts.prefix(5).map { segment in
                        NativeVoiceDebugTranscript(id: segment.id, text: segment.text, speakerName: segment.speakerName)
                    },
                    microphoneAllowed: effectiveMicrophoneAllowed,
                    microphoneStatus: effectiveMicrophoneStatus,
                    error: nil
                )
            }
        }
        apiServer.setNativeVoicePlaybackTester { [weak voiceRuntime] in
            await MainActor.run {
                guard let voiceRuntime else { return false }
                return voiceRuntime.playDebugTone()
            }
        }
        apiServer.setNativeVoiceEventTester { [weak voiceRuntime] type in
            await MainActor.run {
                guard let voiceRuntime else { return false }
                return voiceRuntime.debugInjectVoiceEvent(type)
            }
        }
        apiServer.setNativeVoiceStarter { [weak self] in
            guard let self else { return false }
            await self.startNativeVoiceIfPossible()
            return await MainActor.run { self.voiceRuntime.isRunning }
        }
        do {
            try apiServer.start()
        } catch {
            supervisor.lastError = error.localizedDescription
        }
        do {
            try bridgeServer.start()
        } catch {
            supervisor.lastError = error.localizedDescription
        }
        refreshLaunchAtLogin()
        refreshMicrophonePermission()
        refreshSecretStatus()
        supervisor.startAll(voiceEnvironment: voiceEnvironment())
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            await self?.startNativeVoiceWhenReady()
        }
        Task { [weak self] in
            await self?.checkForUpdates()
        }
    }

    static var currentReleaseTag: String {
        Bundle.main.object(forInfoDictionaryKey: "IRISReleaseTag") as? String ?? "development"
    }

    var workspacePath: String {
        bridgeHealth.codex?.runtime?.cwd ?? settings.workspacePath
    }

    var latestSession: VoiceSession? {
        guard let session = voiceSessions.first else { return nil }
        if let homeClearedAt, let startedAt = session.startedAt, startedAt <= homeClearedAt {
            return nil
        }
        return session
    }

    func start() async {
        await configureRuntimeFromSettings(restartBridge: false)
        do {
            try apiServer.start()
        } catch {
            supervisor.lastError = error.localizedDescription
        }
        do {
            try bridgeServer.start()
        } catch {
            supervisor.lastError = error.localizedDescription
        }
        supervisor.startAll(voiceEnvironment: voiceEnvironment())
        refreshLaunchAtLogin()
        refreshMicrophonePermission()
        refreshSecretStatus()
        if updateStatus.checkedAt == nil {
            await checkForUpdates()
        }
        await refresh()
    }

    func refresh() async {
        isRefreshing = true
        await configureRuntimeFromSettings(restartBridge: false)
        async let apiHealth = api.apiHealth()
        async let voiceHealth = api.voiceHealth()
        async let speakerIDHealth = api.speakerIDHealth()
        async let bridgeHealth = api.bridgeHealth()
        async let transcripts = transcriptStore.transcripts()
        async let voiceSessions = transcriptStore.voiceSessions()
        async let devices = deviceStore.devices()
        self.apiHealth = await apiHealth
        self.voiceHealth = await voiceHealth
        self.speakerIDHealth = await speakerIDHealth
        self.bridgeHealth = await bridgeHealth
        self.transcripts = await transcripts
        self.voiceSessions = await voiceSessions
        self.devices = await devices
        refreshLaunchAtLogin()
        refreshMicrophonePermission()
        refreshSecretStatus()
        isRefreshing = false
    }

    func checkForUpdates() async {
        isCheckingForUpdates = true
        defer { isCheckingForUpdates = false }
        do {
            var request = URLRequest(url: Self.releaseAPIURL)
            request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            request.setValue("IrisMac/0.1", forHTTPHeaderField: "User-Agent")
            let (data, response) = try await URLSession.shared.data(for: request)
            if let httpResponse = response as? HTTPURLResponse, !(200..<300).contains(httpResponse.statusCode) {
                throw URLError(.badServerResponse)
            }
            let release = try JSONDecoder().decode(GitHubRelease.self, from: data)
            let downloadURL = release.assets.first(where: { $0.name == "Iris-macOS-arm64.zip" })?.browserDownloadURL
            updateStatus = AppUpdateStatus(
                currentTag: Self.currentReleaseTag,
                latestTag: release.tagName,
                releaseURL: release.htmlURL,
                downloadURL: downloadURL,
                checkedAt: Date(),
                error: nil
            )
        } catch {
            updateStatus = AppUpdateStatus(
                currentTag: Self.currentReleaseTag,
                latestTag: updateStatus.latestTag,
                releaseURL: updateStatus.releaseURL,
                downloadURL: updateStatus.downloadURL,
                checkedAt: Date(),
                error: error.localizedDescription
            )
        }
    }

    func openUpdateDownload() {
        let url = updateStatus.downloadURL ?? updateStatus.releaseURL ?? Self.releaseDownloadsURL
        NSWorkspace.shared.open(url)
    }

    func clearHome() {
        homeClearedAt = Date()
    }

    func refreshLaunchAtLogin() {
        launchAtLoginEnabled = LaunchAtLogin.isEnabled
        launchAtLoginStatus = LaunchAtLogin.statusDescription
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try LaunchAtLogin.setEnabled(enabled)
            supervisor.lastError = nil
        } catch {
            supervisor.lastError = error.localizedDescription
        }
        refreshLaunchAtLogin()
    }

    func refreshMicrophonePermission() {
        microphoneAllowed = voiceRuntime.isRunning || MicrophonePermission.isGranted
        microphoneStatus = voiceRuntime.isRunning ? "Allowed" : MicrophonePermission.statusDescription
    }

    func requestMicrophonePermission() async {
        _ = await MicrophonePermission.request()
        refreshMicrophonePermission()
    }

    func refreshSecretStatus() {
        deepgramAPIKeyConfigured = secrets.configured(.deepgramAPIKey)
        geminiAPIKeyConfigured = secrets.configured(.geminiAPIKey)
        xaiAPIKeyConfigured = secrets.configured(.xaiAPIKey)
        openAIAPIKeyConfigured = secrets.configured(.openAIAPIKey)
    }

    func saveSecrets(deepgram: String, gemini: String, xai: String, openAI: String) {
        do {
            try saveSecretIfPresent(.deepgramAPIKey, value: deepgram)
            try saveSecretIfPresent(.geminiAPIKey, value: gemini)
            try saveSecretIfPresent(.xaiAPIKey, value: xai)
            try saveSecretIfPresent(.openAIAPIKey, value: openAI)
            refreshSecretStatus()
            settingsStatus = "Provider keys saved locally"
            supervisor.lastError = nil
        } catch {
            settingsStatus = error.localizedDescription
            supervisor.lastError = error.localizedDescription
        }
    }

    func clearSecret(_ kind: NativeSecretKind) {
        do {
            try secrets.delete(kind)
            refreshSecretStatus()
            settingsStatus = "Secret cleared"
        } catch {
            settingsStatus = error.localizedDescription
            supervisor.lastError = error.localizedDescription
        }
    }

    func startNativeVoice() async {
        await configureRuntimeFromSettings(restartBridge: false)
        refreshMicrophonePermission()
        supervisor.startAll(voiceEnvironment: voiceEnvironment())
        await waitForVoiceSidecar()
        await voiceRuntime.start()
        refreshMicrophonePermission()
        await refresh()
    }

    func startNativeVoiceIfPossible() async {
        await voiceRuntime.refreshLocalAudioStatus()
        refreshMicrophonePermission()
        if MicrophonePermission.isNotDetermined {
            voiceRuntime.setStatus("Requesting microphone access")
            let granted = await MicrophonePermission.request()
            refreshMicrophonePermission()
            guard granted else {
                voiceRuntime.setStatus("Microphone access required")
                return
            }
        }
        if microphoneStatus == "Denied" || microphoneStatus == "Restricted" {
            voiceRuntime.setStatus("Microphone \(microphoneStatus.lowercased())")
            return
        }
        guard !voiceRuntime.isRunning else { return }
        await startNativeVoice()
    }

    func startNativeVoiceWhenReady() async {
        await startNativeVoiceIfPossible()
    }

    func stopNativeVoice() {
        voiceRuntime.stop()
        refreshMicrophonePermission()
    }

    func stopNativeSpeech() {
        voiceRuntime.stopSpeaking()
    }

    func applySettings() async {
        await configureRuntimeFromSettings(restartBridge: true)
        supervisor.startAll(voiceEnvironment: voiceEnvironment())
        await refresh()
    }

    func resetSettings() async {
        settings.reset()
        await applySettings()
    }

    private func configureRuntimeFromSettings(restartBridge: Bool) async {
        do {
            let endpoints = try settings.resolvedEndpoints()
            await api.configure(apiURL: endpoints.apiURL, voiceURL: endpoints.voiceURL, bridgeURL: endpoints.bridgeURL)
            if restartBridge && endpoints.workspaceURL.standardizedFileURL != bridgeWorkspaceURL.standardizedFileURL {
                bridgeServer.stop()
                bridgeServer = SwiftCodexBridgeServer(
                    workspace: endpoints.workspaceURL,
                    completionSink: { [weak apiServer] completion in
                        await apiServer?.recordAgentBridgeCompletion(completion)
                    }
                )
                bridgeWorkspaceURL = endpoints.workspaceURL
                try bridgeServer.start()
            }
            settingsStatus = "Applied"
            supervisor.lastError = nil
        } catch {
            settingsStatus = error.localizedDescription
            supervisor.lastError = error.localizedDescription
        }
    }

    private func saveSecretIfPresent(_ kind: NativeSecretKind, value: String) throws {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty else { return }
        try secrets.write(normalized, for: kind)
    }

    private func waitForVoiceSidecar() async {
        for _ in 0..<10 {
            let health = await api.voiceHealth()
            voiceHealth = health
            if health.isRunning {
                return
            }
            try? await Task.sleep(for: .milliseconds(500))
        }
    }

    private func voiceEnvironment() -> [String: String] {
        var environment = secrets.environment()
        environment["IRIS_STT_PROVIDER"] = settings.sttProvider
        environment["IRIS_STT_LANGUAGE"] = settings.sttLanguage
        environment["IRIS_LLM_PROVIDER"] = settings.llmProvider
        environment["IRIS_TTS_PROVIDER"] = settings.ttsProvider
        return environment
    }
}
