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

"""
from __future__ import annotations

import configparser
import functools
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .spoken import words_to_digits

LAUNCH_WATCH_SECONDS = 2.5     # столько после запуска следим в фоне, не упала ли программа (ответ этого не ждёт)
late_failure: Callable[[str], None] = print     # куда сказать, если запуск всё-таки не удался (Джарвис — голосом)


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


def _translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in _norm(text))


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
    digits = words_to_digits(a)                     # «дота два» = «Dota 2», «эф один» = «F1»
    if digits != a:
        return max(name_similarity(digits, candidate), _name_similarity_words(a, b))
    return _name_similarity_words(a, b)


MIN_SOUND_LETTERS = 4      # «кс», «зум»: в 2–3 буквах слишком мало звука — на слух похожи на что угодно («кс» ~ «c s»)


def _name_similarity_words(a: str, b: str) -> float:
    from .names import model                                 # noqa: PLC0415 — веса грузятся при первом сравнении
    best = 0.0
    ta = _translit(a)
    by_sound = len(ta.replace(" ", "")) >= MIN_SOUND_LETTERS
    for variant in _name_variants(b):
        tb = _translit(variant)
        score = model().similarity(a, variant) if by_sound else 0.0      # на слух — обученная модель («каплец» = caplets)
        if ta and tb and (tb.startswith(ta) or ta.startswith(tb)) and min(len(ta), len(tb)) >= 4:
            score = max(score, 0.9)
        ja, jb = ta.replace(" ", ""), tb.replace(" ", "")          # распознавание делит слова: «pass words» = Passwords
        if " " in ta and len(ja) >= 5 and (jb == ja or jb.startswith(ja)):
            score = max(score, 0.9)
        best = max(best, score)
    return best


# =============================================================================
# 1. Панели настроек Ubuntu 24.04 (gnome-control-center): имена панелей
# =============================================================================
def browser_close_patterns() -> list[str]:
    """Процессы браузеров этой машины (категория WebBrowser в .desktop) — для «закрой браузер»."""
    return sorted({re.escape(Path(a.cmd[0]).name) for a in desktop_apps() if "WebBrowser" in a.categories}) or ["firefox"]


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


def find_steam_game(spoken: str, min_score: float = 0.80) -> tuple[str, str, str] | None:
    """Установленная игра Steam по сказанному названию (транслитерация и звучание: «дота два» = Dota 2)."""
    canon = re.sub(r"^(?:игру|игра|в игру|в)\s+", "", _norm(spoken)).strip()
    if not canon:
        return None
    best, best_score = None, 0.0
    for name, appid, idir in steam_games():
        score = max(name_similarity(canon, c) for c in (name, idir, re.sub(r"[^a-z0-9 ]", " ", _norm(name))) if c)
        if score > best_score:
            best, best_score = (name, appid, idir), score
    return best if best_score >= min_score else None


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
    return None


# =============================================================================
# 5. Действия внутри приложения: Steam / Spotify / Telegram (URI), терминал и редактор «в папке X», сортировка в Files
# =============================================================================
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
        installed = find_steam_game(clean_q)
        uri = f"steam://rungameid/{installed[1]}" if installed else f"steam://storesearch/{quote(clean_q)}"
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
            found = find_local_item(named, home, fuzzy=True)
            if found is not None:
                proj_dir = found if found.is_dir() else found.parent
        if kind == "vscode":
            return AppPlan("app", title, [*base_cmd, str(proj_dir)], q, description=f"Открываю {title} в папке {proj_dir}")
        return AppPlan("app", title, [*base_cmd, f"--working-directory={proj_dir}"], q, description=f"Открываю терминал в папке {proj_dir}")
    if kind == "files":
        return AppPlan("folder", str(home), [*base_cmd, str(home)], q, pre_cmds=_nautilus_pre_commands(q_low), description=f"Открываю {title} ({q})")
    # Остальное программа сама не принимает: честно говорим, что параметр не передан (иначе LLM решит, что, например,
    # «sudo apt clean» уже введено в терминал)
    return AppPlan("app", title, base_cmd, q,
                   description=f"Запускаю {title}: {' '.join(base_cmd)}. «{q}» программе не передано — это нужно сделать вручную")


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


# Падежные окончания: «видео» совпадает с «видео», «видосы», но не с «видеоплеер»; «фото» — не с «фотошоп»
_RU_ENDINGS = (r"(?:ами|ями|ого|его|ому|ему|ой|ей|ом|ем|ах|ях|ам|ям|ов|ев|ии|ий|ый|ым|им|ых|их|ые|ие|ая|яя|ое|ее|ую|юю|"
               r"а|я|ы|и|у|ю|е|о)?")


def _word_starts(text: str, key: str) -> bool:
    """key (основа, можно из нескольких слов) стоит в text с начала слова и заканчивается окончанием, а не другим словом."""
    body = r"\s+".join(re.escape(part) + _RU_ENDINGS for part in key.split())
    return re.search(r"(?<!\w)" + body + r"(?!\w)", text) is not None


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


def _search_user_path(name_norm: str, home: Path, dirs: dict[str, Path], fuzzy: bool = True) -> tuple[str, Path] | None:
    """Файл или папка пользователя по имени: стандартные папки и их подпапки (глубина 2). fuzzy — и похожие по звучанию
    имена («эдьюкейшн» = education), если точного совпадения нет."""
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
    if fuzzy:
        for roots_level in levels:
            items: list[Path] = []
            for root in roots_level:
                try:
                    if root.is_dir():
                        items.extend(root.iterdir())
                except OSError:
                    continue
            hit = best_spoken_match(base_name, items, exts=wanted_exts)
            if hit is not None:
                return ("folder" if hit.is_dir() else "file", hit)
    if wanted_exts:
        return ("file", dirs["DOCUMENTS"] / f"{base_name}{wanted_exts[0]}")
    return None


def find_local_item(name: str, home: Path | None = None, fuzzy: bool = False) -> Path | None:
    """Существующий файл или папка пользователя по сказанному имени («открой traning ml» без слова «папку»).
    fuzzy — и похожие по звучанию имена; только там, где ясно, что речь о папке («терминал в папке эдьюкейшн»):
    в «открой ютуб» иначе нашлась бы папка YouTube вместо сайта."""
    home = home or Path.home()
    n = _norm(name)
    if not n:
        return None
    found = _search_user_path(n, home, xdg_user_dirs(home), fuzzy=fuzzy)
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
        if any(_word_starts(site_norm, kw) for kw in keywords):
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


# --- имя файла или папки, сказанное голосом: «эдьюкейшн» = education, «мейн.py» = main.py, «трейнинг ml» = traning ml ---
FUZZY_NAME_MIN = 0.82
_EXT_SAME = {"jpeg": "jpg", "htm": "html", "yml": "yaml", "tgz": "gz"}


def spoken_name_score(spoken: str, item: Path, is_file: bool | None = None) -> float:
    """0..1: похоже ли сказанное имя на имя файла/папки на диске (на слух — модель jarvis/names.py; расширение)."""
    s = _norm(spoken)
    if not s:
        return 0.0
    if is_file is None:
        is_file = item.is_file()
    m = re.fullmatch(r"(.+?)\.([a-z0-9]{1,5})", s)
    s_stem, s_ext = (m.group(1), m.group(2)) if m else (s, "")
    i_stem, i_ext = (item.stem, item.suffix[1:].lower()) if (is_file and item.suffix) else (item.name, "")
    if s_ext:
        a_ext, b_ext = _EXT_SAME.get(s_ext, s_ext), _EXT_SAME.get(i_ext, i_ext)
        # расширение: точное или начало («.pd» = .pdf); на слух его уже перевёл spoken.py («пэдээф» -> pdf)
        if not b_ext or (a_ext != b_ext and not (len(a_ext) >= 2 and b_ext.startswith(a_ext))):
            return 0.0
        from .names import model                             # noqa: PLC0415
        # расширение совпало — короткое имя тоже можно сравнить на слух: «ран.sh» = run.sh
        return max(name_similarity(s_stem, i_stem), model().similarity(s_stem, i_stem))
    return name_similarity(s_stem, i_stem)


def best_spoken_match(spoken: str, items, want_dir: bool | None = None, exts: tuple[str, ...] = ()) -> Path | None:
    """Самый похожий на сказанное файл/папка из items (не ниже FUZZY_NAME_MIN); при равенстве — первый по порядку."""
    best, best_score = None, FUZZY_NAME_MIN
    for item in items:
        if item.name.startswith("."):
            continue
        try:
            is_dir = item.is_dir()
        except OSError:
            continue
        if (want_dir is True and not is_dir) or (want_dir is False and is_dir):
            continue
        if exts and not is_dir and item.suffix.lower() not in exts:
            continue
        score = spoken_name_score(spoken, item, is_file=not is_dir)
        if score > best_score + 1e-9:
            best, best_score = item, score
    return best


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


def _exact_user_path(site: str, home: Path) -> Path | None:
    """«/home/user/Desktop/x/README.md» или «~/Документы/a.pdf» — существующий путь внутри домашней папки или на
    сменном носителе; иначе None (системные файлы так не открываем, а «desktop» в пути — не папка «Рабочий стол»)."""
    raw = (site or "").strip().strip("«»\"'")
    if not raw.startswith(("/", "~")):
        return None
    try:
        path = Path(raw).expanduser()
        real = path.resolve()
        if not path.exists():
            return None
        roots = [home.resolve(), Path("/media").resolve(), Path(f"/run/media/{home.name}").resolve()]
    except (OSError, RuntimeError):
        return None
    return path if any(real == r or r in real.parents for r in roots) else None


def make_app_plan(site: str, query: str = "", home: Path | None = None) -> AppPlan | None:
    """Строит план открытия программы, настроек, папки или файла по словам пользователя."""
    site_norm = _norm(site)
    if not site_norm:
        return None
    home = home or Path.home()
    dirs = xdg_user_dirs(home)
    exact = _exact_user_path(site, home)              # полный путь (его передаёт второй слой после find_files)
    if exact is None and site.strip().startswith(("/", "~")):
        raise AppLaunchError(f"нет такого файла или папки (или он вне домашней папки): {site.strip()}")
    if exact is not None:
        if exact.is_dir():
            return AppPlan("folder", str(exact), _file_manager_cmd(str(exact)), query, _nautilus_pre_commands(_norm(query)),
                           f"Открываю папку «{exact.name}»: {exact}")
        return AppPlan("file", str(exact), [shutil.which("xdg-open") or "xdg-open", str(exact)], query,
                       description=f"Открываю файл «{exact.name}»: {exact}")
    site_clean = re.sub(r"^(?:приложение|программа|программу|утилита|утилиту|папка|папку|каталог|файл|документ|раздел|вкладка|вкладку|игру|игра)\s+", "", site_norm).strip() or site_norm
    query_norm = _norm(query)
    is_folder_phrase = site_clean != site_norm and site_norm.split()[0] in ("папка", "папку", "каталог", "файл", "документ")

    plan = _match_standard_folder(site_clean, query, home, dirs)
    if plan is not None:
        return plan
    if is_folder_phrase:
        plan = _match_user_path(site_clean, query, home, dirs)
        if plan is not None:
            return plan
    panel = _settings_panel(site_clean)
    if panel is not None:
        return panel
    prog = resolve_program(site_clean)
    if prog is not None:
        title, cmd, kind = prog
        return _apply_app_query(kind, title, cmd, query, home)
    game_plan = make_game_plan(site_clean)
    if game_plan is not None:
        return game_plan
    plan = (_match_user_path(site_clean, query, home, dirs) or _match_user_path(site_norm, query, home, dirs)
            or _semantic_plan(site_clean, query, home))
    if plan is not None:
        return plan
    return _store_plan(site.strip(), query)       # не нашлось: диспетчер отдаст фразу второму слою


def _settings_panel(name: str) -> AppPlan | None:
    """Панель gnome-control-center по её имени в системе («bluetooth», «Wi-Fi», «Displays») — так её открывает второй слой."""
    n = _norm(name).replace("-", " ")
    for p in sorted(Path("/usr/share/applications").glob("gnome-*-panel.desktop")):
        panel = p.stem[len("gnome-"):-len("-panel")]
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(p, encoding="utf-8")
            title = cp["Desktop Entry"].get("Name", panel)
        except Exception:                                         # noqa: BLE001
            continue
        if n in (panel.replace("-", " "), _norm(title).replace("-", " "), f"{panel} settings", f"настройки {panel}"):
            gcc = shutil.which("gnome-control-center") or "gnome-control-center"
            return AppPlan("gnome_settings", panel, [gcc, panel], description=f"Открываю настройки «{title}» (gnome-control-center {panel})")
    return None


def _semantic_plan(site: str, query: str, home: Path) -> AppPlan | None:
    """Панель настроек, программа или игра этой машины, ближайшая по смыслу или звучанию (jarvis/semantic.py)."""
    from . import semantic                                     # noqa: PLC0415
    hit = semantic.search(site)
    if hit is None:
        return None
    entry, _score = hit
    if entry.kind == "panel":
        gcc = shutil.which("gnome-control-center") or "gnome-control-center"
        return AppPlan("gnome_settings", entry.key, [gcc, entry.key], query,
                       description=f"Открываю настройки «{entry.title}» (gnome-control-center {entry.key})")
    if entry.kind == "game":
        return AppPlan("app_uri", f"steam:{entry.key}", [*_steam_cmd(), f"steam://rungameid/{entry.key}"], entry.title,
                       description=f"Запускаю игру «{entry.title}» через Steam (steam://rungameid/{entry.key})")
    app = next((a for a in desktop_apps() if a.desktop_id == entry.key), None)
    if app is None:
        return None
    return _apply_app_query(_app_kind(app.cmd[0], app.desktop_id), app.title, list(app.cmd), query, home)


def resolve_close_patterns(spoken: str) -> list[str]:
    """Шаблоны для поиска процессов по сказанному имени программы или игры (для «закрой X»)."""
    n = re.sub(r"^(?:приложение|программу|программа|игру|игра|окно|окно с|процесс|прогу|вкладку с)\s+", "", _norm(spoken)).strip()
    pats: list[str] = []
    prog = resolve_program(n)
    if prog is None:                                   # «закрой браузер», «закрой мессенджер» — по смыслу
        from . import semantic                         # noqa: PLC0415
        hit = semantic.search(n, kinds=("app",))
        app = next((a for a in desktop_apps() if hit and a.desktop_id == hit[0].key), None)
        if app is not None:
            prog = (app.title, list(app.cmd), "")
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


def spawn_watched(cmds: list[list[str]], what: str) -> str | None:
    """Запустить первую команду и сразу вернуться (None) — программа открывается, пока Джарвис уже отвечает.
    В фоне LAUNCH_WATCH_SECONDS следим: упала с ошибкой — запасная команда (у браузера xdg-open); не вышло ничего —
    late_failure(«не получилось открыть …»). Если не запустилась ни одна (нет такой программы) — текст ошибки сразу."""
    errors: list[str] = []
    for i, cmd in enumerate(cmds):
        err = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
        except OSError as exc:
            err.close()
            errors.append(f"{cmd[0]}: {exc}")
            continue
        threading.Thread(target=_watch, args=(proc, err, cmds[i + 1:], what, errors), daemon=True).start()
        return None
    return "; ".join(errors)


def _watch(proc: subprocess.Popen, err, rest: list[list[str]], what: str, errors: list[str]) -> None:
    with err:
        try:
            code = proc.wait(timeout=LAUNCH_WATCH_SECONDS)
        except subprocess.TimeoutExpired:
            return                                    # работает — открылось
        if code == 0:
            return                                    # передала ссылку уже открытому окну и вышла
        err.seek(0)
        errors.append(f"{proc.args[0]} завершился с кодом {code}: {err.read().decode(errors='replace').strip()[-300:]}")
    if rest and spawn_watched(rest, what) is None:
        return
    late_failure(f"Не получилось открыть {what}: {'; '.join(errors)}")


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
    error = spawn_watched([plan.cmd], plan.description.split(":")[0])
    if error is not None:
        raise AppLaunchError(error)
    return plan.description
