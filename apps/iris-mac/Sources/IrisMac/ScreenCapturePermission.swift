import CoreGraphics
import AppKit
import Foundation

enum ScreenCapturePermission {
    static var isGranted: Bool {
        CGPreflightScreenCaptureAccess()
    }

    static var statusDescription: String {
        isGranted ? "Allowed" : "Not allowed"
    }

    static func request() -> Bool {
        CGRequestScreenCaptureAccess()
    }

    static func requestIfNeeded() -> Bool {
        if isGranted {
            return true
        }
        return request()
    }

    static func snapshot() -> [String: Any] {
        [
            "granted": isGranted,
            "status": statusDescription
        ]
    }

    static func captureMainDisplayJPEG(maxDimension: Int = 1600) throws -> Data {
        guard let image = CGDisplayCreateImage(CGMainDisplayID()) else {
            throw ScreenCaptureError.captureFailed
        }
        let scaledImage = try scaled(image: image, maxDimension: maxDimension)
        let rep = NSBitmapImageRep(cgImage: scaledImage)
        guard let data = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.82]) else {
            throw ScreenCaptureError.encodingFailed
        }
        return data
    }

    private static func scaled(image: CGImage, maxDimension: Int) throws -> CGImage {
        let width = image.width
        let height = image.height
        guard width > 0, height > 0 else {
            throw ScreenCaptureError.captureFailed
        }
        let longest = max(width, height)
        guard maxDimension > 0, longest > maxDimension else {
            return image
        }
        let scale = Double(maxDimension) / Double(longest)
        let scaledWidth = max(1, Int(Double(width) * scale))
        let scaledHeight = max(1, Int(Double(height) * scale))
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: nil,
            width: scaledWidth,
            height: scaledHeight,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else {
            throw ScreenCaptureError.encodingFailed
        }
        context.interpolationQuality = .medium
        context.draw(image, in: CGRect(x: 0, y: 0, width: scaledWidth, height: scaledHeight))
        guard let scaled = context.makeImage() else {
            throw ScreenCaptureError.encodingFailed
        }
        return scaled
    }
}

enum ScreenCaptureError: LocalizedError {
    case captureFailed
    case encodingFailed

    var errorDescription: String? {
        switch self {
        case .captureFailed:
            return "Could not capture the main display"
        case .encodingFailed:
            return "Could not encode the screen capture"
        }
    }
}
