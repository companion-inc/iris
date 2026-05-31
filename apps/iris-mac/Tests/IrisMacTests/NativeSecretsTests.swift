import XCTest
@testable import IrisMac

final class NativeSecretsTests: XCTestCase {
    func testPlainSettingsRoundTripAndEnvironmentMapping() throws {
        let secrets = NativeSecrets(service: "IrisMacTests.NativeSecrets.\(UUID().uuidString)")
        defer {
            for kind in NativeSecretKind.allCases {
                try? secrets.delete(kind)
            }
        }

        try secrets.write(" test-deepgram-key ", for: .deepgramAPIKey)
        try secrets.write("test-gemini-key", for: .geminiAPIKey)

        XCTAssertEqual(secrets.read(.deepgramAPIKey), "test-deepgram-key")
        XCTAssertTrue(secrets.configured(.geminiAPIKey))
        XCTAssertEqual(secrets.environment()["DEEPGRAM_API_KEY"], "test-deepgram-key")
        XCTAssertEqual(secrets.environment()["GEMINI_API_KEY"], "test-gemini-key")

        try secrets.delete(.deepgramAPIKey)
        XCTAssertNil(secrets.read(.deepgramAPIKey))
    }
}
