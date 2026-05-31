from __future__ import annotations

import os
import time
from array import array

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


DEFAULT_TARGET_RMS = 6000
DEFAULT_MAX_GAIN = 12.0


class InputAudioAutoGain(FrameProcessor):
    def __init__(
        self,
        *,
        target_rms: int | None = None,
        max_gain: float | None = None,
        log_interval_seconds: float = 10.0,
    ):
        super().__init__()
        self._target_rms = target_rms if target_rms is not None else _int_env(
            "IRIS_INPUT_AUTO_GAIN_TARGET_RMS",
            DEFAULT_TARGET_RMS,
        )
        self._max_gain = max_gain if max_gain is not None else _float_env(
            "IRIS_INPUT_AUTO_GAIN_MAX",
            DEFAULT_MAX_GAIN,
        )
        self._log_interval_seconds = log_interval_seconds
        self._last_log_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, InputAudioRawFrame)
            and self._target_rms > 0
            and self._max_gain > 1.0
        ):
            amplified, rms_before, rms_after, gain = apply_auto_gain(
                frame.audio,
                target_rms=self._target_rms,
                max_gain=self._max_gain,
            )
            if gain > 1.0:
                frame = InputAudioRawFrame(
                    audio=amplified,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            now = time.monotonic()
            if now - self._last_log_at >= self._log_interval_seconds:
                logger.info(
                    "iris.voice.input_auto_gain rms_before={} rms_after={} gain={:.2f} target_rms={} max_gain={:.2f}",
                    rms_before,
                    rms_after,
                    gain,
                    self._target_rms,
                    self._max_gain,
                )
                self._last_log_at = now
        await self.push_frame(frame, direction)


def apply_auto_gain(
    audio: bytes,
    *,
    target_rms: int = DEFAULT_TARGET_RMS,
    max_gain: float = DEFAULT_MAX_GAIN,
) -> tuple[bytes, int, int, float]:
    rms_before = pcm_rms(audio)
    if rms_before <= 0 or target_rms <= 0 or max_gain <= 1.0 or rms_before >= target_rms:
        return audio, rms_before, rms_before, 1.0

    gain = min(max_gain, target_rms / rms_before)
    samples = array("h")
    samples.frombytes(audio[: len(audio) - (len(audio) % 2)])
    for index, sample in enumerate(samples):
        samples[index] = _clip_sample(int(round(int(sample) * gain)))
    amplified = samples.tobytes()
    if len(audio) % 2:
        amplified += audio[-1:]
    return amplified, rms_before, pcm_rms(amplified), gain


def pcm_rms(audio: bytes) -> int:
    if len(audio) < 2:
        return 0
    samples = array("h")
    samples.frombytes(audio[: len(audio) - (len(audio) % 2)])
    if not samples:
        return 0
    total = 0
    for sample in samples:
        total += int(sample) * int(sample)
    return int((total / len(samples)) ** 0.5)


def _clip_sample(value: int) -> int:
    return max(-32768, min(32767, value))


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
