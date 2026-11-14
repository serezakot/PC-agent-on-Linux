#!/usr/bin/env python3
"""Второй слой ассистента: большая LLM (Claude, Anthropic API) для того, что не смог первый слой.

Сюда попадают только фразы, которые парсер отдал в маршрут `llm`: вопросы, размытые цели, многошаговые просьбы
(«скопируй все pdf из загрузок в документы и заархивируй»), неуверенные случаи. LLM ничего не выполняет сама:
она вызывает ТЕ ЖЕ исполнители, что и первый слой, как инструменты:
    system_action — любое из действий ml_spec.SYSTEM_ACTIONS (system_control)
    open_app      — программа / папка / файл / игра (app_launcher)
    open_web      — сайт, поиск, YouTube (gogle_search)
    translate     — Google Переводчик (gogle_search)
    list_folder, find_files — только чтение: что лежит в папке, где файл
Опасные действия (удаление, выключение, закрытие всех окон...) выполняются только после подтверждения.

Ключ API: переменная ANTHROPIC_API_KEY или файл ~/.config/pc_agent/anthropic.env со строкой ANTHROPIC_API_KEY=...
Настройки: PC_AGENT_LLM_MODEL (по умолчанию claude-opus-5-5), PC_AGENT_LLM_EFFORT (low|medium|high, по умолчанию medium).

    python3 llm_agent.py "скопируй все pdf из загрузок в документы"
    python3 llm_agent.py --dry-run "что у меня на рабочем столе и что из этого можно удалить"
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
if str(HERE / "ml") not in sys.path:
    sys.path.insert(0, str(HERE / "ml"))

import app_launcher                 # noqa: E402
import gogle_search                 # noqa: E402
import ml_spec                      # noqa: E402
import system_control               # noqa: E402

KEY_FILE = Path.home() / ".config" / "pc_agent" / "anthropic.env"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 12
HISTORY_IDLE_SECONDS = 15 * 60          # после паузы разговор начинается заново
HISTORY_MAX_TURNS = 20

# Действия, которые нельзя выполнять без «да» от пользователя (необратимые или выключающие компьютер)
DANGEROUS = {
    "fs_trash", "fs_empty_trash", "fs_clean_downloads", "fs_move", "fs_rename", "pkg_remove", "win_close_all",
    "power_off", "power_reboot", "power_hibernate", "power_sleep", "session_logout", "session_switch_user",
}


class LLMUnavailable(RuntimeError):
    """Нет ключа API, пакета anthropic или сети."""


# =============================================================================
# Инструменты: описания строятся из того, что реально умеют исполнители
# =============================================================================
def _action_catalogue() -> str:
    """Список действий, сгруппированный по модулям system/ (берётся из реестра обработчиков, не пишется руками)."""
    groups: dict[str, list[str]] = {}
    for action in ml_spec.SYSTEM_ACTIONS:
        handler = system_control.ACTION_HANDLERS.get(action)
        domain = handler.__module__.rsplit(".", 1)[-1] if handler else "other"
        groups.setdefault(domain, []).append(action)
    return "\n".join(f"{domain}: {', '.join(actions)}" for domain, actions in sorted(groups.items()))


def build_tools() -> list[dict]:
    return [
        {
            "name": "system_action",
            "description": (
                "Run one Ubuntu system action (the same executor the voice parser uses). Arguments are plain words, "
                "the executor resolves them: query = the object (file/folder name as the user calls it, percent like "
                "'30', duration like '10 минут', resolution like '1920 на 1080', app or game to close, Bluetooth device, "
                "time for a reminder like '18:00' or '20 минут'); site = destination folder ('рабочий стол', "
                "'документы', a folder name) or the new name for fs_rename; text = reminder message (reminder_set only). "
                "fs_copy with no site puts the file into the clipboard; clip_paste with site pastes clipboard files there. "
                "Leave unused fields empty. Actions by domain:\n" + _action_catalogue()
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ml_spec.SYSTEM_ACTIONS)},
                    "query": {"type": "string"},
                    "site": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["action", "query", "site", "text"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "open_app",
            "description": (
                "Open an installed program, a game from the Steam library, a standard folder (загрузки, документы, "
                "рабочий стол, корзина...) or a user file/folder by name. name = what to open in the user's words "
                "('блокнот', 'пайчарм', 'доту', 'traning ml'); inner = optional action inside it ('в папке education' "
                "for a terminal, a game to find in Steam, a song in Spotify, 'отсортируй по дате' for a folder)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "inner": {"type": "string"}},
                "required": ["name", "inner"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "open_web",
            "description": (
                "Open the browser. site = site name in the user's words ('ютуб', 'github'), empty for a Google search; "
                "query = what to search or play. mode: web_open (just the site), web_search (search results), "
                "web_open_first (open the first video/result directly), web_channel (a YouTube channel - only when the "
                "user asked for a channel)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "site": {"type": "string"},
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": list(ml_spec.BROWSER_ACTIONS)},
                },
                "required": ["site", "query", "mode"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "translate",
            "description": "Open Google Translate with the text. lang = target language code ('en', 'de', ...) or 'none'.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "lang": {"type": "string", "enum": list(ml_spec.LANGS)}},
                "required": ["text", "lang"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "list_folder",
            "description": (
                "Read-only: list what is inside a folder (names, file/folder, size, modification date). folder = a path "
                "('~/Desktop') or a name in the user's words ('загрузки', 'рабочий стол', 'traning ml'). Use it before "
                "acting on files you have not seen, instead of guessing names."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"folder": {"type": "string"}},
                "required": ["folder"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "find_files",
            "description": (
                "Read-only: find files and folders in the home directory whose name contains `pattern` "
                "(case-insensitive) or ends with an extension like '.pdf'. Returns up to 50 full paths."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


SYSTEM_PROMPT = """You are the second layer of a Russian-speaking voice assistant on the user's own Ubuntu 24.04 laptop \
(GNOME, Wayland). A small local parser already handled everything simple; you get what it could not: questions, vague \
goals, multi-step requests, and phrases it was unsure about. Its guess is given to you as a hint and may be wrong.

How to work:
- If the request is something to do on the computer, do it with the tools. You can chain several tool calls for \
multi-step requests. Use list_folder / find_files to look before acting on files; never invent file names.
- Paths and names you pass to tools may be plain words; the executors resolve them against the real disk.
- If the request is a question or conversation, just answer it. You have no internet access except opening pages \
for the user in the browser.
- If the request is ambiguous in a way that changes what you would do, ask one short question instead of guessing.
- Destructive actions (deleting, moving, renaming, shutting down, closing all windows, removing programs) are \
confirmed by the user automatically before they run; only start them when the user actually asked for that.
- Smart-home devices (lights, kettle, curtains) are not connected; say so briefly.

Your reply is read aloud: answer in Russian, in one to three short sentences, plain text, no markdown, no lists \
unless the user asked for a list. After acting, say what you did, not how."""


# =============================================================================
# Выполнение инструментов
# =============================================================================
def _human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _resolve_folder(folder: str) -> Path | None:
    raw = (folder or "").strip()
    if raw.startswith(("~", "/")):
        p = Path(raw).expanduser()
        return p if p.is_dir() else None
    home = Path.home()
    plan = app_launcher._match_standard_folder(app_launcher._norm(raw), "", home, app_launcher.xdg_user_dirs(home))
    if plan is not None and plan.kind == "folder" and not plan.target.startswith("trash"):
        return Path(plan.target)
    found = app_launcher.find_local_item(raw, home)
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


def _find_files(pattern: str) -> str:
    pat = (pattern or "").strip().lower()
    if len(pat) < 2:
        raise ValueError("слишком короткий образец для поиска")
    skip = {"node_modules", "__pycache__", ".venv", "venv", "snap", "site-packages", ".cache", ".local", ".config", ".git"}
    hits, deadline = [], time.monotonic() + 3.0
    for root, dirs, files in os.walk(Path.home()):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        for name in dirs + files:
            n = name.lower()
            if (pat.startswith(".") and n.endswith(pat)) or (not pat.startswith(".") and pat in n):
                hits.append(str(Path(root) / name))
        if len(hits) >= 50 or time.monotonic() > deadline:
            break
    return "\n".join(hits[:50]) if hits else "ничего не найдено"


class ToolExecutor:
    """Выполняет вызовы инструментов через исполнители первого слоя. confirm(вопрос) -> bool для опасных действий."""

    def __init__(self, dry_run: bool = False, confirm: Callable[[str], bool] | None = None):
        self.dry_run = dry_run
        self.confirm = confirm or (lambda question: input(f"{question} [да/нет] ").strip().lower() in ("да", "д", "y", "yes"))

    def run(self, name: str, args: dict) -> str:
        if name == "system_action":
            action = args.get("action", "")
            plan = system_control.make_system_plan(query=args.get("query", ""), action=action, site=args.get("site", ""),
                                                   text=args.get("text", ""))
            if plan is None:
                raise ValueError(f"нет такого действия: {action}")
            if action in DANGEROUS and not self.dry_run and not self.confirm(f"Выполнить: {plan.description}?"):
                return "Пользователь отказался, действие не выполнено."
            return system_control.execute_system_plan(plan, dry_run=self.dry_run)
        if name == "open_app":
            plan = app_launcher.make_app_plan(args.get("name", ""), args.get("inner", ""))
            if plan is None:
                raise ValueError("не указано, что открыть")
            return app_launcher.execute_app_plan(plan, dry_run=self.dry_run)
        if name == "open_web":
            resolver = (lambda url: None) if self.dry_run else None
            plan = gogle_search.make_plan(args.get("site", ""), args.get("query", ""), resolver=resolver, action=args.get("mode", "web_search"))
            if plan is None:
                raise ValueError("нечего открывать: пустые site и query")
            if not self.dry_run:
                gogle_search.open_in_browser(plan.url)
            return f"[dry-run] {plan.url}" if self.dry_run else gogle_search._describe(plan)
        if name == "translate":
            plan = gogle_search.make_translate_plan(args.get("text", ""), args.get("lang", "none"))
            if plan is None:
                raise ValueError("нечего переводить")
            if not self.dry_run:
                gogle_search.open_in_browser(plan.url)
            return f"[dry-run] {plan.url}" if self.dry_run else gogle_search._describe(plan)
        if name == "list_folder":
            return _list_folder(args.get("folder", ""))
        if name == "find_files":
            return _find_files(args.get("pattern", ""))
        raise ValueError(f"неизвестный инструмент: {name}")


# =============================================================================
# Агент: цикл запрос -> инструменты -> ответ
# =============================================================================
def _load_key_file() -> None:
    if os.environ.get("ANTHROPIC_API_KEY") or not KEY_FILE.is_file():
        return
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")


def make_client():
    _load_key_file()
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("не установлен пакет anthropic: pip install anthropic") from exc
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise LLMUnavailable(f"нет ключа API: задайте ANTHROPIC_API_KEY или положите его в {KEY_FILE}")
    return anthropic.Anthropic(timeout=60.0, max_retries=2)


class LLMAgent:
    def __init__(self, client=None, executor: ToolExecutor | None = None, model: str | None = None, effort: str | None = None):
        self._client = client
        self.executor = executor or ToolExecutor()
        self.model = model or os.environ.get("PC_AGENT_LLM_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("PC_AGENT_LLM_EFFORT", "medium")
        self.tools = build_tools()
        self.history: list[dict] = []
        self.last_used = 0.0

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _request(self, messages: list[dict]):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},              # системный промпт и инструменты одинаковы в каждом запросе
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                              # при отказе модели запрос сам перейдёт на подходящую модель
        )

    def handle(self, phrase: str, hint: str = "") -> str:
        """Одна фраза пользователя -> ответ для озвучивания (действия выполняются по ходу)."""
        now = time.monotonic()
        if now - self.last_used > HISTORY_IDLE_SECONDS or len(self.history) > HISTORY_MAX_TURNS * 4:
            self.history = []
        self.last_used = now
        content = phrase if not hint else f"{phrase}\n\n(подсказка первого слоя: {hint})"
        messages = self.history + [{"role": "user", "content": content}]
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._request(messages)
            messages.append({"role": "assistant", "content": response.content})   # целиком: блоки thinking тоже
            if response.stop_reason == "refusal":
                self.history = []
                return "Эту просьбу я выполнить не могу."
            if response.stop_reason == "pause_turn":
                continue
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                self.history = messages
                text = " ".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    text += " (ответ обрезан)"
                return text or "Готово."
            results = []
            for call in calls:
                try:
                    out = self.executor.run(call.name, dict(call.input))
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": str(out)[:8000]})
                except Exception as exc:                       # ошибка инструмента -> модель увидит и решит, что делать
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Ошибка: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        self.history = []
        return "Не получилось закончить за разумное число шагов, остановилась."


_AGENT: LLMAgent | None = None


def get_agent(dry_run: bool = False) -> LLMAgent:
    """Один агент на сессию ассистента: так он помнит предыдущие реплики («а теперь заархивируй их»)."""
    global _AGENT
    if _AGENT is None or _AGENT.executor.dry_run != dry_run:
        _AGENT = LLMAgent(executor=ToolExecutor(dry_run=dry_run))
    return _AGENT


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    phrase = " ".join(a for a in argv if a != "--dry-run").strip()
    if not phrase:
        print(__doc__)
        return 1
    try:
        print(get_agent(dry_run).handle(phrase))
        return 0
    except LLMUnavailable as exc:
        print(f"LLM недоступна: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
