"""Джарвис: слушает «Джарвис», выполняет команду и отвечает голосом; одна команда текстом; работа в фоне.

    python3 assistant.py                  # Джарвис (в терминале можно и печатать)
    python3 assistant.py "открой стим"    # одна команда текстом
    python3 assistant.py --install        # в фон: служба systemd, клавиша Alt+` и микрофон без звука динамиков
    python3 assistant.py --uninstall      # убрать из фона (и клавишу, и микрофон без эха)
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from . import apps, browser, listen, parser
from .dispatcher import is_complete_command, run_phrase


def jarvis() -> int:
    """Главный режим: слушает «Джарвис», выполняет команду и отвечает голосом; в терминале можно и печатать."""
    import threading

    from . import llm, speak
    t0 = time.perf_counter()
    try:
        recognizer = listen.Recognizer()
        listen.mic_command()
    except listen.VoiceUnavailable as exc:
        print(f"Голосовой ввод недоступен: {exc}")
        return 1
    speaker = speak.Speaker.load()
    parser.preload_model()
    threading.Thread(target=_warm_up, daemon=True).start()       # тяжёлое для первых команд — в фоне, не задерживая старт
    session = listen.VoiceSession(recognizer, say=speaker.say, hush=speaker.stop, complete=is_complete_command)
    apps.late_failure = lambda msg: (print(f"⚠ {msg}"), speaker.say_async(msg))   # программа открывается без ожидания
    busy = threading.Lock()                           # голос и клавиатура не выполняют команды одновременно

    def handle(text: str, heard=None, confirm=None) -> None:
        with busy:
            if heard is not None:
                print(f"\n🎤 «{heard.raw}»" + (f" -> «{text}»" if text != heard.raw.lower().strip(" .!?") else "")
                      + f"  (распознано за {heard.stt_s:.2f} с)")
            reply = run_phrase(text, confirm=confirm or session.ask_yes_no, notify=speaker.say_async)
            print(f"🔊 {reply}")
            if heard is not None:
                session.speak(reply)          # голосом спросили — можно перебить «Джарвис…»
            else:
                speaker.say(reply)            # напечатали — микрофон слушает wake_loop, не мешаем

    if sys.stdin.isatty():
        threading.Thread(target=_keyboard_loop, args=(handle, llm._ask_yes_no), daemon=True).start()
    mic = "без звука динамиков" if listen.echo_free_mic() else "обычный (без подавления эха: assistant.py --install)"
    print(f"Готово за {time.perf_counter() - t0:.1f} с: распознавание {recognizer.name}, голос {speaker.name}, микрофон {mic}.")
    print("Скажите «Джарвис» или нажмите Alt+` — после сигнала команду (или сразу: «Джарвис, открой стим»)."
          + (" Можно и печатать; exit — выход." if sys.stdin.isatty() else ""))
    previous = signal.signal(signal.SIGUSR1, lambda *_: session.summon())     # клавиша: см. HOTKEY_COMMAND
    try:
        session.wake_loop(lambda heard: handle(heard.text, heard=heard))
    except KeyboardInterrupt:
        print()
    except listen.VoiceUnavailable as exc:
        print(f"Микрофон: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGUSR1, previous)
    return 0


def _warm_up() -> None:
    """Чтобы первая команда не ждала: поиск по смыслу, модель названий с названиями программ, ключ доступа Google
    (без этого первая команда с английским названием шла ~4 с вместо ~0.8 с). Без сети облако подождёт до первой нужды."""
    from . import listen, names, semantic
    semantic.preload()
    names.model().prime(v for a in apps.desktop_apps() for n in a.names for v in apps._name_variants(apps._norm(n)))
    try:
        from google.auth.transport.requests import Request
        listen._cloud_session()[0].refresh(Request())
    except Exception:                                 # noqa: BLE001 — нет сети или ключа: облако просто не будет готово заранее
        pass


def _keyboard_loop(handle, confirm) -> None:
    import os
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    while True:
        try:
            text = input().replace("\ufffd", "").strip()
        except (EOFError, KeyboardInterrupt):
            text = "exit"
        if text.lower() in ("exit", "выход"):
            os.kill(os.getpid(), signal.SIGINT)        # главный поток слушает микрофон: остановить его
            return
        if browser.normalize_query(text):
            handle(text, confirm=confirm)


# --- работа в фоне: служба systemd пользователя (запуск при входе в систему) ---
SERVICE = "jarvis.service"

# Клавиша вызова: GNOME запускает команду, команда шлёт службе сигнал, Джарвис слушает без «Джарвис».
# Above_Tab — клавиша над Tab (` ~ ё) в любой раскладке.
HOTKEY = "<Alt>Above_Tab"
HOTKEY_COMMAND = f"systemctl --user kill --kill-whom=main --signal=SIGUSR1 {SERVICE}"
_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
_KEY_PATH = f"/{_KEYS.replace('.', '/')}/custom-keybindings/jarvis/"

# Микрофон без звука динамиков: модуль подавления эха PipeWire (WebRTC — как в браузере и телефоне) вычитает из
# микрофона то, что играют динамики (monitor.mode: любой вывод по умолчанию), и глушит ровный шум комнаты.
ECHO_CANCEL_CONF = f"""# Джарвис: микрофон без звука динамиков. Создан «assistant.py --install», убирается «--uninstall».
context.modules = [
    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            audio.channels = 1
            audio.position = [ MONO ]
            aec.args = {{
                webrtc.noise_suppression = true
                webrtc.high_pass_filter = true
                webrtc.extended_filter = true
                webrtc.delay_agnostic = true
            }}
            capture.props = {{ node.name = "jarvis_mic_capture" node.passive = true }}
            source.props = {{ node.name = "{listen.ECHO_FREE_MIC}" node.description = "Джарвис: микрофон без звука динамиков" }}
        }}
    }}
]
"""
_PIPEWIRE = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")


def _unit_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE


def _echo_cancel_file() -> Path:
    return Path.home() / ".config" / "pipewire" / "pipewire.conf.d" / "60-jarvis-echo-cancel.conf"


def _run(*cmd: str) -> str:
    """Команда установки; ошибка -> RuntimeError с её текстом."""
    import subprocess
    res = subprocess.run([*cmd], capture_output=True, text=True, check=False)
    if res.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {(res.stderr or res.stdout).strip()}")
    return res.stdout.strip()


def _keybinding_paths() -> list[str]:
    import ast
    out = _run("gsettings", "get", _KEYS, "custom-keybindings")
    return list(ast.literal_eval(out.removeprefix("@as ")))


def install_hotkey() -> None:
    paths = _keybinding_paths()
    if _KEY_PATH not in paths:
        _run("gsettings", "set", _KEYS, "custom-keybindings", str(paths + [_KEY_PATH]))
    for key, value in (("name", "Джарвис: слушать команду"), ("command", HOTKEY_COMMAND), ("binding", HOTKEY)):
        _run("gsettings", "set", f"{_KEYS}.custom-keybinding:{_KEY_PATH}", key, value)


def uninstall_hotkey() -> None:
    paths = _keybinding_paths()
    if _KEY_PATH in paths:
        _run("gsettings", "set", _KEYS, "custom-keybindings", str([p for p in paths if p != _KEY_PATH]))
    for key in ("name", "command", "binding"):
        _run("gsettings", "reset", f"{_KEYS}.custom-keybinding:{_KEY_PATH}", key)


def install_service() -> int:
    here, unit, echo_cancel = Path(__file__).resolve().parent.parent, _unit_file(), _echo_cancel_file()
    python = here / ".venv" / "bin" / "python"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=Джарвис — голосовой ассистент
After=graphical-session.target pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
ExecStart={python if python.exists() else sys.executable} {here / "assistant.py"}
WorkingDirectory={here}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
# открытые Джарвисом программы (Steam, браузер...) не закрываются вместе с ним
KillMode=process

[Install]
WantedBy=graphical-session.target
""", encoding="utf-8")
    try:
        if not echo_cancel.is_file() or echo_cancel.read_text(encoding="utf-8") != ECHO_CANCEL_CONF:
            echo_cancel.parent.mkdir(parents=True, exist_ok=True)
            echo_cancel.write_text(ECHO_CANCEL_CONF, encoding="utf-8")
            _run("systemctl", "--user", "restart", *_PIPEWIRE)      # звук прервётся на секунду
        install_hotkey()
        _run("systemctl", "--user", "daemon-reload")
        _run("systemctl", "--user", "enable", SERVICE)
        _run("systemctl", "--user", "restart", SERVICE)
    except (OSError, RuntimeError) as exc:
        print(f"Не получилось: {exc}")
        return 1
    print(f"Джарвис работает в фоне и будет запускаться при входе в систему ({unit}).")
    print("Вызов: «Джарвис» или Alt+` (второе нажатие — «я договорил»). Микрофон — без звука динамиков.")
    print("Что он слышит и делает:  journalctl --user -u jarvis -f")
    print("Перезапустить:           systemctl --user restart jarvis")
    print("Убрать из фона:          python3 assistant.py --uninstall")
    return 0


def uninstall_service() -> int:
    import subprocess
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE], capture_output=True, check=False)
    _unit_file().unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    try:
        uninstall_hotkey()
        if _echo_cancel_file().is_file():
            _echo_cancel_file().unlink()
            _run("systemctl", "--user", "restart", *_PIPEWIRE)
    except (OSError, RuntimeError) as exc:
        print(f"Не всё убрано: {exc}")
    print("Джарвис убран из фона.")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--install"]:
        return install_service()
    if argv == ["--uninstall"]:
        return uninstall_service()
    if argv:
        text = " ".join(argv)
        if not browser.normalize_query(text):
            print("Пустой запрос.")
            return 1
        print(f"🔊 {run_phrase(text)}")
        return 0
    return jarvis()
