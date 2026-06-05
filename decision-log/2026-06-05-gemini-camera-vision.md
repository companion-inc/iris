# Gemini Camera Vision

## Finding

The Mac exposes camera devices through AVFoundation. This machine has a FaceTime HD Camera and an iPhone camera, and `/opt/homebrew/bin/ffmpeg` can capture a single JPEG frame from AVFoundation device `0:none`.

Pipecat already converts image-bearing context messages into Gemini inline image data, so camera vision can share the same Gemini path as screen vision.

## Decision

Add a direct `camera_vision` voice tool. It captures one JPEG frame from the default Mac camera with `ffmpeg`, downsizes it with `sips`, appends it to the current Pipecat LLM context as `image/jpeg`, and reruns Gemini with the user's camera-view question.

Use `screen_vision` for screen pixels, `camera_vision` for physical room/camera pixels, and keep the desktop Codex agent for multi-step computer work.

## Verification

The local `ffmpeg` capture produced a valid JPEG. Contract tests assert the prompt and schema expose `camera_vision` and verify the camera helper captures a non-empty JPEG on this Mac.
