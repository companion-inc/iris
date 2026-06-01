from __future__ import annotations

import os
from typing import Any, Callable

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSSpeakFrame,
)
from pipecat.observers.loggers.transcription_log_observer import TranscriptionLogObserver
from pipecat.observers.startup_timing_observer import StartupTimingObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.google.llm import GoogleLLMService, GoogleLLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService, OpenAIRealtimeSTTSettings
from pipecat.turns.user_start import (
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .api_client import fetch_session_config, llm_override, post_transcript_event
from .audio_gain import InputAudioAutoGain
from .env import merge_keyterms, optional_list_env, required_env, string_list
from .observability.frame_diagnostics import VoiceFrameDiagnostics
from .observability.observers import IrisVoiceObserver
from .prompt import system_instruction
from .response_policy import NoSpeechResponseFilter
from .runtime_events import RuntimeEvents
from .speaker_identity import SpeakerAudioBuffer, SpeakerIdentityMatcher
from .session import VoiceSessionContext
from .speech_emotion import SpeechEmotionClassifier
from .sound_recognition import SoundRecognitionRelay, SoundRecognitionState
from .tools import basic_voice_tools, register_basic_voice_tools
from .transcripts import TranscriptRelay
from .transport.device import DeviceTransport
from .tts import build_tts_service, configured_tts_sample_rate
from .turns.barge_in import (
    BARGE_IN_VAD_SAMPLE_RATE,
    build_barge_in_processor,
    build_barge_in_vad,
)
from .turns.playback_echo import PlaybackEchoGuard
from .turns.playback_wake_gate import PlaybackWakeGateUserTurnStartStrategy
from .turns.timing import USER_SPEECH_TIMEOUT_SECONDS
from .turns.wake import (
    DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS,
    WAKE_PHRASES,
    IrisWakePhraseUserTurnStartStrategy,
)


BUILTIN_STT_KEYTERMS = [
    "Iris",
    "stop",
    "cancel",
    "never mind",
    "what time is it",
    "current test status",
    "use my computer",
    "Reducto",
    "Reducto AI",
    "ParseBench",
    "RD-TableBench",
]
USER_TURN_STOP_TIMEOUT_SECONDS = 3.0


def provider_env(name: str, default: str) -> str:
    return os.getenv(name, default).strip().lower()


def build_stt_service(
    *,
    sample_rate: int,
    stt_model: str,
    stt_language: str,
    stt_keyterms: list[str],
):
    provider = provider_env("IRIS_STT_PROVIDER", "deepgram")
    if provider == "deepgram":
        deepgram_api_key = required_env("DEEPGRAM_API_KEY")
        logger.info(
            "iris.voice.stt_provider provider=deepgram model={} language={} sample_rate={}",
            stt_model,
            stt_language,
            sample_rate,
        )
        return DeepgramSTTService(
            api_key=deepgram_api_key,
            sample_rate=sample_rate,
            settings=DeepgramSTTSettings(
                model=stt_model,
                language=stt_language,
                punctuate=True,
                smart_format=True,
                interim_results=True,
                keyterm=stt_keyterms,
                endpointing=int(os.getenv("IRIS_STT_ENDPOINTING_MS", "500")),
                utterance_end_ms=1000,
                diarize=True,
                profanity_filter=False,
                extra={"vad_events": True},
            ),
        )

    if provider == "openai":
        openai_api_key = required_env("OPENAI_API_KEY")
        model = os.getenv("IRIS_OPENAI_STT_MODEL", os.getenv("IRIS_STT_MODEL", "gpt-4o-mini-transcribe"))
        logger.info(
            "iris.voice.stt_provider provider=openai model={} language={} sample_rate={}",
            model,
            stt_language,
            sample_rate,
        )
        return OpenAIRealtimeSTTService(
            api_key=openai_api_key,
            model=model,
            language=stt_language if stt_language != "multi" else None,
            settings=OpenAIRealtimeSTTSettings(model=model),
        )

    raise RuntimeError(f"Unsupported IRIS_STT_PROVIDER={provider!r}; expected 'deepgram' or 'openai'")


class AssistantRelay(FrameProcessor):
    def __init__(self, events: RuntimeEvents, *, echo_guard: PlaybackEchoGuard):
        super().__init__()
        self._events = events
        self._echo_guard = echo_guard

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseStartFrame):
            self._echo_guard.reset_assistant_text()
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TextFrame):
            self._echo_guard.append_assistant_text(frame.text)
            self._events.emit({"type": "assistant.text", "text": frame.text})
        await self.push_frame(frame, direction)


def attach_assistant_transcript_persistence(
    assistant_aggregator: FrameProcessor,
    *,
    events: RuntimeEvents,
    session: VoiceSessionContext,
    wake_strategy: IrisWakePhraseUserTurnStartStrategy | None = None,
) -> None:
    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(
        aggregator,
        message: AssistantTurnStoppedMessage,
    ):
        text = " ".join((message.content or "").split())
        interrupted = bool(message.interrupted)
        if not text:
            logger.info(
                "iris.voice.assistant_turn_empty session={} device={} interrupted={}",
                session.session_id,
                session.device_id,
                interrupted,
            )
            return

        await post_transcript_event(
            session,
            text=text,
            is_final=True,
            source="assistant",
            speaker="IRIS",
            segment_id=None,
            words=None,
            confidence=None,
        )
        events.emit(
            {
                "type": "assistant.turn.stopped",
                "interrupted": interrupted,
                "text": text,
            }
        )
        if not interrupted and wake_strategy and text.rstrip().endswith("?"):
            wake_strategy.mark_followup_expected(reason="assistant_turn_stopped_question")
        logger.info(
            "iris.voice.assistant_transcript_persisted session={} device={} interrupted={} chars={}",
            session.session_id,
            session.device_id,
            interrupted,
            len(text),
        )


def build_wake_strategy(
    events: RuntimeEvents,
    *,
    wake_active_window_secs: float,
    echo_guard: PlaybackEchoGuard,
    on_wake_only: Callable[[], Any] | None = None,
) -> IrisWakePhraseUserTurnStartStrategy:
    wake_strategy = IrisWakePhraseUserTurnStartStrategy(
        phrases=WAKE_PHRASES,
        timeout=wake_active_window_secs,
        echo_guard=echo_guard,
        on_wake_only=on_wake_only,
    )

    @wake_strategy.event_handler("on_wake_phrase_detected")
    async def on_wake_phrase_detected(strategy, phrase: str):
        logger.info(
            "iris.voice.wake.detected session={} device={} phrase={} timeout_secs={}",
            events.session_id,
            events.device_id,
            phrase,
            wake_active_window_secs,
        )
        events.mark_wake_detected(phrase)
        events.emit({"type": "wake.detected", "text": phrase})
        events.emit({"type": "wake.accepted", "text": phrase, "prompt": ""})

    @wake_strategy.event_handler("on_wake_phrase_timeout")
    async def on_wake_phrase_timeout(strategy):
        reason = strategy.consume_stop_reason()
        events.clear_wake_context_pending(reason=reason)
        logger.info(
            "iris.voice.wake.stopped session={} device={} reason={} timeout_secs={}",
            events.session_id,
            events.device_id,
            reason,
            wake_active_window_secs,
        )
        events.emit({"type": "wake.stopped", "text": "", "reason": reason})

    return wake_strategy


def build_context_aggregators(
    context: LLMContext,
    wake_strategy: IrisWakePhraseUserTurnStartStrategy,
    *,
    playback_active: Callable[[], bool],
    echo_guard: PlaybackEchoGuard,
    initial_awake: bool = False,
    sample_rate: int = BARGE_IN_VAD_SAMPLE_RATE,
) -> LLMContextAggregatorPair:
    start_strategies = [
        PlaybackWakeGateUserTurnStartStrategy(
            playback_active=playback_active,
            echo_guard=echo_guard,
            enable_interruptions=True,
        ),
        VADUserTurnStartStrategy(enable_interruptions=False),
        TranscriptionUserTurnStartStrategy(enable_interruptions=False),
    ]
    if not initial_awake:
        start_strategies.insert(0, wake_strategy)

    return LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=start_strategies,
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=USER_SPEECH_TIMEOUT_SECONDS,
                    )
                ],
            ),
            vad_analyzer=build_barge_in_vad(sample_rate),
            user_turn_stop_timeout=USER_TURN_STOP_TIMEOUT_SECONDS,
        ),
    )


def attach_pipeline_observer_logs(
    task: PipelineTask,
    *,
    events: RuntimeEvents,
    observers: list[Any],
) -> None:
    latency_observer = next(
        (
            observer
            for observer in observers
            if isinstance(observer, UserBotLatencyObserver)
        ),
        None,
    )
    startup_observer = next(
        (
            observer
            for observer in observers
            if isinstance(observer, StartupTimingObserver)
        ),
        None,
    )

    if latency_observer:

        @latency_observer.event_handler("on_latency_measured")
        async def on_latency_measured(observer, latency_seconds: float):
            logger.info(
                "iris.voice.latency session={} device={} user_to_bot_secs={:.3f}",
                events.session_id,
                events.device_id,
                latency_seconds,
            )

        @latency_observer.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(observer, breakdown):
            for event in breakdown.chronological_events():
                logger.info(
                    "iris.voice.latency.breakdown session={} device={} event={}",
                    events.session_id,
                    events.device_id,
                    event,
                )

    if startup_observer:

        @startup_observer.event_handler("on_startup_timing_report")
        async def on_startup_timing_report(observer, report):
            logger.info(
                "iris.voice.startup session={} device={} total_secs={:.3f}",
                events.session_id,
                events.device_id,
                report.total_duration_secs,
            )
            for timing in report.processor_timings:
                logger.info(
                    "iris.voice.startup.processor session={} device={} processor={} secs={:.3f}",
                    events.session_id,
                    events.device_id,
                    timing.processor_name,
                    timing.duration_secs,
                )

    turn_observer = task.turn_tracking_observer
    if turn_observer:

        @turn_observer.event_handler("on_turn_started")
        async def on_turn_started(observer, turn_number: int):
            logger.info(
                "iris.voice.turn.started session={} device={} turn={}",
                events.session_id,
                events.device_id,
                turn_number,
            )

        @turn_observer.event_handler("on_turn_ended")
        async def on_turn_ended(observer, turn_number: int, duration: float, was_interrupted: bool):
            logger.info(
                "iris.voice.turn.ended session={} device={} turn={} duration_secs={:.3f} interrupted={}",
                events.session_id,
                events.device_id,
                turn_number,
                duration,
                was_interrupted,
            )


async def run_voice_runtime(
    transport: DeviceTransport,
    session: VoiceSessionContext,
    events: RuntimeEvents,
    *,
    on_task_ready: Callable[[PipelineTask], None] | None = None,
):
    session_config = await fetch_session_config(session)
    user_memories = session_config.get("memories")
    memory_count = len(user_memories) if isinstance(user_memories, list) else 0
    sound_recognition_state = SoundRecognitionState.from_config(
        session_config.get("soundRecognition")
    )
    stt_model = os.getenv("IRIS_STT_MODEL", "nova-3")
    stt_language = os.getenv("IRIS_STT_LANGUAGE", "en")
    stt_keyterms = merge_keyterms(
        BUILTIN_STT_KEYTERMS,
        optional_list_env("IRIS_STT_KEYTERMS", []),
        string_list(session_config.get("keyterms")),
    )
    wake_active_window_override = os.getenv("IRIS_WAKE_ACTIVE_WINDOW_SECONDS")
    try:
        parsed_wake_active_window_secs = (
            float(wake_active_window_override) if wake_active_window_override else None
        )
    except ValueError:
        parsed_wake_active_window_secs = None
    wake_active_window_secs = (
        max(3.0, min(60.0, parsed_wake_active_window_secs))
        if parsed_wake_active_window_secs is not None
        else DEFAULT_WAKE_ACTIVE_WINDOW_SECONDS
    )
    input_sample_rate = BARGE_IN_VAD_SAMPLE_RATE
    logger.info(
        "iris.voice.stt_config model={} language={} sample_rate={} transport_sample_rate={} diarize=true profanity_filter=false keyterm_count={} memory_count={} wake_timeout_secs={}",
        stt_model,
        stt_language,
        input_sample_rate,
        session.sample_rate,
        len(stt_keyterms),
        memory_count,
        wake_active_window_secs,
    )
    stt = build_stt_service(
        sample_rate=input_sample_rate,
        stt_model=stt_model,
        stt_language=stt_language,
        stt_keyterms=stt_keyterms,
    )
    output_sample_rate = session.sample_rate
    tts_sample_rate = configured_tts_sample_rate(output_sample_rate)
    tts, tts_model = build_tts_service(
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        sample_rate=tts_sample_rate,
    )

    async def speak_tool_update(text: str) -> None:
        events.emit({"type": "assistant.text", "text": text})
        await tts.queue_frame(TTSSpeakFrame(text, append_to_context=True))

    async def speak_wake_ack() -> None:
        text = os.getenv("IRIS_WAKE_ACK_TEXT", "Yes?")
        logger.info(
            "iris.voice.wake.ack_queued session={} device={} text={!r}",
            events.session_id,
            events.device_id,
            text,
        )
        events.emit({"type": "assistant.text", "text": text})
        await tts.queue_frame(TTSSpeakFrame(text, append_to_context=False))

    logger.info(
        "iris.voice.turn_config wake_strategy=WakePhraseUserTurnStartStrategy local_wake_supported=false playback_gate=PlaybackWakeGateUserTurnStartStrategy start_strategy=VADUserTurnStartStrategy,TranscriptionUserTurnStartStrategy vad=SileroVADAnalyzer vad_sample_rate=16000 wake_phrases={} active_window_secs={} stop_strategy=SpeechTimeoutUserTurnStopStrategy user_turn_stop_timeout_secs={} user_speech_timeout_secs={}",
        ",".join(WAKE_PHRASES),
        wake_active_window_secs,
        USER_TURN_STOP_TIMEOUT_SECONDS,
        USER_SPEECH_TIMEOUT_SECONDS,
    )
    playback_echo_guard = PlaybackEchoGuard(playback_active=transport.is_playback_active)
    speaker_audio_buffer = SpeakerAudioBuffer()
    speaker_matcher = SpeakerIdentityMatcher(session_config, session=session)
    speech_emotion = SpeechEmotionClassifier(session=session)
    processors: list[FrameProcessor] = [
        transport.input(),
        speaker_audio_buffer,
        SoundRecognitionRelay(
            events=events,
            session=session,
            state=sound_recognition_state,
        ),
        InputAudioAutoGain(on_audio_activity=events.mark_audio_activity),
        build_barge_in_processor(),
        stt,
        VoiceFrameDiagnostics(label="after_stt"),
        TranscriptRelay(
            events,
            playback_active=transport.is_playback_active,
            playback_echo_guard=playback_echo_guard,
            audio_buffer=speaker_audio_buffer,
            speaker_matcher=speaker_matcher,
            speech_emotion=speech_emotion,
        ),
    ]

    llm_base_url, llm_override_model, llm_api_key = llm_override(session_config)
    llm_provider = provider_env("IRIS_LLM_PROVIDER", "gemini")
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if llm_base_url or llm_override_model:
        llm_model = llm_override_model or os.getenv("IRIS_CUSTOM_LLM_MODEL", "gpt-4.1")
        llm = OpenAILLMService(
            api_key=llm_api_key or "not-needed",
            base_url=llm_base_url,
            enable_async_tool_cancellation=True,
            settings=OpenAILLMService.Settings(
                model=llm_model,
                system_instruction=system_instruction(
                    user_memories if isinstance(user_memories, list) else None
                ),
                temperature=0.7,
            ),
        )
        logger.info(
            "iris.voice.llm_config provider=custom_chat_completions model={} base_url_override={}",
            llm_model,
            bool(llm_base_url),
        )
        wake_strategy = build_wake_strategy(
            events,
            wake_active_window_secs=wake_active_window_secs,
            echo_guard=playback_echo_guard,
            on_wake_only=speak_wake_ack,
        )
        register_basic_voice_tools(
            llm,
            session=session,
            events=events,
            wake_active_window_secs=wake_active_window_secs,
            llm_model=llm_model,
            tts_model=tts_model,
            stt_language=stt_language,
            wake_strategy=wake_strategy,
            sound_recognition_state=sound_recognition_state,
            speak=speak_tool_update,
        )
        context = LLMContext(tools=basic_voice_tools())
        context_aggregator = build_context_aggregators(
            context,
            wake_strategy,
            playback_active=transport.is_playback_active,
            echo_guard=playback_echo_guard,
            initial_awake=session.initial_awake,
            sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
        )
        assistant_aggregator = context_aggregator.assistant()
        attach_assistant_transcript_persistence(
            assistant_aggregator,
            events=events,
            session=session,
            wake_strategy=None if session.initial_awake else wake_strategy,
        )
        processors.extend(
            [
                context_aggregator.user(),
                llm,
                NoSpeechResponseFilter(events),
                AssistantRelay(events, echo_guard=playback_echo_guard),
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )
    elif llm_provider == "openai" and os.getenv("OPENAI_API_KEY"):
        llm_model = os.getenv("IRIS_OPENAI_LLM_MODEL", os.getenv("IRIS_LLM_MODEL", "gpt-4.1"))
        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            enable_async_tool_cancellation=True,
            settings=OpenAILLMService.Settings(
                model=llm_model,
                system_instruction=system_instruction(
                    user_memories if isinstance(user_memories, list) else None
                ),
                temperature=0.7,
            ),
        )
        logger.info("iris.voice.llm_config provider=openai model={}", llm_model)
        wake_strategy = build_wake_strategy(
            events,
            wake_active_window_secs=wake_active_window_secs,
            echo_guard=playback_echo_guard,
            on_wake_only=speak_wake_ack,
        )
        register_basic_voice_tools(
            llm,
            session=session,
            events=events,
            wake_active_window_secs=wake_active_window_secs,
            llm_model=llm_model,
            tts_model=tts_model,
            stt_language=stt_language,
            wake_strategy=wake_strategy,
            sound_recognition_state=sound_recognition_state,
            speak=speak_tool_update,
        )
        context = LLMContext(tools=basic_voice_tools())
        context_aggregator = build_context_aggregators(
            context,
            wake_strategy,
            playback_active=transport.is_playback_active,
            echo_guard=playback_echo_guard,
            initial_awake=session.initial_awake,
            sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
        )
        assistant_aggregator = context_aggregator.assistant()
        attach_assistant_transcript_persistence(
            assistant_aggregator,
            events=events,
            session=session,
            wake_strategy=None if session.initial_awake else wake_strategy,
        )
        processors.extend(
            [
                context_aggregator.user(),
                llm,
                NoSpeechResponseFilter(events),
                AssistantRelay(events, echo_guard=playback_echo_guard),
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )
    elif llm_provider == "gemini" and gemini_api_key:
        llm_model = os.getenv("IRIS_LLM_MODEL", "gemini-3-flash-preview")
        thinking_config = GoogleLLMService.ThinkingConfig(
            thinking_level="minimal",
            include_thoughts=False,
        )
        llm = GoogleLLMService(
            api_key=gemini_api_key,
            enable_async_tool_cancellation=True,
            settings=GoogleLLMSettings(
                model=llm_model,
                system_instruction=system_instruction(
                    user_memories if isinstance(user_memories, list) else None
                ),
                temperature=0.7,
                thinking=thinking_config,
            ),
        )
        logger.info(
            "iris.voice.llm_config provider=gemini model={} thinking={}",
            llm_model,
            thinking_config.model_dump(exclude_none=True) if thinking_config else None,
        )
        wake_strategy = build_wake_strategy(
            events,
            wake_active_window_secs=wake_active_window_secs,
            echo_guard=playback_echo_guard,
            on_wake_only=speak_wake_ack,
        )
        register_basic_voice_tools(
            llm,
            session=session,
            events=events,
            wake_active_window_secs=wake_active_window_secs,
            llm_model=llm_model,
            tts_model=tts_model,
            stt_language=stt_language,
            wake_strategy=wake_strategy,
            sound_recognition_state=sound_recognition_state,
            speak=speak_tool_update,
        )
        context = LLMContext(tools=basic_voice_tools())
        context_aggregator = build_context_aggregators(
            context,
            wake_strategy,
            playback_active=transport.is_playback_active,
            echo_guard=playback_echo_guard,
            initial_awake=session.initial_awake,
            sample_rate=BARGE_IN_VAD_SAMPLE_RATE,
        )
        assistant_aggregator = context_aggregator.assistant()
        attach_assistant_transcript_persistence(
            assistant_aggregator,
            events=events,
            session=session,
            wake_strategy=None if session.initial_awake else wake_strategy,
        )
        processors.extend(
            [
                context_aggregator.user(),
                llm,
                NoSpeechResponseFilter(events),
                AssistantRelay(events, echo_guard=playback_echo_guard),
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )
    else:
        logger.warning(
            "iris.voice.assistant_disabled provider={} missing_api_key=true session={}",
            llm_provider,
            session.session_id,
        )
        processors.append(transport.output())

    pipeline = Pipeline(processors)
    observers = [
        IrisVoiceObserver(events),
        TranscriptionLogObserver(),
        UserBotLatencyObserver(),
        StartupTimingObserver(),
    ]
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=input_sample_rate,
            audio_out_sample_rate=output_sample_rate,
            enable_heartbeats=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        enable_rtvi=False,
        idle_timeout_secs=None,
        observers=observers,
    )
    attach_pipeline_observer_logs(task, events=events, observers=observers)
    if on_task_ready:
        on_task_ready(task)
    await events.send({"type": "ready", "sessionId": session.session_id})
    await PipelineRunner(handle_sigint=False).run(task)
