from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger

DEFAULT_MODEL_REVISION = "06bb40c5ec089e96867ebc5246be02441f4a71e4"

LABEL_ALIASES = {
    "baby cry": ["baby cry infant cry", "crying sobbing"],
    "beep": ["beep bleep"],
    "dog bark": ["bark", "dog"],
    "fire alarm": ["fire alarm"],
    "glass breaking": ["glass", "breaking", "smash crash"],
    "knock": ["knock"],
    "siren": ["siren", "emergency vehicle"],
    "smoke alarm": ["smoke detector smoke alarm"],
    "sneeze": ["sneeze", "sneezing"],
}


@dataclass(frozen=True)
class SoundMatch:
    label: str
    confidence: float
    model: str
    raw_label: str | None = None

    def public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "confidence": round(float(self.confidence), 4),
            "model": self.model,
        }
        if self.raw_label:
            payload["rawLabel"] = self.raw_label
        return payload


def bool_env(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def float_env(name: str, fallback: float) -> float:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def normalize_label(label: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in label)
    return " ".join(cleaned.split())


def pcm16le_to_float32(audio: bytes, channels: int) -> np.ndarray:
    if len(audio) < 2:
        return np.array([], dtype=np.float32)
    usable = len(audio) - (len(audio) % 2)
    samples = np.frombuffer(audio[:usable], dtype="<i2").astype(np.float32) / 32768.0
    channel_count = max(1, int(channels))
    if channel_count > 1:
        frame_count = len(samples) // channel_count
        if frame_count <= 0:
            return np.array([], dtype=np.float32)
        samples = samples[: frame_count * channel_count].reshape(frame_count, channel_count).mean(axis=1)
    return np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)


def resample_linear(waveform: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or waveform.size == 0:
        return waveform
    duration = waveform.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_x = np.linspace(0.0, duration, num=waveform.size, endpoint=False)
    target_x = np.linspace(0.0, duration, num=target_size, endpoint=False)
    return np.interp(target_x, source_x, waveform).astype(np.float32, copy=False)


class SoundRecognizer:
    def __init__(self) -> None:
        self.backend = os.getenv("IRIS_SOUND_RECOGNITION_BACKEND", "transformers").strip().lower()
        self.model_name = os.getenv("IRIS_SOUND_RECOGNITION_MODEL", "mispeech/ced-small").strip()
        self.model_revision = os.getenv(
            "IRIS_SOUND_RECOGNITION_MODEL_REVISION",
            DEFAULT_MODEL_REVISION,
        ).strip()
        self.threshold = float_env("IRIS_SOUND_RECOGNITION_THRESHOLD", 0.45)
        self.fallback_heuristic = bool_env("IRIS_SOUND_RECOGNITION_FALLBACK_HEURISTIC", True)
        self.top_k = max(1, int(float_env("IRIS_SOUND_RECOGNITION_TOP_K", 12)))
        self._model: Any | None = None
        self._feature_extractor: Any | None = None
        self._torch: Any | None = None

    def classify(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        channels: int,
        labels: list[str],
    ) -> list[SoundMatch]:
        candidates = self._candidate_labels(labels)
        if not candidates:
            return []
        waveform = pcm16le_to_float32(audio, channels)
        if waveform.size == 0:
            return []
        if self.backend == "transformers":
            try:
                return self._classify_transformers(
                    waveform,
                    sample_rate=sample_rate,
                    candidates=candidates,
                )
            except Exception as error:
                logger.warning(
                    "iris.sound_recognition.transformers_failed error={}: {} fallback={}",
                    type(error).__name__,
                    error,
                    self.fallback_heuristic,
                )
                if not self.fallback_heuristic:
                    return []
        return self._classify_heuristic(waveform, candidates=candidates)

    def _load_transformers(self) -> None:
        if self._model is not None and self._feature_extractor is not None and self._torch is not None:
            return
        import torch
        from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

        self._torch = torch
        self._feature_extractor = AutoFeatureExtractor.from_pretrained(
            self.model_name,
            revision=self.model_revision,
            trust_remote_code=True,
        )
        self._model = AutoModelForAudioClassification.from_pretrained(
            self.model_name,
            revision=self.model_revision,
            trust_remote_code=True,
        )
        self._model.eval()
        logger.info(
            "iris.sound_recognition.model_loaded backend=transformers model={} revision={}",
            self.model_name,
            self.model_revision,
        )

    def _classify_transformers(
        self,
        waveform: np.ndarray,
        *,
        sample_rate: int,
        candidates: dict[str, str],
    ) -> list[SoundMatch]:
        self._load_transformers()
        assert self._feature_extractor is not None
        assert self._model is not None
        assert self._torch is not None

        target_rate = int(getattr(self._feature_extractor, "sampling_rate", sample_rate) or sample_rate)
        waveform = resample_linear(waveform, sample_rate, target_rate)
        inputs = self._feature_extractor(
            waveform,
            sampling_rate=target_rate,
            return_tensors="pt",
        )
        with self._torch.no_grad():
            logits = self._model(**inputs).logits[0]
            scores = self._torch.sigmoid(logits)
            values, indices = self._torch.topk(scores, min(self.top_k, scores.shape[-1]))

        id2label = getattr(self._model.config, "id2label", {}) or {}
        matches: list[SoundMatch] = []
        for value, index in zip(values.tolist(), indices.tolist(), strict=False):
            if value < self.threshold:
                continue
            raw_label = str(id2label.get(index, id2label.get(str(index), index)))
            label = self._requested_label(raw_label, candidates)
            if not label:
                continue
            matches.append(
                SoundMatch(
                    label=label,
                    confidence=float(value),
                    model=self.model_name,
                    raw_label=raw_label if normalize_label(raw_label) != normalize_label(label) else None,
                )
            )
        return matches

    def _classify_heuristic(
        self,
        waveform: np.ndarray,
        *,
        candidates: dict[str, str],
    ) -> list[SoundMatch]:
        unique_labels = sorted(set(candidates.values()))
        if len(unique_labels) != 1:
            logger.info(
                "iris.sound_recognition.heuristic_skipped reason=ambiguous_labels labels={}",
                len(unique_labels),
            )
            return []

        rms = float(np.sqrt(np.mean(np.square(waveform))))
        peak = float(np.max(np.abs(waveform)))
        if waveform.size > 1:
            zcr = float(np.mean(np.signbit(waveform[1:]) != np.signbit(waveform[:-1])))
            crest = peak / max(rms, 1e-6)
        else:
            zcr = 0.0
            crest = 0.0

        confidence = min(0.99, max(0.0, (peak * 1.6) + (rms * 4.0) + (zcr * 0.6) + min(crest / 30.0, 0.2)))
        if peak < 0.18 or rms < 0.018 or zcr < 0.035 or confidence < self.threshold:
            return []
        label = unique_labels[0]
        if not label:
            return []
        return [
            SoundMatch(
                label=label,
                confidence=confidence,
                model="heuristic-transient-v0",
                raw_label="sharp_transient",
            )
        ]

    def _candidate_labels(self, labels: list[str]) -> dict[str, str]:
        candidates: dict[str, str] = {}
        for label in labels:
            normalized = normalize_label(label)
            if not normalized:
                continue
            candidates.setdefault(normalized, " ".join(label.strip().split()))
            for alias in LABEL_ALIASES.get(normalized, []):
                candidates.setdefault(normalize_label(alias), " ".join(label.strip().split()))
        return candidates

    def _requested_label(self, raw_label: str, candidates: dict[str, str]) -> str | None:
        normalized = normalize_label(raw_label)
        if not normalized:
            return None
        for candidate, requested_label in candidates.items():
            if candidate in normalized or normalized in candidate:
                return requested_label
        return None
