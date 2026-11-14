"""Файлы и папки: поиск по сказанному имени, операции через D-Bus GNOME Files (копирование, перемещение, корзина,
переименование, папки), буфер обмена файлов, архивы, свойства, «где лежит», chmod +x."""
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
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .core import CLIPBOARD_STATE_FILE, KEY_LEFTCTRL, KEY_S, SystemCommandError, SystemPlan, _find_projects_dir, _find_removable_media, _latest_entry, _norm, _norm_stemmed, _register, _send_keys_or_xdotool, find_local_item  # noqa: F401

# =============================================================================
# 3. Поиск и операции с файлами и папками (Unicode NFKC + буфер обмена + диск)
# =============================================================================
_FILE_EXT_HINTS: dict[str, tuple[str, ...]] = {
    "пдф": (".pdf",),
    "pdf": (".pdf",),
    "ворд": (".docx", ".odt", ".doc"),
    "эксель": (".xlsx", ".ods", ".csv", ".xls"),
    "тхт": (".txt", ".md"),
    "зип": (".zip", ".tar.gz", ".7z", ".rar"),
}


def _nfkc_norm(s: str) -> str:
    """Нормализует Unicode (NFKC + приведение украинской І/і к латинской/кириллической для нечёткого совпадения)."""
    text = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    text = re.sub(r"[\u0300-\u036f]+", "", text)
    return _norm(text)


def _resolve_folder_hint(place: str, home: Path, dirs: dict[str, Path]) -> tuple[str, Path] | None:
    """Преобразует название места (`site`) в `(mode, Path)`:
    `mode` == 'dest' или 'general' для стандартных папок."""
    p = _nfkc_norm(place)
    if not p or p in ("компьютере", "ноутбуке", "пк", "системе"):
        return None
    if any(w in p for w in ("рабочий стол", "рабочем стол", "рабочего стол", "desktop")):
        return ("folder", dirs["DESKTOP"])
    if any(w in p for w in ("загрузк", "downloads", "download")):
        return ("folder", dirs["DOWNLOAD"])
    if any(w in p for w in ("документ", "documents", "document")):
        return ("folder", dirs["DOCUMENTS"])
    if any(w in p for w in ("изображен", "картинк", "фотк", "фото", "pictures")):
        return ("folder", dirs["PICTURES"])
    if any(w in p for w in ("скриншот", "снимки экрана", "screenshots")):
        shots = dirs["PICTURES"] / "Снимки экрана"
        if not shots.is_dir() and (dirs["PICTURES"] / "Screenshots").is_dir():
            shots = dirs["PICTURES"] / "Screenshots"
        return ("folder", shots)
    if any(w in p for w in ("музык", "music")):
        return ("folder", dirs["MUSIC"])
    if any(w in p for w in ("видео", "videos")):
        return ("folder", dirs["VIDEOS"])
    if any(w in p for w in ("домашн", "диск с", "диск c", "home")):
        return ("folder", home)
    if any(w in p for w in ("флешк", "диск д", "диск d", "внешний диск")):
        return ("folder", _find_removable_media())
    if any(w in p for w in ("проект", "projects")):
        return ("folder", _find_projects_dir(home))
    # Проверим существующую подпапку в стандартных директориях
    for base in (dirs["DESKTOP"], dirs["DOCUMENTS"], dirs["DOWNLOAD"], home):
        cand = base / place.strip(" ,.;:!?«»\"'")
        if cand.is_dir():
            return ("folder", cand)
    return ("custom", dirs["DOCUMENTS"] / place.strip(" ,.;:!?«»\"'").capitalize())


def _find_target_item(
    name: str,
    home: Path,
    dirs: dict[str, Path],
    want_dir: bool | None = None,
    prefer_root: Path | None = None,
) -> Path:
    """Ищет файл или папку пользователя по имени `name` с поддержкой Unicode NFKC и разговорных суффиксов."""
    raw = (name or "").strip(" ,.;:!?«»\"'")
    raw = re.sub(
        r"^(?:папку|папка|каталог|новую\s+папку|файл|файлы|документ|скрипт|программу|архив|со\s+|с\s+|для\s+)\s*",
        "",
        raw,
        flags=re.I,
    ).strip()
    n_norm = _nfkc_norm(raw)

    roots: list[Path] = []
    if prefer_root is not None and prefer_root.is_dir():
        roots.append(prefer_root)
    for r in (
        dirs["DESKTOP"],
        dirs["DOWNLOAD"],
        dirs["DOCUMENTS"],
        home,
        Path.cwd(),
        _find_projects_dir(home),
        dirs["PICTURES"],
        dirs["MUSIC"],
        dirs["VIDEOS"],
    ):
        if r not in roots and r.is_dir():
            roots.append(r)

    if not n_norm or n_norm in ("последний", "последний скачанный", "этот", "все файлы"):
        latest = _latest_entry(roots[:4], want_dir=bool(want_dir))
        if latest is not None:
            return latest
        return (dirs["DOWNLOAD"] / "folder") if want_dir else (dirs["DOWNLOAD"] / "file")

    # Стандартная папка («загрузки», «рабочий стол» и т.п.)
    if want_dir is not False:
        std = _resolve_folder_hint(n_norm, home, dirs)
        if std is not None and std[0] == "folder" and n_norm in (
            "рабочий стол", "desktop", "загрузки", "downloads", "документы", "documents",
            "изображения", "pictures", "музыка", "music", "видео", "videos", "скриншоты", "screenshots",
        ):
            return std[1]

    words = n_norm.split()
    wanted_exts: tuple[str, ...] = ()
    if len(words) >= 2 and words[-1] in _FILE_EXT_HINTS:
        wanted_exts = _FILE_EXT_HINTS[words[-1]]
        base_q = " ".join(words[:-1])
    else:
        base_q = n_norm
    base_stem = _norm_stemmed(base_q)

    # Двухпроходный поиск: 1) с учётом want_dir (если задан), 2) любой тип (файл или папка), чтобы «файл ml_model» находил и папку ml_model!
    passes = [want_dir, None] if want_dir is not None else [None]
    for dir_filter in passes:
        # Сначала точное совпадение на верхнем уровне и на глубине 1 в Desktop/Downloads/Documents/home
        search_dirs = list(roots)
        for r in roots[:4]:
            try:
                for sub in sorted(r.iterdir()):
                    if sub.is_dir() and not sub.name.startswith(".") and sub not in search_dirs:
                        search_dirs.append(sub)
            except OSError:
                pass

        partial_match: Path | None = None
        for root in search_dirs:
            try:
                for item in sorted(root.iterdir()):
                    if item.name.startswith("."):
                        continue
                    if dir_filter is True and not item.is_dir():
                        continue
                    if dir_filter is False and not item.is_file():
                        continue
                    item_full = _nfkc_norm(item.name)
                    item_base = _nfkc_norm(item.stem if item.is_file() else item.name)
                    item_stem = _norm_stemmed(item_base)
                    if wanted_exts and item.is_file() and item.suffix.lower() not in wanted_exts:
                        continue
                    if item_full == base_q or item_base == base_q or item_stem == base_stem:
                        return item
                    if partial_match is None and len(base_q) >= 3 and (base_q in item_full or item_base in base_q):
                        partial_match = item
            except OSError:
                continue
        if partial_match is not None:
            return partial_match

    # Ограниченный по времени поиск по всей домашней папке (глубина 5, без скрытых и служебных каталогов)
    deep = _deep_find(raw, base_q, base_stem, home, want_dir, wanted_exts)
    if deep is not None:
        return deep

    # Если не найден на диске — возвращаем ожидаемый путь в стандартной папке
    target_root = prefer_root if (prefer_root and prefer_root.is_dir()) else dirs["DOCUMENTS"]
    clean_title = raw if ("." in raw or any(c.isupper() for c in raw)) else (base_q.capitalize() if want_dir else base_q)
    if wanted_exts and not any(clean_title.lower().endswith(ext) for ext in wanted_exts):
        clean_title = f"{clean_title}{wanted_exts[0]}"
    return target_root / (clean_title or "Новый_элемент")


# --- Файловые операции через GNOME Files (org.gnome.Nautilus.FileOperations2): нативный прогресс, отмена (Ctrl+Z), корзина ---
_NAUTILUS_DEST = ("--dest", "org.gnome.Nautilus", "--object-path", "/org/gnome/Nautilus/FileOperations2")


def _gvariant_str(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _gvariant_list(items: list[str]) -> str:
    return "[" + ", ".join(_gvariant_str(x) for x in items) + "]"


def _nautilus_fileop(method: str, *args: str, timeout: float = 6.0) -> bool:
    """Вызывает org.gnome.Nautilus.FileOperations2.<method>(...) через gdbus. True — операция принята Nautilus."""
    if not shutil.which("gdbus"):
        return False
    try:
        res = subprocess.run(
            ["gdbus", "call", "--session", *_NAUTILUS_DEST, "--method", f"org.gnome.Nautilus.FileOperations2.{method}", *args, "{}"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _fo_copy(paths: list[Path], dest_dir: Path) -> bool:
    return _nautilus_fileop("CopyURIs", _gvariant_list([p.resolve().as_uri() for p in paths]), _gvariant_str(dest_dir.resolve().as_uri()))


def _fo_move(paths: list[Path], dest_dir: Path) -> bool:
    return _nautilus_fileop("MoveURIs", _gvariant_list([p.resolve().as_uri() for p in paths]), _gvariant_str(dest_dir.resolve().as_uri()))


def _fo_trash(paths: list[Path]) -> bool:
    return _nautilus_fileop("TrashURIs", _gvariant_list([p.resolve().as_uri() for p in paths]))


def _fo_rename(path: Path, new_name: str) -> bool:
    return _nautilus_fileop("RenameURI", _gvariant_str(path.resolve().as_uri()), _gvariant_str(new_name))


def _fo_mkdir(parent: Path, name: str) -> bool:
    return _nautilus_fileop("CreateFolder", _gvariant_str(parent.resolve().as_uri()), _gvariant_str(name))


def _fo_empty_trash() -> bool:
    return _nautilus_fileop("EmptyTrash", "false")


def _wait_exists(path: Path, seconds: float = 4.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.15)
    return path.exists()


def _wait_gone(path: Path, seconds: float = 4.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.15)
    return not path.exists()


def _file_manager_call(method: str, uris: list[str]) -> bool:
    """org.freedesktop.FileManager1.ShowItems / ShowFolders / ShowItemProperties (GNOME Files)."""
    if not shutil.which("gdbus"):
        return False
    try:
        res = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1", "--object-path", "/org/freedesktop/FileManager1",
             "--method", f"org.freedesktop.FileManager1.{method}", _gvariant_list(uris), "''"],
            capture_output=True, text=True, timeout=6.0, check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- Буфер обмена файлов (Ctrl+C / Ctrl+X / Ctrl+V как в GNOME Files) ---
def _clipboard_files_set(op: str, paths: list[Path]) -> str:
    """Кладёт файлы/папки в буфер обмена Ubuntu как это делает GNOME Files (x-special/gnome-copied-files)
    через wl-copy (Wayland) или xclip (X11); состояние дублируется в файл, чтобы «вставь в папку X» работало всегда.
    Возвращает способ: 'wl-copy' | 'xclip' | 'state'."""
    uris = [p.resolve().as_uri() for p in paths]
    gnome_payload = (op + "\n" + "\n".join(uris) + "\n").encode("utf-8")
    try:
        CLIPBOARD_STATE_FILE.write_text(
            json.dumps({"op": op, "paths": [str(p.resolve()) for p in paths], "uris": uris, "t": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    if shutil.which("wl-copy"):
        try:
            subprocess.run(["wl-copy", "--type", "x-special/gnome-copied-files"], input=gnome_payload,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
            return "wl-copy"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if shutil.which("xclip"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard", "-t", "x-special/gnome-copied-files", "-i"], input=gnome_payload,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
            return "xclip"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "state"


def _clipboard_files_get() -> tuple[str, list[Path]]:
    """(op, paths): что сейчас скопировано/вырезано — сначала настоящий буфер (wl-paste / xclip), затем наш файл состояния."""
    payload = ""
    for cmd in (["wl-paste", "--no-newline", "--type", "x-special/gnome-copied-files"],
                ["xclip", "-selection", "clipboard", "-t", "x-special/gnome-copied-files", "-o"]):
        if shutil.which(cmd[0]):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    payload = res.stdout
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue
    if payload:
        lines = [ln.strip() for ln in payload.splitlines() if ln.strip()]
        if lines and lines[0] in ("copy", "cut"):
            from urllib.parse import unquote, urlparse
            paths = [Path(unquote(urlparse(u).path)) for u in lines[1:] if u.startswith("file://")]
            if paths:
                return lines[0], paths
    try:
        state = json.loads(CLIPBOARD_STATE_FILE.read_text(encoding="utf-8"))
        paths = [Path(x) for x in state.get("paths", [])]
        if paths:
            return state.get("op", "copy"), paths
    except (OSError, ValueError):
        pass
    return "", []


def _clipboard_note(method: str) -> str:
    if method == "state":
        return " (вставка через Ctrl+V в GNOME Files заработает после `sudo apt install wl-clipboard`; команда «вставь в папку …» работает уже сейчас)"
    return " (можно вставить через Ctrl+V)"


_DEEP_SKIP = {"node_modules", "__pycache__", ".venv", "venv", "snap", "site-packages", ".cache", ".local", ".config", "dist", "build", ".git", "steamapps"}


def _deep_find(raw: str, base_q: str, base_stem: str, home: Path, want_dir: bool | None, wanted_exts: tuple[str, ...], budget: float = 2.5, max_depth: int = 5) -> Path | None:
    """os.walk по домашней папке с бюджетом времени: точное совпадение имени (NFKC, без регистра, дефисы/подчёркивания = пробел)."""
    deadline = time.monotonic() + budget
    raw_low = raw.casefold()
    partial: Path | None = None
    for root, dirnames, filenames in os.walk(home):
        if time.monotonic() > deadline:
            break
        depth = len(Path(root).relative_to(home).parts)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _DEEP_SKIP and depth < max_depth]
        names = list(dirnames) if want_dir is True else list(filenames) if want_dir is False else list(dirnames) + list(filenames)
        for name in names:
            if name.startswith("."):
                continue
            path = Path(root) / name
            is_file = name in filenames
            if wanted_exts and is_file and path.suffix.lower() not in wanted_exts:
                continue
            full = _nfkc_norm(name)
            base = _nfkc_norm(path.stem if is_file else name)
            if name.casefold() == raw_low or full == base_q or base == base_q or _norm_stemmed(base) == base_stem:
                return path
            if partial is None and len(base_q) >= 4 and base_q in full:
                partial = path
    return partial


def _unique_copy_dest(src: Path, dest_dir: Path) -> Path:
    """Возвращает безопасный путь назначения в `dest_dir`; если `src` уже лежит в `dest_dir`, добавляет « (копия)»."""
    candidate = dest_dir / src.name
    try:
        if candidate.resolve() == src.resolve() or candidate.exists():
            stem = src.stem if src.is_file() else src.name
            suffix = src.suffix if src.is_file() else ""
            idx = 1
            while True:
                label = " (копия)" if idx == 1 else f" (копия {idx})"
                alt = dest_dir / f"{stem}{label}{suffix}"
                if not alt.exists():
                    return alt
                idx += 1
    except OSError:
        pass
    return candidate


# --- 4.11. Файловая система, копирование, архивы, права запуска ---
@_register("fs_mkdir")
def _h_fs_mkdir(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    parent_dir = _dest_dir_from_site(site, home, dirs) or dirs["DESKTOP"]
    raw_name = (query or "").strip(" ,.;:!?«»\"'")
    raw_name = re.sub(r"^(?:для|под|с|со)\s+", "", raw_name, flags=re.I).strip()
    # Названия из речи приходят строчными («проекты»): русское имя с маленькой буквы делаем с заглавной, как в GNOME Files;
    # латинские / технические имена (snake_case, main.py, traning ml) не трогаем.
    if raw_name and re.fullmatch(r"[а-яёa-z0-9 ]+", raw_name) and re.search(r"[а-яё]", raw_name):
        raw_name = raw_name[:1].upper() + raw_name[1:]
    folder_name = raw_name if raw_name else "Новая папка"
    target = parent_dir / folder_name

    def _do_mkdir() -> str:
        if target.exists():
            return f"Папка уже есть: {target}"
        if _fo_mkdir(parent_dir, folder_name) and _wait_exists(target):
            return f"Создана папка: {target}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Создана папка: {target}"

    return SystemPlan(
        category="fs",
        action="mkdir",
        description=f"Создаю папку «{target.name}» в {parent_dir}",
        target_path=str(target),
        commands=(("mkdir", "-p", str(target)),),
        python_op=_do_mkdir,
    )


@_register("fs_touch", "fs_new_file")
def _h_fs_new_file(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    parent_dir = _dest_dir_from_site(site, home, dirs) or dirs["DESKTOP"]
    raw_name = (query or "").strip(" ,.;:!?«»\"'")
    if not raw_name or raw_name in ("текстовый", "текстовый файл", "новый", "пустой"):
        fname = "Новый документ.txt"
    else:
        fname = raw_name if "." in raw_name else f"{raw_name}.txt"
    target = parent_dir / fname

    def _do_touch() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return f"Файл уже есть: {target}"
        target.touch(exist_ok=True)
        return f"Создан файл: {target}"

    return SystemPlan(
        category="fs",
        action="touch_file",
        description=f"Создаю файл {target}",
        target_path=str(target),
        commands=(("touch", str(target)),),
        python_op=_do_touch,
    )


def _clean_place(site: str) -> str:
    """«папке traning ml» / «в папку education» / «каталог X» -> «traning ml» / «education» / «X»."""
    s_ = (site or "").strip(" ,.;:!?«»\"'")
    return re.sub(r"^(?:в\s+|на\s+|внутри\s+|из\s+|с\s+)?(?:папку|папке|папка|папки|каталог|каталоге|директорию|директории|проект|проекте)\s+", "", s_, flags=re.I).strip() or s_


def _dest_dir_from_site(site: str, home: Path, dirs: dict[str, Path]) -> Path | None:
    """Папка назначения из слота site: стандартное место («рабочий стол», «downloads») или папка пользователя по имени."""
    if not site:
        return None
    site = _clean_place(site)
    hint = _resolve_folder_hint(site, home, dirs)
    if hint is not None and hint[0] == "folder":
        return hint[1]
    found = find_local_item(site, home)
    if found is not None and found.is_dir():
        return found
    found2 = _find_target_item(site, home, dirs, want_dir=True)
    if found2.is_dir():
        return found2
    deep = _deep_find(site, _nfkc_norm(site), _norm_stemmed(site), home, True, ())
    return deep if (deep is not None and deep.is_dir()) else (hint[1] if hint is not None else None)


_SOURCE_FORMS = re.compile(r"\b(?:рабочего\s+стола|загрузок|документов|изображений|картинок|музыки|видео\s+папки|флешки|диска\s+\w|"
                           r"домашней\s+папки|папки|каталога|архива|проектов)\b", re.I)


def _is_source_form(site: str) -> bool:
    """«рабочего стола», «загрузок», «папки X» — родительный падеж: это место, ОТКУДА копируют, а не куда."""
    return bool(_SOURCE_FORMS.search(_norm(site)))


@_register("fs_copy")
def _h_fs_copy(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    """«скопируй X» -> в буфер обмена (как Ctrl+C в GNOME Files); «скопируй X на рабочий стол / в папку Y» -> копия на диск."""
    dest_dir = _dest_dir_from_site(site, home, dirs)
    src_path = _find_target_item(query, home, dirs, want_dir=None, prefer_root=dest_dir)
    # Слот site бывает и «куда» («на рабочий стол»), и «откуда» («с рабочего стола»): различаем по падежу самого слота.
    is_source_place = dest_dir is not None and (_is_source_form(site) or (
        src_path.parent.resolve() == dest_dir.resolve() and not re.search(r"[а-яё]", site.lower())))
    has_dest = dest_dir is not None and not is_source_place

    def _do_copy() -> str:
        if not src_path.exists():
            raise SystemCommandError(f"файл или папка не найдены: «{query}»")
        if has_dest:
            dest_dir.mkdir(parents=True, exist_ok=True)
            final_dst = dest_dir / src_path.name
            if _fo_copy([src_path], dest_dir) and _wait_exists(final_dst, 6.0):
                return f"Скопировано: {src_path} -> {dest_dir}"
            final_dst = _unique_copy_dest(src_path, dest_dir)
            if src_path.is_dir():
                shutil.copytree(src_path, final_dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, final_dst)
            return f"Скопировано: {src_path} -> {final_dst}"
        how = _clipboard_files_set("copy", [src_path])
        kind_label = "Папка" if src_path.is_dir() else "Файл"
        return f"{kind_label} «{src_path.name}» скопирован(а) в буфер обмена{_clipboard_note(how)}"

    return SystemPlan(
        category="fs",
        action="copy",
        description=f"Копирую «{src_path.name}»" + (f" в {dest_dir}" if has_dest else " в буфер обмена"),
        target_path=str(src_path),
        dest_path=str(dest_dir / src_path.name) if has_dest else "",
        commands=(("cp", "-r", str(src_path), str(dest_dir)),) if has_dest else (("wl-copy", "--type", "x-special/gnome-copied-files"),),
        python_op=_do_copy,
    )


@_register("fs_cut")
def _h_fs_cut(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    """«вырежи X» -> в буфер обмена как Ctrl+X в GNOME Files (переместится при вставке)."""
    prefer = _dest_dir_from_site(site, home, dirs)
    src_path = _find_target_item(query, home, dirs, want_dir=None, prefer_root=prefer)

    def _do_cut() -> str:
        if not src_path.exists():
            raise SystemCommandError(f"файл или папка не найдены: «{query}»")
        how = _clipboard_files_set("cut", [src_path])
        kind_label = "Папка" if src_path.is_dir() else "Файл"
        return f"{kind_label} «{src_path.name}» вырезан(а) в буфер обмена{_clipboard_note(how)}"

    return SystemPlan(
        category="fs",
        action="cut",
        description=f"Вырезаю «{src_path.name}» в буфер обмена",
        target_path=str(src_path),
        commands=(("wl-copy", "--type", "x-special/gnome-copied-files"),),
        python_op=_do_cut,
    )


@_register("fs_move")
def _h_fs_move(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    src_path = _find_target_item(query, home, dirs, want_dir=None)
    dst_dir = _dest_dir_from_site(site, home, dirs)
    if dst_dir is None:
        hint = _resolve_folder_hint(site or "документы", home, dirs)
        dst_dir = hint[1] if hint is not None else dirs["DOCUMENTS"]
    dst_path = dst_dir / src_path.name

    def _do_move() -> str:
        if not src_path.exists():
            raise SystemCommandError(f"не найдено для перемещения: «{query}»")
        if src_path.resolve() == dst_dir.resolve() or src_path.parent.resolve() == dst_dir.resolve():
            return f"«{src_path.name}» уже находится в {dst_dir}"
        dst_dir.mkdir(parents=True, exist_ok=True)
        if _fo_move([src_path], dst_dir) and _wait_gone(src_path, 6.0):
            return f"Перемещено: {src_path} -> {dst_dir}"
        shutil.move(str(src_path), str(dst_path))
        return f"Перемещено: {src_path} -> {dst_path}"

    return SystemPlan(
        category="fs",
        action="move",
        description=f"Перемещаю «{src_path.name}» в «{dst_dir}»",
        target_path=str(src_path),
        dest_path=str(dst_path),
        commands=(("mv", str(src_path), str(dst_path)),),
        python_op=_do_move,
    )


@_register("fs_rename")
def _h_fs_rename(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    src_path = _find_target_item(query, home, dirs, want_dir=None)
    new_base = (site or "").strip(" ,.;:!?«»\"'")
    if not new_base:
        new_base = f"{src_path.stem} (новое имя)" if src_path.is_file() else f"{src_path.name} (новое имя)"
    if src_path.is_file() and src_path.suffix and "." not in new_base:
        new_name = f"{new_base}{src_path.suffix}"
    else:
        new_name = new_base
    dst_path = src_path.parent / new_name

    def _do_rename() -> str:
        if not src_path.exists():
            raise SystemCommandError(f"не найдено для переименования: «{query}»")
        if dst_path.exists():
            raise SystemCommandError(f"«{new_name}» уже существует в {src_path.parent}")
        if _fo_rename(src_path, new_name) and _wait_exists(dst_path, 4.0):
            return f"Переименовано: {src_path.name} -> {new_name}"
        src_path.rename(dst_path)
        return f"Переименовано: {src_path.name} -> {new_name}"

    return SystemPlan(
        category="fs",
        action="rename",
        description=f"Переименовываю «{src_path.name}» в «{dst_path.name}»",
        target_path=str(src_path),
        dest_path=str(dst_path),
        commands=(("mv", str(src_path), str(dst_path)),),
        python_op=_do_rename,
    )


@_register("fs_trash", "fs_delete")
def _h_fs_delete(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    prefer_dir = _dest_dir_from_site(site, home, dirs)
    target_path = _find_target_item(query, home, dirs, want_dir=None, prefer_root=prefer_dir)

    def _do_trash() -> str:
        if not target_path.exists():
            raise SystemCommandError(f"не найдено для удаления: «{query}»")
        if _fo_trash([target_path]) and _wait_gone(target_path, 6.0):
            return f"«{target_path.name}» перемещён(а) в Корзину ({target_path})"
        res = subprocess.run(["gio", "trash", str(target_path)], capture_output=True, text=True, timeout=10.0, check=False)
        if res.returncode == 0:
            return f"«{target_path.name}» перемещён(а) в Корзину ({target_path})"
        raise SystemCommandError(f"не удалось удалить {target_path}: {res.stderr.strip()[-200:]}")

    return SystemPlan(
        category="fs",
        action="trash",
        description=f"Перемещаю «{target_path}» в Корзину",
        target_path=str(target_path),
        commands=(("gio", "trash", str(target_path)),),
        python_op=_do_trash,
    )


@_register("fs_chmod_x")
def _h_fs_chmod_x(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    prefer_dir = _dest_dir_from_site(site, home, dirs)
    target_path = _find_target_item(query, home, dirs, want_dir=False, prefer_root=prefer_dir)

    def _do_chmod() -> str:
        if not target_path.exists():
            raise SystemCommandError(f"скрипт или файл не найден: {target_path}")
        mode = target_path.stat().st_mode
        target_path.chmod(mode | 0o111)
        return f"Выданы права на выполнение (chmod +x): {target_path}"

    return SystemPlan(
        category="fs",
        action="make_executable",
        description=f"Делаю файл «{target_path.name}» исполняемым (chmod +x {target_path})",
        target_path=str(target_path),
        commands=(("chmod", "+x", str(target_path)),),
        python_op=_do_chmod,
    )


@_register("fs_archive")
def _h_fs_archive(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    src_path = _find_target_item(query, home, dirs, want_dir=None)
    dest_dir = _dest_dir_from_site(site, home, dirs) or src_path.parent
    archive_file = dest_dir / (src_path.name if src_path.is_dir() else src_path.stem)
    archive_file = archive_file.with_name(archive_file.name + ".zip")

    def _do_zip() -> str:
        if not src_path.exists():
            raise SystemCommandError(f"не найдено для архивации: «{query}»")
        base = str(archive_file)[:-4]
        if archive_file.exists():
            idx = 2
            while Path(f"{base} ({idx}).zip").exists():
                idx += 1
            base = f"{base} ({idx})"
        made = shutil.make_archive(base, "zip", root_dir=str(src_path.parent), base_dir=src_path.name)
        return f"Создан архив: {made}"

    return SystemPlan(
        category="fs",
        action="archive",
        description=f"Создаю архив {archive_file.name} из {src_path}",
        target_path=str(src_path),
        dest_path=str(archive_file),
        commands=(("zip", "-r", str(archive_file), str(src_path)),),
        python_op=_do_zip,
    )


@_register("fs_unpack", "fs_extract")
def _h_fs_extract(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    dl = dirs["DOWNLOAD"]
    if query:
        src_archive = _find_target_item(query, home, dirs, want_dir=False)
    else:
        archives = sorted(
            [p for p in (dl.iterdir() if dl.is_dir() else []) if p.suffix.lower() in (".zip", ".tar", ".gz", ".xz", ".tgz", ".bz2")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        src_archive = archives[0] if archives else (dl / "archive.zip")
    stem = src_archive.name
    for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".zip", ".tar", ".tgz", ".rar", ".7z"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    dest_root = _dest_dir_from_site(site, home, dirs) or src_archive.parent
    dest_dir = dest_root / stem

    def _do_unpack() -> str:
        if not src_archive.exists():
            raise SystemCommandError(f"архив не найден: «{query}»")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.unpack_archive(str(src_archive), str(dest_dir))
        except (shutil.ReadError, ValueError):
            for tool in (("7z", "x", "-y", f"-o{dest_dir}", str(src_archive)), ("unrar", "x", "-y", str(src_archive), str(dest_dir) + "/")):
                if shutil.which(tool[0]) and subprocess.run(list(tool), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False).returncode == 0:
                    break
            else:
                raise SystemCommandError(f"не удалось распаковать {src_archive.name} (нужен 7z или unrar)")
        return f"Архив {src_archive.name} распакован в {dest_dir}"

    return SystemPlan(
        category="fs",
        action="unpack",
        description=f"Распаковываю архив {src_archive.name} в {dest_dir}",
        target_path=str(src_archive),
        dest_path=str(dest_dir),
        commands=(("unzip", "-o", str(src_archive), "-d", str(dest_dir)),),
        python_op=_do_unpack,
    )


@_register("fs_clean_downloads")
def _h_fs_clean_downloads(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    dl = dirs["DOWNLOAD"]

    def _do_clean() -> str:
        items = [p for p in dl.iterdir() if not p.name.startswith(".")] if dl.is_dir() else []
        if not items:
            return f"Папка «Загрузки» уже пуста ({dl})"
        if not _fo_trash(items):
            subprocess.run(["gio", "trash", *map(str, items)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        return f"Содержимое «Загрузки» ({len(items)} элементов) перемещено в Корзину"

    return SystemPlan(
        category="fs",
        action="trash_downloads",
        description=f"Перемещаю содержимое папки «Загрузки» ({dl}) в Корзину",
        target_path=str(dl),
        commands=(("bash", "-c", f'shopt -s nullglob; files=("{dl}"/*); [ ${{#files[@]}} -gt 0 ] && gio trash "${{files[@]}}"'),),
        python_op=_do_clean,
    )


@_register("fs_empty_trash", "trash_empty")
def _h_trash_empty(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    def _do_empty() -> str:
        if _fo_empty_trash():
            return "Корзина очищена"
        res = subprocess.run(["gio", "trash", "--empty"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
        if res.returncode == 0:
            return "Корзина очищена"
        raise SystemCommandError("не удалось очистить корзину")

    return SystemPlan(
        category="fs",
        action="empty_trash",
        description="Очищаю Корзину",
        commands=(("gio", "trash", "--empty"),),
        python_op=_do_empty,
    )


@_register("doc_save", "file_save")
def _h_file_save(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="window",
        action="save_file",
        description="Сохраняю текущий файл (Ctrl+S)",
        commands=(("xdotool", "key", "ctrl+s"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, KEY_S),), ("xdotool", "key", "ctrl+s"), "Отправлено сохранение файла (Ctrl+S)"),
    )


@_register("doc_print", "file_print")
def _h_file_print(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="device",
        action="print",
        description="Открываю диалог печати (Ctrl+P)",
        commands=(("xdotool", "key", "ctrl+p"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTCTRL, 25),), ("xdotool", "key", "ctrl+p"), "Открыт диалог печати (Ctrl+P)"),
    )


@_register("dev_mount", "disk_mount")
def _h_disk_mount(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="device",
        action="mount_disk",
        description="Открываю утилиту «Диски GNOME» для монтирования накопителя",
        commands=(("gnome-disks",),),
    )


@_register("dev_eject", "usb_eject")
def _h_usb_eject(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    rem = _find_removable_media()
    return SystemPlan(
        category="device",
        action="eject_usb",
        description=f"Безопасно извлекаю накопитель ({rem})",
        target_path=str(rem),
        commands=(("gio", "mount", "-u", str(rem)),),
    )


@_register("sys_update")
def _h_sys_update(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="device",
        action="system_update",
        description="Запускаю «Обновление приложений» Ubuntu (update-manager)",
        commands=(("update-manager",),),
        fallback_commands=(("gnome-terminal", "--", "bash", "-c", "sudo apt update && sudo apt upgrade; exec bash"),),
    )


# --- 4.12. Свойства, расположение файла и разрешение экрана ---
@_register("fs_properties")
def _h_fs_properties(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    prefer_dir = _dest_dir_from_site(site, home, dirs)
    target = _find_target_item(query, home, dirs, want_dir=None, prefer_root=prefer_dir)

    def _human(n: float) -> str:
        for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} ПБ"

    def _do_props() -> str:
        if not target.exists():
            raise SystemCommandError(f"не найдено: «{query}»")
        _file_manager_call("ShowItemProperties", [target.resolve().as_uri()])
        if target.is_dir():
            total, files, folders = 0, 0, 0
            for root, dnames, fnames in os.walk(target):
                folders += len(dnames)
                for f in fnames:
                    files += 1
                    try:
                        total += (Path(root) / f).stat().st_size
                    except OSError:
                        pass
            return f"Папка «{target.name}»: {_human(total)}, файлов: {files}, папок: {folders} ({target})"
        st = target.stat()
        return f"Файл «{target.name}»: {_human(st.st_size)}, изменён {dt.datetime.fromtimestamp(st.st_mtime):%d.%m.%Y %H:%M} ({target})"

    return SystemPlan(
        category="fs",
        action="properties",
        description=f"Показываю свойства «{target.name}»",
        target_path=str(target),
        commands=(("gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1", "--object-path", "/org/freedesktop/FileManager1",
                   "--method", "org.freedesktop.FileManager1.ShowItemProperties", f"['{target.resolve().as_uri()}']", "''"),),
        python_op=_do_props,
    )


@_register("fs_show_in_files")
def _h_fs_show_in_files(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    prefer_dir = _dest_dir_from_site(site, home, dirs)
    target = _find_target_item(query, home, dirs, want_dir=None, prefer_root=prefer_dir)

    def _do_show() -> str:
        if not target.exists():
            raise SystemCommandError(f"не найдено на компьютере: «{query}»")
        if _file_manager_call("ShowItems", [target.resolve().as_uri()]):
            return f"«{target.name}» лежит здесь: {target} (показано в GNOME Files)"
        subprocess.Popen(["nautilus", "--select", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return f"«{target.name}» лежит здесь: {target}"

    return SystemPlan(
        category="fs",
        action="show_in_files",
        description=f"Показываю «{target.name}» в файловом менеджере",
        target_path=str(target),
        commands=(("nautilus", "--select", str(target)),),
        python_op=_do_show,
    )
