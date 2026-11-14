#!/usr/bin/env python3
"""Единый диспетчер команд ассистента для Ubuntu 24.04 LTS.

Соединяет обученную модель (`ml_infer.py`) и все исполнительные модули:
  • route == "browser"   -> `gogle_search.py` (поиск в Google, открытие сайта, поиск внутри сайта)
  • route == "translate" -> `gogle_search.py` (Google Переводчик с автоопределением языка)
  • route == "app"       -> `app_launcher.py` (программы Ubuntu/Snap/Flatpak/.desktop, настройки GNOME, папки XDG, файлы)
  • route == "system"    -> `system_control.py` (звук PipeWire, яркость, тема, Wi-Fi/Bluetooth, скриншоты, питание, файлы)
  • route == "llm"       -> передача вопроса большой LLM (пока выводит уведомление, ничего лишнего не открывает)

Запуск:
    python3 assistant.py                                         # интерактивный диалог
    python3 assistant.py "Открой Steam и найди Counter-Strike 2"
    python3 assistant.py --dry-run "сделай скриншот на рабочий стол"
"""
from __future__ import annotations

import sys

import app_launcher
import gogle_search
import system_control

EXIT_WORDS = {"exit", "выход"}


def dispatch_phrase(phrase: str, parser=gogle_search.model_parse, dry_run: bool = False):
    """Разбирает фразу моделью и направляет в нужный исполнитель (browser/translate/app/system/llm)."""
    result = parser(phrase)
    route = result.get("route")
    action = result.get("action") or ""

    # «открой traning ml»: если так называется папка или файл пользователя, это локальный объект, а не сайт
    if route == "browser" and action == "web_open" and result.get("site") and not result.get("query"):
        local = app_launcher.find_local_item(result["site"])
        if local is not None:
            result = dict(result, route="app", action="app_open")
            route, action = "app", "app_open"

    if route in ("browser", "translate"):
        opener = (lambda url: None) if dry_run else gogle_search.open_in_browser
        resolver = (lambda url: None) if dry_run else None
        if route == "browser":
            plan = gogle_search.make_plan(
                result.get("site", ""),
                result.get("query", ""),
                resolver=resolver,
                action=action or "web_search",
            )
        else:
            plan = gogle_search.make_translate_plan(result.get("text", ""), result.get("lang", "none"))
        if plan is None:
            msg = "Искать нечего." if route == "browser" else "Переводить нечего."
            return result, None, msg
        opener(plan.url)
        msg = f"[dry-run] {plan.url}" if dry_run else gogle_search._describe(plan)
        return result, plan, msg

    if route == "app":
        plan = app_launcher.make_app_plan(
            result.get("site", ""),
            result.get("query", ""),
            action=action,
        )
        if plan is None:
            return result, None, "Не указана программа или папка для открытия."
        msg = app_launcher.execute_app_plan(plan, dry_run=dry_run)
        return result, plan, msg

    if route == "system":
        plan = system_control.make_system_plan(
            query=result.get("query", ""),
            action=action,
            site=result.get("site", ""),
            raw_phrase=phrase,
            text=result.get("text", ""),
        )
        if plan is None:
            return result, None, f"Системная команда не распознана: {result.get('query') or phrase}"
        msg = system_control.execute_system_plan(plan, dry_run=dry_run)
        return result, plan, msg

    if result.get("fallback"):
        return result, None, f"Не уверена, что именно сделать ({result.get('reason')}): передаю запрос большой LLM (пока не подключена)."
    return result, None, "Это вопрос или задача для большой LLM (пока не подключена)."


def _run_once(phrase: str, dry_run: bool) -> bool:
    if not gogle_search.normalize_query(phrase):
        return True
    try:
        result, _plan, msg = dispatch_phrase(phrase, dry_run=dry_run)
    except (
        gogle_search.ModelError,
        gogle_search.BrowserError,
        app_launcher.AppLaunchError,
        system_control.SystemCommandError,
    ) as exc:
        print(f"Ошибка: {exc}")
        return False
    print(gogle_search.describe_decision(result))
    if msg:
        print(msg)
    return True


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]

    if args:
        text = " ".join(args)
        if not gogle_search.normalize_query(text):
            print("Пустой запрос.")
            return 1
        return 0 if _run_once(text, dry_run=dry_run) else 1

    try:
        sys.stdin.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    print("Ассистент Ubuntu 24.04 LTS запущен (exit или выход — закрыть).")
    while True:
        try:
            query = input("assistant> ").replace("\ufffd", "")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if gogle_search.normalize_query(query).lower() in EXIT_WORDS:
            return 0
        _run_once(query, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
