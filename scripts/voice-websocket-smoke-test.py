#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse, urlunparse
import wave
from pathlib import Path

import websockets


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed with {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def create_voice_session(api_url: str, device_token: str, *, insecure_tls: bool) -> tuple[str, str]:
    body = json.dumps({"sampleRate": 16000, "channels": 1}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/voice/sessions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {device_token}",
            "Content-Type": "application/json",
            "User-Agent": "IrisVoiceSmoke/1",
        },
    )
    context = ssl._create_unverified_context() if insecure_tls else None
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["voiceUrl"]), str(payload["sessionId"])


def usable_voice_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if parsed.scheme in {"ws", "wss"}:
        return raw_url
    if parsed.scheme in {"http", "https"} and parsed.path not in {"", "/"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse(parsed._replace(scheme=scheme))
    return ""


def synthesize_pcm(text: str, directory: Path) -> bytes:
    if not shutil.which("say") or not shutil.which("afconvert"):
        raise RuntimeError("macOS say and afconvert are required for this smoke test")
    aiff_path = directory / "input.aiff"
    wav_path = directory / "input.wav"
    run(["say", "-r", "145", "-o", str(aiff_path), text])
    run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff_path), str(wav_path)])
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise RuntimeError("generated audio is not 16kHz mono s16le")
        leading_silence = b"\x00\x00" * 16000
        return leading_silence + wav.readframes(wav.getnframes())


async def stream_pcm(websocket, pcm: bytes) -> None:
    for index in range(0, len(pcm), 3200):
        await websocket.send(pcm[index : index + 3200])
        await asyncio.sleep(0.1)


async def run_smoke(args: argparse.Namespace) -> int:
    voice_url = usable_voice_url(args.voice_url)
    session_id = "manual"
    if not voice_url:
        token = args.device_token or "local"
        voice_url, session_id = create_voice_session(
            args.api_url,
            token,
            insecure_tls=args.insecure_tls,
        )

    events: list[dict] = []
    audio_frames = 0
    assistant_text: list[str] = []

    with tempfile.TemporaryDirectory(dir=str(ROOT / ".tmp") if (ROOT / ".tmp").exists() else None) as tmp:
        pcm = synthesize_pcm(args.text, Path(tmp))

    ssl_context = ssl._create_unverified_context() if args.insecure_tls and voice_url.startswith("wss://") else None
    async with websockets.connect(voice_url, ssl=ssl_context, max_size=None) as websocket:
        async def receive() -> None:
            nonlocal audio_frames
            async for message in websocket:
                if isinstance(message, bytes):
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    print(f"text {message}")
                    continue
                if event.get("type") == "audio":
                    audio_frames += 1
                    continue
                events.append(event)
                if event.get("type") == "assistant.text":
                    assistant_text.append(str(event.get("text") or ""))
                print(json.dumps(event, sort_keys=True))

        receiver = asyncio.create_task(receive())
        await asyncio.sleep(0.5)
        await stream_pcm(websocket, pcm)
        await asyncio.sleep(args.wait_seconds)
        await websocket.close()
        await asyncio.sleep(0.2)
        receiver.cancel()

    event_types = [str(event.get("type")) for event in events]
    print(
        json.dumps(
            {
                "sessionId": session_id,
                "eventTypes": event_types,
                "assistantText": "".join(assistant_text),
                "audioFrames": audio_frames,
            },
            sort_keys=True,
        )
    )
    required = {"ready", "wake.accepted", "assistant.text"}
    missing = sorted(required - set(event_types))
    if missing:
        print(f"missing events: {', '.join(missing)}", file=sys.stderr)
        return 1
    if audio_frames <= 0:
        print("missing assistant audio frames", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the local Iris voice websocket.")
    parser.add_argument("--api-url", default=os.getenv("IRIS_API_URL", "http://127.0.0.1:4747"))
    parser.add_argument("--voice-url", default=os.getenv("IRIS_VOICE_URL"))
    parser.add_argument("--device-token", default=os.getenv("IRIS_DEVICE_TOKEN", "local"))
    parser.add_argument("--text", default=os.getenv("IRIS_TEST_WAKE_TEXT", "Iris, what time is it?"))
    parser.add_argument("--wait-seconds", type=float, default=float(os.getenv("IRIS_TEST_WAIT_SECONDS", "15")))
    parser.add_argument("--insecure-tls", action=argparse.BooleanOptionalAction, default=True)
    (ROOT / ".tmp").mkdir(exist_ok=True)
    args = parser.parse_args()
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    raise SystemExit(main())
