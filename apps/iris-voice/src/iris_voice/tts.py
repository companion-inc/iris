from __future__ import annotations

import os

from loguru import logger
from pipecat.services.deepgram.tts import DeepgramTTSService, DeepgramTTSSettings
from pipecat.services.google.tts import GeminiTTSService, GeminiTTSSettings
from pipecat.services.openai.tts import OpenAITTSService, OpenAITTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.services.xai.tts import XAITTSService

from .env import optional_int_env


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def tts_provider() -> str:
    return os.getenv("IRIS_TTS_PROVIDER", "xai").strip().lower()


def default_tts_sample_rate(session_sample_rate: int) -> int:
    return session_sample_rate


def configured_tts_sample_rate(session_sample_rate: int) -> int:
    return optional_int_env("IRIS_TTS_SAMPLE_RATE", default_tts_sample_rate(session_sample_rate))


def build_tts_service(*, deepgram_api_key: str, sample_rate: int) -> tuple[TTSService, str]:
    provider = tts_provider()
    if provider == "xai":
        xai_api_key = _first_env("XAI_API_KEY", "IRIS_XAI_API_KEY", "GROK_API_KEY")
        if not xai_api_key:
            raise RuntimeError("Missing required environment variable for xAI TTS: XAI_API_KEY")

        voice_id = os.getenv("IRIS_XAI_TTS_VOICE_ID", "eve")
        language = os.getenv("IRIS_XAI_TTS_LANGUAGE", "en")
        base_url = os.getenv("IRIS_XAI_TTS_BASE_URL", "wss://api.x.ai/v1/tts")
        optimize_streaming_latency = optional_int_env("IRIS_XAI_TTS_OPTIMIZE_STREAMING_LATENCY", 0)
        codec = os.getenv("IRIS_XAI_TTS_CODEC", "pcm")
        logger.info(
            "iris.voice.tts_config provider=xai mode=websocket voice_id={} sample_rate={} language={} codec={} latency_opt={}",
            voice_id,
            sample_rate,
            language,
            codec,
            optimize_streaming_latency,
        )
        return (
            XAITTSService(
                api_key=xai_api_key,
                base_url=base_url,
                sample_rate=sample_rate,
                codec=codec,
                settings=XAITTSService.Settings(
                    voice=voice_id,
                    language=language,
                    extra={"optimize_streaming_latency": optimize_streaming_latency},
                ),
            ),
            "grok-tts",
        )

    if provider == "deepgram":
        if not deepgram_api_key:
            raise RuntimeError("Missing required environment variable for Deepgram TTS: DEEPGRAM_API_KEY")
        tts_model = os.getenv("IRIS_TTS_MODEL", "aura-2-thalia-en")
        logger.info(
            "iris.voice.tts_config provider=deepgram model={} sample_rate={}",
            tts_model,
            sample_rate,
        )
        return (
            DeepgramTTSService(
                api_key=deepgram_api_key,
                sample_rate=sample_rate,
                settings=DeepgramTTSSettings(model=tts_model, voice=tts_model),
            ),
            tts_model,
        )

    if provider == "openai":
        openai_api_key = _first_env("OPENAI_API_KEY")
        if not openai_api_key:
            raise RuntimeError("Missing required environment variable for OpenAI TTS: OPENAI_API_KEY")

        tts_model = os.getenv("IRIS_OPENAI_TTS_MODEL", os.getenv("IRIS_TTS_MODEL", "gpt-4o-mini-tts"))
        voice = os.getenv("IRIS_OPENAI_TTS_VOICE", "alloy")
        logger.info(
            "iris.voice.tts_config provider=openai model={} voice={} sample_rate={}",
            tts_model,
            voice,
            sample_rate,
        )
        return (
            OpenAITTSService(
                api_key=openai_api_key,
                sample_rate=sample_rate,
                settings=OpenAITTSSettings(model=tts_model, voice=voice),
            ),
            tts_model,
        )

    if provider == "gemini":
        credentials_path = os.getenv("IRIS_GOOGLE_CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path:
            raise RuntimeError(
                "Missing Google Cloud credentials for Gemini TTS: set GOOGLE_APPLICATION_CREDENTIALS"
            )
        tts_model = os.getenv("IRIS_GEMINI_TTS_MODEL", os.getenv("IRIS_TTS_MODEL", "gemini-2.5-flash-tts"))
        voice = os.getenv("IRIS_GEMINI_TTS_VOICE", "Kore")
        language = os.getenv("IRIS_GEMINI_TTS_LANGUAGE", "en-US")
        logger.info(
            "iris.voice.tts_config provider=gemini model={} voice={} sample_rate={} credentials_path={}",
            tts_model,
            voice,
            sample_rate,
            bool(credentials_path),
        )
        return (
            GeminiTTSService(
                credentials_path=credentials_path,
                sample_rate=sample_rate,
                settings=GeminiTTSSettings(model=tts_model, voice=voice, language=language),
            ),
            tts_model,
        )

    raise RuntimeError(
        f"Unsupported IRIS_TTS_PROVIDER={provider!r}; expected 'xai', 'deepgram', 'openai', or 'gemini'"
    )
