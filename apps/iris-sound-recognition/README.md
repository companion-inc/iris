# Iris Sound Recognition

Internal service for classifying short PCM windows against explicitly requested
sound labels.

The voice runtime owns the always-on device websocket. It sends rolling audio
windows here only when the user has configured sound recognition rules. The
service returns matching labels with confidence; the voice runtime owns logging
and any configured follow-up action.

Iris ships with built-in room-alert rules for doorbells, knocks, smoke/fire
alarms, glass breaking, dog barking, baby crying, beeps, sirens, and sneezes. Most
matches are stored as transcript history events; `sneeze` still prompts the
assistant to say "bless you."

```sh
uv run iris-sound-recognition --host 0.0.0.0 --port 8081
```

Useful env:

- `IRIS_SOUND_RECOGNITION_BACKEND=transformers|heuristic`
- `IRIS_SOUND_RECOGNITION_MODEL=mispeech/ced-small`
- `IRIS_SOUND_RECOGNITION_MODEL_REVISION=06bb40c5ec089e96867ebc5246be02441f4a71e4`
- `IRIS_SOUND_RECOGNITION_THRESHOLD=0.45`
