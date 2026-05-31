import Foundation

private let irisCodexBaseInstructions = """
You are Iris Desktop Agent, the local Codex runtime controlled by the user's Iris voice assistant. Use the available Codex tools only when they help complete the user's request on this computer. Keep final results concise and structured for Iris to interpret for voice.
"""

private let irisCodexDeveloperInstructions = """
Handle the current voice request directly. For simple questions, answer without using tools. For computer actions, use the native Codex tools available in this session. When you use tools or change/read desktop state, your final response must be a compact JSON object with type='iris.desktop.result', outcome, summary, screenState, needsUserAction, followUp, suggestedSpoken, and details. Treat suggestedSpoken as optional source material for Iris, not as the final voice turn; keep it to one or two short spoken sentences unless the user explicitly asks for a detailed readout. Set needsUserAction=true only when the user must stop and take a non-voice action before Iris can continue. Do not return only 'done'. Report what changed, what is visible now, and whether the user should be asked a follow-up. Do not explain bridge internals unless the user asks.
"""

private struct AgentRequest: Decodable {
    var action: String?
    var prompt: String?
    var query: String?
    var text: String?
    var context: String?
    var threadId: String?
    var codexThreadId: String?
    var waitMs: Int?
    var thinking: AgentRequestThinking?
    var reasoning: AgentRequestThinking?
    var thinkingEffort: String?
    var reasoningEffort: String?
    var effort: String?
    var iris: AgentRequestIris?
}

private struct AgentRequestThinking: Decodable {
    var effort: String?
}

private struct AgentRequestIris: Decodable, Sendable {
    var runId: String?
    var sessionId: String?
}

struct SwiftCodexBridgeCompletion: @unchecked Sendable {
    var runId: String
    var sessionId: String?
    var result: [String: Any]
}

private struct CodexCompletionContext: Sendable {
    var runId: String
    var sessionId: String?
}

struct SwiftCodexBridgeSnapshot: Sendable {
    var ok: Bool
    var service: String
    var cwd: String
    var sandboxMode: String
    var pid: Int32?
    var threadId: String?
    var active: Bool
}

final class SwiftCodexBridgeServer: @unchecked Sendable {
    private let port: UInt16 = 4750
    private let workspace: URL
    private let client: CodexAppServerClient
    private let completionSink: @Sendable (SwiftCodexBridgeCompletion) async -> Void
    private var server: NativeHTTPServer?

    init(
        workspace: URL,
        completionSink: @escaping @Sendable (SwiftCodexBridgeCompletion) async -> Void = { _ in }
    ) {
        self.workspace = workspace
        self.completionSink = completionSink
        self.client = CodexAppServerClient(workspace: workspace, completionSink: completionSink)
    }

    func start() throws {
        try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
        if server != nil {
            return
        }
        let server = NativeHTTPServer(port: port, label: "iris.native.codex-bridge") { [weak self] data in
            guard let self else {
                return jsonResponse(["ok": false, "error": "server stopped"], status: 503).data
            }
            return await self.handleRequest(data).data
        }
        try server.start()
        self.server = server
    }

    func stop() {
        server?.stop()
        server = nil
        Task {
            await client.stop()
        }
    }

    func snapshot() async -> SwiftCodexBridgeSnapshot {
        await client.snapshot()
    }

    private func handleRequest(_ data: Data) async -> HTTPResponse {
        let request = HTTPRequest(data: data)
        switch (request.method, request.path) {
        case ("GET", "/health"):
            return await jsonResponse(healthPayload())
        case ("POST", "/agent"):
            return await handleAgent(request.body)
        default:
            return jsonResponse(["ok": false, "error": "not found"], status: 404)
        }
    }

    private func handleAgent(_ body: Data) async -> HTTPResponse {
        do {
            let payload = try JSONDecoder().decode(AgentRequest.self, from: body.isEmpty ? Data("{}".utf8) : body)
            let action = normalizedAction(payload)
            if action == "status" {
                return await jsonResponse(healthPayload())
            }
            guard let rawPrompt = normalizedPrompt(payload), !rawPrompt.isEmpty else {
                return jsonResponse(["ok": false, "error": "\(action) requires prompt"], status: 400)
            }
            let prompt = buildCodexAgentPrompt(prompt: rawPrompt, context: payload.context)
            let waitMs = max(0, payload.waitMs ?? 5_000)
            let effort = normalizedEffort(payload)
            let result: CodexTurnResult
            if action == "steer" {
                result = try await client.steer(prompt: prompt, waitMs: waitMs, threadId: payload.threadId, codexThreadId: payload.codexThreadId, effort: effort)
            } else {
                result = try await client.start(
                    prompt: prompt,
                    waitMs: waitMs,
                    effort: effort,
                    threadId: payload.threadId,
                    codexThreadId: payload.codexThreadId,
                    completionContext: completionContext(from: payload)
                )
            }
            return jsonResponse(result.payload)
        } catch {
            return jsonResponse(["ok": false, "status": "failed", "error": error.localizedDescription], status: 500)
        }
    }

    private func normalizedAction(_ payload: AgentRequest) -> String {
        let explicit = payload.action?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if explicit == "status" || explicit == "steer" || explicit == "start" {
            return explicit!
        }
        return "start"
    }

    private func normalizedPrompt(_ payload: AgentRequest) -> String? {
        for candidate in [payload.prompt, payload.query, payload.text] {
            let value = candidate?.trimmingCharacters(in: .whitespacesAndNewlines)
            if let value, !value.isEmpty {
                return value
            }
        }
        return nil
    }

    private func normalizedEffort(_ payload: AgentRequest) -> String {
        for candidate in [
            payload.thinking?.effort,
            payload.reasoning?.effort,
            payload.thinkingEffort,
            payload.reasoningEffort,
            payload.effort
        ] {
            let value = candidate?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if let value, ["none", "minimal", "low", "medium", "high", "xhigh"].contains(value) {
                return value
            }
        }
        return "low"
    }

    private func buildCodexAgentPrompt(prompt: String, context: String?) -> String {
        var lines = [
            "Interpreted desktop task for Codex:",
            prompt,
            "",
            "Do not merely repeat the user's raw words. Infer the intended desktop action and execute or answer it directly. Ask for clarification only when the intended action is genuinely ambiguous."
        ]
        if let context = context?.trimmingCharacters(in: .whitespacesAndNewlines), !context.isEmpty {
            lines.append("")
            lines.append("Raw voice context:")
            lines.append(context)
        }
        return lines.joined(separator: "\n")
    }

    private func healthPayload() async -> [String: Any] {
        let snapshot = await client.snapshot()
        return [
            "ok": snapshot.ok,
            "service": snapshot.service,
            "agentId": "local_desktop_agent",
            "codex": [
                "active": snapshot.active,
                "threadId": snapshot.threadId.map { $0 as Any } ?? NSNull(),
                "codexThreadId": snapshot.threadId.map { $0 as Any } ?? NSNull(),
                "runtime": [
                    "codexBin": "codex",
                    "cwd": snapshot.cwd,
                    "sandboxMode": snapshot.sandboxMode,
                    "pid": snapshot.pid.map { Int($0) as Any } ?? NSNull()
                ]
            ]
        ]
    }

    private func completionContext(from payload: AgentRequest) -> CodexCompletionContext? {
        guard let runId = payload.iris?.runId?.trimmingCharacters(in: .whitespacesAndNewlines),
              !runId.isEmpty else {
            return nil
        }
        return CodexCompletionContext(runId: runId, sessionId: payload.iris?.sessionId)
    }

}

private struct HTTPRequest {
    var method = "GET"
    var path = "/"
    var body = Data()

    init(data: Data) {
        guard let separatorRange = data.range(of: Data("\r\n\r\n".utf8)) else {
            return
        }
        let headerData = data[..<separatorRange.lowerBound]
        body = Data(data[separatorRange.upperBound...])
        guard let headerText = String(data: headerData, encoding: .utf8),
              let requestLine = headerText.split(separator: "\r\n").first else {
            return
        }
        let parts = requestLine.split(separator: " ")
        if parts.count >= 2 {
            method = String(parts[0])
            path = String(parts[1])
        }
    }
}

private struct HTTPResponse {
    var header: Data
    var body: Data

    var data: Data {
        var data = header
        data.append(body)
        return data
    }
}

private func jsonResponse(_ object: [String: Any], status: Int = 200) -> HTTPResponse {
    let body = (try? JSONSerialization.data(withJSONObject: sanitizeJSON(object), options: [])) ?? Data("{}".utf8)
    let reason = status == 200 ? "OK" : "Error"
    let header = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    return HTTPResponse(header: Data(header.utf8), body: body)
}

private func sanitizeJSON(_ value: Any) -> Any {
    switch value {
    case let dictionary as [String: Any]:
        return dictionary.mapValues(sanitizeJSON)
    case let array as [Any]:
        return array.map(sanitizeJSON)
    default:
        return value
    }
}

private struct CodexTurnResult: Sendable {
    var payload: [String: Any] {
        [
            "ok": true,
            "status": status,
            "threadId": irisThreadId.map { $0 as Any } ?? NSNull(),
            "codexThreadId": codexThreadId,
            "turnId": turnId,
            "run": [
                "threadId": irisThreadId.map { $0 as Any } ?? NSNull(),
                "codexThreadId": codexThreadId,
                "turnId": turnId,
                "status": status,
                "assistantText": assistantText
            ]
        ]
    }

    var status: String
    var irisThreadId: String?
    var codexThreadId: String
    var turnId: String
    var assistantText: String
}

private actor CodexAppServerClient {
    private enum CodexClientError: LocalizedError {
        case processNotRunning
        case missingStdin
        case invalidResponse(String)
        case rpcError(String)
        case timeout(String)

        var errorDescription: String? {
            switch self {
            case .processNotRunning:
                "Codex app-server is not running"
            case .missingStdin:
                "Codex app-server stdin is closed"
            case .invalidResponse(let message):
                message
            case .rpcError(let message):
                message
            case .timeout(let method):
                "Codex RPC timed out: \(method)"
            }
        }
    }

    private let workspace: URL
    private let completionSink: @Sendable (SwiftCodexBridgeCompletion) async -> Void
    private var process: Process?
    private var stdin: FileHandle?
    private var stdoutBuffer = Data()
    private var nextID = 1
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]
    private var threadID: String?
    private var activeTurnID: String?
    private var lastTurnStatus: String?
    private var lastAssistantText = ""

    init(
        workspace: URL,
        completionSink: @escaping @Sendable (SwiftCodexBridgeCompletion) async -> Void
    ) {
        self.workspace = workspace
        self.completionSink = completionSink
    }

    func snapshot() -> SwiftCodexBridgeSnapshot {
        SwiftCodexBridgeSnapshot(
            ok: true,
            service: "iris-swift-codex-bridge",
            cwd: workspace.path,
            sandboxMode: "danger-full-access",
            pid: process?.processIdentifier,
            threadId: threadID,
            active: activeTurnID != nil
        )
    }

    func stop() {
        stdoutBuffer.removeAll()
        pending.values.forEach { continuation in
            continuation.resume(throwing: CodexClientError.processNotRunning)
        }
        pending.removeAll()
        stdin = nil
        process?.terminate()
        process = nil
        threadID = nil
        activeTurnID = nil
        lastTurnStatus = nil
    }

    func start(
        prompt: String,
        waitMs: Int,
        effort: String,
        threadId irisThreadID: String?,
        codexThreadId: String?,
        completionContext: CodexCompletionContext? = nil
    ) async throws -> CodexTurnResult {
        let threadID = try await ensureThread(existingThreadID: codexThreadId)
        let result = try await request(
            method: "turn/start",
            params: [
                "threadId": threadID,
                "input": [["type": "text", "text": prompt, "text_elements": []]],
                "cwd": workspace.path,
                "approvalPolicy": "never",
                "sandboxPolicy": ["type": "dangerFullAccess"],
                "effort": effort
            ],
            timeoutSeconds: max(10, Double(waitMs) / 1000 + 10)
        )
        let turn = dictionaryValue(result, for: "turn")
        guard let turnID = stringValue(turn, for: "id") else {
            throw CodexClientError.invalidResponse("Codex turn/start did not return a turn id")
        }
        var status = stringValue(turn, for: "status") ?? "inProgress"
        lastTurnStatus = status
        activeTurnID = status.isTerminalCodexStatus ? nil : turnID
        lastAssistantText = ""
        if !status.isTerminalCodexStatus && waitMs > 0 {
            status = await waitForTurnCompletion(turnID: turnID, waitMs: waitMs)
        }
        let output = CodexTurnResult(
            status: status.isTerminalCodexStatus ? status : "running",
            irisThreadId: irisThreadID,
            codexThreadId: threadID,
            turnId: turnID,
            assistantText: lastAssistantText
        )
        if waitMs == 0, output.status == "running", let completionContext {
            Task {
                await self.deliverCompletionWhenFinished(
                    turnID: turnID,
                    irisThreadID: irisThreadID,
                    codexThreadID: threadID,
                    context: completionContext
                )
            }
        }
        return output
    }

    private func waitForTurnCompletion(turnID: String, waitMs: Int) async -> String {
        let timeout = Date().addingTimeInterval(Double(waitMs) / 1000)
        while activeTurnID == turnID && Date() < timeout {
            try? await Task.sleep(for: .milliseconds(100))
        }
        return lastTurnStatus ?? (activeTurnID == turnID ? "running" : "completed")
    }

    func steer(prompt: String, waitMs: Int, threadId irisThreadID: String?, codexThreadId: String?, effort: String) async throws -> CodexTurnResult {
        guard let activeTurnID, let threadID else {
            return try await start(prompt: prompt, waitMs: waitMs, effort: effort, threadId: irisThreadID, codexThreadId: codexThreadId)
        }
        _ = try await request(
            method: "turn/steer",
            params: [
                "threadId": threadID,
                "expectedTurnId": activeTurnID,
                "input": [["type": "text", "text": prompt, "text_elements": []]]
            ],
            timeoutSeconds: 10
        )
        return CodexTurnResult(
            status: "running",
            irisThreadId: irisThreadID,
            codexThreadId: threadID,
            turnId: activeTurnID,
            assistantText: lastAssistantText
        )
    }

    private func deliverCompletionWhenFinished(
        turnID: String,
        irisThreadID: String?,
        codexThreadID: String,
        context: CodexCompletionContext
    ) async {
        let status = await waitForTurnCompletion(turnID: turnID, waitMs: 600_000)
        if status.isTerminalCodexStatus == false, activeTurnID == turnID {
            activeTurnID = nil
            lastTurnStatus = "failed"
        }
        let result = CodexTurnResult(
            status: status.isTerminalCodexStatus ? status : "failed",
            irisThreadId: irisThreadID,
            codexThreadId: codexThreadID,
            turnId: turnID,
            assistantText: lastAssistantText
        )
        var payload = result.payload
        if status.isTerminalCodexStatus == false {
            payload["error"] = "Codex turn did not finish before the Iris delivery timeout"
        }
        await completionSink(
            SwiftCodexBridgeCompletion(
                runId: context.runId,
                sessionId: context.sessionId,
                result: payload
            )
        )
    }

    private func ensureThread(existingThreadID: String?) async throws -> String {
        try await ensureStarted()
        if let threadID {
            return threadID
        }
        if let existingThreadID, !existingThreadID.isEmpty {
            self.threadID = existingThreadID
            return existingThreadID
        }
        let result = try await request(
            method: "thread/start",
            params: [
                "cwd": workspace.path,
                "sessionStartSource": "startup",
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "baseInstructions": irisCodexBaseInstructions,
                "developerInstructions": irisCodexDeveloperInstructions
            ],
            timeoutSeconds: 20
        )
        let thread = dictionaryValue(result, for: "thread")
        guard let threadID = stringValue(thread, for: "id") else {
            throw CodexClientError.invalidResponse("Codex thread/start did not return a thread id")
        }
        self.threadID = threadID
        return threadID
    }

    private func ensureStarted() async throws {
        if let process, process.isRunning {
            return
        }

        let process = Process()
        process.currentDirectoryURL = workspace
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["codex", "app-server", "-c", "notify=[]", "--listen", "stdio://"]
        process.environment = mergedEnvironment()

        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice

        try process.run()
        self.process = process
        self.stdin = input.fileHandleForWriting

        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task {
                await self?.receive(data)
            }
        }

        _ = try await request(
            method: "initialize",
            params: [
                "clientInfo": [
                    "name": "iris-swift-codex-bridge",
                    "title": "Iris Swift Codex Bridge",
                    "version": "0.1.0"
                ],
                "capabilities": ["experimentalApi": true]
            ],
            timeoutSeconds: 20
        )
        try notify(method: "initialized", params: [:])
    }

    private func request(method: String, params: [String: Any], timeoutSeconds: Double) async throws -> [String: Any] {
        guard process?.isRunning == true else {
            throw CodexClientError.processNotRunning
        }
        guard let stdin else {
            throw CodexClientError.missingStdin
        }

        let requestID = nextID
        nextID += 1
        let line = try jsonLine(["jsonrpc": "2.0", "id": requestID, "method": method, "params": params])

        let resultData: Data = try await withThrowingTaskGroup(of: Data.self) { group in
            group.addTask {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Data, Error>) in
                    Task {
                        await self.storeContinuation(continuation, id: requestID)
                    }
                    stdin.write(Data((line + "\n").utf8))
                }
            }
            group.addTask {
                try await Task.sleep(for: .seconds(timeoutSeconds))
                throw CodexClientError.timeout(method)
            }
            guard let data = try await group.next() else {
                throw CodexClientError.invalidResponse("Codex RPC returned no response: \(method)")
            }
            group.cancelAll()
            pending.removeValue(forKey: requestID)
            return data
        }

        let object = try JSONSerialization.jsonObject(with: resultData)
        return object as? [String: Any] ?? [:]
    }

    private func storeContinuation(_ continuation: CheckedContinuation<Data, Error>, id: Int) {
        pending[id] = continuation
    }

    private func notify(method: String, params: [String: Any]) throws {
        guard let stdin else {
            throw CodexClientError.missingStdin
        }
        let line = try jsonLine(["jsonrpc": "2.0", "method": method, "params": params])
        stdin.write(Data((line + "\n").utf8))
    }

    private func receive(_ data: Data) {
        stdoutBuffer.append(data)
        let newline = Data("\n".utf8)
        while let range = stdoutBuffer.range(of: newline) {
            let line = stdoutBuffer[..<range.lowerBound]
            stdoutBuffer.removeSubrange(..<range.upperBound)
            handleLine(Data(line))
        }
    }

    private func handleLine(_ data: Data) {
        guard !data.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        if let id = object["id"] as? Int, let continuation = pending.removeValue(forKey: id) {
            if let error = object["error"] {
                continuation.resume(throwing: CodexClientError.rpcError(String(describing: error)))
                return
            }
            let result = object["result"] ?? [:]
            let resultData = (try? JSONSerialization.data(withJSONObject: sanitizeJSON(result), options: [])) ?? Data("{}".utf8)
            continuation.resume(returning: resultData)
            return
        }
        applyEvent(object)
    }

    private func applyEvent(_ event: [String: Any]) {
        let method = event["method"] as? String
        let params = event["params"] as? [String: Any]
        let eventTurnID = params?["turnId"] as? String
        if method == "item/agentMessage/delta",
           eventTurnID == activeTurnID,
           let delta = params?["delta"] as? String,
           !delta.isEmpty {
            lastAssistantText += delta
        }
        if let item = params?["item"] as? [String: Any],
           eventTurnID == activeTurnID {
            appendAssistantText(fromThreadItem: item)
            appendAssistantText(fromResponseItem: item)
        }
        if let turn = params?["turn"] as? [String: Any],
           let turnID = turn["id"] as? String,
           turnID == activeTurnID {
            if let items = turn["items"] as? [[String: Any]], lastAssistantText.isEmpty {
                for item in items {
                    appendAssistantText(fromThreadItem: item)
                }
            }
            if let status = turn["status"] as? String {
                lastTurnStatus = status
                if status.isTerminalCodexStatus {
                    activeTurnID = nil
                }
            }
        }
        if let text = params?["text"] as? String, !text.isEmpty {
            lastAssistantText += text
        }
    }

    private func appendAssistantText(fromThreadItem item: [String: Any]) {
        guard item["type"] as? String == "agentMessage",
              let text = item["text"] as? String,
              !text.isEmpty else {
            return
        }
        if lastAssistantText.isEmpty {
            lastAssistantText = text
        }
    }

    private func appendAssistantText(fromResponseItem item: [String: Any]) {
        guard item["type"] as? String == "message",
              item["role"] as? String == "assistant",
              lastAssistantText.isEmpty,
              let content = item["content"] as? [[String: Any]] else {
            return
        }
        let text = content.compactMap { part -> String? in
            guard part["type"] as? String == "output_text" else { return nil }
            return part["text"] as? String
        }.joined()
        if !text.isEmpty {
            lastAssistantText = text
        }
    }

    private func jsonLine(_ object: [String: Any]) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: sanitizeJSON(object), options: [])
        return String(decoding: data, as: UTF8.self)
    }

    private func mergedEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let pathPrefix = [
            "\(NSHomeDirectory())/.vite-plus/bin",
            "\(NSHomeDirectory())/.vite-plus/js_runtime/node/22.22.3/bin",
            "\(NSHomeDirectory())/Library/pnpm",
            "\(NSHomeDirectory())/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin"
        ].joined(separator: ":")
        env["PATH"] = "\(pathPrefix):\(env["PATH"] ?? "")"
        return env
    }
}

private func dictionaryValue(_ dictionary: [String: Any], for key: String) -> [String: Any] {
    dictionary[key] as? [String: Any] ?? [:]
}

private func stringValue(_ dictionary: [String: Any], for key: String) -> String? {
    dictionary[key] as? String
}

private extension String {
    var isTerminalCodexStatus: Bool {
        ["completed", "failed", "cancelled", "interrupted"].contains(lowercased())
    }
}
