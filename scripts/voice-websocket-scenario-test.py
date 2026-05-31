#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import ssl
import tempfile
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = ROOT / "scripts" / "voice-websocket-smoke-test.py"
SMOKE_SPEC = importlib.util.spec_from_file_location("voice_websocket_smoke_test", SMOKE_PATH)
if SMOKE_SPEC is None or SMOKE_SPEC.loader is None:
    raise RuntimeError(f"Unable to load {SMOKE_PATH}")
SMOKE = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(SMOKE)

create_voice_session = SMOKE.create_voice_session
stream_pcm = SMOKE.stream_pcm
synthesize_pcm = SMOKE.synthesize_pcm


DEFAULT_SCENARIOS = [
    {
        "name": "wake_time",
        "text": "Iris, what time is it?",
        "expect": ["wake.accepted", "assistant.text", "assistant.audio.started"],
    },
    {
        "name": "followup_without_wake",
        "text": "What about now?",
        "expect": ["assistant.text", "assistant.audio.started"],
    },
    {
        "name": "tool_status",
        "text": "Iris, ask the Codex agent for its current status.",
        "expect": ["tool.started:agent", "assistant.text", "assistant.audio.started"],
    },
    {
        "name": "background_mention",
        "text": "I was telling Sam that Iris can open DoorDash.",
        "expect": ["assistant.no_speech"],
        "forbid": [
            "assistant.audio.started",
            "tool.started:agent",
            "tool.started:shell_exec",
            "tool.started:command",
            "tool.started:status",
        ],
    },
]


def event_signature(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "tool.started":
        return f"{event_type}:{event.get('name')}"
    return event_type


async def wait_for_events(
    events: list[dict[str, Any]],
    *,
    start_index: int,
    expected: set[str],
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        seen = {event_signature(event) for event in events[start_index:]}
        seen.update(str(event.get("type") or "") for event in events[start_index:])
        if expected <= seen:
            return
        await asyncio.sleep(0.1)
    seen = [event_signature(event) for event in events[start_index:]]
    raise AssertionError(f"timed out waiting for {sorted(expected)}; saw {seen}")


async def run_scenario_suite(args: argparse.Namespace) -> int:
    voice_url = args.voice_url
    session_id = "manual"
    if not voice_url:
        token = args.device_token or "local"
        voice_url, session_id = create_voice_session(
            args.api_url,
            token,
            insecure_tls=args.insecure_tls,
        )

    scenarios = json.loads(args.scenarios_json) if args.scenarios_json else DEFAULT_SCENARIOS
    if not isinstance(scenarios, list):
        raise ValueError("scenarios JSON must be a list")

    pcm_by_name: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".tmp") if (ROOT / ".tmp").exists() else None) as tmp:
        tmp_path = Path(tmp)
        for scenario in scenarios:
            name = str(scenario["name"])
            scenario_dir = tmp_path / name
            scenario_dir.mkdir()
            pcm_by_name[name] = synthesize_pcm(str(scenario["text"]), scenario_dir)

    events: list[dict[str, Any]] = []
    audio_frames = 0
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
                print(json.dumps(event, sort_keys=True))

        receiver = asyncio.create_task(receive())
        await wait_for_events(events, start_index=0, expected={"ready"}, timeout_seconds=10)

        results: list[dict[str, Any]] = []
        for scenario in scenarios:
            name = str(scenario["name"])
            start_index = len(events)
            start_audio = audio_frames
            await stream_pcm(websocket, pcm_by_name[name])
            await wait_for_events(
                events,
                start_index=start_index,
                expected={str(item) for item in scenario.get("expect", [])},
                timeout_seconds=float(scenario.get("timeoutSeconds", args.timeout_seconds)),
            )
            seen_after_expect = {str(event.get("type") or "") for event in events[start_index:]}
            if "assistant.audio.started" in seen_after_expect:
                await wait_for_events(
                    events,
                    start_index=start_index,
                    expected={"assistant.audio.stopped"},
                    timeout_seconds=float(scenario.get("timeoutSeconds", args.timeout_seconds)),
                )
            await asyncio.sleep(float(scenario.get("settleSeconds", args.settle_seconds)))
            seen_events = events[start_index:]
            seen_signatures = {event_signature(event) for event in seen_events}
            seen_signatures.update(str(event.get("type") or "") for event in seen_events)
            forbidden = {str(item) for item in scenario.get("forbid", [])}
            forbidden_seen = sorted(forbidden & seen_signatures)
            if forbidden_seen:
                raise AssertionError(f"{name} saw forbidden events {forbidden_seen}")
            result = {
                "name": name,
                "events": [event_signature(event) for event in seen_events],
                "audioFrames": audio_frames - start_audio,
                "assistantText": " ".join(
                    str(event.get("text") or "")
                    for event in seen_events
                    if event.get("type") == "assistant.text"
                ),
            }
            results.append(result)
            print(json.dumps({"scenario": result}, sort_keys=True))

        await websocket.close()
        await asyncio.sleep(0.2)
        receiver.cancel()

    print(json.dumps({"sessionId": session_id, "audioFrames": audio_frames, "results": results}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run multi-turn Iris voice websocket scenarios.")
    parser.add_argument("--api-url", default=os.getenv("IRIS_API_URL", "http://127.0.0.1:4747"))
    parser.add_argument("--voice-url", default=os.getenv("IRIS_VOICE_URL"))
    parser.add_argument("--device-token", default=os.getenv("IRIS_DEVICE_TOKEN", "local"))
    parser.add_argument("--scenarios-json", default=os.getenv("IRIS_SCENARIOS_JSON"))
    parser.add_argument("--timeout-seconds", type=float, default=18.0)
    parser.add_argument("--settle-seconds", type=float, default=1.5)
    parser.add_argument("--insecure-tls", action=argparse.BooleanOptionalAction, default=True)
    (ROOT / ".tmp").mkdir(exist_ok=True)
    return asyncio.run(run_scenario_suite(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
