"""Клавиатура: раскладка, вкладки браузера, прокрутка и навигация по странице, масштаб, буфер обмена текста, undo/redo, клавиши."""
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

from .core import CLIPBOARD_STATE_FILE, KEY_A, KEY_C, KEY_END, KEY_ENTER, KEY_ESC, KEY_F5, KEY_HOME, KEY_LEFT, KEY_LEFTALT, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_PAGEDOWN, KEY_PAGEUP, KEY_RIGHT, KEY_SPACE, KEY_SUPER, KEY_T, KEY_TAB, KEY_V, KEY_W, KEY_X, KEY_Y, KEY_Z, SystemCommandError, SystemPlan, _norm, _register, _send_keys_or_xdotool, _wayland_send_keycodes  # noqa: F401
from .files import _clipboard_files_get, _dest_dir_from_site, _fo_copy, _fo_move, _unique_copy_dest, _wait_exists, _wait_gone  # noqa: F401
from .media import _h_media_next, _h_media_play_pause, _h_media_prev  # noqa: F401
from .windows import _h_win_fullscreen  # noqa: F401

# --- 4.8. Переключение языка / раскладки клавиатуры ---
def _switch_layout_op(target_lang: str = "") -> str:
    """Переключает раскладку в GNOME 46: переставляет mru-sources/sources и отправляет Super+Space через Mutter."""
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
            capture_output=True, text=True, timeout=2.5, check=False,
        )
        pairs = re.findall(r"\('([^']+)',\s*'([^']+)'\)", res.stdout or "")
        if len(pairs) >= 2:
            if target_lang:
                ordered = sorted(pairs, key=lambda p: (0 if p[1].startswith(target_lang) else 1))
            else:
                ordered = pairs[1:] + pairs[:1]
            formatted = "[" + ", ".join(f"('{t}', '{ layout }')" for t, layout in ordered) + "]"
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.input-sources", "mru-sources", formatted],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5, check=False,
            )
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.input-sources", "sources", formatted],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5, check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _wayland_send_keycodes((KEY_SUPER, KEY_SPACE))
    return f"Переключена раскладка клавиатуры ({target_lang or 'следующий язык'})"


@_register("kb_layout", "layout_switch")
def _h_layout_switch(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    q = _norm(query)
    if "англи" in q:
        return _h_layout_en(query, site, home, dirs)
    if "русск" in q:
        return _h_layout_ru(query, site, home, dirs)
    return SystemPlan(
        category="keyboard",
        action="switch_layout",
        description="Переключаю раскладку клавиатуры (Super+Space)",
        commands=(("gsettings", "get", "org.gnome.desktop.input-sources", "sources"),),
        python_op=lambda: _switch_layout_op(""),
    )


@_register("layout_en")
def _h_layout_en(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="switch_layout_en",
        description="Переключаю раскладку клавиатуры на английский язык (us)",
        commands=(("gsettings", "get", "org.gnome.desktop.input-sources", "sources"),),
        python_op=lambda: _switch_layout_op("us"),
    )


@_register("layout_ru")
def _h_layout_ru(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="switch_layout_ru",
        description="Переключаю раскладку клавиатуры на русский язык (ru)",
        commands=(("gsettings", "get", "org.gnome.desktop.input-sources", "sources"),),
        python_op=lambda: _switch_layout_op("ru"),
    )


# --- 4.10. Вкладки браузера, навигация по странице, буфер обмена и клавиши ---
@_register("tab_new")
def _h_tab_new(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="new_tab",
        description="Открываю новую вкладку (Ctrl+T)",
        commands=(("xdotool", "key", "ctrl+t"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_T),), ("xdotool", "key", "ctrl+t"), "Открыта новая вкладка (Ctrl+T)"),
    )


@_register("tab_close")
def _h_tab_close(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="close_tab",
        description="Закрываю текущую вкладку (Ctrl+W)",
        commands=(("xdotool", "key", "ctrl+w"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_W),), ("xdotool", "key", "ctrl+w"), "Закрыта текущая вкладка (Ctrl+W)"),
    )


@_register("tab_close_all")
def _h_tab_close_all(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="close_all_tabs",
        description="Закрываю окно со всеми вкладками (Ctrl+Shift+W)",
        commands=(("xdotool", "key", "ctrl+shift+w"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_W),), ("xdotool", "key", "ctrl+shift+w"), "Закрыты все вкладки (Ctrl+Shift+W)"),
    )


@_register("tab_reopen")
def _h_tab_reopen(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="reopen_tab",
        description="Восстанавливаю закрытую вкладку (Ctrl+Shift+T)",
        commands=(("xdotool", "key", "ctrl+shift+t"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_T),), ("xdotool", "key", "ctrl+shift+t"), "Восстановлена закрытая вкладка (Ctrl+Shift+T)"),
    )


@_register("tab_next")
def _h_tab_next(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="next_tab",
        description="Переключаю на следующую вкладку (Ctrl+Tab)",
        commands=(("xdotool", "key", "ctrl+Tab"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_TAB),), ("xdotool", "key", "ctrl+Tab"), "Переключено на следующую вкладку (Ctrl+Tab)"),
    )


@_register("tab_prev")
def _h_tab_prev(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="prev_tab",
        description="Переключаю на предыдущую вкладку (Ctrl+Shift+Tab)",
        commands=(("xdotool", "key", "ctrl+shift+Tab"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_TAB),), ("xdotool", "key", "ctrl+shift+Tab"), "Переключено на предыдущую вкладку (Ctrl+Shift+Tab)"),
    )


@_register("page_reload", "page_refresh")
def _h_page_refresh(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="refresh_page",
        description="Обновляю страницу (F5)",
        commands=(("xdotool", "key", "F5"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_F5,),), ("xdotool", "key", "F5"), "Страница обновлена (F5)"),
    )


@_register("page_scroll_down")
def _h_page_scroll_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="scroll_down",
        description="Прокручиваю страницу вниз (PageDown)",
        commands=(("xdotool", "key", "Page_Down"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_PAGEDOWN,),), ("xdotool", "key", "Page_Down"), "Страница прокручена вниз"),
    )


@_register("page_scroll_up")
def _h_page_scroll_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="scroll_up",
        description="Прокручиваю страницу вверх (PageUp)",
        commands=(("xdotool", "key", "Page_Up"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_PAGEUP,),), ("xdotool", "key", "Page_Up"), "Страница прокручена вверх"),
    )


@_register("page_scroll_top", "page_top")
def _h_page_top(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="scroll_top",
        description="Прокручиваю в самое начало страницы (Home)",
        commands=(("xdotool", "key", "Home"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_HOME,),), ("xdotool", "key", "Home"), "Прокручено в начало страницы"),
    )


@_register("page_scroll_bottom", "page_bottom")
def _h_page_bottom(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="scroll_bottom",
        description="Прокручиваю в самый конец страницы (End)",
        commands=(("xdotool", "key", "End"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_END,),), ("xdotool", "key", "End"), "Прокручено в конец страницы"),
    )


@_register("nav_back", "browser_back")
def _h_browser_back(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="browser_back",
        description="Возвращаюсь назад в браузере (Alt+Left)",
        commands=(("xdotool", "key", "alt+Left"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTALT, KEY_LEFT),), ("xdotool", "key", "alt+Left"), "Назад в браузере (Alt+Left)"),
    )


@_register("nav_forward", "browser_forward")
def _h_browser_forward(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="browser_forward",
        description="Перехожу вперёд в браузере (Alt+Right)",
        commands=(("xdotool", "key", "alt+Right"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTALT, KEY_RIGHT),), ("xdotool", "key", "alt+Right"), "Вперёд в браузере (Alt+Right)"),
    )


@_register("zoom_in")
def _h_zoom_in(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="zoom_in",
        description="Увеличиваю масштаб страницы (Ctrl++)",
        commands=(("xdotool", "key", "ctrl+plus"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, 13),), ("xdotool", "key", "ctrl+plus"), "Масштаб страницы увеличен (Ctrl++)"),
    )


@_register("zoom_out")
def _h_zoom_out(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="zoom_out",
        description="Уменьшаю масштаб страницы (Ctrl+-)",
        commands=(("xdotool", "key", "ctrl+minus"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, 12),), ("xdotool", "key", "ctrl+minus"), "Масштаб страницы уменьшен (Ctrl+-)"),
    )


@_register("zoom_reset")
def _h_zoom_reset(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="zoom_reset",
        description="Сбрасываю масштаб страницы (Ctrl+0)",
        commands=(("xdotool", "key", "ctrl+0"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, 11),), ("xdotool", "key", "ctrl+0"), "Масштаб страницы сброшен (Ctrl+0)"),
    )


@_register("browser_incognito")
def _h_browser_incognito(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="incognito",
        description="Открываю приватное окно браузера (Firefox --private-window)",
        commands=(("firefox", "--private-window"),),
    )


@_register("clip_copy")
def _h_clip_copy(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="copy_selection",
        description="Копирую выделенное в буфер обмена (Ctrl+C)",
        commands=(("xdotool", "key", "ctrl+c"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_C),), ("xdotool", "key", "ctrl+c"), "Выделенное скопировано в буфер обмена (Ctrl+C)"),
    )


def _paste_op(site: str, home: Path, dirs: dict[str, Path]) -> str:
    """«вставь» -> Ctrl+V в активное окно; «вставь в папку X / на рабочий стол» -> настоящая вставка файлов из буфера в эту папку."""
    dest = _dest_dir_from_site(site, home, dirs) if site else None
    if dest is None:
        return _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_V),), ("xdotool", "key", "ctrl+v"), "Вставлено из буфера обмена (Ctrl+V)")
    op, paths = _clipboard_files_get()
    paths = [p for p in paths if p.exists()]
    if not paths:
        raise SystemCommandError("в буфере обмена нет файлов: сначала скажите «скопируй …» или «вырежи …»")
    dest.mkdir(parents=True, exist_ok=True)
    if op == "cut":
        ok = _fo_move(paths, dest) and all(_wait_gone(p, 6.0) for p in paths)
        if not ok:
            for p in paths:
                shutil.move(str(p), str(dest / p.name))
        try:
            CLIPBOARD_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return f"Перемещено в {dest}: " + ", ".join(p.name for p in paths)
    ok = _fo_copy(paths, dest) and all(_wait_exists(dest / p.name, 6.0) for p in paths)
    if not ok:
        for p in paths:
            final = _unique_copy_dest(p, dest)
            if p.is_dir():
                shutil.copytree(p, final, dirs_exist_ok=True)
            else:
                shutil.copy2(p, final)
    return f"Вставлено в {dest}: " + ", ".join(p.name for p in paths)


@_register("clip_paste")
def _h_clip_paste(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    dest = _dest_dir_from_site(site, home, dirs) if site else None
    return SystemPlan(
        category="fs" if dest else "keyboard",
        action="paste_files" if dest else "paste_clipboard",
        description=f"Вставляю файлы из буфера обмена в {dest}" if dest else "Вставляю из буфера обмена (Ctrl+V)",
        dest_path=str(dest) if dest else "",
        commands=(("xdotool", "key", "ctrl+v"),),
        python_op=lambda: _paste_op(site, home, dirs),
    )


@_register("clip_cut")
def _h_clip_cut(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="cut_selection",
        description="Вырезаю выделенное (Ctrl+X)",
        commands=(("xdotool", "key", "ctrl+x"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_X),), ("xdotool", "key", "ctrl+x"), "Выделенное вырезано (Ctrl+X)"),
    )


@_register("clip_clear")
def _h_clip_clear(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    def _clear() -> str:
        try:
            CLIPBOARD_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy", "--clear"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False)
        if shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard", "-i", "/dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False)
        return "Буфер обмена очищен"

    return SystemPlan(
        category="keyboard",
        action="clear_clipboard",
        description="Очищаю буфер обмена",
        commands=(("wl-copy", "--clear"),),
        fallback_commands=(("xclip", "-selection", "clipboard", "-i", "/dev/null"),),
        python_op=_clear,
    )


@_register("clip_select_all", "edit_select_all")
def _h_edit_select_all(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="select_all",
        description="Выделяю весь текст (Ctrl+A)",
        commands=(("xdotool", "key", "ctrl+a"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_A),), ("xdotool", "key", "ctrl+a"), "Выделено всё (Ctrl+A)"),
    )


@_register("edit_undo")
def _h_edit_undo(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="undo",
        description="Отменяю последнее действие (Ctrl+Z)",
        commands=(("xdotool", "key", "ctrl+z"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_Z),), ("xdotool", "key", "ctrl+z"), "Отменено последнее действие (Ctrl+Z)"),
    )


@_register("edit_redo")
def _h_edit_redo(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="redo",
        description="Повторяю отменённое действие (Ctrl+Y)",
        commands=(("xdotool", "key", "ctrl+y"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_Y),), ("xdotool", "key", "ctrl+y"), "Повторено действие (Ctrl+Y)"),
    )


@_register("key_enter")
def _h_key_enter(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="press_enter",
        description="Нажимаю клавишу Enter",
        commands=(("xdotool", "key", "Return"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_ENTER,),), ("xdotool", "key", "Return"), "Нажата клавиша Enter"),
    )


@_register("key_escape")
def _h_key_escape(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="press_escape",
        description="Нажимаю клавишу Escape",
        commands=(("xdotool", "key", "Escape"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_ESC,),), ("xdotool", "key", "Escape"), "Нажата клавиша Escape"),
    )


@_register("key_space")
def _h_key_space(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="keyboard",
        action="press_space",
        description="Нажимаю клавишу Пробел",
        commands=(("xdotool", "key", "space"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_SPACE,),), ("xdotool", "key", "space"), "Нажата клавиша Пробел"),
    )


@_register("key_func", "key_press")
def _h_key_press(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    q = _norm(query)
    if q == "f8":
        return _h_media_play_pause(query, site, home, dirs)
    if q == "f9":
        return _h_media_prev(query, site, home, dirs)
    if q == "f10":
        return _h_media_next(query, site, home, dirs)
    if q == "f11":
        return _h_win_fullscreen(query, site, home, dirs)
    if q == "f5":
        return _h_page_refresh(query, site, home, dirs)
    if q in ("энтер", "enter"):
        return _h_key_enter(query, site, home, dirs)
    if q in ("эскейп", "escape", "esc"):
        return _h_key_escape(query, site, home, dirs)
    return _h_key_space(query, site, home, dirs)
