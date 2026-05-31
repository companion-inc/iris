from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib import request

from loguru import logger

from .env import optional_bool_env, optional_float_env
from .session import VoiceSessionContext


@dataclass(frozen=True)
class SpeechEmotionResult:
    label: str
    confidence: float | None
    model: str | None


class SpeechEmotionClassifier:
    def __init__(self, *, session: VoiceSessionContext):
        self._session = session
        self._url = os.getenv("IRIS_SPEECH_EMOTION_URL", "").rstrip("/")
        self._enabled = optional_bool_env("IRIS_SPEECH_EMOTION_ENABLED", bool(self._url))
        self._timeout_secs = optional_float_env("IRIS_SPEECH_EMOTION_TIMEOUT_SECONDS", 45.0)
        if self._enabled and not self._url:
            logger.warning("iris.voice.speech_emotion_disabled reason=missing_url")
            self._enabled = False
        logger.info(
            "iris.voice.speech_emotion_config enabled={} url={}",
            self._enabled,
            bool(self._url),
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def classify(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
        segment_id: str | None,
    ) -> SpeechEmotionResult | None:
        if not self._enabled:
            return None
        if len(audio) < sample_rate * channels * 2:
            return None
        started = time.perf_counter()
        try:
            body = await asyncio.to_thread(
                self._post,
                audio,
                sample_rate=sample_rate,
                channels=channels,
                segment_id=segment_id,
            )
        except Exception:
            logger.exception(
                "iris.voice.speech_emotion_failed session={} device={} segment={}",
                self._session.session_id,
                self._session.device_id,
                segment_id,
            )
            return None
        elapsed_ms = (time.perf_counter() - started) * 1000
        emotion = body.get("emotion") if isinstance(body, dict) else None
        if not isinstance(emotion, dict):
            return None
        label = emotion.get("label")
        if not isinstance(label, str) or not label.strip():
            return None
        confidence = emotion.get("confidence")
        model = emotion.get("model")
        result = SpeechEmotionResult(
            label=label.strip(),
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            model=model if isinstance(model, str) else None,
        )
        logger.info(
            "iris.voice.speech_emotion_classified session={} device={} segment={} label={} confidence={} duration_ms={:.1f}",
            self._session.session_id,
            self._session.device_id,
            segment_id,
            result.label,
            result.confidence,
            elapsed_ms,
        )
        return result

    def _post(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
        segment_id: str | None,
    ) -> dict[str, Any]:
        payload = {
            "audio": base64.b64encode(audio).decode("ascii"),
            "sampleRate": sample_rate,
            "channels": channels,
            "sessionId": self._session.session_id,
            "deviceId": self._session.device_id,
            "segmentId": segment_id,
        }
        req = request.Request(
            f"{self._url}/v1/speech-emotion/classify",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with request.urlopen(req, timeout=self._timeout_secs) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}
