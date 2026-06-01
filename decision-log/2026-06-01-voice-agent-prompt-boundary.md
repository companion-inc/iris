# Voice Agent Prompt Boundary

## Observation
- The voice model is Gemini in `apps/iris-voice`; it receives the system prompt and tool schema that tell it to rewrite spoken requests before calling `agent`.
- The local desktop worker is Codex behind `apps/iris-mac/Sources/IrisMac/SwiftCodexBridgeServer.swift`.
- The bridge was prepending Gemini-facing meta-instructions such as `Interpreted desktop task for Codex` and `Do not merely repeat the user's raw words` to the prompt sent to Codex.

## Decision
- Keep rewrite instructions in the Gemini layer: `prompt.py` and `tool_schemas.py`.
- Send Codex only the interpreted desktop task and optional voice context.
- Add regression tests so the Codex prompt builder does not leak Gemini tool instructions.

## Verification Plan
- Run the Python voice contract test to verify Gemini prompt/tool-schema rewrite guidance remains present.
- Run the Swift Mac test suite to verify the bridge prompt sent to Codex excludes the leaked meta-instructions.
