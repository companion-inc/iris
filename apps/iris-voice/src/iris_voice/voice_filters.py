from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter


def build_pipecat_input_filter() -> BaseAudioFilter | None:
    """Build a configured Pipecat input filter for the sidecar transport."""
    name = os.getenv("IRIS_PIPECAT_AUDIO_IN_FILTER", "").strip().lower()
    if not name:
        logger.info("iris.voice.audio_filter disabled")
        return None

    if name in {"aic", "ai-coustics", "aicoustics"}:
        return _build_aic_filter()

    if name in {"krisp", "krisp-viva", "krisp_viva"}:
        return _build_krisp_filter()

    raise ValueError(
        "Unsupported IRIS_PIPECAT_AUDIO_IN_FILTER. Supported values: aic, krisp."
    )


def _build_aic_filter() -> BaseAudioFilter:
    from pipecat.audio.filters.aic_filter import AICFilter

    license_key = os.getenv("AIC_SDK_LICENSE", "").strip()
    model_id = os.getenv("IRIS_AIC_MODEL_ID", "quail-vf-2.1-l-16khz").strip()
    model_path = os.getenv("IRIS_AIC_MODEL_PATH", "").strip()
    enhancement_level = _optional_float("IRIS_AIC_ENHANCEMENT_LEVEL")
    if not license_key:
        raise ValueError("AIC_SDK_LICENSE is required for IRIS_PIPECAT_AUDIO_IN_FILTER=aic")
    logger.info(
        "iris.voice.audio_filter enabled=aic model_id={} model_path_configured={} enhancement_level={}",
        model_id,
        bool(model_path),
        enhancement_level,
    )
    return AICFilter(
        license_key=license_key,
        model_id=None if model_path else model_id,
        model_path=Path(model_path) if model_path else None,
        enhancement_level=enhancement_level,
    )


def _build_krisp_filter() -> BaseAudioFilter:
    from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

    model_path = os.getenv("KRISP_VIVA_FILTER_MODEL_PATH", "").strip()
    if not model_path:
        raise ValueError(
            "KRISP_VIVA_FILTER_MODEL_PATH is required for IRIS_PIPECAT_AUDIO_IN_FILTER=krisp"
        )
    logger.info("iris.voice.audio_filter enabled=krisp model_path_configured=true")
    return KrispVivaFilter(
        model_path=model_path,
        noise_suppression_level=_optional_int("KRISP_NOISE_SUPPRESSION_LEVEL") or 100,
        api_key=os.getenv("KRISP_VIVA_API_KEY", ""),
    )


def _optional_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return float(raw)


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return int(raw)
