from __future__ import annotations

import asyncio
import audioop
from array import array

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams


class MacVoiceProcessingInputTransport(BaseInputTransport):
    """Pipecat input transport using macOS voice-processing I/O."""

    def __init__(self, params: TransportParams):
        super().__init__(params)
        self._engine = None
        self._input_node = None
        self._source_sample_rate = 0
        self._target_sample_rate = 0
        self._channels = 1
        self._ratecv_state = None
        self._callback_frames = 0
        self._tap_block = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._engine is not None:
            return

        import AVFoundation

        self._target_sample_rate = self._params.audio_in_sample_rate or frame.audio_in_sample_rate
        self._channels = self._params.audio_in_channels
        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        self._input_node = self._engine.inputNode()
        output_node = self._engine.outputNode()

        input_ok, input_error = self._input_node.setVoiceProcessingEnabled_error_(True, None)
        output_ok, output_error = output_node.setVoiceProcessingEnabled_error_(True, None)
        if not input_ok:
            raise RuntimeError(f"failed to enable macOS voice-processing input: {input_error}")
        if not output_ok:
            raise RuntimeError(f"failed to enable macOS voice-processing output: {output_error}")

        source_format = self._input_node.outputFormatForBus_(0)
        self._source_sample_rate = int(source_format.sampleRate())
        fmt = AVFoundation.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVFoundation.AVAudioPCMFormatFloat32,
            source_format.sampleRate(),
            1,
            False,
        )
        buffer_size = max(480, int(self._source_sample_rate / 50))
        self._tap_block = self._tap
        self._input_node.installTapOnBus_bufferSize_format_block_(0, buffer_size, fmt, self._tap_block)
        started, error = self._engine.startAndReturnError_(None)
        if not started:
            self._input_node.removeTapOnBus_(0)
            raise RuntimeError(f"failed to start macOS voice-processing audio engine: {error}")

        logger.info(
            "iris.voice.mac_voice_processing.started source_sample_rate={} target_sample_rate={} source_channels={} target_channels={}",
            self._source_sample_rate,
            self._target_sample_rate,
            int(source_format.channelCount()),
            self._channels,
        )
        await self.set_transport_ready(frame)

    async def cleanup(self):
        await super().cleanup()
        if self._input_node is not None:
            try:
                self._input_node.removeTapOnBus_(0)
            except Exception:
                pass
            self._input_node = None
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        self._ratecv_state = None
        self._tap_block = None

    def _tap(self, buffer, _when) -> None:
        if self._target_sample_rate <= 0:
            return
        audio = self._buffer_to_mono_pcm16(buffer)
        if not audio:
            return
        if self._source_sample_rate != self._target_sample_rate:
            audio, self._ratecv_state = audioop.ratecv(
                audio,
                2,
                1,
                self._source_sample_rate,
                self._target_sample_rate,
                self._ratecv_state,
            )
        frame = InputAudioRawFrame(
            audio=audio,
            sample_rate=self._target_sample_rate,
            num_channels=self._channels,
        )
        self._callback_frames += 1
        if self._callback_frames == 1 or self._callback_frames % 100 == 0:
            logger.info(
                "iris.voice.mac_voice_processing.input_frame frames={} bytes={} sample_rate={} channels={} rms={}",
                self._callback_frames,
                len(audio),
                self._target_sample_rate,
                self._channels,
                audioop.rms(audio, 2) if audio else 0,
            )
        asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame), self.get_event_loop())

    def _buffer_to_mono_pcm16(self, buffer) -> bytes:
        frame_count = int(buffer.frameLength())
        if frame_count <= 0:
            return b""
        channels = int(buffer.format().channelCount())
        data = buffer.floatChannelData()
        if not data or channels <= 0:
            return b""

        samples = array("h")
        primary_channel = data[0]
        for index in range(frame_count):
            value = max(-1.0, min(1.0, float(primary_channel[index])))
            samples.append(int(value * 32767))
        return samples.tobytes()
