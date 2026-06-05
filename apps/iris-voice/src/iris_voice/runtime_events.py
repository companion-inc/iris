from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import WebSocket
from loguru import logger

from .api_client import post_session_end, post_transcript_event
from .session import VoiceSessionContext
from .transcript_types import TranscriptWord


class RuntimeEvents:
    def __init__(self, websocket: WebSocket, session: VoiceSessionContext):
        self._websocket = websocket
        self._session = session
        self._device_results: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._device_commands: dict[str, dict[str, Any]] = {}
        self._pending_agent_tool_results: dict[str, dict[str, Any]] = {}
        self._recent_transcripts: deque[dict[str, Any]] = deque(maxlen=240)
        self._wake_context_pending = False
        self._followup_expected_until: float | None = None
        self._user_speaking = False
        self._bot_speaking = False
        self._assistant_responding = False
        self._last_user_activity_at: float | None = None
        self._last_bot_activity_at: float | None = None
        self._last_user_turn_context: str | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, message: dict[str, Any]) -> None:
        self._notify_listeners(message)
        task = asyncio.create_task(self._send(message))
        task.add_done_callback(self._log_emit_task_failure)

    async def send(self, message: dict[str, Any]) -> None:
        self._notify_listeners(message)
        await self._send(message)

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    async def request_local_discovery(
        self,
        timeout: float = 10.0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request_device_command(
            "device.discovery.request",
            "device.discovery.result",
            timeout=timeout,
            request_id=request_id,
        )

    def new_device_command_id(self) -> str:
        return f"device-{int(asyncio.get_running_loop().time() * 1000)}-{os.urandom(3).hex()}"

    async def request_device_command(
        self,
        command_type: str,
        result_type: str,
        *,
        timeout: float = 5.0,
        request_id: str | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        request_id = request_id or self.new_device_command_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._device_results[request_id] = future
        self._set_device_command(
            request_id,
            command_type=command_type,
            status="running",
            payload=payload,
        )
        logger.info(
            "iris.voice.device_command.send session={} device={} request_id={} command_type={} result_type={} timeout_secs={} payload_keys={}",
            self._session.session_id,
            self._session.device_id,
            request_id,
            command_type,
            result_type,
            timeout,
            list(payload.keys())[:12],
        )
        try:
            await self._send(
                {
                    "type": command_type,
                    "resultType": result_type,
                    "requestId": request_id,
                    **payload,
                }
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.info(
                "iris.voice.device_command.result session={} device={} request_id={} command_type={} result_type={} ok={} result_keys={}",
                self._session.session_id,
                self._session.device_id,
                request_id,
                command_type,
                result_type,
                result.get("ok"),
                list(result.keys())[:12],
            )
            self._set_device_command(
                request_id,
                status="succeeded" if result.get("ok") is not False else "failed",
                result=result,
            )
            return result
        except Exception as error:
            logger.warning(
                "iris.voice.device_command.failed session={} device={} request_id={} command_type={} result_type={} error={}: {}",
                self._session.session_id,
                self._session.device_id,
                request_id,
                command_type,
                result_type,
                type(error).__name__,
                error,
            )
            self._set_device_command(request_id, status="failed", error=str(error))
            raise
        finally:
            self._device_results.pop(request_id, None)

    def handle_device_message(self, message: dict[str, Any]) -> bool:
        if message.get("type") not in {"device.discovery.result", "device.volume.result", "device.light.result"}:
            return False
        request_id = str(message.get("requestId") or "")
        future = self._device_results.get(request_id)
        if future and not future.done():
            result = message.get("result")
            future.set_result(result if isinstance(result, dict) else {})
        return True

    def list_device_commands(self) -> list[dict[str, Any]]:
        return list(self._device_commands.values())[-10:]

    def get_device_command(self, request_id: str) -> dict[str, Any] | None:
        return self._device_commands.get(request_id)

    def register_agent_tool_result(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        request_id: str,
        result_callback: Any,
    ) -> None:
        if not run_id:
            return
        self._pending_agent_tool_results[run_id] = {
            "runId": run_id,
            "toolCallId": tool_call_id,
            "requestId": request_id,
            "resultCallback": result_callback,
            "registeredAt": time.monotonic(),
        }
        self.emit(
            {
                "type": "agent.tool.pending",
                "runId": run_id,
                "toolCallId": tool_call_id,
                "requestId": request_id,
            }
        )

    def get_agent_tool_result(self, run_id: str) -> dict[str, Any] | None:
        return self._pending_agent_tool_results.get(run_id)

    def clear_agent_tool_result(self, run_id: str) -> None:
        self._pending_agent_tool_results.pop(run_id, None)

    def mark_wake_detected(self, phrase: str) -> None:
        self._wake_context_pending = True
        logger.info(
            "iris.voice.wake_context_pending session={} device={} phrase={}",
            self._session.session_id,
            self._session.device_id,
            phrase,
        )

    def clear_wake_context_pending(self, *, reason: str) -> None:
        if self._wake_context_pending:
            logger.info(
                "iris.voice.wake_context_cleared session={} device={} reason={}",
                self._session.session_id,
                self._session.device_id,
                reason,
            )
        self._wake_context_pending = False

    def mark_followup_expected(self, *, reason: str, timeout_seconds: float = 12.0) -> None:
        timeout_seconds = max(0.0, timeout_seconds)
        self._followup_expected_until = time.monotonic() + timeout_seconds
        logger.info(
            "iris.voice.followup_expected session={} device={} reason={} timeout_secs={}",
            self._session.session_id,
            self._session.device_id,
            reason,
            timeout_seconds,
        )

    def followup_expected(self) -> bool:
        deadline = self._followup_expected_until
        if deadline is None:
            return False
        if time.monotonic() <= deadline:
            return True
        self._followup_expected_until = None
        return False

    def consume_followup_expected(self, *, reason: str) -> bool:
        if not self.followup_expected():
            return False
        self._followup_expected_until = None
        logger.info(
            "iris.voice.followup_expected_consumed session={} device={} reason={}",
            self._session.session_id,
            self._session.device_id,
            reason,
        )
        return True

    def remember_transcript_context(
        self,
        *,
        text: str,
        speaker: str | None,
        speaker_user_id: str | None = None,
        speaker_display_name: str | None = None,
        wake_detected: bool = False,
    ) -> None:
        normalized = " ".join(text.split())
        if not normalized:
            return
        now = time.monotonic()
        self._recent_transcripts.append(
            {
                "text": normalized,
                "speaker": speaker,
                "speakerUserId": speaker_user_id,
                "speakerDisplayName": speaker_display_name,
                "wakeDetected": wake_detected,
                "monotonicAt": now,
            }
        )
        self._prune_recent_transcripts(now=now, lookback_seconds=600)

    def remember_user_turn_context(self, text: str) -> None:
        normalized = " ".join(text.split())
        self._last_user_turn_context = normalized or None

    def last_user_turn_context(self) -> str | None:
        return self._last_user_turn_context

    def mark_audio_activity(self, payload: dict[str, Any]) -> None:
        self.emit({"type": "audio.activity", **payload})

    def consume_wake_context(
        self,
        *,
        lookback_seconds: float,
        max_chars: int,
    ) -> str | None:
        if not self._wake_context_pending:
            return None
        self._wake_context_pending = False
        now = time.monotonic()
        records = self._recent_transcript_records(now=now, lookback_seconds=lookback_seconds)
        formatted = self._format_recent_transcripts(records, max_chars=max_chars)
        logger.info(
            "iris.voice.wake_context_consumed session={} device={} records={} chars={}",
            self._session.session_id,
            self._session.device_id,
            len(records),
            len(formatted or ""),
        )
        if formatted:
            self.emit(
                {
                    "type": "wake.context",
                    "lookbackSeconds": int(lookback_seconds),
                    "recordCount": len(records),
                    "chars": len(formatted),
                }
            )
        return formatted or ""

    def set_user_speaking(self, speaking: bool) -> None:
        self._user_speaking = speaking
        self._last_user_activity_at = time.monotonic()

    def set_bot_speaking(self, speaking: bool) -> None:
        self._bot_speaking = speaking
        self._last_bot_activity_at = time.monotonic()

    def set_assistant_responding(self, responding: bool) -> None:
        self._assistant_responding = responding
        self._last_bot_activity_at = time.monotonic()

    def conversation_busy(self, *, playback_active: bool = False, quiet_seconds: float = 0.0) -> bool:
        if self._user_speaking or self._bot_speaking or self._assistant_responding or playback_active:
            return True
        if quiet_seconds <= 0:
            return False
        now = time.monotonic()
        return self._activity_recent(self._last_user_activity_at, now, quiet_seconds) or self._activity_recent(
            self._last_bot_activity_at,
            now,
            quiet_seconds,
        )

    def conversation_activity_snapshot(self, *, playback_active: bool = False) -> dict[str, Any]:
        return {
            "userSpeaking": self._user_speaking,
            "botSpeaking": self._bot_speaking,
            "assistantResponding": self._assistant_responding,
            "playbackActive": playback_active,
            "lastUserActivityAgeMs": self._activity_age_ms(self._last_user_activity_at),
            "lastBotActivityAgeMs": self._activity_age_ms(self._last_bot_activity_at),
        }

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def device_id(self) -> str:
        return self._session.device_id

    async def _send(self, message: dict[str, Any]) -> None:
        try:
            await self._websocket.send_json(message)
        except Exception as error:
            logger.warning(
                "iris.voice.event_send_failed session={} device={} type={} keys={} error={}: {}",
                self._session.session_id,
                self._session.device_id,
                message.get("type"),
                list(message.keys())[:12],
                type(error).__name__,
                error,
            )

    def _log_emit_task_failure(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug(
                "iris.voice.event_send_cancelled session={} device={}",
                self._session.session_id,
                self._session.device_id,
            )
        except Exception:
            logger.exception(
                "iris.voice.event_send_task_failed session={} device={}",
                self._session.session_id,
                self._session.device_id,
            )

    def _notify_listeners(self, message: dict[str, Any]) -> None:
        if not self._listeners:
            return
        for listener in list(self._listeners):
            try:
                listener(message)
            except Exception:
                logger.exception(
                    "iris.voice.event_listener_failed session={} device={} type={}",
                    self._session.session_id,
                    self._session.device_id,
                    message.get("type"),
                )

    def _set_device_command(self, request_id: str, **updates: Any) -> None:
        now = time.time()
        current = self._device_commands.get(request_id)
        if current is None:
            current = {
                "requestId": request_id,
                "startedAt": now,
            }
        next_value = {**current, **updates, "updatedAt": now}
        if next_value.get("status") in {"succeeded", "failed", "cancelled"}:
            next_value.setdefault("finishedAt", now)
        self._device_commands[request_id] = next_value
        if len(self._device_commands) > 20:
            oldest = sorted(
                self._device_commands,
                key=lambda key: self._device_commands[key].get("updatedAt", 0),
            )[:-20]
            for key in oldest:
                self._device_commands.pop(key, None)
        self.emit(
            {
                "type": "device.command.status",
                "requestId": request_id,
                "commandType": next_value.get("command_type"),
                "status": next_value.get("status"),
                "result": next_value.get("result"),
                "error": next_value.get("error"),
            }
        )

    def _activity_age_ms(self, timestamp: float | None) -> int | None:
        if timestamp is None:
            return None
        return int((time.monotonic() - timestamp) * 1000)

    def _activity_recent(self, timestamp: float | None, now: float, quiet_seconds: float) -> bool:
        return timestamp is not None and now - timestamp < quiet_seconds

    def _prune_recent_transcripts(self, *, now: float, lookback_seconds: float) -> None:
        cutoff = now - lookback_seconds
        while self._recent_transcripts and self._recent_transcripts[0].get("monotonicAt", 0) < cutoff:
            self._recent_transcripts.popleft()

    def _recent_transcript_records(
        self,
        *,
        now: float,
        lookback_seconds: float,
    ) -> list[dict[str, Any]]:
        cutoff = now - max(0.0, lookback_seconds)
        return [
            record
            for record in self._recent_transcripts
            if isinstance(record.get("monotonicAt"), (int, float))
            and record["monotonicAt"] >= cutoff
            and not record.get("wakeDetected")
        ]

    def _format_recent_transcripts(
        self,
        records: Iterable[dict[str, Any]],
        *,
        max_chars: int,
    ) -> str | None:
        lines: list[str] = []
        total_chars = 0
        for record in reversed(list(records)):
            text = str(record.get("text") or "").strip()
            if not text:
                continue
            speaker = record.get("speakerDisplayName") or record.get("speaker")
            prefix = f"{speaker}: " if isinstance(speaker, str) and speaker.strip() else ""
            line = f"- {prefix}{text}"
            next_chars = total_chars + len(line) + 1
            if lines and next_chars > max_chars:
                break
            if not lines and len(line) > max_chars:
                line = line[: max(0, max_chars - 3)].rstrip() + "..."
            lines.append(line)
            total_chars += len(line) + 1
        if not lines:
            return None
        lines.reverse()
        return "\n".join(lines)

    def ingest_transcript(
        self,
        text: str,
        is_final: bool,
        speaker: str | None = None,
        segment_id: str | None = None,
        words: list[TranscriptWord] | None = None,
        confidence: float | None = None,
        speaker_user_id: str | None = None,
        speaker_confidence: float | None = None,
        emotion_label: str | None = None,
        emotion_confidence: float | None = None,
        emotion_model: str | None = None,
    ) -> None:
        asyncio.create_task(
            post_transcript_event(
                self._session,
                text=text,
                is_final=is_final,
                speaker=speaker,
                segment_id=segment_id,
                words=words,
                confidence=confidence,
                speaker_user_id=speaker_user_id,
                speaker_confidence=speaker_confidence,
                emotion_label=emotion_label,
                emotion_confidence=emotion_confidence,
                emotion_model=emotion_model,
            )
        )

    async def end_session(self, reason: str) -> None:
        await post_session_end(self._session, reason)
