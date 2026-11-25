"""Инструменты второго слоя для всего, на что нет готового действия: любая команда, запись текста в файл, список
того, что установлено. Так Gemini может сделать на компьютере что угодно — но изменения только после «да».

    run_command      читающие команды из белого списка (sys_query) — сразу; любые другие — после «да» (вопрос голосом
                     своими словами + уведомление с точным текстом команды); sudo и интерактивное — в окне терминала,
                     пароль вводит сам пользователь. Никогда: стереть диск, удалить корень или весь дом, curl | sh,
                     команды с путями к ключам и паролям.
    write_text_file  записать / дописать текст в файл в домашней папке (перезапись существующего — после «да»)
    list_apps        программы, панели настроек и игры Steam этой машины — чтобы сопоставить слова пользователя по смыслу
"""
from __future__ import annotations

import configparser
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import sys_query

MAX_OUTPUT = 4000
TIMEOUT = 120.0

# Что не выполняем ни с каким «да»: необратимое уничтожение системы или всех данных, выполнение скачанного скрипта.
_NEVER = [
    (r"\bmkfs(?:\.\w+)?\b|\bwipefs\b|\b(?:fdisk|sfdisk|gdisk|parted)\b", "разметка и форматирование дисков"),
    (r"\bdd\b[^;&|]*\bof=/dev/|>\s*/dev/(?:sd|nvme|hd|mmcblk)|\bshred\b[^;&|]*/dev/", "запись прямо на диск"),
    (r":\(\)\s*\{", "fork-бомба"),
    (r"\bch(?:mod|own)\s+(?:-\S+\s+)*-R\s+\S+\s+/(?:\s|$)|\bch(?:mod|own)\s+-R\s+(?:\S+\s+)*/(?:\s|$)",
     "смена прав на весь корень"),
    (r"\b(?:curl|wget)\b[^;&|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", "запуск скачанного скрипта"),
]


class CommandRefused(ValueError):
    """Команду выполнять нельзя (с объяснением)."""


_ALL_DATA = re.compile(r"^(?:/|/\*|~/?\*?|\$HOME/?\*?|\$\{HOME\}/?\*?|/home(?:/[^/\s]*)?/?\*?)$")


def _wipes_everything(cmd: str) -> bool:
    """rm -r (в любом порядке флагов, через sudo тоже) по корню, всему дому или /home."""
    for part in re.split(r"[;&|]+", cmd):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if "rm" not in words:
            continue
        args = words[words.index("rm") + 1:]
        recursive = any(a in ("--recursive",) or (a.startswith("-") and not a.startswith("--") and "r" in a.lower())
                        for a in args)
        if recursive and any(_ALL_DATA.match(a) for a in args if not a.startswith("-")):
            return True
    return False


def check_command(command: str) -> str:
    """'read' — только чтение (сразу), 'change' — с подтверждением; CommandRefused — нельзя совсем."""
    cmd = (command or "").strip()
    if not cmd:
        raise CommandRefused("пустая команда")
    if _wipes_everything(cmd):
        raise CommandRefused("это удаление корня или всей домашней папки — такое не выполняю никогда")
    for pattern, why in _NEVER:
        if re.search(pattern, cmd):
            raise CommandRefused(f"это {why} — такое не выполняю никогда")
    if any(m in cmd.lower() for m in sys_query.SENSITIVE):
        raise CommandRefused("команда касается ключей, паролей или профилей браузера — их не трогаю")
    try:
        sys_query.check_query(cmd)
        return "read"
    except sys_query.QueryRefused:
        return "change"


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Джарвис", "-i", "utilities-terminal", title, body],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)


def run_command(command: str, explanation: str, confirm: Callable[[str], bool], dry_run: bool = False,
                in_terminal: bool = False) -> str:
    if check_command(command) == "read":
        return sys_query.run_query(command)
    needs_terminal = in_terminal or bool(re.search(r"(?:^|[;&|\s])(?:sudo|pkexec|su)\s", f" {command}"))
    if dry_run:
        return f"[dry-run] выполнила бы{' в терминале' if needs_terminal else ''}: {command}"
    _notify("Джарвис хочет выполнить команду", command)
    if not confirm(f"Выполнить команду: {explanation or command}?"):
        return "Пользователь отказался, команда не выполнена."
    if needs_terminal:
        script = f"{command}\nprintf '\\n'; read -rp 'Готово. Enter — закрыть окно. '"
        subprocess.Popen(["gnome-terminal", "--", "bash", "-lc", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return "Открыла терминал с командой (пароль администратора, если нужен, вводится там). Результат — в окне терминала."
    if command.rstrip().endswith("&"):                  # программа в фоне: не ждём её завершения
        subprocess.Popen(["bash", "-lc", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=Path.home())
        return f"Запустила в фоне: {command}"
    try:
        res = subprocess.run(["bash", "-lc", command], capture_output=True, text=True, timeout=TIMEOUT, check=False,
                             stdin=subprocess.DEVNULL, cwd=Path.home(), errors="replace")
    except subprocess.TimeoutExpired:
        return f"$ {command}\n(остановлено: дольше {TIMEOUT:.0f} с — долгие задачи лучше запускать в терминале)"
    out = ((res.stdout or "") + (("\n" + res.stderr) if (res.stderr or "").strip() else "")).strip() or "(пустой вывод)"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n… (обрезано, всего {len(out)} символов)"
    return f"$ {command}\n{out}" + (f"\n(код возврата {res.returncode})" if res.returncode else "")


def write_text_file(path: str, text: str, mode: str, confirm: Callable[[str], bool],
                    readable: Callable[[Path], bool], dry_run: bool = False) -> str:
    """mode: append — дописать (файл создаётся, если его нет), overwrite — заменить содержимое, create — только новый."""
    raw = (path or "").strip()
    if not raw.startswith(("/", "~")):
        raise CommandRefused("нужен полный путь к файлу (например ~/Desktop/заметки.txt): узнайте его через list_folder")
    p = Path(raw).expanduser()
    if not p.parent.is_dir() or not readable(p.parent) or p.name.startswith(".") or not sys_query._path_ok(str(p)):
        raise CommandRefused("писать можно только в обычные файлы домашней папки или флешки")
    if p.is_dir():
        raise CommandRefused(f"{p} — это папка, а не файл")
    if p.exists() and (p.stat().st_size > 2_000_000 or b"\x00" in p.read_bytes()[:4096]):
        raise CommandRefused("это не небольшой текстовый файл — такой голосом не правлю")
    mode = (mode or "append").lower()
    if mode not in ("append", "overwrite", "create"):
        raise CommandRefused("mode: append, overwrite или create")
    if mode == "create" and p.exists():
        raise CommandRefused(f"файл {p.name} уже есть — дописать (append) или заменить (overwrite)?")
    if dry_run:
        return f"[dry-run] {'дописала бы' if mode == 'append' else 'записала бы'} в {p}: {text[:200]}"
    if mode == "overwrite" and p.exists() and p.stat().st_size and not confirm(f"Заменить содержимое файла {p.name}?"):
        return "Пользователь отказался, файл не изменён."
    if mode == "append" and p.exists() and p.stat().st_size:
        if not p.read_text(encoding="utf-8", errors="replace").endswith("\n"):
            text = "\n" + text
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
    else:
        p.write_text(text, encoding="utf-8")
    return f"Записала {len(text)} символов в {p}"


def list_apps() -> str:
    """Что есть на этой машине: программы (название — что это), панели настроек GNOME, игры Steam."""
    from .apps import desktop_apps, steam_games    # noqa: PLC0415
    rows = []
    for app in desktop_apps():
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(app.path, encoding="utf-8")
            entry = cp["Desktop Entry"]
            what = entry.get("GenericName") or entry.get("Comment") or ""
        except Exception:                                # noqa: BLE001
            what = ""
        rows.append(f"{app.title} — {what}".rstrip(" —"))
    panels = []
    for p in sorted(Path("/usr/share/applications").glob("gnome-*-panel.desktop")):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(p, encoding="utf-8")
            panels.append(f"{p.stem[len('gnome-'):-len('-panel')]} ({cp['Desktop Entry'].get('Name', '')})")
        except Exception:                                # noqa: BLE001
            continue
    games = [g[0] for g in steam_games()]
    out = ("Программы:\n" + "\n".join(sorted(rows)) +
           "\n\nПанели настроек (открыть: open_app с именем панели, например open_app(name='bluetooth')):\n" + ", ".join(panels) +
           ("\n\nИгры Steam:\n" + ", ".join(sorted(games)) if games else ""))
    return out[:9000]
