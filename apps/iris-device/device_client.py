#!/usr/bin/env python3
"""Linux client for Iris devices.

The client speaks to the Iris API:
- claim a pairing token once to get a device token
- stream ALSA microphone audio to the Iris voice runtime for continuous
  transcription and transcript-gated assistant speech
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import grp
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import WebSocketException

from device_audio import (
    audio_command,
    command_output,
    earcon_pcm,
    frame_size_bytes,
    pcm_channel_rms,
    pcm_rms,
    playback_command,
    set_playback_enabled,
    speaker_volume,
    tone_pcm,
)
from device_control import (
    DeviceCommandHandler,
    DeviceControlState,
    current_wifi_ssid,
    hardware_info as device_hardware_info,
    ip_addresses,
)
from device_xvf import set_amp_enabled, set_mic_muted, xvf_audio_state
from device_playback import VoicePlayback
ROOT = Path(__file__).resolve().parent
STATE_DIR = Path("/opt/iris-device")
ENV_FILE = STATE_DIR / ".env"
TOKEN_FILE = STATE_DIR / ".device-token"
DEVICE_ID_FILE = STATE_DIR / ".device-id"
USER_AGENT = "IrisDevice/1"
VERSION_FILE = STATE_DIR / "current" / ".version"
LOG_FILE = Path("/tmp/device-client.log")
MAX_LOG_BYTES = 256 * 1024

@dataclass(frozen=True)
class Config:
    api_url: str
    device_id: str | None
    device_token: str | None
    device_serial: str | None
    firmware_version: str | None
    sample_rate: int
    channels: int
    capture_channel_index: int | None
    playback_capture_channel_index: int | None
    chunk_ms: int
    audio_device: str | None
    playback_device: str | None
    playback_channels: int
    alsa_buffer_time_us: int | None
    alsa_period_time_us: int | None
    report_interval_s: int
    reconnect_delay_s: float
    playback_idle_stop_s: float
    earcons_enabled: bool
    wake_earcon_enabled: bool
    voice_runtime: str


class DeviceTokenInvalid(RuntimeError):
    pass


def debug_log(event: str, **fields: Any) -> None:
    payload = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, default=str, sort_keys=True)
    print(f"[device] {line}", flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.write_text("")
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError as error:
        print(f"[device-log] write failed: {error}", file=sys.stderr)


def hardware_id() -> str:
    for path in ("/sys/class/net/wlan0/address", "/sys/class/bluetooth/hci0/address"):
        try:
            raw = Path(path).read_text().strip()
            if raw:
                return raw.replace(":", "").upper()
        except OSError:
            pass
    return os.uname().nodename.upper()


def device_serial() -> str:
    configured = env_value("IRIS_DEVICE_SERIAL")
    if configured:
        return configured
    return f"IRIS-DEV-{hardware_id()}"


def firmware_version() -> str:
    configured = env_value("IRIS_DEVICE_FIRMWARE_VERSION")
    if configured:
        return configured
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "unknown"


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_token() -> str | None:
    env_token = env_value("IRIS_DEVICE_TOKEN")
    if env_token:
        return env_token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip() or None
    return None


def read_device_id() -> str | None:
    env_device_id = env_value("IRIS_DEVICE_ID")
    if env_device_id:
        return env_device_id
    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip() or None
    return None


def write_identity(device_id: str, token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    DEVICE_ID_FILE.write_text(device_id)
    DEVICE_ID_FILE.chmod(0o600)
    if os.geteuid() == 0:
        service_user = env_value("IRIS_DEVICE_SERVICE_USER") or "pi"
        service_group = env_value("IRIS_DEVICE_SERVICE_GROUP") or "audio"
        try:
            uid = pwd.getpwnam(service_user).pw_uid
            gid = grp.getgrnam(service_group).gr_gid
        except KeyError:
            return
        os.chown(TOKEN_FILE, uid, gid)
        os.chown(DEVICE_ID_FILE, uid, gid)


def chown_state_path(path: Path) -> None:
    if os.geteuid() != 0:
        return
    service_user = env_value("IRIS_DEVICE_SERVICE_USER") or "pi"
    service_group = env_value("IRIS_DEVICE_SERVICE_GROUP") or "audio"
    try:
        uid = pwd.getpwnam(service_user).pw_uid
        gid = grp.getgrnam(service_group).gr_gid
    except KeyError:
        return
    if path.is_dir():
        for child in path.rglob("*"):
            os.chown(child, uid, gid)
    os.chown(path, uid, gid)


def persist_env_value(key: str, value: str) -> None:
    try:
        lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
        next_line = f"{key}={value}"
        updated = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[index] = next_line
                updated = True
                break
        if not updated:
            lines.append(next_line)
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text("\n".join(lines) + "\n")
    except Exception as error:
        debug_log("env_update_failed", key=key, error=str(error))


def delete_token() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    try:
        DEVICE_ID_FILE.unlink()
    except FileNotFoundError:
        pass


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def optional_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


def optional_float_env(name: str, fallback: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return fallback
    return float(value)


def optional_int_env_with_fallback(name: str, fallback: int) -> int:
    value = optional_int_env(name)
    return fallback if value is None else value


def bool_env(name: str, fallback: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return fallback
    return value.strip().lower() not in {"0", "false", "no", "off"}


def config() -> Config:
    load_env_file()
    api_url = require_env("IRIS_API_URL").rstrip("/")
    return Config(
        api_url=api_url,
        device_id=read_device_id(),
        device_token=read_token(),
        device_serial=device_serial(),
        firmware_version=firmware_version(),
        sample_rate=int(require_env("IRIS_DEVICE_SAMPLE_RATE")),
        channels=int(require_env("IRIS_DEVICE_CHANNELS")),
        capture_channel_index=optional_int_env("IRIS_DEVICE_CAPTURE_CHANNEL_INDEX"),
        playback_capture_channel_index=optional_int_env(
            "IRIS_DEVICE_PLAYBACK_CAPTURE_CHANNEL_INDEX"
        ),
        chunk_ms=int(require_env("IRIS_DEVICE_CHUNK_MS")),
        audio_device=env_value("IRIS_DEVICE_ALSA_DEVICE"),
        playback_device=env_value("IRIS_DEVICE_PLAYBACK_DEVICE"),
        playback_channels=max(
            1,
            min(
                8,
                int(env_value("IRIS_DEVICE_PLAYBACK_CHANNELS") or "2"),
            ),
        ),
        alsa_buffer_time_us=(
            int(value) if (value := env_value("IRIS_DEVICE_ALSA_BUFFER_TIME_US")) else None
        ),
        alsa_period_time_us=(
            int(value) if (value := env_value("IRIS_DEVICE_ALSA_PERIOD_TIME_US")) else None
        ),
        report_interval_s=int(env_value("IRIS_DEVICE_REPORT_INTERVAL_SECONDS") or "30"),
        reconnect_delay_s=max(
            0.0,
            min(10.0, optional_float_env("IRIS_DEVICE_RECONNECT_DELAY_SECONDS", 1.0)),
        ),
        playback_idle_stop_s=max(
            0.2,
            min(10.0, optional_float_env("IRIS_DEVICE_PLAYBACK_IDLE_STOP_SECONDS", 6.0)),
        ),
        earcons_enabled=bool_env("IRIS_DEVICE_EARCONS_ENABLED", True),
        wake_earcon_enabled=bool_env(
            "IRIS_DEVICE_WAKE_EARCON_ENABLED",
            bool_env("IRIS_DEVICE_WAKE_WORD_CHIME_ENABLED", True),
        ),
        voice_runtime=(env_value("IRIS_DEVICE_VOICE_RUNTIME") or "pipecat").strip().lower(),
    )


def api_request(
    cfg: Config,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    device_token: str | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{cfg.api_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    token = device_token or cfg.device_token
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        if error.code == 401:
            debug_log("api_unauthorized", method=method, path=path, status=error.code)
            raise DeviceTokenInvalid(f"{method} {path} failed: {error.code} {message}") from error
        debug_log("api_error", method=method, path=path, status=error.code, message=message[:200])
        raise RuntimeError(f"{method} {path} failed: {error.code} {message}") from error


def create_voice_session(cfg: Config) -> dict[str, Any]:
    return api_request(
        cfg,
        "POST",
        "/v1/voice/sessions",
        {
            "sampleRate": cfg.sample_rate,
            "channels": voice_output_channels(cfg),
        },
    )


def voice_output_channels(cfg: Config) -> int:
    return (
        1
        if cfg.capture_channel_index is not None
        or cfg.playback_capture_channel_index is not None
        else cfg.channels
    )


def active_capture_channel_index(cfg: Config, *, playback_active: bool) -> int | None:
    if playback_active and cfg.playback_capture_channel_index is not None:
        return cfg.playback_capture_channel_index
    return cfg.capture_channel_index


def select_capture_channel(audio: bytes, channels: int, channel_index: int | None) -> bytes:
    if channel_index is None:
        return audio
    if channels <= 1:
        return audio
    if channel_index < 0 or channel_index >= channels:
        raise RuntimeError(f"capture channel index {channel_index} out of range for {channels} channels")
    sample_width = 2
    frame_width = channels * sample_width
    selected = bytearray(len(audio) // channels)
    out = 0
    offset = channel_index * sample_width
    for frame_start in range(0, len(audio) - frame_width + 1, frame_width):
        selected[out : out + sample_width] = audio[frame_start + offset : frame_start + offset + sample_width]
        out += sample_width
    return bytes(selected[:out])


def run_check(label: str, ok: bool, detail: str = "") -> bool:
    status = "ok" if ok else "fail"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {label}{suffix}")
    return ok


def print_command_section(title: str, command: list[str]) -> None:
    ok, output = command_output(command)
    status = "ok" if ok else "fail"
    print(f"\n== {title} [{status}] ==")
    print(output or "no output")


def doctor(_args: argparse.Namespace) -> None:
    cfg = config()
    checks: list[bool] = []

    checks.append(run_check(".env loaded", ENV_FILE.exists(), str(ENV_FILE)))
    checks.append(run_check("device token", bool(cfg.device_token), "claim pairing first" if not cfg.device_token else "present"))
    checks.append(run_check("arecord installed", shutil.which("arecord") is not None))
    checks.append(run_check("aplay installed", shutil.which("aplay") is not None))

    print_command_section("usb devices", ["lsusb"])
    print_command_section("usb tree", ["lsusb", "-tv"])
    print_command_section("kernel sound cards", ["cat", "/proc/asound/cards"])
    print_command_section("alsa pcms", ["arecord", "-L"])
    print_command_section("alsa playback hardware", ["aplay", "-l"])

    ok, output = command_output(["arecord", "-l"])
    checks.append(run_check("capture devices", ok and "card " in output, output.splitlines()[0] if output else "none"))

    if cfg.audio_device:
        ok, output = command_output(
            [
                "arecord",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(cfg.sample_rate),
                "-c",
                str(cfg.channels),
                "-D",
                cfg.audio_device,
                "-t",
                "raw",
                "-d",
                "1",
                "/tmp/device-doctor.raw",
            ]
        )
    else:
        ok, output = command_output(
            [
                "arecord",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(cfg.sample_rate),
                "-c",
                str(cfg.channels),
                "-t",
                "raw",
                "-d",
                "1",
                "/tmp/device-doctor.raw",
            ]
        )
    checks.append(run_check("test capture", ok, output))

    if cfg.device_token:
        try:
            api_request(cfg, "GET", "/health")
            checks.append(run_check("backend health", True, cfg.api_url))
        except Exception as error:
            checks.append(run_check("backend health", False, str(error)))
        try:
            api_request(cfg, "GET", "/health/voice")
            checks.append(run_check("voice health", True, cfg.api_url))
        except Exception as error:
            checks.append(run_check("voice health", False, str(error)))
    raise SystemExit(0 if all(checks) else 1)


def playback_test(_args: argparse.Namespace) -> None:
    cfg = config()
    command = playback_command(cfg)
    state_before = xvf_audio_state()
    debug_log(
        "playback_test_start",
        command=" ".join(command),
        playbackDevice=cfg.playback_device or "default",
        xvf=state_before,
    )
    set_mic_muted(False)
    set_amp_enabled(True)
    set_playback_enabled(cfg, True)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        if process.stdin is None:
            raise RuntimeError("aplay stdin is unavailable")
        process.stdin.write(tone_pcm(cfg.sample_rate, channels=cfg.playback_channels))
        process.stdin.close()
        return_code = process.wait(timeout=5)
        debug_log("playback_test_done", returnCode=return_code, xvf=xvf_audio_state())
        raise SystemExit(return_code)
    except Exception as error:
        process.kill()
        debug_log("playback_test_failed", error=str(error))
        raise
    finally:
        set_amp_enabled(False)


def play_earcon(cfg: Config, name: str) -> None:
    if not cfg.earcons_enabled:
        return
    command = playback_command(cfg)
    pcm = earcon_pcm(name, cfg.sample_rate, channels=cfg.playback_channels)
    if not pcm:
        debug_log("earcon_skipped", name=name, reason="unknown")
        return
    debug_log(
        "earcon_start",
        name=name,
        command=" ".join(command),
        playbackDevice=cfg.playback_device or "default",
    )
    debug_log("xvf_mic_mute_set", muted=False, **set_mic_muted(False))
    debug_log("xvf_amp_set", enabled=True, **set_amp_enabled(True))
    debug_log("playback_path_set", enabled=True, ok=set_playback_enabled(cfg, True))
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        if process.stdin is None:
            raise RuntimeError("aplay stdin is unavailable")
        process.stdin.write(pcm)
        process.stdin.close()
        return_code = process.wait(timeout=3)
        debug_log("earcon_done", name=name, returnCode=return_code, bytes=len(pcm))
    except Exception as error:
        process.kill()
        debug_log("earcon_failed", name=name, error=str(error))
    finally:
        debug_log("xvf_amp_set", enabled=False, **set_amp_enabled(False))


def claim(args: argparse.Namespace) -> None:
    cfg = config()
    body: dict[str, Any] = {"token": args.pairing_token}
    if cfg.device_serial:
        body["serial"] = cfg.device_serial
    if cfg.firmware_version:
        body["firmware"] = cfg.firmware_version
    result = api_request(cfg, "POST", "/v1/devices", body)
    device = result["device"]
    token = str(result["token"])
    write_identity(str(device["id"]), token)
    print(f"Claimed {device['name']} ({device['id']})")
    print(f"Saved device token to {TOKEN_FILE}")


def report(args: argparse.Namespace) -> None:
    cfg = config()
    print(json.dumps(reported_state(cfg, DeviceControlState(cfg, debug_log), args.status), indent=2))


def hardware_info(cfg: Config, control: DeviceControlState | None = None) -> dict[str, Any]:
    info = {**device_hardware_info(cfg), "version": firmware_version()}
    if control and control.speaker_volume is not None:
        info["speakerVolume"] = control.speaker_volume
        info["alsaSpeakerVolume"] = speaker_volume(cfg)
    return info


def reported_state(cfg: Config, control: DeviceControlState, status: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": status,
        "firmwareVersion": firmware_version(),
        "listeningEnabled": control.listening_enabled,
        "speakerVolume": control.speaker_volume,
        "wakeWord": control.wake_word,
        "hardwareInfo": hardware_info(cfg, control),
    }
    for key in ("llmBaseUrl", "llmModel", "llmApiKeyConfigured", "statusLight"):
        if key in control.last_desired:
            state[key] = control.last_desired[key]
    return state


async def stream_voice_audio(
    websocket: Any,
    cfg: Config,
    control: DeviceControlState,
    playback_tasks: set[asyncio.Task[None]],
    playback_active: asyncio.Event,
) -> None:
    device_commands = DeviceCommandHandler(cfg, debug_log, control)
    playback = VoicePlayback(cfg, playback_tasks, playback_active, debug_log)
    playback_failed = False
    received_audio_frames = 0
    received_audio_bytes = 0
    last_earcon_at_by_name: dict[str, float] = {}
    earcon_active = asyncio.Event()
    active_tool_keys: set[str] = set()
    active_tool_started_at: dict[str, float] = {}
    tool_running_task: asyncio.Task[None] | None = None
    tool_running_first_interval_s = 1.15
    tool_running_later_interval_s = 4.0
    tool_running_fast_window_s = 6.0
    tool_running_stale_timeout_s = 180.0

    def schedule_earcon(
        name: str,
        *,
        min_interval_s: float = 0.0,
        wait_for_idle_s: float = 0.0,
    ) -> None:
        if name == "wake" and not cfg.wake_earcon_enabled:
            debug_log("earcon_skipped", name=name, reason="wake_disabled")
            return
        if min_interval_s > 0:
            now = time.monotonic()
            last_at = last_earcon_at_by_name.get(name, 0.0)
            if now - last_at < min_interval_s:
                debug_log("earcon_skipped", name=name, reason="cooldown")
                return
            last_earcon_at_by_name[name] = now

        async def play_when_available() -> None:
            deadline = time.monotonic() + wait_for_idle_s
            while playback_active.is_set() or earcon_active.is_set():
                if wait_for_idle_s <= 0 or time.monotonic() >= deadline:
                    reason = "playback_active" if playback_active.is_set() else "earcon_active"
                    debug_log("earcon_skipped", name=name, reason=reason)
                    return
                await asyncio.sleep(0.03)
            earcon_active.set()
            try:
                await asyncio.to_thread(play_earcon, cfg, name)
            finally:
                earcon_active.clear()

        task = asyncio.create_task(play_when_available())
        playback_tasks.add(task)
        task.add_done_callback(playback_tasks.discard)

    def tool_key(payload: dict[str, Any]) -> str:
        for key in ("toolCallId", "requestId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        name = str(payload.get("name") or "unknown")
        action = str(payload.get("action") or "")
        return f"{name}:{action}"

    async def tool_running_loop() -> None:
        try:
            while active_tool_keys:
                now = time.monotonic()
                stale_keys = [
                    key
                    for key in active_tool_keys
                    if now - active_tool_started_at.get(key, now) > tool_running_stale_timeout_s
                ]
                for key in stale_keys:
                    active_tool_keys.discard(key)
                    active_tool_started_at.pop(key, None)
                    debug_log("tool_running_stale_cleared", key=key, activeTools=len(active_tool_keys))
                if not active_tool_keys:
                    break

                oldest_started_at = min(active_tool_started_at.get(key, now) for key in active_tool_keys)
                elapsed_s = now - oldest_started_at
                interval_s = (
                    tool_running_first_interval_s
                    if elapsed_s < tool_running_fast_window_s
                    else tool_running_later_interval_s
                )
                schedule_earcon(
                    "tool_running",
                    min_interval_s=max(0.9, interval_s - 0.15),
                    wait_for_idle_s=0.6,
                )
                await asyncio.sleep(interval_s)
        finally:
            debug_log("tool_running_earcon_stop", activeTools=len(active_tool_keys))

    def mark_tool_started(payload: dict[str, Any]) -> None:
        nonlocal tool_running_task
        key = tool_key(payload)
        active_tool_keys.add(key)
        active_tool_started_at[key] = time.monotonic()
        debug_log("tool_running_started", key=key, activeTools=len(active_tool_keys))
        if tool_running_task is None or tool_running_task.done():
            tool_running_task = asyncio.create_task(tool_running_loop())
            playback_tasks.add(tool_running_task)
            tool_running_task.add_done_callback(playback_tasks.discard)

    def mark_tool_finished(payload: dict[str, Any]) -> None:
        key = tool_key(payload)
        active_tool_keys.discard(key)
        active_tool_started_at.pop(key, None)
        if payload.get("phase") in {"final", "error", "validation_error"}:
            fallback_key = str(payload.get("name") or "unknown") + ":"
            active_tool_keys.discard(fallback_key)
            active_tool_started_at.pop(fallback_key, None)
        debug_log("tool_running_earcon_update", key=key, activeTools=len(active_tool_keys))
        if not active_tool_keys:
            debug_log("tool_running_earcon_stop", activeTools=0)

    async def wait_for_earcon_idle(reason: str, timeout_s: float = 0.75) -> None:
        if not earcon_active.is_set():
            return
        started_at = time.monotonic()
        deadline = started_at + timeout_s
        while earcon_active.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        debug_log(
            "voice_playback_waited_for_earcon",
            reason=reason,
            waitedMs=int((time.monotonic() - started_at) * 1000),
            earconActive=earcon_active.is_set(),
        )

    try:
        async for raw in websocket:
            if raw == "pong":
                continue
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload_type = payload.get("type")
            if payload_type == "ready":
                debug_log("voice_ready", sessionId=payload.get("sessionId"))
            elif payload_type == "wake.detected":
                debug_log("voice_wake_detected", text=str(payload.get("text") or "")[:120])
                schedule_earcon("wake")
            elif payload_type == "wake.accepted":
                debug_log(
                    "voice_wake_accepted",
                    prompt=str(payload.get("prompt") or "")[:120],
                )
            elif payload_type == "speaker.identified":
                debug_log(
                    "voice_speaker_identified",
                    speakerName=str(payload.get("displayName") or "")[:120],
                    userId=str(payload.get("userId") or "")[:120],
                    score=payload.get("score"),
                )
                schedule_earcon("speaker_identified", min_interval_s=1.0)
            elif payload_type in {"wake.ignored", "wake.stopped"}:
                debug_log(
                    "voice_wake_policy",
                    policyEvent=payload_type,
                    reason=payload.get("reason"),
                    intent=payload.get("intent"),
                    text=str(payload.get("text") or "")[:120],
                )
            elif payload_type in {"transcript.interim", "transcript.final"}:
                words = payload.get("words")
                debug_log(
                    "voice_transcript",
                    final=payload_type == "transcript.final",
                    wakeDetected=payload.get("wakeDetected"),
                    speaker=payload.get("speaker"),
                    confidence=payload.get("confidence"),
                    wordCount=len(words) if isinstance(words, list) else None,
                    text=str(payload.get("text") or "")[:120],
                )
            elif payload_type == "sound.recognition.detected":
                debug_log(
                    "voice_sound_recognition_detected",
                    label=str(payload.get("label") or "")[:80],
                    confidence=payload.get("confidence"),
                    behavior=str(payload.get("behavior") or "")[:40],
                    model=str(payload.get("model") or "")[:120],
                )
            elif payload_type == "sound.recognition.prompted":
                debug_log(
                    "voice_sound_recognition_prompted",
                    label=str(payload.get("label") or "")[:80],
                    confidence=payload.get("confidence"),
                    prompt=str(payload.get("prompt") or "")[:120],
                )
            elif payload_type == "assistant.text":
                debug_log("voice_assistant_text", text=str(payload.get("text") or "")[:160])
            elif payload_type == "assistant.no_speech":
                debug_log("voice_assistant_no_speech")
            elif payload_type == "tool.called":
                debug_log(
                    "voice_tool_called",
                    name=str(payload.get("name") or "")[:80],
                    action=str(payload.get("action") or "")[:40],
                    hubId=str(payload.get("hubId") or "")[:120],
                )
                schedule_earcon("tool_started", min_interval_s=0.8, wait_for_idle_s=1.5)
            elif payload_type == "tool.started":
                debug_log(
                    "voice_tool_started",
                    name=str(payload.get("name") or "")[:80],
                    toolCallId=str(payload.get("toolCallId") or "")[:120],
                    requestId=str(payload.get("requestId") or "")[:120],
                    action=str(payload.get("action") or "")[:40],
                    hubId=str(payload.get("hubId") or "")[:120],
                    threadId=str(payload.get("threadId") or "")[:120],
                )
                schedule_earcon("tool_started", min_interval_s=0.8, wait_for_idle_s=1.5)
                mark_tool_started(payload)
            elif payload_type == "tool.result":
                summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
                debug_log(
                    "voice_tool_result",
                    name=str(payload.get("name") or "")[:80],
                    toolCallId=str(payload.get("toolCallId") or "")[:120],
                    requestId=str(payload.get("requestId") or "")[:120],
                    phase=str(payload.get("phase") or "")[:40],
                    isFinal=payload.get("isFinal"),
                    runLlm=payload.get("runLlm"),
                    elapsedMs=payload.get("elapsedMs"),
                    ok=summary.get("ok"),
                    status=str(summary.get("status") or "")[:80],
                    error=str(summary.get("error") or "")[:160],
                    agentStatus=str(summary.get("agentStatus") or "")[:80],
                )
                phase = str(payload.get("phase") or "")
                if phase in {"final", "error", "validation_error"} or payload.get("isFinal") is True:
                    mark_tool_finished(payload)
                if payload.get("phase") == "error" or summary.get("ok") is False:
                    schedule_earcon("tool_failed", min_interval_s=0.8, wait_for_idle_s=1.5)
            elif payload_type in {"tool.cancelled", "tool.callback.failed"}:
                debug_log(
                    "voice_tool_lifecycle_problem",
                    eventType=payload_type,
                    name=str(payload.get("name") or "")[:80],
                    toolCallId=str(payload.get("toolCallId") or "")[:120],
                    requestId=str(payload.get("requestId") or "")[:120],
                    phase=str(payload.get("phase") or "")[:40],
                    error=str(payload.get("error") or "")[:160],
                    elapsedMs=payload.get("elapsedMs"),
                )
                mark_tool_finished(payload)
                if payload_type == "tool.callback.failed":
                    schedule_earcon("tool_failed", min_interval_s=0.8, wait_for_idle_s=1.5)
            elif payload_type == "hub.completion.tool_result":
                debug_log(
                    "voice_hub_completion_tool_result",
                    completionId=str(payload.get("completionId") or "")[:120],
                    runId=str(payload.get("runId") or "")[:120],
                    toolCallId=str(payload.get("toolCallId") or "")[:120],
                    runLlm=payload.get("runLlm"),
                )
                mark_tool_finished(payload)
            elif payload_type == "hub.completion.injected":
                debug_log(
                    "voice_hub_completion_injected",
                    completionId=str(payload.get("completionId") or "")[:120],
                    runId=str(payload.get("runId") or "")[:120],
                    status=str(payload.get("status") or "")[:80],
                    delivery=str(payload.get("delivery") or "")[:40],
                    runLlm=payload.get("runLlm"),
                )
            elif payload_type == "device.command.status":
                debug_log(
                    "voice_device_command_status",
                    requestId=str(payload.get("requestId") or "")[:120],
                    commandType=str(payload.get("commandType") or "")[:80],
                    status=str(payload.get("status") or "")[:40],
                    error=str(payload.get("error") or "")[:160],
                )
            elif await device_commands.handle(payload, websocket.send):
                continue
            elif payload_type == "assistant.audio.started":
                playback_failed = False
                await wait_for_earcon_idle("assistant_audio_started")
                playback_active.set()
                debug_log("voice_playback_active", active=True)
            elif payload_type == "assistant.audio.stopped":
                playback_failed = False
                reason = str(payload.get("reason") or "completed")
                debug_log(
                    "voice_playback_stop_received",
                    reason=reason,
                    active=playback_active.is_set(),
                )
                await playback.stop(reason=reason)
            elif payload_type == "audio":
                received_audio_frames += 1
                if playback_failed:
                    if received_audio_frames == 1 or received_audio_frames % 50 == 0:
                        debug_log("voice_audio_dropped", reason="playback_failed", frames=received_audio_frames)
                    continue
                encoded_audio = payload.get("audio")
                if not encoded_audio:
                    debug_log("voice_audio_dropped", reason="missing_audio", frames=received_audio_frames)
                    continue
                sample_rate = int(payload.get("sampleRate") or cfg.sample_rate)
                source_channels = max(1, min(8, int(payload.get("channels") or 1)))
                encoded_size = len(encoded_audio) if isinstance(encoded_audio, str) else 0
                if received_audio_frames == 1 or received_audio_frames % 50 == 0:
                    debug_log(
                        "voice_audio_received",
                        frames=received_audio_frames,
                        encodedBytes=encoded_size,
                        totalDecodedBytes=received_audio_bytes,
                        sampleRate=sample_rate,
                        sourceChannels=source_channels,
                        playbackActive=playback_active.is_set(),
                    )
                source_audio = base64.b64decode(encoded_audio)
                received_audio_bytes += len(source_audio)
                await wait_for_earcon_idle("audio_frame")
                if not await playback.write(source_audio, sample_rate, source_channels):
                    playback_failed = True
    finally:
        active_tool_keys.clear()
        if tool_running_task is not None:
            tool_running_task.cancel()
        await playback.close()


async def stream_audio() -> None:
    cfg = config()
    debug_log("xvf_audio_state_startup", **xvf_audio_state())
    debug_log("xvf_mic_mute_set", muted=False, **set_mic_muted(False))
    debug_log("xvf_amp_set", enabled=False, **set_amp_enabled(False))
    debug_log("playback_path_set", enabled=True, ok=set_playback_enabled(cfg, True))
    debug_log(
        "startup",
        version=firmware_version(),
        apiUrl=cfg.api_url,
        audioDevice=cfg.audio_device or "default",
        playbackDevice=cfg.playback_device or "default",
        sampleRate=cfg.sample_rate,
        captureChannels=cfg.channels,
        voiceChannels=voice_output_channels(cfg),
        captureChannelIndex=cfg.capture_channel_index,
        playbackCaptureChannelIndex=cfg.playback_capture_channel_index,
        playbackChannels=cfg.playback_channels,
        playbackIdleStopSeconds=cfg.playback_idle_stop_s,
        reconnectDelaySeconds=cfg.reconnect_delay_s,
        earconsEnabled=cfg.earcons_enabled,
        wakeEarconEnabled=cfg.wake_earcon_enabled,
        voiceRuntime=cfg.voice_runtime,
    )
    stop = asyncio.Event()
    control = DeviceControlState(cfg, debug_log)
    current_status = {"value": "online"}
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            current_status["value"] = "online"
            if not control.listening_enabled:
                current_status["value"] = "muted"
                debug_log("audio_paused")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.report_interval_s)
                except asyncio.TimeoutError:
                    pass
                continue

            stream_stop = asyncio.Event()
            current_status["value"] = "listening"
            playback_tasks: set[asyncio.Task[None]] = set()
            playback_active = asyncio.Event()

            command = audio_command(cfg)
            process = subprocess.Popen(command, stdout=subprocess.PIPE)
            debug_log(
                "arecord_started",
                pid=process.pid,
                command=" ".join(command),
                sampleRate=cfg.sample_rate,
                captureChannels=cfg.channels,
                voiceChannels=voice_output_channels(cfg),
                captureChannelIndex=cfg.capture_channel_index,
                playbackCaptureChannelIndex=cfg.playback_capture_channel_index,
                voiceRuntime=cfg.voice_runtime,
            )

            try:
                frame_size = frame_size_bytes(cfg)
                audio_bytes = 0
                audio_frames = 0
                last_audio_log_at = 0.0
                voice_session = create_voice_session(cfg)
                voice_url = str(voice_session["voiceUrl"])
                parsed_voice_url = urlparse(voice_url)
                debug_log(
                    "voice_session_received",
                    sessionId=voice_session.get("sessionId"),
                    voiceScheme=parsed_voice_url.scheme,
                    voiceHost=parsed_voice_url.netloc,
                    voicePath=parsed_voice_url.path,
                    sampleRate=cfg.sample_rate,
                    captureChannels=cfg.channels,
                    voiceChannels=voice_output_channels(cfg),
                    captureChannelIndex=cfg.capture_channel_index,
                    playbackCaptureChannelIndex=cfg.playback_capture_channel_index,
                )
                async with websockets.connect(
                    voice_url,
                    additional_headers={"User-Agent": USER_AGENT},
                    max_size=None,
                ) as websocket:
                    debug_log(
                        "voice_connected",
                        sessionId=voice_session.get("sessionId"),
                    )
                    receive_task = asyncio.create_task(
                        stream_voice_audio(websocket, cfg, control, playback_tasks, playback_active)
                    )
                    try:
                        while (
                            not stop.is_set()
                            and not stream_stop.is_set()
                            and control.listening_enabled
                        ):
                            if process.stdout is None:
                                raise RuntimeError("arecord stdout is unavailable")
                            chunk = await asyncio.to_thread(process.stdout.read, frame_size)
                            if not chunk:
                                raise RuntimeError("arecord stopped producing audio")
                            playback_is_active = playback_active.is_set()
                            capture_channel_index = active_capture_channel_index(
                                cfg,
                                playback_active=playback_is_active,
                            )
                            voice_chunk = select_capture_channel(
                                chunk,
                                cfg.channels,
                                capture_channel_index,
                            )
                            audio_frames += 1
                            audio_bytes += len(voice_chunk)
                            await websocket.send(voice_chunk)
                            if receive_task.done():
                                await receive_task
                                raise RuntimeError("voice receive loop stopped")
                            now = time.monotonic()
                            rms = pcm_rms(voice_chunk)
                            raw_channel_rms = pcm_channel_rms(chunk, cfg.channels)
                            log_interval_s = 1 if playback_is_active else 10
                            if audio_frames == 1 or now - last_audio_log_at >= log_interval_s:
                                debug_log(
                                    "audio_frame",
                                    bytes=audio_bytes,
                                    frames=audio_frames,
                                    rawBytes=len(chunk),
                                    voiceBytes=len(voice_chunk),
                                    rms=rms,
                                    rawChannelRms=raw_channel_rms,
                                    playbackActive=playback_is_active,
                                    captureChannels=cfg.channels,
                                    voiceChannels=voice_output_channels(cfg),
                                    captureChannelIndex=capture_channel_index,
                                    defaultCaptureChannelIndex=cfg.capture_channel_index,
                                    playbackCaptureChannelIndex=cfg.playback_capture_channel_index,
                                )
                                last_audio_log_at = now
                    finally:
                        receive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await receive_task
            except (
                OSError,
                TimeoutError,
                RuntimeError,
                WebSocketException,
            ) as error:
                debug_log("voice_stream_reconnect", error=f"{type(error).__name__}: {error}")
            except Exception as error:
                debug_log("voice_stream_reconnect", error=f"{type(error).__name__}: {error}")
            finally:
                restart_immediately = stream_stop.is_set()
                stream_stop.set()
                for task in playback_tasks:
                    task.cancel()
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            if not stop.is_set() and not restart_immediately:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.reconnect_delay_s)
                except asyncio.TimeoutError:
                    pass
    finally:
        stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="Iris device client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim_parser = subparsers.add_parser("claim", help="claim a pairing token and save device token")
    claim_parser.add_argument("pairing_token")
    claim_parser.set_defaults(func=claim)

    report_parser = subparsers.add_parser("report", help="print one Iris device reported-state payload")
    report_parser.add_argument("--status", default="online")
    report_parser.set_defaults(func=report)

    doctor_parser = subparsers.add_parser("doctor", help="check device audio, token, and backend reachability")
    doctor_parser.set_defaults(func=doctor)

    playback_test_parser = subparsers.add_parser("playback-test", help="play a short tone through Iris device output")
    playback_test_parser.set_defaults(func=playback_test)

    stream_parser = subparsers.add_parser("stream", help="stream microphone audio to Iris")
    stream_parser.set_defaults(func=lambda _args: asyncio.run(stream_audio()))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
