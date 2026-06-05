from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import WakePhraseUserTurnStartStrategy

from .playback_echo import PlaybackEchoGuard

WAKE_PHRASES = ["iris"]
WAKE_PATTERN = re.compile(r"\biris\b[\s,.:;!?-]*", re.IGNORECASE)
WAKE_RESIDUE_TEXT = {
    "end noise",
    "ellis",
    "ellinois",
    "i know it",
    "i know its",
    "i know it's",
}
LEADING_WAKE_PATTERN = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay)\b[\s,.:;!?-]*)?iris\b[\s,.:;!?-]*",
    re.IGNORECASE,
)
DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS = 12.0
SPEAKER_CONTEXT_PREFIX_PATTERN = re.compile(
    r"^\s*(?:SPEAKER_\d+|[A-Z][A-Za-z0-9_. -]{0,60})\s*:\s*",
    re.IGNORECASE,
)
EMBEDDED_WAKE_COMMAND_START_WORDS = {
    "am",
    "are",
    "can",
    "cancel",
    "check",
    "click",
    "compare",
    "copy",
    "could",
    "create",
    "delete",
    "did",
    "do",
    "does",
    "find",
    "go",
    "how",
    "is",
    "look",
    "make",
    "move",
    "open",
    "please",
    "read",
    "run",
    "search",
    "send",
    "set",
    "show",
    "start",
    "stop",
    "tell",
    "turn",
    "use",
    "was",
    "were",
    "what",
    "when",
    "where",
    "who",
    "why",
    "will",
    "would",
    "write",
}


def has_leading_wake_phrase(text: str) -> bool:
    return bool(LEADING_WAKE_PATTERN.search(text))


def has_transcription_wake_phrase(text: str) -> bool:
    stripped = strip_speaker_context_prefix(text)
    return (
        has_leading_wake_phrase(stripped)
        or has_embedded_wake_command(stripped)
        or is_likely_wake_residue(stripped)
    )


def has_embedded_wake_command(text: str) -> bool:
    return wake_command_text(text) is not None


def wake_command_text(text: str) -> str | None:
    stripped = strip_speaker_context_prefix(text).strip()
    if not stripped:
        return None
    leading_remainder = LEADING_WAKE_PATTERN.sub("", stripped, count=1).strip()
    if leading_remainder != stripped:
        return leading_remainder or None

    match = WAKE_PATTERN.search(stripped)
    if not match:
        return None
    suffix = stripped[match.end() :].strip()
    suffix_words = re.findall(r"\b[\w']+\b", suffix.casefold())
    if not suffix_words:
        return None
    if suffix_words[0] not in EMBEDDED_WAKE_COMMAND_START_WORDS:
        return None
    if len(suffix_words) < 2 and suffix_words[0] not in {"cancel", "stop"}:
        return None
    return suffix


def has_playback_interrupt_wake_phrase(text: str) -> bool:
    return bool(WAKE_PATTERN.search(strip_speaker_context_prefix(text)))


def is_wake_only_transcription(text: str) -> bool:
    stripped = strip_speaker_context_prefix(text).strip()
    remainder = stripped
    while True:
        next_remainder = LEADING_WAKE_PATTERN.sub("", remainder, count=1).strip()
        if next_remainder == remainder:
            break
        remainder = next_remainder
    return remainder == ""


def is_likely_wake_residue(text: str) -> bool:
    normalized = re.sub(r"[^\w\s']", "", strip_speaker_context_prefix(text).lower())
    normalized = " ".join(normalized.split())
    return normalized in WAKE_RESIDUE_TEXT or is_wake_only_transcription(text)


def strip_speaker_context_prefix(text: str) -> str:
    return SPEAKER_CONTEXT_PREFIX_PATTERN.sub("", text, count=1)


class IrisWakePhraseUserTurnStartStrategy(WakePhraseUserTurnStartStrategy):
    def __init__(
        self,
        *args,
        echo_guard: PlaybackEchoGuard | None = None,
        on_wake_only: Callable[[], Awaitable[None]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._stop_reason = "timeout"
        self._echo_guard = echo_guard
        self._on_wake_only = on_wake_only
        self._wake_residue_deadline = 0.0

    def mark_followup_expected(self, *, reason: str) -> None:
        if self.state.name != "AWAKE":
            self._transition_to_awake("iris")
        logger.info(
            "iris.voice.wake.followup_expected state={} reason={}",
            self.state.name,
            reason,
        )

    async def _process_idle(self, frame) -> ProcessFrameResult:
        if (
            self._echo_guard
            and isinstance(frame, TranscriptionFrame)
            and self._echo_guard.is_playback_echo(frame)
        ):
            logger.info(
                "iris.voice.wake.ignored_assistant_echo state=IDLE text={!r}",
                frame.text.strip(),
            )
            await self.trigger_reset_aggregation()
            return ProcessFrameResult.STOP

        if isinstance(frame, InterimTranscriptionFrame):
            if has_transcription_wake_phrase(frame.text):
                self._transition_to_awake("iris")
                self._wake_residue_deadline = time.monotonic() + 5.0
                if is_wake_only_transcription(frame.text):
                    logger.info(
                        "iris.voice.wake.only_detected final=false text={!r} has_ack_callback={}",
                        frame.text.strip(),
                        self._on_wake_only is not None,
                    )
                    if self._on_wake_only:
                        await self._on_wake_only()
                    await self.trigger_reset_aggregation()
                    return ProcessFrameResult.STOP
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            return ProcessFrameResult.STOP

        if isinstance(frame, TranscriptionFrame):
            if has_transcription_wake_phrase(frame.text):
                self._transition_to_awake("iris")
                self._wake_residue_deadline = time.monotonic() + 5.0
                if is_wake_only_transcription(frame.text):
                    logger.info(
                        "iris.voice.wake.only_detected final=true text={!r} has_ack_callback={}",
                        frame.text.strip(),
                        self._on_wake_only is not None,
                    )
                    if self._on_wake_only:
                        await self._on_wake_only()
                    await self.trigger_reset_aggregation()
                    return ProcessFrameResult.STOP
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            await self.trigger_reset_aggregation()
            return ProcessFrameResult.STOP

        return await super()._process_idle(frame)

    async def _process_awake(self, frame) -> ProcessFrameResult:
        if (
            isinstance(frame, TranscriptionFrame)
            and self._wake_residue_deadline
            and time.monotonic() <= self._wake_residue_deadline
            and is_likely_wake_residue(frame.text)
        ):
            logger.info(
                "iris.voice.wake.ignored_wake_residue state=AWAKE text={!r}",
                frame.text.strip(),
            )
            await self.trigger_reset_aggregation()
            return ProcessFrameResult.STOP

        if isinstance(frame, TranscriptionFrame):
            self._wake_residue_deadline = 0.0

        return await super()._process_awake(frame)

    async def force_idle(self, *, reason: str) -> bool:
        if self.state.name != "AWAKE":
            logger.info(
                "iris.voice.wake.force_idle_skipped state={} reason={}",
                self.state.name,
                reason,
            )
            return False

        self._stop_reason = reason
        self._transition_to_idle()
        logger.info("iris.voice.wake.force_idle state={} reason={}", self.state.name, reason)
        return True

    def consume_stop_reason(self) -> str:
        reason = self._stop_reason
        self._stop_reason = "timeout"
        return reason
