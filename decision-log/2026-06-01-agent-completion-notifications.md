# Agent Completion Notifications

## Observation
- Live voice log at `~/Library/Logs/Iris/iris-voice.log` showed the Granola request started with `delivery=auto`.
- The final start response contained a Codex turn id but no local `runId`, so the Python voice layer could not register a pending tool result for the later Swift completion.
- The live session endpoint `/v1/voice/sessions/voice_f48109fd437f4f73b7e58e92db7d6c0d/agent/completions` returned no pending completions.

## Decision
- Preserve the Swift local `agent_run_*` id as `runId` in the voice agent response.
- Default voice-started agent delivery to `speak` unless the user explicitly asks for silent, saved, background, or no-interruption behavior.
- Update the prompt so the model waits for async completion automatically instead of requiring a user status request.

## Verification Plan
- Run the voice completion contract test.
- Run the Mac app test suite.
- Reinstall/reopen Iris, trigger a simple voice-started Codex task, and confirm a completion row is created and spoken without asking status.

## Verification Result
- `uv run ../../scripts/voice-completion-contract-test.py` passed.
- `pnpm voice:check` passed.
- `swift build` in `apps/iris-mac` passed.
- `swift test` initially compiled app/test sources but failed at the SwiftPM test runner link step because SwiftPM generated a Swift Testing runner without linking `Testing.framework`.
- Added explicit `Testing.framework` linkage to the SwiftPM test target; `swift test` now passes 13 XCTest tests with 0 failures.
- The voice completion contract test no longer emits Pipecat error/traceback/runtime-warning output from direct local-audio processor tests; those tests now exercise the local state handlers directly.
- Reinstalled and reopened Iris with `pnpm mac:open`.
- Started native voice through `/debug/native-voice/start`; logs showed `iris.voice.local_audio.pipeline_ready` and `iris.voice.agent_completion_subscribed ... transport=polling`.
- Triggered run `agent_run_31cc008b76d746a1bfd9a3e8e79786bc`; completion `agent_completion_e9cbb369e4cd4d2ba63785952136c019` was enqueued, injected, spoken via XAI TTS, and marked delivered at `2026-06-01T06:56:25.602Z`.
- `/debug/native-voice` showed live transcript from Iris: `Done. Notification smoke two is set.`

## 2026-06-01 Stale Local Audio Follow-Up

## Observation
- Live app status showed local audio running for roughly 7 hours with only a stale interim transcript `So` and no new local-audio events.
- `~/Library/Logs/Iris/iris-voice.log` for that old session showed CoreAudio `PaMacCore (AUHAL)` `err='-50'`, then repeated zero-gain input logs and no live transcription progress.
- After stopping local audio and starting a fresh native voice session, a controlled `say 'Iris can you hear me'` round trip produced Deepgram final transcript `Iris you hear me?`, wake detection, Gemini response generation, XAI TTS generation, and `iris.voice.local_audio.speaker_write ... written=True`.

## Decision
- Treat a long-running local-audio pipeline with no audio activity, or audio activity stalled for more than 45 seconds, as stale and restart it from the watchdog.
- Clear Swift live transcripts and local event de-duplication whenever native voice starts, stops, or observes a new local-audio session id.

## Verification Result
- `pnpm voice:check` passed.
- `uv run ../../scripts/voice-completion-contract-test.py` passed and the `/tmp/iris-voice-contract.log` scan found no `ERROR`, `Traceback`, `RuntimeWarning`, or `exception`.
- `swift test` in `apps/iris-mac` passed 14 XCTest tests with 0 failures, including the new session-change transcript clearing test.
