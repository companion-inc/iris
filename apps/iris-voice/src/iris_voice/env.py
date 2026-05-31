from __future__ import annotations

import os
from typing import Any


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_int_env(name: str, fallback: int) -> int:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def optional_float_env(name: str, fallback: float) -> float:
    value = os.getenv(name)
    if not value:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def optional_bool_env(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def optional_list_env(name: str, fallback: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return fallback
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def merge_keyterms(*term_lists: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term_list in term_lists:
        for term in term_list:
            normalized = " ".join(term.split())
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(normalized)
    return terms


def debug_transcript_text(text: str) -> str:
    if optional_bool_env("IRIS_DEBUG_TRANSCRIPT_TEXT", True):
        return text
    return f"<{len(text)} chars>"
