import SwiftUI

struct RootView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        @Bindable var appState = appState
        NavigationSplitView {
            Sidebar(selectedTab: $appState.selectedTab)
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

    var body: some View {
        List(IrisTab.allCases, selection: $selectedTab) { tab in
            Label(tab.rawValue, systemImage: tab.symbol)
                .tag(tab)
        }
        .safeAreaInset(edge: .top) {
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
    }
}
