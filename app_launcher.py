#!/usr/bin/env python3
"""Запуск программ, настроек GNOME, папок и файлов на Ubuntu 24.04 LTS (маршрут route == "app").

Модель (ml/ml_infer.py) выделяет:
    site  — что открыть, словами пользователя: «пайчарм», «блокнот», «загрузки», «traning ml», «доту»
    query — что сделать внутри: «Counter-Strike 2», «The Weeknd», «отсортируй по дате», «в папке проекта»

Этот модуль превращает слова в конкретную программу БЕЗ словаря приложений. Порядок:
  1. панель «Настроек» GNOME («настройки звука» -> gnome-control-center sound);
  2. стандартные папки («загрузки», «документы», «рабочий стол», «корзина», «флешка»);
  3. понятие («блокнот», «проводник», «браузер», «почта», «плеер», «терминал», «диспетчер задач»):
     умолчания системы (xdg-mime / xdg-settings) и категории freedesktop из .desktop-файлов — какая бы
     программа этого типа ни стояла, найдётся она;
  4. название программы: установленные .desktop-файлы (Native / Snap / Flatpak) и бинарники в PATH, сравнение
     с транслитерацией и нечётким совпадением («обсидан» -> Obsidian, «пайчарм» -> PyCharm, «фаерфокс» -> Firefox);
  5. игры из библиотеки Steam («запусти доту» -> steam://rungameid/570);
  6. файлы и папки пользователя по имени («открой traning ml», «открой файл отчёт пдф»);
  7. ничего не найдено -> поиск в Центре приложений.

Единственные таблицы здесь — знания, а не разбор фраз: имена понятий и их категории, URI-действия трёх программ
(Steam, Spotify, Telegram) и прозвища игр, которые транслитерацией не получить («доту» -> Dota 2).

Запуск отдельно:
    python3 app_launcher.py --dry-run "стим" "Counter-Strike 2"
"""
from __future__ import annotations

import configparser
import difflib
import functools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

LAUNCH_WAIT_SECONDS = 2.5


class AppLaunchError(RuntimeError):
    """Программу или папку не удалось открыть."""


@dataclass(frozen=True)
class AppPlan:
    """kind: gnome_settings | app | app_uri | folder | file | gnome_action"""
    kind: str
    target: str
    cmd: list[str]
    query: str = ""
    pre_cmds: tuple[tuple[str, ...], ...] = ()
    description: str = ""


# =============================================================================
# 0. Нормализация, транслитерация и нечёткое сравнение имён
# =============================================================================
def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").strip(" ,.;:!?«»\"'")
    text = re.sub(r"[-_]+", " ", text)
    return " ".join(text.split())


def _stem_ru(word: str) -> str:
    for suffix in ("ями", "ами", "ого", "его", "ому", "ему", "ую", "юю", "ой", "ей", "ом", "ем", "ах", "ях", "ов", "ев"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    for suffix in ("а", "я", "у", "ю", "е", "и", "ы", "о"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _norm_stemmed(text: str) -> str:
    return " ".join(_stem_ru(w) for w in _norm(text).split())


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_SKEL_SUBS = (("ph", "f"), ("ck", "k"), ("ks", "x"), ("kh", "h"), ("th", "t"), ("tch", "ch"), ("sch", "sh"), ("zh", "j"),
              ("qu", "k"), ("q", "k"), ("w", "v"), ("c", "k"), ("x", "ks"), ("z", "s"))


def _translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in _norm(text))


def _skeleton(text: str) -> str:
    """Согласный скелет латиницей: «пайчарм» -> «pchrm», «pycharm» -> «pchrm», «стим» / «steam» -> «stm»."""
    t = _translit(text)
    for a, b in _SKEL_SUBS:
        t = t.replace(a, b)
    out = []
    for i, ch in enumerate(t):
        if (ch in "aeiouy" and i > 0) or ch == " " or (out and out[-1] == ch):
            continue
        out.append(ch)
    return "".join(out)


def _name_variants(name_norm: str) -> list[str]:
    """«visual studio code» -> [«visual studio code», «vs code», «vsc»]: люди сокращают длинные названия."""
    words = name_norm.split()
    out = [name_norm]
    if len(words) >= 2:
        out.append("".join(w[0] for w in words[:-1]) + " " + words[-1])
        out.append("".join(w[0] for w in words))
    return out


def name_similarity(spoken: str, candidate: str) -> float:
    """0..1: насколько сказанное имя («дискорд», «вс код», «дота 2») похоже на настоящее («discord», «Visual Studio Code», «Dota 2»)."""
    a, b = _norm(spoken), _norm(candidate)
    if not a or not b:
        return 0.0
    if a == b or _norm_stemmed(a) == _norm_stemmed(b):
        return 1.0
    best = 0.0
    ta = _translit(a)
    for variant in _name_variants(b):
        tb = _translit(variant)
        full = difflib.SequenceMatcher(None, ta, tb).ratio()
        sk = difflib.SequenceMatcher(None, _skeleton(a), _skeleton(variant)).ratio() if len(_skeleton(a)) >= 2 else 0.0
        score = max(full, 0.92 * sk)
        if ta and tb and (tb.startswith(ta) or ta.startswith(tb)) and min(len(ta), len(tb)) >= 4:
            score = max(score, 0.9)
        best = max(best, score)
    return best


# =============================================================================
# 1. Панели настроек Ubuntu 24.04 (gnome-control-center): имена панелей
# =============================================================================
_GNOME_SETTINGS_PANELS: list[tuple[tuple[str, ...], str, str]] = [
    (("звук", "аудио", "микрофон", "динамик", "наушник", "громкост", "sound", "audio", "microphone"), "sound", "Настройки звука"),
    (("экран", "монитор", "диспле", "яркост", "разрешен", "герцовк", "частот обнов", "масштаб", "ночн свет", "ночн режим", "display", "displays", "screen", "monitor", "brightness"), "display", "Настройки дисплея"),
    (("вайфай", "вай фай", "wifi", "wi fi"), "wifi", "Настройки Wi-Fi"),
    (("сет", "интернет", "впн", "vpn", "прокси", "ethernet", "network"), "network", "Настройки сети"),
    (("блютуз", "блютус", "bluetooth"), "bluetooth", "Настройки Bluetooth"),
    (("батаре", "питан", "энерг", "аккумулятор", "power", "battery"), "power", "Настройки питания"),
    (("принтер", "печат", "printer", "printers"), "printers", "Настройки принтеров"),
    (("клавиатур", "хотке", "горяч клавиш", "комбинац клавиш", "сочетан", "раскладк", "keyboard"), "keyboard", "Настройки клавиатуры"),
    (("мыш", "тачпад", "сенсорн", "mouse", "touchpad"), "mouse", "Настройки мыши и тачпада"),
    (("камер", "веб камер", "camera"), "camera", "Настройки камеры"),
    (("приватност", "конфиденциальност", "безопасност", "защит", "геолокац", "блокировк экран", "privacy", "security"), "privacy", "Настройки конфиденциальности и безопасности"),
    (("уведомлен", "центр уведомлен", "notification", "notifications"), "notifications", "Настройки уведомлений"),
    (("многозадачн", "multitasking"), "multitasking", "Настройки многозадачности"),
    (("поиск", "search"), "search", "Настройки поиска"),
    (("аккаунт", "online accounts"), "online-accounts", "Сетевые аккаунты"),
    (("общ доступ", "общего доступ", "sharing"), "sharing", "Настройки общего доступа"),
    (("экранн врем", "благополуч", "wellbeing"), "wellbeing", "Экранное время и благополучие"),
    (("цвет", "цветов профил", "color"), "color", "Настройки цвета"),
    (("графическ планшет", "wacom"), "wacom", "Настройки графического планшета"),
    (("специальн возможност", "доступност", "accessibility"), "universal-access", "Специальные возможности"),
    (("пользовател", "учетн запис", "users"), "users", "Настройки пользователей"),
    (("дат", "врем", "часов", "date", "datetime"), "datetime", "Настройки даты и времени"),
    (("язык", "регион", "формат", "language", "region"), "region", "Настройки языка и региона"),
    (("тем", "обо", "заставк", "фон", "внешн", "background", "wallpaper", "appearance"), "background", "Настройки внешнего вида и фона"),
    (("автозагруз", "приложен", "стандартн приложен", "default apps", "apps", "applications"), "applications", "Настройки приложений"),
    (("панел задач", "док", "рабоч стол", "ubuntu desktop", "dock"), "ubuntu", "Настройки рабочего стола Ubuntu"),
    (("системн информац", "сведен о систем", "информац о систем", "о систем", "about system", "system settings"), "system", "О системе"),
]
_SETTINGS_WORDS = (
    "настройк", "параметр", "панель управлен", "системн информац", "информац о систем", "сведен о систем", "о систем",
    "центр уведомлен", "приложен по умолчан", "стандартн приложен", "сетев аккаунт", "онлайн аккаунт", "общий доступ",
    "экранное врем", "комбинац клавиш", "цветов профил", "специальн возможност", "учетн запис пользовател",
    "settings", "control center", "gnome control center", "default apps", "online accounts", "about system",
)


def _match_gnome_settings(site_norm: str, query_norm: str = "") -> AppPlan | None:
    if not any(w in site_norm for w in _SETTINGS_WORDS):
        return None
    combined = f"{site_norm} {query_norm}".strip()
    gcc = shutil.which("gnome-control-center") or "gnome-control-center"
    for keywords, panel, title in _GNOME_SETTINGS_PANELS:
        if any(kw in combined for kw in keywords):
            return AppPlan("gnome_settings", panel, [gcc, panel], query_norm, description=f"Открываю «{title}» (gnome-control-center {panel})")
    return AppPlan("gnome_settings", "overview", [gcc], query_norm, description="Открываю «Настройки Ubuntu» (gnome-control-center)")


def _match_gnome_shell(site_norm: str) -> AppPlan | None:
    """Встроенные функции оболочки: экранная клавиатура, лупа, список приложений."""
    if ("экранн" in site_norm and "клавиатур" in site_norm) or site_norm in ("on screen keyboard", "screen keyboard"):
        if shutil.which("onboard"):
            return AppPlan("app", "onboard", ["onboard"], description="Запускаю экранную клавиатуру (onboard)")
        return AppPlan("gnome_action", "screen-keyboard",
                       ["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", "true"],
                       description="Включаю экранную клавиатуру GNOME")
    if "экранн" in site_norm and "луп" in site_norm:
        return AppPlan("gnome_action", "screen-magnifier",
                       ["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-magnifier-enabled", "true"],
                       description="Включаю экранную лупу GNOME")
    if site_norm in ("меню пуск", "окно выполнить", "список приложений", "все приложения", "меню приложений"):
        return AppPlan("gnome_action", "applications",
                       ["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell",
                        "--method", "org.gnome.Shell.ShowApplications"],
                       description="Открываю список приложений GNOME")
    return None


# =============================================================================
# 2. Установленные программы: индекс .desktop-файлов (Native / Snap / Flatpak) и бинарники PATH
# =============================================================================
_DESKTOP_DIRS: tuple[Path, ...] = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path.home() / ".local/share/applications",
)


@dataclass(frozen=True)
class DesktopApp:
    desktop_id: str            # org.gnome.Calculator.desktop
    title: str                 # Калькулятор
    names: tuple[str, ...]     # Name[ru], Name, GenericName[ru], GenericName, file stem
    keywords: str
    categories: tuple[str, ...]
    cmd: tuple[str, ...]       # Exec без %-кодов
    path: str


def _clean_desktop_exec(exec_line: str) -> list[str]:
    parts = [p for p in shlex.split(exec_line) if not p.startswith("%")]
    if parts and parts[0] == "env":
        parts = parts[1:]
        while parts and "=" in parts[0] and not parts[0].startswith("-"):
            parts = parts[1:]
    while parts and parts[-1] == "--":
        parts.pop()
    return parts


@functools.lru_cache(maxsize=1)
def desktop_apps() -> tuple[DesktopApp, ...]:
    """Все установленные графические приложения (кэш на время работы ассистента)."""
    out, seen = [], set()
    for ddir in _DESKTOP_DIRS:
        if not ddir.is_dir():
            continue
        for path in sorted(ddir.glob("*.desktop")):
            if path.name in seen:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                entry = cp["Desktop Entry"]
            except Exception:
                continue
            if entry.get("NoDisplay", "").lower() == "true" or entry.get("Type", "Application") != "Application":
                continue
            cmd = _clean_desktop_exec(entry.get("Exec", "").strip())
            if not cmd:
                continue
            seen.add(path.name)
            names = tuple(n for n in (entry.get("Name[ru]", ""), entry.get("Name", ""), entry.get("GenericName[ru]", ""),
                                      entry.get("GenericName", ""), path.stem.split("_")[-1]) if n)
            out.append(DesktopApp(
                desktop_id=path.name,
                title=entry.get("Name[ru]") or entry.get("Name") or path.stem,
                names=names,
                keywords=_norm(entry.get("Keywords[ru]", "") + ";" + entry.get("Keywords", "")),
                categories=tuple(c for c in entry.get("Categories", "").split(";") if c),
                cmd=tuple(cmd),
                path=str(path),
            ))
    return tuple(out)


def find_desktop_app(name: str) -> DesktopApp | None:
    """Приложение по сказанному имени: точное имя, основа слова, подстрока, ключевые слова, нечёткая транслитерация."""
    wanted, wanted_stem = _norm(name), _norm_stemmed(name)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for app in desktop_apps():
        score = 0.0
        for cand in app.names:
            cn = _norm(cand)
            if not cn:
                continue
            if cn == wanted or _norm_stemmed(cn) == wanted_stem:
                score = max(score, 100)
            elif len(wanted) >= 4 and (wanted in cn or cn in wanted):
                score = max(score, 70)
            else:
                sim = name_similarity(wanted, cn)
                if sim >= 0.86:
                    score = max(score, 60 + 10 * sim)
        if score < 50 and len(wanted) >= 4 and wanted in app.keywords:
            score = 50
        if score > best_score:
            best, best_score = app, score
            if score >= 100:
                break
    return best


def _which_translit(name: str) -> str | None:
    """Бинарник в PATH по транслитерации («хтоп» -> htop, «nvim» -> nvim)."""
    for cand in {_norm(name).replace(" ", "-"), _norm(name).replace(" ", ""), _translit(name).replace(" ", "-"), _translit(name).replace(" ", "")}:
        if cand and re.fullmatch(r"[a-z0-9][a-z0-9.+-]{2,40}", cand):
            found = shutil.which(cand)
            if found and cand not in ("test", "time", "cat", "cd", "ls", "rm", "mv", "cp", "python", "python3", "bash", "sh", "find", "sort", "echo"):
                return found
    return None


# =============================================================================
# 3. Понятия: «браузер», «блокнот», «проводник», «плеер»... -> умолчания системы и категории freedesktop
# =============================================================================
@dataclass(frozen=True)
class Concept:
    key: str
    words: tuple[str, ...]            # как это называют вслух (в т.ч. привычки из Windows)
    title: str
    mime: str = ""                    # умолчание системы: xdg-mime query default <mime>
    categories: tuple[str, ...] = ()  # Categories= из .desktop (любое совпадение)
    binaries: tuple[tuple[str, ...], ...] = ()   # последний резерв, если ничего не установлено по категории


_CONCEPTS: tuple[Concept, ...] = (
    Concept("browser", ("браузер", "интернет", "веб браузер"), "Браузер", mime="x-scheme-handler/http", categories=("WebBrowser",)),
    Concept("text_editor", ("блокнот", "текстовый редактор", "редактор текста", "notepad", "text editor"), "Текстовый редактор", mime="text/plain", categories=("TextEditor",),
            binaries=(("gnome-text-editor",), ("gedit",))),
    Concept("files", ("проводник", "файлы", "файловый менеджер", "менеджер файлов", "explorer", "file manager"), "Файлы", mime="inode/directory", categories=("FileManager",),
            binaries=(("nautilus",),)),
    Concept("terminal", ("терминал", "консоль", "командную строку", "командная строка", "powershell", "cmd", "terminal"), "Терминал",
            categories=("TerminalEmulator",), binaries=(("gnome-terminal",), ("kgx",), ("x-terminal-emulator",))),
    Concept("calculator", ("калькулятор", "calculator"), "Калькулятор", categories=("Calculator",), binaries=(("gnome-calculator",),)),
    Concept("system_monitor", ("диспетчер задач", "системный монитор", "монитор ресурсов", "монитор системы", "task manager", "system monitor", "процессы"), "Системный монитор",
            categories=("Monitor",), binaries=(("gnome-system-monitor",),)),
    Concept("video_player", ("плеер", "видеоплеер", "медиаплеер", "видео плеер", "проигрыватель"), "Видеоплеер", mime="video/mp4", categories=("Video", "Player"),
            binaries=(("totem",), ("vlc",), ("mpv",))),
    Concept("music_player", ("музыкальный плеер", "аудиоплеер", "аудио плеер", "плеер музыки"), "Музыкальный плеер", mime="audio/mpeg", categories=("Audio", "Music"),
            binaries=(("rhythmbox",), ("lollypop",), ("totem",))),
    Concept("image_viewer", ("галерея", "галерею", "просмотр изображений", "просмотрщик изображений", "просмотрщик фото", "просмотр фото"),
            "Просмотр изображений", mime="image/png", categories=("Photography", "Viewer"), binaries=(("loupe",), ("eog",), ("shotwell",))),
    Concept("pdf_viewer", ("просмотр документов", "просмотрщик документов", "читалка", "читалку", "пдф", "pdf"), "Просмотр документов", mime="application/pdf", categories=("Viewer",),
            binaries=(("papers",), ("evince",), ("okular",))),
    Concept("mail", ("почта", "почту", "почтовый клиент", "email", "имейл", "мейл", "письма"), "Почта", mime="x-scheme-handler/mailto", categories=("Email",),
            binaries=(("thunderbird",), ("geary",), ("evolution",))),
    Concept("word", ("ворд", "word", "текстовый процессор", "документ ворд"), "Текстовый процессор", mime="application/vnd.oasis.opendocument.text", categories=("WordProcessor",),
            binaries=(("libreoffice", "--writer"), ("onlyoffice-desktopeditors",))),
    Concept("spreadsheet", ("эксель", "excel", "таблицы", "электронные таблицы"), "Электронные таблицы", mime="application/vnd.oasis.opendocument.spreadsheet", categories=("Spreadsheet",),
            binaries=(("libreoffice", "--calc"), ("onlyoffice-desktopeditors",))),
    Concept("presentation", ("пауэр пойнт", "пауэрпойнт", "powerpoint", "power point", "презентации"), "Презентации", mime="application/vnd.oasis.opendocument.presentation",
            categories=("Presentation",), binaries=(("libreoffice", "--impress"), ("onlyoffice-desktopeditors",))),
    Concept("office", ("офис", "либреофис", "libreoffice", "office"), "Офисный пакет", categories=("Office",), binaries=(("libreoffice",),)),
    Concept("archiver", ("архиватор", "винрар", "winrar", "7 зип", "7zip", "7 zip", "менеджер архивов"), "Менеджер архивов", mime="application/zip", categories=("Archiving",),
            binaries=(("file-roller",),)),
    Concept("torrent", ("торрент", "торрент клиент", "torrent"), "Торрент-клиент", mime="application/x-bittorrent", categories=("P2P", "FileTransfer"),
            binaries=(("transmission-gtk",), ("qbittorrent",))),
    Concept("camera", ("камера", "камеру", "веб камера", "веб камеру", "webcam"), "Камера", categories=("Camera",), binaries=(("snapshot",), ("cheese",))),
    Concept("recorder", ("диктофон", "запись голоса", "запись звука", "recorder"), "Диктофон", categories=("Recorder",), binaries=(("gnome-sound-recorder",), ("audacity",))),
    Concept("paint", ("пейнт", "paint", "рисование", "графический редактор", "рисовалка"), "Графический редактор", categories=("RasterGraphics", "2DGraphics"),
            binaries=(("drawing",), ("pinta",), ("gimp",), ("krita",))),
    Concept("photo_editor", ("фотошоп", "photoshop", "редактор фото", "фоторедактор"), "Редактор изображений", categories=("RasterGraphics",), binaries=(("gimp",), ("krita",))),
    Concept("ide", ("редактор кода", "иде", "ide", "среда разработки"), "Среда разработки", categories=("IDE",), binaries=(("code",), ("pycharm-community",))),
    Concept("calendar", ("календарь", "calendar"), "Календарь", categories=("Calendar",), binaries=(("gnome-calendar",),)),
    Concept("clock", ("часы", "будильники", "таймеры", "clocks"), "Часы", categories=("Clock",), binaries=(("gnome-clocks",),)),
    Concept("maps", ("карты", "карту", "maps"), "Карты", categories=("Maps",), binaries=(("gnome-maps",),)),
    Concept("contacts", ("контакты", "contacts"), "Контакты", categories=("ContactManagement",), binaries=(("gnome-contacts",),)),
    Concept("weather", ("погода", "погоду", "weather"), "Погода", categories=("Weather",), binaries=(("gnome-weather",),)),
    Concept("app_store", ("магазин приложений", "центр приложений", "app center", "snap store", "магазин", "установка программ"), "Центр приложений",
            categories=("PackageManager", "SoftwareManagement"), binaries=(("snap-store",), ("gnome-software",))),
    Concept("disks", ("диски", "утилита дисков", "диспетчер дисков", "менеджер дисков", "disks"), "Диски", categories=("Filesystem",), binaries=(("gnome-disks",),)),
    Concept("disk_usage", ("анализатор дисков", "очистка диска", "очистку диска", "disk usage"), "Анализатор использования дисков", binaries=(("baobab",),)),
    Concept("logs", ("журналы", "логи", "системный журнал", "просмотр событий", "logs"), "Журналы", binaries=(("gnome-logs",),)),
    Concept("screenshot", ("ножницы", "снимок экрана", "скриншотер", "screenshot"), "Снимок экрана", binaries=(("flameshot", "gui"), ("gnome-screenshot", "--interactive"))),
    Concept("characters", ("таблица символов", "карта символов", "characters"), "Таблица символов", binaries=(("gnome-characters",), ("gucharmap",))),
    Concept("fonts", ("шрифты", "менеджер шрифтов", "fonts"), "Шрифты", binaries=(("gnome-font-viewer",), ("font-manager",))),
    Concept("scanner", ("сканер", "сканер документов", "scanner"), "Сканер документов", categories=("Scanning",), binaries=(("simple-scan",),)),
    Concept("passwords", ("пароли и ключи", "менеджер паролей", "связка ключей", "пароли"), "Пароли и ключи", binaries=(("seahorse",), ("keepassxc",))),
    Concept("tweaks", ("твики", "дополнительные настройки", "tweaks"), "Доп. настройки GNOME", binaries=(("gnome-tweaks",),)),
    Concept("extensions", ("расширения", "менеджер расширений", "extensions"), "Расширения GNOME", binaries=(("extension-manager",), ("gnome-extensions-app",))),
    Concept("updates", ("обновления", "обновление приложений", "менеджер обновлений", "updates"), "Обновление приложений", binaries=(("update-manager",), ("gnome-software", "--mode=updates"))),
    Concept("backup", ("резервное копирование", "бэкап", "backups"), "Резервное копирование", binaries=(("deja-dup",),)),
    Concept("help", ("справка", "справку", "помощь", "help"), "Справка", binaries=(("yelp",),)),
    Concept("firewall", ("брандмауэр", "защитник", "файрвол", "firewall"), "Межсетевой экран", binaries=(("gufw",), ("gnome-control-center", "privacy"))),
    Concept("dconf", ("редактор реестра", "реестр", "dconf"), "Редактор dconf", binaries=(("dconf-editor",),)),
    Concept("python_console", ("консоль питона", "консоль python", "интерактивный питон"), "Консоль Python", binaries=(("gnome-terminal", "--", "python3"), ("x-terminal-emulator", "-e", "python3"))),
    Concept("games", ("игры", "все игры", "библиотека игр"), "Игры", binaries=(("steam", "steam://open/games"), ("lutris",))),
)


def _xdg_default(mime: str) -> DesktopApp | None:
    if not mime:
        return None
    try:
        res = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=3.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    desktop_id = (res.stdout or "").strip()
    if not desktop_id:
        return None
    for app in desktop_apps():
        if app.desktop_id == desktop_id:
            return app
    return None


def _by_categories(categories: tuple[str, ...]) -> DesktopApp | None:
    if not categories:
        return None
    hits = [a for a in desktop_apps() if any(c in a.categories for c in categories)]
    if not hits:
        return None
    hits.sort(key=lambda a: (0 if "GNOME" in a.categories else 1, 0 if "Core" in a.categories else 1, a.title))
    return hits[0]


def _match_concept(site_norm: str) -> Concept | None:
    stem = _norm_stemmed(site_norm)
    best, best_len = None, 0
    for concept in _CONCEPTS:
        for w in concept.words:
            wn = _norm(w)
            if site_norm == wn or stem == _norm_stemmed(wn):
                return concept
            if len(wn) > best_len and len(wn) >= 4 and (wn in site_norm or _norm_stemmed(wn) in stem):
                best, best_len = concept, len(wn)
    return best


def _concept_plan(concept: Concept, query: str) -> AppPlan:
    app = _xdg_default(concept.mime) or _by_categories(concept.categories)
    if app is not None:
        cmd, how = list(app.cmd), f"{app.title} ({app.desktop_id})"
    else:
        cmd, how = None, ""
        for cand in concept.binaries:
            found = shutil.which(cand[0])
            if found:
                cmd, how = [found, *cand[1:]], cand[0]
                break
        if cmd is None:
            return _store_plan(concept.title, query)
    return AppPlan("app", concept.key, cmd, query, description=f"Открываю {concept.title}: {how}")


# =============================================================================
# 4. Игры Steam (по библиотеке на диске) и прозвища игр
# =============================================================================
_STEAM_LIB_ROOTS = (
    Path.home() / ".steam/steam/steamapps",
    Path.home() / ".local/share/Steam/steamapps",
    Path.home() / "snap/steam/common/.local/share/Steam/steamapps",
    Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps",
)
_STEAM_SKIP = ("proton", "steam linux runtime", "steamworks common", "spacewar", "redistributable")
# Русские прозвища игр -> английское название в Steam (транслитерацией не получить). Это знание имён, а не фразы.
_GAME_NICKNAMES: dict[str, str] = {
    "дота": "dota 2", "доту": "dota 2", "дота 2": "dota 2", "доту 2": "dota 2", "dota": "dota 2", "dota2": "dota 2",
    "кс": "counter-strike 2", "кс 2": "counter-strike 2", "кс го": "counter-strike 2", "ксго": "counter-strike 2", "cs": "counter-strike 2",
    "cs 2": "counter-strike 2", "cs2": "counter-strike 2", "cs go": "counter-strike 2", "csgo": "counter-strike 2", "контра": "counter-strike 2",
    "контру": "counter-strike 2", "контр страйк": "counter-strike 2", "контр страйк 2": "counter-strike 2", "counter strike": "counter-strike 2",
    "counter strike 2": "counter-strike 2", "дедлок": "deadlock", "майн": "minecraft", "майнкрафт": "minecraft", "танки": "world of tanks",
    "мир танков": "world of tanks", "вов": "world of warcraft", "варкрафт": "world of warcraft", "лол": "league of legends",
    "лигу легенд": "league of legends", "пубг": "pubg: battlegrounds", "пабг": "pubg: battlegrounds", "pubg": "pubg: battlegrounds",
    "гта": "grand theft auto v", "гта 5": "grand theft auto v", "gta 5": "grand theft auto v",
    "киберпанк": "cyberpunk 2077", "ведьмак": "the witcher 3: wild hunt", "ведьмак 3": "the witcher 3: wild hunt", "ведьмака 3": "the witcher 3: wild hunt",
    "скайрим": "the elder scrolls v: skyrim special edition", "рдр 2": "red dead redemption 2", "рдр": "red dead redemption 2",
    "элден ринг": "elden ring", "дарк соулс": "dark souls iii", "дарк соулс 3": "dark souls iii", "террарию": "terraria", "террария": "terraria",
    "стардью валли": "stardew valley", "валхейм": "valheim", "раст": "rust", "апекс": "apex legends", "овервотч": "overwatch 2",
    "фортнайт": "fortnite", "валорант": "valorant", "тарков": "escape from tarkov", "фазмофобию": "phasmophobia", "фазмофобия": "phasmophobia",
    "халф лайф": "half-life 2", "портал 2": "portal 2", "тим фортресс": "team fortress 2", "факторио": "factorio", "сатисфактори": "satisfactory",
    "римворлд": "rimworld", "хойку": "hearts of iron iv", "хойка": "hearts of iron iv", "стелларис": "stellaris", "цивилизацию 6": "sid meier's civilization vi",
    "цивилизация 6": "sid meier's civilization vi", "симс 4": "the sims 4", "баттлфилд": "battlefield", "амонг ас": "among us", "роблокс": "roblox",
    "секиро": "sekiro: shadows die twice", "хогвартс легаси": "hogwarts legacy", "старфилд": "starfield", "диабло 4": "diablo iv",
    "балдурс гейт 3": "baldur's gate 3", "сталкер 2": "s.t.a.l.k.e.r. 2: heart of chornobyl", "метро исход": "metro exodus", "геншин": "genshin impact",
    "варфрейм": "warframe", "дейз": "dayz", "арк": "ark: survival evolved", "сабнатику": "subnautica", "сабнатика": "subnautica",
    "селесту": "celeste", "холлоу найт": "hollow knight", "капхед": "cuphead", "андертейл": "undertale", "дум": "doom eternal", "квейк": "quake",
    "етс 2": "euro truck simulator 2", "дальнобойщики": "euro truck simulator 2", "форзу": "forza horizon 5", "фифу": "ea sports fc 24",
    "мортал комбат": "mortal kombat 11", "теккен": "tekken 8", "осу": "osu!", "геометри даш": "geometry dash", "зомбоид": "project zomboid",
    "рафт": "raft", "сити скайлайнс": "cities: skylines ii", "эпоху империй": "age of empires iv", "варкрафт 3": "warcraft iii", "старкрафт 2": "starcraft ii",
    "герои 3": "heroes of might and magic iii", "тотал вар": "total war: warhammer iii", "баннерлорд": "mount & blade ii: bannerlord",
    "вар тандер": "war thunder", "кроссаут": "crossout", "марвел райвалс": "marvel rivals", "стандофф": "standoff 2", "бравл старс": "brawl stars",
}


@functools.lru_cache(maxsize=1)
def steam_games() -> tuple[tuple[str, str, str], ...]:
    """(название, appid, папка установки) всех установленных игр Steam (Native / Snap / Flatpak + дополнительные библиотеки)."""
    roots = list(_STEAM_LIB_ROOTS)
    for r in list(roots):
        vdf = r / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                    extra = Path(m.group(1)) / "steamapps"
                    if extra.is_dir() and extra not in roots:
                        roots.append(extra)
            except OSError:
                pass
    out, seen = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        for acf in sorted(root.glob("appmanifest_*.acf")):
            try:
                text = acf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', text)
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            idir = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not (name and appid):
                continue
            n = name.group(1)
            if any(k in n.lower() for k in _STEAM_SKIP) or appid.group(1) in seen:
                continue
            seen.add(appid.group(1))
            out.append((n, appid.group(1), idir.group(1) if idir else n))
    return tuple(out)


def _game_canonical(spoken: str) -> str:
    n = re.sub(r"^(?:игру|игра|в игру|в)\s+", "", _norm(spoken)).strip()
    return _GAME_NICKNAMES.get(n, n)


def find_steam_game(spoken: str, min_score: float = 0.80) -> tuple[str, str, str] | None:
    canon = _game_canonical(spoken)
    if not canon:
        return None
    best, best_score = None, 0.0
    for name, appid, idir in steam_games():
        score = max(name_similarity(canon, c) for c in (name, idir, re.sub(r"[^a-z0-9 ]", " ", _norm(name))) if c)
        if score > best_score:
            best, best_score = (name, appid, idir), score
    return best if best_score >= min_score else None


def is_known_game(spoken: str) -> bool:
    n = re.sub(r"^(?:игру|игра|в игру)\s+", "", _norm(spoken)).strip()
    return n in _GAME_NICKNAMES or any(name_similarity(n, v) >= 0.9 for v in set(_GAME_NICKNAMES.values()))


def _steam_cmd() -> list[str]:
    app = find_desktop_app("steam")
    if app is not None and shutil.which(Path(app.cmd[0]).name):
        return list(app.cmd)
    for exe in ("steam", "/usr/games/steam", "/snap/bin/steam"):
        found = shutil.which(exe) if not exe.startswith("/") else (exe if Path(exe).exists() else None)
        if found:
            return [found]
    return ["steam"]


def make_game_plan(spoken: str) -> AppPlan | None:
    game = find_steam_game(spoken)
    if game is not None:
        name, appid, _ = game
        return AppPlan("app_uri", f"steam:{appid}", [*_steam_cmd(), f"steam://rungameid/{appid}"], name,
                       description=f"Запускаю игру «{name}» через Steam (steam://rungameid/{appid})")
    if is_known_game(spoken):
        canon = _game_canonical(spoken)
        return AppPlan("app_uri", "steam:store", [*_steam_cmd(), f"steam://storesearch/{quote(canon)}"], canon,
                       description=f"Игра «{canon}» не установлена: открываю её в магазине Steam")
    return None


# =============================================================================
# 5. Действия внутри приложения: Steam / Spotify / Telegram (URI), терминал и редактор «в папке X», сортировка в Files
# =============================================================================
_STEAM_SECTIONS = {
    "библиотеку": "steam://open/games", "библиотека": "steam://open/games", "библиотеку игр": "steam://open/games", "мои игры": "steam://open/games",
    "список игр": "steam://open/games", "library": "steam://open/games", "магазин": "steam://store", "store": "steam://store",
    "список желаемого": "steam://url/StoreWishlist", "желаемое": "steam://url/StoreWishlist", "wishlist": "steam://url/StoreWishlist",
    "друзья": "steam://friends", "список друзей": "steam://friends", "friends": "steam://friends", "загрузки": "steam://open/downloads",
    "downloads": "steam://open/downloads", "настройки": "steam://open/settings", "инвентарь": "steam://url/CommunityInventory",
    "inventory": "steam://url/CommunityInventory", "мастерскую": "steam://url/SteamWorkshop", "workshop": "steam://url/SteamWorkshop",
    "сообщество": "steam://url/SteamIDControlPage", "community": "steam://url/SteamIDControlPage", "профиль": "steam://url/SteamIDMyProfile",
    "скриншоты": "steam://open/screenshots", "серверы": "steam://open/servers", "big picture": "steam://open/bigpicture",
}


def _app_kind(exe: str, desktop_id: str = "") -> str:
    """Какое из «особых» приложений запущено: steam | spotify | telegram | terminal | vscode | files | '' (по имени бинарника)."""
    key = f"{Path(exe).name} {desktop_id}".lower()
    for kind, marks in (("steam", ("steam",)), ("spotify", ("spotify",)), ("telegram", ("telegram",)),
                        ("terminal", ("gnome-terminal", "kgx", "konsole", "x-terminal-emulator", "tilix", "alacritty", "kitty")),
                        ("vscode", ("code", "codium")), ("files", ("nautilus", "nemo", "thunar", "dolphin"))):
        if any(m in key for m in marks):
            return kind
    return ""


def _clean_app_query(query: str) -> str:
    q = " ".join((query or "").split())
    return re.sub(r"^(?:музыку|песню|трек|альбом|исполнителя|группу|игру|канал|чат)\s+", "", q, flags=re.I).strip()


def _apply_app_query(kind: str, title: str, base_cmd: list[str], query: str, home: Path) -> AppPlan:
    q = " ".join((query or "").split())
    if not q:
        return AppPlan("app", title, base_cmd, description=f"Запускаю {title}: {' '.join(base_cmd)}")
    q_low = _norm(q)
    if kind == "steam":
        clean_q = _clean_app_query(q)
        installed = find_steam_game(clean_q) if _norm(clean_q) not in _STEAM_SECTIONS else None
        uri = _STEAM_SECTIONS.get(_norm(clean_q), f"steam://rungameid/{installed[1]}" if installed else f"steam://storesearch/{quote(clean_q)}")
        return AppPlan("app_uri", "steam", [*base_cmd, uri], clean_q, description=f"Открываю Steam ({clean_q} -> {uri})")
    if kind == "spotify":
        clean_q = _clean_app_query(q)
        uri = f"spotify:search:{quote(clean_q)}"
        return AppPlan("app_uri", "spotify", [*base_cmd, uri], clean_q, description=f"Открываю Spotify и ищу «{clean_q}» ({uri})")
    if kind == "telegram":
        clean_q = _clean_app_query(q)
        return AppPlan("app_uri", "telegram", base_cmd, clean_q, description=f"Открываю Telegram Desktop (запрос: «{clean_q}»)")
    if kind in ("terminal", "vscode") and ("папк" in q_low or "каталог" in q_low or q_low == "проект"):
        proj_dir = _find_projects_dir(home)
        named = re.sub(r"^(?:в\s+)?(?:папке|папку|папка|каталоге|каталог)\s+", "", q_low).strip()
        if named and named not in ("проекта", "проект", "проектов"):
            found = find_local_item(named, home)
            if found is not None:
                proj_dir = found if found.is_dir() else found.parent
        if kind == "vscode":
            return AppPlan("app", title, [*base_cmd, str(proj_dir)], q, description=f"Открываю {title} в папке {proj_dir}")
        return AppPlan("app", title, [*base_cmd, f"--working-directory={proj_dir}"], q, description=f"Открываю терминал в папке {proj_dir}")
    if kind == "files":
        return AppPlan("folder", str(home), [*base_cmd, str(home)], q, pre_cmds=_nautilus_pre_commands(q_low), description=f"Открываю {title} ({q})")
    return AppPlan("app", title, base_cmd, q, description=f"Запускаю {title} (параметр: «{q}»): {' '.join(base_cmd)}")


def _nautilus_pre_commands(query_norm: str) -> tuple[tuple[str, ...], ...]:
    """Сортировка / скрытые файлы в GNOME Files по слоту query («отсортируй по дате», «покажи скрытые файлы»)."""
    cmds: list[tuple[str, ...]] = []
    for word, order, reverse in (("по дате", "mtime", "true"), ("по времени", "mtime", "true"), ("по имени", "name", "false"),
                                 ("по алфавиту", "name", "false"), ("по размеру", "size", "true"), ("по типу", "type", "false")):
        if word in query_norm:
            cmds.append(("gsettings", "set", "org.gnome.nautilus.preferences", "default-sort-order", order))
            cmds.append(("gsettings", "set", "org.gnome.nautilus.preferences", "default-sort-in-reverse-order", reverse))
            break
    if "скрытые файлы" in query_norm:
        cmds.append(("gsettings", "set", "org.gnome.gtk4.Settings.FileChooser", "show-hidden", "true"))
    return tuple(cmds)


# =============================================================================
# 6. Папки XDG и файлы пользователя
# =============================================================================
def xdg_user_dirs(home: Path | None = None) -> dict[str, Path]:
    home = home or Path.home()
    defaults = {
        "DESKTOP": home / ("Рабочий стол" if (home / "Рабочий стол").is_dir() else "Desktop"),
        "DOWNLOAD": home / ("Загрузки" if (home / "Загрузки").is_dir() else "Downloads"),
        "DOCUMENTS": home / ("Документы" if (home / "Документы").is_dir() else "Documents"),
        "PICTURES": home / ("Изображения" if (home / "Изображения").is_dir() else "Pictures"),
        "MUSIC": home / ("Музыка" if (home / "Музыка").is_dir() else "Music"),
        "VIDEOS": home / ("Видео" if (home / "Видео").is_dir() else "Videos"),
    }
    cfg = home / ".config/user-dirs.dirs"
    if cfg.is_file():
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                key = k.replace("XDG_", "").replace("_DIR", "").strip()
                val = v.strip().strip('"').replace("$HOME", str(home))
                if key in defaults and val:
                    defaults[key] = Path(val)
        except OSError:
            pass
    return defaults


def _find_projects_dir(home: Path) -> Path:
    for name in ("Projects", "projects", "Проекты", "проекты", "dev", "work", "Работа"):
        if (home / name).is_dir():
            return home / name
    return home


def _find_removable_media() -> Path:
    user = os.environ.get("USER") or Path.home().name
    for base in (Path(f"/media/{user}"), Path(f"/run/media/{user}"), Path("/mnt")):
        if base.is_dir():
            subdirs = [p for p in sorted(base.iterdir()) if p.is_dir()]
            return subdirs[0] if subdirs else base
    return Path(f"/media/{user}")


def _latest_entry(dirs: list[Path], want_dir: bool) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for item in d.iterdir():
                if item.name.startswith("."):
                    continue
                if (want_dir and item.is_dir()) or (not want_dir and item.is_file()):
                    candidates.append((item.stat().st_mtime, item))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates)[1]


_STANDARD_FOLDERS: list[tuple[tuple[str, ...], str, str]] = [
    (("загрузк", "скачанн", "downloads"), "DOWNLOAD", "папку «Загрузки»"),
    (("мои документ", "документ", "documents"), "DOCUMENTS", "папку «Документы»"),
    (("рабочий стол", "рабочем стол", "desktop"), "DESKTOP", "папку «Рабочий стол»"),
    (("скриншот", "снимки экрана", "screenshots"), "SCREENSHOTS", "папку «Снимки экрана»"),
    (("семейн фото", "фотограф", "фотк", "фото", "изображен", "картинк", "рисунк", "pictures"), "PICTURES", "папку «Изображения»"),
    (("музык", "music"), "MUSIC", "папку «Музыка»"),
    (("видео", "видос", "фильм", "videos"), "VIDEOS", "папку «Видео»"),
    (("домашн папк", "диск с", "диск c", "home"), "HOME", "домашнюю папку"),
    (("корзин", "trash"), "TRASH", "«Корзину»"),
    (("флешк", "диск д", "диск d", "внешний диск"), "REMOVABLE", "внешний накопитель / флешку"),
]
_FILE_KIND_EXTS: dict[str, tuple[str, ...]] = {"пдф": (".pdf",), "pdf": (".pdf",), "ворд": (".docx", ".odt", ".doc"), "эксель": (".xlsx", ".ods", ".csv", ".xls")}
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "snap", "site-packages", "dist", "build"}


def _file_manager_cmd(target: str) -> list[str]:
    for fm in ("nautilus", "xdg-open", "nemo", "dolphin", "thunar"):
        path = shutil.which(fm)
        if path:
            return [path, target]
    return ["nautilus", target]


def _search_user_path(name_norm: str, home: Path, dirs: dict[str, Path]) -> tuple[str, Path] | None:
    """Файл или папка пользователя по имени: стандартные папки и их подпапки (глубина 2)."""
    search_roots = [Path.cwd(), home, dirs["DESKTOP"], dirs["DOCUMENTS"], dirs["DOWNLOAD"], dirs["PICTURES"], dirs["MUSIC"], dirs["VIDEOS"], _find_projects_dir(home)]
    spoken_dot = re.sub(r"\s+точка\s+пай$", ".py", name_norm)
    if spoken_dot != name_norm:
        for root in search_roots:
            if (root / spoken_dot).exists():
                return ("file", root / spoken_dot)
        return ("file", home / spoken_dot)
    words = name_norm.split()
    wanted_exts: tuple[str, ...] = ()
    base_name = name_norm
    if len(words) >= 2 and words[-1] in _FILE_KIND_EXTS:
        wanted_exts, base_name = _FILE_KIND_EXTS[words[-1]], " ".join(words[:-1])
    base_stem = _norm_stemmed(base_name)
    levels = [list(search_roots)]
    second: list[Path] = []
    for root in search_roots:
        try:
            if root.is_dir():
                second.extend(sub for sub in root.iterdir() if sub.is_dir() and not sub.name.startswith(".") and sub.name not in _SKIP_DIRS)
        except OSError:
            pass
    levels.append(second)
    for roots_level in levels:
        for root in roots_level:
            if not root.is_dir():
                continue
            try:
                for item in root.iterdir():
                    if item.name.startswith("."):
                        continue
                    base = item.stem if item.is_file() else item.name
                    if _norm_stemmed(base) == base_stem or base_name in (_norm(item.name), _norm(base)):
                        if wanted_exts and item.is_file() and item.suffix.lower() not in wanted_exts:
                            continue
                        return ("folder" if item.is_dir() else "file", item)
            except OSError:
                continue
    if wanted_exts:
        return ("file", dirs["DOCUMENTS"] / f"{base_name}{wanted_exts[0]}")
    return None


def find_local_item(name: str, home: Path | None = None) -> Path | None:
    """Существующий файл или папка пользователя по сказанному имени («открой traning ml» без слова «папку»)."""
    home = home or Path.home()
    n = _norm(name)
    if not n:
        return None
    found = _search_user_path(n, home, xdg_user_dirs(home))
    return found[1] if found else None


def _match_standard_folder(site_norm: str, query: str, home: Path, dirs: dict[str, Path]) -> AppPlan | None:
    pre_cmds = _nautilus_pre_commands(_norm(query))
    if site_norm in ("последнюю папку", "последняя папка"):
        target_dir = _latest_entry([dirs["DOWNLOAD"], dirs["DOCUMENTS"], dirs["DESKTOP"], home], want_dir=True) or dirs["DOWNLOAD"]
        return AppPlan("folder", str(target_dir), _file_manager_cmd(str(target_dir)), query, pre_cmds, f"Открываю последнюю изменённую папку: {target_dir}")
    if site_norm in ("последний файл", "последний документ", "последний скачанный файл"):
        target_file = _latest_entry([dirs["DOWNLOAD"], dirs["DOCUMENTS"], dirs["DESKTOP"], home], want_dir=False) or dirs["DOWNLOAD"]
        return AppPlan("file", str(target_file), [shutil.which("xdg-open") or "xdg-open", str(target_file)], query, description=f"Открываю последний изменённый файл: {target_file}")
    for keywords, key, label in _STANDARD_FOLDERS:
        if any(kw in site_norm for kw in keywords):
            if key == "TRASH":
                return AppPlan("folder", "trash:///", _file_manager_cmd("trash:///"), query, description="Открываю «Корзину» в GNOME Files")
            if key == "HOME":
                target_path = home
            elif key == "REMOVABLE":
                target_path = _find_removable_media()
            elif key == "SCREENSHOTS":
                shots = dirs["PICTURES"] / "Снимки экрана"
                if not shots.is_dir() and (dirs["PICTURES"] / "Screenshots").is_dir():
                    shots = dirs["PICTURES"] / "Screenshots"
                target_path = shots if shots.is_dir() else dirs["PICTURES"]
            else:
                target_path = dirs[key]
            return AppPlan("folder", str(target_path), _file_manager_cmd(str(target_path)), query, pre_cmds, f"Открываю {label}: {target_path}")
    return None


def _match_user_path(site_norm: str, query: str, home: Path, dirs: dict[str, Path]) -> AppPlan | None:
    found = _search_user_path(site_norm, home, dirs)
    if found is None:
        return None
    kind, path = found
    if kind == "file":
        return AppPlan("file", str(path), [shutil.which("xdg-open") or "xdg-open", str(path)], query, description=f"Открываю файл «{path.name}»: {path}")
    return AppPlan("folder", str(path), _file_manager_cmd(str(path)), query, _nautilus_pre_commands(_norm(query)), f"Открываю папку «{path.name}»: {path}")


# =============================================================================
# 7. План запуска и выполнение
# =============================================================================
def _store_plan(name: str, query: str = "") -> AppPlan:
    store = shutil.which("snap-store") or shutil.which("gnome-software")
    search = name.strip()
    if store:
        return AppPlan("app", "app_store_search", [store, f"--search={search}"], query,
                       description=f"Программа «{search}» не найдена на компьютере: ищу её в Центре приложений")
    return AppPlan("app", "app_store_search", ["xdg-open", f"https://snapcraft.io/search?q={quote(search)}"], query,
                   description=f"Программа «{search}» не найдена: открываю поиск в каталоге Snap")


def resolve_program(name: str) -> tuple[str, list[str], str] | None:
    """Установленная программа по сказанному имени -> (название, команда, вид). Без словаря: .desktop + PATH + транслитерация."""
    app = find_desktop_app(name)
    if app is not None:
        return app.title, list(app.cmd), _app_kind(app.cmd[0], app.desktop_id)
    exe = _which_translit(name)
    if exe:
        return Path(exe).name, [exe], _app_kind(exe)
    return None


def make_app_plan(site: str, query: str = "", home: Path | None = None, action: str = "") -> AppPlan | None:
    """Строит план открытия программы, настроек, папки или файла по словам пользователя."""
    site_norm = _norm(site)
    if not site_norm:
        return None
    home = home or Path.home()
    dirs = xdg_user_dirs(home)
    site_clean = re.sub(r"^(?:приложение|программа|программу|утилита|утилиту|папка|папку|каталог|файл|документ|раздел|вкладка|вкладку|игру|игра)\s+", "", site_norm).strip() or site_norm
    query_norm = _norm(query)
    is_folder_phrase = site_clean != site_norm and site_norm.split()[0] in ("папка", "папку", "каталог", "файл", "документ")

    plan = _match_gnome_settings(site_clean, query_norm) or _match_gnome_shell(site_clean)
    if plan is not None:
        return plan
    plan = _match_standard_folder(site_clean, query, home, dirs)
    if plan is not None:
        return plan
    if is_folder_phrase:
        plan = _match_user_path(site_clean, query, home, dirs)
        if plan is not None:
            return plan
    concept = _match_concept(site_clean)
    if concept is not None:
        cplan = _concept_plan(concept, query)
        if cplan.target != "app_store_search":
            kind = _app_kind(cplan.cmd[0]) if cplan.cmd else ""
            return _apply_app_query(kind, concept.title, cplan.cmd, query, home) if (query and kind) else cplan
    prog = resolve_program(site_clean)
    if prog is not None:
        title, cmd, kind = prog
        return _apply_app_query(kind, title, cmd, query, home)
    game_plan = make_game_plan(site_clean)
    if game_plan is not None:
        return game_plan
    plan = _match_user_path(site_clean, query, home, dirs) or _match_user_path(site_norm, query, home, dirs)
    if plan is not None:
        return plan
    if concept is not None:
        return _concept_plan(concept, query)
    return _store_plan(site.strip(), query)


def resolve_close_patterns(spoken: str) -> list[str]:
    """Шаблоны для поиска процессов по сказанному имени программы или игры (для «закрой X»)."""
    n = re.sub(r"^(?:приложение|программу|программа|игру|игра|окно|окно с|процесс|прогу|вкладку с)\s+", "", _norm(spoken)).strip()
    pats: list[str] = []
    concept = _match_concept(n)
    if concept is not None and concept.key == "browser":
        return sorted({re.escape(Path(a.cmd[0]).name) for a in desktop_apps() if "WebBrowser" in a.categories}) or ["firefox", "chrome", "chromium"]
    if concept is not None:
        app = _xdg_default(concept.mime) or _by_categories(concept.categories)
        if app is not None:
            pats.append(re.escape(Path(app.cmd[0]).name))
        for cand in concept.binaries:
            if shutil.which(cand[0]):
                pats.append(re.escape(cand[0]))
    prog = resolve_program(n)
    if prog is not None:
        pats.append(re.escape(Path(prog[1][0]).name))
        app = find_desktop_app(n)
        if app is not None and ("flatpak" in app.path or "snapd" in app.path):
            pats.append(re.escape(app.desktop_id.rsplit(".desktop", 1)[0]))
    game = find_steam_game(n)
    if game is not None:
        pats.append(re.escape(f"steamapps/common/{game[2]}"))
    if not pats and re.fullmatch(r"[a-z0-9][a-z0-9 ._+-]{1,40}", n):
        pats.append(re.escape(n.replace(" ", "-")))
    seen, out = set(), []
    for p_ in pats:
        if p_ not in seen:
            seen.add(p_)
            out.append(p_)
    return out


def _spawn_detached(cmd: list[str], wait_seconds: float = LAUNCH_WAIT_SECONDS) -> str | None:
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
            try:
                code = proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                return None
            if code == 0:
                return None
            err.seek(0)
            return f"{cmd[0]} завершился с кодом {code}: {err.read().decode(errors='replace').strip()[-300:]}"
    except OSError as exc:
        return f"{cmd[0]}: {exc}"


def execute_app_plan(plan: AppPlan, dry_run: bool = False) -> str:
    if dry_run:
        return f"[dry-run] {plan.description} -> {shlex.join(plan.cmd)}"
    for pre in plan.pre_cmds:
        try:
            subprocess.run(list(pre), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
        except OSError:
            pass
    if plan.kind == "folder" and not plan.target.startswith("trash://") and not Path(plan.target).exists():
        try:
            Path(plan.target).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    error = _spawn_detached(plan.cmd)
    if error is not None:
        raise AppLaunchError(error)
    return plan.description


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if not args:
        print("Использование: python3 app_launcher.py [--dry-run] <приложение_или_папка> [запрос]")
        return 1
    plan = make_app_plan(args[0], " ".join(args[1:]))
    if plan is None:
        print("Открывать нечего.")
        return 1
    try:
        print(execute_app_plan(plan, dry_run=dry_run))
        return 0
    except AppLaunchError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
