# Voice tool choice prompting

## Problem

Iris routed an explicit named-app launch through the desktop agent, which made the Codex workspace/thread path visible for a task that should be a direct local command.

## Research

- OpenAI's realtime prompting guide recommends clear labeled prompt sections, short bullets, examples for patterns, and explicit use/avoid rules for each tool.
- Pipecat stores tool definitions in `LLMContext` through `ToolsSchema`, so tool descriptions are part of the model's function selection context.
- Gemini function calling uses function declarations with descriptions and parameter descriptions, so the schema text must carry the same tool boundary as the system prompt.

## Decision

Fix this at the prompt and schema layer only. Do not add runtime rewrites inside the agent tool. Teach the model the general rule: direct, safe, one-step local commands and explicit named-app launches use `shell_exec`; screen inspection, active task steering, multi-step work, code work, debugging, investigations, and current web/docs research use `agent`.
