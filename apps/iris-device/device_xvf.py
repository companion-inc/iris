from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any


EFFECTS = {
    "off": 0,
    "breath": 1,
    "rainbow": 2,
    "solid": 3,
    "doa": 4,
}

AMP_ENABLE_GPO = 31
AMP_DISABLED = 1
AMP_ENABLED = 0
MIC_MUTE_GPO = 30
MIC_MUTED = 1
MIC_UNMUTED = 0


def xvf_host_path() -> str | None:
    return shutil.which("xvf_host")


def xvf_available() -> bool:
    return xvf_host_path() is not None


def run_xvf_command(*args: str) -> dict[str, Any]:
    host = xvf_host_path()
    if not host:
        return {"ok": False, "error": "xvf_host is not installed"}
    command = [host, *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
    output = (result.stdout or result.stderr or "").strip()
    return {
        "ok": result.returncode == 0,
        "command": " ".join(command),
        "output": output[-500:],
    }


def set_amp_enabled(enabled: bool) -> dict[str, Any]:
    value = AMP_ENABLED if enabled else AMP_DISABLED
    return run_xvf_command("GPO_WRITE_VALUE", str(AMP_ENABLE_GPO), str(value))


def set_mic_muted(muted: bool) -> dict[str, Any]:
    value = MIC_MUTED if muted else MIC_UNMUTED
    return run_xvf_command("GPO_WRITE_VALUE", str(MIC_MUTE_GPO), str(value))


def xvf_audio_state() -> dict[str, Any]:
    return {
        "available": xvf_available(),
        "gpo": run_xvf_command("GPO_READ_VALUES"),
        "i2sInactive": run_xvf_command("I2S_INACTIVE"),
        "dacDsp": run_xvf_command("I2S_DAC_DSP_ENABLE"),
    }


def normalize_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    color = value.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", color):
        return f"0x{color[1:]}"
    if re.fullmatch(r"0x[0-9a-f]{6}", color):
        return color
    if re.fullmatch(r"[0-9a-f]{6}", color):
        return f"0x{color}"
    return None


def normalize_effect(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    effect = value.strip().lower()
    return effect if effect in EFFECTS else None


def normalize_brightness(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0, min(255, int(round(value))))


def set_status_light(effect: Any = None, color: Any = None, brightness: Any = None) -> dict[str, Any]:
    host = xvf_host_path()
    if not host:
        return {"ok": False, "error": "xvf_host is not installed"}

    commands: list[list[str]] = []
    normalized_effect = normalize_effect(effect)
    normalized_color = normalize_color(color)
    normalized_brightness = normalize_brightness(brightness)

    if normalized_effect is not None:
        commands.append([host, "led_effect", str(EFFECTS[normalized_effect])])
    if normalized_color is not None:
        commands.append([host, "led_color", normalized_color])
    if normalized_brightness is not None:
        commands.append([host, "led_brightness", str(normalized_brightness)])
    if not commands:
        return {"ok": False, "error": "No valid light setting provided"}

    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            return {
                "ok": False,
                "error": (result.stderr or result.stdout or "xvf_host failed").strip()[-300:],
                "command": " ".join(command),
            }
    return {
        "ok": True,
        "effect": normalized_effect,
        "color": normalized_color,
        "brightness": normalized_brightness,
    }
