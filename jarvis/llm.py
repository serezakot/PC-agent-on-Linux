"""Второй слой ассистента: большая LLM для того, что не смог первый слой.

Сюда попадают только фразы, которые парсер отдал в маршрут `llm`: вопросы, размытые цели, многошаговые просьбы
(«скопируй все pdf из загрузок в документы и заархивируй»), неуверенные случаи. LLM ничего не выполняет сама:
она вызывает ТЕ ЖЕ исполнители, что и первый слой, как инструменты:
    system_action — любое из действий ml_spec.SYSTEM_ACTIONS (jarvis.system)
    open_app      — программа / папка / файл / игра (jarvis.apps)
    open_web      — сайт, поиск, YouTube (jarvis.browser)
    translate     — Google Переводчик (jarvis.browser)
    list_folder, find_files — только чтение: что лежит в папке, где файл
Опасные действия (удаление, перемещение, выключение, закрытие всех окон...) выполняются только после подтверждения.

Модель — Gemini 3 Flash через Google Cloud Vertex AI (OpenAI-совместимый API, оплата с аккаунта Google Cloud); вместо
ключа — JSON сервисного аккаунта в ~/.config/pc_agent/vertex-sa.json (тот же, что у распознавания и голоса Google).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import ml_spec

from . import ML_DIR, apps, browser, shell, sys_query, system

CONFIG_DIR = Path.home() / ".config" / "pc_agent"
MAX_TOOL_ROUNDS = 12
# Сколько модель «размышляет» (reasoning_effort; None — сама решает, ~5 с на вопрос). Первый ответ — быстро (~1.6 с):
# вопрос на этом и кончается; начала действовать инструментами — дальше думает как обычно (точность многошаговых дел).
THINK_FIRST: str | None = "minimal"
THINK_AFTER_TOOLS: str | None = None
HISTORY_IDLE_SECONDS = 15 * 60          # после паузы разговор начинается заново
HISTORY_MAX_MESSAGES = 60              # длиннее — забываем самые старые реплики (по границе сообщения пользователя)

# Действия, которые нельзя выполнять без «да» от пользователя (необратимые, меняющие файлы или выключающие компьютер)
DANGEROUS = {
    "fs_trash", "fs_empty_trash", "fs_clean_downloads", "fs_move", "fs_rename", "pkg_remove", "win_close_all",
    "power_off", "power_reboot", "power_hibernate", "power_sleep", "session_logout", "session_switch_user",
}


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str


VERTEX_SA_FILE = CONFIG_DIR / "vertex-sa.json"
VERTEX_LOCATION = "global"
# {project} подставляется из сервисного аккаунта; ключ не нужен — токен выдаёт google-auth
VERTEX = Provider("vertex", "https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/endpoints/openapi",
                  "google/gemini-3-flash-preview")


class LLMUnavailable(RuntimeError):
    """Нет ключа, пакета, сети или кончился лимит провайдера."""


# =============================================================================
# Инструменты: описания строятся из того, что реально умеют исполнители
# =============================================================================
def _action_catalogue() -> str:
    """Действия, сгруппированные по модулям system/ (берётся из реестра обработчиков, не пишется руками)."""
    groups: dict[str, list[str]] = {}
    for action in ml_spec.SYSTEM_ACTIONS:
        handler = system.ACTION_HANDLERS.get(action)
        domain = handler.__module__.rsplit(".", 1)[-1] if handler else "other"
        groups.setdefault(domain, []).append(action)
    return "\n".join(f"{domain}: {', '.join(actions)}" for domain, actions in sorted(groups.items()))


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


def build_tools() -> list[dict]:
    s = {"type": "string"}
    return [
        _fn("system_action",
            "Run one Ubuntu system action (the same executor the voice parser uses). Arguments are plain words, the "
            "executor resolves them: query = the object (file/folder name as the user calls it, percent like '30', "
            "duration like '10 минут', resolution like '1920 на 1080', app or game to close, Bluetooth device, time "
            "for a reminder like '18:00' or '20 минут'); site = destination folder ('рабочий стол', 'документы', a "
            "folder name) or the new name for fs_rename; text = reminder message (reminder_set only). fs_copy without "
            "site puts the file into the clipboard; clip_paste with site pastes clipboard files there. Use empty "
            "strings for unused fields. Actions by domain:\n" + _action_catalogue(),
            {"action": {"type": "string", "enum": list(ml_spec.SYSTEM_ACTIONS)}, "query": s, "site": s, "text": s},
            ["action"]),
        _fn("open_app",
            "Open an installed program, a game from the Steam library, a standard folder (загрузки, документы, "
            "рабочий стол, корзина...) or a user file/folder by name. name = what to open in the user's words "
            "('блокнот', 'пайчарм', 'доту', 'traning ml'); inner = optional action inside it ('в папке education' for "
            "a terminal, a game to find in Steam, a song in Spotify, 'отсортируй по дате' for a folder).",
            {"name": s, "inner": s}, ["name"]),
        _fn("open_web",
            "Open the browser. site = site name in the user's words ('ютуб', 'github'), empty for a Google search; "
            "query = what to search or play. mode: web_open (just the site), web_search (search results), "
            "web_open_first (open the first video/result directly), web_channel (a YouTube channel - only when the "
            "user asked for a channel).",
            {"site": s, "query": s, "mode": {"type": "string", "enum": list(ml_spec.BROWSER_ACTIONS)}}, ["mode"]),
        _fn("translate",
            "Open Google Translate with the text. lang = target language code ('en', 'de', ...) or 'none'.",
            {"text": s, "lang": {"type": "string", "enum": list(ml_spec.LANGS)}}, ["text"]),
        _fn("list_folder",
            "Read-only: list what is inside a folder (names, size, modification date). folder = a path ('~/Desktop') "
            "or a name in the user's words ('загрузки', 'рабочий стол', 'traning ml'). Use it before acting on files "
            "you have not seen, instead of guessing names.",
            {"folder": s}, ["folder"]),
        _fn("find_files",
            "Read-only: find files and folders in the home directory whose name contains `pattern` (case-insensitive) "
            "or ends with an extension like '.pdf'. Returns up to 50 full paths.",
            {"pattern": s}, ["pattern"]),
        _fn("system_query",
            "Read-only: run ONE safe Linux command to answer a question about this computer that no system_action answers "
            "(nothing is changed). Allowed: uptime, df, du (home only), free, ps -eo <fields> (no command lines), "
            "top -b -n 1, lsblk, lscpu, lspci, lsusb, uname, hostnamectl, timedatectl, ip addr/route, nmcli (device status, "
            "device wifi list, connection show, radio, general), systemctl status/list-units/list-timers/is-active, "
            "systemd-analyze blame, sensors, upower -i/-e, pactl list/info/get-*, wpctl status/inspect, snap list/info, "
            "flatpak list, dpkg -l/-s/-L, apt list/show/policy/search, apt-cache, gsettings get/list-*, xdg-mime query, "
            "xdg-settings get, ls, stat, wc, file, pgrep -l, pidof, who, last, ping -c N, dig/host, <program> --version. "
            "No pipes, redirects, globs or sudo; anything that changes the system is refused. Examples: 'uptime -p', "
            "'du -sh ~/Downloads', 'nmcli -t -f SSID,SIGNAL device wifi list', 'pactl list short source-outputs', "
            "'python3 --version', 'apt list --upgradable'.",
            {"command": s}, ["command"]),
        _fn("read_text_file",
            "Read-only: the beginning (about 6000 characters) of a text file in the home folder or on a USB drive: notes, "
            "code, configs, logs. path = a full path from list_folder/find_files.",
            {"path": s}, ["path"]),
        _fn("write_text_file",
            "Write text into a text file in the home folder: mode append (add to the end; creates the file if missing), "
            "overwrite (replace the content; the user confirms), create (only a new file). path = full path "
            "('~/Desktop/заметки.txt'); find existing files with list_folder/find_files first.",
            {"path": s, "text": s, "mode": {"type": "string", "enum": ["append", "overwrite", "create"]}}, ["path", "text"]),
        _fn("run_command",
            "Run ANY shell command on this computer when no other tool does the job (connect to a Wi-Fi network with "
            "nmcli, change a setting with gsettings, edit autostart, install with apt in a terminal, batch-rename files, "
            "kill a process...). Read-only commands run at once; everything else is confirmed by the user by voice — "
            "explanation is what you will say: a short plain-Russian description of what the command does. Use "
            "in_terminal=true for sudo, interactive or long commands (the user sees it and types the password). End "
            "with ' &' to start a program in the background. Never-allowed commands are refused automatically.",
            {"command": s, "explanation": s, "in_terminal": {"type": "boolean"}}, ["command", "explanation"]),
        _fn("list_apps",
            "Read-only: installed programs (title — what it is), GNOME settings panels and Steam games of this computer. "
            "Use it when open_app did not find a program by the user's words, then open the right one by its exact title.",
            {}, []),
    ]


SYSTEM_PROMPT = """You are the second layer of a Russian-speaking voice assistant on the user's own Ubuntu 24.04 laptop \
(GNOME, Wayland). A small local parser already handled everything simple; you get what it could not: questions, vague \
goals, multi-step requests, and phrases it was unsure about.

How to work:
- If the request is something to do on the computer, do it with the tools. Chain several tool calls for multi-step \
requests. Use list_folder / find_files to look before acting on files; never invent file names. When you act on a \
file or folder you have seen in a listing, pass its full path (e.g. /home/user/Downloads/a.pdf) as query and site: \
the same name can exist in several folders.
- Names you pass to tools may be plain words; the executors resolve them against the real disk.
- You can do anything on this computer. Prefer the specific tools for common things (system_action, open_app, \
open_web...). Facts no action gives (uptime, folder size, Wi-Fi networks nearby, what uses the microphone, versions) — \
system_query. Text into a file — write_text_file. Anything else (connect to a named Wi-Fi network, change a setting, \
autostart, batch operations, installing) — run_command; it asks the user itself, so just call it with a clear Russian \
explanation. Never claim you cannot do something that a command can do.
- If open_app did not find a program by the user's words (it opens the app store instead), call list_apps and open the \
program that matches by meaning ("блокнот" = text editor, "проводник" = Files, "диспетчер задач" = System Monitor); \
if nothing matches, say it is not installed.
- If a tool refuses (a file or command is not allowed), accept it and tell the user; never work around a refusal by \
copying, moving or renaming files or by another command.
- If the request is a question or conversation, just answer it. You cannot browse the internet yourself; you can only \
open pages for the user with open_web.
- Never state facts about this computer (time, battery, disk, files, devices) that did not come from a tool result. \
If a tool result starts with "[dry-run]", nothing was executed: say what you would do, without inventing the outcome.
- If the request is ambiguous in a way that changes what you would do, ask one short question instead of guessing.
- Destructive actions (deleting, moving, renaming, shutting down, closing all windows, removing programs) are \
confirmed by the user automatically before they run, so do not ask "are you sure" yourself: if the user clearly asked \
for it, call the tool; the result tells you whether they agreed. Never start such an action the user did not ask for.
- A vague goal ("почисти компьютер", "наведи порядок", "освободи место") is NOT a request to delete anything: first \
look (sys_disk_free, list_folder), then tell the user what you found and what you could remove, and wait for an \
explicit answer. fs_empty_trash, fs_clean_downloads and fs_trash only when the user named that exact thing.
- When you ask the user to choose between several found files, name the options briefly (folder + name).
- Smart-home devices (lights, kettle, curtains) are not connected; say so briefly.

Your reply is read aloud by a Russian voice: answer ONLY in Russian — never write English sentences or words like \
"OK" (only proper names such as YouTube or Steam may stay as they are; a folder like Downloads is «Загрузки»). Speak of \
yourself in the feminine gender (сделала, открыла), in one to three short sentences, plain text, no markdown, no lists \
unless the user asked for a list. After acting, say what you did, not how.
The user speaks to you by voice and the speech recognizer writes English words by ear in Russian letters ("даунс" = \
Downloads, "паспортс" = passwords, "смелс лайк тин спирит" = Smells Like Teen Spirit): guess the intended English name \
and use its real spelling in tool calls."""


# =============================================================================
# Выполнение инструментов (через исполнители первого слоя)
# =============================================================================
def _human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _readable(path: Path) -> bool:
    """Что LLM можно показывать: только домашняя папка и сменные носители, без скрытых папок (~/.ssh, ~/.config с ключами)."""
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return False
    home = Path.home().resolve()
    roots = [home, Path("/media").resolve(), Path(f"/run/media/{os.environ.get('USER', '')}")]
    root = next((r for r in roots if real == r or r in real.parents), None)
    return root is not None and not any(part.startswith(".") for part in real.relative_to(root).parts)


def _resolve_folder(folder: str) -> Path | None:
    raw = (folder or "").strip()
    if raw.startswith(("~", "/")):
        p = Path.home() / raw[1:].lstrip("/") if raw.startswith("~") else Path(raw)
        if p.is_dir() and not _readable(p):
            raise ValueError("эта папка вне домашней или скрытая: её содержимое не показываю")
        return p if p.is_dir() else None
    home = Path.home()
    plan = apps._match_standard_folder(apps._norm(raw), "", home, apps.xdg_user_dirs(home))
    if plan is not None and plan.kind == "folder" and not plan.target.startswith("trash"):
        return Path(plan.target)
    found = apps.find_local_item(raw, home)
    return found if found is not None and found.is_dir() else None


def _list_folder(folder: str) -> str:
    path = _resolve_folder(folder)
    if path is None:
        raise ValueError(f"папка «{folder}» не найдена")
    rows = []
    for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:100]:
        if item.name.startswith("."):
            continue
        try:
            st = item.stat()
        except OSError:
            continue
        kind = "папка" if item.is_dir() else _human_size(st.st_size)
        rows.append(f"{item.name}\t{kind}\t{time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))}")
    return f"{path}:\n" + ("\n".join(rows) if rows else "(пусто)")


def _find_files(pattern: str, root: Path | None = None) -> str:
    pat = (pattern or "").strip().lower()
    if len(pat) < 2:
        raise ValueError("слишком короткий образец для поиска")
    if "/" in pat:
        raise ValueError("образец — часть имени файла, без путей")
    skip = {"node_modules", "__pycache__", ".venv", "venv", "snap", "site-packages", ".cache", ".local", ".config", ".git"}
    hits, deadline = [], time.monotonic() + 3.0
    for base, dirs, files in os.walk(root or Path.home()):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        for name in dirs + [f for f in files if not f.startswith(".")]:     # скрытые файлы (~/.gitconfig...) не показываем
            n = name.lower()
            if (pat.startswith(".") and n.endswith(pat)) or (not pat.startswith(".") and pat in n):
                hits.append(str(Path(base) / name))
        if len(hits) >= 50 or time.monotonic() > deadline:
            break
    return "\n".join(hits[:50]) if hits else "ничего не найдено"


def _ask_yes_no(question: str) -> bool:
    try:
        return input(f"{question} [да/нет] ").strip().lower() in ("да", "д", "y", "yes")
    except EOFError:
        return False


class ToolExecutor:
    """Выполняет вызовы инструментов через исполнители первого слоя. confirm(вопрос) -> bool для опасных действий."""

    def __init__(self, dry_run: bool = False, confirm: Callable[[str], bool] | None = None):
        self.dry_run = dry_run
        self.confirm = confirm or _ask_yes_no
        self.log: list[str] = []                 # что было сделано за последнюю фразу (для вывода пользователю)
        self.calls: list[tuple[str, dict]] = []  # успешные вызовы за последнюю фразу (для обучения первого слоя)

    def run(self, name: str, args: dict) -> str:
        # модель иногда зовёт действие как отдельный инструмент: fs_rename(...) вместо system_action(action="fs_rename")
        if name in ml_spec.SYSTEM_ACTIONS:
            name, args = "system_action", {"action": name, **{k: v for k, v in args.items() if k in ("query", "site", "text")}}
        try:
            out = self._run(name, args)
        except Exception as exc:                       # в журнал попадают и неудачные вызовы — видно в отчёте
            self.log.append(f"{name}({json.dumps(args, ensure_ascii=False)}) -> ОШИБКА: {exc}"[:400])
            raise
        self.log.append(f"{name}({json.dumps(args, ensure_ascii=False)}) -> {str(out)[:200]}")
        self.calls.append((name, args))
        return out

    def _run(self, name: str, args: dict) -> str:
        if name == "system_action":
            action = args.get("action", "")
            plan = system.make_system_plan(query=args.get("query") or "", action=action, site=args.get("site") or "",
                                                   text=args.get("text") or "")
            if plan is None:
                raise ValueError(f"нет такого действия: {action}")
            if action in DANGEROUS and not self.dry_run and plan.category != "refused" and not self.confirm(f"Выполнить: {plan.description}?"):
                return "Пользователь отказался, действие не выполнено."
            # сведения о системе (время, батарея, диск...) только читают — их выполняем и в dry-run, иначе модели
            # нечего ответить и она начинает выдумывать цифры
            read_only = plan.category == "info" and plan.python_op is not None
            return system.execute_system_plan(plan, dry_run=self.dry_run and not read_only)
        if name == "open_app":
            plan = apps.make_app_plan(args.get("name") or "", args.get("inner") or "")
            if plan is None:
                raise ValueError("не указано, что открыть")
            return apps.execute_app_plan(plan, dry_run=self.dry_run)
        if name == "open_web":
            resolver = None                  # распознавание сайта только читает сеть — и в dry-run тоже, чтобы URL был настоящим
            plan = browser.make_plan(args.get("site") or "", args.get("query") or "", resolver=resolver,
                                          action=args.get("mode") or "web_search")
            if plan is None:
                raise ValueError("нечего открывать: пустые site и query")
            if self.dry_run:
                return f"[dry-run] {plan.url}"
            browser.open_in_browser(plan.url)
            return browser._describe(plan)
        if name == "translate":
            plan = browser.make_translate_plan(args.get("text") or "", args.get("lang") or "none")
            if plan is None:
                raise ValueError("нечего переводить")
            if self.dry_run:
                return f"[dry-run] {plan.url}"
            browser.open_in_browser(plan.url)
            return browser._describe(plan)
        if name == "list_folder":
            return _list_folder(args.get("folder") or "")
        if name == "find_files":
            return _find_files(args.get("pattern") or "")
        if name == "system_query":                   # только чтение: выполняется и в dry-run (как сведения о системе)
            return sys_query.run_query(args.get("command") or "")
        if name == "read_text_file":
            return sys_query.read_text_file(args.get("path") or "", _readable)
        if name == "write_text_file":
            return shell.write_text_file(args.get("path") or "", args.get("text") or "", args.get("mode") or "append",
                                         self.confirm, _readable, dry_run=self.dry_run)
        if name == "run_command":
            return shell.run_command(args.get("command") or "", args.get("explanation") or "", self.confirm,
                                     dry_run=self.dry_run, in_terminal=bool(args.get("in_terminal")))
        if name == "list_apps":
            return shell.list_apps()
        raise ValueError(f"неизвестный инструмент: {name}")


# =============================================================================
# Подключение к провайдеру (OpenAI-совместимый API)
# =============================================================================
def current_provider() -> Provider:
    return VERTEX


def _vertex_client(provider: Provider, openai):
    """Vertex AI: OpenAI-совместимый эндпоинт + токен сервисного аккаунта. Токен живёт ~1 час, поэтому он не
    передаётся как постоянный ключ, а подставляется (и при необходимости обновляется) перед каждым запросом."""
    sa = VERTEX_SA_FILE
    if not sa.is_file():
        raise LLMUnavailable(f"нет JSON сервисного аккаунта: положите его в {VERTEX_SA_FILE}")
    try:
        import google.auth.transport.requests
        import httpx
        from google.oauth2 import service_account
    except ImportError as exc:
        raise LLMUnavailable("не установлен google-auth: pip install google-auth") from exc
    try:
        cred = service_account.Credentials.from_service_account_file(
            str(sa), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except (ValueError, OSError) as exc:
        raise LLMUnavailable(f"не читается сервисный аккаунт {sa}: {exc}") from exc

    class _Bearer(httpx.Auth):
        def auth_flow(self, request):
            if not cred.valid:
                cred.refresh(google.auth.transport.requests.Request())
            request.headers["Authorization"] = f"Bearer {cred.token}"
            yield request

    base_url = provider.base_url.format(project=cred.project_id, location=VERTEX_LOCATION)
    return openai.OpenAI(api_key="vertex", base_url=base_url, timeout=60.0, max_retries=2,
                         http_client=httpx.Client(auth=_Bearer(), timeout=60.0))


def make_client(provider: Provider):
    try:
        import openai
    except ImportError as exc:
        raise LLMUnavailable("не установлен пакет openai: pip install openai") from exc
    return _vertex_client(provider, openai)


def _assistant_message(msg) -> dict:
    """Ответ модели -> сообщение для истории. Берём всё, что прислал провайдер (у Gemini в вызовах инструментов есть
    служебные подписи extra_content, их надо вернуть как есть), кроме рассуждений reasoning_content — их назад не шлём."""
    data = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
    data.pop("reasoning_content", None)
    data["role"] = "assistant"
    data.setdefault("content", "")
    return data


def _api_error_text(exc: Exception) -> str | None:
    """Ошибки провайдера, на которых второй слой честно сдаётся (а не падает ассистент)."""
    try:
        from google.auth.exceptions import GoogleAuthError
        if isinstance(exc, GoogleAuthError):
            return f"Google не принял сервисный аккаунт: {str(exc)[:200]}"
    except ImportError:
        pass
    try:
        import openai
    except ImportError:
        return None
    if isinstance(exc, openai.AuthenticationError):
        return "ключ API не подходит"
    if isinstance(exc, openai.RateLimitError):
        return "исчерпан лимит запросов у провайдера, попробуйте позже"
    if isinstance(exc, openai.APIConnectionError):
        return "нет связи с сервером LLM"
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 402:
            return "у провайдера закончился оплаченный баланс: пополните его или привяжите платёжный аккаунт с кредитами"
        return f"ошибка сервера LLM ({exc.status_code}): {str(exc)[:200]}"
    return None


def _trim_history(history: list[dict], limit: int | None = None) -> list[dict]:
    """Оставляет последние сообщения (не больше limit), начиная с реплики пользователя: результат инструмента без
    своего вызова провайдер не примет."""
    limit = HISTORY_MAX_MESSAGES if limit is None else limit
    if len(history) <= limit:
        return history
    tail = history[-limit:]
    for i, m in enumerate(tail):
        if m.get("role") == "user":
            return tail[i:]
    return []


TEACHER_LOG = ML_DIR / "teacher.jsonl"
_ACTION_TOOLS = ("system_action", "open_app", "open_web", "translate")


def _teach(phrase: str, executor: "ToolExecutor") -> None:
    """Большая модель учит маленькую: фраза, которую второй слой выполнил одним действием, — готовый пример для
    дообучения первого слоя на настоящих формулировках пользователя (ml/teacher.jsonl, разбирает лаборатория)."""
    acts = [(n, a) for n, a in executor.calls if n in _ACTION_TOOLS]
    if executor.dry_run or len(acts) != 1:
        return
    try:
        with TEACHER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "phrase": phrase, "tool": acts[0][0],
                                "args": acts[0][1]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class LLMAgent:
    def __init__(self, client=None, executor: ToolExecutor | None = None, provider: Provider | None = None):
        self._client = client
        self.provider = provider
        self.executor = executor or ToolExecutor()
        self.tools = build_tools()
        self.history: list[dict] = []
        self.last_used = 0.0

    def _ensure(self):
        if self.provider is None:
            self.provider = current_provider()
        if self._client is None:
            self._client = make_client(self.provider)
        return self._client

    def _request(self, messages: list[dict], tools: bool = True, effort: str | None = None):
        client = self._ensure()
        return client.chat.completions.create(
            model=self.provider.model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            tools=self.tools,
            tool_choice="auto" if tools else "none",
            **({"reasoning_effort": effort} if effort else {}),
        )

    def handle(self, phrase: str, hint: str = "") -> str:
        """Одна фраза пользователя -> ответ для озвучивания (действия выполняются по ходу)."""
        now = time.monotonic()
        if now - self.last_used > HISTORY_IDLE_SECONDS:
            self.history = []
        self.history = _trim_history(self.history)
        self.last_used = now
        self.executor.log, self.executor.calls = [], []
        content = phrase if not hint else f"{phrase}\n\n(первый слой не справился: {hint})"
        messages = self.history + [{"role": "user", "content": content}]
        try:
            for step in range(MAX_TOOL_ROUNDS):
                response = self._request(messages, effort=THINK_FIRST if step == 0 else THINK_AFTER_TOOLS)
                choice = response.choices[0]
                msg = choice.message
                messages.append(_assistant_message(msg))
                calls = msg.tool_calls or []
                if not calls:
                    self.history = messages
                    _teach(phrase, self.executor)
                    text = (msg.content or "").strip()
                    if choice.finish_reason == "length":
                        text += " (ответ обрезан)"
                    return text or "Готово."
                for call in calls:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": self._run_call(call)})
            # шаги кончились: пусть модель без инструментов скажет, что успела выяснить и что осталось
            messages.append({"role": "user", "content": "Шаги закончились. Не вызывая инструментов, коротко скажи, что "
                                                        "удалось сделать или выяснить и что осталось."})
            final = self._request(messages, tools=False, effort=THINK_AFTER_TOOLS).choices[0].message
            self.history = []
            return (final.content or "").strip() or "Не получилось закончить за разумное число шагов, остановилась."
        except LLMUnavailable:
            raise
        except Exception as exc:
            reason = _api_error_text(exc)
            if reason is None:
                raise
            raise LLMUnavailable(reason) from exc

    def _run_call(self, call) -> str:
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("аргументы должны быть объектом JSON")
        except (json.JSONDecodeError, ValueError) as exc:
            return f"Ошибка: неверные аргументы инструмента ({exc}). Повторите вызов с корректным JSON."
        try:
            return str(self.executor.run(call.function.name, args))[:8000]
        except Exception as exc:                       # ошибка инструмента -> модель увидит и решит, что делать
            return f"Ошибка: {exc}"



_AGENT: LLMAgent | None = None


def get_agent(dry_run: bool = False) -> LLMAgent:
    """Один агент на сессию ассистента: так он помнит предыдущие реплики («а теперь заархивируй их»)."""
    global _AGENT
    if _AGENT is None or _AGENT.executor.dry_run != dry_run:
        _AGENT = LLMAgent(executor=ToolExecutor(dry_run=dry_run))
    return _AGENT
