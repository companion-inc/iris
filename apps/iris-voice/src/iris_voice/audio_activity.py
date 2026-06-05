from __future__ import annotations

import audioop
import time
from collections.abc import Callable
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class InputAudioActivityRelay(FrameProcessor):
    def __init__(
        self,
        *,
        on_audio_activity: Callable[[dict[str, Any]], None],
        log_interval_seconds: float = 10.0,
    ):
        super().__init__()
        self._on_audio_activity = on_audio_activity
        self._log_interval_seconds = log_interval_seconds
        self._last_log_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputAudioRawFrame):
            rms = audioop.rms(frame.audio, 2) if frame.audio else 0
            self._on_audio_activity(
                {
                    "rms": rms,
                    "sampleRate": frame.sample_rate,
                    "channels": frame.num_channels,
                    "bytes": len(frame.audio),
                }
            )
            now = time.monotonic()
            if now - self._last_log_at >= self._log_interval_seconds:
                logger.info(
                    "iris.voice.input_audio_activity bytes={} sample_rate={} channels={} rms={}",
                    len(frame.audio),
                    frame.sample_rate,
                    frame.num_channels,
                    rms,
                )
                self._last_log_at = now
        await self.push_frame(frame, direction)
