# Iris Local Pipecat Voice Decision

Date: 2026-05-31

## Decision

Swift stays the native shell for UI, settings, process supervision, session creation, and local HTTP control. Pipecat owns the voice loop: microphone input, VAD, Deepgram STT, wake gating, LLM/tool flow, TTS, playback, and playback echo suppression.

## Evidence

- Old working desktop code at `3175ecde:apps/iris-desktop/local-voice/voice.py` used Pipecat `LocalAudioTransport` with local microphone input and local audio output.
- Current Pipecat docs describe transports as the media boundary and place `transport.input()` before STT and `transport.output()` after TTS.
- LiveKit docs support the same voice-agent shape for production pipelines: STT, LLM, and TTS are separate stages with observable text/tool boundaries. LiveKit is useful when a room/WebRTC participant is required; it is not required for this local single-Mac desktop path.

## Runtime Result

- Removed the live Swift hidden `WKWebView` microphone capture and custom Swift audio websocket path.
- Added Python `/local-audio/start`, `/local-audio/stop`, and `/local-audio/status` endpoints that start the Pipecat local audio runtime from the existing signed voice session.
- Added Pipecat local playback state tracking so the existing playback echo guard works with `LocalAudioTransport`.
- Added deterministic speech for short `shell_exec` stdout so quick shell commands do not depend on a second model pass before speaking.

## Verification

- `python3 -m py_compile apps/iris-voice/src/iris_voice/server.py apps/iris-voice/src/iris_voice/local_audio.py apps/iris-voice/src/iris_voice/pipeline.py apps/iris-voice/src/iris_voice/turns/wake.py apps/iris-voice/src/iris_voice/tools.py`
- `cd apps/iris-voice && uv run python -c "from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams; print('local audio import ok')"`
- `xcrun swift test --disable-sandbox --scratch-path /private/tmp/iris-mac-build`
- `pnpm mac:open`
- `POST http://127.0.0.1:4747/debug/native-voice/start`
- `GET http://127.0.0.1:4748/local-audio/status`
- End-to-end audio test with macOS `say 'Iris, what time is it?'` through the running desktop app produced wake detection, transcript, assistant text, and local audio playback from the Pipecat runtime.
