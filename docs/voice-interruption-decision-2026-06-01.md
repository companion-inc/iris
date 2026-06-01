# Voice Interruption Decision - 2026-06-01

## Evidence

- BlackHole end-to-end session `voice_e7fc26b41ffc4cf2b29487146b4aa89a` transcribed `Iris, stop and say interrupted.`
- The log emitted `iris.voice.playback_wake_gate interrupt=true` at `2026-05-31 22:52:45.429`.
- No `InterruptionFrame` reached the local audio output after that line.
- The previous assistant TTS continued until `2026-05-31 22:53:08.128`, so wake-word interruption was detected but playback was not cancelled.

## Root Cause

Local playback was marked active only after the first speaker write. During the gap between `TTSStartedFrame` and speaker output, ordinary VAD started a user turn with interruptions disabled. The later wake phrase arrived inside that already-open turn, so Pipecat suppressed the new interrupting turn.

## Decision

Treat local TTS as active at `TTSStartedFrame`, not first speaker write. On `InterruptionFrame`, close the local output stream and drop following audio frames until `TTSStoppedFrame`.

This keeps SwiftUI as UI only and keeps the voice loop in Pipecat.
