import SwiftUI

@main
struct IrisMacApp: App {
    @State private var appState = IrisAppState()

    init() {
        ProcessInfo.processInfo.disableAutomaticTermination("Iris owns local voice and Codex services")
    }

    var body: some Scene {
        WindowGroup("Iris") {
            RootView()
                .environment(appState)
                .frame(minWidth: 920, minHeight: 640)
                .preferredColorScheme(.light)
                .task {
                    await appState.start()
                }
        }
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About Iris") {
                    NSApplication.shared.orderFrontStandardAboutPanel()
                }
            }
        }

        MenuBarExtra("Iris", systemImage: "waveform") {
            Button("Open Iris") {
                openMainWindow()
            }
            Divider()
            Button(appState.voiceHealth.isRunning ? "Restart Services" : "Start Services") {
                Task {
                    await appState.start()
                }
            }
            Button("Stop Voice") {
                appState.supervisor.stopAll()
                Task {
                    await appState.refresh()
                }
            }
            Divider()
            Label(appState.apiHealth.isRunning ? "API Running" : "API Unavailable", systemImage: appState.apiHealth.isRunning ? "checkmark.circle" : "xmark.circle")
            Label(appState.voiceHealth.isRunning ? "Voice Running" : "Voice Unavailable", systemImage: appState.voiceHealth.isRunning ? "checkmark.circle" : "xmark.circle")
            Label(appState.bridgeHealth.isRunning ? "Codex Ready" : "Codex Unavailable", systemImage: appState.bridgeHealth.isRunning ? "checkmark.circle" : "xmark.circle")
            Divider()
            Button("Quit Iris") {
                appState.bridgeServer.stop()
                appState.supervisor.stopAll()
                NSApplication.shared.terminate(nil)
            }
        }
    }

    private func openMainWindow() {
        NSApplication.shared.activate(ignoringOtherApps: true)
        if let window = NSApplication.shared.windows.first {
            window.makeKeyAndOrderFront(nil)
        }
    }
}
