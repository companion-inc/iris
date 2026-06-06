from __future__ import annotations

from collections import deque
from typing import Deque

from loguru import logger
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    StartFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class VADSpeechAudioGate(FrameProcessor):
    """Forward audio to STT only during local VAD speech windows."""

    def __init__(self, *, preroll_seconds: float = 1.0):
        super().__init__()
        self._preroll_seconds = max(0.0, preroll_seconds)
        self._speaking = False
        self._sample_rate = 16_000
        self._channels = 1
        self._bytes_per_second = self._sample_rate * self._channels * 2
        self._preroll: Deque[AudioRawFrame] = deque()
        self._preroll_bytes = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, StartFrame):
            self._sample_rate = frame.audio_in_sample_rate or self._sample_rate
            self._bytes_per_second = self._sample_rate * self._channels * 2
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            logger.info(
                "iris.voice.stt_audio_gate.open preroll_frames={} preroll_bytes={}",
                len(self._preroll),
                self._preroll_bytes,
            )
            while self._preroll:
                await self.push_frame(self._preroll.popleft(), direction)
            self._preroll_bytes = 0
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
            self._preroll.clear()
            self._preroll_bytes = 0
            logger.info("iris.voice.stt_audio_gate.closed")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, AudioRawFrame):
            if self._speaking:
                await self.push_frame(frame, direction)
            else:
                self._append_preroll(frame)
            return

        await self.push_frame(frame, direction)

    def _append_preroll(self, frame: AudioRawFrame) -> None:
        if self._preroll_seconds <= 0:
            return
        self._preroll.append(frame)
        self._preroll_bytes += len(frame.audio)
        max_bytes = int(self._bytes_per_second * self._preroll_seconds)
        while self._preroll and self._preroll_bytes > max_bytes:
            removed = self._preroll.popleft()
            self._preroll_bytes -= len(removed.audio)
