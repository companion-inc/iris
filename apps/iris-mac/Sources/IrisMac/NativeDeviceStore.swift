import Foundation
import SQLite3

actor NativeDeviceStore {
    private let databaseURL: URL

    init(repoRoot: URL = ProcessSupervisor.resolveRepoRoot()) {
        self.databaseURL = repoRoot.appending(path: "apps/iris-api/.iris/iris.sqlite")
    }

    func devices(limit: Int = 100) async -> [IrisDevice] {
        guard FileManager.default.fileExists(atPath: databaseURL.path),
              let database = openDatabase(readOnly: true) else {
            return []
        }
        defer { sqlite3_close(database) }

        let sql = """
        select id, kind, product, model, name, status, device_serial, firmware_version, last_seen_at
        from devices
        order by created_at desc
        limit ?
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            return []
        }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_int(statement, 1, Int32(max(1, limit)))

        var result: [IrisDevice] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            result.append(
                IrisDevice(
                    id: columnText(statement, 0) ?? UUID().uuidString,
                    kind: columnText(statement, 1) ?? "hardware",
                    product: columnText(statement, 2),
                    model: columnText(statement, 3),
                    name: columnText(statement, 4),
                    status: columnText(statement, 5) ?? "unknown",
                    deviceSerial: columnText(statement, 6),
                    firmwareVersion: columnText(statement, 7),
                    lastSeenAt: parseDate(columnText(statement, 8))
                )
            )
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
