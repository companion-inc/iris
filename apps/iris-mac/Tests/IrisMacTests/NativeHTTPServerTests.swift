import Foundation
@testable import IrisMac
import XCTest

final class NativeHTTPServerTests: XCTestCase {
    func testServesHTTPResponseOverNetworkFrameworkListener() async throws {
        let port = UInt16(47_997)
        let server = NativeHTTPServer(port: port, label: "iris.native.http.test") { data in
            let requestText = String(decoding: data, as: UTF8.self)
            let body = Data("{\"ok\":true,\"sawHealth\":\(requestText.contains("GET /health "))}".utf8)
            var response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
            response.append(body)
            return response
        }
        try server.start()
        defer { server.stop() }

        let url = URL(string: "http://127.0.0.1:\(port)/health")!
        let (data, response) = try await fetchWithRetry(url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(object?["ok"] as? Bool, true)
        XCTAssertEqual(object?["sawHealth"] as? Bool, true)
    }

    private func fetchWithRetry(_ url: URL) async throws -> (Data, URLResponse) {
        var lastError: Error?
        for _ in 0..<10 {
            do {
                return try await URLSession.shared.data(from: url)
            } catch {
                lastError = error
                try await Task.sleep(for: .milliseconds(100))
            }
        }
        throw lastError ?? URLError(.cannotConnectToHost)
    }
}
