from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame, LLMMessagesAppendFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .api_client import post_sound_recognition_event
from .env import optional_bool_env, optional_float_env, optional_int_env
from .llm_events import developer_event_message
from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext

DEFAULT_LOG_SOUND_THRESHOLD = 0.65


@dataclass(frozen=True)
class SoundRecognitionWatch:
    label: str
    threshold: float | None = None
    behavior: str = "log"
    prompt: str | None = None
    enabled: bool = True

    def public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "behavior": self.behavior,
            "enabled": self.enabled,
        }
        if self.threshold is not None:
            payload["threshold"] = self.threshold
        if self.prompt:
            payload["prompt"] = self.prompt
        return payload


def default_sound_recognition_watches() -> list[SoundRecognitionWatch]:
    return [
        SoundRecognitionWatch(label="doorbell", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="knock", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="smoke alarm", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="fire alarm", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="glass breaking", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="dog bark", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="baby cry", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="beep", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(label="siren", threshold=DEFAULT_LOG_SOUND_THRESHOLD),
        SoundRecognitionWatch(
            label="sneeze",
            threshold=0.75,
            behavior="prompt",
            prompt='Say "bless you."',
            enabled=True,
        )
    ]


def _normalized_label(label: str) -> str:
    return " ".join(label.casefold().split())


class SoundRecognitionState:
    def __init__(self, *, enabled: bool, watches: list[SoundRecognitionWatch]):
        self._enabled = enabled
        self._watches: dict[str, SoundRecognitionWatch] = {
            _normalized_label(watch.label): watch
            for watch in watches
            if _normalized_label(watch.label)
        }

    @classmethod
    def from_config(cls, config: Any) -> "SoundRecognitionState":
        if not isinstance(config, dict):
            return cls(enabled=True, watches=default_sound_recognition_watches())
        enabled = config.get("enabled")
        return cls(
            enabled=enabled if isinstance(enabled, bool) else True,
            watches=default_sound_recognition_watches(),
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_active(self) -> bool:
        return self._enabled and bool(self._watches)

    def labels(self) -> list[str]:
        if not self._enabled:
            return []
        return [watch.label for watch in self._watches.values() if watch.enabled]

    def watch_for(self, label: str) -> SoundRecognitionWatch | None:
        watch = self._watches.get(_normalized_label(label))
        return watch if watch and watch.enabled else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "watches": [watch.public() for watch in self._watches.values()],
        }


def _mono_pcm16le(audio: bytes, channels: int) -> bytes:
    channel_count = max(1, int(channels))
    if channel_count == 1:
        return audio
    frame_width = channel_count * 2
    usable = len(audio) - (len(audio) % frame_width)
    output = bytearray(usable // channel_count)
    out_index = 0
    for index in range(0, usable, frame_width):
        total = 0
        for channel in range(channel_count):
            total += int.from_bytes(audio[index + channel * 2 : index + channel * 2 + 2], "little", signed=True)
        mixed = int(max(-32768, min(32767, round(total / channel_count))))
        output[out_index : out_index + 2] = mixed.to_bytes(2, "little", signed=True)
        out_index += 2
    return bytes(output)


class SoundRecognitionRelay(FrameProcessor):
    def __init__(
        self,
        *,
        events: RuntimeEvents,
        session: VoiceSessionContext,
        state: SoundRecognitionState,
    ):
        super().__init__()
        self._events = events
        self._session = session
        self._state = state
        self._enabled = optional_bool_env("IRIS_SOUND_RECOGNITION_ENABLED", False)
        self._url = os.getenv("IRIS_SOUND_RECOGNITION_URL", "").rstrip("/")
        self._window_secs = optional_float_env("IRIS_SOUND_RECOGNITION_WINDOW_SECONDS", 2.0)
        self._interval_secs = optional_float_env("IRIS_SOUND_RECOGNITION_INTERVAL_SECONDS", 0.75)
        self._timeout_secs = optional_float_env("IRIS_SOUND_RECOGNITION_TIMEOUT_SECONDS", 1.5)
        self._persist_cooldown_secs = optional_float_env("IRIS_SOUND_RECOGNITION_PERSIST_COOLDOWN_SECONDS", 5.0)
        self._prompt_cooldown_secs = optional_float_env("IRIS_SOUND_RECOGNITION_PROMPT_COOLDOWN_SECONDS", 15.0)
        self._prompt_quiet_secs = max(
            0.0,
            optional_float_env("IRIS_SOUND_RECOGNITION_PROMPT_QUIET_SECONDS", 0.6),
        )
        self._max_buffer_seconds = max(
            self._window_secs,
            optional_float_env("IRIS_SOUND_RECOGNITION_MAX_BUFFER_SECONDS", 4.0),
        )
        self._max_in_flight = max(1, optional_int_env("IRIS_SOUND_RECOGNITION_MAX_IN_FLIGHT", 1))
        self._buffer = bytearray()
        self._sample_rate = session.sample_rate
        self._last_detect_at = 0.0
        self._in_flight = 0
        self._window_count = 0
        self._last_persisted_by_label: dict[str, float] = {}
        self._last_prompted_by_label: dict[str, float] = {}

        if self._enabled and not self._url:
            logger.warning("iris.voice.sound_recognition_disabled reason=missing_url")
            self._enabled = False
        logger.info(
            "iris.voice.sound_recognition_config enabled={} active={} url={} window_secs={} interval_secs={} labels={}",
            self._enabled,
            self._state.is_active(),
            bool(self._url),
            self._window_secs,
            self._interval_secs,
            self._state.labels(),
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._enabled and direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputAudioRawFrame):
            self._observe_audio(frame)
        await self.push_frame(frame, direction)

    def _observe_audio(self, frame: InputAudioRawFrame) -> None:
        labels = self._state.labels()
        if not labels:
            return
        if frame.sample_rate <= 0:
            return
        self._sample_rate = frame.sample_rate
        self._buffer.extend(_mono_pcm16le(frame.audio, frame.num_channels))
        max_bytes = int(self._sample_rate * 2 * self._max_buffer_seconds)
        if len(self._buffer) > max_bytes:
            del self._buffer[: len(self._buffer) - max_bytes]
        now = time.monotonic()
        window_bytes = int(self._sample_rate * 2 * self._window_secs)
        if len(self._buffer) < window_bytes:
            return
        if now - self._last_detect_at < self._interval_secs:
            return
        if self._in_flight >= self._max_in_flight:
            return
        self._last_detect_at = now
        self._window_count += 1
        window = bytes(self._buffer[-window_bytes:])
        window_id = f"{self._session.session_id}-{self._window_count}"
        self._in_flight += 1
        asyncio.create_task(self._classify_window(window, window_id, labels))

    async def _classify_window(self, audio: bytes, window_id: str, labels: list[str]) -> None:
        try:
            result = await asyncio.to_thread(self._post_window, audio, window_id, labels)
            matches = result.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if isinstance(match, dict):
                        await self._handle_match(match, window_id)
        except Exception as error:
            logger.warning(
                "iris.voice.sound_recognition_failed error={}: {}",
                type(error).__name__,
                error,
            )
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def _post_window(self, audio: bytes, window_id: str, labels: list[str]) -> dict[str, Any]:
        payload = {
            "audio": base64.b64encode(audio).decode("ascii"),
            "sampleRate": self._sample_rate,
            "channels": 1,
            "sessionId": self._session.session_id,
            "deviceId": self._session.device_id,
            "windowId": window_id,
            "labels": labels,
        }
        request = urllib.request.Request(
            f"{self._url}/v1/sound-recognition/classify",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout_secs) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    async def _handle_match(self, match: dict[str, Any], window_id: str) -> None:
        label = str(match.get("label") or "").strip()
        if not label:
            return
        watch = self._state.watch_for(label)
        if watch is None:
            return
        confidence = match.get("confidence")
        confidence_value = float(confidence) if isinstance(confidence, (int, float)) else None
        if confidence_value is not None and watch.threshold is not None and confidence_value < watch.threshold:
            return
        model = str(match.get("model") or "")
        now = time.monotonic()
        self._events.emit(
            {
                "type": "sound.recognition.detected",
                "label": label,
                "confidence": confidence_value,
                "behavior": watch.behavior,
                "prompt": watch.prompt,
                "model": model,
                "windowId": window_id,
            }
        )
        logger.info(
            "iris.voice.sound_recognition_detected session={} device={} label={} confidence={} behavior={} model={} window={}",
            self._session.session_id,
            self._session.device_id,
            label,
            confidence_value,
            watch.behavior,
            model,
            window_id,
        )
        if self._should_persist(label, now):
            self._last_persisted_by_label[label.casefold()] = now
            segment_label = "-".join(label.casefold().split())
            await post_sound_recognition_event(
                self._session,
                label=label,
                confidence=confidence_value,
                segment_id=f"sound-{window_id}-{segment_label}",
            )
        if watch.behavior == "prompt" and self._should_prompt(label, now):
            self._last_prompted_by_label[label.casefold()] = now
            await self._trigger_prompt(label, confidence_value, watch, window_id)

    def _should_persist(self, label: str, now: float) -> bool:
        return now - self._last_persisted_by_label.get(label.casefold(), 0.0) >= self._persist_cooldown_secs

    def _should_prompt(self, label: str, now: float) -> bool:
        return now - self._last_prompted_by_label.get(label.casefold(), 0.0) >= self._prompt_cooldown_secs

    async def _trigger_prompt(
        self,
        label: str,
        confidence: float | None,
        watch: SoundRecognitionWatch,
        window_id: str,
    ) -> None:
        instruction = watch.prompt or f"Respond appropriately to the detected {label} sound."
        payload = {
            "type": "sound.recognition",
            "label": label,
            "confidence": confidence,
            "instruction": instruction,
            "windowId": window_id,
        }
        self._events.emit(
            {
                "type": "sound.recognition.prompted",
                "label": label,
                "confidence": confidence,
                "prompt": instruction,
                "windowId": window_id,
            }
        )
        logger.info(
            "iris.voice.sound_recognition_prompted session={} device={} label={} confidence={} window={}",
            self._session.session_id,
            self._session.device_id,
            label,
            confidence,
            window_id,
        )
        await self._wait_for_prompt_slot()
        await self.push_frame(
            LLMMessagesAppendFrame(
                messages=[
                    developer_event_message(
                        event_type="sound.recognition",
                        instruction=(
                            "Use this sensor event as room context, not user speech. If a short "
                            "voice response is appropriate, speak naturally and briefly."
                        ),
                        payload=payload,
                    )
                ],
                run_llm=True,
            ),
            FrameDirection.DOWNSTREAM,
        )

    async def _wait_for_prompt_slot(self) -> None:
        while self._events.conversation_busy(quiet_seconds=self._prompt_quiet_secs):
            await asyncio.sleep(0.25)
