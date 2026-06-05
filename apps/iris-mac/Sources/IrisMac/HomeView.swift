import SwiftUI
import AppKit

struct HomeView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        ContentPage {
            VStack(alignment: .leading, spacing: 28) {
                header
                statusStrip
                liveSection
                recentSection
            }
        }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Iris")
                    .font(.system(size: 34, weight: .semibold))
                Text(appState.bridgeHealth.codex?.active == true ? "Codex is working" : "Ready on this Mac")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if appState.nativeVoiceRunning {
                Button {
                    appState.stopNativeSpeech()
                } label: {
                    Label("Stop Speech", systemImage: "speaker.slash")
                }
                .buttonStyle(.borderedProminent)
            }
            Button {
                appState.clearHome()
            } label: {
                Label("Clear Home", systemImage: "trash")
            }
            .buttonStyle(.bordered)
        }
    }

    private var statusStrip: some View {
        HStack(spacing: 10) {
            StatusPill(title: "API", running: appState.apiHealth.isRunning)
            StatusPill(title: "Voice", running: appState.voiceHealth.isRunning)
            StatusPill(title: "Codex", running: appState.bridgeHealth.isRunning)
            if appState.updateStatus.updateAvailable {
                Button {
                    appState.openUpdateDownload()
                } label: {
                    Label("Update", systemImage: "arrow.down.circle")
                        .font(.caption.weight(.medium))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }
        }
    }

    private var liveSection: some View {
        SectionBlock(title: "Live") {
            VStack(alignment: .leading, spacing: 12) {
                if appState.liveTranscripts.isEmpty {
                    if appState.nativeVoiceRunning {
                        EmptyState(
                            title: "Listening.",
                            subtitle: "Live transcripts appear here."
                        )
                    } else {
                        microphoneActionState
                    }
                } else {
                    ForEach(appState.liveTranscripts.prefix(5)) { segment in
                        TranscriptRow(segment: segment, compact: true)
                    }
                }
            }
        }
    }

    private var recentSection: some View {
        SectionBlock(title: "Recent") {
            if let latestSession = appState.latestSession {
                VStack(alignment: .leading, spacing: 10) {
                    if let startedAt = latestSession.startedAt {
                        Text(startedAt, format: .relative(presentation: .named))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    ForEach((latestSession.segments ?? []).prefix(4)) { segment in
                        Text(segment.text)
                            .font(.body)
                            .foregroundStyle(.secondary)
                            .lineLimit(3)
                    }
                }
            } else {
                EmptyState(title: "No recent conversation.", subtitle: "The Home clear action only hides this surface.")
            }
        }
    }

    private var microphoneActionState: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Not listening.")
                .font(.title3.weight(.medium))
                .foregroundStyle(.secondary)
            Button {
                if appState.microphoneStatus == "Denied" || appState.microphoneStatus == "Restricted" {
                    openMicrophoneSettings()
                } else {
                    Task { await appState.startNativeVoiceIfPossible() }
                }
            } label: {
                Label(
                    appState.microphoneStatus == "Denied" || appState.microphoneStatus == "Restricted"
                        ? "Open Microphone Settings"
                        : "Start Listening",
                    systemImage: "mic"
                )
            }
            .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, minHeight: 120, alignment: .leading)
    }

    private func openMicrophoneSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") else {
            return
        }
        NSWorkspace.shared.open(url)
    }
}

struct StatusPill: View {
    var title: String
    var running: Bool

    var body: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(running ? Color.green : Color.secondary)
                .frame(width: 7, height: 7)
            Text(title)
                .font(.caption.weight(.medium))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.quaternary, in: Capsule())
    }
}
