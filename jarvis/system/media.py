"""Медиа: пауза, следующий / предыдущий трек, перемотка, субтитры, полный экран, скорость (MPRIS + мультимедийные клавиши)."""
from __future__ import annotations

from pathlib import Path

from .core import (
    KEY_C, KEY_COMMA, KEY_DOT, KEY_F, KEY_LEFT, KEY_LEFTSHIFT, KEY_N, KEY_NEXTSONG, KEY_PLAYPAUSE, KEY_PREVIOUSSONG,
    KEY_RIGHT, SystemCommandError, SystemPlan, _mpris_call, _parse_seconds, _register, _send_keys_or_xdotool,
    _wayland_send_keycodes,
)

# --- 4.7. Медиа и управление видеоплеером (MPRIS + Wayland Mutter.RemoteDesktop) ---
def _media_key_op(mpris_method: str, keycode: int, description: str, extra_combo: tuple[int, ...] | None = None) -> str:
    """Отправляет команду в MPRIS и эмулирует аппаратную мультимедийную клавишу через Wayland Mutter.RemoteDesktop."""
    # Либо MPRIS, либо клавиша — не оба: GNOME передаёт медиаклавишу тому же плееру, и команда сработала бы дважды
    # (пауза+воспроизведение = ничего, «следующий трек» = через один).
    if _mpris_call(mpris_method):
        return description
    key_ok = _wayland_send_keycodes((keycode,))
    if extra_combo is not None:
        _wayland_send_keycodes(extra_combo)
    if key_ok:
        return f"{description} (плеер не найден, нажата медиаклавиша)"
    raise SystemCommandError(f"не удалось выполнить медиа-команду: {description}")


@_register("media_pause")
def _h_media_pause(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    """«пауза» — именно пауза (MPRIS Pause), а не переключатель: на уже остановленном видео ничего не включит."""
    return SystemPlan(category="media", action="media_play_pause", description="Ставлю на паузу",
                      commands=(("playerctl", "pause"),), fallback_commands=(("xdotool", "key", "XF86AudioPause"),),
                      python_op=lambda: _media_key_op("Pause", KEY_PLAYPAUSE, "Поставила на паузу"))


@_register("media_resume")
def _h_media_resume(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    """«продолжи» — именно воспроизведение (MPRIS Play): на уже играющем видео не поставит паузу."""
    return SystemPlan(category="media", action="media_play_pause", description="Продолжаю воспроизведение",
                      commands=(("playerctl", "play"),), fallback_commands=(("xdotool", "key", "XF86AudioPlay"),),
                      python_op=lambda: _media_key_op("Play", KEY_PLAYPAUSE, "Продолжила воспроизведение"))


@_register("media_play_pause")
def _h_media_play_pause(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="media_play_pause",
        description="Переключаю паузу / воспроизведение медиа (Play/Pause)",
        commands=(("playerctl", "play-pause"),),
        fallback_commands=(("xdotool", "key", "XF86AudioPlay"),),
        python_op=lambda: _media_key_op("PlayPause", KEY_PLAYPAUSE, "Переключено воспроизведение / пауза (Play/Pause)"),
    )


@_register("media_next")
def _h_media_next(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="media_next",
        description="Переключаю на следующий трек / видео (Next)",
        commands=(("playerctl", "next"),),
        fallback_commands=(("xdotool", "key", "Shift+N"), ("xdotool", "key", "XF86AudioNext")),
        python_op=lambda: _media_key_op(
            "Next",
            KEY_NEXTSONG,
            "Переключено на следующий трек / видео",
            extra_combo=(KEY_LEFTSHIFT, KEY_N),
        ),
    )


@_register("media_prev")
def _h_media_prev(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="media_prev",
        description="Переключаю на предыдущий трек / видео (Previous)",
        commands=(("playerctl", "previous"),),
        fallback_commands=(("xdotool", "key", "XF86AudioPrev"),),
        python_op=lambda: _media_key_op("Previous", KEY_PREVIOUSSONG, "Переключено на предыдущий трек / видео"),
    )


def _media_seek_op(seconds: int, forward: bool) -> str:
    offset_us = (seconds if forward else -seconds) * 1_000_000
    mpris_ok = _mpris_call("Seek", f"{offset_us}")
    key = KEY_RIGHT if forward else KEY_LEFT
    presses = max(1, min(12, round(seconds / 5)))
    key_ok = _wayland_send_keycodes(*((key,) for _ in range(presses)))
    if mpris_ok or key_ok:
        direction = "вперёд" if forward else "назад"
        return f"Перемотано на {seconds} сек. {direction}"
    raise SystemCommandError("не удалось перемотать медиа")


@_register("media_seek_fwd")
def _h_media_seek_fwd(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    sec = _parse_seconds(query, default=10)
    return SystemPlan(
        category="media",
        action="media_seek_forward",
        description=f"Перематываю на {sec} сек. вперёд",
        commands=(("playerctl", "position", f"{sec}+"),),
        fallback_commands=(("xdotool", "key", "Right"),),
        python_op=lambda sec=sec: _media_seek_op(sec, forward=True),
    )


@_register("media_seek_back")
def _h_media_seek_back(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    sec = _parse_seconds(query, default=10)
    return SystemPlan(
        category="media",
        action="media_seek_backward",
        description=f"Перематываю на {sec} сек. назад",
        commands=(("playerctl", "position", f"{sec}-"),),
        fallback_commands=(("xdotool", "key", "Left"),),
        python_op=lambda sec=sec: _media_seek_op(sec, forward=False),
    )


@_register("video_fullscreen")
def _h_video_fullscreen(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="video_fullscreen",
        description="Переключаю полноэкранный режим видео (клавиша F)",
        commands=(("xdotool", "key", "f"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_F,),), ("xdotool", "key", "f"), "Переключён полноэкранный режим видео (F)"),
    )


@_register("video_subtitles")
def _h_video_subtitles(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="video_subtitles",
        description="Переключаю субтитры в видеоплеере (клавиша C)",
        commands=(("xdotool", "key", "c"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_C,),), ("xdotool", "key", "c"), "Переключены субтитры видео (C)"),
    )


@_register("video_speed_up")
def _h_video_speed_up(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="video_speed_up",
        description="Увеличиваю скорость воспроизведения видео (Shift+>)",
        commands=(("xdotool", "key", "greater"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTSHIFT, KEY_DOT),), ("xdotool", "key", "greater"), "Увеличена скорость видео"),
    )


@_register("video_speed_down")
def _h_video_speed_down(query: str, site: str, home: Path, dirs: dict[str, Path]) -> SystemPlan:
    return SystemPlan(
        category="media",
        action="video_speed_down",
        description="Уменьшаю скорость воспроизведения видео (Shift+<)",
        commands=(("xdotool", "key", "less"),),
        python_op=lambda: _send_keys_or_xdotool(((KEY_LEFTSHIFT, KEY_COMMA),), ("xdotool", "key", "less"), "Уменьшена скорость видео"),
    )
