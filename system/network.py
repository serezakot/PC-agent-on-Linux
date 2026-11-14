"""Сеть: Wi‑Fi, Bluetooth (вкл/выкл, подключение устройств), авиарежим, хотспот, VPN, перезапуск Wi‑Fi, IP-адрес."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .core import SystemCommandError, SystemPlan, _norm, _register, name_similarity_safe  # noqa: F401

# --- 4.4. Сеть, Bluetooth, Режим полёта (GNOME Rfkill DBus + NetworkManager) ---
@_register("wifi_on")
def _h_wifi_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="wifi_on",
        description="Включаю Wi-Fi",
        commands=(("nmcli", "radio", "wifi", "on"),),
        fallback_commands=(("rfkill", "unblock", "wifi"),),
    )


@_register("wifi_off")
def _h_wifi_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="wifi_off",
        description="Выключаю Wi-Fi",
        commands=(("nmcli", "radio", "wifi", "off"),),
        fallback_commands=(("rfkill", "block", "wifi"),),
    )


@_register("bt_on")
def _h_bt_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="bluetooth_on",
        description="Включаю Bluetooth",
        commands=(
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "BluetoothAirplaneMode", "<false>",
            ),
        ),
        fallback_commands=(("rfkill", "unblock", "bluetooth"), ("bluetoothctl", "power", "on")),
    )


@_register("bt_off")
def _h_bt_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="bluetooth_off",
        description="Выключаю Bluetooth",
        commands=(
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "BluetoothAirplaneMode", "<true>",
            ),
        ),
        fallback_commands=(("bluetoothctl", "power", "off"), ("rfkill", "block", "bluetooth")),
    )


@_register("bt_restart")
def _h_bt_restart(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="bluetooth_restart",
        description="Перезапускаю Bluetooth",
        commands=(
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "BluetoothAirplaneMode", "<true>",
            ),
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "BluetoothAirplaneMode", "<false>",
            ),
        ),
        fallback_commands=(("rfkill", "block", "bluetooth"), ("rfkill", "unblock", "bluetooth")),
    )


@_register("airplane_on")
def _h_airplane_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="airplane_on",
        description="Включаю режим полёта (отключаю беспроводные сети)",
        commands=(
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "AirplaneMode", "<true>",
            ),
        ),
        fallback_commands=(("nmcli", "radio", "all", "off"), ("rfkill", "block", "all")),
    )


@_register("airplane_off")
def _h_airplane_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="airplane_off",
        description="Выключаю режим полёта",
        commands=(
            (
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Rfkill",
                "--object-path", "/org/gnome/SettingsDaemon/Rfkill",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Rfkill", "AirplaneMode", "<false>",
            ),
        ),
        fallback_commands=(("rfkill", "unblock", "all"), ("nmcli", "radio", "all", "on")),
    )


@_register("hotspot_on")
def _h_hotspot_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="hotspot_on",
        description="Включаю точку доступа Wi-Fi (Hotspot)",
        commands=(("nmcli", "device", "wifi", "hotspot"),),
    )


@_register("hotspot_off")
def _h_hotspot_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="hotspot_off",
        description="Выключаю точку доступа Wi-Fi",
        commands=(("nmcli", "connection", "down", "Hotspot"),),
    )


@_register("vpn_toggle", "vpn_on", "vpn_off")
def _h_vpn(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="network",
        action="vpn_settings",
        description="Открываю настройки сети и VPN в Ubuntu",
        commands=(("gnome-control-center", "network"),),
    )


@_register("wifi_restart")
def _h_wifi_restart(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    def _op() -> str:
        subprocess.run(["nmcli", "radio", "wifi", "off"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5.0, check=False)
        time.sleep(1.5)
        res = subprocess.run(["nmcli", "radio", "wifi", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5.0, check=False)
        if res.returncode != 0:
            subprocess.run(["nmcli", "networking", "off"], timeout=5.0, check=False)
            subprocess.run(["nmcli", "networking", "on"], timeout=5.0, check=False)
        return "Wi-Fi перезапущен, подключаюсь заново"
    return SystemPlan(category="network", action="wifi_restart", description="Перезапускаю Wi-Fi",
                      commands=(("nmcli", "radio", "wifi", "off"), ("nmcli", "radio", "wifi", "on")), python_op=_op)


_BT_KIND = (
    (("наушник", "гарнитур", "airpods", "эирподс", "buds", "бадс"), ("headphone", "headset", "earbuds", "buds", "airpods", "wh-", "wf-", "freebuds", "jbl")),
    (("колонк", "speaker", "саундбар"), ("speaker", "flip", "charge", "boombox", "marshall", "jbl", "soundbar")),
    (("мыш", "mouse"), ("mouse", "mx ", "m7", "m5")),
    (("клавиатур", "keyboard"), ("keyboard", "k3", "k8", "mx keys")),
    (("геймпад", "джойстик", "контроллер", "dualshock", "дуалшок", "xbox"), ("controller", "wireless controller", "xbox", "dualshock", "dualsense", "gamepad")),
    (("час", "watch"), ("watch", "band")),
    (("телефон", "phone"), ("phone", "iphone", "galaxy", "pixel", "redmi", "xiaomi")),
)


_BT_BRANDS = {"эирподс": "airpods", "airpods": "airpods", "аирподс": "airpods", "джибиэль": "jbl", "jbl": "jbl", "флип": "flip", "flip": "flip",
              "самсунг": "galaxy", "galaxy": "galaxy", "сони": "sony", "sony": "sony", "xiaomi": "xiaomi", "сяоми": "xiaomi", "редми": "redmi",
              "хуавей": "huawei", "huawei": "huawei", "маршал": "marshall", "marshall": "marshall", "бадс": "buds", "buds": "buds", "логитек": "logi",
              "logitech": "logi", "дуалшок": "dualshock", "dualshock": "dualshock", "xbox": "xbox", "иксбокс": "xbox", "бозе": "bose", "bose": "bose"}


def _bt_devices() -> list[tuple[str, str]]:
    out = []
    for args in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "devices"], ["bluetoothctl", "paired-devices"]):
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=6.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in (res.stdout or "").splitlines():
            m = re.match(r"Device\s+([0-9A-F:]{17})\s+(.+)", line.strip())
            if m and (m.group(1), m.group(2)) not in out:
                out.append((m.group(1), m.group(2)))
        if out:
            break
    return out


def _bt_op(query: str, connect: bool) -> str:
    devices = _bt_devices()
    if not devices:
        if connect:
            subprocess.Popen(["gnome-control-center", "bluetooth"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "Сопряжённых Bluetooth-устройств нет: открыла настройки Bluetooth, чтобы добавить устройство"
        raise SystemCommandError("нет подключённых Bluetooth-устройств")
    q = _norm(query)
    techs: tuple[str, ...] = ()
    for spoken, t in _BT_KIND:
        if any(w in q for w in spoken):
            techs = t
            break
    brands = [b for spoken, b in _BT_BRANDS.items() if spoken in q]
    best = None
    for mac, name in devices:
        n = name.lower()
        score = sum(1 for t in techs if t in n) * 10 + int(round(name_similarity_safe(q, name) * 8))
        if brands:
            score += 25 if any(b in n for b in brands) else -15      # «эирподс» must not pick the JBL headphones
        if best is None or score > best[0]:
            best = (score, mac, name)
    if best is None or (best[0] <= 0 and len(devices) > 1):
        names = "; ".join(n for _, n in devices)
        raise SystemCommandError(f"не поняла, какое устройство «{query}». Сопряжены: {names}")
    _, mac, name = best
    subprocess.run(["bluetoothctl", "power", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5.0, check=False)
    res = subprocess.run(["bluetoothctl", "connect" if connect else "disconnect", mac], capture_output=True, text=True, timeout=25.0, check=False)
    ok = ("successful" in (res.stdout or "").lower()) or res.returncode == 0
    if not ok:
        raise SystemCommandError(f"не удалось {'подключить' if connect else 'отключить'} «{name}»: {(res.stdout or res.stderr).strip()[-160:]}")
    return f"{'Подключено' if connect else 'Отключено'}: {name}"


@_register("bt_connect")
def _h_bt_connect(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="network", action="bt_connect", description=f"Подключаю Bluetooth-устройство «{query}»",
                      commands=(("bluetoothctl", "connect", query),), python_op=lambda: _bt_op(query, True))


@_register("bt_disconnect")
def _h_bt_disconnect(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="network", action="bt_disconnect", description=f"Отключаю Bluetooth-устройство «{query}»",
                      commands=(("bluetoothctl", "disconnect", query),), python_op=lambda: _bt_op(query, False))
