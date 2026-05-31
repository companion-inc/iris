#!/usr/bin/env python3
"""BLE provisioning server for Iris devices.

Local setup clients use this only for setup:
- discover an unpaired Iris device
- optionally scan nearby Wi-Fi networks
- send Wi-Fi credentials + Iris device pairing token

After provisioning, the device uses Wi-Fi, the Iris HTTP API, and the Iris voice
runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from typing import Any, TYPE_CHECKING

from bless import BlessGATTCharacteristic, BlessServer
from bless.backends.characteristic import GATTAttributePermissions, GATTCharacteristicProperties

from device_client import (
    ENV_FILE,
    TOKEN_FILE,
    USER_AGENT,
    api_request,
    config,
    delete_token,
    device_serial,
    write_identity,
)

if TYPE_CHECKING:
    from device_client import Config


IRIS_BLE_NAMESPACE = "ai.companion.iris.device"


def device_ble_name(path: str) -> str:
    return f"{IRIS_BLE_NAMESPACE}/{path}"


DEVICE_SETUP_SERVICE_NAME = device_ble_name("setup")
DEVICE_INFO_CHAR_NAME = device_ble_name("setup/device-info")
DEVICE_STATUS_CHAR_NAME = device_ble_name("setup/status")
DEVICE_WIFI_SCAN_CHAR_NAME = device_ble_name("setup/wifi-scan")
DEVICE_PROVISION_CHAR_NAME = device_ble_name("setup/provision")
DEVICE_RESET_CHAR_NAME = device_ble_name("setup/reset")


def device_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


DEVICE_SETUP_SERVICE_UUID = device_uuid(DEVICE_SETUP_SERVICE_NAME)
DEVICE_INFO_CHAR_UUID = device_uuid(DEVICE_INFO_CHAR_NAME)
DEVICE_STATUS_CHAR_UUID = device_uuid(DEVICE_STATUS_CHAR_NAME)
DEVICE_WIFI_SCAN_CHAR_UUID = device_uuid(DEVICE_WIFI_SCAN_CHAR_NAME)
DEVICE_PROVISION_CHAR_UUID = device_uuid(DEVICE_PROVISION_CHAR_NAME)
DEVICE_RESET_CHAR_UUID = device_uuid(DEVICE_RESET_CHAR_NAME)
def env_update(values: dict[str, str], *, remove: set[str] | None = None) -> None:
    remove = remove or set()
    lines: list[str] = []
    seen: set[str] = set()
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text().splitlines()

    next_lines: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in remove:
            continue
        if key in values:
            next_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            next_lines.append(line)

    for key, value in values.items():
        if key not in seen:
            next_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(next_lines) + "\n")
    ENV_FILE.chmod(0o600)
    for key, value in values.items():
        os.environ[key] = value
    for key in remove:
        os.environ.pop(key, None)


def clear_env_key(key_to_clear: str) -> None:
    if not ENV_FILE.exists():
        return
    lines: list[str] = []
    for line in ENV_FILE.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
        lines.append(f"{key_to_clear}=" if key == key_to_clear else line)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def current_wifi_ssid() -> str | None:
    result = run_command(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], 10)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        active, ssid = (line.split(":", 1) + [""])[:2]
        if active.strip() == "yes" and ssid:
            return ssid
    return None


def join_wifi(ssid: str, password: str) -> subprocess.CompletedProcess[str]:
    command = ["sudo", "-n", "nmcli", "dev", "wifi", "connect", ssid]
    if password:
        command.extend(["password", password])
    result = run_command(command, 60)
    if result.returncode == 0:
        return result
    # Some environments (netplan-owned wlan0) refuse the raw connect. Try
    # creating/replacing an explicit connection profile as a fallback.
    fallback = ["sudo", "-n", "nmcli", "connection", "up", ssid]
    alt = run_command(fallback, 30)
    if alt.returncode == 0:
        return alt
    return result


def backend_reachable(cfg: "Config") -> bool:
    # The API intentionally returns 4xx for unauthenticated probes; any HTTP
    # response below 500 still proves DNS, TLS, and routing from the device.
    try:
        request = urllib.request.Request(
            f"{cfg.api_url}/v1/devices",
            method="GET",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except Exception:
        return False


class DeviceSetupServer:
    def __init__(self) -> None:
        serial = device_serial()
        self.device_name = os.environ.get("IRIS_DEVICE_SETUP_NAME", "Device 1")
        self.server: BlessServer | None = None
        self.scan_task: asyncio.Task[None] | None = None
        self.status: dict[str, Any] = {
            "state": "ready",
            "message": "Ready for setup",
        }
        self.info = {
            "name": self.device_name,
            "serial": serial,
            "paired": TOKEN_FILE.exists(),
            "apiUrl": config().api_url,
        }

    async def set_status(self, state: str, message: str | None = None, **extra: Any) -> None:
        self.status = {"state": state, **({"message": message} if message else {}), **extra}
        print(f"[device-setup] status {self.status}", flush=True)
        await self.notify(DEVICE_STATUS_CHAR_UUID, self.status)

    async def notify(self, characteristic_uuid: str, value: Any) -> None:
        if not self.server:
            return
        print(f"[device-setup] notify {characteristic_uuid} {value}", flush=True)
        char = self.server.get_characteristic(characteristic_uuid)
        char.value = bytearray(json.dumps(value).encode("utf-8"))
        self.server.update_value(DEVICE_SETUP_SERVICE_UUID, characteristic_uuid)

    def read_request(self, characteristic: BlessGATTCharacteristic) -> bytearray:
        uuid = str(characteristic.uuid).lower()
        print(f"[device-setup] read {uuid}", flush=True)
        if uuid == DEVICE_INFO_CHAR_UUID.lower():
            return bytearray(json.dumps(self.info).encode("utf-8"))
        if uuid == DEVICE_STATUS_CHAR_UUID.lower():
            return bytearray(json.dumps(self.status).encode("utf-8"))
        return bytearray(json.dumps({"state": "ok"}).encode("utf-8"))

    def write_request(self, characteristic: BlessGATTCharacteristic, value: Any) -> int:
        uuid = str(characteristic.uuid).lower()
        print(f"[device-setup] write {uuid} bytes={len(bytes(value))}", flush=True)
        if uuid == DEVICE_WIFI_SCAN_CHAR_UUID.lower():
            if self.scan_task and not self.scan_task.done():
                self.scan_task.cancel()
            self.scan_task = asyncio.create_task(self.send_wifi_scan())
            return 0
        if uuid == DEVICE_PROVISION_CHAR_UUID.lower():
            asyncio.create_task(self.provision(bytes(value)))
            return 0
        if uuid == DEVICE_RESET_CHAR_UUID.lower():
            asyncio.create_task(self.reset())
            return 0
        return 1

    async def send_wifi_scan(self) -> None:
        print("[device-setup] wifi scan requested", flush=True)
        await self.notify(DEVICE_WIFI_SCAN_CHAR_UUID, {"status": "scanning"})
        networks = await asyncio.to_thread(self.scan_wifi_networks)
        print(f"[device-setup] wifi scan found {len(networks)} networks", flush=True)
        for index, network in enumerate(networks, 1):
            await self.notify(
                DEVICE_WIFI_SCAN_CHAR_UUID,
                {
                    "status": "network",
                    "index": index,
                    "total": len(networks),
                    "network": network,
                },
            )
            await asyncio.sleep(0.12)
        await self.notify(DEVICE_WIFI_SCAN_CHAR_UUID, {"status": "complete", "total": len(networks)})

    def scan_wifi_networks(self) -> list[dict[str, Any]]:
        result = run_command(["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,SECURITY", "dev", "wifi", "list"], 15)
        if result.returncode != 0:
            print(
                f"[device-setup] nmcli wifi list failed rc={result.returncode} stderr={result.stderr.strip()}",
                flush=True,
            )
            return []
        networks: dict[str, dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            ssid, bssid, signal, security = parts[0], ":".join(parts[1:7]), parts[-2], parts[-1]
            if not ssid or ssid in networks:
                continue
            try:
                signal_value = int(signal)
            except ValueError:
                signal_value = None
            networks[ssid] = {
                "ssid": ssid,
                "bssid": bssid,
                "signal": signal_value,
                "security": security or "Open",
            }
        return sorted(networks.values(), key=lambda item: item.get("signal") or 0, reverse=True)[:20]

    async def provision(self, raw: bytes) -> None:
        try:
            print(f"[device-setup] provision payload bytes={len(raw)}", flush=True)
            payload = json.loads(raw.decode("utf-8"))
            wifi = payload.get("wifi") or {}
            ssid = str(wifi.get("ssid") or "").strip()
            password = str(wifi.get("password") or "")
            pairing_token = str(payload.get("token") or "").strip()
            print(
                f"[device-setup] provision start ssid={ssid!r} token_present={bool(pairing_token)}",
                flush=True,
            )

            api_url = str(payload.get("apiUrl") or config().api_url).strip().rstrip("/")
            if not api_url:
                await self.set_status("error", "Missing Iris API URL")
                return
            env_update({"IRIS_API_URL": api_url})

            # Reload cfg so api_url change above is picked up immediately.
            cfg = config()

            already_online = False
            if ssid and current_wifi_ssid() == ssid and backend_reachable(cfg):
                already_online = True
                await self.set_status(
                    "wifi_ready",
                    f"Already connected to {ssid}",
                    ssid=ssid,
                )

            if ssid and not already_online:
                await self.set_status("connecting_wifi", f"Connecting to {ssid}", ssid=ssid)
                result = await asyncio.to_thread(join_wifi, ssid, password)
                print(
                    f"[device-setup] join wifi rc={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}",
                    flush=True,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                    await self.set_status(
                        "error",
                        f"Unable to join {ssid}: {detail[0] if detail else 'nmcli failed'}",
                    )
                    return
                await self.set_status("wifi_ready", f"Joined {ssid}", ssid=ssid)

            if not backend_reachable(cfg):
                await self.set_status(
                    "error",
                    f"Wi-Fi joined but {cfg.api_url} is unreachable",
                )
                return

            if not pairing_token:
                if not TOKEN_FILE.exists():
                    await self.set_status(
                        "error",
                        "Wi-Fi updated, but this device is not paired. Remove it in the app and set it up again.",
                    )
                    return
                self.info["paired"] = True
                await self.set_status("paired", "Wi-Fi updated")
                subprocess.run(["systemctl", "restart", "iris-device"], check=False)
                return

            await self.set_status("claiming_pairing_token", "Pairing with Iris")
            body: dict[str, Any] = {
                "token": pairing_token,
                "serial": device_serial(),
            }
            if cfg.firmware_version:
                body["firmware"] = cfg.firmware_version
            response = await asyncio.to_thread(api_request, cfg, "POST", "/v1/devices", body)
            device = response["device"]
            token = str(response["token"])
            write_identity(str(device["id"]), token)
            self.info["paired"] = True
            await self.set_status(
                "paired",
                "Device paired",
                deviceId=device["id"],
                deviceName=device.get("name"),
            )
            subprocess.run(["systemctl", "restart", "iris-device"], check=False)
        except Exception as error:
            print(f"[device-setup] provision failed: {error}", flush=True)
            await self.set_status("error", str(error))

    async def reset(self) -> None:
        print("[device-setup] reset requested", flush=True)
        delete_token()
        clear_env_key("IRIS_DEVICE_TOKEN")
        
        self.info["paired"] = False
        await self.set_status("reset", "Device reset. Ready to pair again.")
        subprocess.run(["systemctl", "stop", "iris-device"], check=False)

    async def run(self) -> None:
        self.server = BlessServer(name=self.device_name, name_overwrite=True)
        self.server.read_request_func = self.read_request
        self.server.write_request_func = self.write_request

        await self.server.add_new_service(DEVICE_SETUP_SERVICE_UUID)
        await self.server.add_new_characteristic(
            DEVICE_SETUP_SERVICE_UUID,
            DEVICE_INFO_CHAR_UUID,
            GATTCharacteristicProperties.read,
            bytearray(json.dumps(self.info).encode("utf-8")),
            GATTAttributePermissions.readable,
        )
        await self.server.add_new_characteristic(
            DEVICE_SETUP_SERVICE_UUID,
            DEVICE_STATUS_CHAR_UUID,
            GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
            bytearray(json.dumps(self.status).encode("utf-8")),
            GATTAttributePermissions.readable,
        )
        await self.server.add_new_characteristic(
            DEVICE_SETUP_SERVICE_UUID,
            DEVICE_WIFI_SCAN_CHAR_UUID,
            GATTCharacteristicProperties.write | GATTCharacteristicProperties.notify,
            bytearray(),
            GATTAttributePermissions.writeable,
        )
        await self.server.add_new_characteristic(
            DEVICE_SETUP_SERVICE_UUID,
            DEVICE_PROVISION_CHAR_UUID,
            GATTCharacteristicProperties.write,
            bytearray(),
            GATTAttributePermissions.writeable,
        )
        await self.server.add_new_characteristic(
            DEVICE_SETUP_SERVICE_UUID,
            DEVICE_RESET_CHAR_UUID,
            GATTCharacteristicProperties.write,
            bytearray(),
            GATTAttributePermissions.writeable,
        )

        await self.server.start()
        print(
            "[device-setup] advertising",
            {
                "name": self.device_name,
                "service": DEVICE_SETUP_SERVICE_UUID,
                "info": DEVICE_INFO_CHAR_UUID,
                "status": DEVICE_STATUS_CHAR_UUID,
                "wifi_scan": DEVICE_WIFI_SCAN_CHAR_UUID,
                "provision": DEVICE_PROVISION_CHAR_UUID,
                "reset": DEVICE_RESET_CHAR_UUID,
            },
            flush=True,
        )
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await self.server.stop()


async def main() -> None:
    server = DeviceSetupServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
