import Foundation

#if canImport(AppIntents)
import AppIntents

struct OpenIrisIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Iris"
    static let description = IntentDescription("Open the native Iris Mac app.")
    static let openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        .result()
    }
}

struct StartIrisServicesIntent: AppIntent {
    static let title: LocalizedStringResource = "Start Iris Services"
    static let description = IntentDescription("Start Iris local services from the Mac app.")
    static let openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        .result(dialog: "Open Iris to start local services.")
    }
}

struct IrisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenIrisIntent(),
            phrases: [
                "Open \(.applicationName)",
                "Show \(.applicationName)"
            ],
            shortTitle: "Open Iris",
            systemImageName: "waveform"
        )
        AppShortcut(
            intent: StartIrisServicesIntent(),
            phrases: [
                "Start \(.applicationName) services"
            ],
            shortTitle: "Start Services",
            systemImageName: "power"
        )
    }
}
#endif
