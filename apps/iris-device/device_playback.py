from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from typing import Any, Callable

from device_audio import convert_pcm_channels, pcm_rms, playback_command_for_format
from device_xvf import set_amp_enabled, xvf_audio_state


LogFn = Callable[..., None]


class VoicePlayback:
    def __init__(
        self,
        cfg: Any,
        tasks: set[asyncio.Task[None]],
        active: asyncio.Event,
        log: LogFn,
    ) -> None:
        self._cfg = cfg
        self._tasks = tasks
        self._active = active
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None
        self._sample_rate = int(cfg.sample_rate)
        self._channels = int(cfg.playback_channels)
        self._bytes = 0
        self._frames = 0
        self._last_audio_at = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    async def write(self, source_audio: bytes, sample_rate: int, source_channels: int) -> bool:
        source_bytes = len(source_audio)
        playback_sample_rate = int(self._cfg.sample_rate)
        target_channels = int(self._cfg.playback_channels)
        if sample_rate != playback_sample_rate:
            self._log(
                "voice_playback_drop_sample_rate_mismatch",
                sourceSampleRate=sample_rate,
                playbackSampleRate=playback_sample_rate,
            )
            return True

        audio = convert_pcm_channels(source_audio, source_channels, target_channels)
        try:
            self._last_audio_at = time.monotonic()
            if self._needs_start(playback_sample_rate, target_channels):
                await self._finish_process(reason="restart", force=True)
                self._start(playback_sample_rate, sample_rate, source_channels, target_channels)
            await self._write_chunk(audio)
        except (BrokenPipeError, OSError, ValueError) as error:
            self._log("voice_playback_write_failed", error=str(error))
            await self.reset()
            return False

        self._bytes += len(audio)
        self._frames += 1
        self._schedule_idle_stop()
        if self._frames == 1 or self._frames % 50 == 0:
            self._log(
                "voice_playback_frame",
                frames=self._frames,
                sourceBytes=source_bytes,
                outputBytes=self._bytes,
                sourceRms=pcm_rms(source_audio),
                outputRms=pcm_rms(audio),
                sourceSampleRate=sample_rate,
                playbackSampleRate=playback_sample_rate,
                sourceChannels=source_channels,
                playbackChannels=target_channels,
            )
        return True

    async def stop(self, reason: str = "completed") -> None:
        self._cancel_idle_stop()
        force = reason in {"interruption", "CancelFrame", "EndFrame", "websocket_disconnected", "restart"}
        started_at = time.monotonic()
        frames = self._frames
        audio_bytes = self._bytes
        self._log(
            "voice_playback_stop_requested",
            reason=reason,
            force=force,
            active=self._active.is_set(),
            frames=frames,
            bytes=audio_bytes,
        )
        async with self._write_lock:
            return_code = await self._finish_process(reason=reason, force=force)
        self._reset_counters()
        self._active.clear()
        self._log(
            "voice_playback_stopped",
            reason=reason,
            force=force,
            frames=frames,
            bytes=audio_bytes,
            elapsedMs=int((time.monotonic() - started_at) * 1000),
            returnCode=return_code,
        )

    async def reset(self) -> None:
        self._cancel_idle_stop()
        async with self._write_lock:
            await self._finish_process(reason="reset", force=True)
        self._reset_counters()
        self._active.clear()

    async def close(self) -> None:
        await self.reset()
        for task in list(self._tasks):
            task.cancel()

    def _needs_start(self, sample_rate: int, channels: int) -> bool:
        return (
            self._process is None
            or self._process.stdin is None
            or self._process.stdin.closed
            or sample_rate != self._sample_rate
            or channels != self._channels
        )

    def _start(
        self,
        playback_sample_rate: int,
        source_sample_rate: int,
        source_channels: int,
        target_channels: int,
    ) -> None:
        self._log("xvf_audio_state_before_playback", **xvf_audio_state())
        self._log("xvf_amp_set", enabled=True, **set_amp_enabled(True))
        self._sample_rate = playback_sample_rate
        self._channels = target_channels
        command = playback_command_for_format(self._cfg, playback_sample_rate, target_channels)
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE)
        self._active.set()
        self._log(
            "voice_playback_start",
            command=" ".join(command),
            sourceSampleRate=source_sample_rate,
            playbackSampleRate=playback_sample_rate,
            sourceChannels=source_channels,
            playbackChannels=target_channels,
        )

    async def _finish_process(self, *, reason: str = "completed", force: bool = False) -> int | None:
        if self._process is None:
            self._log("xvf_amp_set", enabled=False, **set_amp_enabled(False))
            return None
        process = self._process
        if force:
            self._log("voice_playback_process_kill", reason=reason, pid=process.pid)
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
        elif process.stdin:
            try:
                await asyncio.to_thread(process.stdin.close)
            except OSError:
                pass
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=1 if force else 5)
        except asyncio.TimeoutError:
            self._log("voice_playback_process_timeout", reason=reason, force=force, pid=process.pid)
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=1)
        return_code = process.returncode
        self._process = None
        await self._close_stdin(process)
        self._log("xvf_amp_set", enabled=False, **set_amp_enabled(False))
        self._log("xvf_audio_state_after_playback", **xvf_audio_state())
        return return_code

    async def _write_chunk(self, audio: bytes) -> None:
        if self._process is None or self._process.stdin is None or self._process.stdin.closed:
            raise BrokenPipeError("playback stdin is unavailable")
        async with self._write_lock:
            if self._process is None or self._process.stdin is None or self._process.stdin.closed:
                raise BrokenPipeError("playback stdin is unavailable")
            await asyncio.to_thread(self._write_to_stdin, self._process.stdin, audio)

    @staticmethod
    def _write_to_stdin(stdin, audio: bytes) -> None:
        stdin.write(audio)
        stdin.flush()

    @staticmethod
    async def _close_stdin(process: subprocess.Popen[bytes]) -> None:
        stdin = process.stdin
        process.stdin = None
        if stdin is None or stdin.closed:
            return
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            await asyncio.to_thread(stdin.close)

    async def _stop_after_idle(self, expected_frames: int) -> None:
        await asyncio.sleep(float(self._cfg.playback_idle_stop_s))
        if self._process is None or self._frames != expected_frames:
            return
        async with self._write_lock:
            return_code = await self._finish_process(reason="idle", force=False)
        self._log(
            "voice_playback_idle_stop",
            frames=self._frames,
            bytes=self._bytes,
            idleSeconds=self._cfg.playback_idle_stop_s,
            returnCode=return_code,
        )
        self._reset_counters()
        self._active.clear()

    def _schedule_idle_stop(self) -> None:
        self._cancel_idle_stop()
        self._idle_task = asyncio.create_task(self._stop_after_idle(self._frames))
        self._tasks.add(self._idle_task)
        self._idle_task.add_done_callback(self._tasks.discard)

    def _cancel_idle_stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _reset_counters(self) -> None:
        self._bytes = 0
        self._frames = 0
