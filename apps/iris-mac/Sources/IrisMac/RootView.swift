import SwiftUI

struct RootView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        @Bindable var appState = appState
        NavigationSplitView {
            Sidebar(
                selectedTab: $appState.selectedTab,
                updateStatus: appState.updateStatus,
                isCheckingForUpdates: appState.isCheckingForUpdates,
                checkForUpdates: {
                    await appState.checkForUpdates()
                },
                openUpdateDownload: {
                    appState.openUpdateDownload()
                }
            )
                .navigationSplitViewColumnWidth(min: 180, ideal: 210, max: 240)
        } detail: {
            Group {
                switch appState.selectedTab {
                case .home:
                    HomeView()
                case .voice:
                    VoiceView()
                case .devices:
                    DevicesView()
                case .transcripts:
                    TranscriptsView()
                case .settings:
                    SettingsView()
                }
            }
            .toolbar {
                ToolbarItemGroup {
                    Button {
                        Task { await appState.refresh() }
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                    .disabled(appState.isRefreshing)
                }
            }
            .background {
                NativeVoiceCaptureHost(view: appState.voiceRuntime.captureView)
                    .frame(width: 1, height: 1)
                    .opacity(0.01)
                    .accessibilityHidden(true)
            }
        }
    }
}

private struct Sidebar: View {
    @Binding var selectedTab: IrisTab
    var updateStatus: AppUpdateStatus
    var isCheckingForUpdates: Bool
    var checkForUpdates: () async -> Void
    var openUpdateDownload: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            sidebarHeader
            List(IrisTab.allCases, selection: $selectedTab) { tab in
                Label(tab.rawValue, systemImage: tab.symbol)
                    .tag(tab)
            }
            .scrollContentBackground(.hidden)
            updateFooter
        }
    }

    private var sidebarHeader: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Iris")
                .font(.title3.weight(.semibold))
            Text("Local Mac")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    private var updateFooter: some View {
        VStack(alignment: .leading, spacing: 8) {
            if updateStatus.updateAvailable {
                Button {
                    openUpdateDownload()
                } label: {
                    Label("Update available", systemImage: "arrow.down.circle")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            } else {
                HStack(spacing: 6) {
                    Image(systemName: isCheckingForUpdates ? "arrow.triangle.2.circlepath" : "checkmark.circle")
                    Text(isCheckingForUpdates ? "Checking updates" : "Iris up to date")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Button("Check for updates") {
                Task {
                    await checkForUpdates()
                }
            }
            .font(.caption)
            .disabled(isCheckingForUpdates)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(.regularMaterial)
    }
}
