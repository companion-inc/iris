# Native voice sound effects

## Problem

The mac app did not expose separate sound asset files for wake, accepted, sent, and done events. Sound effects are generated as PCM tones inside `NativeVoiceRuntime`.

## Evidence

- `apps/iris-voice/src/iris_voice/pipeline.py` emits `wake.detected` followed immediately by `wake.accepted`.
- `apps/iris-mac/Sources/IrisMac/NativeVoiceRuntime.swift` maps voice event names to `NativeVoiceSoundEffect` values and synthesizes PCM WAV data for `AVAudioPlayer`.
- The previous implementation mapped both `wake.detected` and `wake.accepted` to the same `wake` effect and retained only one sound-effect player.

## Decision

Play the wake sound only for `wake.accepted`; keep `wake.detected` silent. Retain multiple short sound-effect players until playback finishes so rapid event sounds do not cancel each other.
