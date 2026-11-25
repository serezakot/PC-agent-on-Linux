"""Окна и рабочие столы: закрыть / свернуть / развернуть, закрыть все, закрыть программу или игру по имени, переключение,
обзор, прилепить влево / вправо, поверх всех, рабочие столы, браузер."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..apps import browser_close_patterns, find_steam_game, resolve_program
from .core import (
    KEY_D, KEY_ESC, KEY_F11, KEY_F4, KEY_H, KEY_LEFT, KEY_LEFTALT, KEY_LEFTMETA, KEY_PAGEDOWN, KEY_PAGEUP, KEY_RIGHT,
    KEY_SPACE, KEY_TAB, KEY_UP, SystemCommandError, SystemPlan, _MUTTER, _SHELL, _TILING, _WM, _binding_combo,
    _register, _send_keys_or_xdotool, _wayland_send_keycodes, resolve_close_patterns,
)

# --- 4.9. Управление окнами, рабочими столами и закрытием программ ---
def _pkill_pattern(pat: str) -> str:
    """Шаблон для `pkill -f`, который совпадает только с исполняемым файлом в начале командной строки
    («code» не должен убивать всё, где встречается слово code)."""
    if "steamapps/common" in pat or "\\." in pat:
        return pat
    return rf"(^|/){pat}(\s|$)"


# Процессы, без которых рушится сессия, звук, сеть или сам ассистент: «закрой X» их не трогает никогда.
_PROTECTED = {
    "systemd", "init", "dbus-daemon", "dbus-broker", "gnome-shell", "gnome-session", "gnome-session-binary", "gdm", "gdm3",
    "mutter", "xwayland", "xorg", "pipewire", "pipewire-pulse", "wireplumber", "pulseaudio", "networkmanager", "polkitd",
    "gnome-keyring-daemon", "ssh-agent", "gvfsd", "at-spi-bus-launcher", "ibus-daemon", "login", "sudo", "su",
    "bash", "sh", "zsh", "fish", "dash", "python", "python3", "node", "java", "perl", "ruby", "snapd", "snap", "flatpak",
    "gsd-power", "gsd-media-keys", "xdg-desktop-portal", "xdg-desktop-portal-gnome", "xdg-document-portal",
}


def _is_protected(pattern: str) -> bool:
    base = re.sub(r"\\(.)", r"\1", pattern).rsplit("/", 1)[-1].casefold()
    return base in _PROTECTED or len(base) < 3


def _own_ancestors() -> set[int]:
    """Сам ассистент и все его родители (терминал, оболочка): их закрытие убило бы ассистента."""
    pids, pid = set(), os.getpid()
    for _ in range(32):
        pids.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    return pids


def _safe_close_patterns(name: str) -> list[str]:
    if _is_protected(name.strip()):                     # «закрой pipewire» — отказ, а не «похожая» программа qpwgraph
        return []
    return [p_ for p_ in resolve_close_patterns(name) if not _is_protected(p_)]


def _close_app_op(name: str, patterns: list[str] | None = None) -> str:
    patterns = patterns if patterns is not None else _safe_close_patterns(name)
    if not patterns:
        if resolve_close_patterns(name) or _is_protected(name.strip()):
            raise SystemCommandError(f"«{name}» — системный процесс, его закрывать нельзя")
        raise SystemCommandError(f"не нашла программу «{name}» среди установленных, чтобы закрыть её")
    keep = _own_ancestors()
    for pat in patterns:
        res = subprocess.run(["pgrep", "-f", _pkill_pattern(pat)], capture_output=True, text=True, timeout=4.0, check=False)
        pids = [int(x) for x in (res.stdout or "").split() if x.isdigit() and int(x) not in keep]
        if not pids:
            continue
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        return f"Закрываю «{name}»"
    return f"Программа «{name}» сейчас не запущена"


def _app_names(name: str) -> list[str]:
    """Перечисление программ: «дискорд, телеграм и стим» и голосом без запятых «дискорд телеграм и стим» -> три имени.
    Делится, только если каждая часть — программа; одно длинное имя («visual studio code») не делится."""
    if _is_protected(name.strip()):
        return [name]
    out: list[str] = []
    for part in (p_.strip() for p_ in re.split(r"\s*,\s*|\s+и\s+|\s+а также\s+", name) if p_.strip()):
        words = part.split()
        if len(words) > 1 and all(resolve_program(w) or find_steam_game(w) for w in words) \
                and not (resolve_program(part) or find_steam_game(part)):
            out.extend(words)
        else:
            out.append(part)
    if len(out) > 1 and all(resolve_close_patterns(n) or _is_protected(n) for n in out):
        return out
    return [name]


def _close_apps_op(names: list[str]) -> str:
    done, problems = [], []
    for n in names:
        try:
            done.append(_close_app_op(n))
        except SystemCommandError as exc:
            problems.append(str(exc))
    if not done:
        raise SystemCommandError("; ".join(problems))
    return "; ".join(done + problems)


@_register("win_close_app", "app_close")
def _h_app_close(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    name = (query or site or "").strip(" ,.;:!?«»\"'")
    if not name:
        return _h_win_close(query, site, home, dirs)
    names = _app_names(name)
    if len(names) > 1:
        pats = [pat for n in names for pat in _safe_close_patterns(n)]
        return SystemPlan(category="window", action="close_app",
                          description="Закрываю " + ", ".join(f"«{n}»" for n in names),
                          commands=tuple(("pkill", "-TERM", "-f", _pkill_pattern(pat)) for pat in pats),
                          python_op=lambda names=tuple(names): _close_apps_op(list(names)))
    patterns = _safe_close_patterns(name)
    if not patterns:
        why = "системный процесс, закрывать нельзя" if (resolve_close_patterns(name) or _is_protected(name)) else "программа не найдена"
        return SystemPlan(category="window", action="close_app", description=f"Не закрываю «{name}»: {why}",
                          commands=(), python_op=lambda name=name: _close_app_op(name))
    return SystemPlan(
        category="window",
        action="close_app",
        description=f"Закрываю приложение «{name}»",
        commands=tuple(("pkill", "-TERM", "-f", _pkill_pattern(pat)) for pat in patterns),
        python_op=lambda name=name: _close_app_op(name),
    )


@_register("win_close_browser")
def _h_win_close_browser(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    """Браузеры этой машины (категория WebBrowser в .desktop), шаблоны с якорями — как у «закрой X»."""
    patterns = [p_ for p_ in browser_close_patterns() if not _is_protected(p_)]
    return SystemPlan(
        category="window",
        action="close_browser",
        description="Закрываю браузер",
        commands=tuple(("pkill", "-TERM", "-f", _pkill_pattern(pat)) for pat in patterns),
        python_op=lambda patterns=patterns: _close_app_op("браузер", patterns),
    )


@_register("win_restart_browser", "browser_restart")
def _h_browser_restart(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="restart_browser",
        description="Перезапускаю браузер Firefox",
        commands=(("bash", "-c", "pkill -f firefox; nohup firefox >/dev/null 2>&1 &"),),
    )


@_register("win_switch")
def _h_win_switch(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "switch-windows"), (_WM, "switch-applications")), (KEY_LEFTALT, KEY_TAB))
    return SystemPlan(
        category="window",
        action="switch_window",
        description="Переключаю на предыдущее окно",
        commands=(("xdotool", "key", "Alt+Tab"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "Alt+Tab"), "Переключено на предыдущее окно"),
    )


@_register("win_close_active", "win_close")
def _h_win_close(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "close"),), (KEY_LEFTALT, KEY_F4))
    return SystemPlan(
        category="window",
        action="close_window",
        description="Закрываю активное окно",
        commands=(("xdotool", "key", "Alt+F4"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "Alt+F4"), "Закрыто активное окно"),
    )


def _launched_desktop_file(pid: int) -> str:
    """.desktop, из которого GNOME запустил процесс (GIO_LAUNCHED_DESKTOP_FILE), или ''."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    for item in env:
        if item.startswith(b"GIO_LAUNCHED_DESKTOP_FILE="):
            return Path(item.split(b"=", 1)[1].decode(errors="replace")).name
    return ""


def _parent_pid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _running_gui_apps(exclude_pids: set[int]) -> list[tuple[int, str]]:
    """Открытые программы пользователя: главный процесс каждого приложения, запущенного из видимого .desktop
    (то есть из меню / дока / ассистента). Службы автозапуска и программа, в которой работает сам ассистент, не трогаются."""
    from ..apps import desktop_apps
    apps = desktop_apps()
    visible = {a.desktop_id: a.title for a in apps}
    generic = {"env", "sh", "bash", "python", "python3", "flatpak", "snap", "gjs", "java", "electron", "xdg-open", "gtk-launch"}
    by_exe = {Path(a.cmd[0]).name: a.desktop_id for a in apps if Path(a.cmd[0]).name not in generic}

    def app_of(pid: int) -> str:
        """Какому видимому приложению принадлежит процесс: по .desktop запуска, иначе по имени бинарника (D-Bus-активация)."""
        desktop = _launched_desktop_file(pid)
        if desktop in visible:
            return desktop
        if desktop:
            return ""                                  # запущен из службы автозапуска / скрытого .desktop
        try:
            return by_exe.get(Path(os.readlink(f"/proc/{pid}/exe")).name, "")
        except OSError:
            return ""

    own = {app_of(pid) for pid in exclude_pids} - {""}
    uid = os.getuid()
    found: dict[str, tuple[int, str]] = {}
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        try:
            if pid_dir.stat().st_uid != uid or pid in exclude_pids:
                continue
        except OSError:
            continue
        desktop = app_of(pid)
        if not desktop or desktop in own or app_of(_parent_pid(pid)) == desktop:
            continue                                   # чужой процесс / сам ассистент / дочерний процесс того же приложения
        found.setdefault(desktop, (pid, visible[desktop]))
    return list(found.values())


def _close_all_windows_op() -> str:
    """«Закрой все окна»: вежливо (SIGTERM) завершает все графические приложения пользователя, кроме терминала с ассистентом."""
    exclude = set()
    pid = os.getpid()
    while pid > 1:
        exclude.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    apps = _running_gui_apps(exclude)
    names = sorted({n for _, n in apps})
    for p, _ in apps:
        try:
            os.kill(p, 15)
        except OSError:
            pass
    combo = _binding_combo(((_WM, "close"),), (KEY_LEFTALT, KEY_F4))
    if not apps:
        _wayland_send_keycodes(combo)
        return "Закрыто активное окно (других программ с окнами не найдено)"
    return "Закрываю программы: " + ", ".join(names)


@_register("win_close_all")
def _h_win_close_all(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="close_all_windows",
        description="Закрываю все открытые программы",
        commands=(("xdotool", "key", "Alt+F4"),),
        python_op=_close_all_windows_op,
    )


@_register("win_min_active", "win_minimize")
def _h_win_minimize(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "minimize"),), (KEY_LEFTMETA, KEY_H))
    return SystemPlan(
        category="window",
        action="minimize_window",
        description="Сворачиваю активное окно",
        commands=(("xdotool", "key", "super+h"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "super+h"), "Свёрнуто активное окно"),
    )


def _show_desktop_op(keep_browser: bool = False) -> str:
    combo = _binding_combo(((_WM, "show-desktop"),), (KEY_LEFTMETA, KEY_D))
    msg = _send_keys_or_xdotool((combo,), ("xdotool", "key", "super+d"), "Свёрнуты все окна (показан рабочий стол)")
    if keep_browser:
        for exe in ("firefox", "google-chrome", "chromium", "brave-browser", "opera"):
            if shutil.which(exe) and subprocess.run(["pgrep", "-x", exe[:15]], stdout=subprocess.DEVNULL, check=False).returncode == 0:
                subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return "Свёрнуты все окна, кроме браузера"
    return msg


@_register("win_min_all", "win_minimize_all")
def _h_win_minimize_all(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="show_desktop",
        description="Сворачиваю все окна (показываю рабочий стол)",
        commands=(("wmctrl", "-k", "on"),),
        fallback_commands=(("xdotool", "key", "super+d"),),
        python_op=lambda: _show_desktop_op(False),
    )


@_register("win_min_except_browser", "win_minimize_other")
def _h_win_minimize_other(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="show_desktop_keep_browser",
        description="Сворачиваю все окна, кроме браузера",
        commands=(("wmctrl", "-k", "on"),),
        python_op=lambda: _show_desktop_op(True),
    )


@_register("win_max_active", "win_maximize")
def _h_win_maximize(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "maximize"), (_TILING, "tile-maximize"), (_WM, "toggle-maximized")), (KEY_LEFTMETA, KEY_UP))
    return SystemPlan(
        category="window",
        action="maximize_window",
        description="Разворачиваю окно на весь экран",
        commands=(("xdotool", "key", "super+Up"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "super+Up"), "Окно развёрнуто на весь экран"),
    )


@_register("win_fullscreen")
def _h_win_fullscreen(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="fullscreen_window",
        description="Переключаю полноэкранный режим окна (F11)",
        commands=(("xdotool", "key", "F11"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_F11,),), ("xdotool", "key", "F11"), "Переключён полноэкранный режим (F11)"),
    )


@_register("win_exit_fullscreen", "win_unfullscreen")
def _h_win_unfullscreen(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="exit_fullscreen",
        description="Выхожу из полноэкранного режима (F11 / Escape)",
        commands=(("xdotool", "key", "F11"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_F11,), (KEY_ESC,)), ("xdotool", "key", "F11"), "Выход из полноэкранного режима"),
    )


@_register("win_tile_left", "win_snap_left")
def _h_win_snap_left(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_MUTTER, "toggle-tiled-left"), (_TILING, "tile-left-half")), (KEY_LEFTMETA, KEY_LEFT))
    return SystemPlan(
        category="window",
        action="snap_left",
        description="Прикрепляю окно к левой половине экрана",
        commands=(("xdotool", "key", "super+Left"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "super+Left"), "Окно прикреплено слева"),
    )


@_register("win_tile_right", "win_snap_right")
def _h_win_snap_right(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_MUTTER, "toggle-tiled-right"), (_TILING, "tile-right-half")), (KEY_LEFTMETA, KEY_RIGHT))
    return SystemPlan(
        category="window",
        action="snap_right",
        description="Прикрепляю окно к правой половине экрана",
        commands=(("xdotool", "key", "super+Right"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "super+Right"), "Окно прикреплено справа"),
    )


@_register("win_always_on_top")
def _h_win_always_on_top(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "toggle-above"),), ())
    menu = _binding_combo(((_WM, "activate-window-menu"),), (KEY_LEFTALT, KEY_SPACE))

    def _op() -> str:
        if combo and _wayland_send_keycodes(combo):
            return "Окно закреплено поверх всех окон"
        # Комбинация «поверх всех окон» не назначена: открываем меню окна (Alt+Space), где есть пункт «Всегда сверху»
        if _wayland_send_keycodes(menu):
            return "Открыто меню окна: выберите «Всегда сверху» (комбинация toggle-above в GNOME не назначена)"
        raise SystemCommandError("не удалось закрепить окно поверх всех")

    return SystemPlan(
        category="window",
        action="always_on_top",
        description="Закрепляю окно поверх всех окон",
        commands=(("wmctrl", "-r", ":ACTIVE:", "-b", "toggle,above"),),
        python_op=_op,
    )


@_register("win_overview")
def _h_win_overview(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_SHELL, "toggle-overview"),), (KEY_LEFTMETA,))
    return SystemPlan(
        category="window",
        action="overview",
        description="Открываю обзор окон и рабочих столов GNOME",
        commands=(("xdotool", "key", "Super_L"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "Super_L"), "Открыт режим обзора GNOME"),
    )


@_register("ws_next")
def _h_ws_next(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "switch-to-workspace-right"), (_WM, "switch-to-workspace-down")), (KEY_LEFTMETA, KEY_PAGEDOWN))
    return SystemPlan(
        category="window",
        action="workspace_next",
        description="Переключаю на следующий рабочий стол",
        commands=(("xdotool", "key", "ctrl+alt+Right"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "ctrl+alt+Right"), "Переключено на следующий рабочий стол"),
    )


@_register("ws_prev")
def _h_ws_prev(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    combo = _binding_combo(((_WM, "switch-to-workspace-left"), (_WM, "switch-to-workspace-up")), (KEY_LEFTMETA, KEY_PAGEUP))
    return SystemPlan(
        category="window",
        action="workspace_prev",
        description="Переключаю на предыдущий рабочий стол",
        commands=(("xdotool", "key", "ctrl+alt+Left"),),
        python_op=lambda: _send_keys_or_xdotool((combo,), ("xdotool", "key", "ctrl+alt+Left"), "Переключено на предыдущий рабочий стол"),
    )
