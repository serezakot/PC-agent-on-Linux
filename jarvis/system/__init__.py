"""Исполнитель системных команд Ubuntu 24.04 LTS — по одному модулю на домен (Wayland + GNOME 46, PipeWire/WirePlumber, DBus, NetworkManager).

Как работает модуль:
  • Модель (`ml_infer.py` / `ml_spec.py`) классифицирует намерение `route == "system"`,
    конкретный ID системного действия `action` (из 146 действий `SYSTEM_ACTIONS`)
    и выделяет аргументы:
      - `query` — целевой объект (имя файла/папки, процент громкости/яркости, секунды перемотки, приложение);
      - `site`  — папка назначения/источника или новое имя при переименовании.
  • Этот модуль не разбирает фразы: он вызывает обработчик `ACTION_HANDLERS[action](query, site, home, dirs)`
    и выполняет команду нативно в Ubuntu 24.04 LTS. Аргументы (проценты, секунды, время) разбираются из слотов.
  • На Wayland (GNOME 46) мультимедийные клавиши и хоткеи окон/вкладок/буфера обмена отправляются
    напрямую через композитор `org.gnome.Mutter.RemoteDesktop` (системный `/usr/bin/python3` с `gi.repository.Gio`),
    поэтому работают без root и обходят изоляцию Snap между PyCharm/терминалом и Firefox/Spotify.
"""
from . import audio, core, display, everyday, files, info, keyboard, media, network, power, screenshots, windows  # noqa: F401  (регистрируют действия)
from .core import ACTION_HANDLERS, SystemCommandError, SystemPlan, execute_system_plan, make_system_plan  # noqa: F401
