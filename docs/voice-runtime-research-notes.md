# Voice Runtime Research Notes

Iris is now a local desktop voice app. The active runtime is the native Swift
macOS app plus a local Pipecat sidecar over a raw PCM websocket. LiveKit is not
on the active desktop path unless a future room/telephony product requirement
brings it back.

## Current Docs Checked

- Pipecat docs, current Context7 source `/pipecat-ai/docs`.
- LiveKit Agents docs, current Context7 source `/websites/livekit_io_agents`.
- Current repo memory for prior Iris Pipecat decisions.

## Pipecat Fit

The current Iris shape matches Pipecat's documented pipeline model:

```txt
transport.input
  -> STT
  -> LLM user context aggregator
  -> LLM with tools
  -> TTS
  -> transport.output
  -> assistant context aggregator
```

Function/tool calls should return through Pipecat function-call callbacks, and
`FunctionCallResultProperties` is the right control point:

- `run_llm=false` for terminal no-speech/noop results.
- `run_llm=true` when the model should speak after reading tool output.
- `is_final=false` only for accepted/progress updates before the final result.

Iris now follows that contract for `noop`, `shell_exec`, and `agent`.

## Turn Policy

Pipecat 1.x centralizes turn starts/stops in user turn strategies. Iris uses
that shape:

- Wake phrase strategy first unless a session starts intentionally awake.
- Playback wake gate before VAD/transcription starts so playback can only be
interrupted by a wake phrase.
- VAD and transcription start strategies do not auto-interrupt.
- Speech timeout is the current stop strategy.

The docs' default path uses VAD plus smarter turn stop detection. Iris uses a
timeout stop for now because wake-gated desktop commands need predictable
behavior more than conversational overlap. If end-of-turn latency becomes the
main issue, the next research target is a local Smart Turn stop strategy behind
the existing `build_context_aggregators` seam.

## LiveKit Fit

LiveKit Agents is useful when Iris needs LiveKit rooms, remote participants,
telephony, or room-level RPC tools. The current local desktop app does not need
those primitives. Keeping Pipecat with a local websocket is simpler and avoids
reintroducing hosted room infrastructure into the default desktop path.

## Required E2E Proof

The local desktop proof must not depend on Orb/Pi hardware. The required checks
are:

- Native app launches through `pnpm desktop:start`.
- Swift API, Swift Codex bridge, Pipecat voice sidecar, and speaker-id sidecar
  are healthy on local ports.
- Synthesized spoken audio reaches the websocket and produces `wake.accepted`,
  transcript events, assistant text, and assistant audio.
- A spoken Codex-agent request produces `tool.started:agent`, tool results, and
  a spoken response.
- A simple spoken command may use `shell_exec`, but it must run in the
  configured Iris workspace and finish quickly.

## Current Design Decisions

- `shell_exec` is async but not detached: it awaits a short subprocess result
  and returns stdout/stderr/exit code to the LLM.
- Long-running or interpretive work belongs to the Codex `agent` tool.
- The desktop app is the process supervisor. Test scripts should never SSH into
  hardware by default.
