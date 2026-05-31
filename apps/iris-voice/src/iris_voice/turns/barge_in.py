from __future__ import annotations

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor

from .timing import (
    BARGE_IN_VAD_CONFIDENCE,
    BARGE_IN_VAD_MIN_VOLUME,
    BARGE_IN_VAD_START_SECONDS,
    BARGE_IN_VAD_STOP_SECONDS,
)

BARGE_IN_VAD_PARAMS = VADParams(
    confidence=BARGE_IN_VAD_CONFIDENCE,
    start_secs=BARGE_IN_VAD_START_SECONDS,
    stop_secs=BARGE_IN_VAD_STOP_SECONDS,
    min_volume=BARGE_IN_VAD_MIN_VOLUME,
)


def build_barge_in_vad() -> SileroVADAnalyzer:
    return SileroVADAnalyzer(params=BARGE_IN_VAD_PARAMS)


def build_barge_in_processor() -> VADProcessor:
    return VADProcessor(vad_analyzer=build_barge_in_vad())
