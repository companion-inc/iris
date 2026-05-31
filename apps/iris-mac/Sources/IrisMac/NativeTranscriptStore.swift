import Foundation
import SQLite3

actor NativeTranscriptStore {
    private let databaseURL: URL

    init(repoRoot: URL = ProcessSupervisor.resolveRepoRoot()) {
        self.databaseURL = repoRoot.appending(path: "apps/iris-api/.iris/iris.sqlite")
    }

    func isAvailable() -> Bool {
        FileManager.default.fileExists(atPath: databaseURL.path)
    }

    func transcripts(limit: Int = 40) async -> [TranscriptSegment] {
        guard isAvailable(), let database = openDatabase(readOnly: true) else { return [] }
        defer { sqlite3_close(database) }

        let sql = """
        select id, text, started_at, speaker_name, emotion_label
        from transcript_segments
        where is_interim = 0
        order by started_at desc
        limit ?
        """
        return rows(database: database, sql: sql, limit: limit) { statement in
            TranscriptSegment(
                id: columnText(statement, 0) ?? UUID().uuidString,
                text: columnText(statement, 1) ?? "",
                startedAt: parseDate(columnText(statement, 2)),
                speakerName: columnText(statement, 3),
                emotionLabel: columnText(statement, 4)
            )
        }
    }

    func voiceSessions(limit: Int = 20, segmentLimit: Int = 12) async -> [VoiceSession] {
        guard isAvailable(), let database = openDatabase(readOnly: true) else { return [] }
        defer { sqlite3_close(database) }

        let sessionSQL = """
        select id, status, started_at
        from voice_sessions
        where status = 'active'
           or exists (
                select 1
                from transcript_segments ts
                where ts.session_id = voice_sessions.id
                  and ts.is_interim = 0
           )
        order by started_at desc
        limit ?
        """
        let sessions = rows(database: database, sql: sessionSQL, limit: limit) { statement in
            (
                id: columnText(statement, 0) ?? UUID().uuidString,
                status: columnText(statement, 1) ?? "unknown",
                startedAt: parseDate(columnText(statement, 2))
            )
        }

        var result: [VoiceSession] = []
        for session in sessions {
            result.append(VoiceSession(
                id: session.id,
                status: session.status,
                startedAt: session.startedAt,
                segments: segments(database: database, sessionID: session.id, limit: segmentLimit)
            ))
        }
        return result
    }

    private func segments(database: OpaquePointer, sessionID: String, limit: Int) -> [TranscriptSegment] {
        let sql = """
        select id, text, started_at, speaker_name, emotion_label
        from transcript_segments
        where session_id = ?
          and is_interim = 0
        order by started_at asc
        limit ?
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            return []
        }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_text(statement, 1, sessionID, -1, SQLITE_TRANSIENT)
        sqlite3_bind_int(statement, 2, Int32(max(1, limit)))

        var result: [TranscriptSegment] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            result.append(
                TranscriptSegment(
                    id: columnText(statement, 0) ?? UUID().uuidString,
                    text: columnText(statement, 1) ?? "",
                    startedAt: parseDate(columnText(statement, 2)),
                    speakerName: columnText(statement, 3),
                    emotionLabel: columnText(statement, 4)
                )
            )
        }
        return result
    }

    private func rows<T>(database: OpaquePointer, sql: String, limit: Int, map: (OpaquePointer) -> T) -> [T] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            return []
        }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_int(statement, 1, Int32(max(1, limit)))

        var result: [T] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            result.append(map(statement))
        }
        return result
    }

    private func openDatabase(readOnly: Bool) -> OpaquePointer? {
        var database: OpaquePointer?
        let flags = readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE
        guard sqlite3_open_v2(databaseURL.path, &database, flags, nil) == SQLITE_OK else {
            if let database {
                sqlite3_close(database)
            }
            return nil
        }
        return database
    }
}

private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private func columnText(_ statement: OpaquePointer, _ index: Int32) -> String? {
    guard let text = sqlite3_column_text(statement, index) else {
        return nil
    }
    return String(cString: text)
}

private func parseDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) {
        return date
    }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}
