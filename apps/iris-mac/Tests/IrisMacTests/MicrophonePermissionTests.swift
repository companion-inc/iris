import AVFoundation
@testable import IrisMac
import XCTest

final class MicrophonePermissionTests: XCTestCase {
    func testStatusDescriptionsAreStable() {
        XCTAssertEqual(MicrophonePermission.description(for: .authorized), "Allowed")
        XCTAssertEqual(MicrophonePermission.description(for: .denied), "Denied")
        XCTAssertEqual(MicrophonePermission.description(for: .restricted), "Restricted")
        XCTAssertEqual(MicrophonePermission.description(for: .notDetermined), "Not requested")
    }
}
