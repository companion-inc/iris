from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import httpx
from loguru import logger

from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings, assert_given
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

from .audio import apply_pcm16le_gain


class XAITTSService(TTSService):
    Settings = TTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        voice_id: str = "eve",
        language: str = "en",
        sample_rate: int | None = None,
        optimize_streaming_latency: int = 1,
        text_normalization: bool = True,
        output_gain: float = 0.85,
        **kwargs,
    ):
        settings = self.Settings(model="grok-tts", voice=voice_id, language=language)
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings,
            **kwargs,
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._requested_sample_rate = sample_rate or 24000
        self._optimize_streaming_latency = optimize_streaming_latency
        self._text_normalization = text_normalization
        self._output_gain = max(0.0, min(1.0, output_gain))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0))
        self._tts_lock = asyncio.Lock()

    def can_generate_metrics(self) -> bool:
        return True

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._client.aclose()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._client.aclose()

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        async with self._tts_lock:
            async for frame in self._run_tts_locked(text, context_id):
                yield frame

    async def _run_tts_locked(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        voice_id = assert_given(self._settings.voice)
        language = assert_given(self._settings.language)
        if not voice_id:
            yield ErrorFrame(error="xAI TTS voice_id must be specified")
            return
        if not language:
            yield ErrorFrame(error="xAI TTS language must be specified")
            return

        logger.debug("{}: Generating xAI TTS [{}]", self, text)
        try:
            sample_rate = self.sample_rate or self._requested_sample_rate
            chunk_size = max(int(sample_rate * 0.5 * 2), 1)
            async with self._client.stream(
                "POST",
                f"{self._base_url}/tts",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "audio/pcm",
                },
                json={
                    "text": text,
                    "voice_id": voice_id,
                    "language": language,
                    "output_format": {
                        "codec": "pcm",
                        "sample_rate": sample_rate,
                    },
                    "optimize_streaming_latency": self._optimize_streaming_latency,
                    "text_normalization": self._text_normalization,
                },
            ) as response:
                if response.status_code != 200:
                    error = await response.aread()
                    detail = error.decode("utf-8", errors="replace")
                    logger.error(
                        "{} error getting xAI TTS audio (status={}, error={})",
                        self,
                        response.status_code,
                        detail,
                    )
                    yield ErrorFrame(
                        error=f"xAI TTS request failed with status {response.status_code}: {detail}"
                    )
                    return

                await self.start_tts_usage_metrics(text)
                pending_pcm_byte = b""
                async for chunk in response.aiter_bytes(chunk_size):
                    if not chunk:
                        continue
                    pcm = pending_pcm_byte + chunk
                    even_length = len(pcm) - (len(pcm) % 2)
                    pending_pcm_byte = pcm[even_length:]
                    pcm = pcm[:even_length]
                    if pcm:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            apply_pcm16le_gain(pcm, self._output_gain),
                            sample_rate,
                            1,
                            context_id=context_id,
                        )
                if pending_pcm_byte:
                    logger.warning("{} dropped trailing partial PCM16 sample byte", self)
        except httpx.HTTPError as exc:
            logger.exception("{} xAI TTS request failed", self)
            yield ErrorFrame(error=f"xAI TTS request failed: {exc}")
