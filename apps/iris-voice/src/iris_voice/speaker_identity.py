from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
import wave
from typing import Any
from urllib import request

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .api_client import fetch_session_config
from .session import VoiceSessionContext


class SpeakerAudioBuffer(FrameProcessor):
    def __init__(self, *, max_seconds: float = 120.0):
        super().__init__()
        self._max_seconds = max_seconds
        self._audio = bytearray()
        self._sample_rate = 16000
        self._channels = 1
        self._trimmed_seconds = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InputAudioRawFrame):
            self._sample_rate = frame.sample_rate
            self._channels = frame.num_channels
            self._audio.extend(frame.audio)
            self._trim()
        await self.push_frame(frame, direction)

    def slice(
        self,
        start: float | None,
        end: float | None,
        *,
        padding_seconds: float = 0.45,
        min_seconds: float = 1.4,
    ) -> tuple[bytes, int, int] | None:
        if start is None or end is None or end <= start:
            return None
        padded_start = max(0.0, start - padding_seconds)
        padded_end = end + padding_seconds
        if padded_end - padded_start < min_seconds:
            midpoint = (padded_start + padded_end) / 2
            half = min_seconds / 2
            padded_start = max(0.0, midpoint - half)
            padded_end = midpoint + half
        relative_start = max(0.0, padded_start - self._trimmed_seconds)
        relative_end = max(relative_start, padded_end - self._trimmed_seconds)
        bytes_per_second = self._sample_rate * self._channels * 2
        start_byte = int(relative_start * bytes_per_second)
        end_byte = int(relative_end * bytes_per_second)
        frame_width = self._channels * 2
        start_byte -= start_byte % frame_width
        end_byte -= end_byte % frame_width
        if start_byte < 0 or end_byte <= start_byte or start_byte >= len(self._audio):
            return None
        chunk = bytes(self._audio[start_byte : min(end_byte, len(self._audio))])
        return chunk, self._sample_rate, self._channels

    def _trim(self) -> None:
        bytes_per_second = self._sample_rate * self._channels * 2
        max_bytes = int(self._max_seconds * bytes_per_second)
        if len(self._audio) <= max_bytes:
            return
        remove = len(self._audio) - max_bytes
        frame_width = self._channels * 2
        remove -= remove % frame_width
        if remove <= 0:
            return
        del self._audio[:remove]
        self._trimmed_seconds += remove / bytes_per_second


class SpeakerIdentityMatcher:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        session: VoiceSessionContext | None = None,
        refresh_interval_seconds: float = 30.0,
    ):
        recognition = config.get("speakerRecognition")
        if not isinstance(recognition, dict):
            recognition = {}
        self._url = (os.getenv("IRIS_SPEAKER_ID_URL") or "").rstrip("/")
        self._profiles = self._profiles_from_config(recognition.get("profiles"))
        self._session = session
        self._refresh_interval_seconds = refresh_interval_seconds
        self._last_refresh = time.monotonic()
        self._refresh_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._profiles)

    async def identify(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> dict[str, Any] | None:
        await self._refresh_profiles_if_stale()
        if not self.enabled or len(audio) < sample_rate:
            return None
        payload = {
            "sample": {
                "audioBase64": base64.b64encode(wav_bytes(audio, sample_rate, channels)).decode("ascii"),
                "mimeType": "audio/wav",
            },
            "profiles": self._profiles,
        }

        def post() -> dict[str, Any] | None:
            req = request.Request(
                f"{self._url}/v1/identify",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "IrisVoice/1",
                },
            )
            with request.urlopen(req, timeout=8) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body if isinstance(body, dict) else None

        try:
            started = time.perf_counter()
            result = await asyncio.to_thread(post)
        except Exception:
            logger.exception("iris.voice.speaker_identity_failed")
            return None
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "iris.voice.speaker_identity_request duration_ms={:.1f} result={}",
            elapsed_ms,
            "match" if result and result.get("userId") else "miss",
        )
        if not result or not result.get("userId"):
            return None
        return result

    async def _refresh_profiles_if_stale(self) -> None:
        if not self._session or not self._url:
            return
        now = time.monotonic()
        if self._profiles and now - self._last_refresh < self._refresh_interval_seconds:
            return
        async with self._refresh_lock:
            now = time.monotonic()
            if self._profiles and now - self._last_refresh < self._refresh_interval_seconds:
                return
            config = await fetch_session_config(self._session)
            recognition = config.get("speakerRecognition")
            profiles = (
                self._profiles_from_config(recognition.get("profiles"))
                if isinstance(recognition, dict)
                else []
            )
            self._last_refresh = time.monotonic()
            if profiles:
                self._profiles = profiles
            logger.info(
                "iris.voice.speaker_profiles_refreshed session={} profile_count={}",
                self._session.session_id,
                len(self._profiles),
            )

    def _profiles_from_config(self, raw_profiles: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_profiles, list):
            return []
        profiles: list[dict[str, Any]] = []
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            embedding = raw.get("embedding")
            if isinstance(embedding, str):
                try:
                    embedding = json.loads(embedding)
                except json.JSONDecodeError:
                    embedding = None
            if not isinstance(embedding, list):
                continue
            user_id = raw.get("userId")
            display_name = raw.get("displayName")
            if not isinstance(user_id, str) or not isinstance(display_name, str):
                continue
            profiles.append(
                {
                    "userId": user_id,
                    "displayName": display_name,
                    "embedding": embedding,
                }
            )
        return profiles


def wav_bytes(audio: bytes, sample_rate: int, channels: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio)
    return out.getvalue()
