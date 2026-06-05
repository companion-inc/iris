# Follow-up Routing and Sound Cues

## Finding

- Live logs showed the follow-up sentence was transcribed after Iris asked a question, but no model turn followed it.
- The wake strategy was marked as expecting a follow-up, but the transcript relay had no separate follow-up state to force the next final transcript through as the current user turn.
- Existing UI sound effects were mapped, but volumes were configured at 0.07-0.12, which is too quiet for reliable acknowledgement cues.

## Decision

- Keep voice/STT/turn-routing logic in the Pipecat sidecar.
- Store an explicit follow-up window in `RuntimeEvents` when Iris finishes a question.
- Let `TranscriptRelay` route the next non-echo final transcript as a follow-up turn, even while the playback-active flag is clearing.
- Keep playback echo filtering before chat routing so Iris's own words are still shown only when appropriate and never become a user turn.
- Raise native UI sound-effect volumes and cover the minimum volume in Swift tests.

## Verification

- Targeted follow-up relay tests passed.
- Voice contract suite passed with only the pre-existing camera capture check omitted because ffmpeg camera capture timed out.
- `swift test` passed for `apps/iris-mac`.
