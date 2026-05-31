# Release Download Readiness

Date: 2026-05-31

## Decision

The macOS release package is self-contained at the app/runtime source level:

- `Iris.app/Contents/Resources/repo-root.txt` points to bundled `IrisRuntime`.
- `IrisRuntime` contains the voice and speaker-id source trees, lockfiles, and a bundled `uv` binary.
- On launch, the Swift app copies `IrisRuntime` to `~/Library/Application Support/Iris/Runtime` and runs the sidecars from there.
- The Swift local API creates the local SQLite schema and seeds a local organization, user, and desktop device on a fresh install.

The release package does not copy the repo's existing `.venv` directories. Those virtualenvs contain absolute interpreter paths in console scripts, so copying them into a public app would make the release machine-specific.

## Verification

Clean-download test performed by moving the existing installed runtime aside, unzipping `dist/Iris-macOS-arm64.zip` into a temporary directory, and launching the extracted `Iris.app`.

Results:

- `repo-root.txt` resolves to bundled `IrisRuntime`, not the repo checkout.
- The runtime is copied to `~/Library/Application Support/Iris/Runtime`.
- The local SQLite database is created under `apps/iris-api/.iris/iris.sqlite`.
- The seeded device exists as `agent_local_smoke`.
- `GET http://127.0.0.1:4747/health` returns `iris-api-swift`.
- `GET http://127.0.0.1:4749/health` returns `iris-speaker-id`.
- `GET http://127.0.0.1:4748/health` returns `iris-voice`.
- `POST http://127.0.0.1:4747/v1/voice/sessions` returns a websocket URL for the voice runtime.

Observed first-run readiness on this Mac: sidecars became healthy at 35 seconds after launch while `uv` created environments from lockfiles.
