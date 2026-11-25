"""Скриншоты и запись экрана (gnome-screenshot / org.gnome.Shell.Screenshot / клавиша Print)."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import time
from pathlib import Path

from .core import (
    KEY_LEFTALT, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_SYSRQ, SystemCommandError, SystemPlan, _SHELL, _binding_combo,
    _norm, _register, _send_keys_or_xdotool,
)

# --- 4.5. Скриншоты и запись экрана ---
_SHOT_BINDINGS = {          # (ключ gsettings org.gnome.shell.keybindings, запасная комбинация)
    "full": ("screenshot", (KEY_LEFTSHIFT, KEY_SYSRQ)),            # Shift+Print — весь экран сразу в файл
    "window": ("screenshot-window", (KEY_LEFTALT, KEY_SYSRQ)),      # Alt+Print — активное окно сразу в файл
    "area": ("show-screenshot-ui", (KEY_SYSRQ,)),                   # Print — окно выбора области (выбирает человек)
}


def _gnome_shots_dir() -> Path:
    """Куда GNOME сам кладёт снимки: ~/Pictures/Screenshots (или локализованное имя)."""
    pics = Path(os.environ.get("XDG_PICTURES_DIR", "") or (Path.home() / "Pictures"))
    try:
        out = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=2.0, check=False).stdout.strip()
        if out:
            pics = Path(out)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for name in ("Screenshots", "Снимки экрана"):
        if (pics / name).is_dir():
            return pics / name
    return pics / "Screenshots"


def _newest_png(folder: Path, after: float) -> Path | None:
    try:
        cands = [p for p in folder.glob("*.png") if p.stat().st_mtime >= after]
    except OSError:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def _screenshot_op(target_file: Path, mode: str, wait: float = 4.0) -> str:
    """Скриншот на GNOME 46 (Wayland): gnome-screenshot, если установлен; иначе горячая клавиша GNOME из настроек
    пользователя (Shift+Print / Alt+Print / окно выбора), ожидание файла и перенос туда, куда просили."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("gnome-screenshot"):
        flag = ["-w", "-f", str(target_file)] if mode == "window" else (["-a", "-f", str(target_file)] if mode == "area" else ["-f", str(target_file)])
        res = subprocess.run(["gnome-screenshot", *flag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60.0, check=False)
        if res.returncode == 0 and target_file.exists():
            return f"Скриншот сохранён: {target_file}"
    key, fallback = _SHOT_BINDINGS[mode]
    combo = _binding_combo(((_SHELL, key),), fallback)
    shots, started = _gnome_shots_dir(), time.time() - 0.5
    if not _send_keys_or_xdotool((combo,), ("xdotool", "key", "shift+Print"), "ok"):
        raise SystemCommandError("не удалось нажать клавишу скриншота GNOME")
    if mode == "area":
        return f"Открыла выбор области: выделите её мышью, снимок сохранится в {shots}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        made = _newest_png(shots, started)
        if made is not None:
            time.sleep(0.3)                                   # GNOME дописывает файл
            if made.parent.resolve() != target_file.parent.resolve():
                final = target_file if not target_file.exists() else target_file.with_name(f"{target_file.stem}_{int(time.time())}.png")
                shutil.move(str(made), str(final))
                made = final
            return f"Скриншот сохранён: {made} (копия в буфере обмена)"
        time.sleep(0.15)
    raise SystemCommandError(f"GNOME не сохранил скриншот (ждала {wait:.0f} с в {shots})")


@_register("shot_screen", "screenshot_full")
def _h_screenshot_full(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    shots_dir = dirs["DESKTOP"] if ("рабоч" in _norm(site) or "desktop" in _norm(site)) else _gnome_shots_dir()
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
    target = _gnome_shots_dir() / f"Снимок_окна_{stamp}.png"
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
    target = _gnome_shots_dir() / f"Снимок_области_{stamp}.png"
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
        # GNOME 46 кладёт каждый снимок и в буфер обмена, и в файл: делаем снимок экрана сразу (Shift+Print)
        python_op=lambda: _screenshot_op(_gnome_shots_dir() / f"Снимок_экрана_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.png", "full")
        .replace("Скриншот сохранён", "Скриншот в буфере обмена, файл"),
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
