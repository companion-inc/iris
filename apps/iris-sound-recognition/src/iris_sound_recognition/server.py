from __future__ import annotations

import argparse
import base64
from typing import Any

import uvicorn
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel, Field

from .classifier import SoundRecognizer


class ClassifyRequest(BaseModel):
    audio: str = Field(min_length=1)
    sampleRate: int = Field(gt=0)
    channels: int = Field(default=1, ge=1, le=8)
    labels: list[str] = Field(default_factory=list)
    sessionId: str | None = None
    deviceId: str | None = None
    windowId: str | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="Iris Sound Recognition")
    recognizer = SoundRecognizer()

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "service": "iris-sound-recognition",
            "backend": recognizer.backend,
            "model": recognizer.model_name,
            "modelRevision": recognizer.model_revision,
        }

    @app.post("/v1/sound-recognition/classify")
    async def classify(body: ClassifyRequest) -> dict[str, Any]:
        audio = base64.b64decode(body.audio)
        matches = recognizer.classify(
            audio,
            sample_rate=body.sampleRate,
            channels=body.channels,
            labels=body.labels,
        )
        logger.info(
            "iris.sound_recognition.classified session={} device={} window={} bytes={} labels={} matches={}",
            body.sessionId,
            body.deviceId,
            body.windowId,
            len(audio),
            len(body.labels),
            len(matches),
        )
        return {
            "object": "sound_recognition_classification",
            "windowId": body.windowId,
            "backend": recognizer.backend,
            "model": recognizer.model_name,
            "modelRevision": recognizer.model_revision,
            "matches": [match.public() for match in matches],
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
