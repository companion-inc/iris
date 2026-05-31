# Iris Speaker ID

Self-hosted speaker enrollment and identification service for Iris.

It uses SpeechBrain ECAPA-TDNN embeddings:

- `POST /v1/enroll` turns one or more 16 kHz-compatible audio samples into one
  normalized speaker embedding.
- `POST /v1/identify` compares a speech sample against registered org member
  embeddings and returns a member only when the cosine score clears the
  configured threshold.

Run locally:

```sh
uv run iris-speaker-id --host 0.0.0.0 --port 8082
```

Point `iris-api` at it with:

```sh
IRIS_SPEAKER_ID_URL=http://127.0.0.1:8082
```
