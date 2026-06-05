#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="${IRIS_NATIVE_APP_NAME:-Iris}"
APP_ID="${IRIS_NATIVE_APP_ID:-ai.companion.iris}"
APP_DIR="${IRIS_NATIVE_APP_DIR:-$HOME/Applications}"
DEST_APP="$APP_DIR/$APP_NAME.app"
PLIST="$DEST_APP/Contents/Info.plist"
EXECUTABLE="$DEST_APP/Contents/MacOS/$APP_NAME"
PLIST_BUDDY="/usr/libexec/PlistBuddy"
REPO_ROOT_FILE="$DEST_APP/Contents/Resources/repo-root.txt"

[[ -x "$EXECUTABLE" ]] || {
  echo "Missing executable: $EXECUTABLE" >&2
  exit 1
}

[[ -f "$PLIST" ]] || {
  echo "Missing Info.plist: $PLIST" >&2
  exit 1
}

[[ "$("$PLIST_BUDDY" -c 'Print :CFBundleExecutable' "$PLIST")" == "$APP_NAME" ]]
[[ "$("$PLIST_BUDDY" -c 'Print :CFBundleIdentifier' "$PLIST")" == "$APP_ID" ]]
[[ "$("$PLIST_BUDDY" -c 'Print :CFBundlePackageType' "$PLIST")" == "APPL" ]]
"$PLIST_BUDDY" -c 'Print :NSMicrophoneUsageDescription' "$PLIST" >/dev/null
"$PLIST_BUDDY" -c 'Print :NSCameraUsageDescription' "$PLIST" >/dev/null
[[ -f "$REPO_ROOT_FILE" ]] || {
  echo "Missing repo root marker: $REPO_ROOT_FILE" >&2
  exit 1
}
[[ -f "$(tr -d '\n' < "$REPO_ROOT_FILE")/package.json" ]] || {
  echo "Repo root marker does not point at Iris repo" >&2
  exit 1
}

codesign --verify --deep --strict "$DEST_APP" >/dev/null
codesign -d --entitlements :- "$DEST_APP" 2>/dev/null | grep -q 'com.apple.security.device.camera'
echo "Iris bundle verified: $DEST_APP"
