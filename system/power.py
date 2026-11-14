"""Питание и сеанс: профили питания, выключение / перезагрузка / сон / гибернация (с таймером), блокировка, выход,
режим «не выключай экран»."""
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

from .core import CLIPBOARD_STATE_FILE, SystemPlan, _norm, _parse_delay_minutes, _register  # noqa: F401

# --- 4.3. Профили питания ---
@_register("power_perf")
def _h_power_perf(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="power_performance",
        description="Включаю режим максимальной производительности Ubuntu (performance)",
        commands=(("powerprofilesctl", "set", "performance"),),
    )


@_register("power_saver")
def _h_power_saver(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="power_saver",
        description="Включаю режим энергосбережения Ubuntu (power-saver)",
        commands=(("powerprofilesctl", "set", "power-saver"),),
    )


@_register("power_balanced")
def _h_power_balanced(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="power_balanced",
        description="Включаю сбалансированный режим питания Ubuntu (balanced)",
        commands=(("powerprofilesctl", "set", "balanced"),),
    )


# --- 4.6. Питание компьютера и сессия ---
@_register("power_off")
def _h_power_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    if _norm(query):
        return _h_power_off_timer(query, site, home, dirs)
    return SystemPlan(
        category="power",
        action="poweroff",
        description="Выключаю компьютер",
        commands=(("systemctl", "poweroff"),),
    )


@_register("power_off_timer")
def _h_power_off_timer(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    mins = _parse_delay_minutes(query, default=60)
    return SystemPlan(
        category="power",
        action="poweroff_delayed",
        description=f"Планирую выключение компьютера через {mins} мин.",
        commands=(("shutdown", f"+{mins}"),),
    )


@_register("power_reboot")
def _h_power_reboot(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    if _norm(query):
        return _h_power_reboot_timer(query, site, home, dirs)
    return SystemPlan(
        category="power",
        action="reboot",
        description="Перезагружаю компьютер",
        commands=(("systemctl", "reboot"),),
    )


@_register("power_reboot_timer")
def _h_power_reboot_timer(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    mins = _parse_delay_minutes(query, default=5)
    return SystemPlan(
        category="power",
        action="reboot_delayed",
        description=f"Планирую перезагрузку компьютера через {mins} мин.",
        commands=(("shutdown", "-r", f"+{mins}"),),
    )


@_register("power_cancel")
def _h_power_cancel(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="shutdown_cancel",
        description="Отменяю запланированное выключение/перезагрузку",
        commands=(("shutdown", "-c"),),
    )


@_register("power_sleep")
def _h_power_sleep(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="suspend",
        description="Перевожу компьютер в спящий режим",
        commands=(("systemctl", "suspend"),),
    )


@_register("power_hibernate")
def _h_power_hibernate(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="hibernate",
        description="Перевожу компьютер в режим гибернации",
        commands=(("systemctl", "hibernate"),),
        fallback_commands=(("systemctl", "suspend"),),
    )


@_register("power_wakeup", "power_wake")
def _h_power_wake(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="wake",
        description="Пробуждаю экран компьютера",
        commands=(("loginctl", "unlock-session"),),
    )


@_register("session_lock")
def _h_session_lock(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    if _norm(query):
        return _h_session_lock_timer(query, site, home, dirs)
    return SystemPlan(
        category="power",
        action="lock",
        description="Блокирую экран компьютера",
        commands=(("loginctl", "lock-session"),),
        fallback_commands=((
            "gdbus", "call", "--session",
            "--dest", "org.gnome.ScreenSaver",
            "--object-path", "/org/gnome/ScreenSaver",
            "--method", "org.gnome.ScreenSaver.Lock",
        ),),
    )


@_register("session_lock_timer")
def _h_session_lock_timer(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    mins = _parse_delay_minutes(query, default=10)
    secs = mins * 60
    return SystemPlan(
        category="power",
        action="lock_delayed",
        description=f"Блокировка экрана запланирована через {mins} мин.",
        commands=(("bash", "-c", f"(sleep {secs} && loginctl lock-session) >/dev/null 2>&1 &"),),
    )


@_register("session_logout")
def _h_session_logout(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="logout",
        description="Выхожу из сеанса пользователя GNOME",
        commands=(("gnome-session-quit", "--logout", "--no-prompt"),),
    )


@_register("session_switch_user")
def _h_session_switch_user(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="power",
        action="switch_user",
        description="Переключаю экран выбора пользователя (gdmflexiserver)",
        commands=(("dm-tool", "switch-to-greeter"),),
        fallback_commands=(("loginctl", "lock-session"),),
    )


_CAFFEINE_STATE = CLIPBOARD_STATE_FILE.with_name("pc_agent_caffeine.json")


def _caffeine_op(enable: bool) -> str:
    def _get(schema, key):
        return subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=3.0, check=False).stdout.strip()

    def _set(schema, key, value):
        subprocess.run(["gsettings", "set", schema, key, value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)

    if enable:
        saved = {"idle": _get("org.gnome.desktop.session", "idle-delay"), "lock": _get("org.gnome.desktop.screensaver", "lock-enabled"),
                 "ac": _get("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type")}
        try:
            _CAFFEINE_STATE.write_text(json.dumps(saved), encoding="utf-8")
        except OSError:
            pass
        _set("org.gnome.desktop.session", "idle-delay", "uint32 0")
        _set("org.gnome.desktop.screensaver", "lock-enabled", "false")
        _set("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'nothing'")
        return "Экран не будет гаснуть и блокироваться, компьютер не уснёт (пока не скажете «верни затухание экрана»)"
    saved = {}
    try:
        saved = json.loads(_CAFFEINE_STATE.read_text(encoding="utf-8"))
        _CAFFEINE_STATE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    _set("org.gnome.desktop.session", "idle-delay", saved.get("idle") or "uint32 300")
    _set("org.gnome.desktop.screensaver", "lock-enabled", saved.get("lock") or "true")
    _set("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", saved.get("ac") or "'suspend'")
    return "Вернула обычное затухание экрана, блокировку и сон"


@_register("caffeine_on")
def _h_caffeine_on(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="power", action="caffeine_on", description="Отключаю затухание экрана, блокировку и сон",
                      commands=(("gsettings", "set", "org.gnome.desktop.session", "idle-delay", "0"),), python_op=lambda: _caffeine_op(True))


@_register("caffeine_off")
def _h_caffeine_off(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="power", action="caffeine_off", description="Возвращаю обычное затухание экрана и блокировку",
                      commands=(("gsettings", "reset", "org.gnome.desktop.session", "idle-delay"),), python_op=lambda: _caffeine_op(False))
