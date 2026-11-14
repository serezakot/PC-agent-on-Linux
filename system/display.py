"""Экран: яркость, ночной свет, тема, масштаб, «не беспокоить», тачпад, автоповорот, разрешение (Mutter DisplayConfig),
выключение экрана, обои."""
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

from .core import SystemCommandError, SystemPlan, _adjust_brightness_op, _norm, _parse_percent, _register  # noqa: F401
from .files import _find_target_item  # noqa: F401

# --- 4.2. Яркость экрана, тема, ночной свет, масштаб, Не беспокоить ---
@_register("bright_set")
def _h_bright_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    pct = _parse_percent(query, default=50)
    return SystemPlan(
        category="display",
        action="brightness_set",
        description=f"Устанавливаю яркость экрана на {pct}%",
        commands=((
            "gdbus", "call", "--session",
            "--dest", "org.gnome.SettingsDaemon.Power",
            "--object-path", "/org/gnome/SettingsDaemon/Power",
            "--method", "org.freedesktop.DBus.Properties.Set",
            "org.gnome.SettingsDaemon.Power.Screen", "Brightness", f"<int32 {pct}>",
        ),),
        fallback_commands=(("brightnessctl", "set", f"{pct}%"),),
    )


@_register("bright_up")
def _h_bright_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="display",
        action="brightness_up",
        description=f"Прибавляю яркость экрана на {step}%",
        commands=((
            "gdbus", "call", "--session",
            "--dest", "org.gnome.SettingsDaemon.Power",
            "--object-path", "/org/gnome/SettingsDaemon/Power",
            "--method", "org.gnome.SettingsDaemon.Power.Screen.StepUp",
        ),),
        fallback_commands=(("brightnessctl", "set", f"+{step}%"),),
        python_op=lambda step=step: _adjust_brightness_op(+step),
    )


@_register("bright_down")
def _h_bright_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="display",
        action="brightness_down",
        description=f"Убавляю яркость экрана на {step}%",
        commands=((
            "gdbus", "call", "--session",
            "--dest", "org.gnome.SettingsDaemon.Power",
            "--object-path", "/org/gnome/SettingsDaemon/Power",
            "--method", "org.gnome.SettingsDaemon.Power.Screen.StepDown",
        ),),
        fallback_commands=(("brightnessctl", "set", f"{step}%-"),),
        python_op=lambda step=step: _adjust_brightness_op(-step),
    )


@_register("night_light_on", "night_on")
def _h_night_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="night_light_on",
        description="Включаю ночной режим экрана (Night Light)",
        commands=(("gsettings", "set", "org.gnome.settings-daemon.plugins.color", "night-light-enabled", "true"),),
    )


@_register("night_light_off", "night_off")
def _h_night_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="night_light_off",
        description="Выключаю ночной режим экрана",
        commands=(("gsettings", "set", "org.gnome.settings-daemon.plugins.color", "night-light-enabled", "false"),),
    )


@_register("theme_dark", "dark_on")
def _h_dark_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="dark_theme",
        description="Включаю тёмную тему Ubuntu (Yaru-dark / prefer-dark)",
        commands=(
            ("gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark"),
            ("gsettings", "set", "org.gnome.desktop.interface", "gtk-theme", "Yaru-dark"),
        ),
    )


@_register("theme_light", "dark_off")
def _h_dark_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="light_theme",
        description="Включаю светлую тему Ubuntu (Yaru)",
        commands=(
            ("gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "default"),
            ("gsettings", "set", "org.gnome.desktop.interface", "gtk-theme", "Yaru"),
        ),
    )


@_register("dnd_on")
def _h_dnd_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="dnd_on",
        description="Выключаю уведомления (режим «Не беспокоить»)",
        commands=(("gsettings", "set", "org.gnome.desktop.notifications", "show-banners", "false"),),
    )


@_register("dnd_off")
def _h_dnd_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="dnd_off",
        description="Включаю уведомления (режим «Не беспокоить» выключен)",
        commands=(("gsettings", "set", "org.gnome.desktop.notifications", "show-banners", "true"),),
    )


@_register("scale_up")
def _h_scale_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="scale_up",
        description="Увеличиваю масштаб интерфейса GNOME (1.25x)",
        commands=(("gsettings", "set", "org.gnome.desktop.interface", "text-scaling-factor", "1.25"),),
    )


@_register("scale_down", "scale_reset")
def _h_scale_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="scale_down",
        description="Сбрасываю масштаб интерфейса GNOME (1.0x)",
        commands=(("gsettings", "set", "org.gnome.desktop.interface", "text-scaling-factor", "1.0"),),
    )


@_register("autorotate_on")
def _h_autorotate_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="orientation_unlock",
        description="Включаю автоповорот экрана",
        commands=(("gsettings", "set", "org.gnome.settings-daemon.peripherals.touchscreen", "orientation-lock", "false"),),
    )


@_register("autorotate_off")
def _h_autorotate_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="display",
        action="orientation_lock",
        description="Блокирую автоповорот экрана",
        commands=(("gsettings", "set", "org.gnome.settings-daemon.peripherals.touchscreen", "orientation-lock", "true"),),
    )


@_register("touchpad_on")
def _h_touchpad_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="device",
        action="touchpad_on",
        description="Включаю тачпад",
        commands=(("gsettings", "set", "org.gnome.desktop.peripherals.touchpad", "send-events", "enabled"),),
    )


@_register("touchpad_off")
def _h_touchpad_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="device",
        action="touchpad_off",
        description="Выключаю тачпад",
        commands=(("gsettings", "set", "org.gnome.desktop.peripherals.touchpad", "send-events", "disabled"),),
    )


# --- Разрешение экрана: org.gnome.Mutter.DisplayConfig (как делает «Настройки -> Экран») ---
_DISPLAY_SCRIPT = r"""
import json, sys
from gi.repository import Gio, GLib
want = sys.argv[1]            # "WxH" | "preferred" | "max" | "min" | "smaller" | "bigger" | "list"
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
proxy = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, "org.gnome.Mutter.DisplayConfig",
                               "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig", None)
serial, monitors, logical, props = proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, 5000, None).unpack()
by_connector = {m[0][0]: m for m in monitors}
if want == "list":
    print(json.dumps({c: sorted({(md[1], md[2]) for md in m[1]}, reverse=True) for c, m in by_connector.items()})); sys.exit(0)

def current_mode(modes):
    return next((md for md in modes if md[6].get("is-current")), modes[0])

def pick(modes):
    current = current_mode(modes)
    preferred = next((md for md in modes if md[6].get("is-preferred")), current)
    res_set = sorted({(md[1], md[2]) for md in modes}, key=lambda r: (r[0] * r[1], r[0]), reverse=True)
    if want == "preferred":
        target = (preferred[1], preferred[2])
    elif want == "max":
        target = res_set[0]
    elif want == "min":
        target = res_set[-1]
    elif want in ("smaller", "bigger"):
        cur = (current[1], current[2])
        i = res_set.index(cur) if cur in res_set else 0
        target = res_set[min(len(res_set) - 1, i + 1)] if want == "smaller" else res_set[max(0, i - 1)]
    else:
        w, h = (int(x) for x in want.split("x"))
        target = (w, h) if (w, h) in res_set else min(res_set, key=lambda r: abs(r[0] - w) + abs(r[1] - h))
    cands = [md for md in modes if (md[1], md[2]) == target]
    same_rate = [md for md in cands if abs(md[3] - current[3]) < 0.5]
    return (same_rate or sorted(cands, key=lambda md: -md[3]))[0]

# Меняется только основной монитор; остальные остаются в текущем режиме и масштабе.
new_logical, chosen = [], None
for (lx, ly, lscale, ltransform, lprimary, lmonitors, lprops) in logical:
    mons, scale = [], lscale
    for (connector, vendor, product, mserial) in lmonitors:
        modes = by_connector[connector][1]
        if lprimary and chosen is None:
            mode = pick(modes)
            scale = mode[4]                                           # рекомендованный масштаб нового режима (как в «Настройках»)
            chosen = (connector, (mode[1], mode[2]), mode[3], scale)
        else:
            mode = current_mode(modes)
        mons.append((connector, mode[0], {}))
    new_logical.append((lx, ly, scale, ltransform, lprimary, mons))
if chosen is None:
    print("no primary monitor"); sys.exit(1)
proxy.call_sync("ApplyMonitorsConfig", GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 2, new_logical, {})),
                Gio.DBusCallFlags.NONE, 5000, None)
print(json.dumps({"connector": chosen[0], "width": chosen[1][0], "height": chosen[1][1], "rate": round(chosen[2]), "scale": chosen[3]}))
"""


def _parse_resolution(query: str) -> str:
    q = re.sub(r"(?<=\d)\s*х\s*(?=\d)", "x", _norm(query))     # «1920х1080» с кириллической «х», но не «хд»
    if not q:
        return ""
    m = re.search(r"(\d{3,4})\s*(?:x|на|по|\*)\s*(\d{3,4})", q)
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    presets = {
        "4k": "3840x2160", "4к": "3840x2160", "2k": "2560x1440", "2к": "2560x1440", "1440p": "2560x1440", "1080p": "1920x1080",
        "full hd": "1920x1080", "фулл хд": "1920x1080", "фул хд": "1920x1080", "фуллхд": "1920x1080", "фул эйчди": "1920x1080",
        "720p": "1280x720", "hd": "1280x720", "хд": "1280x720",
    }
    for key, val in presets.items():
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", q):
            return val
    if any(w in q for w in ("максим", "самое больш", "наибольш")):
        return "max"
    if any(w in q for w in ("родн", "натив", "рекоменд", "стандарт", "по умолчани", "обычн", "нормальн")):
        return "preferred"
    if any(w in q for w in ("минимал", "самое мален", "наимень")):
        return "min"
    if any(w in q for w in ("поменьше", "меньше", "ниже", "пониж")):
        return "smaller"
    if any(w in q for w in ("побольше", "больше", "выше", "повыс")):
        return "bigger"
    return ""


def _set_resolution_op(query: str) -> str:
    want = _parse_resolution(query)
    if not want:
        subprocess.Popen(["gnome-control-center", "display"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Какое разрешение поставить — не поняла; открыла настройки экрана"
    sys_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    try:
        res = subprocess.run([sys_py, "-c", _DISPLAY_SCRIPT, want], capture_output=True, text=True, timeout=12.0, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemCommandError(f"не удалось сменить разрешение: {exc}")
    if res.returncode != 0:
        raise SystemCommandError(f"не удалось сменить разрешение: {(res.stderr or res.stdout).strip()[-300:]}")
    try:
        info = json.loads(res.stdout.strip().splitlines()[-1])
        return (f"Разрешение экрана {info['connector']}: {info['width']}x{info['height']} @ {info['rate']} Гц, масштаб {info['scale']:g}x "
                f"(вернуть: «верни родное разрешение»)")
    except (ValueError, KeyError, IndexError):
        return "Разрешение экрана изменено"


@_register("display_res_set")
def _h_display_res_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    want = _parse_resolution(query)
    return SystemPlan(
        category="display",
        action="resolution_set",
        description=f"Меняю разрешение экрана: {want or 'открываю настройки экрана'}",
        commands=(("gnome-control-center", "display"),),
        python_op=lambda: _set_resolution_op(query),
    )


@_register("screen_off")
def _h_screen_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    cmd = ("gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig", "--object-path", "/org/gnome/Mutter/DisplayConfig",
           "--method", "org.freedesktop.DBus.Properties.Set", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode", "<int32 3>")

    def _op() -> str:
        time.sleep(0.4)
        res = subprocess.run(list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=4.0, check=False)
        if res.returncode == 0:
            return "Экран выключен (любая клавиша или движение мыши включит его обратно)"
        raise SystemCommandError("не удалось выключить экран через Mutter.DisplayConfig")

    return SystemPlan(category="display", action="screen_off", description="Выключаю экран", commands=(cmd,), python_op=_op)


def _wallpaper_op(query: str, home: Path, dirs: dict[str, Path]) -> str:
    if not _norm(query):
        subprocess.Popen(["gnome-control-center", "background"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Какие обои поставить — не сказано; открыла настройки фона"
    pic = _find_target_item(query, home, dirs, want_dir=False, prefer_root=dirs["PICTURES"])
    if not pic.exists():
        raise SystemCommandError(f"картинка «{query}» не найдена")
    uri = pic.resolve().as_uri()
    for key in ("picture-uri", "picture-uri-dark"):
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", key, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
    return f"Обои рабочего стола: {pic}"


@_register("wallpaper_set")
def _h_wallpaper_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="display", action="wallpaper", description=f"Ставлю обои: {query or 'настройки фона'}",
                      commands=(("gsettings", "set", "org.gnome.desktop.background", "picture-uri", query),),
                      python_op=lambda: _wallpaper_op(query, home, dirs))
