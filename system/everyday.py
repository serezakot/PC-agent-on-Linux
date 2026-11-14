"""Повседневное: таймер, напоминание (с текстом), установка / удаление программ через Центр приложений."""
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

from .core import SystemCommandError, SystemPlan, _REMINDER_TEXT, _norm, _register  # noqa: F401

# --- Таймеры и напоминания: отдельный процесс (переживает выход ассистента) + уведомление GNOME + звук ---
_NUM_WORDS = {"одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
              "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
              "пятьдесят": 50, "шестьдесят": 60, "девяносто": 90, "сто": 100, "полтора": 1.5, "полторы": 1.5}


def _parse_duration_seconds(query: str) -> int | None:
    """«10 минут», «полтора часа», «1 час 30 минут», «сорок секунд», «две с половиной минуты», «полчаса» -> секунды."""
    q = _norm(query).replace("ё", "е")
    if not q:
        return None
    if "полчаса" in q:
        return 1800
    if "полминуты" in q:
        return 30
    total = 0.0
    found = False
    q = re.sub(r"(\S+) с половиной", lambda m: f"{_NUM_WORDS.get(m.group(1), m.group(1))}.5", q)   # «две с половиной» -> 2.5
    for m in re.finditer(r"(\d+(?:[.,]\d+)?|[а-я]+)(?:\s+и)?\s*(секунд\w*|сек\b|минут\w*|мин\b|час\w*)", q):
        num_s, unit = m.group(1), m.group(2)
        if num_s.replace(",", ".").replace(".", "", 1).isdigit():
            num = float(num_s.replace(",", "."))
        elif num_s in _NUM_WORDS:
            num = float(_NUM_WORDS[num_s])
        elif num_s in ("минуту", "час", "секунду"):
            num = 1.0
        else:
            continue
        found = True
        total += num * (1 if unit.startswith("сек") else 60 if unit.startswith("мин") else 3600)
    if not found:
        if re.search(r"\bминут[уа]?\b", q):
            return 60
        if re.search(r"\bчас\b", q):
            return 3600
        m = re.search(r"(\d+)", q)
        return int(m.group(1)) * 60 if m else None
    return int(total) if total > 0 else None


_HOUR_WORDS = {"час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
               "одиннадцать": 11, "двенадцать": 12}
_DAY_WORDS = {"утром": (9, 0), "днем": (13, 0), "в обед": (13, 0), "после обеда": (15, 0), "после работы": (18, 30), "вечером": (19, 0),
              "перед сном": (22, 0), "ночью": (23, 0)}
_MINUTE_WORDS = {"пяти": 5, "десяти": 10, "пятнадцати": 15, "двадцати": 20, "двадцати пяти": 25}
_WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4, "субботу": 5, "суббота": 5,
             "воскресенье": 6}


def _parse_clock_seconds(query: str) -> tuple[int, str] | None:
    """«в 18:00», «в 7 утра», «семь тридцать», «вечером», «завтра в 10», «в субботу утром» -> (секунд до момента, 'когда')."""
    q = _norm(query).replace("ё", "е")
    now = dt.datetime.now()
    days_ahead = 2 if "послезавтра" in q else 1 if "завтра" in q else 0
    weekday = next((d for w, d in _WEEKDAYS.items() if re.search(rf"\b{w}\b", q)), None)
    hh = mm = None
    if "полдень" in q:
        hh, mm = 12, 0
    elif "полночь" in q:
        hh, mm = 0, 0
    else:
        q_num = q
        m_bez = re.search(r"без\s+(двадцати пяти|двадцати|пятнадцати|десяти|пяти)\s+(\w+)", q_num)
        if m_bez:
            hour_w = m_bez.group(2)
            hour = _HOUR_WORDS.get(hour_w, int(hour_w) if hour_w.isdigit() else None)
            if hour is not None:
                q_num = q_num.replace(m_bez.group(0), f"{(hour - 1) % 24}:{60 - _MINUTE_WORDS[m_bez.group(1)]:02d}")
        for w, n in _HOUR_WORDS.items():
            q_num = re.sub(rf"\b{w}\b", str(n), q_num)
        q_num = q_num.replace(" тридцать", ":30").replace(" пятнадцать", ":15").replace(" сорок пять", ":45")
        m = re.search(r"(?<!\d)(\d{1,2})(?:[:.\s](\d{2}))?(?:\s*(утра|вечера|дня|ночи|часов))?", q_num)
        if m and not re.search(r"\d\s*(?:минут|секунд|час(?:а|ов)?\b(?!\s*(?:утра|вечера)))", q_num[m.start():m.end() + 8]) or (m and ("завтра" in q or weekday is not None or re.search(r"\b(?:утра|вечера|дня|ночи|часов)\b|\d:\d{2}", q_num))):
            hh, mm = int(m.group(1)), int(m.group(2) or 0)
            suffix = m.group(3) or ""
            if suffix in ("вечера", "дня") and hh < 12:
                hh += 12
            if suffix == "ночи" and hh == 12:
                hh = 0
        else:
            for w, (h_, m_) in _DAY_WORDS.items():
                if w in q:
                    hh, mm = h_, m_
                    break
            if hh is None and (days_ahead or weekday is not None):
                hh, mm = 10, 0
            if hh is None:
                return None
            suffix = "часов"
        if hh > 23 or mm > 59:
            return None
        if suffix == "" and hh <= 12 and hh != 0 and not days_ahead and weekday is None:
            # «в 7» без уточнения: ближайшее будущее время из 7:00 / 19:00
            cand = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0)
            cand2 = now.replace(hour=(hh + 12) % 24, minute=mm, second=0, microsecond=0)
            opts = [c_ for c_ in (cand, cand2) if c_ > now] or [cand + dt.timedelta(days=1)]
            target = min(opts)
            return int((target - now).total_seconds()), f"{target:%H:%M}"
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if weekday is not None:
        delta = (weekday - now.weekday()) % 7
        if delta == 0 and target <= now:
            delta = 7
        target += dt.timedelta(days=delta)
    elif days_ahead:
        target += dt.timedelta(days=days_ahead)
    elif target <= now:
        target += dt.timedelta(days=1)
    label = f"{target:%H:%M}" if target.date() == now.date() else f"{target:%d.%m %H:%M}"
    return int((target - now).total_seconds()), label


def _human_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s_ = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if s_ or not parts:
        parts.append(f"{s_} сек")
    return " ".join(parts)


def _schedule_notification(seconds: int, title: str, body: str) -> None:
    """Запускает отдельный процесс: sleep -> notify-send (критичное уведомление) -> звук."""
    sound = ""
    for cand in ("/usr/share/sounds/freedesktop/stereo/complete.oga", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                 "/usr/share/sounds/Yaru/stereo/complete.oga"):
        if Path(cand).exists():
            sound = cand
            break
    play = f"paplay {shlex.quote(sound)} || canberra-gtk-play -f {shlex.quote(sound)}" if sound else "true"
    script = (f"sleep {int(seconds)}; notify-send -u critical -a 'Ассистент' {shlex.quote(title)} {shlex.quote(body)}; "
              f"for i in 1 2 3; do {play} >/dev/null 2>&1; sleep 1; done")
    subprocess.Popen(["bash", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


@_register("timer_set")
def _h_timer_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    secs = _parse_duration_seconds(query)

    def _op() -> str:
        if secs is None:
            subprocess.Popen(["gnome-clocks"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "На сколько поставить таймер — не поняла; открыла Часы GNOME (вкладка «Таймер»)"
        _schedule_notification(secs, "Таймер", f"Прошло {_human_duration(secs)}")
        return f"Таймер на {_human_duration(secs)} запущен (сработает в {(dt.datetime.now() + dt.timedelta(seconds=secs)):%H:%M:%S})"

    return SystemPlan(category="timer", action="timer", description=f"Ставлю таймер на {_human_duration(secs) if secs else '?'}",
                      commands=(("notify-send", "Таймер"),), python_op=_op)


@_register("reminder_set")
def _h_reminder_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    when = _norm(query)
    is_duration = when.startswith("через") or bool(re.search(r"\b(?:минут\w*|секунд\w*|мин|сек|полчаса|полминуты)\b", when)) and not re.search(r"\b(?:утра|вечера|завтра|в обед|вечером|утром)\b", when)
    clock = None if is_duration else _parse_clock_seconds(query)
    secs = clock[0] if clock else _parse_duration_seconds(re.sub(r"^через\s+", "", when))
    label = clock[1] if clock else None

    def _op() -> str:
        if secs is None:
            subprocess.Popen(["gnome-clocks"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "Когда напомнить — не поняла; открыла Часы GNOME (вкладка «Будильники»)"
        msg_text = _REMINDER_TEXT.get("text") or ("Будильник" if "будильник" in when or not when else "Вы просили напомнить")
        _schedule_notification(secs, "Напоминание", msg_text)
        at = label or (dt.datetime.now() + dt.timedelta(seconds=secs)).strftime("%H:%M")
        return f"Напомню в {at}" + (f": «{_REMINDER_TEXT.get('text')}»" if _REMINDER_TEXT.get("text") else "")

    return SystemPlan(category="timer", action="reminder", description=f"Ставлю напоминание ({when or 'время не указано'})",
                      commands=(("notify-send", "Напоминание"),), python_op=_op)


def _store_op(name: str, remove: bool) -> str:
    from app_launcher import resolve_program, _translit
    n = _norm(name)
    if not n:
        raise SystemCommandError("не сказано, какую программу " + ("удалить" if remove else "установить"))
    prog = resolve_program(name)
    if prog is not None and not remove:
        return f"«{prog[0]}» уже установлена: {' '.join(prog[1])}"
    # В магазине имена латиницей: кириллическое название транслитерируем («обсидиан» -> obsidian)
    search = prog[0] if prog is not None else (_translit(name) if re.search(r"[а-яё]", n) else name)
    store = shutil.which("snap-store") or shutil.which("gnome-software")
    if store:
        subprocess.Popen([store, f"--search={search}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        verb = "Удалить" if remove else "Установить"
        return f"Открыла Центр приложений с поиском «{search}»: {verb.lower()} можно одной кнопкой (нужен пароль администратора)"
    subprocess.Popen(["xdg-open", f"https://snapcraft.io/search?q={quote(search)}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return f"Открыла каталог Snap с поиском «{search}»"


@_register("pkg_install")
def _h_pkg_install(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="packages", action="install", description=f"Устанавливаю «{query}» через Центр приложений",
                      commands=(("snap-store", f"--search={query}"),), python_op=lambda: _store_op(query, False))


@_register("pkg_remove")
def _h_pkg_remove(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="packages", action="remove", description=f"Удаляю «{query}» через Центр приложений",
                      commands=(("snap-store", f"--search={query}"),), python_op=lambda: _store_op(query, True))
