# 2026-06-05 Pipecat sidecar boundary

- Repo: /Users/advaitpaliwal/Companion/Code/iris, branch main.
- User requirement: mac app is UI only; all voice logic stays in apps/iris-voice and Pipecat.
- Current evidence: uncommitted Swift runtime-state edits were reverted; git status is clean before sidecar changes.
- Pipecat docs/source checked: LocalAudioTransport is PyAudio/PortAudio input/output; interruption is via Pipecat InterruptionFrame and user turn strategies.
- Live failure signal from iris-voice.log: while playback is active, STT receives Iris playback as mic transcripts, so prompt-only changes cannot make wake interruption reliable.
- Working hypothesis: fix must be a Pipecat-side sidecar transport/processor path, not Swift audio logic.

## Change

- Added `iris_voice.voice_filters.build_pipecat_input_filter()` to wire configured Pipecat `audio_in_filter` implementations into the sidecar transport params.
- Added Pipecat `aic` extra to the sidecar dependency so `pipecat.audio.filters.aic_filter.AICFilter` imports when configured.
- Removed `InputAudioAutoGain` from the runtime pipeline and deleted the old gain helper/test. This avoids amplifying Iris playback leakage into Deepgram/VAD.

## Verification

- `uv run python -m py_compile src/iris_voice/local_audio.py src/iris_voice/pipeline.py src/iris_voice/voice_filters.py` passed.
- `IRIS_PIPECAT_AUDIO_IN_FILTER=aic AIC_SDK_LICENSE=test uv run python -c ...` constructed `AICFilter`.
- `uv run ../../scripts/voice-completion-contract-test.py` passed, including wake interruption and playback echo contracts.

## Follow-up Change

- Added `MacVoiceProcessingInputTransport`, a Pipecat `BaseInputTransport` subclass in the Python sidecar.
- It uses PyObjC/AVFoundation from Python to enable Apple's macOS voice-processing I/O, captures input through a retained AVAudioEngine tap block, converts float input to mono PCM16, resamples 48 kHz input to Pipecat's 16 kHz STT path, and pushes `InputAudioRawFrame` into the same Pipecat pipeline.
- The AVAudioEngine tap uses the input node's output format and an explicit Float32 mono tap format. A direct Python harness proved callbacks arrive in the same Python environment; retaining the sidecar tap block fixed the installed-app callback loss.
- `LocalAudioRuntimeTransport` now uses this sidecar input path directly on macOS. The old Pipecat `LocalAudioTransport` input fallback was removed from this runtime.
- Added `InputAudioActivityRelay`, a sidecar Pipecat processor that marks input activity without changing audio samples, so the watchdog does not restart a healthy input stream.
- Swift remains UI/launcher only; no Swift audio/runtime logic was added.

## Live Verification

- Restarted `/Users/advaitpaliwal/Applications/Iris.app` with `./apps/iris-mac/scripts/open-macos.sh`.
- `/local-audio/status` returned `running=true`, `lastError=null`, `playbackActive=false`.
- `iris-voice.log` showed `iris.voice.local_audio.input_transport=mac_voice_processing`.
- `iris-voice.log` showed `iris.voice.mac_voice_processing.started source_sample_rate=48000 target_sample_rate=16000 source_channels=9 target_channels=1`.
- `iris-voice.log` showed `iris.voice.mac_voice_processing.input_frame frames=1 bytes=3200 sample_rate=16000 channels=1 rms=30`.
- `iris-voice.log` showed continuous frames through `frames=1400` over a 145 second live run with no `watchdog_restart`.
- `iris-voice.log` showed `iris.voice.input_audio_activity` every 10 seconds, proving the watchdog sees real Pipecat input frames rather than transcript timing.
- `iris-voice.log` showed `DeepgramSTTService#0: Websocket connection initialized`.
- BlackHole 2ch loopback check was restored safely afterward. The voice-processing sidecar captured frames from BlackHole but RMS stayed 0, so BlackHole did not prove speech transcription under macOS voice processing. Real mic input did prove the installed sidecar capture path.
