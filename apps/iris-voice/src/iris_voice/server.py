from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from urllib.parse import parse_qs, urlparse
from typing import TYPE_CHECKING


def configure_tls_cert_bundle() -> None:
    if os.getenv("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    bundle = certifi.where()
    if bundle and os.path.exists(bundle):
        os.environ["SSL_CERT_FILE"] = bundle


configure_tls_cert_bundle()

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.transports.base_transport import TransportParams

from .agent_completion_events import AgentCompletionSubscriber
from .local_audio import LocalAudioRuntimeManager
from .runtime_events import RuntimeEvents
from .session import verify_session_token
from .turns.barge_in import BARGE_IN_VAD_SAMPLE_RATE
from .transport.device import DeviceTransport

if TYPE_CHECKING:
    from pipecat.pipeline.task import PipelineTask


def create_app() -> FastAPI:
    app = FastAPI(title="Iris Voice")
    local_audio = LocalAudioRuntimeManager()

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "iris-voice"}

    @app.get("/ping")
    async def ping():
        return {"status": "healthy"}

    @app.get("/local-audio/status")
    async def local_audio_status():
        return local_audio.status()

    @app.post("/local-audio/start")
    async def local_audio_start(payload: dict[str, str]):
        token = payload.get("token") or _token_from_voice_url(payload.get("voiceUrl") or "")
        if not token:
            return {"ok": False, "error": "missing voice session token"}
        try:
            session = verify_session_token(token)
        except Exception as error:
            logger.warning("iris.voice.local_audio.session_rejected error={}", error)
            return {"ok": False, "error": "invalid voice session token"}
        return await local_audio.start(session)

    @app.post("/local-audio/stop")
    async def local_audio_stop(payload: dict[str, str] | None = None):
        reason = (payload or {}).get("reason") or "stopped"
        return await local_audio.stop(reason=reason)

    @app.post("/local-audio/stop-speaking")
    async def local_audio_stop_speaking(payload: dict[str, str] | None = None):
        reason = (payload or {}).get("reason") or "user_stop_speaking"
        return await local_audio.stop_speaking(reason=reason)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=1008)
            return
        try:
            session = verify_session_token(token)
        except Exception as error:
            logger.warning("iris.voice.session_rejected error={}", error)
            await websocket.close(code=1008)
            return

        await websocket.accept()
        disconnect_event = asyncio.Event()
        pipeline_task: "PipelineTask | None" = None
        completion_task: asyncio.Task[None] | None = None
        audio_frames = 0
        audio_bytes = 0
        logger.info(
            "iris.voice.connected session={} device={} source={}",
            session.session_id,
            session.device_id,
            session.source,
        )

        async def cancel_pipeline(reason: str) -> None:
            nonlocal pipeline_task, completion_task
            if disconnect_event.is_set():
                return
            disconnect_event.set()
            if completion_task:
                completion_task.cancel()
            transport.close()
            logger.info(
                "iris.voice.disconnect_requested session={} device={} reason={}",
                session.session_id,
                session.device_id,
                reason,
            )
            if pipeline_task:
                await pipeline_task.cancel(reason=reason)

        async def on_transport_send_failed(error: Exception) -> None:
            reason = f"transport_send_failed:{type(error).__name__}"
            if disconnect_event.is_set():
                return
            disconnect_event.set()
            transport.close()
            logger.info(
                "iris.voice.disconnect_requested session={} device={} reason={}",
                session.session_id,
                session.device_id,
                reason,
            )
            if pipeline_task:
                asyncio.create_task(pipeline_task.cancel(reason=reason))

        transport = DeviceTransport(
            websocket,
            TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
                audio_out_enabled=True,
                audio_out_sample_rate=session.sample_rate,
                audio_out_channels=1,
            ),
            on_send_failed=on_transport_send_failed,
        )
        events = RuntimeEvents(websocket, session)
        def on_task_ready(task: "PipelineTask") -> None:
            nonlocal pipeline_task, completion_task
            pipeline_task = task
            completion_task = asyncio.create_task(
                AgentCompletionSubscriber(
                    session=session,
                    events=events,
                    task=task,
                    playback_active=transport.is_playback_active,
                ).run()
            )
            logger.info(
                "iris.voice.pipeline_ready session={} device={}",
                session.session_id,
                session.device_id,
            )

        from .pipeline import run_voice_runtime

        voice_task = asyncio.create_task(
            run_voice_runtime(transport, session, events, on_task_ready=on_task_ready)
        )
        try:
            while True:
                message = await websocket.receive()
                if voice_task.done():
                    logger.warning(
                        "iris.voice.pipeline_task_done_before_receive session={} device={} cancelled={} exception={}",
                        session.session_id,
                        session.device_id,
                        voice_task.cancelled(),
                        voice_task.exception() if not voice_task.cancelled() else None,
                    )
                    await voice_task
                if message.get("bytes") is not None:
                    audio_frames += 1
                    audio_bytes += len(message["bytes"])
                    if audio_frames == 1 or audio_frames % 100 == 0:
                        logger.info(
                            "iris.voice.audio_input frames={} bytes={} last_bytes={} sample_rate={} channels={}",
                            audio_frames,
                            audio_bytes,
                            len(message["bytes"]),
                            session.sample_rate,
                            session.channels,
                        )
                    await transport.push_pcm(message["bytes"], session.sample_rate, session.channels)
                    continue
                text = message.get("text")
                if not text:
                    continue
                if text == "ping":
                    await websocket.send_text("pong")
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("iris.voice.websocket_text_ignored reason=json_decode")
                    continue
                if events.handle_device_message(payload):
                    continue
                if payload.get("type") == "control" and payload.get("action") == "stop_speaking":
                    interrupted = await transport.interrupt_playback(reason="user_stop_speaking")
                    logger.info(
                        "iris.voice.control_stop_speaking session={} device={} interrupted={}",
                        session.session_id,
                        session.device_id,
                        interrupted,
                    )
                    continue
                if payload.get("type") != "audio":
                    logger.debug(
                        "iris.voice.websocket_text_ignored type={}",
                        payload.get("type"),
                    )
                    continue
                encoded_audio = payload.get("audio")
                if not encoded_audio:
                    logger.debug("iris.voice.audio_payload_ignored reason=missing_audio")
                    continue
                await transport.push_pcm(
                    base64.b64decode(encoded_audio),
                    int(payload.get("sampleRate") or session.sample_rate),
                    int(payload.get("channels") or session.channels),
                )
        except WebSocketDisconnect:
            logger.info(
                "iris.voice.websocket_disconnect session={} device={}",
                session.session_id,
                session.device_id,
            )
        except RuntimeError as error:
            logger.info(
                "iris.voice.websocket_runtime_closed session={} device={} error={}: {}",
                session.session_id,
                session.device_id,
                type(error).__name__,
                error,
            )
        except Exception:
            logger.exception("iris.voice.websocket_failed")
        finally:
            await cancel_pipeline("websocket_disconnected")
            try:
                await asyncio.wait_for(voice_task, timeout=5)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("iris.voice.task_cancel_timeout session={}", session.session_id)
            except Exception:
                logger.exception("iris.voice.task_failed session={}", session.session_id)
            await events.end_session("websocket_disconnected")
            logger.info("iris.voice.disconnected session={}", session.session_id)

    return app


def _token_from_voice_url(voice_url: str) -> str | None:
    if not voice_url:
        return None
    query = parse_qs(urlparse(voice_url).query)
    values = query.get("token")
    if not values:
        return None
    return values[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
