from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from loguru import logger

from .session import VoiceSessionContext
from .transcript_types import TranscriptWord


def auth_headers() -> dict[str, str]:
    api_key = os.getenv("IRIS_API_KEY")
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def fetch_session_config(session: VoiceSessionContext) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        return {"keyterms": [], "llm": {}}
    session_id = urllib.parse.quote(session.session_id, safe="")

    def get() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/config",
            method="GET",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {"keyterms": [], "llm": {}}

    try:
        return await asyncio.to_thread(get)
    except urllib.error.HTTPError as error:
        logger.warning("iris.voice.config_http_failed status={}", error.code)
    except Exception:
        logger.exception("iris.voice.config_fetch_failed")
    return {"keyterms": [], "llm": {}}


async def fetch_session_event_token(session: VoiceSessionContext) -> dict[str, Any] | None:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if os.getenv("IRIS_ENABLE_HOSTED_EVENTS", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    if not api_url:
        return None
    session_id = urllib.parse.quote(session.session_id, safe="")

    def post() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/events/token",
            data=b"{}",
            method="POST",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    try:
        return await asyncio.to_thread(post)
    except urllib.error.HTTPError as error:
        logger.warning("iris.voice.events_token_http_failed status={}", error.code)
    except Exception:
        logger.exception("iris.voice.events_token_failed")
    return None


async def list_session_agent_completions(
    session: VoiceSessionContext,
    *,
    after: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        return []
    session_id = urllib.parse.quote(session.session_id, safe="")
    params = {"limit": str(max(1, min(limit, 50)))}
    if after:
        params["after"] = after
    url = f"{api_url}/v1/voice/sessions/{session_id}/agent/completions?{urllib.parse.urlencode(params)}"

    def get() -> list[dict[str, Any]]:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        completions = body.get("completions") if isinstance(body, dict) else None
        return [item for item in completions if isinstance(item, dict)] if isinstance(completions, list) else []

    try:
        return await asyncio.to_thread(get)
    except urllib.error.HTTPError as error:
        logger.warning("iris.voice.agent_completions_http_failed status={}", error.code)
    except Exception:
        logger.exception("iris.voice.agent_completions_failed")
    return []


async def mark_session_agent_completion_delivered(
    session: VoiceSessionContext,
    completion_id: str,
) -> None:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url or not completion_id:
        return
    session_id = urllib.parse.quote(session.session_id, safe="")
    encoded_completion_id = urllib.parse.quote(completion_id, safe="")

    def post() -> None:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/agent/completions/{encoded_completion_id}/delivered",
            data=b"{}",
            method="POST",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10):
            return

    try:
        await asyncio.to_thread(post)
    except urllib.error.HTTPError as error:
        logger.warning("iris.voice.agent_completion_delivered_http_failed status={}", error.code)
    except Exception:
        logger.exception("iris.voice.agent_completion_delivered_failed")


async def post_session_volume(
    session: VoiceSessionContext,
    *,
    action: str,
    volume: int | None = None,
    sync_device: bool = True,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for device volume control")
    session_id = urllib.parse.quote(session.session_id, safe="")
    payload: dict[str, Any] = {"action": action}
    if volume is not None:
        payload["volume"] = max(0, min(100, int(round(volume))))
    payload["syncDevice"] = sync_device

    def post() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/device-volume",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(post)


async def post_session_light(
    session: VoiceSessionContext,
    *,
    effect: str | None = None,
    color: str | None = None,
    brightness: int | None = None,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for device light control")
    session_id = urllib.parse.quote(session.session_id, safe="")
    payload: dict[str, Any] = {}
    if effect is not None:
        payload["effect"] = effect
    if color is not None:
        payload["color"] = color
    if brightness is not None:
        payload["brightness"] = max(0, min(255, int(round(brightness))))

    def post() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/device-light",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(post)


async def search_transcripts(
    session: VoiceSessionContext,
    *,
    query: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    current_device_only: bool = False,
    limit: int = 8,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for transcript search")

    params = {
        "userId": session.user_id,
        "organizationId": session.organization_id,
        "limit": str(max(1, min(20, int(limit)))),
    }
    if query and query.strip():
        params["query"] = query.strip()
    if from_time and from_time.strip():
        params["from"] = from_time.strip()
    if to_time and to_time.strip():
        params["to"] = to_time.strip()
    if current_device_only:
        params["deviceId"] = session.device_id
    url = f"{api_url}/v1/transcripts/search?{urllib.parse.urlencode(params)}"

    def get() -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(get)


async def list_user_memories(
    session: VoiceSessionContext,
    *,
    limit: int = 24,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for memory storage")
    session_id = urllib.parse.quote(session.session_id, safe="")
    params = urllib.parse.urlencode({"limit": str(max(1, min(50, int(limit))))})

    def get() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/memories?{params}",
            method="GET",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(get)


async def save_user_memory(
    session: VoiceSessionContext,
    *,
    content: str,
    kind: str = "fact",
    confidence: str = "high",
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for memory storage")
    session_id = urllib.parse.quote(session.session_id, safe="")
    payload = {
        "content": content,
        "kind": kind,
        "confidence": confidence,
    }

    def post() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/memories",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(post)


async def update_user_memory(
    session: VoiceSessionContext,
    *,
    memory_id: str,
    content: str | None = None,
    kind: str | None = None,
    confidence: str | None = None,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for memory storage")
    session_id = urllib.parse.quote(session.session_id, safe="")
    encoded_memory_id = urllib.parse.quote(memory_id, safe="")
    payload: dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if kind is not None:
        payload["kind"] = kind
    if confidence is not None:
        payload["confidence"] = confidence

    def patch() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/memories/{encoded_memory_id}",
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(patch)


async def delete_user_memory(
    session: VoiceSessionContext,
    *,
    memory_id: str,
) -> dict[str, Any]:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        raise RuntimeError("Iris API is not configured for memory storage")
    session_id = urllib.parse.quote(session.session_id, safe="")
    encoded_memory_id = urllib.parse.quote(memory_id, safe="")

    def delete() -> dict[str, Any]:
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/memories/{encoded_memory_id}",
            data=b"{}",
            method="DELETE",
            headers={
                **auth_headers(),
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {}

    return await asyncio.to_thread(delete)


def llm_override(config: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return None, None, None
    base_url = llm.get("baseUrl")
    model = llm.get("model")
    api_key = llm.get("apiKey")
    return (
        base_url.strip() if isinstance(base_url, str) and base_url.strip() else None,
        model.strip() if isinstance(model, str) and model.strip() else None,
        api_key.strip() if isinstance(api_key, str) and api_key.strip() else None,
    )


async def post_transcript_event(
    session: VoiceSessionContext,
    *,
    text: str,
    is_final: bool,
    speaker: str | None,
    segment_id: str | None,
    words: list[TranscriptWord] | None,
    confidence: float | None,
    speaker_user_id: str | None = None,
    speaker_confidence: float | None = None,
    emotion_label: str | None = None,
    emotion_confidence: float | None = None,
    emotion_model: str | None = None,
    source: str = "device",
) -> None:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        return
    body: dict[str, Any] = {
        "userId": session.user_id,
        "organizationId": session.organization_id,
        "deviceId": session.device_id,
        "source": source,
        "sessionId": session.session_id,
        "transcript": text,
        "isFinal": is_final,
        "speakerId": speaker,
    }
    if speaker_user_id:
        body["speakerUserId"] = speaker_user_id
    if speaker_confidence is not None:
        body["speakerConfidence"] = speaker_confidence
    if segment_id:
        body["segmentId"] = segment_id
    if words:
        body["words"] = words
    if confidence is not None:
        body["confidence"] = confidence
    if emotion_label:
        body["emotionLabel"] = emotion_label
    if emotion_confidence is not None:
        body["emotionConfidence"] = emotion_confidence
    if emotion_model:
        body["emotionModel"] = emotion_model

    def post() -> None:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{api_url}/v1/transcripts/events",
            data=data,
            method="POST",
            headers={
                **auth_headers(),
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10):
            return

    try:
        await asyncio.to_thread(post)
    except urllib.error.HTTPError as error:
        logger.warning(
            "iris.voice.transcript_ingest_http_failed status={} final={} chars={}",
            error.code,
            is_final,
            len(text),
        )
    except Exception:
        logger.exception("iris.voice.transcript_ingest_failed")


async def post_sound_recognition_event(
    session: VoiceSessionContext,
    *,
    label: str,
    confidence: float | None,
    segment_id: str | None = None,
    text: str | None = None,
) -> None:
    display_text = text or f"{label.replace('_', ' ').strip().capitalize()} detected"
    await post_transcript_event(
        session,
        text=display_text,
        is_final=True,
        source="sound_recognition",
        speaker="SOUND",
        segment_id=segment_id,
        words=None,
        confidence=confidence,
    )


async def post_session_end(session: VoiceSessionContext, reason: str) -> None:
    api_url = os.getenv("IRIS_API_URL", "").rstrip("/")
    api_key = os.getenv("IRIS_API_KEY")
    if not api_url:
        return
    session_id = urllib.parse.quote(session.session_id, safe="")
    body = {"reason": reason}

    def post() -> None:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{api_url}/v1/voice/sessions/{session_id}/end",
            data=data,
            method="POST",
            headers={
                **auth_headers(),
                "Content-Type": "application/json",
                "User-Agent": "IrisVoice/1",
            },
        )
        with urllib.request.urlopen(request, timeout=10):
            return

    try:
        await asyncio.to_thread(post)
    except urllib.error.HTTPError as error:
        logger.warning(
            "iris.voice.session_end_http_failed status={} session={}",
            error.code,
            session.session_id,
        )
    except Exception:
        logger.exception("iris.voice.session_end_failed")
