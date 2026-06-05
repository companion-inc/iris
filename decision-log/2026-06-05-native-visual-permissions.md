# Native Visual Permissions

## Finding

The camera vision tool captured through `ffmpeg`, but the Iris bundle only declared microphone usage. Without `NSCameraUsageDescription`, camera entitlement, and a native AVFoundation permission request from the app, macOS cannot show a clean Iris camera permission dialog.

Screen capture has a separate macOS permission model. It uses CoreGraphics screen-capture preflight/request APIs and appears in System Settings under Screen & System Audio Recording.

## Decision

Add native `CameraPermission` and `ScreenCapturePermission` helpers in the macOS app. Expose local debug endpoints that the voice sidecar can call before visual capture:

- `POST /debug/permissions/camera/request`
- `POST /debug/permissions/screen-capture/request`
- `POST /debug/vision/screen-jpeg`

Add `NSCameraUsageDescription` and `com.apple.security.device.camera` to the bundle. Keep screen capture entitlement-free and request it through CoreGraphics.

Capture screen pixels in native Iris through `CGDisplayCreateImage`, scale and encode to JPEG, then return base64 JSON to the voice sidecar. Do not shell out to `/usr/sbin/screencapture` for `screen_vision`, because that can make macOS attribute capture permission to the wrong process.

If macOS does not grant a requested permission, the native endpoint opens the corresponding Privacy & Security pane so the user can toggle Iris directly.

## Verification

The bundle verifier checks for camera usage description and camera entitlement. Python visual tools now call the native permission endpoints before capturing pixels.

After removing stale Electron/system Iris copies, adding the native app to System Settings, and relaunching, camera permission and screen capture permission were granted for the native Iris app. Native screen JPEG capture and Gemini inline-data conversion both passed.
