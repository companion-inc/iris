# Voice VAD Runtime Debug

Date: 2026-05-31

## Finding

The desktop app captured microphone audio and stored transcripts, but no assistant response was produced after the latest wake phrase.

Evidence:

- Iris, the Swift local API, the voice service, and speaker-id were all running.
- The app held the microphone attribution in macOS Control Center.
- The voice service websocket was connected to the app.
- The current transcript database stored `iris can you hear me` and `hello iris`.
- The voice log repeated `SileroVADAnalyzer object has no attribute '_vad_frames_num_bytes'` from `VADProcessor` and `LLMUserAggregator`.

## Decision

Initialize every manually-created Silero VAD analyzer with Pipecat's Silero-compatible 16 kHz sample rate and call `set_sample_rate()` before giving it to Pipecat processors or aggregators.

Pipecat 1.2.1 computes `_vad_frames_num_bytes` inside `VADAnalyzer.set_params()`, which is called by `set_sample_rate()`. Creating `SileroVADAnalyzer(params=...)` alone leaves the analyzer incomplete for direct use.

The native desktop audio stream can be 48 kHz, but Silero VAD rejects that rate. The websocket boundary resamples inbound PCM to 16 kHz for Pipecat input, VAD, and STT while TTS output stays at the native session rate.

## Verification

After the patch, restart Iris and confirm:

- `~/Library/Logs/Iris/iris-voice.log` no longer emits `_vad_frames_num_bytes`.
- A wake phrase creates a device transcript and a later assistant transcript.
- The app increments native voice output frames or emits assistant audio through the voice websocket.
