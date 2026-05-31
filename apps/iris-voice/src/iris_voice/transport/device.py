from __future__ import annotations

import base64
import asyncio
import time
from typing import Callable, Awaitable

from fastapi import WebSocket
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams


class DeviceInputTransport(BaseInputTransport):
    def __init__(self, params: TransportParams):
        super().__init__(params)
        self._sample_rate = params.audio_in_sample_rate or 16000

    async def start(self, frame):
        await super().start(frame)
        self._sample_rate = self._params.audio_in_sample_rate or frame.audio_in_sample_rate
        await self.set_transport_ready(frame)

    async def push_pcm(self, audio: bytes, sample_rate: int, channels: int):
        if not hasattr(self, "_audio_in_queue"):
            self._create_audio_task()
        await self.push_audio_frame(
            InputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=channels)
        )


class DeviceOutputTransport(BaseOutputTransport):
    def __init__(
        self,
        websocket: WebSocket,
        params: TransportParams,
        *,
        on_send_failed: Callable[[Exception], Awaitable[None]] | None = None,
    ):
        super().__init__(params)
        self._websocket = websocket
        self._on_send_failed = on_send_failed
        self._audio_frames = 0
        self._audio_bytes = 0
        self._closed = False
        self._current_tts_context_id: str | None = None
        self._interrupted_tts_context_ids: set[str] = set()
        self._dropped_interrupted_audio_frames = 0
        self._drop_audio_until_tts_stop = False
        self._bot_speaking = False
        self._device_playback_active = False
        self._send_interval = 0.0
        self._next_send_time = 0.0

    async def start(self, frame):
        await super().start(frame)
        bytes_per_second = self.sample_rate * self._params.audio_out_channels * 2
        self._send_interval = self.audio_chunk_size / bytes_per_second if bytes_per_second else 0
        await self.set_transport_ready(frame)

    def close(self) -> None:
        self._closed = True

    def is_playback_active(self) -> bool:
        return self._device_playback_active

    async def interrupt_playback(self, *, reason: str) -> bool:
        if not (self._bot_speaking or self._device_playback_active):
            logger.info(
                "iris.voice.output_interrupt_direct_inactive reason={} bot_speaking={} device_playback_active={} context_id={}",
                reason,
                self._bot_speaking,
                self._device_playback_active,
                self._current_tts_context_id,
            )
            return False
        if self._current_tts_context_id:
            self._interrupted_tts_context_ids.add(self._current_tts_context_id)
        self._drop_audio_until_tts_stop = True
        logger.info(
            "iris.voice.output_interrupt_direct_applied reason={} bot_speaking={} device_playback_active={} context_id={}",
            reason,
            self._bot_speaking,
            self._device_playback_active,
            self._current_tts_context_id,
        )
        await self._send_bot_stopped(reason=reason)
        return True

    async def _send_json(self, payload: dict) -> bool:
        if self._closed:
            logger.debug("iris.voice.transport_send_skipped_closed type={}", payload.get("type"))
            return False
        try:
            await self._websocket.send_json(payload)
            return True
        except Exception as error:
            self._closed = True
            encoded_audio = payload.get("audio")
            logger.warning(
                "iris.voice.transport_send_failed type={} keys={} encoded_audio_chars={} closed={} error={}: {}",
                payload.get("type"),
                list(payload.keys())[:12],
                len(encoded_audio) if isinstance(encoded_audio, str) else 0,
                self._closed,
                type(error).__name__,
                error,
            )
            if self._on_send_failed:
                await self._on_send_failed(error)
            return False

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        self._audio_frames += 1
        self._audio_bytes += len(frame.audio)
        if self._audio_frames == 1 or self._audio_frames % 50 == 0:
            logger.info(
                "iris.voice.audio_output_sent frames={} bytes={} sample_rate={} channels={}",
                self._audio_frames,
                self._audio_bytes,
                frame.sample_rate,
                frame.num_channels,
            )
        sent = await self._send_json(
            {
                "type": "audio",
                "sampleRate": frame.sample_rate,
                "channels": frame.num_channels,
                "audio": base64.b64encode(frame.audio).decode("ascii"),
            }
        )
        if sent:
            await self._write_audio_sleep()
        return sent

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        name = frame.__class__.__name__
        if direction == FrameDirection.DOWNSTREAM and name == "BotStartedSpeakingFrame":
            await self._send_bot_started()
        elif direction == FrameDirection.DOWNSTREAM and name == "BotStoppedSpeakingFrame":
            await self._send_bot_stopped(reason="completed")
        await super().push_frame(frame, direction)

    async def write_transport_frame(self, frame: Frame):
        name = frame.__class__.__name__
        if name == "BotStartedSpeakingFrame":
            await self._send_bot_started()
            return
        if name == "BotStoppedSpeakingFrame":
            await self._send_bot_stopped(reason="completed")
            return
        await super().write_transport_frame(frame)

    async def _send_bot_started(self) -> None:
        if self._device_playback_active:
            return
        self._bot_speaking = True
        self._device_playback_active = True
        self._next_send_time = time.monotonic()
        self._dropped_interrupted_audio_frames = 0
        self._drop_audio_until_tts_stop = False
        logger.info("iris.voice.bot_speaking_started context_id={}", self._current_tts_context_id)
        await self._send_json({"type": "assistant.audio.started"})

    async def _send_bot_stopped(self, *, reason: str) -> None:
        was_active = self._bot_speaking or self._current_tts_context_id is not None or self._device_playback_active
        if reason == "completed" and not was_active:
            logger.info("iris.voice.bot_speaking_stopped_skipped reason=completed was_active=false")
            return
        self._bot_speaking = False
        self._current_tts_context_id = None
        self._device_playback_active = False
        logger.info("iris.voice.bot_speaking_stopped reason={} was_active={}", reason, was_active)
        await self._send_json({"type": "assistant.audio.stopped", "reason": reason})

    async def _write_audio_sleep(self) -> None:
        if self._send_interval <= 0:
            return
        current_time = time.monotonic()
        sleep_duration = max(0, self._next_send_time - current_time)
        await asyncio.sleep(sleep_duration)
        if sleep_duration == 0:
            self._next_send_time = time.monotonic() + self._send_interval
        else:
            self._next_send_time += self._send_interval

    async def process_frame(self, frame: Frame, direction):
        if isinstance(frame, TTSStartedFrame):
            self._current_tts_context_id = frame.context_id
            self._drop_audio_until_tts_stop = False
        elif isinstance(frame, TTSStoppedFrame):
            if frame.context_id is None or frame.context_id == self._current_tts_context_id:
                self._current_tts_context_id = None
                self._drop_audio_until_tts_stop = False
            logger.info(
                "iris.voice.tts_stopped context_id={} bot_speaking={}",
                frame.context_id,
                self._bot_speaking,
            )
        elif isinstance(frame, InterruptionFrame):
            interrupted = await self.interrupt_playback(reason="interruption")
            if not interrupted:
                logger.info(
                    "iris.voice.output_interruption_inactive bot_speaking={} device_playback_active={} context_id={}",
                    self._bot_speaking,
                    self._device_playback_active,
                    self._current_tts_context_id,
                )
        elif isinstance(frame, (CancelFrame, EndFrame)):
            await self._send_bot_stopped(reason=frame.__class__.__name__)
        elif isinstance(frame, OutputAudioRawFrame):
            if self._drop_audio_until_tts_stop:
                self._dropped_interrupted_audio_frames += 1
                if self._dropped_interrupted_audio_frames == 1 or self._dropped_interrupted_audio_frames % 50 == 0:
                    logger.info(
                        "iris.voice.audio_output_dropped_after_interruption frames={}",
                        self._dropped_interrupted_audio_frames,
                    )
                return
            context_id = getattr(frame, "context_id", None)
            if isinstance(context_id, str) and context_id:
                if context_id in self._interrupted_tts_context_ids:
                    self._dropped_interrupted_audio_frames += 1
                    if self._dropped_interrupted_audio_frames == 1 or self._dropped_interrupted_audio_frames % 50 == 0:
                        logger.info(
                            "iris.voice.audio_output_dropped_interrupted context_id={} frames={}",
                            context_id,
                            self._dropped_interrupted_audio_frames,
                        )
                    return
                self._current_tts_context_id = context_id
            self._bot_speaking = True
        await super().process_frame(frame, direction)


class DeviceTransport(BaseTransport):
    def __init__(
        self,
        websocket: WebSocket,
        params: TransportParams,
        *,
        on_send_failed: Callable[[Exception], Awaitable[None]] | None = None,
    ):
        super().__init__()
        self._websocket = websocket
        self._params = params
        self._on_send_failed = on_send_failed
        self._input: DeviceInputTransport | None = None
        self._output: DeviceOutputTransport | None = None

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = DeviceInputTransport(self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = DeviceOutputTransport(
                self._websocket,
                self._params,
                on_send_failed=self._on_send_failed,
            )
        return self._output

    def close(self) -> None:
        if self._output:
            self._output.close()

    def is_playback_active(self) -> bool:
        return bool(self._output and self._output.is_playback_active())

    async def interrupt_playback(self, *, reason: str) -> bool:
        if not self._output:
            return False
        return await self._output.interrupt_playback(reason=reason)

    async def push_pcm(self, audio: bytes, sample_rate: int, channels: int):
        if self._input:
            await self._input.push_pcm(audio, sample_rate, channels)
