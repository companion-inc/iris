from __future__ import annotations

import json
from typing import Any


def developer_event_message(
    *,
    event_type: str,
    payload: dict[str, Any],
    instruction: str,
) -> dict[str, str]:
    return {
        "role": "developer",
        "content": json.dumps(
            {
                "type": "iris.internal_event",
                "event": event_type,
                "description": "Internal Iris runtime event. This is not user speech.",
                "instruction": instruction,
                "payload": payload,
            },
            ensure_ascii=False,
        ),
    }
