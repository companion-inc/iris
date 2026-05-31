from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class VoiceFrameDiagnostics(FrameProcessor):
    def __init__(self, *, label: str):
        super().__init__()
        self._label = label

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(
            frame,
            (
                VADUserStartedSpeakingFrame,
                VADUserStoppedSpeakingFrame,
                UserStartedSpeakingFrame,
                UserStoppedSpeakingFrame,
                InterruptionFrame,
            ),
        ):
            logger.info(
                "iris.voice.frame label={} frame={} direction={}",
                self._label,
                frame.__class__.__name__,
                direction.name,
            )
        await self.push_frame(frame, direction)
