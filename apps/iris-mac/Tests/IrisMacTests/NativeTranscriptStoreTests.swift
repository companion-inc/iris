import XCTest
@testable import IrisMac

final class NativeTranscriptStoreTests: XCTestCase {
    func testReadsLocalTranscriptHistoryFromSQLite() async throws {
        let store = NativeTranscriptStore(repoRoot: repoRoot())

        let isAvailable = await store.isAvailable()
        XCTAssertTrue(isAvailable)

        let transcripts = await store.transcripts(limit: 5)
        XCTAssertFalse(transcripts.isEmpty)
        XCTAssertTrue(transcripts.allSatisfy { !$0.text.isEmpty })

        let sessions = await store.voiceSessions(limit: 5, segmentLimit: 3)
        XCTAssertFalse(sessions.isEmpty)
        XCTAssertTrue(sessions.allSatisfy { !$0.id.isEmpty })
    }

    private func repoRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
