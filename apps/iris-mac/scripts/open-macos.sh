#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PACKAGE_DIR/../.." && pwd)"
APP_NAME="Iris"
APP_ID="ai.companion.iris"
APP_DIR="${IRIS_NATIVE_APP_DIR:-$HOME/Applications}"
DEST_APP="$APP_DIR/$APP_NAME.app"
EXECUTABLE="$DEST_APP/Contents/MacOS/$APP_NAME"
LEGACY_NATIVE_APP="$APP_DIR/Iris Native.app"
PLIST_TEMPLATE="$PACKAGE_DIR/Resources/Info.plist"
ENTITLEMENTS="$PACKAGE_DIR/Resources/Iris.entitlements"
ICON_FILE="$PACKAGE_DIR/Resources/Iris.icns"
SIGN_IDENTITY="${IRIS_CODESIGN_IDENTITY:-Developer ID Application: Companion, Inc. (5LYD7HDS6X)}"
SWIFT_BUILD_DIR="${IRIS_SWIFT_BUILD_DIR:-/private/tmp/iris-mac-build}"
export CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-/private/tmp/iris-clang-cache}"
export SWIFT_MODULE_CACHE_PATH="${SWIFT_MODULE_CACHE_PATH:-/private/tmp/iris-swift-module-cache}"

cd "$PACKAGE_DIR"
xcrun swift build --disable-sandbox --scratch-path "$SWIFT_BUILD_DIR"

pkill -f "$EXECUTABLE" >/dev/null 2>&1 || true
pkill -f "$LEGACY_NATIVE_APP/Contents/MacOS/Iris Native" >/dev/null 2>&1 || true
rm -rf "$DEST_APP" "$LEGACY_NATIVE_APP"
mkdir -p "$DEST_APP/Contents/MacOS" "$DEST_APP/Contents/Resources"
cp "$SWIFT_BUILD_DIR/debug/Iris" "$EXECUTABLE"
chmod +x "$EXECUTABLE"
if [[ -f "$ICON_FILE" ]]; then
  cp "$ICON_FILE" "$DEST_APP/Contents/Resources/Iris.icns"
fi
rm -f "$DEST_APP/Contents/Resources/repo"
printf '%s\n' "$REPO_ROOT" > "$DEST_APP/Contents/Resources/repo-root.txt"

sed \
  -e "s/__IRIS_APP_NAME__/$APP_NAME/g" \
  -e "s/__IRIS_APP_ID__/$APP_ID/g" \
  "$PLIST_TEMPLATE" > "$DEST_APP/Contents/Info.plist"

if ! codesign --force --deep --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$DEST_APP" >/dev/null 2>&1; then
  codesign --force --deep --entitlements "$ENTITLEMENTS" --sign - "$DEST_APP" >/dev/null 2>&1 || true
fi

touch "$DEST_APP"
kill_matching() {
  local pattern="$1"
  local pid
  for pid in $(pgrep -f "$pattern" 2>/dev/null); do
    [[ "$pid" == "$$" ]] && continue
    kill "$pid" >/dev/null 2>&1 || true
  done
  sleep 0.2
  for pid in $(pgrep -f "$pattern" 2>/dev/null); do
    [[ "$pid" == "$$" ]] && continue
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
}

kill_matching "pnpm voice:dev"
kill_matching "uv run iris-voice"
kill_matching "uv run iris-speaker-id"
kill_matching "apps/iris-voice/.venv/bin/iris-voice"
kill_matching "apps/iris-speaker-id/.venv/bin/iris-speaker-id"
kill_matching "apps/iris-api/node_modules/.bin/../tsx/dist/cli.mjs src/server.ts"
for port in 4747 4748 4749 4750; do
  for pid in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
    kill "$pid" >/dev/null 2>&1 || true
  done
done
sleep 0.2
for port in 4747 4748 4749 4750; do
  for pid in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
done
open -n "$DEST_APP"
