from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext
from .turns.barge_in import BARGE_IN_VAD_SAMPLE_RATE


LOCAL_PLAYBACK_ECHO_TAIL_SECONDS = 3.0


class LocalRuntimeWebSocket:
    async def send_json(self, message: dict[str, Any]) -> None:
        return None


class LocalAudioRuntimeTransport:
    def __init__(self, *, sample_rate: int, channels: int):
        from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

        self._playback_active = False
        self._playback_active_until = 0.0
        self._output: Pipeline | None = None
        self._transport = LocalAudioTransport(
            params=LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
                audio_in_channels=channels,
                audio_out_enabled=True,
                audio_out_sample_rate=sample_rate,
                audio_out_channels=channels,
            )
        )

    def input(self):
        return self._transport.input()

    def output(self):
        if self._output is None:
            self._output = Pipeline(
                [
                    LocalPlaybackStateTracker(
                        on_started=self._mark_playback_started,
                        on_stopped=self._mark_playback_stopped,
                    ),
                    self._transport.output(),
                ]
            )
        return self._output

    async def close(self) -> None:
        for processor in (self._transport._input, self._transport._output):
            if processor is not None:
                await processor.cleanup()
        self._transport._pyaudio.terminate()

    def is_playback_active(self) -> bool:
        return self._playback_active or time.monotonic() < self._playback_active_until

    def _mark_playback_started(self) -> None:
        if not self._playback_active:
            logger.info("iris.voice.local_audio.playback_started")
        self._playback_active = True
        self._playback_active_until = time.monotonic() + LOCAL_PLAYBACK_ECHO_TAIL_SECONDS

    def _mark_playback_stopped(self) -> None:
        if self._playback_active:
            logger.info("iris.voice.local_audio.playback_stopped")
        self._playback_active = False
        self._playback_active_until = time.monotonic() + LOCAL_PLAYBACK_ECHO_TAIL_SECONDS


class LocalPlaybackStateTracker(FrameProcessor):
    def __init__(self, *, on_started, on_stopped):
        super().__init__()
        self._on_started = on_started
        self._on_stopped = on_stopped

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if direction == FrameDirection.DOWNSTREAM:
            name = frame.__class__.__name__
            if name == "BotStartedSpeakingFrame" or isinstance(frame, OutputAudioRawFrame):
                self._on_started()
            elif name == "BotStoppedSpeakingFrame":
                self._on_stopped()
        await super().process_frame(frame, direction)


class LocalAudioRuntimeManager:
    def __init__(self):
        self._task: asyncio.Task[None] | None = None
        self._pipeline_task: PipelineTask | None = None
        self._session: VoiceSessionContext | None = None
        self._events: RuntimeEvents | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=80)

    def status(self) -> dict[str, Any]:
        running = self._task is not None and not self._task.done()
        return {
            "ok": True,
            "running": running,
            "sessionId": self._session.session_id if self._session else None,
            "startedAt": self._started_at,
            "uptimeSeconds": int(time.time() - self._started_at) if running and self._started_at else 0,
            "lastError": self._last_error,
            "recentEvents": list(self._recent_events)[-20:],
        }

    async def start(self, session: VoiceSessionContext) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            if self._session and self._session.session_id == session.session_id:
                return self.status()
            await self.stop(reason="replaced")

        self._session = session
        self._pipeline_task = None
        self._events = RuntimeEvents(LocalRuntimeWebSocket(), session)
        self._events.add_listener(self._remember_event)
        self._started_at = time.time()
        self._last_error = None
        self._recent_events.clear()
        transport = LocalAudioRuntimeTransport(sample_rate=session.sample_rate, channels=session.channels)

        def on_task_ready(task: PipelineTask) -> None:
            self._pipeline_task = task

        async def run() -> None:
            from .pipeline import run_voice_runtime

            logger.info(
                "iris.voice.local_audio.start session={} device={} sample_rate={} channels={}",
                session.session_id,
                session.device_id,
                session.sample_rate,
                session.channels,
            )
            try:
                await run_voice_runtime(transport, session, self._events, on_task_ready=on_task_ready)
            except asyncio.CancelledError:
                logger.info(
                    "iris.voice.local_audio.cancelled session={} device={}",
                    session.session_id,
                    session.device_id,
                )
                raise
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
                logger.exception(
                    "iris.voice.local_audio.failed session={} device={}",
                    session.session_id,
                    session.device_id,
                )
            finally:
                await transport.close()
                logger.info(
                    "iris.voice.local_audio.stopped session={} device={}",
                    session.session_id,
                    session.device_id,
                )

        self._task = asyncio.create_task(run())
        await asyncio.sleep(0)
        return self.status()

    async def stop(self, *, reason: str = "stopped") -> dict[str, Any]:
        if self._pipeline_task is not None:
            try:
                await asyncio.wait_for(self._pipeline_task.cancel(reason=reason), timeout=2)
            except TimeoutError:
                logger.warning(
                    "iris.voice.local_audio.pipeline_cancel_timeout session={} reason={}",
                    self._session.session_id if self._session else None,
                    reason,
                )
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                logger.warning(
                    "iris.voice.local_audio.task_cancel_timeout session={} reason={}",
                    self._session.session_id if self._session else None,
                    reason,
                )
        self._pipeline_task = None
        self._task = None
        return self.status()

    def _remember_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if not event_type:
            return
        self._recent_events.append(
            {
                "type": event_type,
                "text": event.get("text"),
                "reason": event.get("reason"),
                "at": time.time(),
            }
        )
