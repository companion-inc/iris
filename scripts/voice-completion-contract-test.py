#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "iris-voice" / "src"))

from iris_voice.agent_completion_events import (  # noqa: E402
    AgentCompletionSubscriber,
    desktop_completion_message,
    desktop_completion_payload,
    should_run_llm_for_completion,
)
import iris_voice.agent_completion_events as agent_completion_events  # noqa: E402
import iris_voice.completion_delivery_scheduler as completion_delivery_scheduler  # noqa: E402
from iris_voice.agent_bridge import infer_agent_delivery  # noqa: E402
from iris_voice.local_audio import (  # noqa: E402
    DirectLocalAudioOutput,
    LocalAudioRuntimeManager,
    LocalPlaybackStateTracker,
)
from iris_voice.runtime_events import RuntimeEvents  # noqa: E402
from iris_voice.session import VoiceSessionContext  # noqa: E402
from iris_voice.prompt import system_instruction  # noqa: E402
from iris_voice.sound_recognition import (  # noqa: E402
    DEFAULT_LOG_SOUND_THRESHOLD,
    default_sound_recognition_watches,
)
from iris_voice.tools import agent_result_run_id, agent_result_status  # noqa: E402
from iris_voice.tools import _capture_default_camera_jpeg  # noqa: E402
from iris_voice.tools import _capture_main_screen_jpeg  # noqa: E402
from iris_voice.tools import register_basic_voice_tools  # noqa: E402
from iris_voice.tool_schemas import basic_voice_tools  # noqa: E402
from iris_voice.transcripts import TranscriptRelay  # noqa: E402
from iris_voice.turns.wake import (  # noqa: E402
    DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS,
    IrisWakePhraseUserTurnStartStrategy,
    has_leading_wake_phrase,
    has_transcription_wake_phrase,
    is_wake_only_transcription,
    wake_command_text,
)
from iris_voice.turns.playback_echo import PlaybackEchoGuard  # noqa: E402
from iris_voice.turns.playback_wake_gate import PlaybackWakeGateUserTurnStartStrategy  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.llm_service import FunctionCallParams  # noqa: E402
from pipecat.turns.types import ProcessFrameResult  # noqa: E402
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy  # noqa: E402


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class FakeTask:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def queue_frame(self, frame: Any) -> None:
        self.frames.append(frame)


class FakeLLM:
    def __init__(self) -> None:
        self.functions: dict[str, tuple[Any, dict[str, Any]]] = {}

    def register_function(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.functions[name] = (handler, kwargs)


class FakeEchoGuard:
    def __init__(self, playback_echo: bool = False) -> None:
        self.playback_echo = playback_echo

    def is_playback_echo(self, _frame: Any) -> bool:
        return self.playback_echo


class CapturingTranscriptRelay(TranscriptRelay):
    def __init__(self, events: RuntimeEvents, **kwargs: Any) -> None:
        super().__init__(events, **kwargs)
        self.pushed_frames: list[Any] = []

    async def push_frame(self, frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        self.pushed_frames.append(frame)


class FakeWord:
    def __init__(self, word: str, speaker: int | str | None) -> None:
        self.word = word
        self.speaker = speaker


class FakeAlternative:
    def __init__(self, words: list[FakeWord]) -> None:
        self.words = words
        self.confidence = 0.9


class FakeChannel:
    def __init__(self, alternatives: list[FakeAlternative]) -> None:
        self.alternatives = alternatives


class FakeResult:
    def __init__(self, words: list[FakeWord]) -> None:
        self.channel = FakeChannel([FakeAlternative(words)])


def transcription(text: str, *, speaker: int | str | None = None, final: bool = True):
    words = [FakeWord(word.strip(".,!?"), speaker) for word in text.split()]
    frame_type = TranscriptionFrame if final else InterimTranscriptionFrame
    return frame_type(text=text, user_id="test_user", timestamp="2026-05-24T12:00:00Z", result=FakeResult(words))


def pcm_tone(*, sample_count: int, amplitude: int) -> bytes:
    samples = bytearray()
    for index in range(sample_count):
        value = amplitude if index % 2 == 0 else -amplitude
        samples.extend(value.to_bytes(2, "little", signed=True))
    return bytes(samples)


def session() -> VoiceSessionContext:
    return VoiceSessionContext(
        session_id="voice_contract_session",
        device_id="device_contract",
        user_id="user_contract",
        organization_id="org_contract",
        source="test",
        sample_rate=16000,
        channels=1,
    )


async def test_pending_tool_result_delivery() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    task = FakeTask()
    subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)
    callback_calls: list[tuple[dict[str, Any], Any]] = []

    async def result_callback(result: dict[str, Any], *, properties: Any = None) -> None:
        callback_calls.append((result, properties))

    events.register_agent_tool_result(
        run_id="agent_run_contract",
        tool_call_id="tool_contract",
        request_id="device_contract",
        result_callback=result_callback,
    )
    payload = desktop_completion_payload(
        completion={
            "id": "agent_completion_contract",
            "result": {
                "voiceHandoff": {
                    "type": "iris.desktop.result",
                    "outcome": "completed",
                    "summary": "The desktop task finished.",
                    "suggestedSpoken": "Done.",
                    "needsUserAction": False,
                }
            },
        },
        content="The desktop task finished.",
        status="completed",
        delivery="speak",
        run_id="agent_run_contract",
    )

    delivered = await subscriber._deliver_as_pending_tool_result(  # noqa: SLF001
        run_id="agent_run_contract",
        completion_id="agent_completion_contract",
        payload=payload,
        run_llm=True,
    )
    await asyncio.sleep(0)

    assert delivered is True
    assert task.frames == []
    assert events.get_agent_tool_result("agent_run_contract") is None
    assert len(callback_calls) == 1
    result, properties = callback_calls[0]
    assert result["ok"] is True
    assert result["completion"]["result"]["voiceHandoff"]["suggestedSpoken"] == "Done."
    assert "rawAssistantText" not in result["completion"]["result"]
    assert properties.is_final is True
    assert properties.run_llm is True
    assert any(message.get("type") == "agent.completion.tool_result" for message in websocket.messages)


async def test_pending_tool_result_completion_is_not_enqueued() -> None:
    delivered_ids: list[str] = []
    original_mark_delivered = agent_completion_events.mark_session_agent_completion_delivered

    async def fake_mark_delivered(_session: VoiceSessionContext, completion_id: str) -> None:
        delivered_ids.append(completion_id)

    agent_completion_events.mark_session_agent_completion_delivered = fake_mark_delivered
    try:
        websocket = FakeWebSocket()
        events = RuntimeEvents(websocket, session())
        task = FakeTask()
        subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)
        callback_calls: list[tuple[dict[str, Any], Any]] = []

        async def result_callback(result: dict[str, Any], *, properties: Any = None) -> None:
            callback_calls.append((result, properties))

        events.register_agent_tool_result(
            run_id="agent_run_single_path",
            tool_call_id="tool_single_path",
            request_id="device_contract",
            result_callback=result_callback,
        )
        await subscriber._handle_completion(  # noqa: SLF001
            {
                "id": "agent_completion_single_path",
                "sessionId": session().session_id,
                "runId": "agent_run_single_path",
                "status": "completed",
                "delivery": "speak",
                "content": "The desktop task finished.",
                "createdAt": "2026-05-24T12:00:00.000Z",
            }
        )
        await asyncio.sleep(0)

        assert len(callback_calls) == 1
        assert callback_calls[0][1].run_llm is True
        assert task.frames == []
        assert delivered_ids == ["agent_completion_single_path"]
        assert events.get_agent_tool_result("agent_run_single_path") is None
        assert any(message.get("type") == "agent.completion.tool_result" for message in websocket.messages)
        assert not any(message.get("type") == "agent.completion.queued" for message in websocket.messages)
        assert not any(message.get("type") == "agent.completion.injected" for message in websocket.messages)
    finally:
        agent_completion_events.mark_session_agent_completion_delivered = original_mark_delivered


async def test_developer_event_fallback_message() -> None:
    payload = desktop_completion_payload(
        completion={
            "id": "agent_completion_fallback",
            "voice": {
                "type": "iris.desktop.result",
                "summary": "Saved the note.",
                "suggestedSpoken": "I saved it.",
                "needsUserAction": False,
            },
        },
        content="Saved the note.",
        status="completed",
        delivery="save",
        run_id="agent_run_fallback",
    )
    message = desktop_completion_message(payload)
    body = json.loads(message["content"])

    assert message["role"] == "developer"
    assert body["type"] == "iris.internal_event"
    assert body["description"] == "Internal Iris runtime event. This is not user speech."
    assert body["payload"]["voice"]["suggestedSpoken"] == "I saved it."
    assert "spoken" not in body["payload"]["voice"]


async def test_completion_policy_helpers() -> None:
    assert should_run_llm_for_completion(delivery="silent", status="completed") is False
    assert should_run_llm_for_completion(delivery="save", status="completed") is False
    assert should_run_llm_for_completion(delivery="save", status="failed") is True
    assert should_run_llm_for_completion(delivery="speak", status="completed") is True
    assert should_run_llm_for_completion(delivery="speak", status="interrupted") is False
    assert should_run_llm_for_completion(delivery="speak", status="cancelled") is False
    assert infer_agent_delivery("save", prompt="Check this and tell me when it's done") == "speak"
    assert (
        infer_agent_delivery(
            "save",
            prompt="Check git status.",
            context="Current user turn: check git status and tell me when it is done",
        )
        == "speak"
    )
    assert infer_agent_delivery("auto", prompt="Run this in the background") == "save"
    assert infer_agent_delivery("speak", prompt="Run this silently") == "silent"
    assert infer_agent_delivery("auto", prompt="Quit the Granola app.") == "speak"
    assert agent_result_run_id({"requestId": "agent_run_1"}) == "agent_run_1"
    assert agent_result_run_id({"run": {"id": "agent_run_2"}}) == "agent_run_2"
    assert agent_result_run_id({"runId": "agent_run_3", "requestId": "turn_1"}) == "agent_run_3"
    assert agent_result_status({"status": "inProgress"}) == "running"


async def test_interrupted_completion_clears_pending_without_llm_callback() -> None:
    delivered_ids: list[str] = []
    original_mark_delivered = agent_completion_events.mark_session_agent_completion_delivered

    async def fake_mark_delivered(_session: VoiceSessionContext, completion_id: str) -> None:
        delivered_ids.append(completion_id)

    agent_completion_events.mark_session_agent_completion_delivered = fake_mark_delivered
    try:
        websocket = FakeWebSocket()
        events = RuntimeEvents(websocket, session())
        task = FakeTask()
        subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)
        callback_calls: list[tuple[dict[str, Any], Any]] = []

        async def result_callback(result: dict[str, Any], *, properties: Any = None) -> None:
            callback_calls.append((result, properties))

        events.register_agent_tool_result(
            run_id="agent_run_superseded",
            tool_call_id="tool_superseded",
            request_id="device_contract",
            result_callback=result_callback,
        )
        await subscriber._handle_completion(  # noqa: SLF001
            {
                "id": "agent_completion_superseded",
                "sessionId": session().session_id,
                "runId": "agent_run_superseded",
                "status": "interrupted",
                "delivery": "speak",
                "content": "Superseded by a newer desktop instruction.",
                "createdAt": "2026-05-24T12:00:00.000Z",
            }
        )
        await asyncio.sleep(0)

        assert callback_calls == []
        assert task.frames == []
        assert delivered_ids == ["agent_completion_superseded"]
        assert events.get_agent_tool_result("agent_run_superseded") is None
        assert any(message.get("type") == "agent.completion.suppressed" for message in websocket.messages)
    finally:
        agent_completion_events.mark_session_agent_completion_delivered = original_mark_delivered
async def test_wake_phrase_only_matches_direct_turns() -> None:
    direct = [
        "Iris, use my computer.",
        "Hey, Iris.",
        "okay iris print the date",
        "Iris stop",
        "Iris cancel",
    ]
    object_references = [
        "Print the current date in the Iris workspace.",
        "Check git status in the Iris repo.",
        "The Iris folder is open.",
        "SPEAKER_1: iris",
        "stop Iris",
        "cancel Iris",
    ]

    for text in direct:
        assert has_leading_wake_phrase(text), text
    for text in object_references:
        assert not has_leading_wake_phrase(text), text


async def test_transcription_wake_phrase_allows_speaker_context_prefix() -> None:
    direct_transcripts = [
        "SPEAKER_0: iris",
        "SPEAKER_0: Iris, use my computer.",
        "Advait: Iris stop",
        "Because obviously the Iris, can you hear me?",
    ]
    object_reference_transcripts = [
        "SPEAKER_0: Print the current date in the Iris workspace.",
        "SPEAKER_1: The Iris folder is open.",
        "Advait: stop Iris",
    ]

    for text in direct_transcripts:
        assert has_transcription_wake_phrase(text), text
    for text in object_reference_transcripts:
        assert not has_transcription_wake_phrase(text), text
    assert wake_command_text("Because obviously the Iris, can you hear me?") == "can you hear me?"
    assert is_wake_only_transcription("SPEAKER_0: iris")
    assert is_wake_only_transcription("SPEAKER_0: Iris. Iris.")
    assert not is_wake_only_transcription("SPEAKER_0: Iris, use my computer.")
    assert not is_wake_only_transcription("SPEAKER_0: Iris stop")
    assert not is_wake_only_transcription("SPEAKER_0: stop Iris")


async def test_default_sound_recognition_ignores_low_confidence_room_noise() -> None:
    log_watches = [
        watch for watch in default_sound_recognition_watches() if watch.behavior == "log"
    ]
    assert log_watches
    for watch in log_watches:
        assert watch.threshold == DEFAULT_LOG_SOUND_THRESHOLD
        assert watch.threshold > 0.52


async def test_default_wake_window_allows_real_followup_pacing() -> None:
    assert DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS >= 10.0


async def test_wake_only_does_not_emit_spoken_ack() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    emitted: list[dict[str, Any]] = []
    events.add_listener(emitted.append)
    wake_strategy = IrisWakePhraseUserTurnStartStrategy(
        phrases=["iris"],
        timeout=12.0,
        echo_guard=FakeEchoGuard(False),
    )
    resets: list[Any] = []
    starts: list[Any] = []
    wake_strategy.add_event_handler("on_reset_aggregation", lambda *_args: resets.append(_args[-1]))
    wake_strategy.add_event_handler("on_user_turn_started", lambda *_args: starts.append(_args[-1]))
    wake_strategy._transition_to_awake = (  # noqa: SLF001
        lambda _phrase: setattr(wake_strategy, "_state", wake_strategy.state.__class__.AWAKE)
    )

    assert await wake_strategy.process_frame(transcription("Iris", final=False)) == ProcessFrameResult.STOP

    assert wake_strategy.state.name == "AWAKE"
    assert len(resets) == 1
    assert starts == []
    assert not any(message.get("type") == "assistant.text" for message in emitted)
    assert not any(message.get("type") == "assistant.text" for message in websocket.messages)


async def test_system_prompt_treats_user_turns_as_imperfect_speech() -> None:
    prompt = system_instruction()

    assert "speech-to-text output" in prompt
    assert "not perfect typed text" in prompt
    assert "Quietly correct obvious speech recognition mistakes" in prompt
    assert "ask a short clarification before acting" in prompt
    assert "too garbled to recover a request" in prompt
    assert "[inhale]" in prompt
    assert "Speech tags are available" in prompt
    assert "Pick the lowest-latency tool that fully satisfies the request" in prompt
    assert "Use shell_exec for direct, safe, one-step local commands" in prompt
    assert "Use shell_exec for explicit named-app launches" in prompt
    assert "Use screen_vision when the user asks about what is visible" in prompt
    assert "Use camera_vision when the user asks you to look through the camera" in prompt
    assert "simple built-in Iris capabilities" in prompt
    assert "Current web/docs research, code debugging, or multi-step desktop work" in prompt
    assert "Before calling agent, rewrite the spoken turn" in prompt
    assert "Do not pass a raw transcript through as the agent prompt" in prompt
    assert "open Safari" not in prompt
    assert "open DoorDash" not in prompt

    schema = basic_voice_tools()
    shell_tool = next(tool for tool in schema.standard_tools if tool.name == "shell_exec")
    assert "open a named Mac app with open -a" in shell_tool.description
    assert "opening apps" not in shell_tool.description
    screen_tool = next(tool for tool in schema.standard_tools if tool.name == "screen_vision")
    assert "let Gemini answer a visual question from the screenshot" in screen_tool.description
    assert screen_tool.required == ["question"]
    camera_tool = next(tool for tool in schema.standard_tools if tool.name == "camera_vision")
    assert "let Gemini answer a visual question from that camera image" in camera_tool.description
    assert camera_tool.required == ["question"]
    agent_tool = next(tool for tool in schema.standard_tools if tool.name == "agent")
    assert "Rewrite the user's spoken request" in agent_tool.properties["prompt"]["description"]
    assert "Do not use agent when a regular Iris tool can fully handle the request" in agent_tool.description
    assert "Use screen_vision instead when Gemini should answer from the current screen pixels" in agent_tool.description
    assert "Use camera_vision instead when Gemini should answer from the current camera view" in agent_tool.description
    assert "Use shell_exec instead for safe" in agent_tool.description
    assert "open a named Mac app with open -a" in agent_tool.description


async def test_screen_vision_can_capture_jpeg() -> None:
    image = await _capture_main_screen_jpeg()
    assert image.startswith(b"\xff\xd8\xff")
    assert len(image) > 1024


async def test_camera_vision_can_capture_jpeg() -> None:
    image = await _capture_default_camera_jpeg()
    assert image.startswith(b"\xff\xd8\xff")
    assert len(image) > 1024


async def test_playback_wake_interrupt_allows_interruption_command() -> None:
    gate = PlaybackWakeGateUserTurnStartStrategy(
        playback_active=lambda: True,
        echo_guard=FakeEchoGuard(False),
        enable_interruptions=True,
    )
    gate_starts: list[Any] = []
    gate.add_event_handler("on_user_turn_started", lambda *_args: gate_starts.append(_args[-1]))

    frame = transcription("Iris", speaker=1, final=False)
    assert await gate.process_frame(frame) == ProcessFrameResult.STOP
    assert len(gate_starts) == 1


async def test_playback_wake_interrupt_wins_over_echo_filter() -> None:
    gate = PlaybackWakeGateUserTurnStartStrategy(
        playback_active=lambda: True,
        echo_guard=FakeEchoGuard(True),
        enable_interruptions=True,
    )
    starts: list[Any] = []
    resets: list[Any] = []
    gate.add_event_handler("on_user_turn_started", lambda *_args: starts.append(_args[-1]))
    gate.add_event_handler("on_reset_aggregation", lambda *_args: resets.append(_args[-1]))

    frame = transcription("please Iris", speaker=1, final=False)
    assert await gate.process_frame(frame) == ProcessFrameResult.STOP
    assert len(starts) == 1
    assert len(resets) == 0


async def test_playback_wake_interrupt_does_not_need_downstream_addressing() -> None:
    gate = PlaybackWakeGateUserTurnStartStrategy(
        playback_active=lambda: True,
        echo_guard=FakeEchoGuard(False),
        enable_interruptions=True,
    )
    starts: list[Any] = []
    resets: list[Any] = []
    gate.add_event_handler("on_user_turn_started", lambda *_args: starts.append(_args[-1]))
    gate.add_event_handler("on_reset_aggregation", lambda *_args: resets.append(_args[-1]))

    wake_only = transcription("Iris", speaker=1, final=False)
    assert await gate.process_frame(wake_only) == ProcessFrameResult.STOP
    assert len(starts) == 1
    assert len(resets) == 0

    wake_with_prefix = transcription("Iris, are you there", speaker=1, final=False)
    assert await gate.process_frame(wake_with_prefix) == ProcessFrameResult.STOP
    assert len(starts) == 2
    assert len(resets) == 0


async def test_playback_wake_interrupt_accepts_iris_anywhere() -> None:
    gate = PlaybackWakeGateUserTurnStartStrategy(
        playback_active=lambda: True,
        echo_guard=FakeEchoGuard(False),
        enable_interruptions=True,
    )
    starts: list[Any] = []
    resets: list[Any] = []
    gate.add_event_handler("on_user_turn_started", lambda *_args: starts.append(_args[-1]))
    gate.add_event_handler("on_reset_aggregation", lambda *_args: resets.append(_args[-1]))

    prefixed = transcription("please Iris", speaker=1, final=False)
    assert await gate.process_frame(prefixed) == ProcessFrameResult.STOP
    assert len(starts) == 1
    assert len(resets) == 0

    greeting = transcription("hey Iris", speaker=1, final=False)
    assert await gate.process_frame(greeting) == ProcessFrameResult.STOP
    assert len(starts) == 2
    assert len(resets) == 0

    wake_command = transcription("Iris stop", speaker=1, final=False)
    assert await gate.process_frame(wake_command) == ProcessFrameResult.STOP
    assert len(starts) == 3
    assert len(resets) == 0

    non_wake = transcription("please increase your volume", speaker=1, final=True)
    assert await gate.process_frame(non_wake) == ProcessFrameResult.STOP
    assert len(starts) == 3
    assert len(resets) == 1


async def test_playback_echo_guard_only_filters_assistant_text() -> None:
    guard = PlaybackEchoGuard(playback_active=lambda: True)
    guard.append_assistant_text("How can I help you today?")

    assert guard.is_playback_echo(transcription("How can I help you today?", speaker=1))
    assert not guard.is_playback_echo(transcription("Find benchmarks for Reducto", speaker=0))


async def test_local_playback_is_active_at_tts_start() -> None:
    events = RuntimeEvents(
        FakeWebSocket(),
        VoiceSessionContext(
            session_id="test",
            device_id="test",
            user_id="user",
            organization_id="org",
            source="test",
            sample_rate=16000,
            channels=1,
        ),
    )
    calls: list[str] = []
    tracker = LocalPlaybackStateTracker(
        events=events,
        on_started=lambda: calls.append("started"),
        on_stopped=lambda: calls.append("stopped"),
        on_interrupted=lambda: calls.append("interrupted"),
        on_audio_frame=lambda _frame: calls.append("audio"),
    )

    tracker.handle_downstream_frame(TTSStartedFrame(context_id="ctx"))
    tracker.handle_downstream_frame(InterruptionFrame())

    assert calls == ["started", "interrupted"]


async def test_local_audio_output_drops_frames_after_interruption() -> None:
    writes: list[bool] = []
    output = DirectLocalAudioOutput(
        py_audio=None,
        sample_rate=16000,
        channels=1,
        on_speaker_write=lambda _frame, *, written: writes.append(written),
    )

    await output.handle_downstream_frame(InterruptionFrame())
    await output.handle_downstream_frame(
        OutputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1),
    )
    await output.handle_downstream_frame(TTSStoppedFrame(context_id="ctx"))

    assert writes == [False]


async def test_local_audio_output_resumes_on_next_tts_start() -> None:
    writes: list[bool] = []
    output = DirectLocalAudioOutput(
        py_audio=None,
        sample_rate=16000,
        channels=1,
        on_speaker_write=lambda _frame, *, written: writes.append(written),
    )

    await output.handle_downstream_frame(InterruptionFrame())
    await output.handle_downstream_frame(
        OutputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1),
    )
    await output.handle_downstream_frame(TTSStartedFrame(context_id="next"))
    assert output._drop_audio_until_tts_stop is False  # noqa: SLF001
    assert writes == [False]


async def test_local_audio_watchdog_detects_stalled_input_stream() -> None:
    manager = LocalAudioRuntimeManager()
    now = time.time()
    manager._task = asyncio.create_task(asyncio.sleep(60))  # noqa: SLF001
    manager._started_at = now - 120  # noqa: SLF001
    try:
        manager._last_audio_activity_at = None  # noqa: SLF001
        assert manager._is_stale_transcription_stream() is True  # noqa: SLF001
        assert manager._stale_audio_reason() == "no_audio_frames"  # noqa: SLF001

        manager._last_audio_activity_at = now - 50  # noqa: SLF001
        assert manager._is_stale_transcription_stream() is True  # noqa: SLF001
        assert manager._stale_audio_reason() == "audio_input_stalled"  # noqa: SLF001

        manager._last_audio_activity_at = now  # noqa: SLF001
        manager._last_transcript_at = now - 120  # noqa: SLF001
        assert manager._is_stale_transcription_stream() is True  # noqa: SLF001
        assert manager._stale_audio_reason() == "stale_transcripts"  # noqa: SLF001
    finally:
        manager._task.cancel()  # noqa: SLF001
        try:
            await manager._task  # noqa: SLF001
        except asyncio.CancelledError:
            pass


async def test_noop_tool_finishes_without_running_llm() -> None:
    llm = FakeLLM()
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    emitted: list[dict[str, Any]] = []
    events.add_listener(emitted.append)
    register_basic_voice_tools(
        llm,
        session=session(),
        events=events,
        wake_active_window_secs=DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS,
        llm_model="test-llm",
        tts_model="test-tts",
        stt_language="multi",
    )
    assert "noop" in llm.functions
    handler, _kwargs = llm.functions["noop"]
    callback_calls: list[tuple[dict[str, Any], Any]] = []

    async def result_callback(result: dict[str, Any], *, properties: Any = None) -> None:
        callback_calls.append((result, properties))

    params = FunctionCallParams(
        function_name="noop",
        tool_call_id="noop_contract",
        arguments={"reason": "ambient"},
        llm=llm,
        context=LLMContext(),
        result_callback=result_callback,
    )
    await handler(params)
    await asyncio.sleep(0)

    assert callback_calls
    result, properties = callback_calls[0]
    assert result == {"ok": True, "action": "noop", "reason": "ambient"}
    assert properties.run_llm is False
    assert properties.is_final is True
    assert any(message.get("type") == "assistant.no_speech" for message in emitted)


async def test_transcript_relay_marks_post_wake_turn_before_downstream_wake_event() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    relay = CapturingTranscriptRelay(events)

    await relay.process_frame(transcription("Iris,", speaker=0), FrameDirection.DOWNSTREAM)
    await relay.process_frame(transcription("What", speaker=0), FrameDirection.DOWNSTREAM)
    await relay.process_frame(transcription("time is", speaker=1), FrameDirection.DOWNSTREAM)

    assert len(relay.pushed_frames) == 3
    first_fragment = relay.pushed_frames[1]
    assert isinstance(first_fragment, TranscriptionFrame)
    assert "Iris just accepted a wake phrase" not in first_fragment.text
    followup_frame = relay.pushed_frames[2]
    assert isinstance(followup_frame, TranscriptionFrame)
    assert "Iris just accepted a wake phrase" in followup_frame.text
    assert "Current user turn:" in followup_frame.text
    assert "What time is" in followup_frame.text


async def test_transcript_relay_ingests_but_blocks_llm_while_playback_active() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    emitted: list[dict[str, Any]] = []
    events.add_listener(emitted.append)
    relay = CapturingTranscriptRelay(events, playback_active=lambda: True)

    frame = transcription("Should we go somewhere else entirely?", speaker=2)
    await relay.process_frame(frame, FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)

    assert relay.pushed_frames == []
    assert any(message.get("type") == "transcript.final" for message in emitted)
    assert any(message.get("type") == "transcript.final" for message in websocket.messages)


async def test_transcript_relay_hides_echo_and_blocks_chat() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    emitted: list[dict[str, Any]] = []
    events.add_listener(emitted.append)
    relay = CapturingTranscriptRelay(
        events,
        playback_active=lambda: True,
        playback_echo_guard=FakeEchoGuard(True),
    )

    frame = transcription("Should we go somewhere else entirely?", speaker=2)
    await relay.process_frame(frame, FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)

    assert relay.pushed_frames == []
    assert not any(message.get("type") == "transcript.final" for message in emitted)
    assert not any(message.get("type") == "transcript.final" for message in websocket.messages)


async def test_transcript_relay_interrupts_playback_when_transcriber_hears_iris() -> None:
    websocket = FakeWebSocket()
    events = RuntimeEvents(websocket, session())
    interrupted: list[str] = []

    async def interrupt_playback(reason: str) -> bool:
        interrupted.append(reason)
        return True

    relay = CapturingTranscriptRelay(
        events,
        playback_active=lambda: True,
        interrupt_playback=interrupt_playback,
    )

    frame = transcription("please Iris stop for a second", speaker=1, final=False)
    await relay.process_frame(frame, FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)

    assert interrupted == ["wake_transcript"]
    assert relay.pushed_frames == [frame]


async def test_regular_turn_strategy_accepts_assistant_followup_after_question() -> None:
    wake_strategy = IrisWakePhraseUserTurnStartStrategy(
        phrases=["iris"],
        timeout=12.0,
        echo_guard=FakeEchoGuard(False),
    )
    wake_strategy._state = wake_strategy.state.__class__.AWAKE  # noqa: SLF001
    transcription_strategy = TranscriptionUserTurnStartStrategy(enable_interruptions=False)
    starts: list[Any] = []
    transcription_strategy.add_event_handler(
        "on_user_turn_started",
        lambda *_args: starts.append(_args[-1]),
    )

    choice = transcription(
        "Yeah. Actually, it truly seems great. Let's do that. I'm vegetarian, so can you just order the vegetarian option that you see?",
        speaker=0,
    )
    assert await wake_strategy.process_frame(choice) == ProcessFrameResult.CONTINUE
    assert await transcription_strategy.process_frame(choice) == ProcessFrameResult.STOP
    assert len(starts) == 1


async def test_conversation_busy_guard() -> None:
    events = RuntimeEvents(FakeWebSocket(), session())
    events.set_assistant_responding(True)
    assert events.conversation_busy(quiet_seconds=0.01) is True
    events.set_assistant_responding(False)
    assert events.conversation_busy(quiet_seconds=0.01) is True
    await asyncio.sleep(0.02)
    assert events.conversation_busy(quiet_seconds=0.01) is False


async def test_many_pending_tool_results_clear_without_batch_injection() -> None:
    previous_env = {
        "IRIS_COMPLETION_QUIET_SECONDS": os.environ.get("IRIS_COMPLETION_QUIET_SECONDS"),
        "IRIS_COMPLETION_BATCH_SECONDS": os.environ.get("IRIS_COMPLETION_BATCH_SECONDS"),
        "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS": os.environ.get(
            "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"
        ),
    }
    os.environ["IRIS_COMPLETION_QUIET_SECONDS"] = "0"
    os.environ["IRIS_COMPLETION_BATCH_SECONDS"] = "0.05"
    os.environ["IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"] = "1"
    delivered_ids: list[str] = []
    original_mark_delivered = agent_completion_events.mark_session_agent_completion_delivered

    async def fake_mark_delivered(_session: VoiceSessionContext, completion_id: str) -> None:
        delivered_ids.append(completion_id)

    agent_completion_events.mark_session_agent_completion_delivered = fake_mark_delivered
    try:
        websocket = FakeWebSocket()
        events = RuntimeEvents(websocket, session())
        task = FakeTask()
        subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)
        callback_calls: list[tuple[dict[str, Any], Any]] = []

        async def result_callback(result: dict[str, Any], *, properties: Any = None) -> None:
            callback_calls.append((result, properties))

        for index in range(5):
            events.register_agent_tool_result(
                run_id=f"agent_run_batch_{index}",
                tool_call_id=f"tool_batch_{index}",
                request_id=f"request_batch_{index}",
                result_callback=result_callback,
            )
            await subscriber._handle_completion(  # noqa: SLF001
                {
                    "id": f"agent_completion_batch_{index}",
                    "sessionId": session().session_id,
                    "runId": f"agent_run_batch_{index}",
                    "status": "completed",
                    "delivery": "speak",
                    "content": f"Task {index} finished.",
                    "voice": {
                        "summary": f"Task {index} finished.",
                        "suggestedSpoken": f"Task {index} is done.",
                        "needsUserAction": False,
                    },
                    "createdAt": f"2026-05-24T12:00:0{index}.000Z",
                }
            )

        await asyncio.sleep(0)
        assert len(callback_calls) == 5
        assert all(properties.run_llm is True for _result, properties in callback_calls)
        assert all(events.get_agent_tool_result(f"agent_run_batch_{index}") is None for index in range(5))
        assert task.frames == []
        assert delivered_ids == [f"agent_completion_batch_{index}" for index in range(5)]
        assert any(
            message.get("type") == "agent.completion.tool_result" for message in websocket.messages
        )
        assert not any(message.get("type") == "agent.completion.queued" for message in websocket.messages)
        assert not any(message.get("type") == "agent.completion.injected" for message in websocket.messages)
        assert not any(message.get("type") == "agent.completion.batch_delivered" for message in websocket.messages)
    finally:
        agent_completion_events.mark_session_agent_completion_delivered = original_mark_delivered
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def test_scheduler_flushes_followup_batch_after_delivery_outcome() -> None:
    previous_env = {
        "IRIS_COMPLETION_QUIET_SECONDS": os.environ.get("IRIS_COMPLETION_QUIET_SECONDS"),
        "IRIS_COMPLETION_BATCH_SECONDS": os.environ.get("IRIS_COMPLETION_BATCH_SECONDS"),
        "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS": os.environ.get(
            "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"
        ),
    }
    os.environ["IRIS_COMPLETION_QUIET_SECONDS"] = "0"
    os.environ["IRIS_COMPLETION_BATCH_SECONDS"] = "0.01"
    os.environ["IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"] = "1"
    delivered_ids: list[str] = []
    original_mark_delivered = completion_delivery_scheduler.mark_session_agent_completion_delivered

    async def fake_mark_delivered(_session: VoiceSessionContext, completion_id: str) -> None:
        delivered_ids.append(completion_id)

    completion_delivery_scheduler.mark_session_agent_completion_delivered = fake_mark_delivered
    try:
        events = RuntimeEvents(FakeWebSocket(), session())
        task = FakeTask()
        subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)

        await subscriber._handle_completion(  # noqa: SLF001
            {
                "id": "agent_completion_first",
                "sessionId": session().session_id,
                "runId": "agent_run_first",
                "status": "completed",
                "delivery": "speak",
                "content": "First task finished.",
                "createdAt": "2026-05-24T12:01:00.000Z",
            }
        )
        for _ in range(20):
            if len(task.frames) == 1:
                break
            await asyncio.sleep(0.01)
        assert len(task.frames) == 1

        await subscriber._handle_completion(  # noqa: SLF001
            {
                "id": "agent_completion_second",
                "sessionId": session().session_id,
                "runId": "agent_run_second",
                "status": "completed",
                "delivery": "speak",
                "content": "Second task finished.",
                "createdAt": "2026-05-24T12:01:01.000Z",
            }
        )
        await asyncio.sleep(0.02)
        assert len(task.frames) == 1

        events.emit({"type": "assistant.turn.stopped", "interrupted": False, "text": "First done."})
        for _ in range(20):
            if len(task.frames) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(task.frames) == 2

        events.emit({"type": "assistant.turn.stopped", "interrupted": False, "text": "Second done."})
        for _ in range(20):
            if delivered_ids == ["agent_completion_first", "agent_completion_second"]:
                break
            await asyncio.sleep(0.01)
        assert delivered_ids == ["agent_completion_first", "agent_completion_second"]
    finally:
        completion_delivery_scheduler.mark_session_agent_completion_delivered = original_mark_delivered
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def test_interrupted_completion_delivery_retries_once() -> None:
    previous_env = {
        "IRIS_COMPLETION_QUIET_SECONDS": os.environ.get("IRIS_COMPLETION_QUIET_SECONDS"),
        "IRIS_COMPLETION_BATCH_SECONDS": os.environ.get("IRIS_COMPLETION_BATCH_SECONDS"),
        "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS": os.environ.get(
            "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"
        ),
        "IRIS_COMPLETION_INTERRUPTED_RETRY_LIMIT": os.environ.get(
            "IRIS_COMPLETION_INTERRUPTED_RETRY_LIMIT"
        ),
    }
    os.environ["IRIS_COMPLETION_QUIET_SECONDS"] = "0"
    os.environ["IRIS_COMPLETION_BATCH_SECONDS"] = "0.01"
    os.environ["IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS"] = "1"
    os.environ["IRIS_COMPLETION_INTERRUPTED_RETRY_LIMIT"] = "1"
    delivered_ids: list[str] = []
    original_mark_delivered = completion_delivery_scheduler.mark_session_agent_completion_delivered

    async def fake_mark_delivered(_session: VoiceSessionContext, completion_id: str) -> None:
        delivered_ids.append(completion_id)

    completion_delivery_scheduler.mark_session_agent_completion_delivered = fake_mark_delivered
    try:
        websocket = FakeWebSocket()
        events = RuntimeEvents(websocket, session())
        task = FakeTask()
        subscriber = AgentCompletionSubscriber(session=session(), events=events, task=task)

        await subscriber._handle_completion(  # noqa: SLF001
            {
                "id": "agent_completion_retry",
                "sessionId": session().session_id,
                "runId": "agent_run_retry",
                "status": "completed",
                "delivery": "speak",
                "content": "Retry task finished.",
                "createdAt": "2026-05-24T12:02:00.000Z",
            }
        )
        for _ in range(20):
            if len(task.frames) == 1:
                break
            await asyncio.sleep(0.01)
        assert len(task.frames) == 1

        events.emit({"type": "assistant.turn.stopped", "interrupted": True, "text": "Retry..."})
        for _ in range(20):
            if len(task.frames) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(task.frames) == 2
        assert delivered_ids == []
        assert any(message.get("type") == "agent.completion.batch_requeued" for message in websocket.messages)

        events.emit({"type": "assistant.turn.stopped", "interrupted": False, "text": "Retry done."})
        for _ in range(20):
            if delivered_ids == ["agent_completion_retry"]:
                break
            await asyncio.sleep(0.01)
        assert delivered_ids == ["agent_completion_retry"]
    finally:
        completion_delivery_scheduler.mark_session_agent_completion_delivered = original_mark_delivered
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def main() -> None:
    await test_pending_tool_result_delivery()
    await test_pending_tool_result_completion_is_not_enqueued()
    await test_developer_event_fallback_message()
    await test_completion_policy_helpers()
    await test_wake_phrase_only_matches_direct_turns()
    await test_transcription_wake_phrase_allows_speaker_context_prefix()
    await test_default_sound_recognition_ignores_low_confidence_room_noise()
    await test_default_wake_window_allows_real_followup_pacing()
    await test_wake_only_does_not_emit_spoken_ack()
    await test_system_prompt_treats_user_turns_as_imperfect_speech()
    await test_screen_vision_can_capture_jpeg()
    await test_camera_vision_can_capture_jpeg()
    await test_playback_wake_interrupt_allows_interruption_command()
    await test_playback_wake_interrupt_wins_over_echo_filter()
    await test_playback_wake_interrupt_does_not_need_downstream_addressing()
    await test_playback_wake_interrupt_accepts_iris_anywhere()
    await test_playback_echo_guard_only_filters_assistant_text()
    await test_local_playback_is_active_at_tts_start()
    await test_local_audio_output_drops_frames_after_interruption()
    await test_local_audio_output_resumes_on_next_tts_start()
    await test_local_audio_watchdog_detects_stalled_input_stream()
    await test_noop_tool_finishes_without_running_llm()
    await test_transcript_relay_marks_post_wake_turn_before_downstream_wake_event()
    await test_transcript_relay_ingests_but_blocks_llm_while_playback_active()
    await test_transcript_relay_hides_echo_and_blocks_chat()
    await test_transcript_relay_interrupts_playback_when_transcriber_hears_iris()
    await test_regular_turn_strategy_accepts_assistant_followup_after_question()
    await test_conversation_busy_guard()
    await test_many_pending_tool_results_clear_without_batch_injection()
    await test_scheduler_flushes_followup_batch_after_delivery_outcome()
    await test_interrupted_completion_delivery_retries_once()


if __name__ == "__main__":
    asyncio.run(main())
