# Playback Wake Before Echo

## Finding

Iris currently uses Pipecat `LocalAudioTransport` through PyAudio for local mic input and speaker output. That path does not provide macOS VoiceProcessingIO or WebRTC acoustic echo cancellation by itself, so assistant playback can leak back into transcription.

The existing transcript-level playback echo guard is only a workaround for leaked assistant audio. It must not suppress the explicit playback interruption wake phrase.

## Decision

During assistant playback, the wake gate checks `has_playback_interrupt_wake_phrase()` before consulting `PlaybackEchoGuard`. In playback mode, any transcript containing the word `iris` interrupts playback.

This preserves the intended interruption rule:

- `Iris`, `hey Iris`, `please Iris`, or `Iris stop` can interrupt playback.
- The wake transcript is consumed by the playback gate as an interruption signal; any extra words in that same transcript are not treated as a user command.
- The always-on STT transcript relay cuts playback as soon as a playback-time transcript contains `iris`; interruption does not depend on VAD.
- Playback-time transcripts are still emitted to the UI.
- Non-wake assistant echo is still blocked from starting a user turn or reaching chat.
- Non-wake final transcripts during playback still reset aggregation instead of reaching chat.
- Wake-only detection must not queue a spoken TTS acknowledgement. The native `wake.accepted` sound is the acknowledgement; spoken `Yes?` creates playback that can mask or block the user's immediate follow-up.

## Verification

Added a regression test that forces the echo guard to return true and confirms `Iris stop` still triggers `on_user_turn_started` with no aggregation reset.

Ran:

```sh
python3 -m py_compile apps/iris-voice/src/iris_voice/turns/playback_wake_gate.py scripts/voice-completion-contract-test.py
cd apps/iris-voice && uv run ../../scripts/voice-completion-contract-test.py
```

The contract run logged:

```text
iris.voice.playback_wake_gate interrupt=true final=False text='Iris stop'
```

## Remaining Risk

If playback is loud enough that the STT provider never emits a transcript containing the wake phrase, this gate will not see `Iris` and cannot interrupt. That remaining issue requires real acoustic echo cancellation or a local wake detector that can operate during playback; adding more transcript filters cannot fix audio that never reaches transcription.
