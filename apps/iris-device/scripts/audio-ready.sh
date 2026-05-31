#!/usr/bin/env bash
set -euo pipefail

# OpenHome-inspired audio preflight for boot-time USB mic weirdness.
# It keeps the core client simple while giving systemd a cheap readiness gate.

if ! command -v arecord >/dev/null 2>&1; then
  echo "arecord is not installed. Install alsa-utils." >&2
  exit 1
fi

dump_audio_diagnostics() {
  local stamp_file="/tmp/iris-device-audio-diagnostics.last"
  local now last
  now="$(date +%s 2>/dev/null || echo 0)"
  last="$(cat "$stamp_file" 2>/dev/null || echo 0)"
  if [[ "$now" != "0" && "$last" =~ ^[0-9]+$ && $((now - last)) -lt 60 ]]; then
    echo "audio diagnostics skipped; last dump was $((now - last))s ago" >&2
    return
  fi
  printf '%s\n' "$now" >"$stamp_file" 2>/dev/null || true

  {
    echo "----- iris device audio diagnostics -----"
    echo "time=$(date -Is 2>/dev/null || date)"
    echo "user=$(id 2>/dev/null || true)"
    echo "kernel=$(uname -a 2>/dev/null || true)"
    echo "env IRIS_DEVICE_ALSA_DEVICE=${IRIS_DEVICE_ALSA_DEVICE:-}"
    echo "env IRIS_DEVICE_PLAYBACK_DEVICE=${IRIS_DEVICE_PLAYBACK_DEVICE:-}"

    echo
    echo "[usb tree]"
    lsusb -tv 2>&1 || true

    echo
    echo "[usb devices]"
    lsusb 2>&1 || true

    echo
    echo "[kernel sound cards]"
    cat /proc/asound/cards 2>&1 || true

    echo
    echo "[alsa capture hardware]"
    arecord -l 2>&1 || true

    echo
    echo "[alsa pcms]"
    arecord -L 2>&1 | sed -n '1,120p' || true

    echo
    echo "[alsa playback hardware]"
    aplay -l 2>&1 || true

    echo
    echo "[loaded sound modules]"
    lsmod 2>/dev/null | grep -E '^(snd|u_audio|usb_audio)' || true

    echo
    echo "[recent audio/usb kernel log]"
    dmesg 2>/dev/null | grep -Ei 'usb|snd|audio|xmos|seeed|respeaker|xvf|pcm' | tail -120 || true
    echo "----- end iris device audio diagnostics -----"
  } >&2
}

CAPTURE_VOLUME="${IRIS_DEVICE_ALSA_CAPTURE_VOLUME:-}"
CAPTURE_DEVICE="${IRIS_DEVICE_ALSA_DEVICE:-}"

if command -v amixer >/dev/null 2>&1 && [[ -n "$CAPTURE_VOLUME" ]]; then
  capture_card=""
  if [[ "$CAPTURE_DEVICE" =~ CARD=([^,]+) ]]; then
    capture_card="${BASH_REMATCH[1]}"
  fi

  if [[ -n "${capture_card}" ]]; then
    amixer -q -c "${capture_card}" sset Mic "$CAPTURE_VOLUME" cap >/dev/null 2>&1 || true
    amixer -q -c "${capture_card}" sset "Auto Gain Control" on >/dev/null 2>&1 || true
  fi
fi

if arecord -l 2>/dev/null | grep -q "card "; then
  echo "audio capture device detected"
  arecord -l >&2 || true
  exit 0
fi

echo "no ALSA capture device detected" >&2
dump_audio_diagnostics

sleep 2

if arecord -l 2>/dev/null | grep -q "card "; then
  echo "audio capture device detected after retry"
  arecord -l >&2 || true
  exit 0
fi

echo "still no audio capture device" >&2
dump_audio_diagnostics
exit 1
