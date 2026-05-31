# Swift Migration

Iris is moving to a native macOS shell first, then collapsing runtime services into Swift where that actually reduces complexity.

## Current Native App

- Package: `apps/iris-mac`
- Build: `pnpm mac:build`
- Run: `pnpm mac:run`
- Open app bundle: `pnpm desktop:start` or `pnpm mac:open`
- Verify app bundle: `pnpm mac:verify-bundle`
- UI: SwiftUI with native `NavigationSplitView`
- Local API: Swift Network.framework listener on `127.0.0.1:4747` for health, voice config, voice session creation/end, transcript history/search, voice session history, voice memory list/save/update/delete, device list/inventory, Codex thread/run/completion/approval inventory, device volume/light state, desktop task proxying, and completion poll/delivery
- Codex bridge: Swift Network.framework listener on `127.0.0.1:4750` plus a Swift `Process`/`Pipe` JSON-RPC client for `codex app-server`
- Native voice I/O: Swift-owned hidden `WKWebView` microphone capture, Swift `URLSessionWebSocketTask` PCM transport, native PCM playback, and generated native earcons. Direct CoreAudio/`AVAudioEngine` capture is not the active path because the current Mac capture layer stalled while the WebKit/getUserMedia path produced frames.
- Voice backend sidecar: launched directly by Swift as `uv run iris-voice`, not through Electron, Node, or `pnpm`
- Native provider settings: provider choices and Deepgram, Gemini, xAI, and OpenAI API keys are stored in local Swift app settings; keys are injected into the voice sidecar environment by Swift
- Local history reads: Swift SQLite reads from `apps/iris-api/.iris/iris.sqlite` for Home, Devices, Transcripts, and session lists
- System hooks: App Intents, SwiftUI `MenuBarExtra`, `SMAppService` launch-at-login control, and AVFoundation microphone authorization/status
- Native settings: Swift `UserDefaults` model with explicit apply/reset, URL validation, and Codex bridge recreation when the workspace changes
- Native bundle: checked-in `Resources/Info.plist`, generated `repo-root.txt`, and ad-hoc signing for local macOS service compatibility
- Local workspace: user-selected workspace directory, defaulting to `~/Iris/Workspace`

The native shell owns the desktop and Codex boundaries directly:

- API: `http://127.0.0.1:4747`
- Voice: `http://127.0.0.1:4748`
- Codex bridge: `http://127.0.0.1:4750`

The voice sidecar is started directly from Swift with `IRIS_AGENT_BRIDGE_URL=http://127.0.0.1:4750/agent`, so voice-to-Codex handoff goes to the Swift bridge first. In this contract, `agent` is the developer/protocol noun for the local desktop worker that accepts tasks; user-facing UI should say Iris, this Mac, or desktop runtime. The native app now owns the local microphone/WebSocket/playback client path; the Python voice process remains the Pipecat/STT/LLM/TTS backend sidecar.

The Swift app no longer launches Hono by default. The native local API owns the local endpoints the Python voice sidecar and native UI need for development: health, session creation, config, transcript writes/history/search, voice session history, session end, voice memory list/save/update/delete, device list/inventory, Codex thread/run/completion/approval inventory, device volume/light state, desktop task proxying, and completion poll/delivery.

The Swift app owns local provider configuration. STT/LLM/TTS provider choices and API keys are stored as native local app settings and passed into the voice sidecar as `DEEPGRAM_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, and `OPENAI_API_KEY`. On first launch, the Swift app can import the legacy desktop settings file at `~/Library/Application Support/Iris/settings.json`: workspace/API/voice/provider values and existing provider keys move into Swift settings.

## Migration Order

1. Replace the old desktop UI and process supervision with `apps/iris-mac`.
2. Fill in remaining cloud-only organization administration paths if they are still needed locally.
3. Move voice runtime stages out of the Python sidecar piece-by-piece now that the native app owns audio capture, WebSocket transport, and playback.
4. Keep speaker-id service as a sidecar until native enrollment/inference parity is stable.

## Non-Negotiables

- Codex requests must run in the configured Iris workspace, not the Iris repo.
- Voice-to-Codex handoff must send an interpreted desktop task, not a raw transcript replay.
- `desktop:start` and `desktop:open` must launch the Swift app.
- Use Iris/desktop runtime in product copy and `agent` in developer-facing protocol, database, route names, and voice events. Keep legacy `hub` event aliases only as compatibility shims.
