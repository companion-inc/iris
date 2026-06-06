import Foundation
import Darwin

@MainActor
@Observable
final class ProcessSupervisor {
    private(set) var voiceProcessID: Int32?
    private(set) var speakerIDProcessID: Int32?
    private(set) var voiceLaunchCommand = "Not started"
    private(set) var speakerIDLaunchCommand = "Not started"
    var lastError: String?

    private var voiceProcess: Process?
    private var speakerIDProcess: Process?
    private var voiceLogHandle: FileHandle?
    private var speakerIDLogHandle: FileHandle?

    private let repoRoot: URL
    private let workspace: URL
    private let uvExecutable: URL?

    init(
        repoRoot: URL = ProcessSupervisor.resolveRepoRoot(),
        workspace: URL = URL(fileURLWithPath: NSHomeDirectory()).appending(path: "Iris/Workspace")
    ) {
        self.repoRoot = repoRoot
        self.workspace = workspace
        let bundledUV = repoRoot.appending(path: "bin/uv")
        self.uvExecutable = FileManager.default.isExecutableFile(atPath: bundledUV.path) ? bundledUV : nil
    }

    func startAll(voiceEnvironment: [String: String] = [:]) {
        do {
            try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
            if speakerIDProcess == nil {
                Self.terminateKnownSidecars(kinds: [.speakerID])
                Self.appendSupervisorLog("starting iris-speaker-id repoRoot=\(repoRoot.path)")
                speakerIDProcess = try startSpeakerIDSidecar()
                speakerIDProcessID = speakerIDProcess?.processIdentifier
                speakerIDLaunchCommand = "uv run iris-speaker-id"
            }
            if voiceProcess == nil {
                Self.terminateKnownSidecars(kinds: [.voice])
                Self.appendSupervisorLog("starting iris-voice repoRoot=\(repoRoot.path)")
                var environment = [
                    "IRIS_AUTH_MODE": "local",
                    "IRIS_API_URL": "http://127.0.0.1:4747",
                    "IRIS_TOKEN_SECRET": "iris-development-token-secret",
                    "IRIS_AGENT_BRIDGE_URL": "http://127.0.0.1:4750/agent",
                    "IRIS_SPEAKER_ID_URL": "http://127.0.0.1:4749",
                    "IRIS_VOICE_PORT": "4748"
                ]
                voiceEnvironment.forEach { environment[$0.key] = $0.value }
                voiceProcess = try startVoiceSidecar(env: environment)
                voiceProcessID = voiceProcess?.processIdentifier
                voiceLaunchCommand = "uv run iris-voice"
            }
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            Self.appendSupervisorLog("start failed: \(error.localizedDescription)")
        }
    }

    func stopAll() {
        Self.appendSupervisorLog("stopping iris sidecars")
        let processIDs = [voiceProcess, speakerIDProcess]
            .compactMap { $0?.processIdentifier }
        for processID in processIDs {
            Self.terminateProcessGroup(processID)
        }
        Self.terminateKnownSidecars()
        voiceLogHandle?.closeFile()
        speakerIDLogHandle?.closeFile()
        voiceProcess = nil
        voiceProcessID = nil
        speakerIDProcess = nil
        speakerIDProcessID = nil
        voiceLogHandle = nil
        speakerIDLogHandle = nil
        voiceLaunchCommand = "Not started"
        speakerIDLaunchCommand = "Not started"
    }

    nonisolated static func appendSupervisorLog(_ message: String) {
        let directory = FileManager.default
            .homeDirectoryForCurrentUser
            .appending(path: "Library/Logs/Iris", directoryHint: .isDirectory)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let url = directory.appending(path: "iris-supervisor.log")
            let timestamp = ISO8601DateFormatter().string(from: Date())
            let line = "\(timestamp) \(message)\n"
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil)
            }
            let handle = try FileHandle(forWritingTo: url)
            try handle.seekToEnd()
            try handle.write(contentsOf: Data(line.utf8))
            try handle.close()
        } catch {
            // Logging must never break sidecar lifecycle.
        }
    }

    private func startVoiceSidecar(env: [String: String]) throws -> Process {
        let process = Process()
        process.currentDirectoryURL = repoRoot.appending(path: "apps/iris-voice")
        if let uvExecutable {
            process.executableURL = uvExecutable
            process.arguments = ["run", "iris-voice", "--host", "0.0.0.0", "--port", "4748"]
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["uv", "run", "iris-voice", "--host", "0.0.0.0", "--port", "4748"]
        }
        process.environment = mergedEnvironment(env)
        voiceLogHandle = try logHandle(named: "iris-voice.log")
        process.standardOutput = voiceLogHandle
        process.standardError = voiceLogHandle
        try process.run()
        return process
    }

    private func startSpeakerIDSidecar() throws -> Process {
        let process = Process()
        process.currentDirectoryURL = repoRoot.appending(path: "apps/iris-speaker-id")
        if let uvExecutable {
            process.executableURL = uvExecutable
            process.arguments = ["run", "iris-speaker-id", "--host", "0.0.0.0", "--port", "4749"]
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["uv", "run", "iris-speaker-id", "--host", "0.0.0.0", "--port", "4749"]
        }
        process.environment = mergedEnvironment([
            "IRIS_SPEAKER_ID_PORT": "4749"
        ])
        speakerIDLogHandle = try logHandle(named: "iris-speaker-id.log")
        process.standardOutput = speakerIDLogHandle
        process.standardError = speakerIDLogHandle
        try process.run()
        return process
    }

    private func logHandle(named name: String) throws -> FileHandle {
        let directory = FileManager.default
            .homeDirectoryForCurrentUser
            .appending(path: "Library/Logs/Iris", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appending(path: name)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.truncate(atOffset: 0)
        try handle.seekToEnd()
        return handle
    }

    private func mergedEnvironment(_ values: [String: String]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let pathPrefix = [
            "\(NSHomeDirectory())/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin"
        ].joined(separator: ":")
        env["PATH"] = "\(pathPrefix):\(env["PATH"] ?? "")"
        values.forEach { env[$0.key] = $0.value }
        return env
    }

    nonisolated static func resolveRepoRoot() -> URL {
        if let explicit = ProcessInfo.processInfo.environment["IRIS_REPO_ROOT"], !explicit.isEmpty {
            return URL(fileURLWithPath: explicit)
        }
        if let resourceRepo = bundledRepoRoot(),
           FileManager.default.fileExists(atPath: resourceRepo.appending(path: "package.json").path) {
            return resourceRepo
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    nonisolated private static func bundledRepoRoot() -> URL? {
        guard let marker = Bundle.main.resourceURL?.appending(path: "repo-root.txt"),
              let contents = try? String(contentsOf: marker, encoding: .utf8) else {
            return nil
        }
        let path = contents.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else {
            return nil
        }
        let url: URL
        if path.hasPrefix("/") {
            url = URL(fileURLWithPath: path, isDirectory: true)
        } else {
            url = Bundle.main.resourceURL?.appending(path: path, directoryHint: .isDirectory) ?? URL(fileURLWithPath: path, isDirectory: true)
        }
        return preparedRuntimeRoot(from: url.resolvingSymlinksInPath())
    }

    nonisolated private static func preparedRuntimeRoot(from bundledURL: URL) -> URL {
        let fileManager = FileManager.default
        let bundledVersionURL = bundledURL.appending(path: ".iris-runtime-version")
        guard let bundledVersion = try? String(contentsOf: bundledVersionURL, encoding: .utf8) else {
            return bundledURL
        }
        let supportRoot = fileManager.homeDirectoryForCurrentUser
            .appending(path: "Library/Application Support/Iris/Runtime", directoryHint: .isDirectory)
        let marker = supportRoot.appending(path: ".iris-runtime-version")
        let installedVersion = try? String(contentsOf: marker, encoding: .utf8)
        if installedVersion == bundledVersion,
           isUsableRuntimeRoot(supportRoot) {
            return supportRoot
        }
        do {
            try? fileManager.removeItem(at: supportRoot)
            try fileManager.createDirectory(at: supportRoot.deletingLastPathComponent(), withIntermediateDirectories: true)
            try fileManager.copyItem(at: bundledURL, to: supportRoot)
            return isUsableRuntimeRoot(supportRoot) ? supportRoot : bundledURL
        } catch {
            return bundledURL
        }
    }

    nonisolated private static func isUsableRuntimeRoot(_ url: URL) -> Bool {
        let fileManager = FileManager.default
        return fileManager.fileExists(atPath: url.appending(path: "package.json").path)
            && fileManager.fileExists(atPath: url.appending(path: "apps/iris-voice/pyproject.toml").path)
            && fileManager.fileExists(atPath: url.appending(path: "apps/iris-speaker-id/pyproject.toml").path)
    }

    nonisolated static func terminateKnownSidecars(kinds: Set<SidecarKind> = Set(SidecarKind.allCases)) {
        let snapshot = processSnapshot()
        let groups = matchingSidecarProcessGroups(from: snapshot, kinds: kinds)
        for group in groups {
            terminateProcessGroup(group)
        }
    }

    nonisolated static func matchingSidecarProcessGroups(
        from processSnapshot: String,
        kinds: Set<SidecarKind> = Set(SidecarKind.allCases)
    ) -> Set<Int32> {
        var groups = Set<Int32>()
        for line in processSnapshot.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty else { continue }
            let fields = trimmed.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
            guard fields.count == 3,
                  let pid = Int32(fields[0]),
                  let processGroup = Int32(fields[1]) else {
                continue
            }
            let command = String(fields[2])
            guard sidecarKind(forCommand: command).map(kinds.contains) == true else {
                continue
            }
            groups.insert(processGroup == 0 ? pid : processGroup)
        }
        return groups
    }

    nonisolated static func sidecarKind(forCommand command: String) -> SidecarKind? {
        guard command.contains("uv run ") || command.contains("Python ") || command.contains(".venv/bin/") else {
            return nil
        }
        if command.contains("iris-voice") && command.contains("--port 4748") {
            return .voice
        }
        if command.contains("iris-speaker-id") && command.contains("--port 4749") {
            return .speakerID
        }
        return nil
    }

    nonisolated private static func processSnapshot() -> String {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-axo", "pid=,pgid=,command="]
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }

    nonisolated static func terminateProcessGroup(_ processID: Int32) {
        guard processID > 1 else { return }
        let groupID = processGroupID(for: processID) ?? processID
        kill(-groupID, SIGTERM)
        usleep(250_000)
        if processGroupIsAlive(groupID) {
            kill(-groupID, SIGKILL)
        }
    }

    nonisolated private static func processGroupID(for processID: Int32) -> Int32? {
        let snapshot = processSnapshot()
        for line in snapshot.split(separator: "\n") {
            let fields = line.trimmingCharacters(in: .whitespaces).split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
            guard fields.count == 3,
                  let pid = Int32(fields[0]),
                  let processGroup = Int32(fields[1]),
                  pid == processID else {
                continue
            }
            return processGroup
        }
        return nil
    }

    nonisolated private static func processGroupIsAlive(_ processGroupID: Int32) -> Bool {
        let snapshot = processSnapshot()
        for line in snapshot.split(separator: "\n") {
            let fields = line.trimmingCharacters(in: .whitespaces).split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
            guard fields.count >= 2,
                  let group = Int32(fields[1]),
                  group == processGroupID else {
                continue
            }
            return true
        }
        return false
    }
}

enum SidecarKind: CaseIterable {
    case voice
    case speakerID
}
