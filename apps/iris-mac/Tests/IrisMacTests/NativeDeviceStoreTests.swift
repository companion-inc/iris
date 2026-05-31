import XCTest
@testable import IrisMac

final class NativeDeviceStoreTests: XCTestCase {
    func testReadsLocalDevicesFromSQLite() async throws {
        let store = NativeDeviceStore(repoRoot: repoRoot())

        let devices = await store.devices(limit: 10)
        XCTAssertFalse(devices.isEmpty)
        XCTAssertTrue(devices.allSatisfy { !$0.id.isEmpty })
        XCTAssertTrue(devices.contains { $0.kind == "agent" })
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
