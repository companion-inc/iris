#!/usr/bin/env bash
set -euo pipefail

SERVICE_GROUP="${SERVICE_GROUP:-audio}"
RULE_FILE="/etc/udev/rules.d/60-iris-xvf3800.rules"

sudo tee "$RULE_FILE" >/dev/null <<RULE
# Allow iris-device to control the Seeed reSpeaker XVF3800 vendor interface.
SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="001a", MODE="0660", GROUP="${SERVICE_GROUP}", TAG+="uaccess"
RULE

sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2886 --attr-match=idProduct=001a >/dev/null 2>&1 || true
