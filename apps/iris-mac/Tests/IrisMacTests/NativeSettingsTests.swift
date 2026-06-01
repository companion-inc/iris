import Foundation
@testable import IrisMac
import XCTest

@MainActor
final class NativeSettingsTests: XCTestCase {
    func testResolvesPersistedEndpointsAndWorkspace() throws {
        let defaults = UserDefaults(suiteName: "IrisMacTests.NativeSettings.\(UUID().uuidString)")!
        let settings = NativeSettings(defaults: defaults)
        settings.workspacePath = "~/Iris/TestWorkspace"
        settings.apiURL = "http://127.0.0.1:4747"
        settings.voiceURL = "http://127.0.0.1:4748"
        settings.bridgeURL = "http://127.0.0.1:4750"

        let endpoints = try settings.resolvedEndpoints()

        XCTAssertEqual(endpoints.apiURL.absoluteString, "http://127.0.0.1:4747")
        XCTAssertEqual(endpoints.voiceURL.absoluteString, "http://127.0.0.1:4748")
        XCTAssertEqual(endpoints.bridgeURL.absoluteString, "http://127.0.0.1:4750")
        XCTAssertEqual(endpoints.workspaceURL.path, "\(NSHomeDirectory())/Iris/TestWorkspace")
    }

    func testRejectsInvalidServiceURL() {
        let defaults = UserDefaults(suiteName: "IrisMacTests.NativeSettings.\(UUID().uuidString)")!
        let settings = NativeSettings(defaults: defaults)
        settings.apiURL = "not a url"

        XCTAssertThrowsError(try settings.resolvedEndpoints())
    }

    func testPersistsVoiceProviderChoices() {
        let suiteName = "IrisMacTests.NativeSettings.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let settings = NativeSettings(defaults: defaults)
        settings.sttProvider = "openai"
        settings.llmProvider = "openai"
        settings.ttsProvider = "deepgram"
        settings.sttKeyterms = "Iris, Advait, Reducto"

        let reloaded = NativeSettings(defaults: defaults)

        XCTAssertEqual(reloaded.sttProvider, "openai")
        XCTAssertEqual(reloaded.llmProvider, "openai")
        XCTAssertEqual(reloaded.ttsProvider, "deepgram")
        XCTAssertEqual(reloaded.sttKeyterms, "Iris, Advait, Reducto")
    }
}
