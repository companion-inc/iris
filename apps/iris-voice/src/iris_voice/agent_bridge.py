from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .session import VoiceSessionContext

DEFAULT_AGENT_TIMEOUT_SECONDS = 40.0

AGENT_ACTIONS = {"start", "steer", "interrupt", "status"}
CODEX_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
CODEX_REASONING_SUMMARIES = {"auto", "concise", "detailed", "none"}


def normalize_agent_prompt(arguments: dict[str, Any]) -> str | None:
    for key in ("prompt", "question", "query", "task"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_agent_action(value: Any, prompt: str | None) -> str:
    action = normalize_explicit_agent_action(value)
    if action:
        return action
    return "start" if prompt else "status"


def normalize_explicit_agent_action(value: Any) -> str | None:
    if isinstance(value, str):
        action = value.strip().lower()
        if action in AGENT_ACTIONS:
            return action
    return None


def normalize_agent_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    agent_id = value.strip()
    return agent_id or None


def normalize_agent_thread_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    thread_id = value.strip()
    return thread_id or None


def normalize_agent_thread(value: Any) -> str:
    if not isinstance(value, str):
        return "auto"
    thread = value.strip().lower()
    return thread if thread in {"auto", "same", "new"} else "auto"


def normalize_agent_context(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    context = value.strip()
    return context or None


def normalize_agent_response_style(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    style = value.strip().lower()
    return style if style in {"brief", "normal", "detailed"} else None


def normalize_agent_delivery(value: Any) -> str:
    if not isinstance(value, str):
        return "auto"
    delivery = value.strip().lower()
    return delivery if delivery in {"auto", "speak", "save", "silent"} else "auto"


def infer_agent_delivery(value: Any, *, prompt: str | None, context: str | None = None) -> str:
    delivery = normalize_agent_delivery(value)
    text = f"{prompt or ''} {context or ''}".lower()
    if user_requested_silent_delivery(text):
        return "silent"
    if user_requested_spoken_delivery(text):
        return "speak"
    if delivery != "auto":
        return delivery
    if user_requested_saved_delivery(text):
        return "save"
    return "speak"


def user_requested_spoken_delivery(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "tell me when",
            "tell me after",
            "tell me once",
            "let me know",
            "notify me",
            "say when",
            "report back",
            "when it is done",
            "when it's done",
            "when they are done",
            "when they're done",
            "when all done",
            "when it finishes",
            "when it finished",
            "when they finish",
            "when they finished",
            "when finished",
            "when done",
        )
    )


def user_requested_silent_delivery(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "don't say",
            "do not say",
            "don't speak",
            "do not speak",
            "stay silent",
            "silently",
            "no speech",
            "without telling me",
        )
    )


def user_requested_saved_delivery(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "in the background",
            "background task",
            "just save",
            "save it",
            "save the result",
            "don't interrupt",
            "do not interrupt",
        )
    )


def normalize_agent_wait_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        wait_ms = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            wait_ms = int(float(value.strip()))
        except ValueError:
            return None
    else:
        return None
    return max(0, min(wait_ms, 35000))


def normalize_agent_reasoning_value(value: Any, allowed: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in allowed else None


def normalize_agent_thinking(arguments: dict[str, Any], *, default_effort: str | None = None) -> dict[str, str] | None:
    thinking = arguments.get("thinking")
    if not isinstance(thinking, dict):
        thinking = arguments.get("reasoning")
    if not isinstance(thinking, dict):
        thinking = {}
    effort = normalize_agent_reasoning_value(
        thinking.get("effort")
        if thinking.get("effort") is not None
        else arguments.get("thinkingEffort") or arguments.get("reasoningEffort") or arguments.get("effort"),
        CODEX_REASONING_EFFORTS,
    )
    if not effort and default_effort:
        effort = normalize_agent_reasoning_value(default_effort, CODEX_REASONING_EFFORTS)
    summary = normalize_agent_reasoning_value(
        thinking.get("summary")
        if thinking.get("summary") is not None
        else arguments.get("thinkingSummary") or arguments.get("reasoningSummary"),
        CODEX_REASONING_SUMMARIES,
    )
    result = {
        **({"effort": effort} if effort else {}),
        **({"summary": summary} if summary else {}),
    }
    return result or None


def configured_agent_bridge_url() -> str | None:
    value = os.getenv("IRIS_AGENT_BRIDGE_URL") or os.getenv("IRIS_AGENT_URL")
    if not value or not value.strip():
        return None
    return normalize_agent_bridge_url(value.strip())


def configured_agent_transport() -> str | None:
    if configured_agent_bridge_url():
        return "bridge"
    if os.getenv("IRIS_API_URL", "").strip():
        return "api"
    return None


def normalize_agent_bridge_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Iris agent bridge URL must start with http:// or https://")
    if parsed.path in {"", "/"}:
        return urllib.parse.urlunparse(parsed._replace(path="/agent"))
    return value


async def post_agent_bridge(
    session: VoiceSessionContext,
    *,
    agent_id: str | None,
    thread_id: str | None,
    thread: str,
    action: str | None,
    prompt: str | None,
    context: str | None,
    response_style: str | None,
    delivery: str,
    wait_ms: int | None,
    thinking: dict[str, str] | None,
) -> dict[str, Any]:
    bridge_url = configured_agent_bridge_url()
    body: dict[str, Any] = {
        "iris": {
            "deviceId": session.device_id,
            "sessionId": session.session_id,
            "userId": session.user_id,
            "organizationId": session.organization_id,
            "source": session.source,
        },
    }
    if action:
        body["action"] = action
    if agent_id:
        body["agentId"] = agent_id
    if thread_id:
        body["threadId"] = thread_id
    if thread:
        body["thread"] = thread
    if prompt:
        body["prompt"] = prompt
    if context:
        body["context"] = context
    if response_style:
        body["responseStyle"] = response_style
    if delivery:
        body["delivery"] = delivery
    if wait_ms is not None:
        body["waitMs"] = wait_ms
    if thinking:
        body["thinking"] = thinking

    if bridge_url:
        token = os.getenv("IRIS_AGENT_BRIDGE_TOKEN") or os.getenv("IRIS_AGENT_TOKEN")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "IrisVoice/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await asyncio.to_thread(
            lambda: post_json(
                bridge_url,
                body=body,
                headers=headers,
                timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
                error_prefix="Iris agent bridge",
            )
        )

    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("The local Codex bridge is not running in the Iris Mac app")
    session_id = urllib.parse.quote(session.session_id, safe="")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "IrisVoice/1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = await asyncio.to_thread(
        lambda: post_json(
            f"{api_url}/v1/voice/sessions/{session_id}/agent",
            body=body,
            headers=headers,
            timeout=DEFAULT_AGENT_TIMEOUT_SECONDS,
            error_prefix="Iris agent API",
        )
    )
    return payload if isinstance(payload, dict) else {}


def post_json(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    error_prefix: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{error_prefix} failed with HTTP {error.code}: {detail}") from error
    return payload if isinstance(payload, dict) else {"ok": False, "result": payload}
