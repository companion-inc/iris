# Iris

Iris is a local-first desktop voice assistant. The current product target is an
open-source app that runs on one Mac, stores its own data locally, and lets the
owner bring their own model, speech, and TTS API keys.

Hosted deploys, organization auth, app-store companion pairing, cloud device
management, hosted vector search, and remote update infrastructure have been
removed from the app path.

## What Runs

- `apps/iris-mac` - native Swift macOS app for settings, transcripts, voice,
  device state, Codex bridge control, and local process supervision.
- `apps/iris-api` - local Hono API for app state, sessions, transcripts, device
  metadata, and desktop/voice coordination.
- `apps/iris-voice` - local Pipecat voice runtime for microphone audio,
  transcription, wake-gated assistant turns, tools, and assistant audio.
- `apps/iris-speaker-id` - optional local voice profile service.
- `apps/iris-sound-recognition` - optional local sound classifier. It should stay
  disabled unless the user explicitly configures sound alerts.

## Local Setup

```sh
pnpm install
cp .env.example .env
pnpm desktop:start
```

`pnpm desktop:start` opens the native Swift app bundle. The Swift app owns the
local API, Codex bridge, microphone/WebSocket/playback client, and directly
starts the Python voice backend sidecar when needed. For a desktop-only test,
the Mac microphone and speakers are enough.

## Configuration

Iris should work in local auth mode by default:

```sh
IRIS_AUTH_MODE=local
IRIS_API_URL=http://127.0.0.1:4747
IRIS_VOICE_URL=http://127.0.0.1:4748
IRIS_SPEAKER_ID_URL=http://127.0.0.1:4749
```

External API keys are user-owned local settings. The app lets the user enter
keys in the native desktop settings UI, stores provider keys in local app settings,
and injects them into the local voice backend sidecar. The local API should
avoid requiring hosted secret managers. On first launch, the native app can
import the old desktop settings file from
`~/Library/Application Support/Iris/settings.json` and moves existing provider
keys into native app settings.

The local API uses SQLite at `apps/iris-api/.iris/iris.sqlite`.

## Checks

```sh
pnpm typecheck
pnpm mac:build
pnpm mac:verify-bundle
pnpm voice:check
pnpm speaker-id:check
pnpm sound-recognition:check
```

## Project Docs

- [Local-first architecture](docs/local-first.md)
- [Local search plan](docs/local-search.md)
- [Migration plan](docs/migration-plan.md)
- [Voice runtime architecture](docs/architecture.md)
- [Voice runtime readiness](docs/voice-runtime-readiness.md)
