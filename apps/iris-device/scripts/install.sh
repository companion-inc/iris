#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/iris-device}"
SERVICE_USER="${SERVICE_USER:-pi}"
SERVICE_GROUP="${SERVICE_GROUP:-audio}"
RELEASE_NAME="${IRIS_DEVICE_LOCAL_RELEASE:-local-$(date -u +%Y%m%d%H%M%S)}"

if [ -z "${IRIS_API_URL:-}" ]; then
  IRIS_API_URL="http://127.0.0.1:4747"
fi
export IRIS_API_URL

cd "$(dirname "$0")/.."

sudo apt update
sudo apt install -y curl tar rsync python3 alsa-utils bluetooth bluez network-manager rfkill
sudo ./scripts/uv-ready.sh
SERVICE_GROUP="$SERVICE_GROUP" bash ./scripts/install-xvf-udev.sh

RELEASE_DIR="$INSTALL_DIR/releases/$RELEASE_NAME"

sudo mkdir -p "$RELEASE_DIR"
sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
sudo rsync -a \
  --exclude "__pycache__" \
  ./ "$RELEASE_DIR/"

printf '%s\n' "$RELEASE_NAME" | sudo tee "$RELEASE_DIR/.version" >/dev/null
sudo chmod +x "$RELEASE_DIR"/scripts/*.sh
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/releases"

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
printf '%s\n' "$RELEASE_NAME" | sudo tee "$INSTALL_DIR/.version" >/dev/null
sudo chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/.version" "$INSTALL_DIR/.venv"
sudo ln -sfn "releases/$RELEASE_NAME" "$INSTALL_DIR/current"
sudo chown -h "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/current"

sudo install -m 0644 "$RELEASE_DIR/systemd/iris-device.service.example" \
  /etc/systemd/system/iris-device.service
sudo install -m 0644 "$RELEASE_DIR/systemd/iris-device-setup.service.example" \
  /etc/systemd/system/iris-device-setup.service
for removed_service in iris-device-ui iris-device-console iris-device-touch-ui iris-device-kiosk; do
  sudo systemctl disable --now "$removed_service" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/${removed_service}.service"
done
sudo systemctl daemon-reload

echo "Installed to $INSTALL_DIR"
echo "Edit $INSTALL_DIR/.env, claim a pairing token, then run:"
echo "  sudo systemctl enable --now iris-device-setup"
echo "  sudo systemctl enable --now iris-device"
