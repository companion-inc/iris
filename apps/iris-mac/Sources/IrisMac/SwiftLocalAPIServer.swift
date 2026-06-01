import CryptoKit
import Foundation
import SQLite3

final class SwiftLocalAPIServer: @unchecked Sendable {
    private let port: UInt16 = 4747
    private let databaseURL: URL
    private var server: NativeHTTPServer?
    private var nativeVoiceStatusProvider: (@Sendable () async -> NativeVoiceDebugStatus)?
    private var nativeVoicePlaybackTester: (@Sendable () async -> Bool)?
    private var nativeVoiceEventTester: (@Sendable (String) async -> Bool)?
    private var nativeVoiceStarter: (@Sendable () async -> Bool)?

    init(repoRoot: URL = ProcessSupervisor.resolveRepoRoot()) {
        self.databaseURL = repoRoot.appending(path: "apps/iris-api/.iris/iris.sqlite")
    }

    func start() throws {
        if server != nil {
            return
        }
        try FileManager.default.createDirectory(at: databaseURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try initializeDatabaseIfNeeded()
        let server = NativeHTTPServer(port: port, label: "iris.native.local-api") { [weak self] data in
            guard let self else {
                return json(["ok": false, "error": "server stopped"], status: 503).data
            }
            return await self.handle(data).data
        }
        try server.start()
        self.server = server
    }

    func stop() {
        server?.stop()
        server = nil
    }

    func setNativeVoiceStatusProvider(_ provider: @escaping @Sendable () async -> NativeVoiceDebugStatus) {
        nativeVoiceStatusProvider = provider
    }

    func setNativeVoicePlaybackTester(_ tester: @escaping @Sendable () async -> Bool) {
        nativeVoicePlaybackTester = tester
    }

    func setNativeVoiceEventTester(_ tester: @escaping @Sendable (String) async -> Bool) {
        nativeVoiceEventTester = tester
    }

    func setNativeVoiceStarter(_ starter: @escaping @Sendable () async -> Bool) {
        nativeVoiceStarter = starter
    }

    func recordAgentBridgeCompletion(_ completion: SwiftCodexBridgeCompletion) async {
        guard let database = openDatabase() else { return }
        defer { sqlite3_close(database) }
        completeLocalAgentRun(database: database, runID: completion.runId, result: completion.result)
    }

    private func handle(_ data: Data) async -> LocalHTTPResponse {
        let request = LocalHTTPRequest(data: data)
        switch (request.method, request.path) {
        case ("GET", "/health"):
            return json(["ok": true, "service": "iris-api-swift", "environment": "local"])
        case ("GET", "/debug/native-voice"):
            if let nativeVoiceStatusProvider {
                return json((await nativeVoiceStatusProvider()).jsonObject)
            }
            return json(["ok": false, "error": "native voice unavailable"], status: 503)
        case ("POST", "/debug/native-voice/play-test"):
            guard let nativeVoicePlaybackTester else {
                return json(["ok": false, "error": "native voice unavailable"], status: 503)
            }
            let played = await nativeVoicePlaybackTester()
            return json(["ok": played])
        case ("POST", "/debug/native-voice/event"):
            guard let nativeVoiceEventTester else {
                return json(["ok": false, "error": "native voice unavailable"], status: 503)
            }
            let body = jsonObject(request.body)
            guard let type = body["type"] as? String, !type.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                return json(["ok": false, "error": "missing event type"], status: 400)
            }
            let injected = await nativeVoiceEventTester(type)
            return json(["ok": injected, "type": type])
        case ("POST", "/debug/native-voice/start"):
            guard let nativeVoiceStarter else {
                return json(["ok": false, "error": "native voice unavailable"], status: 503)
            }
            let started = await nativeVoiceStarter()
            return json(["ok": started])
        case ("GET", "/health/voice"):
            return json(["ok": true, "service": "iris-api-swift", "voice": ["url": "http://127.0.0.1:4748"]])
        case ("POST", "/v1/voice/sessions"):
            return createVoiceSession(body: request.body)
        case ("GET", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/config"):
            return voiceConfig(sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/config"))
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/events/token"):
            return json(["token": NSNull(), "url": NSNull(), "expiresAt": NSNull()])
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/agent"):
            return await voiceAgentAction(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/agent"),
                body: request.body
            )
        case ("GET", let path) where path.hasPrefix("/v1/voice/sessions/") && path.contains("/agent/completions"):
            return agentCompletions(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/agent/completions"),
                query: request.query
            )
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.contains("/agent/completions/") && path.hasSuffix("/delivered"):
            guard let ids = path.voiceSessionCompletionDeliveredPath() else {
                return json(["error": "Agent completion not found"], status: 404)
            }
            return markAgentCompletionDelivered(sessionID: ids.sessionID, completionID: ids.completionID)
        case ("GET", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/memories"):
            return voiceMemories(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/memories"),
                query: request.query
            )
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/memories"):
            return saveVoiceMemory(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/memories"),
                body: request.body
            )
        case ("PATCH", let path) where path.hasPrefix("/v1/voice/sessions/") && path.contains("/memories/"):
            guard let ids = path.voiceSessionMemoryPath() else {
                return json(["error": "Memory not found"], status: 404)
            }
            return updateVoiceMemory(sessionID: ids.sessionID, memoryID: ids.memoryID, body: request.body)
        case ("DELETE", let path) where path.hasPrefix("/v1/voice/sessions/") && path.contains("/memories/"):
            guard let ids = path.voiceSessionMemoryPath() else {
                return json(["error": "Memory not found"], status: 404)
            }
            return deleteVoiceMemory(sessionID: ids.sessionID, memoryID: ids.memoryID)
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/device-volume"):
            return updateDeviceVolume(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/device-volume"),
                body: request.body
            )
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/device-light"):
            return updateDeviceLight(
                sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/device-light"),
                body: request.body
            )
        case ("POST", let path) where path.hasPrefix("/v1/voice/sessions/") && path.hasSuffix("/end"):
            return endVoiceSession(sessionID: path.pathComponent(after: "/v1/voice/sessions/", before: "/end"))
        case ("GET", "/v1/devices"):
            return deviceList(query: request.query)
        case ("GET", "/v1/inventory"):
            return inventory(query: request.query)
        case ("GET", "/v1/speaker-profiles"):
            return speakerProfiles()
        case ("GET", "/v1/speaker-profile"):
            return speakerProfile()
        case ("PATCH", "/v1/speaker-profile"):
            return updateSpeakerProfile(body: request.body)
        case ("POST", "/v1/speaker-profile/enroll"):
            return await enrollSpeakerProfile(body: request.body)
        case ("GET", "/v1/transcripts"):
            return transcriptList(query: request.query)
        case ("GET", "/v1/transcripts/search"):
            return transcriptSearch(query: request.query)
        case ("GET", "/v1/voice-sessions"):
            return voiceSessionList(query: request.query)
        case ("POST", "/v1/transcripts/events"):
            return ingestTranscriptEvent(body: request.body)
        default:
            return json(["ok": false, "error": "not found"], status: 404)
        }
    }

    private func createVoiceSession(body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let bodyObject = jsonObject(body)
        let requestedDeviceID = bodyObject["deviceId"] as? String
        guard let device = findDevice(database: database, id: requestedDeviceID) else {
            return json(["error": "No local Iris device found"], status: 404)
        }

        let now = isoNow()
        let sessionID = "voice_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        _ = execute(database, "update voice_sessions set status = 'ended', ended_at = ?, updated_at = ? where device_id = ? and status = 'active' and ended_at is null", [now, now, device.id])
        _ = execute(
            database,
            """
            insert into voice_sessions
            (id, organization_id, user_id, device_id, source, room_name, status, started_at, ended_at, created_at, updated_at)
            values (?, ?, ?, ?, 'device', ?, 'active', ?, null, ?, ?)
            """,
            [sessionID, device.organizationID, device.userID, device.id, sessionID, now, now, now]
        )
        _ = execute(database, "update devices set status = 'listening', last_seen_at = ?, updated_at = ? where id = ?", [now, now, device.id])

        let sampleRate = bodyObject["sampleRate"] as? Int ?? 16_000
        let channels = bodyObject["channels"] as? Int ?? 1
        let initialAwake = bodyObject["initialAwake"] as? Bool ?? false
        let expiresAt = Date().addingTimeInterval(600)
        let token = signedVoiceToken(
            sessionID: sessionID,
            device: device,
            sampleRate: sampleRate,
            channels: channels,
            initialAwake: initialAwake,
            expiresAt: expiresAt
        )
        return json([
            "voiceUrl": "ws://127.0.0.1:4748/ws?token=\(token)",
            "sessionId": sessionID,
            "expiresAt": isoDate(expiresAt)
        ])
    }

    private func voiceConfig(sessionID: String?) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["keyterms": [], "llm": [:]], status: 200)
        }
        defer { sqlite3_close(database) }
        guard sessionExists(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let memories: [[String: Any]]
        let session = voiceSession(database: database, id: sessionID)
        if let session {
            memories = listMemories(database: database, organizationID: session.organizationID, userID: session.userID, limit: 24)
        } else {
            memories = []
        }
        let profiles = listSpeakerRecognitionProfiles(database: database, organizationID: session?.organizationID)
        return json([
            "keyterms": [],
            "llm": [:],
            "soundRecognition": [:],
            "speakerRecognition": ["provider": "speechbrain-ecapa", "profiles": profiles],
            "memories": memories
        ])
    }

    private func ingestTranscriptEvent(body: Data) -> LocalHTTPResponse {
        let object = jsonObject(body)
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        guard let organizationID = object["organizationId"] as? String,
              let userID = object["userId"] as? String,
              let sessionID = object["sessionId"] as? String,
              let transcript = object["transcript"] as? String else {
            return json(["error": "Missing transcript fields"], status: 400)
        }

        let segmentID = object["segmentId"] as? String ?? "seg_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let isFinal = object["isFinal"] as? Bool ?? true
        let now = isoNow()
        let words = jsonString(object["words"])
        let startedAt = object["startedAt"] as? String ?? now
        let endedAt = isFinal ? (object["endedAt"] as? String ?? now) : nil
        let didInsert = execute(
            database,
            """
            insert into transcript_segments
            (id, organization_id, user_id, device_id, session_id, source, text, words, is_interim, speaker_label,
             speaker_user_id, speaker_name, speaker_confidence, emotion_label, emotion_confidence, emotion_model,
             confidence, started_at, ended_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
              text = excluded.text,
              words = excluded.words,
              is_interim = excluded.is_interim,
              speaker_label = excluded.speaker_label,
              speaker_user_id = excluded.speaker_user_id,
              speaker_confidence = excluded.speaker_confidence,
              emotion_label = excluded.emotion_label,
              emotion_confidence = excluded.emotion_confidence,
              emotion_model = excluded.emotion_model,
              confidence = excluded.confidence,
              ended_at = excluded.ended_at
            """,
            [
                segmentID,
                organizationID,
                userID,
                object["deviceId"] as? String,
                sessionID,
                object["source"] as? String ?? "device",
                transcript,
                words,
                isFinal ? 0 : 1,
                object["speakerId"] as? String,
                object["speakerUserId"] as? String,
                object["speakerConfidence"],
                object["emotionLabel"] as? String,
                object["emotionConfidence"],
                object["emotionModel"] as? String,
                object["confidence"],
                startedAt,
                endedAt,
                now
            ]
        )
        guard didInsert else {
            return json(["error": "transcript insert failed", "sqlite": databaseError(database)], status: 500)
        }
        return json(["ok": true, "segment": ["id": segmentID]])
    }

    private func transcriptList(query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let limit = clampInt(query["limit"], defaultValue: 25, range: 1...100)
        var clauses = ["is_interim = 0"]
        var values: [Any?] = []
        if let sessionID = nonEmpty(query["sessionId"]) {
            clauses.append("session_id = ?")
            values.append(sessionID)
        }
        if let deviceID = nonEmpty(query["deviceId"]) {
            clauses.append("device_id = ?")
            values.append(deviceID)
        }
        if let before = nonEmpty(query["before"]) {
            clauses.append("started_at < ?")
            values.append(before)
        }
        values.append(limit + 1)

        let rows = queryRows(
            database: database,
            sql: """
            select id, user_id, device_id, session_id, source, text, words, is_interim, speaker_label,
                   speaker_user_id, speaker_name, speaker_confidence, emotion_label, emotion_confidence,
                   emotion_model, confidence, started_at, ended_at, created_at
            from transcript_segments
            where \(clauses.joined(separator: " and "))
            order by started_at desc
            limit ?
            """,
            values: values
        ) { statement, _ in
            publicTranscriptSegment(statement, includeWords: true)
        }
        let segments = Array(rows.prefix(limit))
        let nextCursor = rows.count > limit ? (segments.last?["startedAt"] ?? NSNull()) : NSNull()
        return json(["segments": segments, "nextCursor": nextCursor])
    }

    private func transcriptSearch(query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let limit = clampInt(query["limit"], defaultValue: 20, range: 1...50)
        let queryText = (query["query"] ?? query["q"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        var clauses = ["is_interim = 0"]
        var values: [Any?] = []
        if let sessionID = nonEmpty(query["sessionId"]) {
            clauses.append("session_id = ?")
            values.append(sessionID)
        }
        if let deviceID = nonEmpty(query["deviceId"]) {
            clauses.append("device_id = ?")
            values.append(deviceID)
        }
        if let from = nonEmpty(query["from"] ?? query["start"]) {
            clauses.append("started_at >= ?")
            values.append(from)
        }
        if let to = nonEmpty(query["to"] ?? query["end"]) {
            clauses.append("started_at < ?")
            values.append(to)
        }
        if let before = nonEmpty(query["before"]) {
            clauses.append("started_at < ?")
            values.append(before)
        }
        if !queryText.isEmpty {
            clauses.append("lower(text) like ? escape '\\'")
            values.append("%\(escapeLike(queryText))%")
        }
        let sql = """
        select id, user_id, device_id, session_id, source, text, words, is_interim, speaker_label,
               speaker_user_id, speaker_name, speaker_confidence, confidence, started_at, ended_at, created_at
        from transcript_segments
        where \(clauses.joined(separator: " and "))
        order by
          case
            when ? = '' then 2
            when lower(text) = ? then 0
            when lower(text) like ? escape '\\' then 1
            else 2
          end,
          started_at desc
        limit ?
        """
        let orderedValues = values + [queryText, queryText, "\(escapeLike(queryText))%", limit]
        let segments = queryRows(database: database, sql: sql, values: orderedValues) { statement, index in
            [
                "id": columnText(statement, 0) ?? "",
                "userId": columnText(statement, 1) ?? "",
                "deviceId": nullableText(statement, 2),
                "sessionId": columnText(statement, 3) ?? "",
                "source": columnText(statement, 4) ?? "",
                "text": columnText(statement, 5) ?? "",
                "words": jsonValue(columnText(statement, 6)) ?? NSNull(),
                "isInterim": sqlite3_column_int(statement, 7) != 0,
                "speakerLabel": nullableText(statement, 8),
                "speakerUserId": nullableText(statement, 9),
                "speakerName": nullableText(statement, 10),
                "speakerConfidence": columnDoubleOrNull(statement, 11),
                "confidence": columnDoubleOrNull(statement, 12),
                "startedAt": columnText(statement, 13) ?? "",
                "endedAt": nullableText(statement, 14),
                "createdAt": columnText(statement, 15) ?? "",
                "score": queryText.isEmpty ? NSNull() : Double(1) / Double(index + 1)
            ] as [String: Any]
        }
        return json(["segments": segments, "facets": ["devices": [], "sessions": [], "speakers": []], "nextCursor": NSNull()])
    }

    private func voiceSessionList(query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let limit = clampInt(query["limit"], defaultValue: 25, range: 1...50)
        let segmentLimit = clampInt(query["segmentLimit"], defaultValue: 12, range: 1...50)
        var clauses = [
            """
            (status = 'active' or exists (
              select 1 from transcript_segments ts
              where ts.session_id = voice_sessions.id
                and ts.is_interim = 0
            ))
            """
        ]
        var values: [Any?] = []
        if let deviceID = nonEmpty(query["deviceId"]) {
            clauses.append("device_id = ?")
            values.append(deviceID)
        }
        if let before = nonEmpty(query["before"]) {
            clauses.append("started_at < ?")
            values.append(before)
        }
        values.append(limit + 1)

        let rows = queryRows(
            database: database,
            sql: """
            select id, user_id, device_id, room_name, status, started_at, ended_at, created_at, updated_at
            from voice_sessions
            where \(clauses.joined(separator: " and "))
            order by started_at desc
            limit ?
            """,
            values: values
        ) { statement, _ in
            let sessionID = columnText(statement, 0) ?? ""
            return [
                "id": sessionID,
                "userId": columnText(statement, 1) ?? "",
                "deviceId": nullableText(statement, 2),
                "roomName": nullableText(statement, 3),
                "status": columnText(statement, 4) ?? "",
                "startedAt": columnText(statement, 5) ?? "",
                "endedAt": nullableText(statement, 6),
                "createdAt": columnText(statement, 7) ?? "",
                "updatedAt": columnText(statement, 8) ?? "",
                "segments": transcriptSegments(database: database, sessionID: sessionID, limit: segmentLimit, includeWords: false)
            ] as [String: Any]
        }
        let sessions = Array(rows.prefix(limit))
        let nextCursor = rows.count > limit ? (sessions.last?["startedAt"] ?? NSNull()) : NSNull()
        return json(["sessions": sessions, "nextCursor": nextCursor])
    }

    private func voiceMemories(sessionID: String?, query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["memories": [], "nextCursor": NSNull()])
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let limit = clampInt(query["limit"], defaultValue: 24, range: 1...50)
        let queryText = nonEmpty(query["query"] ?? query["q"])?.lowercased()
        var clauses = [
            "organization_id = ?",
            "user_id = ?",
            "status = 'active'"
        ]
        var values: [Any?] = [session.organizationID, session.userID]
        if let queryText {
            clauses.append("lower(content) like ? escape '\\'")
            values.append("%\(escapeLike(queryText))%")
        }
        values.append(limit)
        let memories = listMemories(database: database, matching: clauses, values: values)
        return json(["memories": memories, "nextCursor": NSNull()])
    }

    private func saveVoiceMemory(sessionID: String?, body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["error": "Voice session not found"], status: 404)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let object = jsonObject(body)
        guard let content = normalizedText(object["content"] as? String), content.count <= 500 else {
            return json(["error": "Memory content is required"], status: 400)
        }
        guard let kind = enumValue(object["kind"] as? String, defaultValue: "fact", allowed: ["fact", "preference", "instruction"]),
              let confidence = enumValue(object["confidence"] as? String, defaultValue: "high", allowed: ["explicit", "high", "medium"]) else {
            return json(["error": "Invalid memory fields"], status: 400)
        }
        let now = isoNow()
        let normalizedContent = normalizeMemoryContent(content)
        let memoryID = "mem_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let didSave = execute(
            database,
            """
            insert into user_memories
            (id, organization_id, user_id, source_device_id, source_session_id, kind, content, normalized_content,
             confidence, status, metadata, last_used_at, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, null, ?, ?)
            on conflict(organization_id, user_id, normalized_content) do update set
              source_device_id = excluded.source_device_id,
              source_session_id = excluded.source_session_id,
              kind = excluded.kind,
              content = excluded.content,
              confidence = excluded.confidence,
              status = 'active',
              metadata = excluded.metadata,
              updated_at = excluded.updated_at
            """,
            [memoryID, session.organizationID, session.userID, session.deviceID, session.id, kind, content, normalizedContent, confidence, "{\"source\":\"voice\"}", now, now]
        )
        guard didSave, let memory = memory(database: database, organizationID: session.organizationID, userID: session.userID, normalizedContent: normalizedContent) else {
            return json(["error": "memory save failed", "sqlite": databaseError(database)], status: 500)
        }
        return json(["ok": true, "memory": memory])
    }

    private func updateVoiceMemory(sessionID: String, memoryID: String, body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let object = jsonObject(body)
        var assignments = ["updated_at = ?"]
        var values: [Any?] = [isoNow()]
        if let contentValue = object["content"] as? String {
            guard let content = normalizedText(contentValue), content.count <= 500 else {
                return json(["error": "Memory content is required"], status: 400)
            }
            assignments.append("content = ?")
            assignments.append("normalized_content = ?")
            values.append(content)
            values.append(normalizeMemoryContent(content))
        }
        if object.keys.contains("kind") {
            guard let kind = enumValue(object["kind"] as? String, defaultValue: nil, allowed: ["fact", "preference", "instruction"]) else {
                return json(["error": "Invalid memory kind"], status: 400)
            }
            assignments.append("kind = ?")
            values.append(kind)
        }
        if object.keys.contains("confidence") {
            guard let confidence = enumValue(object["confidence"] as? String, defaultValue: nil, allowed: ["explicit", "high", "medium"]) else {
                return json(["error": "Invalid memory confidence"], status: 400)
            }
            assignments.append("confidence = ?")
            values.append(confidence)
        }
        values.append(contentsOf: [memoryID, session.organizationID, session.userID])
        let didUpdate = execute(
            database,
            """
            update user_memories
            set \(assignments.joined(separator: ", ")),
                metadata = '{"source":"voice"}'
            where id = ?
              and organization_id = ?
              and user_id = ?
              and status = 'active'
            """,
            values
        )
        guard didUpdate, let memory = memory(database: database, organizationID: session.organizationID, userID: session.userID, memoryID: memoryID) else {
            return json(["error": "Memory not found"], status: 404)
        }
        return json(["ok": true, "memory": memory])
    }

    private func deleteVoiceMemory(sessionID: String, memoryID: String) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        _ = execute(
            database,
            """
            update user_memories
            set status = 'deleted',
                updated_at = ?
            where id = ?
              and organization_id = ?
              and user_id = ?
              and status = 'active'
            """,
            [isoNow(), memoryID, session.organizationID, session.userID]
        )
        guard let memory = memory(database: database, organizationID: session.organizationID, userID: session.userID, memoryID: memoryID) else {
            return json(["error": "Memory not found"], status: 404)
        }
        return json(["ok": true, "memory": memory])
    }

    private func updateDeviceVolume(sessionID: String?, body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["error": "Voice session not found"], status: 404)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID),
              let deviceID = session.deviceID,
              var device = deviceSettings(database: database, deviceID: deviceID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let object = jsonObject(body)
        let action = (object["action"] as? String) ?? "set"
        guard ["set", "increase", "decrease", "mute", "unmute"].contains(action) else {
            return json(["error": "Invalid volume action"], status: 400)
        }
        let requestedVolume = numericInt(object["volume"])
        let currentVolume = currentSpeakerVolume(settings: device.settings, hardwareInfo: device.hardwareInfo)
        let speakerVolume = nextSpeakerVolume(currentVolume: currentVolume, action: action, volume: requestedVolume)
        device.settings["speakerVolume"] = speakerVolume
        guard updateDeviceSettings(database: database, deviceID: deviceID, settings: device.settings) else {
            return json(["error": "device volume update failed", "sqlite": databaseError(database)], status: 500)
        }
        return json(["deviceId": deviceID, "volume": speakerVolume, "deviceSyncOk": false])
    }

    private func updateDeviceLight(sessionID: String?, body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["error": "Voice session not found"], status: 404)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID),
              let deviceID = session.deviceID,
              var device = deviceSettings(database: database, deviceID: deviceID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let object = jsonObject(body)
        var statusLight: [String: Any] = [:]
        if let effect = object["effect"] as? String {
            guard ["off", "breath", "rainbow", "solid", "doa"].contains(effect) else {
                return json(["error": "Invalid light effect"], status: 400)
            }
            statusLight["effect"] = effect
        }
        if let color = nonEmpty(object["color"] as? String) {
            guard color.range(of: #"^(#|0x)?[0-9a-fA-F]{6}$"#, options: .regularExpression) != nil else {
                return json(["error": "Invalid light color"], status: 400)
            }
            statusLight["color"] = color
        }
        if object.keys.contains("brightness") {
            guard let brightness = numericInt(object["brightness"]) else {
                return json(["error": "Invalid light brightness"], status: 400)
            }
            statusLight["brightness"] = min(max(brightness, 0), 255)
        }
        guard !statusLight.isEmpty else {
            return json(["error": "No light setting provided"], status: 400)
        }
        device.settings["statusLight"] = statusLight
        guard updateDeviceSettings(database: database, deviceID: deviceID, settings: device.settings) else {
            return json(["error": "device light update failed", "sqlite": databaseError(database)], status: 500)
        }
        return json(["deviceId": deviceID, "statusLight": statusLight])
    }

    private func voiceAgentAction(sessionID: String?, body: Data) async -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["error": "Voice session not found"], status: 404)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }

        let original = jsonObject(body)
        let action = agentAction(from: original)
        if ["start", "steer"].contains(action), !hasAgentPrompt(original) {
            return json(["error": "\(action) requires prompt"], status: 400)
        }
        let agentID = localAgentID(from: session.deviceID)
        let prompt = agentPrompt(from: original)
        let runID = "agent_run_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let shouldRecordRun = ["start", "steer"].contains(action) && agentID != nil && prompt != nil

        var payload = original
        payload["action"] = action
        if shouldRecordRun, let agentID, let prompt {
            insertLocalAgentRun(
                database: database,
                runID: runID,
                session: session,
                agentID: agentID,
                action: action,
                prompt: prompt,
                context: original["context"] as? String,
                responseStyle: original["responseStyle"] as? String,
                request: payload
            )
        }
        if payload["iris"] == nil {
            payload["iris"] = [
                "deviceId": session.deviceID as Any? ?? NSNull(),
                "sessionId": session.id,
                "userId": session.userID,
                "organizationId": session.organizationID,
                "runId": shouldRecordRun ? runID as Any : NSNull(),
                "source": "device"
            ]
        } else if shouldRecordRun, var iris = payload["iris"] as? [String: Any] {
            iris["runId"] = runID
            iris["sessionId"] = iris["sessionId"] ?? session.id
            payload["iris"] = iris
        }

        do {
            let bridgePayload = try await postJSON(to: URL(string: "http://127.0.0.1:4750/agent")!, object: payload, timeout: 40)
            var response = bridgePayload
            response["requestId"] = response["requestId"] ?? response["turnId"] ?? response["id"] ?? NSNull()
            response["agentId"] = response["agentId"] ?? agentID ?? NSNull()
            if shouldRecordRun {
                response["runId"] = response["runId"] ?? runID
            }
            if shouldRecordRun {
                let status = String(describing: response["status"] ?? "").lowercased()
                if status != "running" && status != "queued" {
                    completeLocalAgentRun(database: database, runID: runID, result: response)
                }
            }
            return json(response)
        } catch {
            if shouldRecordRun {
                completeLocalAgentRun(
                    database: database,
                    runID: runID,
                    result: ["ok": false, "status": "failed", "error": error.localizedDescription]
                )
            }
            return json(["ok": false, "status": "failed", "error": error.localizedDescription], status: 502)
        }
    }

    private func agentCompletions(sessionID: String?, query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["error": "Voice session not found"], status: 404)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let limit = clampInt(query["limit"], defaultValue: 20, range: 1...50)
        var clauses = [
            "organization_id = ?",
            "session_id = ?",
            "delivered_at is null"
        ]
        var values: [Any?] = [session.organizationID, session.id]
        if let after = nonEmpty(query["after"]) {
            clauses.append("created_at > ?")
            values.append(after)
        }
        values.append(limit)
        let completions = queryRows(
            database: database,
            sql: """
            select id, organization_id, user_id, run_id, session_id, source_device_id, agent_id, thread_id,
                   delivery, status, content, result, error, delivered_at, created_at, updated_at
            from agent_completions
            where \(clauses.joined(separator: " and "))
            order by created_at desc
            limit ?
            """,
            values: values
        ) { statement, _ in
            publicAgentCompletion(statement)
        }
        return json(["completions": completions])
    }

    private func markAgentCompletionDelivered(sessionID: String, completionID: String) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let session = voiceSession(database: database, id: sessionID) else {
            return json(["error": "Voice session not found"], status: 404)
        }
        let now = isoNow()
        _ = execute(
            database,
            """
            update agent_completions
            set delivered_at = ?,
                updated_at = ?
            where id = ?
              and organization_id = ?
              and session_id = ?
            """,
            [now, now, completionID, session.organizationID, session.id]
        )
        guard let completion = agentCompletion(database: database, organizationID: session.organizationID, sessionID: session.id, completionID: completionID) else {
            return json(["error": "Agent completion not found"], status: 404)
        }
        return json(["completion": completion])
    }

    private func endVoiceSession(sessionID: String?) -> LocalHTTPResponse {
        guard let database = openDatabase(), let sessionID else {
            return json(["ok": true])
        }
        defer { sqlite3_close(database) }
        let now = isoNow()
        _ = execute(database, "update voice_sessions set status = 'ended', ended_at = ?, updated_at = ? where id = ?", [now, now, sessionID])
        return json(["ok": true])
    }

    private func deviceList(query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let limit = clampInt(query["limit"], defaultValue: 100, range: 1...200)
        let devices = listPublicDevices(database: database, limit: limit)
        return json(["devices": devices])
    }

    private func inventory(query: [String: String]) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }

        let limit = clampInt(query["limit"], defaultValue: 100, range: 1...200)
        let devices = listPublicDevices(database: database, limit: limit)
        let hardware = devices.filter { ($0["kind"] as? String) == "hardware" }
        let agentDevices = devices.filter { ($0["kind"] as? String) == "agent" && ($0["status"] as? String) != "pairing" }
        let threadLimit = clampInt(query["threadLimit"], defaultValue: 50, range: 1...100)
        let runLimit = clampInt(query["runLimit"], defaultValue: 50, range: 1...100)
        let completionLimit = clampInt(query["completionLimit"], defaultValue: 50, range: 1...100)
        let approvalLimit = clampInt(query["approvalLimit"], defaultValue: 50, range: 1...100)
        let threads = listPublicCodexThreads(database: database, limit: threadLimit)
        return json([
            "devices": devices,
            "hardware": hardware,
            "agentDevices": agentDevices,
            "agents": agentDevices.map { agent in
                var agent = agent
                if let agentID = agent["id"] as? String {
                    agent["threads"] = threads.filter { ($0["agentId"] as? String) == agentID }
                } else {
                    agent["threads"] = []
                }
                return agent
            },
            "threads": threads,
            "runs": listPublicAgentRuns(database: database, limit: runLimit),
            "completions": listPublicAgentCompletions(database: database, limit: completionLimit),
            "approvals": listPublicAgentApprovals(database: database, limit: approvalLimit)
        ])
    }

    private func speakerProfiles() -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let user = localUser(database: database) else {
            return json(["members": []])
        }
        return json(["members": listOrganizationSpeakerProfiles(database: database, organizationID: user.organizationID)])
    }

    private func speakerProfile() -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let user = localUser(database: database) else {
            return json(["error": "No local Iris user found"], status: 404)
        }
        guard let profile = ensureSpeakerProfile(database: database, user: user) else {
            return json(["error": "speaker profile unavailable"], status: 500)
        }
        return json(["profile": publicSpeakerProfile(profile, user: user)])
    }

    private func updateSpeakerProfile(body: Data) -> LocalHTTPResponse {
        guard let database = openDatabase() else {
            return json(["error": "database unavailable"], status: 503)
        }
        defer { sqlite3_close(database) }
        guard let user = localUser(database: database),
              let current = ensureSpeakerProfile(database: database, user: user) else {
            return json(["error": "No local Iris user found"], status: 404)
        }
        let object = jsonObject(body)
        let enabled = object["enabled"] as? Bool
        let nextStatus: String
        if let enabled {
            nextStatus = enabled ? (current.status == "disabled" ? "not_registered" : current.status) : "disabled"
        } else {
            nextStatus = current.status
        }
        _ = execute(
            database,
            "update speaker_profiles set status = ?, updated_at = ? where id = ? and organization_id = ?",
            [nextStatus, isoNow(), current.id, user.organizationID]
        )
        guard let profile = storedSpeakerProfile(database: database, organizationID: user.organizationID, userID: user.id) else {
            return json(["error": "speaker profile unavailable"], status: 500)
        }
        return json(["profile": publicSpeakerProfile(profile, user: user)])
    }

    private func enrollSpeakerProfile(body: Data) async -> LocalHTTPResponse {
        let object = jsonObject(body)
        guard let samples = object["samples"] as? [[String: Any]], !samples.isEmpty, samples.count <= 8 else {
            return json(["error": "Expected 1-8 audio samples"], status: 400)
        }
        do {
            let enrollment = try await postJSON(
                to: URL(string: "http://127.0.0.1:4749/v1/enroll")!,
                object: ["samples": samples],
                timeout: 45
            )
            guard let embedding = enrollment["embedding"] as? [Double], !embedding.isEmpty else {
                return json(["error": "Speaker enrollment did not return an embedding"], status: 502)
            }
            guard let database = openDatabase() else {
                return json(["error": "database unavailable"], status: 503)
            }
            defer { sqlite3_close(database) }
            guard let user = localUser(database: database),
                  let current = ensureSpeakerProfile(database: database, user: user),
                  let embeddingJSON = jsonString(embedding) else {
                return json(["error": "speaker profile unavailable"], status: 500)
            }
            let now = isoNow()
            let sampleCount = numericInt(enrollment["sampleCount"]) ?? samples.count
            let model = nonEmpty(enrollment["model"] as? String) ?? "speechbrain/spkrec-ecapa-voxceleb"
            _ = execute(
                database,
                """
                update speaker_profiles
                set status = 'registered',
                    provider = 'speechbrain-ecapa',
                    sample_count = ?,
                    model = ?,
                    embedding_ciphertext = ?,
                    enrolled_at = ?,
                    updated_at = ?
                where id = ?
                  and organization_id = ?
                """,
                [sampleCount, model, embeddingJSON, now, now, current.id, user.organizationID]
            )
            guard let profile = storedSpeakerProfile(database: database, organizationID: user.organizationID, userID: user.id) else {
                return json(["error": "speaker profile unavailable"], status: 500)
            }
            return json(["profile": publicSpeakerProfile(profile, user: user)])
        } catch {
            return json(["error": error.localizedDescription], status: 502)
        }
    }

    private func openDatabase() -> OpaquePointer? {
        var database: OpaquePointer?
        guard sqlite3_open_v2(databaseURL.path, &database, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK, let database else {
            if let database { sqlite3_close(database) }
            return nil
        }
        configureSQLiteConnection(database)
        return database
    }

    private func initializeDatabaseIfNeeded() throws {
        var database: OpaquePointer?
        guard sqlite3_open_v2(databaseURL.path, &database, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) == SQLITE_OK, let database else {
            if let database { sqlite3_close(database) }
            throw NSError(domain: "IrisSQLite", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not open local database"])
        }
        defer { sqlite3_close(database) }
        configureSQLiteConnection(database)
        for statement in localSchemaStatements {
            guard sqlite3_exec(database, statement, nil, nil, nil) == SQLITE_OK else {
                throw NSError(domain: "IrisSQLite", code: 2, userInfo: [NSLocalizedDescriptionKey: databaseError(database)])
            }
        }
        let now = isoNow()
        _ = execute(database, "insert or ignore into organizations (id, name, created_at, updated_at) values ('org_local', 'Local', ?, ?)", [now, now])
        _ = execute(database, "insert or ignore into users (id, email, name, first_name, last_name, created_at, updated_at) values ('user_local', 'local@iris.local', 'Local User', 'Local', 'User', ?, ?)", [now, now])
        _ = execute(database, "insert or ignore into organization_members (organization_id, user_id, role, created_at) values ('org_local', 'user_local', 'owner', ?)", [now])
        _ = execute(database, """
            insert or ignore into devices
            (id, organization_id, user_id, kind, product, model, name, status, settings, hardware_info, last_seen_at, created_at, updated_at)
            values ('agent_local_smoke', 'org_local', 'user_local', 'desktop', 'iris-mac', 'native', 'Iris Desktop', 'online', '{}', '{}', ?, ?, ?)
            """, [now, now, now])
    }
}

private let localSchemaStatements = [
    "create table if not exists organizations (id text primary key, name text not null, created_at text not null, updated_at text not null)",
    "create table if not exists users (id text primary key, email text not null unique, name text, first_name text, last_name text, created_at text not null, updated_at text not null)",
    "create table if not exists organization_members (organization_id text not null, user_id text not null, role text not null, created_at text not null, primary key (organization_id, user_id))",
    "create table if not exists devices (id text primary key, organization_id text not null, user_id text not null, kind text not null, product text, model text, name text not null, status text not null, settings text not null, device_serial text, firmware_version text, hardware_info text, last_seen_at text, created_at text not null, updated_at text not null)",
    "create table if not exists agent_runs (id text primary key, organization_id text not null, user_id text not null, session_id text, source_device_id text, agent_id text not null, thread_id text, status text not null, action text not null, prompt text, context text, response_style text, request text not null, result text, error text, created_at text not null, updated_at text not null, started_at text, completed_at text)",
    "create table if not exists codex_threads (id text primary key, organization_id text not null, user_id text not null, agent_id text not null, session_id text, source_device_id text, codex_thread_id text, title text, summary text, status text not null, current_run_id text, last_activity_at text not null, created_at text not null, updated_at text not null)",
    "create table if not exists agent_completions (id text primary key, organization_id text not null, user_id text not null, run_id text not null, session_id text, source_device_id text, agent_id text not null, thread_id text, delivery text not null, status text not null, content text, result text, error text, delivered_at text, created_at text not null, updated_at text not null, unique (run_id))",
    "create table if not exists agent_approvals (id text primary key, organization_id text not null, user_id text not null, run_id text, session_id text, source_device_id text, agent_id text not null, thread_id text, codex_request_id text, codex_method text not null, status text not null, request text not null, response text, error text, expires_at text, created_at text not null, updated_at text not null, resolved_at text)",
    "create table if not exists speaker_profiles (id text primary key, organization_id text not null, user_id text not null, display_name text not null, status text not null, provider text not null, sample_count integer not null default 0, model text, embedding_ciphertext text, enrolled_at text, created_at text not null, updated_at text not null, unique (organization_id, user_id))",
    "create table if not exists user_memories (id text primary key, organization_id text not null, user_id text not null, source_device_id text, source_session_id text, kind text not null, content text not null, normalized_content text not null, confidence text not null, status text not null, metadata text not null default '{}', last_used_at text, created_at text not null, updated_at text not null, unique (organization_id, user_id, normalized_content))",
    "create table if not exists voice_sessions (id text primary key, organization_id text not null, user_id text not null, device_id text not null, source text not null, room_name text not null, status text not null, started_at text not null, ended_at text, created_at text not null, updated_at text not null)",
    "create table if not exists transcript_segments (id text primary key, organization_id text not null, user_id text not null, device_id text, session_id text not null, source text not null, text text not null, words text, is_interim integer not null default 0, speaker_label text, speaker_user_id text, speaker_name text, speaker_confidence real, emotion_label text, emotion_confidence real, emotion_model text, confidence real, started_at text not null, ended_at text, created_at text not null)",
    "create table if not exists summaries (id text primary key, organization_id text not null, user_id text not null, type text not null, title text not null, summary text not null, important_points text not null default '[]', action_items text not null default '[]', source_segment_ids text not null default '[]', period_start text not null, period_end text not null, status text not null, generated_at text, created_at text not null, updated_at text not null, unique (organization_id, user_id, type, period_start, period_end))",
    "create virtual table if not exists transcript_segments_fts using fts5(text, content='transcript_segments', content_rowid='rowid')",
    "create trigger if not exists transcript_segments_fts_ai after insert on transcript_segments begin insert into transcript_segments_fts(rowid, text) values (new.rowid, new.text); end",
    "create trigger if not exists transcript_segments_fts_ad after delete on transcript_segments begin insert into transcript_segments_fts(transcript_segments_fts, rowid, text) values ('delete', old.rowid, old.text); end",
    "create trigger if not exists transcript_segments_fts_au after update on transcript_segments begin insert into transcript_segments_fts(transcript_segments_fts, rowid, text) values ('delete', old.rowid, old.text); insert into transcript_segments_fts(rowid, text) values (new.rowid, new.text); end",
    "create table if not exists event_tokens (id text primary key, token_hash text not null unique, user_id text not null, expires_at text not null, created_at text not null)",
    "create table if not exists audit_events (id text primary key, organization_id text not null, user_id text, device_id text, type text not null, data text not null, created_at text not null)",
    "create index if not exists transcript_segments_session_idx on transcript_segments (session_id, started_at)",
    "create index if not exists voice_sessions_org_started_idx on voice_sessions (organization_id, started_at desc)",
    "create index if not exists agent_completions_session_created_idx on agent_completions (session_id, created_at)",
    "create index if not exists user_memories_user_status_idx on user_memories (organization_id, user_id, status, updated_at)"
]

private struct LocalDevice {
    var id: String
    var userID: String
    var organizationID: String
}

private struct LocalUser {
    var id: String
    var organizationID: String
    var email: String
    var name: String?
}

private struct LocalSpeakerProfile {
    var id: String
    var organizationID: String
    var userID: String
    var displayName: String
    var status: String
    var provider: String
    var sampleCount: Int
    var model: String?
    var embedding: String?
    var enrolledAt: String?
    var createdAt: String
    var updatedAt: String
}

private struct LocalVoiceSession {
    var id: String
    var userID: String
    var organizationID: String
    var deviceID: String?
}

private struct LocalDeviceSettings {
    var settings: [String: Any]
    var hardwareInfo: [String: Any]
}

private struct LocalHTTPRequest {
    var method = "GET"
    var path = "/"
    var query: [String: String] = [:]
    var body = Data()

    init(data: Data) {
        guard let separatorRange = data.range(of: Data("\r\n\r\n".utf8)) else { return }
        let headerData = data[..<separatorRange.lowerBound]
        body = Data(data[separatorRange.upperBound...])
        guard let headerText = String(data: headerData, encoding: .utf8),
              let requestLine = headerText.split(separator: "\r\n").first else {
            return
        }
        let parts = requestLine.split(separator: " ")
        if parts.count >= 2 {
            method = String(parts[0])
            let target = String(parts[1])
            let splitTarget = target.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
            path = splitTarget.first.map(String.init)?.removingPercentEncoding ?? "/"
            if splitTarget.count == 2 {
                query = parseQuery(String(splitTarget[1]))
            }
        }
    }
}

private struct LocalHTTPResponse {
    var header: Data
    var body: Data

    var data: Data {
        var data = header
        data.append(body)
        return data
    }
}

private func json(_ object: [String: Any], status: Int = 200) -> LocalHTTPResponse {
    let body = (try? JSONSerialization.data(withJSONObject: sanitizeLocalJSON(object), options: [])) ?? Data("{}".utf8)
    let reason = status == 200 ? "OK" : "Error"
    let header = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    return LocalHTTPResponse(header: Data(header.utf8), body: body)
}

private func sanitizeLocalJSON(_ value: Any) -> Any {
    switch value {
    case let dictionary as [String: Any]:
        return dictionary.mapValues(sanitizeLocalJSON)
    case let array as [Any]:
        return array.map(sanitizeLocalJSON)
    default:
        return value
    }
}

private func jsonObject(_ data: Data) -> [String: Any] {
    guard !data.isEmpty,
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return [:]
    }
    return object
}

private func postJSON(to url: URL, object: [String: Any], timeout: TimeInterval) async throws -> [String: Any] {
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = timeout
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.httpBody = try JSONSerialization.data(withJSONObject: sanitizeLocalJSON(object), options: [])
    let (data, response) = try await URLSession.shared.data(for: request)
    guard let http = response as? HTTPURLResponse else {
        throw URLError(.badServerResponse)
    }
    guard (200..<300).contains(http.statusCode) else {
        let errorObject = jsonObject(data)
        let message = (errorObject["error"] as? String) ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
        throw LocalAPIError.remoteError(message)
    }
    return jsonObject(data)
}

private enum LocalAPIError: LocalizedError {
    case remoteError(String)

    var errorDescription: String? {
        switch self {
        case .remoteError(let message):
            return message
        }
    }
}

private func jsonString(_ value: Any?) -> String? {
    guard let value, JSONSerialization.isValidJSONObject(value),
          let data = try? JSONSerialization.data(withJSONObject: value, options: []) else {
        return nil
    }
    return String(decoding: data, as: UTF8.self)
}

private func agentAction(from object: [String: Any]) -> String {
    if let rawAction = object["action"] as? String {
        let action = rawAction.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if ["start", "steer", "interrupt", "status"].contains(action) {
            return action
        }
    }
    return hasAgentPrompt(object) ? "start" : "status"
}

private func agentPrompt(from object: [String: Any]) -> String? {
    for key in ["prompt", "query", "question", "task", "text"] {
        guard let value = object[key] as? String else {
            continue
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            return trimmed
        }
    }
    return nil
}

private func hasAgentPrompt(_ object: [String: Any]) -> Bool {
    agentPrompt(from: object) != nil
}

private func insertLocalAgentRun(
    database: OpaquePointer,
    runID: String,
    session: LocalVoiceSession,
    agentID: String,
    action: String,
    prompt: String,
    context: String?,
    responseStyle: String?,
    request: [String: Any]
) {
    let now = isoNow()
    _ = execute(
        database,
        """
        insert into agent_runs (
          id, organization_id, user_id, session_id, source_device_id, agent_id, thread_id,
          status, action, prompt, context, response_style, request, result, error,
          created_at, updated_at, started_at, completed_at
        ) values (?, ?, ?, ?, ?, ?, null, 'running', ?, ?, ?, ?, ?, null, null, ?, ?, ?, null)
        """,
        [
            runID,
            session.organizationID,
            session.userID,
            session.id,
            session.deviceID,
            agentID,
            action,
            prompt,
            cleanOptionalText(context),
            cleanOptionalText(responseStyle),
            jsonString(request) ?? "{}",
            now,
            now,
            now
        ]
    )
}

private func completeLocalAgentRun(database: OpaquePointer, runID: String, result: [String: Any]) {
    let now = isoNow()
    let status = localAgentCompletionStatus(result)
    let resultJSON = jsonString(result) ?? "{}"
    let error = result["error"] as? String
    _ = execute(
        database,
        """
        update agent_runs
        set status = ?, result = ?, error = ?, completed_at = ?, updated_at = ?
        where id = ? and status not in ('completed', 'failed', 'interrupted', 'cancelled')
        """,
        [status, resultJSON, cleanOptionalText(error), now, now, runID]
    )
    insertLocalAgentCompletion(database: database, runID: runID, status: status, result: result)
}

private func insertLocalAgentCompletion(
    database: OpaquePointer,
    runID: String,
    status: String,
    result: [String: Any]
) {
    let existing = queryRows(
        database: database,
        sql: "select id from agent_completions where run_id = ? limit 1",
        values: [runID]
    ) { statement, _ in
        ["id": columnText(statement, 0) ?? ""]
    }
    guard existing.isEmpty else { return }

    let rows = queryRows(
        database: database,
        sql: """
        select organization_id, user_id, session_id, source_device_id, agent_id, thread_id, request
        from agent_runs
        where id = ?
        limit 1
        """,
        values: [runID]
    ) { statement, _ in
        [
            "organizationID": columnText(statement, 0) ?? "",
            "userID": columnText(statement, 1) ?? "",
            "sessionID": nullableText(statement, 2),
            "sourceDeviceID": nullableText(statement, 3),
            "agentID": columnText(statement, 4) ?? "",
            "threadID": nullableText(statement, 5),
            "request": columnText(statement, 6) ?? "{}"
        ]
    }
    guard let run = rows.first,
          let organizationID = run["organizationID"] as? String,
          let userID = run["userID"] as? String,
          let agentID = run["agentID"] as? String else {
        return
    }
    let completionID = "agent_completion_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    let now = isoNow()
    let request = jsonDictionary(run["request"] as? String)
    let delivery = (request["delivery"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
    _ = execute(
        database,
        """
        insert into agent_completions (
          id, organization_id, user_id, run_id, session_id, source_device_id, agent_id,
          thread_id, delivery, status, content, result, error, delivered_at, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?)
        """,
        [
            completionID,
            organizationID,
            userID,
            runID,
            run["sessionID"],
            run["sourceDeviceID"],
            agentID,
            run["threadID"],
            (delivery?.isEmpty == false ? delivery : "auto"),
            status,
            localAgentCompletionContent(result),
            jsonString(result) ?? "{}",
            cleanOptionalText(result["error"] as? String),
            now,
            now
        ]
    )
}

private func localAgentCompletionStatus(_ result: [String: Any]) -> String {
    let status = String(describing: result["status"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if ["completed", "failed", "interrupted", "cancelled"].contains(status) {
        return status
    }
    if let ok = result["ok"] as? Bool, ok == false {
        return "failed"
    }
    return "completed"
}

private func localAgentCompletionContent(_ result: [String: Any]) -> String? {
    if let error = cleanOptionalText(result["error"] as? String) {
        return error
    }
    if let handoff = cleanVoiceHandoff(result["voiceHandoff"]), let spoken = handoff["suggestedSpoken"] as? String {
        return spoken
    }
    if let assistantText = cleanOptionalText(result["assistantText"] as? String) {
        return assistantText
    }
    if let run = result["run"] as? [String: Any],
       let assistantText = cleanOptionalText(run["assistantText"] as? String) {
        return assistantText
    }
    return cleanOptionalText(result["summary"] as? String)
}

private func cleanOptionalText(_ value: String?) -> String? {
    let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed?.isEmpty == false ? trimmed : nil
}

private func localAgentID(from deviceID: String?) -> String? {
    guard let deviceID, !deviceID.isEmpty else {
        return nil
    }
    return deviceID
}

private func jsonDictionary(_ text: String?) -> [String: Any] {
    guard let value = jsonValue(text) as? [String: Any] else {
        return [:]
    }
    return value
}

private func findDevice(database: OpaquePointer, id: String?) -> LocalDevice? {
    let sql: String
    if id == nil {
        sql = "select id, user_id, organization_id from devices order by updated_at desc limit 1"
    } else {
        sql = "select id, user_id, organization_id from devices where id = ? limit 1"
    }
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
        return nil
    }
    defer { sqlite3_finalize(statement) }
    if let id {
        sqlite3_bind_text(statement, 1, id, -1, SQLITE_TRANSIENT)
    }
    guard sqlite3_step(statement) == SQLITE_ROW,
          let deviceID = columnText(statement, 0),
          let userID = columnText(statement, 1),
          let organizationID = columnText(statement, 2) else {
        return nil
    }
    return LocalDevice(id: deviceID, userID: userID, organizationID: organizationID)
}

private func localUser(database: OpaquePointer) -> LocalUser? {
    let rows = queryRows(
        database: database,
        sql: """
        select users.id, organization_members.organization_id, users.email, users.name
        from organization_members
        inner join users on users.id = organization_members.user_id
        order by organization_members.created_at asc
        limit 1
        """,
        values: []
    ) { statement, _ in
        [
            "id": columnText(statement, 0) ?? "",
            "organizationID": columnText(statement, 1) ?? "",
            "email": columnText(statement, 2) ?? "",
            "name": nullableText(statement, 3)
        ]
    }
    guard let row = rows.first,
          let id = row["id"] as? String, !id.isEmpty,
          let organizationID = row["organizationID"] as? String, !organizationID.isEmpty,
          let email = row["email"] as? String, !email.isEmpty else {
        return nil
    }
    return LocalUser(id: id, organizationID: organizationID, email: email, name: row["name"] as? String)
}

private func storedSpeakerProfile(database: OpaquePointer, organizationID: String, userID: String) -> LocalSpeakerProfile? {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(
        database,
        """
        select id, organization_id, user_id, display_name, status, provider, sample_count, model,
               embedding_ciphertext, enrolled_at, created_at, updated_at
        from speaker_profiles
        where organization_id = ?
          and user_id = ?
        limit 1
        """,
        -1,
        &statement,
        nil
    ) == SQLITE_OK, let statement else {
        return nil
    }
    defer { sqlite3_finalize(statement) }
    bind(statement, position: 1, value: organizationID)
    bind(statement, position: 2, value: userID)
    guard sqlite3_step(statement) == SQLITE_ROW else {
        return nil
    }
    return localSpeakerProfile(statement)
}

private func ensureSpeakerProfile(database: OpaquePointer, user: LocalUser) -> LocalSpeakerProfile? {
    if let existing = storedSpeakerProfile(database: database, organizationID: user.organizationID, userID: user.id) {
        return existing
    }
    let now = isoNow()
    let profileID = "speaker_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    let displayName = accountDisplayName(user)
    _ = execute(
        database,
        """
        insert into speaker_profiles
        (id, organization_id, user_id, display_name, status, provider, sample_count, model, embedding_ciphertext, enrolled_at, created_at, updated_at)
        values (?, ?, ?, ?, 'not_registered', 'speechbrain-ecapa', 0, 'speechbrain/spkrec-ecapa-voxceleb', null, null, ?, ?)
        on conflict(organization_id, user_id) do update set updated_at = excluded.updated_at
        """,
        [profileID, user.organizationID, user.id, displayName, now, now]
    )
    return storedSpeakerProfile(database: database, organizationID: user.organizationID, userID: user.id)
}

private func listOrganizationSpeakerProfiles(database: OpaquePointer, organizationID: String) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select organization_members.user_id, users.email, users.name,
               speaker_profiles.id, speaker_profiles.display_name, speaker_profiles.status,
               speaker_profiles.provider, speaker_profiles.sample_count, speaker_profiles.model,
               speaker_profiles.enrolled_at, speaker_profiles.created_at, speaker_profiles.updated_at
        from organization_members
        inner join users on users.id = organization_members.user_id
        left join speaker_profiles
          on speaker_profiles.user_id = organization_members.user_id
         and speaker_profiles.organization_id = organization_members.organization_id
        where organization_members.organization_id = ?
        order by organization_members.created_at asc
        """,
        values: [organizationID]
    ) { statement, _ in
        let user = LocalUser(
            id: columnText(statement, 0) ?? "",
            organizationID: organizationID,
            email: columnText(statement, 1) ?? "",
            name: columnText(statement, 2)
        )
        let profile: Any
        if let profileID = columnText(statement, 3) {
            profile = [
                "id": profileID,
                "userId": user.id,
                "displayName": accountDisplayName(user),
                "status": columnText(statement, 5) ?? "not_registered",
                "provider": columnText(statement, 6) ?? "speechbrain-ecapa",
                "sampleCount": Int(sqlite3_column_int(statement, 7)),
                "model": nullableText(statement, 8),
                "enrolledAt": nullableText(statement, 9),
                "createdAt": columnText(statement, 10) ?? "",
                "updatedAt": columnText(statement, 11) ?? ""
            ]
        } else {
            profile = NSNull()
        }
        return [
            "userId": user.id,
            "email": user.email,
            "name": user.name ?? NSNull(),
            "profile": profile
        ]
    }
}

private func listSpeakerRecognitionProfiles(database: OpaquePointer, organizationID: String?) -> [[String: Any]] {
    guard let organizationID else {
        return []
    }
    return queryRows(
        database: database,
        sql: """
        select speaker_profiles.user_id, users.email, users.name, speaker_profiles.embedding_ciphertext
        from speaker_profiles
        inner join users on users.id = speaker_profiles.user_id
        where speaker_profiles.organization_id = ?
          and speaker_profiles.status = 'registered'
          and speaker_profiles.embedding_ciphertext is not null
        order by speaker_profiles.updated_at desc
        """,
        values: [organizationID]
    ) { statement, _ in
        let embedding = (jsonValue(columnText(statement, 3)) as? [Any]) ?? []
        let user = LocalUser(
            id: columnText(statement, 0) ?? "",
            organizationID: organizationID,
            email: columnText(statement, 1) ?? "",
            name: columnText(statement, 2)
        )
        return [
            "userId": user.id,
            "displayName": accountDisplayName(user),
            "provider": "speechbrain-ecapa",
            "embedding": embedding
        ]
    }
}

private func localSpeakerProfile(_ statement: OpaquePointer) -> LocalSpeakerProfile {
    LocalSpeakerProfile(
        id: columnText(statement, 0) ?? "",
        organizationID: columnText(statement, 1) ?? "",
        userID: columnText(statement, 2) ?? "",
        displayName: columnText(statement, 3) ?? "You",
        status: columnText(statement, 4) ?? "not_registered",
        provider: columnText(statement, 5) ?? "speechbrain-ecapa",
        sampleCount: Int(sqlite3_column_int(statement, 6)),
        model: columnText(statement, 7),
        embedding: columnText(statement, 8),
        enrolledAt: columnText(statement, 9),
        createdAt: columnText(statement, 10) ?? "",
        updatedAt: columnText(statement, 11) ?? ""
    )
}

private func publicSpeakerProfile(_ profile: LocalSpeakerProfile, user: LocalUser) -> [String: Any] {
    [
        "id": profile.id,
        "userId": profile.userID,
        "displayName": accountDisplayName(user),
        "status": profile.status,
        "provider": profile.provider,
        "sampleCount": profile.sampleCount,
        "model": profile.model ?? NSNull(),
        "enrolledAt": profile.enrolledAt ?? NSNull(),
        "createdAt": profile.createdAt,
        "updatedAt": profile.updatedAt
    ]
}

private func accountDisplayName(_ user: LocalUser) -> String {
    if let name = nonEmpty(user.name) {
        return name
    }
    let emailName = user.email.split(separator: "@").first.map(String.init) ?? ""
    return nonEmpty(emailName) ?? "You"
}

private func deviceSettings(database: OpaquePointer, deviceID: String) -> LocalDeviceSettings? {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, "select settings, hardware_info from devices where id = ? limit 1", -1, &statement, nil) == SQLITE_OK, let statement else {
        return nil
    }
    defer { sqlite3_finalize(statement) }
    bind(statement, position: 1, value: deviceID)
    guard sqlite3_step(statement) == SQLITE_ROW else {
        return nil
    }
    return LocalDeviceSettings(
        settings: jsonDictionary(columnText(statement, 0)),
        hardwareInfo: jsonDictionary(columnText(statement, 1))
    )
}

private func updateDeviceSettings(database: OpaquePointer, deviceID: String, settings: [String: Any]) -> Bool {
    guard let settingsJSON = jsonString(settings) else {
        return false
    }
    return execute(
        database,
        "update devices set settings = ?, updated_at = ? where id = ?",
        [settingsJSON, isoNow(), deviceID]
    )
}

private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private func configureSQLiteConnection(_ database: OpaquePointer) {
    sqlite3_busy_timeout(database, 2000)
    sqlite3_exec(database, "pragma journal_mode=WAL", nil, nil, nil)
    sqlite3_exec(database, "pragma synchronous=NORMAL", nil, nil, nil)
}

private func parseQuery(_ queryString: String) -> [String: String] {
    var components = URLComponents()
    components.percentEncodedQuery = queryString
    var result: [String: String] = [:]
    for item in components.queryItems ?? [] {
        result[item.name] = item.value ?? ""
    }
    return result
}

private func columnText(_ statement: OpaquePointer, _ index: Int32) -> String? {
    guard let text = sqlite3_column_text(statement, index) else {
        return nil
    }
    return String(cString: text)
}

private func nullableText(_ statement: OpaquePointer, _ index: Int32) -> Any {
    columnText(statement, index) ?? NSNull()
}

private func columnDoubleOrNull(_ statement: OpaquePointer, _ index: Int32) -> Any {
    guard sqlite3_column_type(statement, index) != SQLITE_NULL else {
        return NSNull()
    }
    return sqlite3_column_double(statement, index)
}

private func jsonValue(_ text: String?) -> Any? {
    guard let text, let data = text.data(using: .utf8) else {
        return nil
    }
    return try? JSONSerialization.jsonObject(with: data)
}

private func sessionExists(database: OpaquePointer, id: String) -> Bool {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, "select 1 from voice_sessions where id = ? limit 1", -1, &statement, nil) == SQLITE_OK, let statement else {
        return false
    }
    defer { sqlite3_finalize(statement) }
    sqlite3_bind_text(statement, 1, id, -1, SQLITE_TRANSIENT)
    return sqlite3_step(statement) == SQLITE_ROW
}

private func voiceSession(database: OpaquePointer, id: String) -> LocalVoiceSession? {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, "select id, user_id, organization_id, device_id from voice_sessions where id = ? limit 1", -1, &statement, nil) == SQLITE_OK, let statement else {
        return nil
    }
    defer { sqlite3_finalize(statement) }
    bind(statement, position: 1, value: id)
    guard sqlite3_step(statement) == SQLITE_ROW,
          let sessionID = columnText(statement, 0),
          let userID = columnText(statement, 1),
          let organizationID = columnText(statement, 2) else {
        return nil
    }
    return LocalVoiceSession(id: sessionID, userID: userID, organizationID: organizationID, deviceID: columnText(statement, 3))
}

private func queryRows(database: OpaquePointer, sql: String, values: [Any?], map: (OpaquePointer, Int) -> [String: Any]) -> [[String: Any]] {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
        return []
    }
    defer { sqlite3_finalize(statement) }
    for (index, value) in values.enumerated() {
        bind(statement, position: Int32(index + 1), value: value)
    }
    var rows: [[String: Any]] = []
    var index = 0
    while sqlite3_step(statement) == SQLITE_ROW {
        rows.append(map(statement, index))
        index += 1
    }
    return rows
}

private func memory(database: OpaquePointer, organizationID: String, userID: String, memoryID: String? = nil, normalizedContent: String? = nil) -> [String: Any]? {
    let predicate: String
    let predicateValue: String
    if let memoryID {
        predicate = "id = ?"
        predicateValue = memoryID
    } else if let normalizedContent {
        predicate = "normalized_content = ?"
        predicateValue = normalizedContent
    } else {
        return nil
    }
    let rows = queryRows(
        database: database,
        sql: """
        select id, kind, content, confidence, status, source_device_id, source_session_id, last_used_at, created_at, updated_at
        from user_memories
        where organization_id = ?
          and user_id = ?
          and \(predicate)
        limit 1
        """,
        values: [organizationID, userID, predicateValue]
    ) { statement, _ in
        publicMemory(statement)
    }
    return rows.first
}

private func listMemories(database: OpaquePointer, organizationID: String, userID: String, limit: Int) -> [[String: Any]] {
    listMemories(
        database: database,
        matching: ["organization_id = ?", "user_id = ?", "status = 'active'"],
        values: [organizationID, userID, limit]
    )
}

private func listMemories(database: OpaquePointer, matching clauses: [String], values: [Any?]) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, kind, content, confidence, status, source_device_id, source_session_id, last_used_at, created_at, updated_at
        from user_memories
        where \(clauses.joined(separator: " and "))
        order by updated_at desc
        limit ?
        """,
        values: values
    ) { statement, _ in
        publicMemory(statement)
    }
}

private func publicMemory(_ statement: OpaquePointer) -> [String: Any] {
    [
        "id": columnText(statement, 0) ?? "",
        "kind": columnText(statement, 1) ?? "fact",
        "content": columnText(statement, 2) ?? "",
        "confidence": columnText(statement, 3) ?? "medium",
        "status": columnText(statement, 4) ?? "active",
        "sourceDeviceId": nullableText(statement, 5),
        "sourceSessionId": nullableText(statement, 6),
        "lastUsedAt": nullableText(statement, 7),
        "createdAt": columnText(statement, 8) ?? "",
        "updatedAt": columnText(statement, 9) ?? ""
    ]
}

private func listPublicDevices(database: OpaquePointer, limit: Int) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, user_id, kind, product, model, name, settings, status, device_serial,
               firmware_version, hardware_info, last_seen_at, created_at, updated_at
        from devices
        order by created_at desc
        limit ?
        """,
        values: [limit]
    ) { statement, _ in
        publicDevice(statement)
    }
}

private func listPublicCodexThreads(database: OpaquePointer, limit: Int) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, organization_id, user_id, agent_id, session_id, source_device_id, codex_thread_id,
               title, summary, status, current_run_id, last_activity_at, created_at, updated_at
        from codex_threads
        where status != 'archived'
        order by last_activity_at desc, created_at desc
        limit ?
        """,
        values: [limit]
    ) { statement, _ in
        publicCodexThread(statement)
    }
}

private func publicCodexThread(_ statement: OpaquePointer) -> [String: Any] {
    [
        "id": columnText(statement, 0) ?? "",
        "organizationId": columnText(statement, 1) ?? "",
        "userId": columnText(statement, 2) ?? "",
        "agentId": columnText(statement, 3) ?? "",
        "sessionId": nullableText(statement, 4),
        "sourceDeviceId": nullableText(statement, 5),
        "codexThreadId": nullableText(statement, 6),
        "title": nullableText(statement, 7),
        "summary": nullableText(statement, 8),
        "status": columnText(statement, 9) ?? "",
        "currentRunId": nullableText(statement, 10),
        "lastActivityAt": columnText(statement, 11) ?? "",
        "createdAt": columnText(statement, 12) ?? "",
        "updatedAt": columnText(statement, 13) ?? ""
    ]
}

private func publicDevice(_ statement: OpaquePointer) -> [String: Any] {
    [
        "id": columnText(statement, 0) ?? "",
        "userId": columnText(statement, 1) ?? "",
        "kind": columnText(statement, 2) ?? "hardware",
        "product": nullableText(statement, 3),
        "model": nullableText(statement, 4),
        "name": nullableText(statement, 5),
        "settings": publicDeviceSettings(jsonDictionary(columnText(statement, 6))),
        "status": columnText(statement, 7) ?? "unknown",
        "deviceSerial": nullableText(statement, 8),
        "firmwareVersion": nullableText(statement, 9),
        "hardwareInfo": jsonValue(columnText(statement, 10)) ?? NSNull(),
        "lastSeenAt": nullableText(statement, 11),
        "createdAt": columnText(statement, 12) ?? "",
        "updatedAt": columnText(statement, 13) ?? ""
    ]
}

private func listPublicAgentRuns(database: OpaquePointer, limit: Int) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, organization_id, user_id, session_id, source_device_id, agent_id, thread_id,
               status, action, prompt, context, response_style, request, result, error,
               created_at, updated_at, started_at, completed_at
        from agent_runs
        order by created_at desc
        limit ?
        """,
        values: [limit]
    ) { statement, _ in
        publicAgentRun(statement)
    }
}

private func publicAgentRun(_ statement: OpaquePointer) -> [String: Any] {
    let request = jsonValue(columnText(statement, 12))
    return [
        "id": columnText(statement, 0) ?? "",
        "organizationId": columnText(statement, 1) ?? "",
        "userId": columnText(statement, 2) ?? "",
        "sessionId": nullableText(statement, 3),
        "sourceDeviceId": nullableText(statement, 4),
        "agentId": columnText(statement, 5) ?? "",
        "threadId": nullableText(statement, 6),
        "status": columnText(statement, 7) ?? "",
        "action": columnText(statement, 8) ?? "",
        "prompt": nullableText(statement, 9),
        "context": nullableText(statement, 10),
        "responseStyle": nullableText(statement, 11),
        "delivery": deliveryMode(from: request),
        "request": request ?? NSNull(),
        "result": jsonValue(columnText(statement, 13)) ?? NSNull(),
        "error": nullableText(statement, 14),
        "createdAt": columnText(statement, 15) ?? "",
        "updatedAt": columnText(statement, 16) ?? "",
        "startedAt": nullableText(statement, 17),
        "completedAt": nullableText(statement, 18)
    ]
}

private func listPublicAgentCompletions(database: OpaquePointer, limit: Int) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, organization_id, user_id, run_id, session_id, source_device_id, agent_id, thread_id,
               delivery, status, content, result, error, delivered_at, created_at, updated_at
        from agent_completions
        order by created_at desc
        limit ?
        """,
        values: [limit]
    ) { statement, _ in
        publicAgentCompletion(statement)
    }
}

private func listPublicAgentApprovals(database: OpaquePointer, limit: Int) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, organization_id, user_id, run_id, session_id, source_device_id, agent_id, thread_id,
               codex_request_id, codex_method, status, request, response, error, expires_at,
               created_at, updated_at, resolved_at
        from agent_approvals
        order by created_at desc
        limit ?
        """,
        values: [limit]
    ) { statement, _ in
        publicAgentApproval(statement)
    }
}

private func publicAgentApproval(_ statement: OpaquePointer) -> [String: Any] {
    [
        "id": columnText(statement, 0) ?? "",
        "organizationId": columnText(statement, 1) ?? "",
        "userId": columnText(statement, 2) ?? "",
        "runId": nullableText(statement, 3),
        "sessionId": nullableText(statement, 4),
        "sourceDeviceId": nullableText(statement, 5),
        "agentId": columnText(statement, 6) ?? "",
        "threadId": nullableText(statement, 7),
        "codexRequestId": nullableText(statement, 8),
        "codexMethod": columnText(statement, 9) ?? "",
        "status": columnText(statement, 10) ?? "",
        "request": jsonValue(columnText(statement, 11)) ?? NSNull(),
        "response": jsonValue(columnText(statement, 12)) ?? NSNull(),
        "error": nullableText(statement, 13),
        "expiresAt": nullableText(statement, 14),
        "createdAt": columnText(statement, 15) ?? "",
        "updatedAt": columnText(statement, 16) ?? "",
        "resolvedAt": nullableText(statement, 17)
    ]
}

private func deliveryMode(from request: Any?) -> String {
    (request as? [String: Any])?["delivery"] as? String ?? "auto"
}

private func publicDeviceSettings(_ settings: [String: Any]) -> [String: Any] {
    var safeSettings = settings
    let llmAPIKey = nonEmpty(safeSettings.removeValue(forKey: "llmApiKey") as? String)
    safeSettings["llmApiKeyConfigured"] = llmAPIKey != nil
    safeSettings["soundRecognition"] = soundRecognitionSettings(settings["soundRecognition"])
    return safeSettings
}

private func soundRecognitionSettings(_ value: Any?) -> [String: Any] {
    let enabled = (value as? [String: Any])?["enabled"] as? Bool ?? false
    return ["enabled": enabled, "watches": []]
}

private func transcriptSegments(database: OpaquePointer, sessionID: String, limit: Int, includeWords: Bool) -> [[String: Any]] {
    queryRows(
        database: database,
        sql: """
        select id, user_id, device_id, session_id, source, text, words, is_interim, speaker_label,
               speaker_user_id, speaker_name, speaker_confidence, emotion_label, emotion_confidence,
               emotion_model, confidence, started_at, ended_at, created_at
        from transcript_segments
        where session_id = ?
          and is_interim = 0
        order by started_at asc
        limit ?
        """,
        values: [sessionID, limit]
    ) { statement, _ in
        publicTranscriptSegment(statement, includeWords: includeWords)
    }
}

private func publicTranscriptSegment(_ statement: OpaquePointer, includeWords: Bool) -> [String: Any] {
    [
        "id": columnText(statement, 0) ?? "",
        "userId": columnText(statement, 1) ?? "",
        "deviceId": nullableText(statement, 2),
        "sessionId": columnText(statement, 3) ?? "",
        "source": columnText(statement, 4) ?? "",
        "text": columnText(statement, 5) ?? "",
        "words": includeWords ? (jsonValue(columnText(statement, 6)) ?? NSNull()) : NSNull(),
        "isInterim": sqlite3_column_int(statement, 7) != 0,
        "speakerLabel": nullableText(statement, 8),
        "speakerUserId": nullableText(statement, 9),
        "speakerName": nullableText(statement, 10),
        "speakerConfidence": columnDoubleOrNull(statement, 11),
        "emotionLabel": nullableText(statement, 12),
        "emotionConfidence": columnDoubleOrNull(statement, 13),
        "emotionModel": nullableText(statement, 14),
        "confidence": columnDoubleOrNull(statement, 15),
        "startedAt": columnText(statement, 16) ?? "",
        "endedAt": nullableText(statement, 17),
        "createdAt": columnText(statement, 18) ?? ""
    ]
}

private func agentCompletion(database: OpaquePointer, organizationID: String, sessionID: String, completionID: String) -> [String: Any]? {
    let rows = queryRows(
        database: database,
        sql: """
        select id, organization_id, user_id, run_id, session_id, source_device_id, agent_id, thread_id,
               delivery, status, content, result, error, delivered_at, created_at, updated_at
        from agent_completions
        where id = ?
          and organization_id = ?
          and session_id = ?
        limit 1
        """,
        values: [completionID, organizationID, sessionID]
    ) { statement, _ in
        publicAgentCompletion(statement)
    }
    return rows.first
}

private func publicAgentCompletion(_ statement: OpaquePointer) -> [String: Any] {
    let result = jsonValue(columnText(statement, 11))
    return [
        "id": columnText(statement, 0) ?? "",
        "organizationId": columnText(statement, 1) ?? "",
        "userId": columnText(statement, 2) ?? "",
        "runId": columnText(statement, 3) ?? "",
        "sessionId": nullableText(statement, 4),
        "sourceDeviceId": nullableText(statement, 5),
        "agentId": columnText(statement, 6) ?? "",
        "threadId": nullableText(statement, 7),
        "delivery": columnText(statement, 8) ?? "auto",
        "status": columnText(statement, 9) ?? "",
        "content": nullableText(statement, 10),
        "voice": voiceHandoff(from: result) ?? NSNull(),
        "result": result ?? NSNull(),
        "error": nullableText(statement, 12),
        "deliveredAt": nullableText(statement, 13),
        "createdAt": columnText(statement, 14) ?? "",
        "updatedAt": columnText(statement, 15) ?? ""
    ]
}

private func voiceHandoff(from result: Any?) -> Any? {
    guard let result = result as? [String: Any] else {
        return nil
    }
    if let direct = cleanVoiceHandoff(result["voiceHandoff"]) {
        return direct
    }
    if let next = result["next"] as? [String: Any] {
        return cleanVoiceHandoff(next["voiceHandoff"])
    }
    return nil
}

private func cleanVoiceHandoff(_ value: Any?) -> [String: Any]? {
    guard let source = value as? [String: Any] else {
        return nil
    }
    var handoff: [String: Any] = [:]
    for key in ["type", "outcome", "summary", "screenState", "followUp", "suggestedSpoken"] {
        if let text = nonEmpty(source[key] as? String) {
            handoff[key] = text
        }
    }
    if let needsUserAction = source["needsUserAction"] as? Bool {
        handoff["needsUserAction"] = needsUserAction
    }
    if let details = source["details"] as? [String] {
        let filtered = details.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.prefix(6)
        if !filtered.isEmpty {
            handoff["details"] = Array(filtered)
        }
    }
    return handoff.isEmpty ? nil : handoff
}

@discardableResult
private func execute(_ database: OpaquePointer, _ sql: String, _ values: [Any?]) -> Bool {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
        return false
    }
    defer { sqlite3_finalize(statement) }
    for (index, value) in values.enumerated() {
        bind(statement, position: Int32(index + 1), value: value)
    }
    return sqlite3_step(statement) == SQLITE_DONE
}

private func bind(_ statement: OpaquePointer, position: Int32, value: Any?) {
    switch value {
    case let string as String:
        sqlite3_bind_text(statement, position, string, -1, SQLITE_TRANSIENT)
    case let int as Int:
        sqlite3_bind_int(statement, position, Int32(int))
    case let int64 as Int64:
        sqlite3_bind_int64(statement, position, int64)
    case let double as Double:
        sqlite3_bind_double(statement, position, double)
    case let bool as Bool:
        sqlite3_bind_int(statement, position, bool ? 1 : 0)
    default:
        sqlite3_bind_null(statement, position)
    }
}

private func clampInt(_ value: String?, defaultValue: Int, range: ClosedRange<Int>) -> Int {
    guard let value, let parsed = Int(value) else {
        return defaultValue
    }
    return min(max(parsed, range.lowerBound), range.upperBound)
}

private func numericInt(_ value: Any?) -> Int? {
    switch value {
    case is Bool:
        return nil
    case let int as Int:
        return int
    case let double as Double where double.isFinite:
        return Int(double.rounded())
    case let number as NSNumber:
        return Int(round(number.doubleValue))
    default:
        return nil
    }
}

private func currentSpeakerVolume(settings: [String: Any], hardwareInfo: [String: Any]) -> Int {
    if let value = numericInt(settings["speakerVolume"]) {
        return min(max(value, 0), 100)
    }
    if let value = numericInt(hardwareInfo["speakerVolume"]) {
        return min(max(value, 0), 100)
    }
    return 50
}

private func nextSpeakerVolume(currentVolume: Int, action: String, volume: Int?) -> Int {
    let step = volume ?? 15
    let next: Int
    switch action {
    case "set":
        next = volume ?? currentVolume
    case "increase":
        next = currentVolume + step
    case "decrease":
        next = currentVolume - step
    case "mute":
        next = 0
    default:
        next = volume ?? 50
    }
    return min(max(next, 0), 100)
}

private func nonEmpty(_ value: String?) -> String? {
    guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
        return nil
    }
    return trimmed
}

private func normalizedText(_ value: String?) -> String? {
    guard let value else {
        return nil
    }
    let normalized = value
        .split(whereSeparator: \.isWhitespace)
        .joined(separator: " ")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return normalized.isEmpty ? nil : normalized
}

private func normalizeMemoryContent(_ value: String) -> String {
    normalizedText(value)?.lowercased() ?? ""
}

private func enumValue(_ value: String?, defaultValue: String?, allowed: Set<String>) -> String? {
    let resolved = value ?? defaultValue
    guard let resolved, allowed.contains(resolved) else {
        return nil
    }
    return resolved
}

private func escapeLike(_ value: String) -> String {
    value
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "%", with: "\\%")
        .replacingOccurrences(of: "_", with: "\\_")
}

private func databaseError(_ database: OpaquePointer) -> String {
    guard let message = sqlite3_errmsg(database) else {
        return "unknown sqlite error"
    }
    return String(cString: message)
}

private func signedVoiceToken(sessionID: String, device: LocalDevice, sampleRate: Int, channels: Int, initialAwake: Bool, expiresAt: Date) -> String {
    let payload: [String: Any] = [
        "sessionId": sessionID,
        "deviceId": device.id,
        "userId": device.userID,
        "organizationId": device.organizationID,
        "source": "device",
        "sampleRate": sampleRate,
        "channels": channels,
        "initialAwake": initialAwake,
        "iat": Int(Date().timeIntervalSince1970),
        "exp": Int(expiresAt.timeIntervalSince1970)
    ]
    let payloadData = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data("{}".utf8)
    let encoded = base64URL(payloadData)
    let key = SymmetricKey(data: Data("iris-development-token-secret".utf8))
    let signature = HMAC<SHA256>.authenticationCode(for: Data(encoded.utf8), using: key)
    return "\(encoded).\(base64URL(Data(signature)))"
}

private func base64URL(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

private func isoNow() -> String {
    isoDate(Date())
}

private func isoDate(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}

private extension String {
    func pathComponent(after prefix: String, before suffix: String) -> String? {
        guard hasPrefix(prefix), hasSuffix(suffix) else { return nil }
        return String(dropFirst(prefix.count).dropLast(suffix.count))
            .removingPercentEncoding
    }

    func voiceSessionMemoryPath() -> (sessionID: String, memoryID: String)? {
        let prefix = "/v1/voice/sessions/"
        guard hasPrefix(prefix) else { return nil }
        let remainder = String(dropFirst(prefix.count))
        let marker = "/memories/"
        guard let markerRange = remainder.range(of: marker) else { return nil }
        let sessionID = String(remainder[..<markerRange.lowerBound]).removingPercentEncoding
        let memoryID = String(remainder[markerRange.upperBound...]).removingPercentEncoding
        guard let sessionID, let memoryID, !sessionID.isEmpty, !memoryID.isEmpty else {
            return nil
        }
        return (sessionID, memoryID)
    }

    func voiceSessionCompletionDeliveredPath() -> (sessionID: String, completionID: String)? {
        let prefix = "/v1/voice/sessions/"
        guard hasPrefix(prefix), hasSuffix("/delivered") else { return nil }
        let remainder = String(dropFirst(prefix.count).dropLast("/delivered".count))
        let marker = "/agent/completions/"
        guard let markerRange = remainder.range(of: marker) else { return nil }
        let sessionID = String(remainder[..<markerRange.lowerBound]).removingPercentEncoding
        let completionID = String(remainder[markerRange.upperBound...]).removingPercentEncoding
        guard let sessionID, let completionID, !sessionID.isEmpty, !completionID.isEmpty else {
            return nil
        }
        return (sessionID, completionID)
    }
}
