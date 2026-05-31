from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from loguru import logger
from pipecat.pipeline.task import PipelineTask
from pipecat.services.llm_service import FunctionCallResultProperties

from .api_client import (
    fetch_session_event_token,
    list_session_agent_completions,
    mark_session_agent_completion_delivered,
)
from .completion_delivery_scheduler import CompletionDeliveryScheduler, ScheduledCompletion
from .env import optional_float_env
from .llm_events import developer_event_message
from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext


FAILURE_COMPLETION_STATUSES = {"failed"}
SUPPRESSED_COMPLETION_STATUSES = {"interrupted", "cancelled"}
DEFAULT_COMPLETION_QUIET_SECONDS = 1.25


def should_run_llm_for_completion(*, delivery: str, status: str) -> bool:
    normalized_delivery = delivery.strip().lower()
    normalized_status = status.strip().lower()
    if normalized_status in SUPPRESSED_COMPLETION_STATUSES:
        return False
    if normalized_delivery == "silent":
        return False
    if normalized_delivery == "save":
        return normalized_status in FAILURE_COMPLETION_STATUSES
    return True


class AgentCompletionSubscriber:
    def __init__(
        self,
        *,
        session: VoiceSessionContext,
        events: RuntimeEvents,
        task: PipelineTask,
        playback_active: Callable[[], bool] | None = None,
    ):
        self._session = session
        self._events = events
        self._task = task
        self._playback_active = playback_active or (lambda: False)
        self._seen_ids: set[str] = set()
        self._last_seen_at: str | None = None
        self._quiet_seconds = max(
            0.0,
            optional_float_env("IRIS_COMPLETION_QUIET_SECONDS", DEFAULT_COMPLETION_QUIET_SECONDS),
        )
        self._scheduler = CompletionDeliveryScheduler(
            session=session,
            events=events,
            task=task,
            playback_active=self._safe_playback_active,
            quiet_seconds=self._quiet_seconds,
            message_builder=desktop_completion_batch_message,
            failure_statuses=FAILURE_COMPLETION_STATUSES,
        )

    async def run(self) -> None:
        token = await fetch_session_event_token(self._session)
        if token:
            try:
                await self._run_ably(token)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "iris.voice.agent_completion_ably_failed session={} device={}",
                    self._session.session_id,
                    self._session.device_id,
                )
        await self._run_polling()

    async def _run_ably(self, initial_token: dict[str, Any]) -> None:
        try:
            from ably import AblyRealtime  # type: ignore
        except Exception as error:
            raise RuntimeError("Ably Python SDK is not installed") from error

        next_token: dict[str, Any] | None = initial_token

        async def auth_callback(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal next_token
            if next_token:
                token = next_token
                next_token = None
            else:
                token = await fetch_session_event_token(self._session)
            if not token:
                raise RuntimeError("Iris event token is unavailable")
            return token.get("token_request")

        channel_name = str(initial_token.get("channel") or "")
        if not channel_name:
            raise RuntimeError("Iris event token did not include a channel")

        client = AblyRealtime(auth_callback=auth_callback)
        channel = client.channels.get(channel_name)

        async def on_message(message: Any) -> None:
            data = getattr(message, "data", None)
            if isinstance(data, dict):
                await self._handle_event(data)

        await channel.subscribe(on_message)
        logger.info(
            "iris.voice.agent_completion_subscribed session={} device={} transport=ably channel={}",
            self._session.session_id,
            self._session.device_id,
            channel_name,
        )
        try:
            await self._poll_once()
            while True:
                await asyncio.sleep(30)
                await self._poll_once()
        finally:
            try:
                channel.unsubscribe(on_message)
            except Exception:
                pass
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def _run_polling(self) -> None:
        logger.warning(
            "iris.voice.agent_completion_subscribed session={} device={} transport=polling",
            self._session.session_id,
            self._session.device_id,
        )
        while True:
            await self._poll_once()
            await asyncio.sleep(2)

    async def _poll_once(self) -> None:
        completions = await list_session_agent_completions(
            self._session,
            after=self._last_seen_at,
            limit=20,
        )
        for completion in reversed(completions):
            await self._handle_completion(completion)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "agent.completion.created":
            return
        data = event.get("data")
        if not isinstance(data, dict):
            return
        completion = data.get("completion")
        if isinstance(completion, dict):
            await self._handle_completion(completion)

    async def _handle_completion(self, completion: dict[str, Any]) -> None:
        completion_id = str(completion.get("id") or "")
        if not completion_id or completion_id in self._seen_ids:
            return
        if completion.get("sessionId") != self._session.session_id:
            return
        self._seen_ids.add(completion_id)
        created_at = completion.get("createdAt")
        if isinstance(created_at, str):
            self._last_seen_at = created_at

        content = str(completion.get("content") or completion.get("error") or "").strip()
        status = str(completion.get("status") or "completed")
        delivery = str(completion.get("delivery") or "auto")
        run_id = str(completion.get("runId") or "")
        payload = desktop_completion_payload(
            completion=completion,
            content=content,
            status=status,
            delivery=delivery,
            run_id=run_id,
        )
        if status.strip().lower() in SUPPRESSED_COMPLETION_STATUSES:
            self._events.clear_agent_tool_result(run_id)
            self._events.emit(
                {
                    "type": "agent.completion.suppressed",
                    "completionId": completion_id,
                    "runId": run_id,
                    "status": status,
                    "delivery": delivery,
                    "reason": "superseded",
                }
            )
            logger.info(
                "iris.voice.agent_completion_suppressed session={} device={} completion_id={} run_id={} status={} delivery={}",
                self._session.session_id,
                self._session.device_id,
                completion_id,
                run_id,
                status,
                delivery,
            )
            await mark_session_agent_completion_delivered(self._session, completion_id)
            return
        if not has_desktop_completion_payload(payload):
            logger.info(
                "iris.voice.agent_completion_skip_empty session={} device={} completion_id={} run_id={} status={} delivery={}",
                self._session.session_id,
                self._session.device_id,
                completion_id,
                run_id,
                status,
                delivery,
            )
            self._events.emit(
                {
                    "type": "agent.completion.skipped",
                    "completionId": completion_id,
                    "runId": run_id,
                    "status": status,
                    "delivery": delivery,
                    "reason": "empty",
                }
            )
            await mark_session_agent_completion_delivered(self._session, completion_id)
            return
        run_llm = should_run_llm_for_completion(delivery=delivery, status=status)
        if await self._deliver_as_pending_tool_result(
            run_id=run_id,
            completion_id=completion_id,
            payload=payload,
            run_llm=run_llm,
        ):
            logger.info(
                "iris.voice.agent_completion_lifecycle_closed session={} device={} completion_id={} run_id={} run_llm={}",
                self._session.session_id,
                self._session.device_id,
                completion_id,
                run_id,
                run_llm,
            )
            await mark_session_agent_completion_delivered(self._session, completion_id)
            return

        if not run_llm:
            self._events.emit(
                {
                    "type": "agent.completion.suppressed",
                    "completionId": completion_id,
                    "runId": run_id,
                    "status": status,
                    "delivery": delivery,
                }
            )
            await mark_session_agent_completion_delivered(self._session, completion_id)
            return

        await self._scheduler.enqueue(
            ScheduledCompletion(
                completion_id=completion_id,
                run_id=run_id,
                status=status,
                delivery=delivery,
                payload=payload,
                created_monotonic=time.monotonic(),
            )
        )

    async def _deliver_as_pending_tool_result(
        self,
        *,
        run_id: str,
        completion_id: str,
        payload: dict[str, Any],
        run_llm: bool,
    ) -> bool:
        if not run_id:
            return False
        pending = self._events.get_agent_tool_result(run_id)
        if not pending:
            return False
        callback = pending.get("resultCallback")
        if not callable(callback):
            self._events.clear_agent_tool_result(run_id)
            return False

        status = str(payload.get("status") or "")
        error = payload.get("error")
        result = {
            "ok": status not in FAILURE_COMPLETION_STATUSES and not error,
            "status": status,
            "delivery": payload.get("delivery"),
            "completion": payload,
        }
        try:
            await callback(
                result,
                properties=FunctionCallResultProperties(
                    is_final=True,
                    run_llm=run_llm,
                ),
            )
        except Exception:
            logger.exception(
                "iris.voice.agent_completion_tool_result_failed session={} device={} completion_id={} run_id={} tool_call_id={}",
                self._session.session_id,
                self._session.device_id,
                completion_id,
                run_id,
                pending.get("toolCallId"),
            )
            self._events.clear_agent_tool_result(run_id)
            return False

        self._events.clear_agent_tool_result(run_id)
        self._events.emit(
            {
                "type": "agent.completion.tool_result",
                "completionId": completion_id,
                "runId": run_id,
                "toolCallId": pending.get("toolCallId"),
                "runLlm": run_llm,
            }
        )
        logger.info(
            "iris.voice.agent_completion_tool_result session={} device={} completion_id={} run_id={} tool_call_id={} run_llm={}",
            self._session.session_id,
            self._session.device_id,
            completion_id,
            run_id,
            pending.get("toolCallId"),
            run_llm,
        )
        return True

    def _safe_playback_active(self) -> bool:
        try:
            return bool(self._playback_active())
        except Exception:
            logger.exception(
                "iris.voice.agent_completion_playback_state_failed session={} device={}",
                self._session.session_id,
                self._session.device_id,
            )
            return False

def desktop_completion_payload(
    *,
    completion: dict[str, Any],
    content: str,
    status: str,
    delivery: str,
    run_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "agent.completion",
        "completionId": str(completion.get("id") or ""),
        "runId": run_id,
        "status": status,
        "delivery": delivery,
    }
    if content:
        payload["content"] = content
    result = completion.get("result")
    if result is not None:
        payload["result"] = slim_desktop_completion_result(result)
    error = completion.get("error")
    if isinstance(error, str) and error.strip():
        payload["error"] = error.strip()
    voice = completion.get("voice")
    if isinstance(voice, dict):
        payload["voice"] = voice
    return payload


def slim_desktop_completion_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    slim: dict[str, Any] = {}
    for key in ("ok", "status", "assistantText", "text", "summary"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            slim[key] = value.strip()
        elif key == "ok" and isinstance(value, bool):
            slim[key] = value
    voice_handoff = result.get("voiceHandoff")
    if isinstance(voice_handoff, dict):
        slim["voiceHandoff"] = slim_voice_handoff(voice_handoff)
    voice = result.get("voice")
    if isinstance(voice, dict):
        slim["voice"] = slim_voice_handoff(voice)
    error = result.get("error")
    if isinstance(error, str) and error.strip():
        slim["error"] = error.strip()
    return slim or result


def slim_voice_handoff(voice: dict[str, Any]) -> dict[str, Any]:
    slim: dict[str, Any] = {}
    for key in ("type", "outcome", "summary", "screenState", "followUp", "suggestedSpoken"):
        value = voice.get(key)
        if isinstance(value, str) and value.strip():
            slim[key] = value.strip()
    needs_user_action = voice.get("needsUserAction")
    if isinstance(needs_user_action, bool):
        slim["needsUserAction"] = needs_user_action
    return slim


def has_desktop_completion_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("content", "result", "error", "voice"))


def desktop_completion_message(payload: dict[str, Any]) -> dict[str, str]:
    return developer_event_message(
        event_type="agent.completion",
        instruction=(
            "Use this finished desktop result as tool state, not user speech. Decide whether Iris "
            "should speak in the current voice conversation. Say the result at most once. Treat "
            "voice.suggestedSpoken as the preferred concise wording when present. If status is "
            "completed, do not say the task is still running."
        ),
        payload=payload,
    )


def desktop_completion_batch_message(payloads: list[dict[str, Any]]) -> dict[str, str]:
    if len(payloads) == 1:
        return desktop_completion_message(payloads[0])
    return developer_event_message(
        event_type="agent.completions",
        instruction=(
            "Use these finished desktop results as tool state, not user speech. Decide whether Iris "
            "should speak in the current voice conversation. If speaking, summarize the batch in one "
            "short update and mention at most two or three important items. Do not read every result."
        ),
        payload={
            "type": "agent.completions",
            "count": len(payloads),
            "completions": payloads,
        },
    )
