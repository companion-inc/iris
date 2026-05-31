# Iris Voice

Local Pipecat runtime for Iris.

The desktop app or optional device client streams PCM audio to this service over
a websocket. The runtime stores transcripts through the local API, wake-gates
assistant turns, calls tools, and streams assistant audio back to the selected
speaker path.

## Run

```sh
uv run iris-voice --host 0.0.0.0 --port 4748
```

From the repo root:

```sh
pnpm voice:dev
```

## Environment

```sh
IRIS_API_URL=http://127.0.0.1:4747
IRIS_TOKEN_SECRET=dev-token
DEEPGRAM_API_KEY=
GEMINI_API_KEY=
XAI_API_KEY=
OPENAI_API_KEY=
```

The keys are user-owned local settings. The open-source path should not require
a hosted secret manager.

## Behavior

- Transcribes while audio is connected.
- Sends final and interim transcript events to the local API.
- Uses the Iris wake phrase before assistant turns.
- Returns assistant PCM audio over the same websocket.
- Can call the Iris Mac app's local Codex agent bridge when configured.
- Can use speaker identification when `apps/iris-speaker-id` is running.
- Keeps sound recognition optional and disabled unless explicitly enabled.

## Wake Timing

```sh
IRIS_WAKE_ACTIVE_WINDOW_SECONDS=12
```

## Speech-To-Text

Iris defaults to Deepgram Nova-3 multilingual transcription:

```sh
IRIS_STT_MODEL=nova-3
IRIS_STT_LANGUAGE=multi
IRIS_STT_KEYTERMS=Iris
```

## Local Codex Agent Bridge

```sh
IRIS_AGENT_BRIDGE_URL=http://127.0.0.1:4750/agent
IRIS_AGENT_BRIDGE_TOKEN=dev-token
IRIS_AGENT_ID=local_desktop_agent
```
