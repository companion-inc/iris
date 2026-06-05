from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .env import debug_transcript_text
from .runtime_events import RuntimeEvents
from .speech_emotion import SpeechEmotionClassifier
from .speaker_identity import SpeakerAudioBuffer, SpeakerIdentityMatcher
from .transcript_types import TranscriptWord
from .turns.playback_echo import PlaybackEchoGuard
from .turns.wake import (
    WAKE_PATTERN,
    has_transcription_wake_phrase,
    is_likely_wake_residue,
    wake_command_text,
)

SPEAKER_ID_MIN_EVIDENCE_SECONDS = 2.5
SPEAKER_ID_MAX_EVIDENCE_SECONDS = 8.0
DEFAULT_WAKE_CONTEXT_SECONDS = 300.0
DEFAULT_WAKE_CONTEXT_MAX_CHARS = 1400
POST_WAKE_MIN_FINAL_WORDS = 3


def transcription_word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text))


class TranscriptRelay(FrameProcessor):
    def __init__(
        self,
        events: RuntimeEvents,
        *,
        playback_active: Callable[[], bool] | None = None,
        playback_echo_guard: PlaybackEchoGuard | None = None,
        audio_buffer: SpeakerAudioBuffer | None = None,
        speaker_matcher: SpeakerIdentityMatcher | None = None,
        speech_emotion: SpeechEmotionClassifier | None = None,
    ):
        super().__init__()
        self._events = events
        self._playback_active = playback_active or (lambda: False)
        self._playback_echo_guard = playback_echo_guard
        self._audio_buffer = audio_buffer
        self._speaker_matcher = speaker_matcher
        self._speech_emotion = speech_emotion
        self._speaker_identities: dict[str, dict[str, Any]] = {}
        self._speaker_audio_evidence: dict[str, bytearray] = {}
        self._speaker_audio_rates: dict[str, tuple[int, int]] = {}
        self._speaker_identity_tasks: dict[str, asyncio.Task[None]] = {}
        self._speaker_pending_segments: dict[str, dict[str, dict[str, Any]]] = {}
        self._post_wake_turn_pending = False
        self._post_wake_fragments: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (InterimTranscriptionFrame, TranscriptionFrame)
        ):
            text = frame.text.strip()
            if text:
                is_final = isinstance(frame, TranscriptionFrame)
                words, confidence = deepgram_words_and_confidence(frame)
                speaker = speaker_label_for_words(words)
                wake_detected = has_transcription_wake_phrase(text)
                block_llm_for_playback = self._playback_active() and not wake_detected
                self._events.emit(
                    {
                        "type": "transcript.final" if is_final else "transcript.interim",
                        "text": text,
                        "final": is_final,
                        "wakeDetected": wake_detected,
                        "speaker": speaker,
                        "words": words,
                        "confidence": confidence,
                    }
                )
                if self._playback_echo_guard and self._playback_echo_guard.is_playback_echo(frame):
                    logger.info(
                        "iris.voice.transcript_filtered_playback_echo final={} session={} device={} speaker={} words={} chars={} wake_candidate={} text={!r}",
                        is_final,
                        self._events.session_id,
                        self._events.device_id,
                        speaker,
                        len(words),
                        len(text),
                        wake_detected,
                        debug_transcript_text(text),
                    )
                    return
                if is_final:
                    context_parts: list[str] = []
                    for group in speaker_word_groups(words, text):
                        logger.info(
                            "iris.voice.transcript final=true session={} device={} speaker={} words={} chars={} wake_candidate={} text={!r}",
                            self._events.session_id,
                            self._events.device_id,
                            group["speaker"],
                            len(group["words"]),
                            len(group["text"]),
                            has_transcription_wake_phrase(group["text"]),
                            debug_transcript_text(group["text"]),
                        )
                        speaker_label = group.get("speaker")
                        identity = (
                            self._speaker_identities.get(speaker_label)
                            if isinstance(speaker_label, str)
                            else None
                        )
                        segment_id = transcript_segment_id(self._events.session_id, group)
                        segment_record = {
                            "text": group["text"],
                            "speaker": group["speaker"],
                            "segment_id": segment_id,
                            "words": group["words"],
                            "confidence": confidence,
                        }
                        context_parts.append(
                            speaker_context_text(
                                group["text"],
                                group["speaker"],
                                identity,
                            )
                        )
                        self._ingest_segment(segment_record, identity=identity)
                        self._schedule_speech_emotion(group, segment_record)
                        if isinstance(speaker_label, str) and not identity and segment_id:
                            self._speaker_pending_segments.setdefault(speaker_label, {})[segment_id] = (
                                segment_record
                            )
                        self._schedule_speaker_identity(group)
                    if block_llm_for_playback:
                        logger.info(
                            "iris.voice.transcript_blocked_from_llm_playback_active final=true session={} device={} speaker={} words={} chars={} text={!r}",
                            self._events.session_id,
                            self._events.device_id,
                            speaker,
                            len(words),
                            len(text),
                            debug_transcript_text(text),
                        )
                        return
                    current_plain_text = text
                    context_text = current_plain_text
                    if context_text.strip():
                        if is_wake_only_text(current_plain_text):
                            self._post_wake_turn_pending = True
                            self._post_wake_fragments = []
                            wake_context = None
                        else:
                            local_post_wake_turn = self._post_wake_turn_pending
                            if local_post_wake_turn:
                                self._post_wake_fragments.append(current_plain_text)
                                accumulated_text = " ".join(self._post_wake_fragments).strip()
                                if (
                                    transcription_word_count(accumulated_text)
                                    < POST_WAKE_MIN_FINAL_WORDS
                                ):
                                    wake_context = None
                                else:
                                    self._post_wake_turn_pending = False
                                    self._post_wake_fragments = []
                                    context_text = accumulated_text
                                    wake_context = self._events.consume_wake_context(
                                        lookback_seconds=wake_context_seconds(),
                                        max_chars=wake_context_max_chars(),
                                    )
                                    if wake_context is None:
                                        wake_context = ""
                            else:
                                wake_context = self._events.consume_wake_context(
                                    lookback_seconds=wake_context_seconds(),
                                    max_chars=wake_context_max_chars(),
                                )
                        if wake_context is not None:
                            post_wake_context = (
                                "Iris just accepted a wake phrase. Treat the current user turn "
                                "as the follow-on command directed to Iris. If it is incomplete "
                                "or unclear, ask a short clarification instead of using noop."
                            )
                            if wake_context.strip():
                                post_wake_context = (
                                    f"{post_wake_context}\n\n"
                                    "Recent room context before the wake phrase. "
                                    "This is background only, not the current request:\n"
                                    f"{wake_context}"
                                )
                            context_text = f"{post_wake_context}\n\nCurrent user turn:\n{context_text}"
                        self._events.remember_user_turn_context(context_text)
                        frame = TranscriptionFrame(
                            context_text,
                            user_id=frame.user_id,
                            timestamp=frame.timestamp,
                            language=frame.language,
                            result=frame.result,
                            finalized=getattr(frame, "finalized", False),
                        )
                else:
                    logger.info(
                        "iris.voice.transcript final=false session={} device={} speaker={} words={} chars={} wake_candidate={} text={!r}",
                        self._events.session_id,
                        self._events.device_id,
                        speaker,
                        len(words),
                        len(text),
                        has_transcription_wake_phrase(text),
                        debug_transcript_text(text),
                    )
                    self._events.ingest_transcript(
                        text,
                        False,
                        speaker=speaker,
                        segment_id=f"interim-{self._events.session_id}",
                        words=words,
                        confidence=confidence,
                    )
                    if block_llm_for_playback:
                        logger.info(
                            "iris.voice.transcript_blocked_from_llm_playback_active final=false session={} device={} speaker={} words={} chars={} text={!r}",
                            self._events.session_id,
                            self._events.device_id,
                            speaker,
                            len(words),
                            len(text),
                            debug_transcript_text(text),
                        )
                        return
        await self.push_frame(frame, direction)

    def _schedule_speaker_identity(self, group: dict[str, Any]) -> None:
        speaker_label = group.get("speaker")
        if not isinstance(speaker_label, str) or speaker_label in self._speaker_identities:
            return
        if not self._audio_buffer or not self._speaker_matcher or not self._speaker_matcher.enabled:
            return
        audio_slice = self._audio_buffer.slice(group.get("startedAt"), group.get("endedAt"))
        if not audio_slice:
            return
        audio, sample_rate, channels = audio_slice
        audio_for_identity = self._speaker_identity_audio(
            speaker_label,
            audio,
            sample_rate=sample_rate,
            channels=channels,
        )
        if not audio_for_identity:
            return
        task = self._speaker_identity_tasks.get(speaker_label)
        if task and not task.done():
            return
        self._speaker_identity_tasks[speaker_label] = asyncio.create_task(
            self._identify_speaker_from_audio(
                speaker_label,
                audio_for_identity,
                sample_rate=sample_rate,
                channels=channels,
            )
        )

    def _schedule_speech_emotion(self, group: dict[str, Any], record: dict[str, Any]) -> None:
        if not self._audio_buffer or not self._speech_emotion or not self._speech_emotion.enabled:
            return
        segment_id = record.get("segment_id")
        if not isinstance(segment_id, str):
            return
        if is_wake_only_text(str(record.get("text") or "")):
            return
        audio_slice = self._audio_buffer.slice(group.get("startedAt"), group.get("endedAt"))
        if not audio_slice:
            return
        audio, sample_rate, channels = audio_slice
        asyncio.create_task(
            self._classify_segment_emotion(
                dict(record),
                audio,
                sample_rate=sample_rate,
                channels=channels,
            )
        )

    async def _classify_segment_emotion(
        self,
        record: dict[str, Any],
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> None:
        if not self._speech_emotion:
            return
        segment_id = record.get("segment_id") if isinstance(record.get("segment_id"), str) else None
        result = await self._speech_emotion.classify(
            audio,
            sample_rate=sample_rate,
            channels=channels,
            segment_id=segment_id,
        )
        if not result:
            return
        self._events.ingest_transcript(
            str(record["text"]),
            True,
            speaker=record.get("speaker") if isinstance(record.get("speaker"), str) else None,
            segment_id=segment_id,
            words=record.get("words") if isinstance(record.get("words"), list) else None,
            confidence=record.get("confidence") if isinstance(record.get("confidence"), (int, float)) else None,
            emotion_label=result.label,
            emotion_confidence=result.confidence,
            emotion_model=result.model,
        )

    async def _identify_speaker_from_audio(
        self,
        speaker_label: str,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> None:
        if not self._speaker_matcher:
            return
        try:
            result = await self._speaker_matcher.identify(audio, sample_rate=sample_rate, channels=channels)
        finally:
            task = self._speaker_identity_tasks.get(speaker_label)
            if task is asyncio.current_task():
                self._speaker_identity_tasks.pop(speaker_label, None)
        if not result:
            return
        self._speaker_identities[speaker_label] = result
        if result:
            self._events.emit(
                {
                    "type": "speaker.identified",
                    "userId": result.get("userId"),
                    "displayName": result.get("displayName"),
                    "score": result.get("score"),
                }
            )
            logger.info(
                "iris.voice.speaker_identified session={} device={} user={} score={}",
                self._events.session_id,
                self._events.device_id,
                result.get("userId"),
                result.get("score"),
            )
        pending = self._speaker_pending_segments.pop(speaker_label, {})
        for record in pending.values():
            self._ingest_segment(record, identity=result)

    def _speaker_identity_audio(
        self,
        speaker_label: str,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> bytes | None:
        previous_rate = self._speaker_audio_rates.get(speaker_label)
        if previous_rate != (sample_rate, channels):
            self._speaker_audio_evidence[speaker_label] = bytearray()
            self._speaker_audio_rates[speaker_label] = (sample_rate, channels)
        evidence = self._speaker_audio_evidence.setdefault(speaker_label, bytearray())
        evidence.extend(audio)
        bytes_per_second = sample_rate * channels * 2
        max_bytes = int(SPEAKER_ID_MAX_EVIDENCE_SECONDS * bytes_per_second)
        if len(evidence) > max_bytes:
            del evidence[: len(evidence) - max_bytes]
        if len(evidence) < int(SPEAKER_ID_MIN_EVIDENCE_SECONDS * bytes_per_second):
            logger.info(
                "iris.voice.speaker_identity_waiting session={} device={} speaker={} evidence_seconds={:.2f}",
                self._events.session_id,
                self._events.device_id,
                speaker_label,
                len(evidence) / bytes_per_second,
            )
            return None
        return bytes(evidence)

    def _ingest_segment(
        self,
        record: dict[str, Any],
        *,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self._events.ingest_transcript(
            str(record["text"]),
            True,
            speaker=record.get("speaker") if isinstance(record.get("speaker"), str) else None,
            segment_id=record.get("segment_id") if isinstance(record.get("segment_id"), str) else None,
            words=record.get("words") if isinstance(record.get("words"), list) else None,
            confidence=record.get("confidence") if isinstance(record.get("confidence"), (int, float)) else None,
            speaker_user_id=identity.get("userId") if identity else None,
            speaker_confidence=identity.get("score") if identity else None,
        )
        text = str(record["text"])
        self._events.remember_transcript_context(
            text=text,
            speaker=record.get("speaker") if isinstance(record.get("speaker"), str) else None,
            speaker_user_id=identity.get("userId") if identity else None,
            speaker_display_name=identity.get("displayName") if identity else None,
            wake_detected=has_transcription_wake_phrase(text),
        )


def deepgram_words_and_confidence(
    frame: InterimTranscriptionFrame | TranscriptionFrame,
) -> tuple[list[TranscriptWord], float | None]:
    alternative = None
    result = getattr(frame, "result", None)
    channel = getattr(result, "channel", None)
    alternatives = getattr(channel, "alternatives", None)
    if alternatives:
        alternative = alternatives[0]
    raw_words = getattr(alternative, "words", None) or []
    words: list[TranscriptWord] = []
    for raw_word in raw_words:
        text = raw_word_value(raw_word, "word")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = raw_word_value(raw_word, "speaker")
        if speaker is None:
            logger.info(
                "iris.voice.deepgram_word_missing_speaker type={} fields={} has_model_dump={}",
                type(raw_word).__name__,
                raw_word_fields(raw_word),
                hasattr(raw_word, "model_dump"),
            )
        words.append(
            {
                "text": text,
                "start": numeric_or_none(raw_word_value(raw_word, "start")),
                "end": numeric_or_none(raw_word_value(raw_word, "end")),
                "speaker": speaker_index_or_none(speaker),
            }
        )
    return words, numeric_or_none(raw_word_value(alternative, "confidence"))


def raw_word_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict) and key in dumped:
            return dumped.get(key)
    return getattr(value, key, None)


def numeric_or_none(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def raw_word_fields(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value.keys())
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return sorted(str(key) for key in dumped.keys())
    if hasattr(value, "__dict__"):
        return sorted(str(key) for key in value.__dict__.keys())
    return []


def speaker_index_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def speaker_label_for_words(words: list[TranscriptWord]) -> str | None:
    for word in words:
        speaker = word.get("speaker")
        if isinstance(speaker, int):
            return f"SPEAKER_{speaker}"
    return None


def speaker_context_text(
    text: str,
    speaker: str | None,
    identity: dict[str, Any] | None,
) -> str:
    display_name = identity.get("displayName") if identity else None
    if isinstance(display_name, str) and display_name.strip():
        return f"{display_name.strip()}: {text}"
    if speaker:
        return f"{speaker}: {text}"
    return text


def wake_context_seconds() -> float:
    value = os.getenv("IRIS_WAKE_CONTEXT_SECONDS")
    if not value:
        return DEFAULT_WAKE_CONTEXT_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_WAKE_CONTEXT_SECONDS
    return max(30.0, min(600.0, parsed))


def wake_context_max_chars() -> int:
    value = os.getenv("IRIS_WAKE_CONTEXT_MAX_CHARS")
    if not value:
        return DEFAULT_WAKE_CONTEXT_MAX_CHARS
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_WAKE_CONTEXT_MAX_CHARS
    return max(300, min(3000, parsed))


def is_wake_only_text(text: str) -> bool:
    return is_likely_wake_residue(text) or not WAKE_PATTERN.sub("", text).strip()


def transcript_segment_id(session_id: str, group: dict[str, Any]) -> str | None:
    start = group.get("startedAt")
    end = group.get("endedAt")
    speaker = group.get("speaker")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    speaker_part = speaker if isinstance(speaker, str) else "speaker"
    return f"seg_{session_id}_{speaker_part}_{int(start * 1000)}_{int(end * 1000)}"


def speaker_word_groups(words: list[TranscriptWord], fallback_text: str) -> list[dict[str, Any]]:
    if not words:
        return [
            {
                "speaker": None,
                "text": fallback_text,
                "words": None,
                "startedAt": None,
                "endedAt": None,
            }
        ]

    groups: list[dict[str, Any]] = []
    current_speaker: int | None = None
    current_words: list[TranscriptWord] = []

    def flush() -> None:
        if not current_words:
            return
        groups.append(
            {
                "speaker": f"SPEAKER_{current_speaker}" if current_speaker is not None else None,
                "text": " ".join(str(word["text"]) for word in current_words).strip(),
                "words": [*current_words],
                "startedAt": current_words[0].get("start"),
                "endedAt": current_words[-1].get("end"),
            }
        )

    for word in words:
        normalized_speaker = speaker_index_or_none(word.get("speaker"))
        if current_words and normalized_speaker != current_speaker:
            flush()
            current_words = []
        current_speaker = normalized_speaker
        current_words.append(word)

    flush()
    return groups
