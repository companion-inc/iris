import SwiftUI

struct VoiceView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        ContentPage {
            Text("Voice")
                .font(.largeTitle.weight(.semibold))

            SectionBlock(title: "Microphone") {
                HStack(alignment: .center, spacing: 12) {
                    StatusPill(title: "Mic", running: appState.microphoneAllowed)
                    Text(appState.microphoneStatus)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Request Access") {
                        Task {
                            await appState.requestMicrophonePermission()
                        }
                    }
                    .disabled(appState.microphoneAllowed)
                }
            }

            SectionBlock(title: "Runtime") {
                VStack(alignment: .leading, spacing: 16) {
                    HStack {
                        StatusPill(title: "Voice", running: appState.voiceHealth.isRunning)
                        Text(appState.voiceHealth.service ?? "iris-voice")
                            .foregroundStyle(.secondary)
                        Spacer()
                    }
                    HStack {
                        StatusPill(title: "Native I/O", running: appState.nativeVoiceRunning)
                        Text(appState.nativeVoiceStatus)
                            .foregroundStyle(.secondary)
                        Spacer()
                    }
                    HStack {
                        Button("Start services") {
                            Task { await appState.start() }
                        }
                        Button(appState.nativeVoiceRunning ? "Stop listening" : "Start listening") {
                            if appState.nativeVoiceRunning {
                                appState.stopNativeVoice()
                            } else {
                                Task { await appState.startNativeVoice() }
                            }
                        }
                        .disabled(!appState.microphoneAllowed)
                        Button("Stop speech") {
                            appState.stopNativeSpeech()
                        }
                        .disabled(!appState.nativeVoiceAssistantSpeaking)
                        Button("Stop services") {
                            appState.stopNativeVoice()
                            appState.bridgeServer.stop()
                            appState.supervisor.stopAll()
                            Task { await appState.refresh() }
                        }
                    }
                }
            }

            SectionBlock(title: "Processes") {
                Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                    ProcessRow(name: "Local API", value: "In-process Swift")
                    ProcessRow(name: "Voice sidecar", value: appState.supervisor.voiceProcessID.map(String.init))
                    ProcessRow(name: "Sidecar launch", value: appState.supervisor.voiceLaunchCommand)
                    ProcessRow(name: "Codex app-server", pid: appState.bridgeHealth.codex?.runtime?.pid.map(String.init))
                    ProcessRow(name: "Native input frames", value: String(appState.nativeVoiceInputFrames))
                    ProcessRow(name: "Native output frames", value: String(appState.nativeVoiceOutputFrames))
                    ProcessRow(name: "Last event", value: appState.nativeVoiceLastEvent)
                }
                if let error = appState.supervisor.lastError {
                    Text(error)
                        .foregroundStyle(.red)
                        .font(.callout)
                }
            }
        }
    }
}

private struct ProcessRow: View {
    var name: String
    var value: String?

    init(name: String, value: String?) {
        self.name = name
        self.value = value
    }

    init(name: String, pid: String?) {
        self.name = name
        self.value = pid ?? "Not running"
    }

    var body: some View {
        GridRow {
            Text(name)
                .foregroundStyle(.secondary)
            Text(value ?? "Not running")
        }
    }
}
