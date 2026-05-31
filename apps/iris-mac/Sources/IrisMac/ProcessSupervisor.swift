import Foundation
import Network

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
            if speakerIDProcess == nil && !isPortListening(4749) {
                speakerIDProcess = try startSpeakerIDSidecar()
                speakerIDProcessID = speakerIDProcess?.processIdentifier
                speakerIDLaunchCommand = "uv run iris-speaker-id"
            }
            if voiceProcess == nil && !isPortListening(4748) {
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
        }
    }

    func stopAll() {
        for process in [voiceProcess, speakerIDProcess] {
            process?.terminate()
        }
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
        let supportRoot = fileManager.homeDirectoryForCurrentUser
            .appending(path: "Library/Application Support/Iris/Runtime", directoryHint: .isDirectory)
        let marker = supportRoot.appending(path: ".iris-runtime-version")
        let bundledVersion = (try? String(contentsOf: bundledURL.appending(path: ".iris-runtime-version"), encoding: .utf8)) ?? "dev"
        let installedVersion = try? String(contentsOf: marker, encoding: .utf8)
        if installedVersion == bundledVersion,
           fileManager.fileExists(atPath: supportRoot.appending(path: "package.json").path) {
            return supportRoot
        }
        do {
            try? fileManager.removeItem(at: supportRoot)
            try fileManager.createDirectory(at: supportRoot.deletingLastPathComponent(), withIntermediateDirectories: true)
            try fileManager.copyItem(at: bundledURL, to: supportRoot)
            return supportRoot
        } catch {
            return bundledURL
        }
    }

    private func isPortListening(_ port: UInt16) -> Bool {
        guard let endpointPort = NWEndpoint.Port(rawValue: port),
              let host = IPv4Address("127.0.0.1") else {
            return false
        }
        let connection = NWConnection(host: .ipv4(host), port: endpointPort, using: .tcp)
        let queue = DispatchQueue(label: "iris.native.port-check.\(port)")
        let semaphore = DispatchSemaphore(value: 0)
        let result = PortCheckResult()
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                result.setListening()
                semaphore.signal()
            case .failed, .cancelled:
                semaphore.signal()
            default:
                break
            }
        }
        connection.start(queue: queue)
        _ = semaphore.wait(timeout: .now() + 0.35)
        connection.cancel()
        return result.listening
    }
}

private final class PortCheckResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    var listening: Bool {
        lock.withLock { value }
    }

    func setListening() {
        lock.withLock {
            value = true
        }
    }
}
