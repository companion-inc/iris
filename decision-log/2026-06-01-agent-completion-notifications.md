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
- `swift test` compiled app/test sources but failed at the SwiftPM test runner link step because this local Xcode/SwiftPM setup could not link the `Testing` library.
- Reinstalled and reopened Iris with `pnpm mac:open`.
- Started native voice through `/debug/native-voice/start`; logs showed `iris.voice.local_audio.pipeline_ready` and `iris.voice.agent_completion_subscribed ... transport=polling`.
- Triggered run `agent_run_31cc008b76d746a1bfd9a3e8e79786bc`; completion `agent_completion_e9cbb369e4cd4d2ba63785952136c019` was enqueued, injected, spoken via XAI TTS, and marked delivered at `2026-06-01T06:56:25.602Z`.
- `/debug/native-voice` showed live transcript from Iris: `Done. Notification smoke two is set.`
