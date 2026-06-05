# Block playback transcripts from the LLM

## Problem

Speech during Iris playback can be picked up by STT and diarized as a normal speaker. Text matching against the assistant response is not sufficient because a user can repeat the same phrase and late TTS tails can arrive after the assistant turn boundary.

## Decision

While Iris playback is active, `TranscriptRelay` still emits and ingests transcripts for the UI/history. It does not forward non-wake playback transcripts to the LLM path, so Iris does not react to them as user commands. Wake-addressed playback transcripts still pass through so the playback wake gate can interrupt.

## Expected behavior

- Speech heard during Iris playback still appears in the UI/history.
- Non-wake speech heard during Iris playback does not trigger an LLM response.
- Wake-addressed speech during playback can still interrupt because frame forwarding is preserved for wake transcripts.
