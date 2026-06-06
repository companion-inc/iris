from __future__ import annotations

import asyncio
import audioop
import os

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams


class FFmpegAVFoundationInputTransport(BaseInputTransport):
    """Pipecat input transport that binds ffmpeg to a specific AVFoundation audio device."""

    def __init__(self, params: TransportParams):
        super().__init__(params)
        self._sample_rate = 0
        self._channels = 1
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._frames = 0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._reader_task is not None:
            return

        self._sample_rate = self._params.audio_in_sample_rate or frame.audio_in_sample_rate
        self._channels = self._params.audio_in_channels
        await self.set_transport_ready(frame)
        self._reader_task = asyncio.create_task(self._run_ffmpeg_reader())

    async def cleanup(self):
        await super().cleanup()
        await self._stop_reader()

    async def stop(self, frame):
        await super().stop(frame)
        await self._stop_reader()

    async def cancel(self, frame):
        await super().cancel(frame)
        await self._stop_reader()

    async def _stop_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _run_ffmpeg_reader(self) -> None:
        device = _avfoundation_audio_input()
        chunk_bytes = max(640, int(self._sample_rate * self._channels * 2 / 50))
        command = _ffmpeg_command(
            device=device,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )
        logger.info(
            "iris.voice.ffmpeg_avfoundation_input.started device={} sample_rate={} channels={} chunk_bytes={}",
            device,
            self._sample_rate,
            self._channels,
            chunk_bytes,
        )
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        stderr_task = asyncio.create_task(self._log_stderr(self._process.stderr))
        try:
            while True:
                audio = await self._process.stdout.readexactly(chunk_bytes)
                frame = InputAudioRawFrame(
                    audio=audio,
                    sample_rate=self._sample_rate,
                    num_channels=self._channels,
                )
                self._frames += 1
                if self._frames == 1 or self._frames % 100 == 0:
                    logger.info(
                        "iris.voice.ffmpeg_avfoundation_input.input_frame frames={} bytes={} sample_rate={} channels={} rms={}",
                        self._frames,
                        len(audio),
                        self._sample_rate,
                        self._channels,
                        audioop.rms(audio, 2) if audio else 0,
                    )
                await self.push_audio_frame(frame)
        except asyncio.IncompleteReadError:
            logger.info("iris.voice.ffmpeg_avfoundation_input.eof")
        except asyncio.CancelledError:
            raise
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

    async def _log_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            logger.info("iris.voice.ffmpeg_avfoundation_input.ffmpeg {}", line.decode(errors="replace").strip())


def _avfoundation_audio_input() -> str:
    explicit = os.getenv("IRIS_AVFOUNDATION_AUDIO_INPUT")
    if explicit:
        return explicit
    index = os.getenv("IRIS_AVFOUNDATION_AUDIO_INPUT_INDEX", "1")
    return f":{index}"


def _ffmpeg_command(*, device: str, sample_rate: int, channels: int) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-i",
        device,
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
