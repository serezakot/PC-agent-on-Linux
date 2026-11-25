"""Звук: громкость и микрофон (PipeWire wpctl / pactl), переключение устройства вывода."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .core import SystemCommandError, SystemPlan, _norm, _parse_percent, _register, name_similarity_safe

# --- 4.1. Звук и микрофон (PipeWire wpctl + PulseAudio pactl) ---
@_register("vol_mute")
def _h_vol_mute(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="audio",
        action="volume_mute",
        description="Выключаю звук на компьютере",
        commands=(("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"),),
        fallback_commands=(("pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"),),
    )


@_register("vol_unmute")
def _h_vol_unmute(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="audio",
        action="volume_unmute",
        description="Включаю звук на компьютере",
        commands=(("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"),),
        fallback_commands=(("pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"),),
    )


@_register("vol_set")
def _h_vol_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    pct = _parse_percent(query, default=50)
    return SystemPlan(
        category="audio",
        action="volume_set",
        description=f"Устанавливаю громкость на {pct}%",
        commands=(
            ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct}%"),
            ("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"),
        ),
        fallback_commands=(("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"),),
    )


@_register("vol_up")
def _h_vol_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="audio",
        action="volume_up",
        description=f"Прибавляю громкость на {step}%",
        commands=(
            ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{step}%+"),
            ("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"),
        ),
        fallback_commands=(("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{step}%"),),
    )


@_register("vol_down")
def _h_vol_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="audio",
        action="volume_down",
        description=f"Убавляю громкость на {step}%",
        commands=(("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{step}%-"),),
        fallback_commands=(("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{step}%"),),
    )


@_register("mic_mute")
def _h_mic_mute(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="audio",
        action="mic_mute",
        description="Выключаю микрофон",
        commands=(("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"),),
        fallback_commands=(("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"),),
    )


@_register("mic_unmute")
def _h_mic_unmute(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="audio",
        action="mic_unmute",
        description="Включаю микрофон",
        commands=(("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"),),
        fallback_commands=(("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"),),
    )


@_register("mic_set")
def _h_mic_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    pct = _parse_percent(query, default=50)
    return SystemPlan(
        category="audio",
        action="mic_set",
        description=f"Устанавливаю чувствительность микрофона на {pct}%",
        commands=(("wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{pct}%"),),
        fallback_commands=(("pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{pct}%"),),
    )


@_register("mic_up")
def _h_mic_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="audio",
        action="mic_up",
        description=f"Прибавляю громкость микрофона на {step}%",
        commands=(("wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{step}%+"),),
        fallback_commands=(("pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"+{step}%"),),
    )


@_register("mic_down")
def _h_mic_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    step = _parse_percent(query, default=10)
    return SystemPlan(
        category="audio",
        action="mic_down",
        description=f"Убавляю громкость микрофона на {step}%",
        commands=(("wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{step}%-"),),
        fallback_commands=(("pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"-{step}%"),),
    )


_AUDIO_KEYWORDS = (
    (("наушник", "гарнитур", "headphone", "headset"), ("headphone", "headset", "bluez", "usb", "a2dp")),
    (("блютуз", "bluetooth", "беспровод", "airpods", "эирподс", "jbl", "джибиэль", "buds"), ("bluez", "a2dp", "bluetooth")),
    (("колонк", "динамик", "встроен", "speaker"), ("speaker", "analog", "built-in", "builtin", "pci")),
    (("hdmi", "монитор", "телевизор", "displayport", "дисплей"), ("hdmi", "displayport", "dp")),
    (("usb", "звуков", "карт", "усилител"), ("usb",)),
)


def _audio_output_op(query: str) -> str:
    try:
        res = subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=4.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SystemCommandError("wpctl (PipeWire) недоступен")
    text = res.stdout or ""
    sinks = []
    in_sinks = False
    for line in text.splitlines():
        if re.search(r"\bSinks:", line):
            in_sinks = True
            continue
        if in_sinks:
            if re.search(r"\b(Sources|Filters|Streams|Sink endpoints|Source endpoints):", line) or not line.strip(" │├└─"):
                if sinks:
                    break
                in_sinks = bool(line.strip(" │├└─")) and not re.search(r"\b(Sources|Filters|Streams):", line)
                continue
            m = re.search(r"(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*)?$", line.strip(" │├└─"))
            if m:
                sinks.append((m.group(2), m.group(3).strip(), bool(m.group(1))))
    if not sinks:
        raise SystemCommandError("устройства вывода звука не найдены")
    q = _norm(query)
    wanted: tuple[str, ...] = ()
    for spoken, techs in _AUDIO_KEYWORDS:
        if any(w in q for w in spoken):
            wanted = techs
            break
    best = None
    for sid, name, _current in sinks:
        n = name.lower()
        score = sum(1 for t in wanted if re.search(rf"(?<![a-z]){re.escape(t)}" + (r"(?![a-z])" if len(t) <= 3 else ""), n)) * 10   # «dp» не внутри «headphones»
        score += int(round(name_similarity_safe(q, name) * 5))
        if best is None or score > best[0]:
            best = (score, sid, name)
    if wanted and best is not None and best[0] < 10:
        # Названный тип устройства (HDMI, наушники...) сейчас не подключён: не подменяем его другим молча
        best = None
    if best is None or best[0] <= 0:
        names = "; ".join(n for _, n, _ in sinks)
        raise SystemCommandError(f"не нашла устройство «{query}». Доступные выходы: {names}")
    subprocess.run(["wpctl", "set-default", best[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
    return f"Звук выводится на: {best[2]}"


@_register("audio_output_set")
def _h_audio_output_set(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(category="audio", action="audio_output", description=f"Переключаю вывод звука на «{query}»",
                      commands=(("wpctl", "status"),), python_op=lambda: _audio_output_op(query))
