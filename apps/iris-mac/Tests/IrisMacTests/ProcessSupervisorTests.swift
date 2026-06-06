import XCTest
@testable import IrisMac

final class ProcessSupervisorTests: XCTestCase {
    func testMatchesIrisSidecarProcessGroups() {
        let snapshot = """
          101 101 uv run iris-voice --host 0.0.0.0 --port 4748
          102 101 /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python /tmp/iris-voice --host 0.0.0.0 --port 4748
          201 201 uv run iris-speaker-id --host 0.0.0.0 --port 4749
          202 201 /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python /tmp/iris-speaker-id --host 0.0.0.0 --port 4749
          301 301 rg iris-voice
        """

        XCTAssertEqual(ProcessSupervisor.matchingSidecarProcessGroups(from: snapshot), Set<Int32>([101, 201]))
        XCTAssertEqual(ProcessSupervisor.matchingSidecarProcessGroups(from: snapshot, kinds: [.voice]), Set<Int32>([101]))
        XCTAssertEqual(ProcessSupervisor.matchingSidecarProcessGroups(from: snapshot, kinds: [.speakerID]), Set<Int32>([201]))
    }

    func testSidecarCommandRequiresIrisPort() {
        XCTAssertNil(ProcessSupervisor.sidecarKind(forCommand: "uv run iris-voice --port 9999"))
        XCTAssertNil(ProcessSupervisor.sidecarKind(forCommand: "rg iris-voice --port 4748"))
        XCTAssertEqual(
            ProcessSupervisor.sidecarKind(forCommand: "uv run iris-speaker-id --host 0.0.0.0 --port 4749"),
            .speakerID
        )
    }
}
