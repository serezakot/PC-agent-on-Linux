"""Ядро системных команд: план, реестр обработчиков, разбор аргументов (проценты, секунды, время), нативный ввод клавиш
через org.gnome.Mutter.RemoteDesktop, чтение горячих клавиш GNOME, MPRIS, яркость, точка входа make_system_plan / execute_system_plan."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..apps import (  # noqa: F401
    _find_projects_dir, _find_removable_media, _latest_entry, _norm, _norm_stemmed, find_local_item,
    resolve_close_patterns, xdg_user_dirs,
)

# PC_AGENT_CLIPBOARD_FILE — отдельный файл и без системного буфера обмена (тесты не трогают буфер пользователя)
CLIPBOARD_STATE_FILE = Path(os.environ.get("PC_AGENT_CLIPBOARD_FILE") or
                            Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "pc_agent_clipboard.json")
SYSTEM_CLIPBOARD = not os.environ.get("PC_AGENT_CLIPBOARD_FILE")
KEY_LEFTMETA = 125
KEY_H = 35
KEY_D = 32


class SystemCommandError(RuntimeError):
    """Системную команду не удалось выполнить."""


@dataclass(frozen=True)
class SystemPlan:
    """План выполнения системной команды в Ubuntu 24.04 LTS."""
    category: str
    action: str
    description: str
    commands: tuple[tuple[str, ...], ...] = ()
    fallback_commands: tuple[tuple[str, ...], ...] = ()
    target_path: str = ""
    dest_path: str = ""
    python_op: Callable[[], str] | None = None


# =============================================================================
# 1. Вспомогательные парсеры аргументов (проценты, секунды, минуты, пути)
# =============================================================================
# --- Числа словами -> цифры: «двадцать пять» -> 25, «до тридцати» -> 30, «тысяча девятьсот двадцать» -> 1920 (знание языка,
# не фразы; общее для исполнителей и голосового ввода — jarvis/spoken.py) ---
from ..spoken import words_to_digits  # noqa: E402,F401


_PERCENT_WORDS = (("максимум", 100), ("полную", 100), ("полностью", 100), ("минимум", 5), ("половину", 50), ("половина", 50),
                  ("четверть", 25), ("треть", 33))


def _parse_percent(query: str, default: int | None = None) -> int | None:
    """Число процентов (0..100) из аргумента: «30», «двадцать пять процентов», «на максимум», «150» -> 100."""
    q = words_to_digits(query)
    if not q:
        return default
    m = re.search(r"(?<![\d.])(\d{1,4})(?![\d.])", q)
    if m:
        return max(0, min(100, int(m.group(1))))
    for word, val in _PERCENT_WORDS:
        if re.search(rf"\b{word}\b", q):
            return val
    return default


_UNIT_SECONDS = (("сек", 1), ("мин", 60), ("час", 3600))


def parse_duration(query: str, bare_unit: int = 60) -> int | None:
    """Длительность в секундах: «10 минут», «полтора часа», «1 час 30 минут», «две с половиной минуты», «минуту тридцать»,
    «двадцать пять минут». Число без единицы считается в bare_unit секунд (60 — минуты, 1 — секунды)."""
    q = _norm(query).replace("ё", "е")
    if not q:
        return None
    q = q.replace("полчаса", "30 минут").replace("полминуты", "30 секунд")
    q = words_to_digits(q)
    # единица без числа = одна единица: «минуту», «час», но не «1 минуту» / «2 часа»
    q = re.sub(r"(?<![\d.] )(?<![\d.])\b(?:минуту|минутку|минуточку)\b", "1 минута", q)
    q = re.sub(r"(?<![\d.] )(?<![\d.])\bчас\b", "1 час", q)
    q = re.sub(r"(?<![\d.] )(?<![\d.])\bсекунду\b", "1 секунда", q)
    q = re.sub(r"(\d+) с половиной", lambda m: f"{m.group(1)}.5", q)
    total, found, last_unit = 0.0, False, None
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:и\s+)?(сек\w*|мин\w*|час\w*)?", q):
        num = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        if unit:
            mult = next(v for k, v in _UNIT_SECONDS if unit.startswith(k))
        elif last_unit == 3600:
            mult = 60                       # «час двадцать» -> 1 ч 20 мин
        elif last_unit == 60:
            mult = 1                        # «минуту тридцать» -> 1 мин 30 сек
        else:
            mult = bare_unit
        total += num * mult
        found, last_unit = True, mult
    return int(round(total)) if found and total > 0 else None


def _parse_seconds(query: str, default: int = 10) -> int:
    """Секунды для перемотки: «20 секунд», «три минуты», «полторы минуты», «минуту тридцать»; голое число — секунды."""
    secs = parse_duration(query, bare_unit=1)
    return max(1, secs) if secs else default


def _parse_delay_minutes(query: str, default: int = 10) -> int:
    """Минуты задержки для выключения / перезагрузки / сна: «через 3 часа», «через двадцать пять минут»; голое число — минуты."""
    secs = parse_duration(query, bare_unit=60)
    return max(1, round(secs / 60)) if secs else default


# =============================================================================
# 2. Нативная интеграция с Wayland / GNOME 46 (Mutter.RemoteDesktop + DBus)
# =============================================================================
# Коды клавиш Linux input-event-codes (evdev), используемые Mutter.RemoteDesktop:
KEY_ESC = 1
KEY_TAB = 15
KEY_W = 17
KEY_T = 20
KEY_Y = 21
KEY_ENTER = 28
KEY_LEFTCTRL = 29
KEY_A = 30
KEY_S = 31
KEY_F = 33
KEY_LEFTSHIFT = 42
KEY_Z = 44
KEY_X = 45
KEY_C = 46
KEY_V = 47
KEY_N = 49
KEY_COMMA = 51
KEY_DOT = 52
KEY_LEFTALT = 56
KEY_SPACE = 57
KEY_F4 = 62
KEY_F5 = 63
KEY_F11 = 87
KEY_SYSRQ = 99
KEY_HOME = 102
KEY_UP = 103
KEY_PAGEUP = 104
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_END = 107
KEY_PAGEDOWN = 109
KEY_SUPER = 125
KEY_NEXTSONG = 163
KEY_PLAYPAUSE = 164
KEY_PREVIOUSSONG = 165

_MUTTER_KEYCODE_SCRIPT = r"""
import sys, time
from gi.repository import Gio, GLib

combos = [[int(x) for x in grp.split("+") if x] for grp in sys.argv[1:] if grp]
if not combos:
    sys.exit(0)

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
rd = Gio.DBusProxy.new_sync(
    bus, Gio.DBusProxyFlags.NONE, None,
    "org.gnome.Mutter.RemoteDesktop",
    "/org/gnome/Mutter/RemoteDesktop",
    "org.gnome.Mutter.RemoteDesktop", None
)
sess_path = rd.call_sync("CreateSession", None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
sess = Gio.DBusProxy.new_sync(
    bus, Gio.DBusProxyFlags.NONE, None,
    "org.gnome.Mutter.RemoteDesktop",
    sess_path,
    "org.gnome.Mutter.RemoteDesktop.Session", None
)
sess.call_sync("Start", None, Gio.DBusCallFlags.NONE, 2000, None)
try:
    for keys in combos:
        for k in keys:
            sess.call_sync(
                "NotifyKeyboardKeycode",
                GLib.Variant("(ub)", (k, True)),
                Gio.DBusCallFlags.NONE, 1000, None
            )
            time.sleep(0.015)
        time.sleep(0.025)
        for k in reversed(keys):
            sess.call_sync(
                "NotifyKeyboardKeycode",
                GLib.Variant("(ub)", (k, False)),
                Gio.DBusCallFlags.NONE, 1000, None
            )
            time.sleep(0.015)
        if len(combos) > 1:
            time.sleep(0.04)
finally:
    sess.call_sync("Stop", None, Gio.DBusCallFlags.NONE, 2000, None)
"""


def _wayland_send_keycodes(*combos: tuple[int, ...]) -> bool:
    """Отправляет одну или несколько комбинаций evdev-клавиш через org.gnome.Mutter.RemoteDesktop."""
    sys_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    args = ["+".join(str(k) for k in combo) for combo in combos if combo]
    if not args:
        return False
    try:
        res = subprocess.run(
            [sys_py, "-c", _MUTTER_KEYCODE_SCRIPT, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=6.0,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _send_keys_or_xdotool(
    combos: tuple[tuple[int, ...], ...],
    xdotool_cmd: tuple[str, ...],
    description: str,
) -> str:
    """Отправляет клавиши через Wayland Mutter.RemoteDesktop (или xdotool на X11)."""
    if _wayland_send_keycodes(*combos):
        return description
    if shutil.which(xdotool_cmd[0]):
        res = subprocess.run(list(xdotool_cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4.0, check=False)
        if res.returncode == 0:
            return description
    raise SystemCommandError(f"не удалось отправить клавиши ({description})")


# --- Горячие клавиши читаются из настроек GNOME пользователя (gsettings), а не зашиты в код ---
_ACCEL_KEYS = {
    "super": 125, "meta": 125, "alt": 56, "control": 29, "ctrl": 29, "shift": 42, "primary": 29,
    "tab": 15, "escape": 1, "return": 28, "enter": 28, "space": 57, "delete": 111, "insert": 110, "home": 102, "end": 107,
    "page_up": 104, "page_down": 109, "up": 103, "down": 108, "left": 105, "right": 106, "grave": 41, "minus": 12, "equal": 13,
    "plus": 13, "comma": 51, "period": 52, "print": 99, "kp_1": 79, "kp_2": 80, "kp_3": 81, "kp_4": 75, "kp_5": 76, "kp_6": 77,
    "kp_7": 71, "kp_8": 72, "kp_9": 73, "kp_0": 82, "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66,
    "f9": 67, "f10": 68, "f11": 87, "f12": 88, "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35, "i": 23,
    "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17,
    "x": 45, "y": 21, "z": 44, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
}


def _accel_to_keycodes(accel: str) -> tuple[int, ...] | None:
    """'<Super><Alt>Right' -> (125, 56, 106); None, если в комбинации есть неизвестная клавиша."""
    mods = [m.lower() for m in re.findall(r"<([^>]+)>", accel)]
    key = re.sub(r"<[^>]+>", "", accel).strip().lower()
    codes = []
    for m in mods:
        if m not in _ACCEL_KEYS:
            return None
        codes.append(_ACCEL_KEYS[m])
    if not key or key not in _ACCEL_KEYS:
        return None
    codes.append(_ACCEL_KEYS[key])
    return tuple(codes)


def _gsettings_accels(schema: str, key: str) -> list[str]:
    try:
        res = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=2.5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    return re.findall(r"'([^']+)'", res.stdout or "")


def _binding_combo(candidates: tuple[tuple[str, str], ...], fallback: tuple[int, ...]) -> tuple[int, ...]:
    """Первая реально назначенная в GNOME комбинация из списка (schema, key); иначе `fallback`."""
    for schema, key in candidates:
        for accel in _gsettings_accels(schema, key):
            combo = _accel_to_keycodes(accel)
            if combo:
                return combo
    return fallback


_WM = "org.gnome.desktop.wm.keybindings"
_MUTTER = "org.gnome.mutter.keybindings"
_SHELL = "org.gnome.shell.keybindings"
_TILING = "org.gnome.shell.extensions.tiling-assistant"


def _mpris_call(method: str, *variant_args: str) -> bool:
    """Отправляет команду активному медиаплееру через MPRIS DBus или playerctl."""
    try:
        res = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.ListNames",
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        players = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", res.stdout or "")
        for dest in players:
            cmd = [
                "gdbus", "call", "--session",
                "--dest", dest,
                "--object-path", "/org/mpris/MediaPlayer2",
                "--method", f"org.mpris.MediaPlayer2.Player.{method}",
                *variant_args,
            ]
            r2 = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5, check=False)
            if r2.returncode == 0:
                return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def _get_gnome_brightness() -> int | None:
    """Читает текущий процент яркости экрана (0..100) из org.gnome.SettingsDaemon.Power.Screen."""
    try:
        res = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Power",
                "--object-path", "/org/gnome/SettingsDaemon/Power",
                "--method", "org.freedesktop.DBus.Properties.Get",
                "org.gnome.SettingsDaemon.Power.Screen", "Brightness",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        m = re.search(r"(-?\d+)", res.stdout or "")
        if res.returncode == 0 and m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return val
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _set_gnome_brightness(pct: int) -> bool:
    """Устанавливает яркость экрана (0..100) через org.gnome.SettingsDaemon.Power.Screen."""
    pct = max(1, min(100, int(pct)))
    try:
        res = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Power",
                "--object-path", "/org/gnome/SettingsDaemon/Power",
                "--method", "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Power.Screen", "Brightness", f"<int32 {pct}>",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _adjust_brightness_op(delta: int) -> str:
    """Изменяет яркость экрана на `delta` процентов (например, -10 или +15) на Ubuntu 24.04."""
    cur = _get_gnome_brightness()
    if cur is not None:
        target = max(1, min(100, cur + delta))
        if _set_gnome_brightness(target):
            direction = "Уменьшена" if delta < 0 else "Увеличена"
            return f"{direction} яркость экрана: {cur}% -> {target}%"
    method = "StepDown" if delta < 0 else "StepUp"
    steps = max(1, round(abs(delta) / 10))
    ok = False
    for _ in range(steps):
        res = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.SettingsDaemon.Power",
                "--object-path", "/org/gnome/SettingsDaemon/Power",
                "--method", f"org.gnome.SettingsDaemon.Power.Screen.{method}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0:
            ok = True
    if ok:
        return "Уменьшена яркость экрана" if delta < 0 else "Увеличена яркость экрана"
    raise SystemCommandError("не удалось изменить яркость экрана через SettingsDaemon.Power")


# =============================================================================
# 4. Обработчики всех 104 действий SYSTEM_ACTIONS (без статических if по фразе!)
# =============================================================================
HandlerFn = Callable[[str, str, Path, dict[str, Path]], SystemPlan]
ACTION_HANDLERS: dict[str, HandlerFn] = {}


def _register(*names: str):
    def deco(fn: HandlerFn) -> HandlerFn:
        for name in names:
            ACTION_HANDLERS[name] = fn
        return fn
    return deco



# Текст напоминания текущей команды (reminder_set): make_system_plan кладёт его сюда, обработчик читает
_REMINDER_TEXT: dict[str, str] = {}


def name_similarity_safe(a: str, b: str) -> float:
    try:
        from ..apps import name_similarity
        return name_similarity(a, b)
    except Exception:
        return 0.0


# =============================================================================
# 5. Главная точка входа: make_system_plan и execute_system_plan
# =============================================================================
def make_system_plan(
    query: str = "",
    home: Path | None = None,
    action: str = "",
    site: str = "",
    text: str = "",
) -> SystemPlan | None:
    """Строит план выполнения системной команды Ubuntu 24.04 LTS по `(action, query, site, text)` от модели.
    Никакого разбора фразы здесь нет: действие и слоты всегда приходят от модели (`ml_infer`)."""
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return None

    _REMINDER_TEXT["text"] = (text or "").strip(" ,.;:!?«»\"'")

    home = home or Path.home()
    dirs = xdg_user_dirs(home)
    try:
        return handler(query, site, home, dirs)
    except (SystemCommandError, OSError) as exc:
        # Обработчик отказался (путь вне дома, слишком длинное имя...): честный план «не могу», а не падение ассистента
        reason = str(exc) if isinstance(exc, SystemCommandError) else f"ошибка файловой системы ({exc.strerror or exc})"

        def _refuse() -> str:
            raise SystemCommandError(reason)
        return SystemPlan(category="refused", action=action, description=f"Не выполняю: {reason}", commands=(), python_op=_refuse)


def _run_cmd_sequence(cmds: tuple[tuple[str, ...], ...]) -> bool:
    if not cmds:
        return False
    for cmd in cmds:
        exe = cmd[0]
        if not exe.startswith("/") and shutil.which(exe) is None:
            return False
        try:
            res = subprocess.run(list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8.0, check=False)
            if res.returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return True


def execute_system_plan(plan: SystemPlan, dry_run: bool = False) -> str:
    """Выполняет `SystemPlan` (или выводит план без выполнения при `dry_run=True`)."""
    cmd_str = " && ".join(shlex.join(c) for c in plan.commands) if plan.commands else plan.action
    if dry_run:
        return f"[dry-run] {plan.description} -> {cmd_str}"

    if plan.python_op is not None:
        return plan.python_op()

    if plan.category == "screenshot" and plan.target_path:
        Path(plan.target_path).parent.mkdir(parents=True, exist_ok=True)

    if _run_cmd_sequence(plan.commands):
        return plan.description
    if plan.fallback_commands and _run_cmd_sequence(plan.fallback_commands):
        return plan.description

    raise SystemCommandError(f"не удалось выполнить системную команду ({cmd_str})")
