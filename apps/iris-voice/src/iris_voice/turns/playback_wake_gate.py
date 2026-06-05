from __future__ import annotations

from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import BaseUserTurnStartStrategy

from .playback_echo import PlaybackEchoGuard
from .wake import has_playback_interrupt_wake_phrase


class PlaybackWakeGateUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Only the wake phrase can start an interrupting user turn during playback."""

    def __init__(
        self,
        *,
        playback_active: Callable[[], bool],
        echo_guard: PlaybackEchoGuard,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._playback_active = playback_active
        self._echo_guard = echo_guard

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        await super().process_frame(frame)

        if not self._playback_active():
            return ProcessFrameResult.CONTINUE

        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
            text = frame.text.strip()
            if has_playback_interrupt_wake_phrase(text):
                logger.info(
                    "iris.voice.playback_wake_gate interrupt=true final={} text={!r}",
                    isinstance(frame, TranscriptionFrame),
                    text,
                )
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP

            if self._echo_guard.is_playback_echo(frame):
                logger.info(
                    "iris.voice.playback_wake_gate ignored_assistant_echo final={} text={!r}",
                    isinstance(frame, TranscriptionFrame),
                    text,
                )
                if isinstance(frame, TranscriptionFrame):
                    await self.trigger_reset_aggregation()
                return ProcessFrameResult.STOP

            if isinstance(frame, TranscriptionFrame):
                logger.info(
                    "iris.voice.playback_wake_gate ignored_non_wake final=true text={!r}",
                    text,
                )
                await self.trigger_reset_aggregation()
            return ProcessFrameResult.STOP

        if isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            logger.debug(
                "iris.voice.playback_wake_gate blocked_vad frame={}",
                frame.__class__.__name__,
            )
            return ProcessFrameResult.STOP

        return ProcessFrameResult.CONTINUE
