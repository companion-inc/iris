import Foundation
import Network

final class NativeHTTPServer: @unchecked Sendable {
    private let port: NWEndpoint.Port
    private let host: NWEndpoint.Host
    private let queue: DispatchQueue
    private let handler: @Sendable (Data) async -> Data
    private var listener: NWListener?

    init(port: UInt16, label: String, handler: @escaping @Sendable (Data) async -> Data) {
        self.port = NWEndpoint.Port(rawValue: port)!
        self.host = .ipv4(IPv4Address("127.0.0.1")!)
        self.queue = DispatchQueue(label: label)
        self.handler = handler
    }

    func start() throws {
        guard listener == nil else {
            return
        }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: host, port: port)
        let listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            self?.handle(connection)
        }
        listener.start(queue: queue)
        self.listener = listener
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    private func handle(_ connection: NWConnection) {
        connection.start(queue: queue)
        receive(connection: connection, buffer: Data())
    }

    private func receive(connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1_048_576) { [weak self] data, _, isComplete, error in
            guard let self else {
                connection.cancel()
                return
            }
            var nextBuffer = buffer
            if let data {
                nextBuffer.append(data)
            }
            if let error {
                self.send(http500(error), on: connection)
                return
            }
            if requestIsComplete(nextBuffer) || isComplete {
                Task {
                    let response = await self.handler(nextBuffer)
                    self.send(response, on: connection)
                }
                return
            }
            self.receive(connection: connection, buffer: nextBuffer)
        }
    }

    private func send(_ data: Data, on connection: NWConnection) {
        connection.send(content: data, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }
}

private func requestIsComplete(_ data: Data) -> Bool {
    guard let separatorRange = data.range(of: Data("\r\n\r\n".utf8)) else {
        return false
    }
    let headerData = data[..<separatorRange.lowerBound]
    guard let headerText = String(data: headerData, encoding: .utf8) else {
        return true
    }
    let contentLength = headerText
        .split(separator: "\r\n")
        .dropFirst()
        .compactMap { line -> Int? in
            let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, parts[0].trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "content-length" else {
                return nil
            }
            return Int(parts[1].trimmingCharacters(in: .whitespacesAndNewlines))
        }
        .first ?? 0
    return data.count >= separatorRange.upperBound + contentLength
}

private func http500(_ error: Error) -> Data {
    let payload = ["ok": false, "error": error.localizedDescription] as [String: Any]
    let body = (try? JSONSerialization.data(withJSONObject: payload, options: [])) ?? Data("{}".utf8)
    var response = Data("HTTP/1.1 500 Error\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
    response.append(body)
    return response
}
