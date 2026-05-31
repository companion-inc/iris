#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/iris-device}"
SERVICE_USER="${SERVICE_USER:-pi}"
SERVICE_GROUP="${SERVICE_GROUP:-audio}"
MANIFEST_URL="${IRIS_DEVICE_UPDATE_MANIFEST_URL:-}"
FORCE="${IRIS_DEVICE_UPDATE_FORCE:-0}"
SKIP_SELF_RESTART="${IRIS_DEVICE_SKIP_SELF_RESTART:-0}"
RELEASES_DIR="$INSTALL_DIR/releases"
CURRENT_LINK="$INSTALL_DIR/current"

if [ -z "${IRIS_API_URL:-}" ]; then
  IRIS_API_URL="http://127.0.0.1:4747"
fi
export IRIS_API_URL

if [ -z "$MANIFEST_URL" ]; then
  echo "IRIS_DEVICE_UPDATE_MANIFEST_URL is required for local file-based updates."
  exit 2
fi

LOCK_FILE="/tmp/iris-device-update.lock"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  flock -n 9 || {
    echo "Another Iris device update is already running."
    exit 0
  }
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command curl
require_command tar
require_command python3
require_command rsync
require_command /usr/local/bin/uv

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

ARCHIVE="$TMP_DIR/iris-device.tar.gz"
MANIFEST="$TMP_DIR/latest.json"
EXTRACT_DIR="$TMP_DIR/extract"
SOURCE_DIR="$TMP_DIR/iris-device"
mkdir -p "$EXTRACT_DIR" "$SOURCE_DIR"

echo "Checking Iris device update manifest: ${MANIFEST_URL}"
curl -fsSL "$MANIFEST_URL" -o "$MANIFEST"

read_manifest_field() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as file:
    value = json.load(file).get(sys.argv[2], "")
print(value)
PY
}

VERSION="$(read_manifest_field version)"
URL="$(read_manifest_field url)"
SHA256="$(read_manifest_field sha256)"

if [ -z "$VERSION" ] || [ -z "$URL" ] || [ -z "$SHA256" ]; then
  echo "Update manifest must include version, url, and sha256." >&2
  exit 1
fi

CURRENT_VERSION=""
if [ -f "$CURRENT_LINK/.version" ]; then
  CURRENT_VERSION="$(cat "$CURRENT_LINK/.version")"
elif [ -f "$INSTALL_DIR/.version" ]; then
  CURRENT_VERSION="$(cat "$INSTALL_DIR/.version")"
fi

if [ "$FORCE" != "1" ] && [ "$CURRENT_VERSION" = "$VERSION" ]; then
  echo "Iris device is already on version ${VERSION}."
  exit 0
fi

echo "Downloading Iris device update ${VERSION}..."
curl -fsSL "$URL" -o "$ARCHIVE"

verify_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum -c -
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    printf '%s  %s\n' "$SHA256" "$ARCHIVE" | shasum -a 256 -c -
    return
  fi
  echo "Missing sha256sum or shasum." >&2
  exit 1
}

verify_sha256

tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"

if [ -d "$EXTRACT_DIR/apps/iris-device" ]; then
  rsync -a "$EXTRACT_DIR/apps/iris-device/" "$SOURCE_DIR/"
elif [ -d "$EXTRACT_DIR/apps/iris-device-device" ]; then
  rsync -a "$EXTRACT_DIR/apps/iris-device-device/" "$SOURCE_DIR/"
elif [ -f "$EXTRACT_DIR/device_client.py" ] && [ -f "$EXTRACT_DIR/requirements.txt" ]; then
  rsync -a "$EXTRACT_DIR/" "$SOURCE_DIR/"
else
  echo "Update archive did not contain Iris device files." >&2
  exit 1
fi

RELEASE_DIR="$RELEASES_DIR/$VERSION"
PREVIOUS_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
  PREVIOUS_TARGET="$(readlink "$CURRENT_LINK")"
fi

sudo mkdir -p "$RELEASE_DIR"
sudo rsync -a --delete --exclude "__pycache__" "$SOURCE_DIR/" "$RELEASE_DIR/"
printf '%s\n' "$VERSION" | sudo tee "$RELEASE_DIR/.version" >/dev/null
sudo chmod +x "$RELEASE_DIR"/scripts/*.sh
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASES_DIR"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  sudo cp "$RELEASE_DIR/.env.example" "$INSTALL_DIR/.env"
  sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.env"
fi
sudo IRIS_API_URL="$IRIS_API_URL" python3 - "$INSTALL_DIR/.env" <<'PY'
from pathlib import Path
import os
import sys

env = Path(sys.argv[1])
api_url = os.environ["IRIS_API_URL"].rstrip("/")
lines = env.read_text().splitlines()
next_lines = []
seen_api_url = False
seen_keys = set()
required_defaults = {
    "IRIS_DEVICE_ALSA_DEVICE": "plughw:CARD=Array,DEV=0",
    "IRIS_DEVICE_PLAYBACK_DEVICE": "plughw:CARD=Array,DEV=0",
    "IRIS_DEVICE_CAPTURE_CHANNEL_INDEX": "0",
    "IRIS_DEVICE_PLAYBACK_CAPTURE_CHANNEL_INDEX": "1",
    "IRIS_DEVICE_ALSA_BUFFER_TIME_US": "500000",
    "IRIS_DEVICE_ALSA_PERIOD_TIME_US": "100000",
    "IRIS_DEVICE_ALSA_CAPTURE_VOLUME": "",
    "IRIS_DEVICE_PLAYBACK_IDLE_STOP_SECONDS": "6.0",
}
legacy_migrations = {
    "IRIS_DEVICE_ALSA_DEVICE": {"plughw:2,0": "plughw:CARD=Array,DEV=0"},
    "IRIS_DEVICE_PLAYBACK_DEVICE": {"plughw:2,0": "plughw:CARD=Array,DEV=0"},
    "IRIS_DEVICE_CAPTURE_CHANNEL_INDEX": {"1": "0"},
}
removed_keys = {
    "IRIS_DEVICE_WAKE_WORD_MODEL",
    "IRIS_DEVICE_WAKE_WORD_THRESHOLD",
    "IRIS_DEVICE_WAKE_WORD_DEBOUNCE_SECONDS",
    "IRIS_DEVICE_WAKE_WORD_INTERVAL_SECONDS",
    "IRIS_DEVICE_WAKE_WORD_SCORE_LOG_INTERVAL_SECONDS",
}
for line in lines:
    if line.startswith("IRIS_API_URL="):
        next_lines.append(f"IRIS_API_URL={api_url}")
        seen_api_url = True
    elif "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        if key in removed_keys:
            continue
        replacement = legacy_migrations.get(key, {}).get(value)
        next_lines.append(f"{key}={replacement}" if replacement is not None else line)
    else:
        next_lines.append(line)
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0]
        if key not in removed_keys:
            seen_keys.add(key)
if not seen_api_url:
    next_lines.insert(0, f"IRIS_API_URL={api_url}")
for key, value in required_defaults.items():
    if key not in seen_keys:
        next_lines.append(f"{key}={value}")
env.write_text("\n".join(next_lines) + "\n")
PY
sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.env"
sudo chmod 0600 "$INSTALL_DIR/.env"
for identity_file in "$INSTALL_DIR/.device-token" "$INSTALL_DIR/.device-id"; do
  if [ -f "$identity_file" ]; then
    sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$identity_file"
    sudo chmod 0600 "$identity_file"
  fi
done

sudo rm -rf "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" /usr/local/bin/uv venv --python python3 "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" /usr/local/bin/uv pip install --python "$INSTALL_DIR/.venv/bin/python" -r "$RELEASE_DIR/requirements.txt"
printf '%s\n' "$VERSION" | sudo tee "$INSTALL_DIR/.version" >/dev/null
sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.version" "$INSTALL_DIR/.venv"

sudo ln -sfn "releases/$VERSION" "$CURRENT_LINK"
sudo chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT_LINK"

SERVICE_GROUP="$SERVICE_GROUP" bash "$RELEASE_DIR/scripts/install-xvf-udev.sh"
sudo install -m 0644 "$RELEASE_DIR/systemd/iris-device.service.example" /etc/systemd/system/iris-device.service
sudo install -m 0644 "$RELEASE_DIR/systemd/iris-device-setup.service.example" /etc/systemd/system/iris-device-setup.service
for removed_service in iris-device-ui iris-device-console iris-device-touch-ui iris-device-kiosk; do
  sudo systemctl disable --now "$removed_service" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/${removed_service}.service"
done

sudo systemctl daemon-reload

rollback() {
  if [ -n "$PREVIOUS_TARGET" ]; then
    echo "Rolling back Iris device to ${PREVIOUS_TARGET}..." >&2
    sudo ln -sfn "$PREVIOUS_TARGET" "$CURRENT_LINK"
    sudo chown -h "$SERVICE_USER:$SERVICE_GROUP" "$CURRENT_LINK"
    sudo systemctl daemon-reload
    sudo systemctl restart iris-device-setup iris-device >/dev/null 2>&1 || true
  fi
}

if systemctl is-enabled iris-device-setup >/dev/null 2>&1; then
  sudo systemctl restart iris-device-setup || {
    rollback
    exit 1
  }
fi
if systemctl is-enabled iris-device >/dev/null 2>&1; then
  if [ "$SKIP_SELF_RESTART" = "1" ]; then
    echo "iris-device restart deferred to the job runner."
  else
    sudo systemctl restart iris-device || {
      rollback
      exit 1
    }
  fi
fi
echo "Updated Iris device code in ${CURRENT_LINK} to ${VERSION}."
