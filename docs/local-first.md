# Local-First Iris

This repo is a self-contained local-first app. The default developer path runs
on the owner's Mac.

## Product Boundary

The product is the desktop app plus local voice runtime.

Required:

- Native Swift macOS app.
- In-process Swift local API.
- Local voice runtime.
- Local database.
- Locally entered provider API keys.
- Microphone and speaker selection.
- Voice profile setup.
- Transcripts and chat as separate surfaces.

Optional local services:

- Speaker identification service.
- Sound recognition service.

Not default:

- Hosted environments.
- Hosted deploys.
- Organization auth.
- Cloud device management.
- Remote update services.
- Managed vector databases.
- Hosted event brokers.
- Mobile app.
- Public site.

## Local Runtime

Recommended local ports:

```txt
4747  iris-api
4748  iris-voice
4749  iris-speaker-id
4751  iris-sound-recognition, optional
```

Run the core loop:

```sh
pnpm desktop:start
```

`pnpm desktop:start` opens the native Swift app. The Swift app starts its local
API and Codex bridge in-process, launches the Python voice backend sidecar
directly, and owns microphone capture plus playback. The desktop app should be
able to test with the Mac microphone and selected Mac speaker. AirPods or
external audio devices should be explicitly selectable.

## Local Data

The target is one local SQLite database under app data. It should contain:

- local owner profile
- settings
- API key metadata
- voice sessions
- transcript segments
- assistant conversation messages
- speaker profiles
- local Iris runs and completions
- memories

Secrets should not be stored in SQLite. The native macOS app stores provider
API keys as local app settings. When the native app first launches, it can import
the legacy desktop settings file at
`~/Library/Application Support/Iris/settings.json` and writes existing provider
keys to native app settings.

## Current Truth

The active desktop/API runtime is local-first and Swift-native at the app
boundary: local auth, local ports, local SQLite storage, in-process Swift API,
and no hosted event broker. The old app-store companion, public website, and
cloud infrastructure folders have been removed.
