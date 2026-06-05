# Gemini Screen Vision

## Finding

Iris previously routed screen/app inspection through the desktop Codex agent. That path can summarize visible state, but the voice LLM receives a text result rather than the screenshot pixels.

Pipecat 1.2.1 supports image-bearing LLM context messages through `LLMContext.add_image_frame_message`, and the installed Gemini adapter converts `data:image/...` messages into Gemini inline image data.

## Decision

Add a direct `screen_vision` voice tool for visual questions. The tool captures the main Mac display with `screencapture`, downsizes the JPEG with `sips`, appends it to the current Pipecat LLM context as `image/jpeg`, and reruns Gemini with the user's visual question.

Keep the desktop Codex agent for multi-step computer work, debugging, code edits, and long-running investigation.

## Verification

Contract tests assert the prompt and schema expose `screen_vision`. The local contract harness also verifies that the screenshot helper captures a non-empty JPEG on this Mac.
