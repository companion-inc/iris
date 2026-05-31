from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame


_WORD_PATTERN = re.compile(r"[a-z0-9]+")
DEFAULT_PLAYBACK_ECHO_TAIL_SECONDS = 3.0
DEFAULT_ASSISTANT_ECHO_MATCH_SECONDS = 20.0
_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}


def _normalize(text: str) -> str:
    return " ".join(_NUMBER_WORDS.get(word, word) for word in _WORD_PATTERN.findall(text.casefold()))


class PlaybackEchoGuard:
    def __init__(
        self,
        *,
        playback_active: Callable[[], bool],
        echo_tail_seconds: float = DEFAULT_PLAYBACK_ECHO_TAIL_SECONDS,
        assistant_match_seconds: float = DEFAULT_ASSISTANT_ECHO_MATCH_SECONDS,
    ):
        self._playback_active = playback_active
        self._assistant_text = ""
        self._echo_speakers: set[str] = set()
        self._echo_tail_seconds = max(0.0, echo_tail_seconds)
        self._assistant_match_seconds = max(self._echo_tail_seconds, assistant_match_seconds)
        self._last_playback_seen_at = 0.0
        self._last_assistant_text_at = 0.0

    def playback_active(self) -> bool:
        active = self._playback_active()
        if active:
            self._last_playback_seen_at = time.monotonic()
        return active

    def in_playback_echo_window(self) -> bool:
        if self.playback_active():
            return True
        return (
            self._last_playback_seen_at > 0
            and (time.monotonic() - self._last_playback_seen_at) <= self._echo_tail_seconds
        )

    def append_assistant_text(self, text: str) -> None:
        normalized = _normalize(text)
        if not normalized:
            return
        self._assistant_text = f"{self._assistant_text} {normalized}".strip()[-3000:]
        self._last_assistant_text_at = time.monotonic()

    def reset_assistant_text(self) -> None:
        self._assistant_text = ""
        self._echo_speakers.clear()

    def is_playback_echo(
        self,
        frame: InterimTranscriptionFrame | TranscriptionFrame,
    ) -> bool:
        in_echo_window = self.in_playback_echo_window()
        if not in_echo_window:
            self._echo_speakers.clear()

        transcript = _normalize(frame.text)
        if not transcript:
            return False

        speaker = _speaker_label(frame)
        if speaker and speaker in self._echo_speakers:
            return True

        if _is_wake_only(transcript):
            return False

        if self._matches_assistant_text(transcript):
            if speaker:
                self._echo_speakers.add(speaker)
            return True

        return False

    def _matches_assistant_text(self, transcript: str) -> bool:
        if not self._assistant_text:
            return False
        if (time.monotonic() - self._last_assistant_text_at) > self._assistant_match_seconds:
            return False
        if transcript in self._assistant_text:
            return True

        transcript_words = transcript.split()
        if len(transcript_words) < 2:
            return False

        assistant_words = set(self._assistant_text.split())
        overlap = sum(1 for word in transcript_words if word in assistant_words)
        return overlap / len(transcript_words) >= 0.8


def _speaker_label(frame: InterimTranscriptionFrame | TranscriptionFrame) -> str | None:
    result = getattr(frame, "result", None)
    channel = getattr(result, "channel", None)
    alternatives = getattr(channel, "alternatives", None)
    if not alternatives:
        return None
    words = getattr(alternatives[0], "words", None) or []
    for word in words:
        speaker = _word_value(word, "speaker")
        if isinstance(speaker, bool) or speaker is None:
            continue
        if isinstance(speaker, int):
            return f"SPEAKER_{speaker}"
        if isinstance(speaker, float) and speaker.is_integer():
            return f"SPEAKER_{int(speaker)}"
        if isinstance(speaker, str) and speaker.strip().isdigit():
            return f"SPEAKER_{speaker.strip()}"
    return None


def _is_wake_only(transcript: str) -> bool:
    return transcript == "iris"


def _word_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict) and key in dumped:
            return dumped.get(key)
    return getattr(value, key, None)
