from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.task import PipelineTask

from .api_client import mark_session_agent_completion_delivered
from .env import optional_float_env
from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext


DEFAULT_COMPLETION_BATCH_SECONDS = 1.0
DEFAULT_COMPLETION_URGENT_SECONDS = 0.5
DEFAULT_COMPLETION_DELIVERY_TIMEOUT_SECONDS = 30.0
DEFAULT_COMPLETION_INTERRUPTED_RETRY_LIMIT = 0


@dataclass
class ScheduledCompletion:
    completion_id: str
    run_id: str
    status: str
    delivery: str
    payload: dict[str, Any]
    created_monotonic: float
    delivery_attempts: int = 0


def completion_needs_fast_delivery(payload: dict[str, Any], *, failure_statuses: set[str]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    if status in failure_statuses:
        return True
    voice = payload.get("voice")
    if isinstance(voice, dict) and voice.get("needsUserAction") is True:
        return True
    result = payload.get("result")
    if isinstance(result, dict):
        voice_handoff = result.get("voiceHandoff")
        if isinstance(voice_handoff, dict) and voice_handoff.get("needsUserAction") is True:
            return True
    return False


class CompletionDeliveryScheduler:
    def __init__(
        self,
        *,
        session: VoiceSessionContext,
        events: RuntimeEvents,
        task: PipelineTask,
        playback_active: Callable[[], bool],
        quiet_seconds: float,
        message_builder: Callable[[list[dict[str, Any]]], dict[str, str]],
        failure_statuses: set[str],
    ):
        self._session = session
        self._events = events
        self._task = task
        self._playback_active = playback_active
        self._quiet_seconds = quiet_seconds
        self._message_builder = message_builder
        self._failure_statuses = failure_statuses
        self._batch_seconds = max(
            0.0,
            optional_float_env("IRIS_COMPLETION_BATCH_SECONDS", DEFAULT_COMPLETION_BATCH_SECONDS),
        )
        self._urgent_seconds = max(
            0.0,
            optional_float_env("IRIS_COMPLETION_URGENT_SECONDS", DEFAULT_COMPLETION_URGENT_SECONDS),
        )
        self._delivery_timeout_seconds = max(
            1.0,
            optional_float_env(
                "IRIS_COMPLETION_DELIVERY_TIMEOUT_SECONDS",
                DEFAULT_COMPLETION_DELIVERY_TIMEOUT_SECONDS,
            ),
        )
        self._interrupted_retry_limit = max(
            0,
            int(
                optional_float_env(
                    "IRIS_COMPLETION_INTERRUPTED_RETRY_LIMIT",
                    float(DEFAULT_COMPLETION_INTERRUPTED_RETRY_LIMIT),
                )
            ),
        )
        self._queue: list[ScheduledCompletion] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def enqueue(self, completion: ScheduledCompletion) -> None:
        async with self._lock:
            self._queue.append(completion)
            urgent = self._needs_fast_delivery(completion.payload)
            logger.info(
                "iris.voice.completion_scheduler_enqueued session={} device={} completion_id={} run_id={} status={} delivery={} queue_size={} urgent={}",
                self._session.session_id,
                self._session.device_id,
                completion.completion_id,
                completion.run_id,
                completion.status,
                completion.delivery,
                len(self._queue),
                urgent,
            )
            self._events.emit(
                {
                    "type": "agent.completion.queued",
                    "completionId": completion.completion_id,
                    "runId": completion.run_id,
                    "status": completion.status,
                    "delivery": completion.delivery,
                    "queueSize": len(self._queue),
                    "urgent": urgent,
                }
            )
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_when_ready())

    async def _flush_when_ready(self) -> None:
        try:
            started_at = time.monotonic()
            logged_defer = False
            while True:
                async with self._lock:
                    if not self._queue:
                        return
                    urgent = any(self._needs_fast_delivery(item.payload) for item in self._queue)
                    target_delay = self._urgent_seconds if urgent else self._batch_seconds
                    oldest = min(item.created_monotonic for item in self._queue)
                    wait_for_batch = max(0.0, oldest + target_delay - time.monotonic())
                    queued = list(self._queue)
                if wait_for_batch > 0:
                    await asyncio.sleep(min(wait_for_batch, 0.1))
                    continue
                if self._conversation_busy():
                    if not logged_defer:
                        snapshot = self._conversation_snapshot()
                        logger.info(
                            "iris.voice.completion_scheduler_defer_busy session={} device={} queue_size={} urgent={} activity={}",
                            self._session.session_id,
                            self._session.device_id,
                            len(queued),
                            urgent,
                            snapshot,
                        )
                        self._events.emit(
                            {
                                "type": "agent.completion.batch_deferred",
                                "completionIds": [item.completion_id for item in queued],
                                "reason": "conversation_busy",
                                "activity": snapshot,
                            }
                        )
                        logged_defer = True
                    await asyncio.sleep(0.25)
                    continue
                await self._flush_batch(started_at=started_at)
                started_at = time.monotonic()
                logged_defer = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "iris.voice.completion_scheduler_failed session={} device={}",
                self._session.session_id,
                self._session.device_id,
            )

    async def _flush_batch(self, *, started_at: float) -> None:
        async with self._lock:
            batch = list(self._queue)
            self._queue.clear()
        if not batch:
            return
        deferred_ms = int((time.monotonic() - started_at) * 1000)
        message = self._message_builder([item.payload for item in batch])
        outcome_future = self._delivery_outcome_future()
        await self._task.queue_frame(LLMMessagesAppendFrame(messages=[message], run_llm=True))
        for item in batch:
            self._events.emit(
                {
                    "type": "agent.completion.injected",
                    "completionId": item.completion_id,
                    "runId": item.run_id,
                    "status": item.status,
                    "delivery": item.delivery,
                    "deferredMs": deferred_ms,
                    "runLlm": True,
                    "batchSize": len(batch),
                }
            )
        logger.info(
            "iris.voice.completion_scheduler_inject session={} device={} batch_size={} completion_ids={} deferred_ms={} chars={}",
            self._session.session_id,
            self._session.device_id,
            len(batch),
            [item.completion_id for item in batch],
            deferred_ms,
            len(json.dumps([item.payload for item in batch], ensure_ascii=False)),
        )
        outcome = await self._wait_for_delivery_outcome(outcome_future)
        self._events.emit(
            {
                "type": "agent.completion.batch_delivered",
                "completionIds": [item.completion_id for item in batch],
                "outcome": outcome,
                "batchSize": len(batch),
            }
        )
        if outcome == "interrupted":
            retried = await self._requeue_interrupted_batch(batch)
            logger.info(
                "iris.voice.completion_scheduler_delivery_interrupted session={} device={} completion_ids={} retried={}",
                self._session.session_id,
                self._session.device_id,
                [item.completion_id for item in batch],
                retried,
            )
            return
        for item in batch:
            await mark_session_agent_completion_delivered(self._session, item.completion_id)

    async def _requeue_interrupted_batch(self, batch: list[ScheduledCompletion]) -> bool:
        retryable = [
            ScheduledCompletion(
                completion_id=item.completion_id,
                run_id=item.run_id,
                status=item.status,
                delivery=item.delivery,
                payload=item.payload,
                created_monotonic=time.monotonic(),
                delivery_attempts=item.delivery_attempts + 1,
            )
            for item in batch
            if item.delivery_attempts < self._interrupted_retry_limit
        ]
        if not retryable:
            return False
        async with self._lock:
            self._queue = retryable + self._queue
        self._events.emit(
            {
                "type": "agent.completion.batch_requeued",
                "completionIds": [item.completion_id for item in retryable],
                "reason": "interrupted",
                "attempt": max(item.delivery_attempts for item in retryable),
            }
        )
        return True

    def _delivery_outcome_future(self) -> asyncio.Future[str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def listener(message: dict[str, Any]) -> None:
            if future.done():
                return
            message_type = message.get("type")
            if message_type == "assistant.no_speech":
                future.set_result("no_speech")
            elif message_type == "assistant.turn.stopped":
                future.set_result("interrupted" if message.get("interrupted") else "spoken")

        remove = self._events.add_listener(listener)
        future.add_done_callback(lambda _future: remove())
        return future

    async def _wait_for_delivery_outcome(self, future: asyncio.Future[str]) -> str:
        try:
            return await asyncio.wait_for(future, timeout=self._delivery_timeout_seconds)
        except asyncio.TimeoutError:
            return "queued_timeout"

    def _needs_fast_delivery(self, payload: dict[str, Any]) -> bool:
        return completion_needs_fast_delivery(payload, failure_statuses=self._failure_statuses)

    def _conversation_busy(self) -> bool:
        return self._events.conversation_busy(
            playback_active=self._safe_playback_active(),
            quiet_seconds=self._quiet_seconds,
        )

    def _conversation_snapshot(self) -> dict[str, Any]:
        return self._events.conversation_activity_snapshot(
            playback_active=self._safe_playback_active()
        )

    def _safe_playback_active(self) -> bool:
        try:
            return bool(self._playback_active())
        except Exception:
            logger.exception(
                "iris.voice.completion_scheduler_playback_state_failed session={} device={}",
                self._session.session_id,
                self._session.device_id,
            )
            return False
