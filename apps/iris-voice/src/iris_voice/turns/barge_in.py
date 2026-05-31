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
BARGE_IN_VAD_SAMPLE_RATE = 16000


def build_barge_in_vad(sample_rate: int = BARGE_IN_VAD_SAMPLE_RATE) -> SileroVADAnalyzer:
    analyzer = SileroVADAnalyzer(sample_rate=sample_rate, params=BARGE_IN_VAD_PARAMS)
    analyzer.set_sample_rate(sample_rate)
    return analyzer


def build_barge_in_processor(sample_rate: int = BARGE_IN_VAD_SAMPLE_RATE) -> VADProcessor:
    return VADProcessor(vad_analyzer=build_barge_in_vad(sample_rate))
