"""Голос ассистента: что сказать в ответ и как это произнести.

Голос выбирается строкой в ~/.config/pc_agent/voice (или переменной PC_AGENT_VOICE), по умолчанию piper:irina:
    piper:irina | piper:dmitri | piper:denis | piper:ruslan
        офлайн, ~0.2 с на фразу. Модель ONNX (63 МБ, только данные) скачивается один раз с Hugging Face; фонемы даёт
        системная libespeak-ng (она же читает числа), звук считает onnxruntime (стоит вместе с распознаванием GigaAM).
    google:Chirp3-HD-Kore | Chirp3-HD-Aoede | Chirp3-HD-Leda | Chirp3-HD-Zephyr | Chirp3-HD-Charon | Chirp3-HD-Puck | ...
        Google Cloud Text-to-Speech через тот же сервисный аккаунт, что и Gemini: самые живые голоса, ~1.2 с на фразу,
        текст ответа уходит в Google. Нет сети — фраза говорится голосом Piper.
Если ничего не загрузилось — системный speech-dispatcher (spd-say). Воспроизведение — pacat, на диск ничего не пишется.

spoken_reply() — что сказать после команды: сделанное не пересказывается («выполняю», «делаю», «готово»…), справка
(время, батарея, память) и объяснение, почему не вышло, — целиком; ответ второго слоя (LLM) уже написан для чтения
вслух и тоже произносится целиком. Пока думает второй слой, звучит thinking() («думаю», «секунду»…).
Речь можно оборвать на полуслове (stop) — так работает перебивание «Джарвис…» во время ответа.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import random
import re
import shutil
import subprocess
import threading
import unicodedata
from pathlib import Path

PIPER_REPO = "rhasspy/piper-voices"
PIPER_SPEAKERS = ("irina", "dmitri", "denis", "ruslan")
VOICE_FILE = Path.home() / ".config" / "pc_agent" / "voice"
DEFAULT_VOICE = "piper:irina"


# =============================================================================
# 1. Что сказать
# =============================================================================
def speakable(text: str, limit: int | None = 220) -> str:
    """Текст для голоса: без ссылок, команд и путей (от пути остаётся имя файла), без разметки и кавычек."""
    t = (text or "").replace("[dry-run]", " ")
    t = t.split(" -> ")[0]                                          # «… -> gnome-terminal --working-directory=…»
    t = re.sub(r"\s*:?\s*https?://\S+", "", t)
    t = re.sub(r"\([^()]*(?:/|--|\+x|=)[^()]*\)", "", t)            # (chmod +x /home/…), (gnome-control-center wifi)
    t = re.sub(r"\s*:\s*(?:~|/)[^«»()\n]*$", "", t)                  # «Запускаю Steam: /usr/bin/steam» — где лежит, не нужно
    t = re.sub(r"(?:~|(?<![\w.]))/[^«»()\n,;]*", lambda m: _path_name(m.group(0)), t)
    t = re.sub(r"[*_#`«»\"“”]", " ", t)
    t = re.sub(r"\s+([,.!?:;])", r"\1", " ".join(t.split())).strip(" :;,-")
    if limit and len(t) > limit:
        cut = max(t.rfind(". ", 0, limit), t.rfind("! ", 0, limit), t.rfind("? ", 0, limit))
        t = t[:cut + 1] if cut > 40 else t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def _path_name(path: str) -> str:
    """«/home/u/Desktop/report_final.pdf» -> «report final»; «/home/u/Downloads» -> «Downloads»."""
    name = path.strip().rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name) if "." in name[1:] else name
    return " " + name.replace("_", " ") + " "


ACKS = ("Выполняю", "Делаю", "Сейчас", "Готово", "Есть", "Уже делаю", "Хорошо", "Сделано")
THINKING = ("Думаю", "Секунду, думаю", "Сейчас подумаю", "Минутку", "Дай подумать")
_LAST: dict[tuple, str] = {}


def _pick(options: tuple[str, ...]) -> str:
    """Случайный вариант, но не тот же, что в прошлый раз."""
    choice = random.choice([o for o in options if o != _LAST.get(options)] or list(options))
    _LAST[options] = choice
    return choice


def thinking() -> str:
    return _pick(THINKING)


def spoken_reply(result: dict, plan, msg: str) -> str:
    """Что сказать после команды: сделанное — коротко («выполняю»); справку, ответ второго слоя и объяснение, почему
    ничего не сделано (исполнителю нечего было делать), — целиком."""
    if (result.get("trace") or {}).get("layer") == "llm":
        return speakable(msg, limit=None)
    if plan is None or getattr(plan, "category", "") == "info":
        return speakable(msg) or "Готово"
    return _pick(ACKS)


# =============================================================================
# 2. Как произнести: Piper (onnxruntime + фонемы libespeak-ng)
# =============================================================================
class PiperVoice:
    """Минимальный Piper: текст -> фонемы IPA (libespeak-ng) -> номера фонем -> VITS (onnxruntime) -> 16-бит звук."""

    def __init__(self, model_path: str | Path):
        import numpy as np                                          # noqa: PLC0415
        import onnxruntime as ort                                   # noqa: PLC0415
        self._np = np
        self.config = json.loads(Path(f"{model_path}.json").read_text(encoding="utf-8"))
        self.sample_rate = int(self.config["audio"]["sample_rate"])
        self.ids = self.config["phoneme_id_map"]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        lib = ctypes.CDLL(ctypes.util.find_library("espeak-ng") or "libespeak-ng.so.1")
        if lib.espeak_Initialize(1, 0, None, 0) <= 0:              # AUDIO_OUTPUT_RETRIEVAL: только фонемы, без звука
            raise OSError("libespeak-ng не запустилась")
        lib.espeak_SetVoiceByName(self.config.get("espeak", {}).get("voice", "ru").encode())
        lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
        lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int]
        self._espeak = lib
        self._lock = threading.Lock()

    def _phonemes(self, text: str) -> str:
        buf = ctypes.c_char_p(text.encode("utf-8"))
        out = []
        while buf.value:
            ph = self._espeak.espeak_TextToPhonemes(ctypes.byref(buf), 1, 0x02)   # UTF-8 -> IPA
            if ph:
                out.append(ph.decode("utf-8", errors="ignore"))
        return re.sub(r"\([a-z]{2,3}\)", "", " ".join(out))           # «(en)juː tjˈuːb(ru)» — метки смены языка

    def synthesize(self, text: str) -> bytes:
        """Текст -> 16-битный моно-звук с частотой self.sample_rate. Знаки препинания дают паузы и интонацию."""
        np = self._np
        pad, seq = self.ids["_"], list(self.ids["^"] + self.ids["_"])
        with self._lock:
            for chunk in re.findall(r"[^.,!?;:]+[.,!?;:]?", text):
                punct = chunk[-1] if chunk[-1] in ".,!?;:" else ""
                words = chunk.rstrip(".,!?;:").strip()
                if not words:
                    continue
                for ch in unicodedata.normalize("NFD", self._phonemes(words) + punct + " "):
                    if ch in self.ids:
                        seq += self.ids[ch] + pad
        seq += self.ids["$"]
        inf = self.config.get("inference", {})
        audio = self.session.run(None, {
            "input": np.array([seq], dtype=np.int64),
            "input_lengths": np.array([len(seq)], dtype=np.int64),
            "scales": np.array([inf.get("noise_scale", 0.667), inf.get("length_scale", 1.0), inf.get("noise_w", 0.8)],
                               dtype=np.float32),
        })[0].squeeze()
        audio = audio / max(0.01, float(np.abs(audio).max())) * 0.8
        return (audio * 32767).astype(np.int16).tobytes()


class GoogleVoice:
    """Google Cloud Text-to-Speech (Chirp 3 HD / WaveNet) через сервисный аккаунт Vertex (~/.config/pc_agent/vertex-sa.json)."""
    URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

    def __init__(self, name: str):
        import httpx                                                # noqa: PLC0415
        from google.oauth2 import service_account                   # noqa: PLC0415
        sa = Path.home() / ".config" / "pc_agent" / "vertex-sa.json"
        self.creds = service_account.Credentials.from_service_account_file(
            str(sa), scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.project = json.loads(sa.read_text(encoding="utf-8"))["project_id"]
        self.name = name if name.startswith("ru-RU-") else f"ru-RU-{name}"
        self.sample_rate = 24000
        self._http = httpx.Client(timeout=15)
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> bytes:
        import base64                                               # noqa: PLC0415
        from google.auth.transport.requests import Request          # noqa: PLC0415
        with self._lock:
            if not self.creds.valid:
                self.creds.refresh(Request())
            r = self._http.post(self.URL, headers={"Authorization": f"Bearer {self.creds.token}",
                                                   "x-goog-user-project": self.project}, json={
                "input": {"text": text}, "voice": {"languageCode": "ru-RU", "name": self.name},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": self.sample_rate}})
        r.raise_for_status()
        return base64.b64decode(r.json()["audioContent"])[44:]     # без заголовка WAV


def _piper(speaker: str) -> PiperVoice:
    from huggingface_hub import hf_hub_download                     # noqa: PLC0415 — ставится вместе с onnx-asr
    path = f"ru/ru_RU/{speaker}/medium/ru_RU-{speaker}-medium.onnx"
    try:                                                            # сначала из кэша: при входе в систему сети может не быть
        model = hf_hub_download(PIPER_REPO, path, local_files_only=True)
        hf_hub_download(PIPER_REPO, path + ".json", local_files_only=True)
    except Exception:                                               # noqa: BLE001
        model = hf_hub_download(PIPER_REPO, path)
        hf_hub_download(PIPER_REPO, path + ".json")
    return PiperVoice(model)


def chosen_voice() -> str:
    import os                                                       # noqa: PLC0415
    env = os.environ.get("PC_AGENT_VOICE", "").strip()
    if env:
        return env
    try:
        return VOICE_FILE.read_text(encoding="utf-8").strip() or DEFAULT_VOICE
    except OSError:
        return DEFAULT_VOICE


_PLAYING: set[subprocess.Popen] = set()              # что сейчас звучит (обрывает stop)


def _run_audible(cmd: list[str], pcm: bytes | None = None) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if pcm is not None else subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _PLAYING.add(proc)
    try:
        proc.communicate(pcm, timeout=120)
    except (subprocess.TimeoutExpired, BrokenPipeError):
        proc.kill()
    finally:
        _PLAYING.discard(proc)


def _play(pcm: bytes, rate: int) -> None:
    cmd = ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1"] if shutil.which("pacat") \
        else ["aplay", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"]
    _run_audible(cmd, pcm)


class Speaker:
    """Произносит фразы. say ждёт конца фразы; say_async — не ждёт («думаю», пока работает второй слой); stop — оборвать."""

    def __init__(self, voice=None, backup: PiperVoice | None = None, name: str = ""):
        self.voice = voice                    # PiperVoice или GoogleVoice
        self.backup = backup                  # для облачного голоса: Piper, если нет сети
        self.name = name or ("piper" if voice else ("spd-say" if shutil.which("spd-say") else "без голоса"))
        self._stopped = threading.Event()
        self._background: threading.Thread | None = None

    def say_async(self, text: str) -> None:
        self._background = threading.Thread(target=self.say, args=(text,), daemon=True)
        self._background.start()

    def stop(self) -> None:
        """Оборвать то, что сейчас говорится (и не начинать то, что ещё синтезируется)."""
        self._stopped.set()
        for proc in list(_PLAYING):
            proc.terminate()

    @classmethod
    def load(cls, choice: str = "") -> "Speaker":
        choice = choice or chosen_voice()
        kind, _, name = choice.partition(":")
        try:
            if kind == "google":
                backup = None
                try:
                    backup = _piper("irina")
                except Exception:                                    # noqa: BLE001
                    pass
                return cls(GoogleVoice(name or "Chirp3-HD-Kore"), backup, f"google/{name or 'Chirp3-HD-Kore'}")
            speaker = name if name in PIPER_SPEAKERS else "irina"
            return cls(_piper(speaker), None, f"piper/{speaker}")
        except Exception as exc:                                     # noqa: BLE001 — нет сети/пакета: голос попроще
            print(f"Голос {choice} недоступен ({type(exc).__name__}: {exc}); говорю через spd-say")
            return cls(None)

    def say(self, text: str) -> None:
        background = self._background
        if background is not None and background is not threading.current_thread():
            background.join()                 # «думаю» договорить до ответа, а не поверх него
        self._stopped.clear()
        text = speakable(text, limit=None)
        if not text:
            return
        try:
            if self.voice is not None:
                try:
                    pcm = self.voice.synthesize(text)
                    rate = self.voice.sample_rate
                except Exception as exc:                             # noqa: BLE001 — облако недоступно: Piper
                    if self.backup is None:
                        raise
                    print(f"Голос {self.name} не ответил ({type(exc).__name__}), говорю голосом Piper")
                    pcm, rate = self.backup.synthesize(text), self.backup.sample_rate
                if not self._stopped.is_set():
                    _play(pcm, rate)
            elif shutil.which("spd-say"):
                _run_audible(["spd-say", "-w", "-l", "ru", "-y", "Russian+Alicia", "--", text])
        except Exception as exc:                                     # noqa: BLE001 — не смогла сказать: не падаем
            print(f"Не смогла произнести: {type(exc).__name__}: {exc}")
