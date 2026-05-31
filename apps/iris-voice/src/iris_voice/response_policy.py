from __future__ import annotations

import re

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .runtime_events import RuntimeEvents

NO_SPEECH_SENTINEL = "<NO_SPEECH>"
NORMALIZED_NO_SPEECH_SENTINEL = NO_SPEECH_SENTINEL.casefold()
UNSUPPORTED_SPEECH_TAG_RE = re.compile(r"</?speak\b[^>]*>", re.IGNORECASE)


def is_no_speech_response(text: str) -> bool:
    return text.strip().casefold() == NORMALIZED_NO_SPEECH_SENTINEL


def sanitize_response_text(text: str) -> str:
    return UNSUPPORTED_SPEECH_TAG_RE.sub("", text)


class NoSpeechResponseFilter(FrameProcessor):
    def __init__(self, events: RuntimeEvents):
        super().__init__()
        self._events = events
        self._checking_response = False
        self._pending_text_frames: list[TextFrame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TextFrame):
            frame.text = self._sanitize_text(frame.text)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._checking_response = True
            self._pending_text_frames.clear()
            await self.push_frame(frame, direction)
            return

        if self._checking_response and isinstance(frame, TextFrame):
            self._pending_text_frames.append(frame)
            buffered_text = self._buffered_text()
            stripped = buffered_text.strip()
            normalized = stripped.casefold()
            if (
                normalized == NORMALIZED_NO_SPEECH_SENTINEL
                or NORMALIZED_NO_SPEECH_SENTINEL.startswith(normalized)
            ):
                return

            await self._flush_pending(direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._checking_response and self._pending_text_frames:
                buffered_text = self._buffered_text()
                if is_no_speech_response(buffered_text):
                    self._pending_text_frames.clear()
                    self._checking_response = False
                    self._events.emit({"type": "assistant.no_speech"})
                    logger.info(
                        "iris.voice.no_speech_response session={} device={}",
                        self._events.session_id,
                        self._events.device_id,
                    )
                    await self.push_frame(frame, direction)
                    return
                await self._flush_pending(direction)

            self._checking_response = False
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    def _buffered_text(self) -> str:
        return "".join(frame.text for frame in self._pending_text_frames)

    async def _flush_pending(self, direction: FrameDirection) -> None:
        pending = list(self._pending_text_frames)
        self._pending_text_frames.clear()
        self._checking_response = False
        for pending_frame in pending:
            await self.push_frame(pending_frame, direction)

    def _sanitize_text(self, text: str) -> str:
        sanitized = sanitize_response_text(text)
        if sanitized != text:
            self._events.emit({"type": "assistant.unsupported_tag_stripped", "tag": "speak"})
            logger.info(
                "iris.voice.unsupported_speech_tag_stripped session={} device={} tag=speak",
                self._events.session_id,
                self._events.device_id,
            )
        return sanitized
