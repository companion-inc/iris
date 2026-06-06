# VAD-Gated STT

## Finding

- Iris used Pipecat `DeepgramSTTService`, which inherits continuous `STTService`.
- Pipecat `VADProcessor` forwards audio immediately and emits VAD events separately.
- Deepgram `vad_events`, endpointing, and utterance end settings do not prevent audio from being sent to Deepgram.
- Pipecat `SegmentedSTTService` documents the desired pattern: use VAD to run STT only on speech segments, but Deepgram's streaming service does not inherit that class.

## Decision

- Keep local mic capture, audio activity, sound recognition, and VAD always on in the sidecar.
- Add `VADSpeechAudioGate` between `VADProcessor` and `DeepgramSTTService`.
- The gate buffers one second of pre-roll, flushes it on VAD start, forwards audio only while VAD says the user is speaking, and passes VAD stop through so Deepgram finalization still runs.
- LLM routing remains separate: wake phrase, follow-up, and interruption policy still decide whether a transcript reaches Gemini.

## Verification

- Added a contract test proving non-speech audio is not forwarded, VAD start flushes pre-roll, speech audio is forwarded, and post-stop audio is buffered again.
- `./scripts/voice-completion-contract-test.py` passed.
- Rebuilt and reopened `/Users/advaitpaliwal/Applications/Iris.app`.
- Live pipeline logs show `VADProcessor -> VADSpeechAudioGate -> DeepgramSTTService`.
- Live status after restart showed no transcript events during a quiet check window.
