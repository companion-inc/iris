from __future__ import annotations

import base64
import io
import os
import subprocess
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

MODEL_NAME = os.getenv("IRIS_SPEAKER_ID_MODEL", "speechbrain/spkrec-ecapa-voxceleb")
DEVICE = os.getenv("IRIS_SPEAKER_ID_DEVICE", "cpu")
THRESHOLD = float(os.getenv("IRIS_SPEAKER_ID_THRESHOLD", "0.45"))


class AudioSample(BaseModel):
    audioBase64: str
    mimeType: str
    durationMs: int | None = None


class EnrollRequest(BaseModel):
    samples: Annotated[list[AudioSample], Field(min_length=1, max_length=8)]


class EnrollResponse(BaseModel):
    embedding: list[float]
    sampleCount: int
    model: str


class SpeakerProfile(BaseModel):
    userId: str
    displayName: str
    embedding: list[float]


class IdentifyRequest(BaseModel):
    sample: AudioSample
    profiles: Annotated[list[SpeakerProfile], Field(min_length=1, max_length=100)]
    threshold: float | None = None


class IdentifyResponse(BaseModel):
    userId: str | None
    displayName: str | None
    score: float | None
    model: str


app = FastAPI(title="Iris Speaker ID")
_model: Any | None = None


def model() -> Any:
    global _model
    if _model is None:
        from speechbrain.inference.speaker import SpeakerRecognition

        logger.info("iris.speaker_id.model_loading model={} device={}", MODEL_NAME, DEVICE)
        _model = SpeakerRecognition.from_hparams(
            source=MODEL_NAME,
            savedir="pretrained_models/spkrec-ecapa",
            run_opts={"device": DEVICE},
        )
        logger.info("iris.speaker_id.model_loaded model={} device={}", MODEL_NAME, DEVICE)
    return _model


def decode_audio_with_torchaudio(raw: bytes) -> tuple[Any, int]:
    import torchaudio

    return torchaudio.load(io.BytesIO(raw))


def decode_audio_with_ffmpeg(raw: bytes) -> tuple[Any, int]:
    import numpy as np
    import torch

    process = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0 or not process.stdout:
        message = process.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(message or "ffmpeg could not decode audio")
    audio = np.frombuffer(process.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.from_numpy(audio).unsqueeze(0), 16000


def decode_audio(sample: AudioSample) -> Any:
    import torchaudio

    raw = base64.b64decode(sample.audioBase64)
    try:
        waveform, sample_rate = decode_audio_with_ffmpeg(raw)
    except Exception as ffmpeg_error:
        logger.warning(
            "iris.speaker_id.decode.ffmpeg_failed mime_type={} bytes={} error={}",
            sample.mimeType,
            len(raw),
            ffmpeg_error,
        )
        try:
            waveform, sample_rate = decode_audio_with_torchaudio(raw)
        except Exception as torchaudio_error:
            logger.warning(
                "iris.speaker_id.decode.torchaudio_failed mime_type={} bytes={} error={}",
                sample.mimeType,
                len(raw),
                torchaudio_error,
            )
            error = torchaudio_error
        else:
            error = None
        if error is not None:
            raise HTTPException(400, "Could not decode audio sample") from error

    if waveform.numel() == 0:
        raise HTTPException(400, "Audio sample is empty")
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sample_rate != 16000:
        waveform = torchaudio.functional.resample(waveform, sample_rate, 16000)
    return waveform.squeeze(0)


def normalized_embedding(sample: AudioSample) -> Any:
    import numpy as np
    import torch

    signal = decode_audio(sample)
    with torch.no_grad():
        embedding = model().encode_batch(signal.unsqueeze(0), normalize=True)
    vector = embedding.squeeze().detach().cpu().numpy().astype(np.float32)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 0:
        raise HTTPException(400, "Could not extract speaker embedding")
    return vector / norm


def average_embeddings(samples: list[AudioSample]) -> Any:
    import numpy as np

    embeddings = [normalized_embedding(sample) for sample in samples]
    averaged = np.mean(np.stack(embeddings, axis=0), axis=0)
    norm = np.linalg.norm(averaged)
    if not np.isfinite(norm) or norm <= 0:
        raise HTTPException(400, "Could not create speaker profile")
    return averaged / norm


def cosine(left: Any, right: Any) -> float:
    import numpy as np

    return float(np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right)))


@app.get("/health")
async def health():
    return {"ok": True, "service": "iris-speaker-id", "model": MODEL_NAME}


@app.post("/v1/enroll", response_model=EnrollResponse)
async def enroll(request: EnrollRequest):
    embedding = average_embeddings(request.samples)
    return EnrollResponse(
        embedding=embedding.astype(float).tolist(),
        sampleCount=len(request.samples),
        model=MODEL_NAME,
    )


@app.post("/v1/identify", response_model=IdentifyResponse)
async def identify(request: IdentifyRequest):
    import numpy as np

    candidate = normalized_embedding(request.sample)
    threshold = request.threshold if request.threshold is not None else THRESHOLD
    best_profile: SpeakerProfile | None = None
    best_score: float | None = None

    for profile in request.profiles:
        reference = np.asarray(profile.embedding, dtype=np.float32)
        if reference.shape != candidate.shape:
            continue
        score = cosine(candidate, reference)
        if best_score is None or score > best_score:
            best_score = score
            best_profile = profile

    if best_profile is None or best_score is None or best_score < threshold:
        logger.info(
            "iris.speaker_id.identify result=miss profile_count={} best_user={} best_score={} threshold={}",
            len(request.profiles),
            best_profile.userId if best_profile else None,
            best_score,
            threshold,
        )
        return IdentifyResponse(userId=None, displayName=None, score=best_score, model=MODEL_NAME)
    logger.info(
        "iris.speaker_id.identify result=match user={} score={} threshold={}",
        best_profile.userId,
        best_score,
        threshold,
    )
    return IdentifyResponse(
        userId=best_profile.userId,
        displayName=best_profile.displayName,
        score=best_score,
        model=MODEL_NAME,
    )


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Iris speaker identification service.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8082")))
    parser.add_argument("--reload", action="store_true", default=os.getenv("IRIS_RELOAD") == "true")
    args = parser.parse_args()

    uvicorn.run(
        "iris_speaker_id.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
