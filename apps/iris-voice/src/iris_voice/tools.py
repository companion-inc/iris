from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams, FunctionCallResultProperties

from .agent_bridge import (
    configured_agent_transport,
    infer_agent_delivery,
    normalize_agent_action,
    normalize_agent_context,
    normalize_agent_id,
    normalize_agent_prompt,
    normalize_agent_response_style,
    normalize_agent_thinking,
    normalize_agent_thread,
    normalize_agent_thread_id,
    normalize_agent_wait_ms,
    normalize_explicit_agent_action,
    post_agent_bridge,
)
from .api_client import (
    delete_user_memory,
    list_user_memories,
    post_session_light,
    post_session_volume,
    capture_native_screen_jpeg,
    request_native_permission,
    save_user_memory,
    search_transcripts,
    update_user_memory,
)
from .runtime_events import RuntimeEvents
from .session import VoiceSessionContext
from .sound_recognition import SoundRecognitionState
from .tool_schemas import basic_voice_tools
from .turns.wake import IrisWakePhraseUserTurnStartStrategy


RUNNING_AGENT_STATUSES = {"queued", "running"}
SENSITIVE_ARGUMENT_PARTS = ("authorization", "password", "secret", "token", "key")
MAX_LOG_TEXT_CHARS = 180
SHELL_EXEC_FORBIDDEN_CHARS = {"\n", "\r", "\x00", ";", "|", "&", ">", "<", "`"}
SHELL_EXEC_MAX_OUTPUT_CHARS = 6000
SCREEN_VISION_MAX_DIMENSION = 1600
CAMERA_VISION_MAX_DIMENSION = 1280


def _truncate_log_text(value: str, *, limit: int = MAX_LOG_TEXT_CHARS) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[:limit]}..."


def _summarize_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate_log_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {"type": "dict", "keys": list(value.keys())[:12], "count": len(value)}
    if isinstance(value, list | tuple):
        return {"type": "list", "count": len(value)}
    return type(value).__name__


def _summarize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if any(part in key_text.lower() for part in SENSITIVE_ARGUMENT_PARTS):
            summary[key_text] = "[redacted]"
        else:
            summary[key_text] = _summarize_log_value(value)
    return summary


def _summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"type": type(result).__name__}
    summary: dict[str, Any] = {
        "keys": list(result.keys())[:12],
    }
    for key in ("ok", "status", "requestId", "action", "agentId", "threadId", "error"):
        if key in result:
            summary[key] = _summarize_log_value(result.get(key))
    agent = result.get("agent")
    if isinstance(agent, Mapping):
        summary["agentStatus"] = agent_result_status(agent)
        summary["agentKeys"] = list(agent.keys())[:12]
    return summary


def _tool_started(
    params: FunctionCallParams,
    *,
    session: VoiceSessionContext,
    events: RuntimeEvents,
    command_id: str | None = None,
    **extra: Any,
) -> float:
    started_at = time.monotonic()
    events.emit(
        {
            "type": "tool.started",
            "name": params.function_name,
            "toolCallId": params.tool_call_id,
            "requestId": command_id,
            **{key: value for key, value in extra.items() if value is not None},
        }
    )
    logger.info(
        "iris.voice.tool.started session={} device={} name={} tool_call_id={} request_id={} args={} extra={}",
        session.session_id,
        session.device_id,
        params.function_name,
        params.tool_call_id,
        command_id,
        _summarize_arguments(params.arguments),
        {key: _summarize_log_value(value) for key, value in extra.items() if value is not None},
    )
    return started_at


async def _send_tool_result(
    params: FunctionCallParams,
    result: dict[str, Any],
    *,
    properties: FunctionCallResultProperties,
    session: VoiceSessionContext,
    events: RuntimeEvents,
    phase: str,
    started_at: float,
    command_id: str | None = None,
) -> None:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    result_summary = _summarize_result(result)
    events.emit(
        {
            "type": "tool.result",
            "name": params.function_name,
            "toolCallId": params.tool_call_id,
            "requestId": command_id,
            "phase": phase,
            "isFinal": properties.is_final,
            "runLlm": properties.run_llm,
            "elapsedMs": elapsed_ms,
            "summary": result_summary,
        }
    )
    logger.info(
        "iris.voice.tool.result session={} device={} name={} tool_call_id={} request_id={} phase={} is_final={} run_llm={} elapsed_ms={} summary={}",
        session.session_id,
        session.device_id,
        params.function_name,
        params.tool_call_id,
        command_id,
        phase,
        properties.is_final,
        properties.run_llm,
        elapsed_ms,
        result_summary,
    )
    try:
        await params.result_callback(result, properties=properties)
    except asyncio.CancelledError:
        events.emit(
            {
                "type": "tool.cancelled",
                "name": params.function_name,
                "toolCallId": params.tool_call_id,
                "requestId": command_id,
                "phase": phase,
                "elapsedMs": elapsed_ms,
            }
        )
        logger.warning(
            "iris.voice.tool.result_callback_cancelled session={} device={} name={} tool_call_id={} request_id={} phase={} elapsed_ms={}",
            session.session_id,
            session.device_id,
            params.function_name,
            params.tool_call_id,
            command_id,
            phase,
            elapsed_ms,
        )
        raise
    except Exception as error:
        events.emit(
            {
                "type": "tool.callback.failed",
                "name": params.function_name,
                "toolCallId": params.tool_call_id,
                "requestId": command_id,
                "phase": phase,
                "error": str(error),
                "elapsedMs": elapsed_ms,
            }
        )
        logger.exception(
            "iris.voice.tool.result_callback_failed session={} device={} name={} tool_call_id={} request_id={} phase={} elapsed_ms={}",
            session.session_id,
            session.device_id,
            params.function_name,
            params.tool_call_id,
            command_id,
            phase,
            elapsed_ms,
        )
        raise
    logger.info(
        "iris.voice.tool.result_callback_sent session={} device={} name={} tool_call_id={} request_id={} phase={} elapsed_ms={}",
        session.session_id,
        session.device_id,
        params.function_name,
        params.tool_call_id,
        command_id,
        phase,
        elapsed_ms,
    )


def agent_result_status(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if isinstance(status, str):
        return normalize_agent_status(status)
    run = result.get("run")
    if isinstance(run, dict) and isinstance(run.get("status"), str):
        return normalize_agent_status(run["status"])
    return None


def agent_result_run_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("runId", "requestId", "id"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    run = result.get("run")
    if isinstance(run, dict):
        value = run.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_agent_status(status: str) -> str:
    normalized = status.strip().lower().replace("_", "").replace("-", "")
    if normalized == "inprogress":
        return "running"
    return status.strip().lower()


def agent_result_waits_for_completion(status: str | None) -> bool:
    return status in RUNNING_AGENT_STATUSES


def agent_result_should_run_llm(*, status: str | None, delivery: str) -> bool:
    if delivery == "silent":
        return False
    if delivery == "save":
        return status in {*RUNNING_AGENT_STATUSES, "failed"}
    return True



def _tool_error_message(error: Exception, fallback: str) -> str:
    message = str(error).strip()
    if message:
        return message
    if isinstance(error, TimeoutError):
        return fallback
    return type(error).__name__


async def _maybe_speak(
    speak: Callable[[str], Awaitable[None]] | None,
    text: str,
) -> None:
    if not speak:
        return
    try:
        await speak(text)
    except Exception:
        logger.exception("iris.voice.tool_speak_failed text={}", _truncate_log_text(text))


def _volume_spoken_ack(action: str, volume: Any) -> str:
    volume_text = (
        f" to {int(round(volume))} percent"
        if isinstance(volume, (int, float)) and not isinstance(volume, bool)
        else ""
    )
    if action == "increase":
        return f"Increased{volume_text}."
    if action == "decrease":
        return f"Decreased{volume_text}."
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return f"Unmuted{volume_text}."
    return f"Volume set{volume_text}."


def _normalize_memory_kind(value: Any) -> str:
    return value if value in {"fact", "preference", "instruction"} else "fact"


def _normalize_memory_confidence(value: Any) -> str:
    return value if value in {"explicit", "high", "medium"} else "high"


def _normalize_memory_action(value: Any) -> str:
    return value if value in {"list", "save", "update", "delete"} else "list"


def _shell_exec_cwd() -> str:
    configured = os.getenv("IRIS_SHELL_EXEC_CWD", "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.expanduser("~"), "Iris", "Workspace")


def _validate_shell_exec_command(command: Any) -> tuple[str | None, str | None]:
    if not isinstance(command, str):
        return None, "Command must be a string"
    normalized = command.strip()
    if not normalized:
        return None, "Command is empty"
    if len(normalized) > 240:
        return None, "Command is too long"
    if any(char in normalized for char in SHELL_EXEC_FORBIDDEN_CHARS):
        return None, "Command must be one simple command without shell operators"
    if "$(" in normalized or "${" in normalized:
        return None, "Command substitution is not allowed"
    try:
        parts = shlex.split(normalized)
    except ValueError as error:
        return None, str(error)
    if not parts:
        return None, "Command is empty"
    return normalized, None


def _shell_exec_timeout(value: Any) -> float:
    if isinstance(value, bool):
        return 5.0
    if isinstance(value, (int, float)):
        return float(max(1, min(10, int(value))))
    return 5.0


def _shell_exec_spoken_result(result: Mapping[str, Any]) -> str | None:
    if result.get("ok") is not True:
        error = result.get("stderr") or result.get("error")
        return f"Command failed: {str(error).strip()}" if error else "Command failed."
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return None
    text = " ".join(stdout.strip().split())
    if not text or len(text) > 180 or "\n[truncated]" in stdout:
        return None
    return text


async def _run_capture_command(*args: str, timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return 124, stdout, stderr
    return process.returncode or 0, stdout, stderr


async def _capture_main_screen_jpeg() -> bytes:
    image = await capture_native_screen_jpeg()
    if not image:
        raise RuntimeError("native screen capture produced an empty image")
    return image


async def _capture_default_camera_jpeg() -> bytes:
    fd, path = tempfile.mkstemp(prefix="iris-camera-", suffix=".jpg")
    os.close(fd)
    ffmpeg = os.getenv("IRIS_FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")
    try:
        code, _stdout, stderr = await _run_capture_command(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-video_size",
            "1280x720",
            "-framerate",
            "30",
            "-i",
            "0:none",
            "-frames:v",
            "1",
            "-y",
            path,
            timeout=15.0,
        )
        if code != 0:
            error = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or f"ffmpeg camera capture exited with {code}")
        if CAMERA_VISION_MAX_DIMENSION > 0:
            resize_code, _resize_stdout, resize_stderr = await _run_capture_command(
                "/usr/bin/sips",
                "-Z",
                str(CAMERA_VISION_MAX_DIMENSION),
                path,
                timeout=8.0,
            )
            if resize_code != 0:
                logger.warning(
                    "iris.voice.camera_vision_resize_failed exit_code={} stderr={}",
                    resize_code,
                    _truncate_log_text(resize_stderr.decode("utf-8", errors="replace")),
                )
        with open(path, "rb") as file:
            image = file.read()
        if not image:
            raise RuntimeError("camera capture produced an empty image")
        return image
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def register_basic_voice_tools(
    llm: Any,
    *,
    session: VoiceSessionContext,
    events: RuntimeEvents,
    wake_active_window_secs: float,
    llm_model: str,
    tts_model: str,
    stt_language: str,
    wake_strategy: IrisWakePhraseUserTurnStartStrategy | None = None,
    sound_recognition_state: SoundRecognitionState | None = None,
    speak: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    async def noop(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        reason = params.arguments.get("reason")
        result = {
            "ok": True,
            "action": "noop",
            "reason": reason if reason in {"ambient", "not_addressed", "ambiguous", "already_handled"} else "ambiguous",
        }
        events.emit({"type": "assistant.no_speech", "reason": result["reason"]})
        logger.info(
            "iris.voice.noop session={} device={} reason={}",
            session.session_id,
            session.device_id,
            result["reason"],
        )
        await _send_tool_result(
            params,
            result,
            properties=FunctionCallResultProperties(run_llm=False),
            session=session,
            events=events,
            phase="final",
            started_at=started_at,
        )

    async def status(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        result = {
            "deviceId": session.device_id,
            "sessionId": session.session_id,
            "source": session.source,
            "sampleRate": session.sample_rate,
            "channels": session.channels,
            "runtime": "pipecat",
            "sttModel": os.getenv("IRIS_STT_MODEL", "nova-3"),
            "sttLanguage": stt_language,
            "llmModel": llm_model,
            "ttsModel": tts_model,
            "wakeWord": "Iris",
            "wakeActiveWindowSeconds": wake_active_window_secs,
            "transcriptStorageConfigured": bool(
                os.getenv("IRIS_API_URL") and os.getenv("IRIS_API_KEY")
            ),
            "recentCommands": events.list_device_commands(),
            "soundRecognition": sound_recognition_state.snapshot()
            if sound_recognition_state
            else {"enabled": False, "watches": []},
        }
        events.emit({"type": "tool.called", "name": "status"})
        logger.info(
            "iris.voice.tool.called session={} device={} name=status",
            session.session_id,
            session.device_id,
        )
        await _send_tool_result(
            params,
            result,
            properties=FunctionCallResultProperties(run_llm=True),
            session=session,
            events=events,
            phase="final",
            started_at=started_at,
        )

    async def command(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        request_id = params.arguments.get("requestId")
        command = (
            events.get_device_command(str(request_id))
            if isinstance(request_id, str) and request_id
            else None
        )
        result = {
            "deviceId": session.device_id,
            "command": command,
            "recentCommands": [] if command else events.list_device_commands(),
        }
        events.emit({"type": "tool.called", "name": "command"})
        logger.info(
            "iris.voice.tool.called session={} device={} name=command request_id={}",
            session.session_id,
            session.device_id,
            request_id,
        )
        await _send_tool_result(
            params,
            result,
            properties=FunctionCallResultProperties(run_llm=True),
            session=session,
            events=events,
            phase="final",
            started_at=started_at,
            command_id=str(request_id) if isinstance(request_id, str) else None,
        )

    async def shell_exec(params: FunctionCallParams) -> None:
        command, validation_error = _validate_shell_exec_command(params.arguments.get("command"))
        timeout = _shell_exec_timeout(params.arguments.get("timeoutSeconds"))
        cwd = _shell_exec_cwd()
        command_id = events.new_device_command_id()
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            command_id=command_id,
            command=command or params.arguments.get("command"),
            timeoutSeconds=timeout,
            cwd=cwd,
        )
        events.emit({
            "type": "tool.called",
            "name": "shell_exec",
            "requestId": command_id,
        })
        if validation_error or not command:
            await _send_tool_result(
                params,
                {"ok": False, "error": validation_error or "Invalid command", "requestId": command_id},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
                command_id=command_id,
            )
            return
        try:
            os.makedirs(cwd, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                timed_out = False
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                timed_out = True
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            if len(stdout_text) > SHELL_EXEC_MAX_OUTPUT_CHARS:
                stdout_text = stdout_text[:SHELL_EXEC_MAX_OUTPUT_CHARS] + "\n[truncated]"
            if len(stderr_text) > SHELL_EXEC_MAX_OUTPUT_CHARS:
                stderr_text = stderr_text[:SHELL_EXEC_MAX_OUTPUT_CHARS] + "\n[truncated]"
            result = {
                "ok": process.returncode == 0 and not timed_out,
                "requestId": command_id,
                "command": command,
                "cwd": cwd,
                "exitCode": process.returncode,
                "timedOut": timed_out,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
            logger.info(
                "iris.voice.tool.called session={} device={} name=shell_exec request_id={} exit_code={} timed_out={} stdout_chars={} stderr_chars={}",
                session.session_id,
                session.device_id,
                command_id,
                process.returncode,
                timed_out,
                len(stdout_text),
                len(stderr_text),
            )
            await _send_tool_result(
                params,
                result,
                properties=FunctionCallResultProperties(run_llm=False),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
                command_id=command_id,
            )
            spoken_result = _shell_exec_spoken_result(result)
            if spoken_result:
                await _maybe_speak(speak, spoken_result)
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=shell_exec request_id={}",
                session.session_id,
                session.device_id,
                command_id,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": str(error), "requestId": command_id, "command": command},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
                command_id=command_id,
            )

    async def end_chat(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        events.emit({"type": "tool.called", "name": "end"})
        wake_reset = False
        if wake_strategy:
            wake_reset = await wake_strategy.force_idle(reason="tool")
        else:
            events.emit({"type": "wake.stopped", "text": "", "reason": "tool"})
        logger.info(
            "iris.voice.tool.called session={} device={} name=end wake_reset={}",
            session.session_id,
            session.device_id,
            wake_reset,
        )
        await _send_tool_result(
            params,
            {"ended": True},
            properties=FunctionCallResultProperties(run_llm=False),
            session=session,
            events=events,
            phase="final",
            started_at=started_at,
        )
        await events.end_session("tool:end")

    async def volume(params: FunctionCallParams) -> None:
        action = params.arguments.get("action")
        if action not in {"set", "increase", "decrease", "mute", "unmute"}:
            action = "set"
        raw_volume = params.arguments.get("volume")
        volume = None
        if isinstance(raw_volume, (int, float)) and not isinstance(raw_volume, bool):
            volume = max(0, min(100, int(round(raw_volume))))
        command_id = events.new_device_command_id()
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            command_id=command_id,
            action=action,
            volume=volume,
        )
        events.emit({"type": "tool.called", "name": "volume"})
        await _send_tool_result(
            params,
            {
                "ok": True,
                "status": "accepted",
                "deviceId": session.device_id,
                "requestId": command_id,
            },
            properties=FunctionCallResultProperties(is_final=False, run_llm=False),
            session=session,
            events=events,
            phase="accepted",
            started_at=started_at,
            command_id=command_id,
        )
        try:
            logger.info(
                "iris.voice.tool.device_command_start session={} device={} name=volume request_id={} action={} volume={} timeout_secs=8.0",
                session.session_id,
                session.device_id,
                command_id,
                action,
                volume,
            )
            direct_result = await events.request_device_command(
                "device.volume.change",
                "device.volume.result",
                timeout=8.0,
                request_id=command_id,
                action=action,
                volume=volume,
            )
            if direct_result.get("ok") is False:
                raise RuntimeError(str(direct_result.get("error") or "Failed to set volume"))
            direct_volume = direct_result.get("volume")
            if not isinstance(direct_volume, (int, float)) or isinstance(direct_volume, bool):
                raise RuntimeError("Device did not return a valid volume")
            result = await post_session_volume(
                session,
                action="set",
                volume=int(round(direct_volume)),
                sync_device=True,
            )
            logger.info(
                "iris.voice.tool.called session={} device={} name=volume action={} volume={}",
                session.session_id,
                session.device_id,
                action,
                result.get("volume"),
            )
            await _maybe_speak(speak, _volume_spoken_ack(action, result.get("volume")))
            await _send_tool_result(
                params,
                {
                    "ok": True,
                    "volume": result.get("volume"),
                    "deviceId": result.get("deviceId") or session.device_id,
                    "deviceSyncOk": result.get("deviceSyncOk"),
                },
                properties=FunctionCallResultProperties(run_llm=False),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
                command_id=command_id,
            )
        except Exception as error:
            error_message = _tool_error_message(
                error,
                "Timed out waiting for the device to confirm the volume change",
            )
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=volume error={}",
                session.session_id,
                session.device_id,
                error_message,
            )
            await _maybe_speak(speak, "I couldn't change the volume.")
            await _send_tool_result(
                params,
                {"ok": False, "error": error_message},
                properties=FunctionCallResultProperties(run_llm=False),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
                command_id=command_id,
            )

    async def light(params: FunctionCallParams) -> None:
        effect = params.arguments.get("effect")
        if effect not in {"off", "breath", "rainbow", "solid", "doa"}:
            effect = None
        color = params.arguments.get("color")
        if not isinstance(color, str) or not re.fullmatch(r"(#|0x)?[0-9a-fA-F]{6}", color.strip()):
            color = None
        raw_brightness = params.arguments.get("brightness")
        brightness = None
        if isinstance(raw_brightness, (int, float)) and not isinstance(raw_brightness, bool):
            brightness = max(0, min(255, int(round(raw_brightness))))
        if effect is None and color is None and brightness is None:
            effect = "solid"
            color = "#ff8800"
        command_id = events.new_device_command_id()
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            command_id=command_id,
            effect=effect,
            color=color,
            brightness=brightness,
        )
        events.emit({"type": "tool.called", "name": "light"})
        await _send_tool_result(
            params,
            {
                "ok": True,
                "status": "accepted",
                "deviceId": session.device_id,
                "requestId": command_id,
            },
            properties=FunctionCallResultProperties(is_final=False, run_llm=False),
            session=session,
            events=events,
            phase="accepted",
            started_at=started_at,
            command_id=command_id,
        )
        try:
            logger.info(
                "iris.voice.tool.device_command_start session={} device={} name=light request_id={} effect={} color={} brightness={} timeout_secs=20.0",
                session.session_id,
                session.device_id,
                command_id,
                effect,
                color,
                brightness,
            )
            direct_result = await events.request_device_command(
                "device.light.change",
                "device.light.result",
                timeout=20.0,
                request_id=command_id,
                effect=effect,
                color=color,
                brightness=brightness,
            )
            if direct_result.get("ok") is False:
                raise RuntimeError(str(direct_result.get("error") or "Failed to set light"))
            result = await post_session_light(
                session,
                effect=effect,
                color=color,
                brightness=brightness,
            )
            status_light = result.get("statusLight")
            direct = status_light if isinstance(status_light, dict) else {}
            logger.info(
                "iris.voice.tool.called session={} device={} name=light "
                "effect={} color={} brightness={}",
                session.session_id,
                session.device_id,
                effect,
                color,
                brightness,
            )
            await _maybe_speak(speak, "Light updated.")
            await _send_tool_result(
                params,
                {"ok": True, "statusLight": status_light or direct, "deviceId": session.device_id},
                properties=FunctionCallResultProperties(run_llm=False),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
                command_id=command_id,
            )
        except Exception as error:
            error_message = _tool_error_message(
                error,
                "Timed out waiting for the device to confirm the light change",
            )
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=light error={}",
                session.session_id,
                session.device_id,
                error_message,
            )
            await _maybe_speak(speak, "I couldn't update the light.")
            await _send_tool_result(
                params,
                {"ok": False, "error": error_message},
                properties=FunctionCallResultProperties(run_llm=False),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
                command_id=command_id,
            )

    async def discover(params: FunctionCallParams) -> None:
        command_id = events.new_device_command_id()
        started_at = _tool_started(params, session=session, events=events, command_id=command_id)
        events.emit({"type": "tool.called", "name": "discover"})
        await _send_tool_result(
            params,
            {
                "ok": True,
                "status": "accepted",
                "deviceId": session.device_id,
                "requestId": command_id,
            },
            properties=FunctionCallResultProperties(is_final=False, run_llm=False),
            session=session,
            events=events,
            phase="accepted",
            started_at=started_at,
            command_id=command_id,
        )
        try:
            logger.info(
                "iris.voice.tool.device_command_start session={} device={} name=discover request_id={} timeout_secs=20.0",
                session.session_id,
                session.device_id,
                command_id,
            )
            result = await events.request_local_discovery(timeout=20.0, request_id=command_id)
            logger.info(
                "iris.voice.tool.called session={} device={} name=discover",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                {"ok": True, "deviceId": session.device_id, "discovery": result},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
                command_id=command_id,
            )
        except Exception as error:
            error_message = _tool_error_message(
                error,
                "Timed out waiting for local discovery results from the device",
            )
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=discover error={}",
                session.session_id,
                session.device_id,
                error_message,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": error_message},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
                command_id=command_id,
            )

    async def search(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        query = params.arguments.get("query")
        from_time = params.arguments.get("from")
        to_time = params.arguments.get("to")
        raw_limit = params.arguments.get("limit")
        limit = int(raw_limit) if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool) else 8
        events.emit({"type": "tool.called", "name": "search"})
        try:
            result = await search_transcripts(
                session,
                query=query if isinstance(query, str) else None,
                from_time=from_time if isinstance(from_time, str) else None,
                to_time=to_time if isinstance(to_time, str) else None,
                current_device_only=True,
                limit=limit,
            )
            logger.info(
                "iris.voice.tool.called session={} device={} name=search",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                result,
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
            )
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=search",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": str(error)},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
            )

    async def screen_vision(params: FunctionCallParams) -> None:
        raw_question = params.arguments.get("question")
        question = raw_question.replace("\n", " ").strip() if isinstance(raw_question, str) else ""
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            display=params.arguments.get("display") or "main",
        )
        events.emit({"type": "tool.called", "name": "screen_vision"})
        if not question:
            await _send_tool_result(
                params,
                {"ok": False, "error": "screen_vision requires a visual question"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
            )
            return
        try:
            permission = await request_native_permission("screen-capture")
            if permission.get("ok") is not True:
                await _send_tool_result(
                    params,
                    {
                        "ok": False,
                        "error": str(permission.get("error") or "screen capture permission is not granted"),
                        "permission": permission,
                        "requiresScreenRecordingPermission": True,
                    },
                    properties=FunctionCallResultProperties(run_llm=True),
                    session=session,
                    events=events,
                    phase="permission",
                    started_at=started_at,
                )
                return
            image = await _capture_main_screen_jpeg()
            await params.context.add_image_frame_message(
                format="image/jpeg",
                size=(0, 0),
                image=image,
                text=(
                    "Answer the user's visual question from this current Mac screenshot. "
                    "Use only what is visible in the image unless the question requires a clearly marked "
                    "inference. Visual question: "
                    f"{question}"
                ),
            )
            result = {
                "ok": True,
                "source": "mac_screen",
                "display": "main",
                "imageMimeType": "image/jpeg",
                "imageBytes": len(image),
                "question": question,
            }
            logger.info(
                "iris.voice.tool.called session={} device={} name=screen_vision image_bytes={} question={}",
                session.session_id,
                session.device_id,
                len(image),
                _truncate_log_text(question),
            )
            await _send_tool_result(
                params,
                result,
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
            )
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=screen_vision",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": str(error), "requiresScreenRecordingPermission": True},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
            )

    async def camera_vision(params: FunctionCallParams) -> None:
        raw_question = params.arguments.get("question")
        question = raw_question.replace("\n", " ").strip() if isinstance(raw_question, str) else ""
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            camera=params.arguments.get("camera") or "default",
        )
        events.emit({"type": "tool.called", "name": "camera_vision"})
        if not question:
            await _send_tool_result(
                params,
                {"ok": False, "error": "camera_vision requires a visual question"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
            )
            return
        try:
            permission = await request_native_permission("camera")
            if permission.get("ok") is not True:
                await _send_tool_result(
                    params,
                    {
                        "ok": False,
                        "error": str(permission.get("error") or "camera permission is not granted"),
                        "permission": permission,
                        "requiresCameraPermission": True,
                    },
                    properties=FunctionCallResultProperties(run_llm=True),
                    session=session,
                    events=events,
                    phase="permission",
                    started_at=started_at,
                )
                return
            image = await _capture_default_camera_jpeg()
            await params.context.add_image_frame_message(
                format="image/jpeg",
                size=(0, 0),
                image=image,
                text=(
                    "Answer the user's visual question from this current Mac camera frame. "
                    "Use only what is visible in the image unless the question requires a clearly marked "
                    "inference. Camera-view question: "
                    f"{question}"
                ),
            )
            result = {
                "ok": True,
                "source": "mac_camera",
                "camera": "default",
                "imageMimeType": "image/jpeg",
                "imageBytes": len(image),
                "question": question,
            }
            logger.info(
                "iris.voice.tool.called session={} device={} name=camera_vision image_bytes={} question={}",
                session.session_id,
                session.device_id,
                len(image),
                _truncate_log_text(question),
            )
            await _send_tool_result(
                params,
                result,
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
            )
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=camera_vision",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": str(error), "requiresCameraPermission": True},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
            )

    async def memory(params: FunctionCallParams) -> None:
        started_at = _tool_started(params, session=session, events=events)
        action = _normalize_memory_action(params.arguments.get("action"))
        memory_id = params.arguments.get("memoryId")
        memory_id = memory_id.strip() if isinstance(memory_id, str) else ""
        raw_content = params.arguments.get("content")
        content = raw_content.replace("\n", " ").strip() if isinstance(raw_content, str) else ""
        raw_kind = params.arguments.get("kind")
        raw_confidence = params.arguments.get("confidence")
        kind = _normalize_memory_kind(raw_kind) if raw_kind is not None else None
        confidence = (
            _normalize_memory_confidence(raw_confidence) if raw_confidence is not None else None
        )
        raw_limit = params.arguments.get("limit")
        limit = (
            int(raw_limit)
            if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool)
            else 24
        )
        events.emit({"type": "tool.called", "name": "memory", "action": action, "kind": kind})
        if action == "save" and not content:
            await _send_tool_result(
                params,
                {"ok": False, "error": "memory save requires content"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
            )
            return
        if action in {"update", "delete"} and not memory_id:
            await _send_tool_result(
                params,
                {"ok": False, "error": f"memory {action} requires memoryId"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
            )
            return
        if action == "update" and content == "" and kind is None and confidence is None:
            await _send_tool_result(
                params,
                {"ok": False, "error": "memory update requires content, kind, or confidence"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
            )
            return
        try:
            if action == "list":
                result = await list_user_memories(session, limit=limit)
            elif action == "save":
                result = await save_user_memory(
                    session,
                    content=content,
                    kind=kind or "fact",
                    confidence=confidence or "high",
                )
            elif action == "update":
                result = await update_user_memory(
                    session,
                    memory_id=memory_id,
                    content=content or None,
                    kind=kind,
                    confidence=confidence,
                )
            else:
                result = await delete_user_memory(session, memory_id=memory_id)
            memory = result.get("memory") if isinstance(result, dict) else None
            tool_result: dict[str, Any] = {"ok": True}
            if isinstance(result, dict):
                tool_result.update(result)
            else:
                tool_result["result"] = result
            logger.info(
                "iris.voice.tool.called session={} device={} name=memory action={} kind={} confidence={} memory_id={}",
                session.session_id,
                session.device_id,
                action,
                kind,
                confidence,
                memory.get("id") if isinstance(memory, dict) else None,
            )
            await _send_tool_result(
                params,
                tool_result,
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
            )
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=memory action={}",
                session.session_id,
                session.device_id,
                action,
            )
            await _send_tool_result(
                params,
                {"ok": False, "error": str(error)},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
            )

    async def agent(params: FunctionCallParams) -> None:
        prompt = normalize_agent_prompt(params.arguments)
        explicit_action = normalize_explicit_agent_action(params.arguments.get("action"))
        action = normalize_agent_action(params.arguments.get("action"), prompt)
        agent_id = normalize_agent_id(params.arguments.get("agentId"))
        thread_id = normalize_agent_thread_id(params.arguments.get("threadId"))
        thread = normalize_agent_thread(params.arguments.get("thread"))
        context = normalize_agent_context(params.arguments.get("context"))
        delivery_context = "\n".join(
            part
            for part in (context, events.last_user_turn_context())
            if isinstance(part, str) and part.strip()
        )
        response_style = normalize_agent_response_style(params.arguments.get("responseStyle"))
        delivery = infer_agent_delivery(
            params.arguments.get("delivery"),
            prompt=prompt,
            context=delivery_context,
        )
        thinking = normalize_agent_thinking(
            params.arguments,
            default_effort="low" if action in {"start", "steer"} else None,
        )
        requested_wait_ms = normalize_agent_wait_ms(params.arguments.get("waitMs"))
        if requested_wait_ms is not None:
            wait_ms = requested_wait_ms
        elif action in {"start", "steer"}:
            wait_ms = 0
        else:
            wait_ms = 2500
        command_id = events.new_device_command_id()
        agent_transport = configured_agent_transport()
        started_at = _tool_started(
            params,
            session=session,
            events=events,
            command_id=command_id,
            action=action,
            agentId=agent_id,
            threadId=thread_id,
            thread=thread,
            responseStyle=response_style,
            delivery=delivery,
            thinking=thinking,
            waitMs=wait_ms,
            requestedWaitMs=requested_wait_ms if requested_wait_ms != wait_ms else None,
            bridge=agent_transport or "missing",
        )
        if action in {"start", "steer"} and not prompt:
            await _send_tool_result(
                params,
                {"ok": False, "error": f"agent action '{action}' requires prompt"},
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="validation_error",
                started_at=started_at,
                command_id=command_id,
            )
            return

        events.emit({
            "type": "tool.called",
            "name": "agent",
            "action": action,
            "agentId": agent_id,
            "threadId": thread_id,
        })
        await _send_tool_result(
            params,
            {
                "ok": True,
                "status": "accepted",
                "requestId": command_id,
                "action": action,
                "agentId": agent_id,
                "threadId": thread_id,
                "thread": thread,
                "delivery": delivery,
                "thinking": thinking,
                "waitMs": wait_ms,
                "requestedWaitMs": requested_wait_ms if requested_wait_ms != wait_ms else None,
                "bridge": agent_transport or "missing",
            },
            properties=FunctionCallResultProperties(is_final=False, run_llm=False),
            session=session,
            events=events,
            phase="accepted",
            started_at=started_at,
            command_id=command_id,
        )
        try:
            if not agent_transport:
                raise RuntimeError("The local Codex bridge is not running in the Iris Mac app")
            logger.info(
                "iris.voice.tool.agent_bridge_start session={} device={} request_id={} bridge={} action={} agent_id={} thread_id={} thread={} prompt_chars={} context_chars={} response_style={} delivery={} thinking={} wait_ms={} requested_wait_ms={}",
                session.session_id,
                session.device_id,
                command_id,
                agent_transport,
                explicit_action or action,
                agent_id,
                thread_id,
                thread,
                len(prompt or ""),
                len(context or ""),
                response_style,
                delivery,
                thinking,
                wait_ms,
                requested_wait_ms,
            )
            result = await post_agent_bridge(
                session,
                agent_id=agent_id,
                thread_id=thread_id,
                thread=thread,
                action=explicit_action,
                prompt=prompt,
                context=context,
                response_style=response_style,
                delivery=delivery,
                wait_ms=wait_ms,
                thinking=thinking,
            )
            logger.info(
                "iris.voice.tool.called session={} device={} name=agent bridge={}",
                session.session_id,
                session.device_id,
                agent_transport,
            )
            status = agent_result_status(result)
            waits_for_completion = agent_result_waits_for_completion(status)
            run_id = agent_result_run_id(result)
            final_result = {"ok": True, "requestId": command_id, "agent": result}
            if waits_for_completion:
                final_result["voice"] = {
                    "state": status or "running",
                    "completionExpected": True,
                    "ackOnly": True,
                    "ackTiming": "post_tool_start",
                    "ackStyle": "brief_request_specific",
                    "task": {
                        "action": explicit_action or action,
                        "prompt": prompt,
                        "thread": thread,
                        "responseStyle": response_style,
                    },
                }
                if run_id:
                    events.register_agent_tool_result(
                        run_id=run_id,
                        tool_call_id=params.tool_call_id,
                        request_id=command_id,
                        result_callback=params.result_callback,
                    )
                    await _send_tool_result(
                        params,
                        final_result,
                        properties=FunctionCallResultProperties(
                            is_final=False,
                            run_llm=True,
                        ),
                        session=session,
                        events=events,
                        phase="desktop_started",
                        started_at=started_at,
                        command_id=command_id,
                    )
                    return
            await _send_tool_result(
                params,
                final_result,
                properties=FunctionCallResultProperties(
                    run_llm=agent_result_should_run_llm(status=status, delivery=delivery)
                ),
                session=session,
                events=events,
                phase="final",
                started_at=started_at,
                command_id=command_id,
            )
        except Exception as error:
            logger.exception(
                "iris.voice.tool.failed session={} device={} name=agent",
                session.session_id,
                session.device_id,
            )
            await _send_tool_result(
                params,
                {
                    "ok": False,
                    "requestId": command_id,
                    "action": action,
                    "agentId": agent_id,
                    "threadId": thread_id,
                    "error": str(error),
                    "requiresDesktopApp": not agent_transport,
                },
                properties=FunctionCallResultProperties(run_llm=True),
                session=session,
                events=events,
                phase="error",
                started_at=started_at,
                command_id=command_id,
            )

    llm.register_function("noop", noop, timeout_secs=5.0)
    llm.register_function("status", status, timeout_secs=5.0)
    llm.register_function("command", command, timeout_secs=5.0)
    llm.register_function(
        "shell_exec",
        shell_exec,
        cancel_on_interruption=False,
        timeout_secs=15.0,
    )
    llm.register_function("end", end_chat, timeout_secs=5.0)
    llm.register_function(
        "volume",
        volume,
        cancel_on_interruption=False,
        timeout_secs=15.0,
    )
    llm.register_function(
        "light",
        light,
        cancel_on_interruption=False,
        timeout_secs=30.0,
    )
    llm.register_function(
        "discover",
        discover,
        cancel_on_interruption=False,
        timeout_secs=30.0,
    )
    llm.register_function(
        "search",
        search,
        cancel_on_interruption=False,
        timeout_secs=20.0,
    )
    llm.register_function(
        "screen_vision",
        screen_vision,
        cancel_on_interruption=False,
        timeout_secs=25.0,
    )
    llm.register_function(
        "camera_vision",
        camera_vision,
        cancel_on_interruption=False,
        timeout_secs=30.0,
    )
    llm.register_function(
        "memory",
        memory,
        cancel_on_interruption=False,
        timeout_secs=15.0,
    )
    llm.register_function(
        "agent",
        agent,
        cancel_on_interruption=False,
        timeout_secs=40.0,
    )
