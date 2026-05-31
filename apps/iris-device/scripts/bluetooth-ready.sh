#!/usr/bin/env bash
set -euo pipefail

# Bluetooth preflight for Iris device BLE setup.
#
# We only use BLE GATT advertising via bless, so we intentionally leave
# classic Bluetooth *discoverable* and *pairable* off. iOS otherwise
# surfaces a "Bluetooth Pairing Request" PIN dialog for the controller's
# old classic-BT name. BLE central scanning
# by our service UUID still finds the Iris without classic pairing.

SETUP_NAME="${IRIS_DEVICE_SETUP_NAME:-Iris 1}"

if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock bluetooth || true
fi

systemctl start bluetooth

if command -v bluetoothctl >/dev/null 2>&1; then
  bluetoothctl disconnect || true
  bluetoothctl power on
  bluetoothctl system-alias "$SETUP_NAME" || true
  bluetoothctl pairable off || true
  bluetoothctl discoverable off || true
fi

if command -v hciconfig >/dev/null 2>&1; then
  hciconfig hci0 noleadv || true
  sleep 0.5
  hciconfig hci0 down || true
  sleep 0.5
  hciconfig hci0 up || true
  hciconfig hci0 name "$SETUP_NAME" || true
  hciconfig hci0 noscan || true
fi

bluetoothctl show | sed -n '1,14p'
