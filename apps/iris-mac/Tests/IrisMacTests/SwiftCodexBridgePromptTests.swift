import XCTest
@testable import IrisMac

final class SwiftCodexBridgePromptTests: XCTestCase {
    func testCodexPromptDoesNotIncludeGeminiToolInstructions() {
        let prompt = buildCodexAgentPrompt(
            prompt: "Look at the user's Apple Music library and start something they would like.",
            context: "Raw voice: play something I like"
        )

        XCTAssertTrue(prompt.hasPrefix("Look at the user's Apple Music library"))
        XCTAssertTrue(prompt.contains("Voice context:"))
        XCTAssertTrue(prompt.contains("Raw voice: play something I like"))
        XCTAssertFalse(prompt.contains("Interpreted desktop task for Codex"))
        XCTAssertFalse(prompt.contains("Do not merely repeat the user's raw words"))
    }
}
