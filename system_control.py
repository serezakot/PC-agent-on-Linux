#!/usr/bin/env python3
"""Исполнитель системных команд Ubuntu 24.04 LTS — фасад над пакетом system/ (по одному модулю на домен) (Wayland + GNOME 46, PipeWire/WirePlumber, DBus, NetworkManager).

Как работает модуль:
  • Модель (`ml_infer.py` / `ml_spec.py`) классифицирует намерение `route == "system"`,
    конкретный ID системного действия `action` (из 104 канонических действий `SYSTEM_ACTIONS`)
    и выделяет аргументы:
      - `query` — целевой объект (имя файла/папки, процент громкости/яркости, секунды перемотки, приложение);
      - `site`  — папка назначения/источника или новое имя при переименовании.
  • Этот модуль не разбирает фразы: он вызывает обработчик `ACTION_HANDLERS[action](query, site, home, dirs)`
    и выполняет команду нативно в Ubuntu 24.04 LTS. Аргументы (проценты, секунды, время) разбираются из слотов.
  • На Wayland (GNOME 46) мультимедийные клавиши и хоткеи окон/вкладок/буфера обмена отправляются
    напрямую через композитор `org.gnome.Mutter.RemoteDesktop` (системный `/usr/bin/python3` с `gi.repository.Gio`),
    поэтому работают без root и обходят изоляцию Snap между PyCharm/терминалом и Firefox/Spotify.
"""
from __future__ import annotations

import sys

from system import audio, core, display, everyday, files, info, keyboard, media, network, power, screenshots, windows  # noqa: F401
from system.core import ACTION_HANDLERS, SystemCommandError, SystemPlan, execute_system_plan, make_system_plan  # noqa: F401

# Совместимость: имена всех доменных модулей доступны и отсюда (system_control._parse_resolution, ...).
for _mod in (core, files, audio, display, power, network, screenshots, media, keyboard, windows, info, everyday):
    for _name, _value in vars(_mod).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)


def main(argv: list[str]) -> int:
    """CLI: фраза разбирается моделью, затем выполняется (или показывается при --dry-run)."""
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if not args:
        print("Использование: python3 system_control.py [--dry-run] <системная команда>")
        return 1
    raw_phrase = " ".join(args)
    from gogle_search import model_parse
    res = model_parse(raw_phrase)
    if res.get("route") != "system":
        print(f"Это не системная команда (модель: {res.get('route')}{' / ' + res.get('reason') if res.get('fallback') else ''})", file=sys.stderr)
        return 1
    plan = make_system_plan(query=res.get("query", ""), action=res.get("action", ""), site=res.get("site", ""), raw_phrase=raw_phrase, text=res.get("text", ""))
    if plan is None:
        print(f"Нет обработчика для действия {res.get('action')!r}", file=sys.stderr)
        return 1
    try:
        msg = execute_system_plan(plan, dry_run=dry_run)
        print(msg)
        return 0
    except SystemCommandError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
