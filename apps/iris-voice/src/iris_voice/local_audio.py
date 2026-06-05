from __future__ import annotations

import asyncio
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import TransportParams

from .agent_completion_events import AgentCompletionSubscriber
from .mac_voice_processing import MacVoiceProcessingInputTransport
from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext
from .turns.barge_in import BARGE_IN_VAD_SAMPLE_RATE
from .voice_filters import build_pipecat_input_filter


LOCAL_PLAYBACK_ECHO_TAIL_SECONDS = 0.5


class LocalRuntimeWebSocket:
    async def send_json(self, message: dict[str, Any]) -> None:
        return None


class LocalAudioRuntimeTransport:
    def __init__(self, *, sample_rate: int, channels: int, events: RuntimeEvents):
        import pyaudio

        self._playback_active = False
        self._playback_active_until = 0.0
        self._events = events
        self._output: Pipeline | None = None
        self._direct_output: DirectLocalAudioOutput | None = None
        self._pyaudio = pyaudio.PyAudio()
        self._output_audio_frames = 0
        self._speaker_write_frames = 0
        self._speaker_write_bytes = 0
        self._playback_stop_task: asyncio.Task[None] | None = None
        self._params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
            audio_in_channels=channels,
            audio_in_filter=build_pipecat_input_filter(),
            audio_out_enabled=True,
            audio_out_sample_rate=sample_rate,
            audio_out_channels=channels,
        )
        self._input = MacVoiceProcessingInputTransport(self._params)
        logger.info("iris.voice.local_audio.input_transport=mac_voice_processing")

    def input(self):
        return self._input

    def output(self):
        if self._output is None:
            self._direct_output = DirectLocalAudioOutput(
                py_audio=self._pyaudio,
                sample_rate=self._params.audio_out_sample_rate or BARGE_IN_VAD_SAMPLE_RATE,
                channels=self._params.audio_out_channels,
                on_speaker_write=self._mark_speaker_write,
            )
            self._output = Pipeline(
                [
                    LocalPlaybackStateTracker(
                        events=self._events,
                        on_started=self._mark_playback_started,
                        on_stopped=self._mark_playback_stopped,
                        on_interrupted=self._mark_playback_interrupted,
                        on_audio_frame=self._mark_audio_frame,
                    ),
                    self._direct_output,
                ]
            )
        return self._output

    async def close(self) -> None:
        if self._playback_stop_task and not self._playback_stop_task.done():
            self._playback_stop_task.cancel()
        if self._direct_output is not None:
            await self._direct_output.cleanup()
        await self._input.cleanup()
        if self._pyaudio is not None:
            self._pyaudio.terminate()

    def is_playback_active(self) -> bool:
        return self._playback_active or time.monotonic() < self._playback_active_until

    async def interrupt_playback(self, *, reason: str) -> bool:
        if not self.is_playback_active():
            logger.info("iris.voice.local_audio.interrupt_inactive reason={}", reason)
            return False
        if self._direct_output is not None:
            await self._direct_output.handle_downstream_frame(InterruptionFrame())
        self._mark_playback_interrupted()
        logger.info("iris.voice.local_audio.interrupt_direct reason={}", reason)
        return True

    def _mark_playback_started(self) -> None:
        if self._playback_stop_task and not self._playback_stop_task.done():
            self._playback_stop_task.cancel()
        if not self._playback_active:
            logger.info("iris.voice.local_audio.playback_started")
            self._events.emit({"type": "assistant.audio.started"})
        self._playback_active = True
        self._playback_active_until = time.monotonic() + LOCAL_PLAYBACK_ECHO_TAIL_SECONDS

    def _mark_playback_stopped(self) -> None:
        if self._playback_active:
            logger.info("iris.voice.local_audio.playback_stopped")
            self._events.emit({"type": "assistant.audio.stopped", "reason": "completed"})
        self._playback_active = False
        self._playback_active_until = time.monotonic() + LOCAL_PLAYBACK_ECHO_TAIL_SECONDS

    def _mark_playback_interrupted(self) -> None:
        if self._playback_active:
            logger.info("iris.voice.local_audio.playback_interrupted")
            self._events.emit({"type": "assistant.audio.stopped", "reason": "interruption"})
        self._playback_active = False
        self._playback_active_until = time.monotonic() + LOCAL_PLAYBACK_ECHO_TAIL_SECONDS

    def _schedule_playback_stopped(self) -> None:
        if self._playback_stop_task and not self._playback_stop_task.done():
            self._playback_stop_task.cancel()
        self._playback_stop_task = asyncio.create_task(self._delayed_playback_stopped())

    async def _delayed_playback_stopped(self) -> None:
        try:
            await asyncio.sleep(1.5)
            self._mark_playback_stopped()
        except asyncio.CancelledError:
            return

    def _mark_audio_frame(self, frame: OutputAudioRawFrame) -> None:
        self._output_audio_frames += 1
        if self._output_audio_frames == 1 or self._output_audio_frames % 50 == 0:
            logger.info(
                "iris.voice.local_audio.output_frame frames={} bytes={} sample_rate={} channels={}",
                self._output_audio_frames,
                len(frame.audio),
                frame.sample_rate,
                frame.num_channels,
            )

    def _mark_speaker_write(self, frame: OutputAudioRawFrame, *, written: bool) -> None:
        self._speaker_write_frames += 1
        self._speaker_write_bytes += len(frame.audio)
        if written:
            self._mark_playback_started()
            self._schedule_playback_stopped()
        if self._speaker_write_frames == 1 or self._speaker_write_frames % 50 == 0 or not written:
            logger.info(
                "iris.voice.local_audio.speaker_write frames={} bytes={} last_bytes={} sample_rate={} channels={} written={}",
                self._speaker_write_frames,
                self._speaker_write_bytes,
                len(frame.audio),
                frame.sample_rate,
                frame.num_channels,
                written,
            )


class LocalPlaybackStateTracker(FrameProcessor):
    def __init__(self, *, events: RuntimeEvents, on_started, on_stopped, on_interrupted, on_audio_frame):
        super().__init__()
        self._events = events
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._on_interrupted = on_interrupted
        self._on_audio_frame = on_audio_frame

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            self.handle_downstream_frame(frame)
        await self.push_frame(frame, direction)

    def handle_downstream_frame(self, frame: Frame) -> None:
        name = frame.__class__.__name__
        if isinstance(frame, TTSStartedFrame):
            self._on_started()
        elif isinstance(frame, OutputAudioRawFrame):
            self._on_audio_frame(frame)
        elif name == "BotStoppedSpeakingFrame":
            self._on_stopped()
        elif isinstance(frame, InterruptionFrame):
            self._on_interrupted()


class DirectLocalAudioOutput(FrameProcessor):
    def __init__(self, *, py_audio, sample_rate: int, channels: int, on_speaker_write):
        super().__init__()
        self._py_audio = py_audio
        self._sample_rate = sample_rate
        self._channels = channels
        self._on_speaker_write = on_speaker_write
        self._stream = None
        self._stream_sample_rate = 0
        self._stream_channels = 0
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._drop_audio_until_tts_stop = False
        self._dropped_interrupted_audio_frames = 0

    async def cleanup(self):
        await super().cleanup()
        if self._stream is not None:
            await self._close_stream(reason="cleanup")
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            handled = await self.handle_downstream_frame(frame)
            if handled:
                await self.push_frame(frame, direction)
                return
        await self.push_frame(frame, direction)

    async def handle_downstream_frame(self, frame: Frame) -> bool:
        if isinstance(frame, TTSStartedFrame):
            self._drop_audio_until_tts_stop = False
            self._dropped_interrupted_audio_frames = 0
        elif isinstance(frame, InterruptionFrame):
            self._drop_audio_until_tts_stop = True
            self._dropped_interrupted_audio_frames = 0
            await self._close_stream(reason="interruption")
        elif isinstance(frame, TTSStoppedFrame):
            self._drop_audio_until_tts_stop = False
        elif isinstance(frame, OutputAudioRawFrame):
            if self._drop_audio_until_tts_stop:
                self._dropped_interrupted_audio_frames += 1
                if (
                    self._dropped_interrupted_audio_frames == 1
                    or self._dropped_interrupted_audio_frames % 50 == 0
                ):
                    logger.info(
                        "iris.voice.local_audio.output_dropped_after_interruption frames={}",
                        self._dropped_interrupted_audio_frames,
                    )
                self._on_speaker_write(frame, written=False)
                return True
            written = await self._write_audio_frame(frame)
            self._on_speaker_write(frame, written=written)
        return False

    async def _write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if not frame.audio:
            return False
        sample_rate = frame.sample_rate or self._sample_rate
        channels = frame.num_channels or self._channels
        await self._ensure_stream(sample_rate=sample_rate, channels=channels)
        if self._stream is None:
            return False
        stream = self._stream
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, stream.write, frame.audio)
        except OSError as error:
            logger.warning("iris.voice.local_audio.write_failed error={}", error)
            if self._stream is stream:
                self._stream = None
                self._stream_sample_rate = 0
                self._stream_channels = 0
            return False
        return True

    async def _ensure_stream(self, *, sample_rate: int, channels: int) -> None:
        if (
            self._stream is not None
            and self._stream_sample_rate == sample_rate
            and self._stream_channels == channels
        ):
            return
        if self._stream is not None:
            await self._close_stream(reason="format_change")
            self._stream = None
        self._stream = self._py_audio.open(
            format=self._py_audio.get_format_from_width(2),
            channels=channels,
            rate=sample_rate,
            output=True,
        )
        self._stream.start_stream()
        self._stream_sample_rate = sample_rate
        self._stream_channels = channels

    async def _close_stream(self, *, reason: str) -> None:
        if self._stream is None:
            return
        logger.info("iris.voice.local_audio.stream_closed reason={}", reason)
        stream = self._stream
        self._stream = None
        self._stream_sample_rate = 0
        self._stream_channels = 0

        def close_stream() -> None:
            try:
                stream.stop_stream()
            finally:
                stream.close()

        await asyncio.get_running_loop().run_in_executor(self._executor, close_stream)


class LocalAudioRuntimeManager:
    def __init__(self):
        self._task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._completion_task: asyncio.Task[None] | None = None
        self._pipeline_task: PipelineTask | None = None
        self._transport: LocalAudioRuntimeTransport | None = None
        self._session: VoiceSessionContext | None = None
        self._events: RuntimeEvents | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._last_audio_activity_at: float | None = None
        self._last_transcript_at: float | None = None
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=80)

    def status(self) -> dict[str, Any]:
        running = self._task is not None and not self._task.done()
        playback_active = self._transport.is_playback_active() if self._transport else False
        return {
            "ok": True,
            "running": running,
            "playbackActive": playback_active,
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
        self._last_audio_activity_at = None
        self._last_transcript_at = None
        self._recent_events.clear()
        transport = LocalAudioRuntimeTransport(
            sample_rate=session.sample_rate,
            channels=session.channels,
            events=self._events,
        )
        self._transport = transport

        def on_task_ready(task: PipelineTask) -> None:
            self._pipeline_task = task
            self._completion_task = asyncio.create_task(
                AgentCompletionSubscriber(
                    session=session,
                    events=self._events,
                    task=task,
                    playback_active=transport.is_playback_active,
                ).run()
            )
            logger.info(
                "iris.voice.local_audio.pipeline_ready session={} device={}",
                session.session_id,
                session.device_id,
            )

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
                if self._transport is transport:
                    self._transport = None
                logger.info(
                    "iris.voice.local_audio.stopped session={} device={}",
                    session.session_id,
                    session.device_id,
                )

        self._task = asyncio.create_task(run())
        self._watchdog_task = asyncio.create_task(self._watchdog(session))
        await asyncio.sleep(0)
        return self.status()

    async def stop(self, *, reason: str = "stopped") -> dict[str, Any]:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None
        return await self._stop_pipeline(reason=reason)

    async def stop_speaking(self, *, reason: str = "user_stop_speaking") -> dict[str, Any]:
        interrupted = False
        if self._transport is not None:
            interrupted = await self._transport.interrupt_playback(reason=reason)
        logger.info(
            "iris.voice.local_audio.stop_speaking reason={} interrupted={}",
            reason,
            interrupted,
        )
        return {
            **self.status(),
            "interrupted": interrupted,
            "reason": reason,
        }

    async def _stop_pipeline(self, *, reason: str) -> dict[str, Any]:
        if self._completion_task is not None and not self._completion_task.done():
            self._completion_task.cancel()
            try:
                await self._completion_task
            except asyncio.CancelledError:
                pass
        self._completion_task = None
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

    async def _watchdog(self, session: VoiceSessionContext) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                if self._task is None or self._task.done():
                    return
                if self._session is None or self._session.session_id != session.session_id:
                    return
                if self._is_stale_transcription_stream():
                    reason = self._stale_audio_reason()
                    logger.warning(
                        "iris.voice.local_audio.watchdog_restart session={} device={} reason={}",
                        session.session_id,
                        session.device_id,
                        reason,
                    )
                    self._last_error = f"Restarted stale audio stream: {reason}"
                    await self._stop_pipeline(reason=reason)
                    if self._session is not None and self._session.session_id == session.session_id:
                        await self.start(session)
                    return
        except asyncio.CancelledError:
            return

    def _is_stale_transcription_stream(self) -> bool:
        running = self._task is not None and not self._task.done()
        if not running or not self._started_at:
            return False
        now = time.time()
        if now - self._started_at < 90:
            return False
        last_audio_at = self._last_audio_activity_at
        if last_audio_at is None or now - last_audio_at > 45:
            return True
        return False

    def _stale_audio_reason(self) -> str:
        last_audio_at = self._last_audio_activity_at
        if last_audio_at is None:
            return "no_audio_frames"
        if time.time() - last_audio_at > 45:
            return "audio_input_stalled"
        return "audio_stream_active"

    def _remember_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if not event_type:
            return
        now = time.time()
        if event_type == "audio.activity":
            self._last_audio_activity_at = now
            return
        if event_type in {"transcript.final", "transcript.interim"}:
            self._last_transcript_at = now
        self._recent_events.append(
            {
                "type": event_type,
                "text": event.get("text"),
                "reason": event.get("reason"),
                "speaker": event.get("speaker"),
                "speakerDisplayName": event.get("speakerDisplayName"),
                "speakerUserId": event.get("speakerUserId"),
                "speakerConfidence": event.get("speakerConfidence"),
                "at": now,
            }
        )
