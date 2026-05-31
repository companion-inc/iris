# Iris Architecture

Iris is a local-first desktop voice assistant. The owner runs the desktop app,
API, voice runtime, and optional speaker services on their own Mac.

## Runtime Shape

```txt
Mac microphone or optional room device
  -> apps/iris-mac Swift-owned WebKit microphone capture
  -> URLSessionWebSocketTask
  -> apps/iris-voice backend sidecar
  -> streaming STT
  -> transcript storage through Swift local API
  -> wake-gated assistant turn
  -> tools and Swift Codex bridge
  -> assistant audio back through native Swift playback

apps/iris-mac
  -> local settings
  -> microphone and speaker selection
  -> transcripts
  -> chat history
  -> voice profile creation
  -> optional device controls

Swift local API
  -> local owner profile
  -> sessions and transcripts
  -> desktop events
  -> device metadata
  -> local search
```

The native Swift macOS app is the product surface. Electron is no longer part of
the active workspace or default desktop runtime.

## Auth

The open-source default is local auth:

```txt
IRIS_AUTH_MODE=local
```

There is no hosted auth setup for local usage. A local owner profile is enough:
name, email, optional avatar, and local settings.

## Secrets

The local app should not require hosted secret managers. API keys are
user-owned settings entered in the native Swift desktop app and stored as local
app settings. Swift injects configured keys into the local voice sidecar
environment when it starts the backend.

Expected locally owned keys:

- STT key, currently Deepgram.
- LLM key, currently Gemini-compatible in the voice runtime.
- TTS key, currently XAI/OpenAI-compatible depending on configured provider.

## Data

The active local database is SQLite. The API creates
`apps/iris-api/.iris/iris.sqlite`.

Local storage:

- SQLite tables for the local owner, sessions, transcripts, memories,
  local Iris runs, and settings.
- SQLite FTS5 tables for transcript and memory search.
- Local app settings storage for provider API keys.

## Search

Transcript search is local. It no longer requires hosted vector search or remote
embeddings. The current implementation searches `transcript_segments` directly.
The durable target is SQLite FTS5 with BM25 ranking and optional trigram search.

## Voice Policy

Continuous transcription and assistant turns are separate.

```txt
transcription path:
  audio -> STT -> transcript_segments -> desktop display/search

assistant path:
  audio -> STT -> wake phrase -> agent/tools/TTS -> speaker
```

A transcript segment is not automatically a conversation message. The assistant
path should only run after an intentional wake phrase or direct user action.

## Optional Services

`apps/iris-speaker-id` is local and optional. It should be started when the user
wants to create voice profiles.

`apps/iris-sound-recognition` is local and optional. It should stay off unless
the user explicitly enables sound alerts, because false room-alert labels are a
bad default experience.

## Removed Hosted Surfaces

These are no longer part of the default open-source runtime:

- Hosted organization auth.
- Hosted secret managers.
- Hosted realtime event brokers.
- Hosted vector search.
- Cloud device management, object storage, managed inference, container deploys,
  and infrastructure-as-code deploy stacks.
- Remote update publishing.
- Hosted deploy URLs.
- Mobile pairing as the default setup path.

Keep these paths out of README commands, root scripts, and active API/desktop
dependencies.
