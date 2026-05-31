from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from device_audio import command_output, set_speaker_volume, speaker_volume
from device_xvf import set_status_light, xvf_available


LogFn = Callable[..., None]


def coerce_speaker_volume(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0, min(100, int(round(value))))


def next_speaker_volume(current_volume: int | None, action: str, volume: int | None) -> int:
    current = current_volume if current_volume is not None else 50
    step = volume if volume is not None else 15
    if action == "set":
        return coerce_speaker_volume(volume) if volume is not None else current
    if action == "increase":
        return min(100, current + step)
    if action == "decrease":
        return max(0, current - step)
    if action == "mute":
        return 0
    return coerce_speaker_volume(volume) if volume is not None else 50


def current_wifi_ssid() -> str | None:
    ok, output = command_output(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    if not ok:
        return None
    for line in output.splitlines():
        active, ssid = (line.split(":", 1) + [""])[:2]
        if active == "yes" and ssid:
            return ssid
    return None


def ip_addresses() -> list[str]:
    ok, output = command_output(["hostname", "-I"])
    if not ok:
        return []
    return [part for part in output.split() if part]


def command_lines(command: list[str], limit: int = 40) -> list[str]:
    ok, output = command_output(command)
    if not ok:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()][:limit]


def discover_local_devices() -> dict[str, Any]:
    return {
        "wifiSsid": current_wifi_ssid(),
        "ipAddresses": ip_addresses(),
        "neighbors": command_lines(["ip", "neigh", "show"]),
        "arp": command_lines(["arp", "-an"]),
        "wifiNetworks": command_lines(["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,SECURITY", "dev", "wifi"], 30),
        "bluetoothDevices": command_lines(["bluetoothctl", "devices"], 30),
    }


def hardware_info(cfg: Any) -> dict[str, Any]:
    return {
        "wifiSsid": current_wifi_ssid(),
        "ipAddresses": ip_addresses(),
        "apiUrl": cfg.api_url,
        "version": cfg.firmware_version,
        "speakerVolume": speaker_volume(cfg),
        "xvfHostAvailable": xvf_available(),
    }


def apply_speaker_volume(cfg: Any, log: LogFn, volume: int | None, reason: str) -> dict[str, Any]:
    if volume is None:
        return {"ok": False, "error": "No valid volume provided"}
    if set_speaker_volume(cfg, volume):
        actual_volume = speaker_volume(cfg)
        persisted_volume = actual_volume if actual_volume is not None else volume
        log(
            "speaker_volume_set",
            requestedVolume=volume,
            volume=persisted_volume,
            reason=reason,
        )
        return {"ok": True, "volume": persisted_volume, "requestedVolume": volume}
    log("speaker_volume_failed", volume=volume, reason=reason)
    return {"ok": False, "volume": volume, "error": "Failed to set speaker volume"}


class DeviceControlState:
    def __init__(self, cfg: Any, log: LogFn) -> None:
        self._log = log
        self.listening_enabled = True
        self.speaker_volume = speaker_volume(cfg)
        self.wake_word = "iris"
        self.last_desired: dict[str, Any] = {
            "listeningEnabled": True,
            "speakerVolume": self.speaker_volume,
            "wakeWord": self.wake_word,
            "statusLight": None,
        }

    def apply_desired(self, cfg: Any, desired: dict[str, Any]) -> None:
        self.last_desired = {**self.last_desired, **desired}
        listening = desired.get("listeningEnabled")
        if isinstance(listening, bool):
            self.listening_enabled = listening
            self._log("desired_listening_applied", listeningEnabled=listening)

        volume = coerce_speaker_volume(desired.get("speakerVolume"))
        if volume is not None:
            result = apply_speaker_volume(cfg, self._log, volume, "local_settings")
            if result.get("ok"):
                self.speaker_volume = volume

        wake_word = desired.get("wakeWord")
        if isinstance(wake_word, str) and wake_word.strip():
            self.wake_word = wake_word.strip()

        status_light = desired.get("statusLight")
        if isinstance(status_light, dict):
            result = set_status_light(
                effect=status_light.get("effect"),
                color=status_light.get("color"),
                brightness=status_light.get("brightness"),
            )
            self._log("status_light_applied", reason="local_settings", **result)


class DeviceCommandHandler:
    def __init__(self, cfg: Any, log: LogFn, control: DeviceControlState) -> None:
        self._cfg = cfg
        self._log = log
        self._control = control

    def _record_speaker_volume(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        volume = coerce_speaker_volume(result.get("volume"))
        if volume is not None:
            self._control.speaker_volume = volume

    async def handle(self, payload: dict[str, Any], send_text: Callable[[str], Awaitable[None]]) -> bool:
        payload_type = payload.get("type")
        if payload_type == "device.volume.set":
            volume = coerce_speaker_volume(payload.get("volume"))
            result = await asyncio.to_thread(
                apply_speaker_volume,
                self._cfg,
                self._log,
                volume,
                "voice_direct",
            )
            self._record_speaker_volume(result)
            return True

        if payload_type == "device.volume.change":
            request_id = str(payload.get("requestId") or "")
            action = str(payload.get("action") or "set")
            volume = coerce_speaker_volume(payload.get("volume"))
            next_volume = next_speaker_volume(speaker_volume(self._cfg), action, volume)
            result = await asyncio.to_thread(
                apply_speaker_volume,
                self._cfg,
                self._log,
                next_volume,
                "voice_direct",
            )
            self._record_speaker_volume(result)
            if request_id:
                await send_text(
                    json.dumps(
                        {
                            "type": "device.volume.result",
                            "requestId": request_id,
                            "result": {**result, "volume": result.get("volume", next_volume), "action": action},
                        }
                    )
                )
            return True

        if payload_type == "device.light.set":
            result = await asyncio.to_thread(
                set_status_light,
                effect=payload.get("effect"),
                color=payload.get("color"),
                brightness=payload.get("brightness"),
            )
            self._log("status_light_applied", reason="voice_direct", **result)
            return True

        if payload_type == "device.light.change":
            request_id = str(payload.get("requestId") or "")
            result = await asyncio.to_thread(
                set_status_light,
                effect=payload.get("effect"),
                color=payload.get("color"),
                brightness=payload.get("brightness"),
            )
            self._log("status_light_applied", reason="voice_direct", **result)
            if request_id:
                await send_text(
                    json.dumps({"type": "device.light.result", "requestId": request_id, "result": result})
                )
            return True

        if payload_type == "device.discovery.request":
            request_id = str(payload.get("requestId") or "")
            result = await asyncio.to_thread(discover_local_devices)
            if request_id:
                await send_text(
                    json.dumps(
                        {
                            "type": "device.discovery.result",
                            "requestId": request_id,
                            "result": result,
                        }
                    )
                )
            self._log("local_discovery_completed", requestId=request_id)
            return True

        return False
