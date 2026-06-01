# Iris Voice Event Feed Decision

Date: 2026-05-31

## Decision

Keep microphone activity as an internal liveness signal for the local Pipecat watchdog. Do not expose `audio.activity` through the Swift-facing `recentEvents` feed, and ignore it defensively in the Swift runtime.

## Evidence

- Live status before the fix showed `/local-audio/status` filling with `audio.activity` every 10 seconds while Swift `/debug/native-voice` reported `lastEvent: "audio.activity"`.
- The same runtime had already heard the user, accepted wake, generated assistant text, and wrote TTS audio, so the failing boundary was the user-facing event projection, not the Pipecat voice loop.
- Pipecat docs describe local audio transport as PyAudio-based local input/output and frame processors as pass-through pipeline processors. The current Swift app should keep Pipecat as the audio loop and only consume user-visible runtime events.

## Verification

- `pnpm voice:check`
- `swift test --package-path apps/iris-mac --disable-sandbox --scratch-path /private/tmp/iris-mac-build`
- `pnpm mac:open`
- Controlled BlackHole end-to-end test with `say 'Iris, say the word ready once.'` produced:
  - `transcript.final`: `Iris,`
  - `wake.detected` and `wake.accepted`
  - `transcript.interim`: `say the word ready`
  - `transcript.final`: `say the word ready once.`
  - `assistant.text`: `Ready`
  - `assistant.audio.started` and `assistant.audio.stopped`
- Swift `/debug/native-voice` then reported two live transcript rows and `lastEvent: "assistant.audio.stopped"`.
- After restoring MacBook Pro microphone and speakers, the fresh app stayed `Listening` and `/local-audio/status` no longer filled with `audio.activity`.
