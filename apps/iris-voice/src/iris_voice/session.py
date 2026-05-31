from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from .env import required_env


@dataclass(frozen=True)
class VoiceSessionContext:
    session_id: str
    device_id: str
    user_id: str
    organization_id: str
    source: str
    sample_rate: int
    channels: int
    initial_awake: bool = False


def verify_session_token(token: str) -> VoiceSessionContext:
    secret = required_env("IRIS_TOKEN_SECRET").encode("utf-8")
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as error:
        raise RuntimeError("Invalid voice token") from error
    expected = hmac.new(secret, encoded.encode("utf-8"), hashlib.sha256).digest()
    actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("Invalid voice token signature")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if int(payload.get("exp") or 0) < int(time.time()):
        raise RuntimeError("Expired voice token")
    return VoiceSessionContext(
        session_id=str(payload["sessionId"]),
        device_id=str(payload["deviceId"]),
        user_id=str(payload["userId"]),
        organization_id=str(payload["organizationId"]),
        source=str(payload.get("source") or "device"),
        sample_rate=int(payload.get("sampleRate") or 16000),
        channels=int(payload.get("channels") or 1),
        initial_awake=bool(payload.get("initialAwake")),
    )
