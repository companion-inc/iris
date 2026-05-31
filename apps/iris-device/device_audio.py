from __future__ import annotations

import math
import re
import subprocess
from typing import Any


def _playback_channels(cfg: Any) -> int:
    value = getattr(cfg, "playback_channels", 2) or 2
    return max(1, min(8, int(value)))


def audio_command(cfg: Any) -> list[str]:
    command = [
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
    ]
    if cfg.alsa_buffer_time_us is not None:
        command.extend(["--buffer-time", str(cfg.alsa_buffer_time_us)])
    if cfg.alsa_period_time_us is not None:
        command.extend(["--period-time", str(cfg.alsa_period_time_us)])
    if cfg.audio_device:
        command.extend(["-D", cfg.audio_device])
    return command


def playback_command(cfg: Any) -> list[str]:
    return playback_command_for_format(cfg, cfg.sample_rate, _playback_channels(cfg))


def playback_command_for_format(cfg: Any, sample_rate: int, channels: int | None = None) -> list[str]:
    sample_rate = max(8000, min(48000, int(sample_rate)))
    channels = _playback_channels(cfg) if channels is None else max(1, min(8, int(channels)))
    command = [
        "aplay",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
        "-t",
        "raw",
    ]
    if cfg.alsa_buffer_time_us is not None:
        command.extend(["--buffer-time", str(cfg.alsa_buffer_time_us)])
    if cfg.alsa_period_time_us is not None:
        command.extend(["--period-time", str(cfg.alsa_period_time_us)])
    if cfg.playback_device:
        command.extend(["-D", cfg.playback_device])
    return command


def speaker_volume(cfg: Any) -> int | None:
    card_args = _alsa_mixer_card_args(getattr(cfg, "playback_device", None))
    for control in ("Master", "PCM"):
        command = ["amixer", "-M", *card_args, "get", control]
        ok, output = command_output(command)
        if ok:
            volume = _parse_percent(output)
            if volume is not None:
                return volume
    return None


def set_playback_enabled(cfg: Any, enabled: bool) -> bool:
    card_args = _alsa_mixer_card_args(getattr(cfg, "playback_device", None))
    state = "unmute" if enabled else "mute"
    updated = False
    for control in ("PCM", "PCM,0", "PCM,1"):
        command = ["amixer", "-q", *card_args, "sset", control, state]
        if subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0:
            updated = True
    return updated


def set_speaker_volume(cfg: Any, volume: int) -> bool:
    clamped = max(0, min(100, int(volume)))
    volume_value = f"{clamped}%"
    card_args = _alsa_mixer_card_args(getattr(cfg, "playback_device", None))
    updated = False
    for control in ("Master", "PCM", "PCM,0", "PCM,1"):
        command = ["amixer", "-q", "-M", *card_args, "sset", control, volume_value]
        if subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0:
            updated = True
    return updated


def _alsa_mixer_card_args(device: str | None) -> list[str]:
    card = _alsa_card_from_device(device)
    return ["-c", card] if card else []


def _alsa_card_from_device(device: str | None) -> str | None:
    if not device:
        return None
    normalized = device.strip()
    match = re.search(r"(?:^|:)CARD=([^,]+)", normalized)
    if match:
        return match.group(1)
    match = re.match(r"(?:plug)?hw:([^,]+)", normalized)
    if match:
        return match.group(1)
    return None


def _parse_percent(output: str) -> int | None:
    match = re.search(r"(\d+)%", output)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def tone_pcm(
    sample_rate: int,
    duration_s: float = 0.8,
    frequency_hz: float = 880.0,
    channels: int = 1,
    amplitude: int = 9000,
) -> bytes:
    sample_count = int(sample_rate * duration_s)
    channel_count = max(1, channels)
    samples = bytearray(sample_count * channel_count * 2)
    for index in range(sample_count):
        envelope = _tone_envelope(index, sample_count, sample_rate)
        value = int(amplitude * envelope * math.sin(2 * math.pi * frequency_hz * index / sample_rate))
        sample = value.to_bytes(2, "little", signed=True)
        for channel in range(channel_count):
            offset = (index * channel_count + channel) * 2
            samples[offset : offset + 2] = sample
    return bytes(samples)


def earcon_pcm(name: str, sample_rate: int, channels: int = 1) -> bytes:
    cues: dict[str, list[tuple[float, float, int]]] = {
        "wake": [(0.08, 523.25, 6200), (0.11, 783.99, 6000)],
        "speaker_identified": [(0.06, 659.25, 5200), (0.06, 783.99, 5200), (0.10, 987.77, 4800)],
        "tool_started": [(0.07, 587.33, 6200), (0.09, 880.0, 6000)],
        "tool_running": [(0.045, 659.25, 4200)],
        "tool_failed": [(0.085, 392.0, 6000), (0.11, 311.13, 5600)],
    }
    parts = cues.get(name)
    if not parts:
        return b""
    silence = b"\x00" * int(sample_rate * max(1, channels) * 2 * 0.018)
    audio = bytearray()
    for index, (duration_s, frequency_hz, amplitude) in enumerate(parts):
        if index:
            audio.extend(silence)
        audio.extend(
            tone_pcm(
                sample_rate,
                duration_s=duration_s,
                frequency_hz=frequency_hz,
                channels=channels,
                amplitude=amplitude,
            )
        )
    return bytes(audio)


def _tone_envelope(index: int, sample_count: int, sample_rate: int) -> float:
    if sample_count <= 0:
        return 0.0
    fade_samples = max(1, min(sample_count // 2, int(sample_rate * 0.012)))
    attack = min(1.0, index / fade_samples)
    release = min(1.0, (sample_count - index - 1) / fade_samples)
    return max(0.0, min(attack, release))


def convert_pcm_channels(chunk: bytes, from_channels: int, to_channels: int) -> bytes:
    from_channels = max(1, min(8, int(from_channels)))
    to_channels = max(1, min(8, int(to_channels)))
    if from_channels == to_channels or len(chunk) < 2:
        return chunk

    samples = memoryview(chunk[: len(chunk) - (len(chunk) % 2)]).cast("h")
    frame_count = len(samples) // from_channels
    if frame_count <= 0:
        return b""

    output = bytearray(frame_count * to_channels * 2)
    output_samples = memoryview(output).cast("h")
    for frame_index in range(frame_count):
        input_offset = frame_index * from_channels
        output_offset = frame_index * to_channels
        if from_channels == 1:
            sample = int(samples[input_offset])
            for channel_index in range(to_channels):
                output_samples[output_offset + channel_index] = sample
            continue

        if to_channels == 1:
            total = 0
            for channel_index in range(from_channels):
                total += int(samples[input_offset + channel_index])
            output_samples[output_offset] = int(total / from_channels)
            continue

        for channel_index in range(to_channels):
            output_samples[output_offset + channel_index] = int(
                samples[input_offset + min(channel_index, from_channels - 1)]
            )
    return bytes(output)


def frame_size_bytes(cfg: Any) -> int:
    bytes_per_sample = 2
    return max(1, int(cfg.sample_rate * cfg.channels * bytes_per_sample * cfg.chunk_ms / 1000))


def pcm_rms(chunk: bytes) -> int:
    if len(chunk) < 2:
        return 0
    samples = memoryview(chunk[: len(chunk) - (len(chunk) % 2)]).cast("h")
    if len(samples) == 0:
        return 0
    total = 0
    for sample in samples:
        total += int(sample) * int(sample)
    return int((total / len(samples)) ** 0.5)


def pcm_channel_rms(chunk: bytes, channels: int) -> list[int]:
    channel_count = max(1, min(8, int(channels)))
    if len(chunk) < 2:
        return [0] * channel_count
    samples = memoryview(chunk[: len(chunk) - (len(chunk) % 2)]).cast("h")
    frame_count = len(samples) // channel_count
    if frame_count <= 0:
        return [0] * channel_count

    totals = [0] * channel_count
    for frame_index in range(frame_count):
        input_offset = frame_index * channel_count
        for channel_index in range(channel_count):
            sample = int(samples[input_offset + channel_index])
            totals[channel_index] += sample * sample
    return [int((total / frame_count) ** 0.5) for total in totals]


def audio_level_from_rms(rms: int) -> float:
    return round(min(1.0, rms / 6000), 3)


def command_output(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        return False, "command not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    output = (result.stdout or result.stderr).strip()
    return result.returncode == 0, output
