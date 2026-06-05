import AVFoundation
import Foundation

struct CameraPermissionSnapshot: Sendable {
    var granted: Bool
    var status: String

    var jsonObject: [String: Any] {
        [
            "granted": granted,
            "status": status
        ]
    }
}

@MainActor
enum CameraPermission {
    static var status: AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .video)
    }

    static var isGranted: Bool {
        status == .authorized
    }

    static var isNotDetermined: Bool {
        status == .notDetermined
    }

    static var statusDescription: String {
        description(for: status)
    }

    static func request() async -> Bool {
        await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: .video) { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    static func requestIfNeeded() async -> Bool {
        if isGranted {
            return true
        }
        if isNotDetermined {
            return await request()
        }
        return false
    }

    static func snapshot() -> CameraPermissionSnapshot {
        CameraPermissionSnapshot(granted: isGranted, status: statusDescription)
    }

    nonisolated static func description(for status: AVAuthorizationStatus) -> String {
        switch status {
        case .authorized:
            return "Allowed"
        case .notDetermined:
            return "Not requested"
        case .denied:
            return "Denied"
        case .restricted:
            return "Restricted"
        @unknown default:
            return "Unknown"
        }
    }
}
