# Voice Runtime Readiness

This file is the working checklist for the real speaker loop. It should stay
close to the runtime code and logs.

## Runtime Truth

The current local runtime path is:

```txt
ReSpeaker/XVF input on Pi
  -> device websocket PCM
  -> Pipecat VAD
  -> Deepgram streaming STT
  -> TranscriptRelay for continuous transcript storage
  -> WakePhraseUserTurnStartStrategy first in user turn start strategies
  -> Gemini or custom chat-completions LLM
  -> TTS
  -> DeviceOutputTransport websocket PCM
  -> Pi aplay process
```

Pipecat owns wake gating, turn starts, and end-of-turn. The device owns hardware
capture, hardware playback, AEC path setup, and chime playback.

## Runtime Modules

The voice package is split by runtime boundary:

- `transport/`: device websocket transport and raw PCM input/output.
- `turns/`: wake phrase constants, playback wake gate, and Pipecat VAD construction.
- `observability/`: Pipecat frame observer and local frame diagnostics.
- top-level `pipeline.py`: assembly only. It should compose services and
  processors, not hide turn policy or transport behavior.

## Docs Checked

- Pipecat `WakePhraseUserTurnStartStrategy`: must be first in the start strategy
  list, defaults to a 10 second timeout, and timeout mode resets on activity.
- Pipecat playback wake gate: keep transcription and VAD live during assistant
  playback, but block non-wake speech from starting a user turn while audio is
  playing. If the transcript contains `Iris` during playback, start a user turn
  with Pipecat interruptions enabled so current audio stops immediately.
- Pipecat VAD/transcription start strategies: keep them after the wake phrase
  strategy and playback wake gate, but disable their automatic interruption
  frames. Normal follow-up speech is accepted while Iris is awake and not
  speaking; during playback only the wake phrase can interrupt.
- Pipecat observability examples: attach a custom `BaseObserver` to
  `PipelineTask` and log `InterruptionFrame`, `BotStartedSpeakingFrame`,
  `BotStoppedSpeakingFrame`, and user speaking frames at the processor boundary.
- Pipecat audio recording examples: if we need raw evidence, insert
  `AudioBufferProcessor` after `transport.output()` and handle
  `on_audio_data` / `on_track_audio_data`. We do not persist raw room audio by
  default until storage/privacy policy is explicit.
- Deepgram streaming STT: Nova-3 supports multilingual code-switching on live
  streams with `language=multi`; `interim_results`, `utterance_end_ms`,
  `vad_events`, and endpointing are the relevant knobs for live turn timing and
  finalization.

## Current Policy

- Saying `Iris` wakes the assistant path.
- Wake events are emitted from Pipecat's wake phrase strategy, not from a second
  transcript gate.
- Follow-up turns are allowed during Pipecat's wake timeout window.
- Interruptions do not mute user input. During playback, only a wake-gated
  Pipecat interruption stops device playback; non-wake transcripts are logged
  and cleared from LLM aggregation.
- Normal completion stays graceful: the Pi closes `aplay` stdin and waits for it
  to finish so the last syllable is not cut off.

## Required Log Chain

For a successful normal turn:

```txt
voice_ready
voice_transcript final=false/true speaker=...
voice_wake_detected
voice_wake_accepted
iris.voice.bot_speaking_started
voice_playback_active
voice_playback_frame
iris.voice.bot_speaking_stopped
voice_playback_stop_received reason=completed
voice_playback_stopped reason=completed force=false
```

For rejected echo while Iris is speaking:

```txt
iris.voice.playback_wake_gate ignored_non_wake final=true text='...'
voice_playback_frame
voice_playback_stop_received reason=completed
voice_playback_stopped reason=completed force=false
```

For an intentional interruption while Iris is speaking:

```txt
iris.voice.playback_wake_gate interrupt=true final=false text='Iris ...'
iris.voice.observer.frame frame=InterruptionFrame
iris.voice.output_interruption_applied bot_speaking=true
voice_playback_stop_received reason=interruption
voice_playback_stop_requested reason=interruption force=true
voice_playback_stopped reason=interruption force=true
```

## Manual E2E Tests

Use the Mac speaker as the room talker and the Pi/speaker as the device:

```sh
IRIS_TEST_WAKE_TEXT="Iris, please say one long sentence so I can interrupt you." \
IRIS_TEST_INTERRUPT_TEXT="Actually stop and answer this interruption instead." \
IRIS_TEST_INTERRUPT_DELAY_SECONDS=6 \
IRIS_TEST_LOG_WINDOW_SECONDS=18 \
bash scripts/voice-loop-test.sh
```

Then test the follow-up window:

```sh
say "Iris, answer in one short sentence."
sleep 20
say "Can you still hear this without me saying your name?"
sleep 5
say "Iris, can you hear this after the timeout?"
```

Expected result: the second sentence is transcribed but should not create an
assistant response after the wake timeout; the third sentence should wake and
respond.

## Acceptance

- Wake to first transcript, first assistant text, and first audio are all visible
  in logs.
- Assistant echo while Iris is speaking does not trigger an interruption stop
  unless the user says the wake phrase again.
- Saying the wake phrase while Iris is speaking stops active playback immediately.
- Short background speech while Iris is talking does not stop playback.
- Normal playback does not blip in the middle from premature `assistant.audio`
  stop events.
- Diarized speaker labels and word counts are present in live device logs and
  transcript storage.
- After the timeout, follow-up speech without `Iris` does not reach TTS.
