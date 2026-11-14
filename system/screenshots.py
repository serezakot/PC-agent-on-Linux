"""Скриншоты и запись экрана (gnome-screenshot / org.gnome.Shell.Screenshot / клавиша Print)."""
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

from .core import KEY_LEFTALT, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_SYSRQ, SystemCommandError, SystemPlan, _norm, _register, _send_keys_or_xdotool, _wayland_send_keycodes  # noqa: F401

# --- 4.5. Скриншоты и запись экрана ---
def _screenshot_op(target_file: Path, mode: str) -> str:
    """Делает скриншот в Ubuntu 24.04 (gnome-screenshot / org.gnome.Shell.Screenshot / Wayland Print key)."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("gnome-screenshot"):
        flag = ["-w", "-f", str(target_file)] if mode == "window" else (["-a", "-f", str(target_file)] if mode == "area" else ["-f", str(target_file)])
        res = subprocess.run(["gnome-screenshot", *flag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8.0, check=False)
        if res.returncode == 0 and target_file.exists():
            return f"Скриншот сохранён: {target_file}"
    # DBus org.gnome.Shell.Screenshot
    method = "ScreenshotWindow" if mode == "window" else "Screenshot"
    args = ["true", "false", "true", str(target_file)] if mode == "window" else ["false", "true", str(target_file)]
    res = subprocess.run(
        [
            "gdbus", "call", "--session",
            "--dest", "org.gnome.Shell.Screenshot",
            "--object-path", "/org/gnome/Shell/Screenshot",
            "--method", f"org.gnome.Shell.Screenshot.{method}",
            *args,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5.0,
        check=False,
    )
    if res.returncode == 0 and target_file.exists():
        return f"Скриншот сохранён: {target_file}"
    # Резерв через встроенный UI снимка экрана GNOME 46 (клавиша Print)
    combo = (KEY_LEFTALT, KEY_SYSRQ) if mode == "window" else (KEY_SYSRQ,)
    if _wayland_send_keycodes(combo):
        return f"Скриншот выполнен (GNOME Screenshot)"
    raise SystemCommandError("не удалось сделать скриншот")


@_register("shot_screen", "screenshot_full")
def _h_screenshot_full(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    shots_dir = dirs["DESKTOP"] if ("рабоч" in _norm(site) or "desktop" in _norm(site)) else (dirs["PICTURES"] / "Снимки экрана")
    target = shots_dir / f"Снимок_экрана_{stamp}.png"
    return SystemPlan(
        category="screenshot",
        action="screenshot_full",
        description=f"Делаю скриншот экрана: {target}",
        target_path=str(target),
        commands=(("gnome-screenshot", "-f", str(target)),),
        fallback_commands=((
            "gdbus", "call", "--session",
            "--dest", "org.gnome.Shell.Screenshot",
            "--object-path", "/org/gnome/Shell/Screenshot",
            "--method", "org.gnome.Shell.Screenshot.Screenshot",
            "false", "true", str(target),
        ),),
        python_op=lambda target=target: _screenshot_op(target, "full"),
    )


@_register("shot_window", "screenshot_window")
def _h_screenshot_window(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = (dirs["PICTURES"] / "Снимки экрана") / f"Снимок_окна_{stamp}.png"
    return SystemPlan(
        category="screenshot",
        action="screenshot_window",
        description=f"Делаю скриншот активного окна: {target}",
        target_path=str(target),
        commands=(("gnome-screenshot", "-w", "-f", str(target)),),
        python_op=lambda target=target: _screenshot_op(target, "window"),
    )


@_register("shot_area", "screenshot_area")
def _h_screenshot_area(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = (dirs["PICTURES"] / "Снимки экрана") / f"Снимок_области_{stamp}.png"
    return SystemPlan(
        category="screenshot",
        action="screenshot_area",
        description=f"Делаю скриншот выбранной области: {target}",
        target_path=str(target),
        commands=(("gnome-screenshot", "-a", "-f", str(target)),),
        python_op=lambda target=target: _screenshot_op(target, "area"),
    )


@_register("shot_clipboard", "screenshot_clip")
def _h_screenshot_clip(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="screenshot",
        action="screenshot_clipboard",
        description="Делаю скриншот экрана в буфер обмена",
        commands=(("gnome-screenshot", "-c"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_SYSRQ,),), ("gnome-screenshot", "-c"), "Делаю скриншот в буфер обмена"),
    )


@_register("rec_start", "rec_stop", "screencast_toggle")
def _h_screencast(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, 19)  # Ctrl+Shift+Alt+R (keycode 19 = R)
    return SystemPlan(
        category="screenshot",
        action="screen_record",
        description="Переключаю запись экрана GNOME (Ctrl+Shift+Alt+R)",
        commands=(("xdotool", "key", "ctrl+shift+alt+r"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "ctrl+shift+alt+r"), "Переключаю запись экрана GNOME"),
    )
