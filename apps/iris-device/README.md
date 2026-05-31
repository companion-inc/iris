# Iris Device Client

Optional Linux room-speaker client for Iris.

Desktop-only Iris does not need this client. Use it when you want a separate
speaker/microphone device in another room while the Mac runs the local API and
voice runtime.

## Local Shape

```txt
Linux room device
  -> captures microphone PCM
  -> streams to local Mac iris-voice
  -> plays assistant PCM
  -> reports local hardware state to local Mac iris-api
```

Set the API URL to the Mac on the LAN:

```sh
IRIS_API_URL=http://<mac-lan-ip>:4747
```

## Setup

Runtime state lives outside releases:

- `/opt/iris-device/.env`
- `/opt/iris-device/.device-token`
- `/opt/iris-device/.device-id`
- `/opt/iris-device/.venv`

Manual local install:

```bash
sudo apt update
sudo apt install -y python3 alsa-utils bluetooth bluez network-manager

scripts/uv-ready.sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -r requirements.txt

cp .env.example .env
```

Then run:

```bash
scripts/install.sh
```

## Test

```bash
python device_client.py report
python device_client.py doctor
arecord -l
arecord -q -f S16_LE -r 16000 -c 2 -t raw -d 3 /tmp/iris-device-test.raw
```

If your microphone is not the default ALSA input:

```bash
IRIS_DEVICE_ALSA_DEVICE=hw:1,0
```

## Stream

```bash
python device_client.py stream
```

The client captures `pcm16`, `16kHz` audio and sends mono audio to the Iris
voice runtime. Multi-channel devices can set:

```bash
IRIS_DEVICE_CAPTURE_CHANNEL_INDEX=0
IRIS_DEVICE_PLAYBACK_CAPTURE_CHANNEL_INDEX=1
```

The voice runtime owns streaming transcription, transcript storage, wake phrase
gating, assistant turns, and assistant audio. The device should stay simple:
capture, playback, local hardware setup, and status reporting.

## Earcons

The device can play local PCM cues for wake detection, speaker identity, tool
start, and tool failure:

```bash
IRIS_DEVICE_EARCONS_ENABLED=true
IRIS_DEVICE_WAKE_EARCON_ENABLED=true
```

Disable all cues:

```bash
IRIS_DEVICE_EARCONS_ENABLED=false
```

## systemd

```bash
scripts/install.sh
sudo systemctl enable --now iris-device-setup
sudo systemctl enable --now iris-device
```

Logs:

```bash
journalctl -u iris-device -f
journalctl -u iris-device-setup -f
```

## Local Control

The device uses the local Iris API and voice WebSocket. It does not use a cloud
device-control plane or remote update jobs.
