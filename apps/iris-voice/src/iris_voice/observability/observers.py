from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from ..runtime_events import RuntimeEvents


OBSERVED_FRAME_TYPES = (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)


class IrisVoiceObserver(BaseObserver):
    """Logs the frame boundaries that explain playback and interruption behavior."""

    def __init__(self, events: RuntimeEvents):
        super().__init__(name="iris_voice_observer")
        self._events = events

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if not isinstance(frame, OBSERVED_FRAME_TYPES):
            return

        extra = _frame_extra(frame)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._events.set_user_speaking(True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._events.set_user_speaking(False)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._events.set_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._events.set_bot_speaking(False)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._events.set_assistant_responding(True)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._events.set_assistant_responding(False)
        elif isinstance(frame, InterruptionFrame):
            self._events.set_bot_speaking(False)
            self._events.set_assistant_responding(False)

        logger.info(
            "iris.voice.observer.frame session={} device={} frame={} direction={} source={} destination={} timestamp_ns={} extra={}",
            self._events.session_id,
            self._events.device_id,
            frame.__class__.__name__,
            data.direction.name,
            _processor_name(data.source),
            _processor_name(data.destination),
            data.timestamp,
            extra,
        )


def _frame_extra(frame: Frame) -> dict[str, object]:
    if isinstance(frame, FunctionCallsStartedFrame):
        return {
            "calls": [
                {
                    "name": call.function_name,
                    "toolCallId": call.tool_call_id,
                }
                for call in frame.function_calls
            ],
        }
    if isinstance(frame, FunctionCallInProgressFrame):
        return {
            "name": frame.function_name,
            "toolCallId": frame.tool_call_id,
            "cancelOnInterruption": frame.cancel_on_interruption,
            "argumentsKeys": list(frame.arguments.keys())[:12]
            if isinstance(frame.arguments, dict)
            else [],
        }
    if isinstance(frame, FunctionCallResultFrame):
        properties = frame.properties
        result = frame.result if isinstance(frame.result, dict) else {}
        return {
            "name": frame.function_name,
            "toolCallId": frame.tool_call_id,
            "isFinal": properties.is_final if properties else True,
            "runLlm": properties.run_llm if properties else None,
            "resultKeys": list(result.keys())[:12],
            "ok": result.get("ok"),
            "status": result.get("status"),
        }
    if isinstance(frame, FunctionCallCancelFrame):
        return {
            "name": frame.function_name,
            "toolCallId": frame.tool_call_id,
        }
    return {}


def _processor_name(processor: object) -> str:
    name = processor.__class__.__name__
    processor_id = getattr(processor, "id", None)
    if callable(processor_id):
        try:
            value = processor_id()
        except Exception:
            value = None
        if isinstance(value, int | str):
            return f"{name}#{value}"
    return name
