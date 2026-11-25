"""Голосовой ввод: микрофон -> отрезок речи (по паузам) -> распознавание GigaAM -> печатный вид -> команда.

Распознавание — GigaAM v3 (Сбер) через onnx-asr, на этом компьютере, без облака: ~0.1 с на фразу. Модель
`gigaam-v3-e2e-rnnt` в варианте int8 (225 МБ) скачивается один раз в ~/.cache/huggingface, дальше работает без сети.
GigaAM знает только русский: английские названия пишет на слух русскими буквами («паспортс», «Dom Loots»). Поэтому
GigaAM только слушает «Джарвис» (на этом компьютере, в облако ничего не уходит), а саму команду после обращения
распознаёт Google Chirp 3 (cloud_transcribe, ~0.75 с): «открой Passwords», «загугли how to center a div». Нет сети или
облако молчит дольше CLOUD_ASR_WAIT_S — команда остаётся в записи GigaAM.
Микрофон читается через pacat (PipeWire); звук нигде не сохраняется. Если есть микрофон с подавлением эха
(ECHO_FREE_MIC, его создаёт `assistant.py --install`), слушаем его: музыка и голоса из динамиков ноутбука из него
вычтены — как в браузере и телефоне (та же библиотека WebRTC), и на них Джарвис не отвлекается.

Микрофон пишет всё время (отдельный поток, очередь кадров): пока Джарвис выполняет команду или говорит, сказанное не
теряется. Пока он говорит, он слушает: «Джарвис…» обрывает ответ (перебивание) — если микрофон без эха.

Как слушаем: каждая фраза (по паузе) распознаётся; команда — то, что сказано после слова-активатора «Джарвис».
«Джарвис» отдельно или клавиша (summon) -> звуковой сигнал -> следующая фраза и есть команда; второе нажатие клавиши
заканчивает её сразу. Чужой разговор (Discord, музыка) без «Джарвиса» пропускается. Опасные действия подтверждаются
голосом: «да» / «нет».
"""
from __future__ import annotations

import functools
import json
from concurrent.futures import Future, ThreadPoolExecutor
import math
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

from .spoken import normalize_spoken

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2         # 16 бит, моно
ASR_REPO, ASR_MODEL = "istupakov/gigaam-v3-onnx", "gigaam-v3-e2e-rnnt"
ASR_FILES = ["config.json", "v3_e2e_rnnt_*.int8.onnx", "v3_e2e_rnnt_vocab.txt"]     # из репозитория нужны только они


class VoiceUnavailable(RuntimeError):
    """Нет микрофона или распознавания (с подсказкой, что сделать)."""


# =============================================================================
# 1. Микрофон
# =============================================================================
ECHO_FREE_MIC = "jarvis_mic"          # источник PipeWire «микрофон минус звук динамиков» (app.ECHO_CANCEL_CONF)


@functools.lru_cache(maxsize=1)
def echo_free_mic() -> bool:
    """Есть ли микрофон с подавлением эха (проверяется один раз за запуск)."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split("\t")[1:2] == [ECHO_FREE_MIC] for line in out.splitlines())


def mic_command() -> list[str]:
    """Запись звука 16 кГц моно в stdout: pacat (PipeWire/PulseAudio) — с микрофона без эха, если он есть, — или arecord."""
    if shutil.which("pacat"):
        device = [f"--device={ECHO_FREE_MIC}"] if echo_free_mic() else []
        return ["pacat", "--record", "--raw", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1", "--latency-msec=30",
                *device]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"]
    raise VoiceUnavailable("нет программы записи звука (pacat / arecord): sudo apt install pulseaudio-utils")


class Microphone:
    """Микрофон, который пишет всё время: кадры по 30 мс копит в очереди отдельный поток, поэтому сказанное, пока
    Джарвис выполняет команду или говорит, не теряется (и нет зазора на переоткрытие между фразами).
    echo_free — звук динамиков из него вычтен (ECHO_FREE_MIC): Джарвис не слышит сам себя."""
    BACKLOG_S = 30                                   # дольше не копим: самое старое выбрасывается

    def __init__(self):
        self.cmd = mic_command()
        self.echo_free = f"--device={ECHO_FREE_MIC}" in self.cmd
        self.proc: subprocess.Popen | None = None
        self.queue: queue.Queue[bytes | None] = queue.Queue()

    def __enter__(self) -> "Microphone":
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise VoiceUnavailable(f"не удалось открыть микрофон: {exc}") from exc
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        return self

    def _read(self, proc: subprocess.Popen) -> None:
        limit = self.BACKLOG_S * 1000 // FRAME_MS
        while True:
            chunk = proc.stdout.read(FRAME_BYTES)
            if len(chunk) < FRAME_BYTES:
                self.queue.put(None)                 # запись кончилась: микрофон отключён или закрыт
                return
            if self.queue.qsize() >= limit:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
            self.queue.put(chunk)

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def frames(self) -> Iterator[bytes]:
        while True:
            chunk = self.queue.get()
            if chunk is None:
                self.queue.put(None)
                raise VoiceUnavailable("запись с микрофона прервалась (микрофон отключён или занят?)")
            yield chunk

    def flush(self) -> None:
        """Выбросить накопленное: свой сигнал или голос, если микрофон слышит динамики."""
        while True:
            try:
                chunk = self.queue.get_nowait()
            except queue.Empty:
                return
            if chunk is None:
                self.queue.put(None)
                return


def frame_db(frame: bytes) -> float:
    """Громкость кадра в dBFS (0 — максимум, тишина около -90)."""
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    rms = math.sqrt(float(np.mean(x * x))) if x.size else 0.0
    return 20 * math.log10(max(rms, 1.0) / 32768.0)


# =============================================================================
# 2. Где начинается и кончается фраза (громкость с подстройкой под шум комнаты)
# =============================================================================
@dataclass
class VadConfig:
    start_frames: int = 3            # подряд «громких» кадров для начала речи (90 мс)
    end_silence_ms: int = 1200       # тишина, завершающая фразу (с 0,8 с обрывало задумавшихся посреди команды)
    max_ms: int = 15000              # длиннее фраза обрезается
    preroll_ms: int = 300            # сколько звука до начала речи сохранить (первый слог)
    min_speech_ms: int = 250         # короче — щелчок, а не речь
    margin_db: float = 10.0          # насколько речь громче шума комнаты
    min_db: float = -60.0            # тише этого — всегда тишина (встроенный цифровой микрофон тихий: тишина ~ -66 дБ)
    calib_frames: int = 8            # первые 240 мс — только слушаем шум комнаты (если его уровень ещё не известен)


GUESS_MS = 450            # столько тишины — уже угадываем: обращение ли это, и запись команды отправляется в облако
EARLY_END_MS = 780        # столько тишины — ясная полная команда считается законченной (иначе ждём end_silence_ms)


class Segmenter:
    """Кадры по одному; когда фраза закончилась, feed() возвращает её звук (bytes), иначе None.
    noise_db — известный уровень шума комнаты (с прошлой фразы); None — сначала короткая калибровка."""

    def __init__(self, cfg: VadConfig | None = None, noise_db: float | None = None):
        self.cfg = cfg or VadConfig()
        self.noise_db = noise_db if noise_db is not None else -60.0
        self.calibrated = noise_db is not None
        self._calib: list[float] = []
        self.last_speech_ms = 0                        # сколько речи было в последней законченной фразе
        self.reset()

    def reset(self) -> None:
        self.pre: list[bytes] = []
        self.voiced: list[bytes] = []
        self.in_speech = False
        self.loud_run = self.silence_ms = self.speech_ms = 0

    @property
    def threshold_db(self) -> float:
        return max(self.cfg.min_db, self.noise_db + self.cfg.margin_db)

    def feed(self, frame: bytes) -> bytes | None:
        db = frame_db(frame)
        if not self.calibrated:                        # калибровка: медиана громкости первых кадров = шум комнаты
            self._calib.append(db)
            self.pre.append(frame)
            if len(self._calib) >= self.cfg.calib_frames:
                self.noise_db, self.calibrated = float(np.median(self._calib)), True
            return None
        loud = db > self.threshold_db
        if not self.in_speech:
            self.noise_db = db if db < self.noise_db else 0.97 * self.noise_db + 0.03 * db   # шум: быстро вниз, медленно вверх
            self.pre = (self.pre + [frame])[-(self.cfg.preroll_ms // FRAME_MS + self.cfg.start_frames):]
            self.loud_run = self.loud_run + 1 if loud else 0
            if self.loud_run >= self.cfg.start_frames:
                self.in_speech, self.voiced = True, list(self.pre)
                self.speech_ms, self.silence_ms = self.loud_run * FRAME_MS, 0
            return None
        self.voiced.append(frame)
        if loud:
            self.silence_ms = 0
            self.speech_ms += FRAME_MS
        else:
            self.silence_ms += FRAME_MS
        if self.silence_ms >= self.cfg.end_silence_ms or len(self.voiced) * FRAME_MS >= self.cfg.max_ms:
            return self.flush()
        return None

    def flush(self) -> bytes | None:
        """Фраза закончена (паузой или снаружи — вторым нажатием клавиши): что успели сказать; речи не было — None."""
        audio = b"".join(self.voiced) if self.in_speech and self.speech_ms >= self.cfg.min_speech_ms else None
        self.last_speech_ms = self.speech_ms
        self.reset()
        return audio


def listen_once(frames: Iterable[bytes], seg: Segmenter | None = None, wait_s: float | None = None,
                until: Callable[[], bool] | None = None,
                on_pause: Callable[[Segmenter], bool] | None = None) -> bytes | None:
    """Одна фраза из потока кадров. wait_s — сколько ждать начала речи (None — сколько угодно);
    until() -> True — закончить сейчас (клавиша): сказанное к этому моменту или None;
    on_pause(seg) — вызывается при тишине GUESS_MS и EARLY_END_MS посреди фразы; True — фраза закончена сейчас."""
    seg = seg or Segmenter()
    waited = 0
    for frame in frames:
        if until is not None and until():
            return seg.flush()
        audio = seg.feed(frame)
        if audio is not None:
            return audio
        if on_pause is not None and seg.in_speech and seg.silence_ms in (GUESS_MS, EARLY_END_MS) and on_pause(seg):
            return seg.flush()
        if not seg.in_speech:
            waited += FRAME_MS
            if wait_s is not None and waited >= wait_s * 1000:
                return None
    return None


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


# =============================================================================
# 3. Распознавание речи: GigaAM v3
# =============================================================================
class Recognizer:
    """GigaAM v3 e2e (int8): сам ставит знаки, цифры («21:30», «60%») и часть латиницы («YouTube», «Steam»).
    Проверено на 60 фразах корпуса естественными голосами: 58/60 то же намерение, что у напечатанной фразы."""
    name = f"GigaAM ({ASR_MODEL}, int8)"

    def __init__(self):
        try:
            import onnx_asr                                         # noqa: PLC0415
        except ImportError as exc:
            raise VoiceUnavailable('нет распознавания речи: .venv/bin/pip install "onnx-asr[cpu,hub]"') from exc
        import onnxruntime as ort                                   # noqa: PLC0415
        opts = ort.SessionOptions()                                 # 4 потока без холостого ожидания: так же быстро,
        opts.intra_op_num_threads = 4                               # а процессора между фразами не тратится
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        try:                                                        # сначала из кэша: при входе в систему сети может не быть
            from huggingface_hub import snapshot_download          # noqa: PLC0415
            path = snapshot_download(ASR_REPO, local_files_only=True, allow_patterns=ASR_FILES)
            self.model = onnx_asr.load_model(ASR_MODEL, path, quantization="int8", sess_options=opts)
        except Exception:                                           # noqa: BLE001 — нет в кэше: скачать
            try:
                self.model = onnx_asr.load_model(ASR_MODEL, quantization="int8", sess_options=opts)
            except Exception as exc:                                # noqa: BLE001
                raise VoiceUnavailable(f"модель распознавания не загрузилась: {exc}") from exc

    def transcribe(self, pcm: bytes) -> str:
        out = self.model.recognize(pcm_to_float(pcm), sample_rate=SAMPLE_RATE)
        return out if isinstance(out, str) else " ".join(map(str, out))

    def transcribe_timed(self, pcm: bytes) -> tuple[str, list[tuple[str, float]]]:
        """Текст и слова со временем начала: («Джарес, открой мне YouTube.», [(«джарес», 0.2), («открой», 1.32), …])."""
        res = self.model.with_timestamps().recognize(pcm_to_float(pcm), sample_rate=SAMPLE_RATE)
        words: list[list] = []
        for token, t in zip(res.tokens, res.timestamps):
            if token.startswith(" ") or not words:
                words.append([token, t])
            else:
                words[-1][0] += token
        clean = [(" ".join(_words(w)), t) for w, t in words]
        return res.text, [(w, t) for w, t in clean if w]


# Google Chirp 3 (Speech-to-Text v2): русская речь с английскими названиями латиницей — на записях с русским акцентом
# 14 названий из 15 (GigaAM — 6, Gemini — 14, но 1.5–25 с), ~0.75 с из Европы. Тот же проект и сервисный аккаунт, что у
# Gemini (роль Cloud Speech Client). Только «русский»: с «ru+en» и автоопределением языка выходило хуже (41 и 39 из 45).
CLOUD_ASR_WAIT_S = 3.0
CLOUD_ASR_URL = "https://eu-speech.googleapis.com/v2/projects/{project}/locations/eu/recognizers/_:recognize"
CLOUD_ASR_KEY = Path.home() / ".config" / "pc_agent" / "vertex-sa.json"


@functools.lru_cache(maxsize=1)
def _cloud_session():
    import httpx                                                    # noqa: PLC0415
    from google.oauth2 import service_account                       # noqa: PLC0415
    creds = service_account.Credentials.from_service_account_file(
        str(CLOUD_ASR_KEY), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return creds, json.loads(CLOUD_ASR_KEY.read_text(encoding="utf-8"))["project_id"], httpx.Client()


def cloud_transcribe(pcm: bytes, timeout: float = CLOUD_ASR_WAIT_S) -> str:
    """Та же запись — в Chirp 3: что сказано, английские названия латиницей. Нет сети, долго, нет доступа — исключение."""
    import base64                                                   # noqa: PLC0415
    from google.auth.transport.requests import Request              # noqa: PLC0415
    creds, project, http = _cloud_session()
    if not creds.valid:
        creds.refresh(Request())
    r = http.post(CLOUD_ASR_URL.format(project=project), timeout=timeout, headers={"Authorization": f"Bearer {creds.token}"},
                  json={"config": {"model": "chirp_3", "languageCodes": ["ru-RU"], "explicitDecodingConfig": {
                      "encoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE, "audioChannelCount": 1}},
                        "content": base64.b64encode(pcm).decode()})
    r.raise_for_status()
    return " ".join(res["alternatives"][0].get("transcript", "") for res in r.json().get("results", [])
                    if res.get("alternatives")).strip()


# =============================================================================
# 4. «Джарвис» и ответ «да / нет»
# =============================================================================
WAKE_WORD = "джарвис"
WAKE_PEEK_BYTES = 3 * SAMPLE_RATE * 2            # обращение ищем в первых 3 с каждого высказывания
WAKE_PAUSE_MS = 250           # пауза, после которой внутри фразы начинается новое высказывание («…стук клавиш… Джарвис»)
WAKE_VOICE_MS = 240           # столько громкого звука в ближайшие 0.6 с — это голос, а не щелчок мыши или клавиши
WAKE_CANDIDATES = 12          # больше высказываний в одной фразе не проверяем (процессор)
WAKE_NEAR = 0.6               # «похоже на Джарвис, но не принято» — пишется в журнал (для настройки)
# Как GigaAM записал «Джарвис» у вас (замеры 19.11): «Джарвис», «Джарус», «Дарвис», «Джарес».
WAKE_MIN_SIMILARITY = 0.90       # на слух: «джарес» 0.94, «джарез» 0.93 — обращение; «джаве» 0.89, «джава» 0.85 — нет
WAKE_CONFIRM = 0.8               # 0.8…0.9 («жарес» 0.82, но и «джава» 0.85) — переспросить облако: слышит ли оно «Джарвис»
KEY_PAUSE_MS = 2000                               # после нажатия клавиши можно задуматься дольше (или нажать ещё раз)
_LEAD_INS = {"эй", "слушай", "ну", "так", "окей", "ok", "о", "привет", "алло"}


def utterance_starts(audio: bytes, threshold_db: float) -> list[int]:
    """Где внутри фразы начинаются высказывания голосом (после паузы ≥ WAKE_PAUSE_MS): фраза может начаться раньше
    «Джарвис» — со стука клавиш, щелчков или чужого разговора, а обращение звучит в начале своего высказывания."""
    loud = [frame_db(audio[i:i + FRAME_BYTES]) > threshold_db for i in range(0, len(audio) - FRAME_BYTES + 1, FRAME_BYTES)]
    pause, voice, window = WAKE_PAUSE_MS // FRAME_MS, WAKE_VOICE_MS // FRAME_MS, 600 // FRAME_MS
    starts, quiet = [], pause
    for i, is_loud in enumerate(loud):
        if is_loud:
            if quiet >= pause and sum(loud[i:i + window]) >= voice:
                starts.append(max(0, i - 3) * FRAME_BYTES)        # чуть раньше: первый слог
            quiet = 0
        else:
            quiet += 1
    return (starts or [0])[:WAKE_CANDIDATES]


def command_start(words: list[tuple[str, float]], wake: str = WAKE_WORD) -> float | None:
    """Где после обращения начинается команда (с): время первого слова после имени и его повторов; нет — None.
    Так в облако уходит только команда: имя облако слышит хуже GigaAM («Джарвис» -> «Да, Алиса»)."""
    for i, (w, _) in enumerate(words[:3]):
        if is_wake(w, wake, WAKE_CALLED_SIMILARITY):
            rest = words[i + 1:]
            while rest and is_wake(rest[0][0], wake):
                rest = rest[1:]
            return rest[0][1] if rest else None
        if w not in _LEAD_INS:
            break
    return None


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s-]", " ", (text or "").lower().replace("ё", "е")).split()


WAKE_HEARD_AS = ("джарв", "jarv", "джару", "дарвис", "чарвис")    # так GigaAM записывает «Джарвис» (замер голоса 19.11)
WAKE_CALLED_SIMILARITY = 0.8      # обращение уже услышал GigaAM: облако пишет имя как угодно («Javiers» — 0.88)


def is_wake(word: str, wake: str = WAKE_WORD, min_similarity: float = WAKE_MIN_SIMILARITY) -> bool:
    """«Джарвис», «Jarvis», «джаруз», «дарвис» (так его записывает распознавание) — да; «жара», «джаз», «джава» — нет."""
    from .apps import name_similarity                         # noqa: PLC0415
    w = word.lower().replace("ё", "е")
    return w == wake or w.startswith(WAKE_HEARD_AS) or name_similarity(w, wake) >= min_similarity


def split_wake(text: str, wake: str = WAKE_WORD, called: bool = False) -> tuple[bool, str]:
    """(обратились ли к ассистенту, команда после обращения): «Эй, Джарвис, открой стим» -> (True, «открой стим»).
    called — обращение уже точно было (его услышал GigaAM): имя узнаётся и в необычной записи облака («Javiers»)."""
    words = _words(text)
    for i, w in enumerate(words[:3]):
        if is_wake(w, wake, WAKE_CALLED_SIMILARITY if called else WAKE_MIN_SIMILARITY):
            rest = words[i + 1:]
            while rest and is_wake(rest[0], wake):           # «Джарвис. Джарвис.» — позвали ещё раз, это не команда
                rest = rest[1:]
            while rest and is_wake(rest[-1], wake):          # «включи дискорд, Джарвис» — имя в конце тоже не команда
                rest = rest[:-1]
            return True, " ".join(rest)
        if w not in _LEAD_INS:
            break
    return False, " ".join(words)


def _wake_score(text: str, wake: str = WAKE_WORD) -> tuple[float, str]:
    """Насколько первые слова похожи на обращение: (0.87, «жарес») — сомнительно, (0.43, «дарвин») — нет."""
    from .apps import name_similarity                         # noqa: PLC0415
    return max(((name_similarity(w, wake), w) for w in _words(text)[:3] if len(w) >= 4), default=(0.0, ""))


_YES = {"да", "ага", "угу", "давай", "конечно", "выполняй", "выполни", "подтверждаю", "верно", "можно", "делай", "yes",
        "окей", "ок", "ok", "хорошо", "согласна", "согласен", "точно", "разумеется", "естественно"}
_NO = {"нет", "не", "неа", "отмена", "отмени", "стоп", "стой", "отставить", "no", "отказ", "нельзя", "погоди", "подожди",
       "хватит", "замолчи", "достаточно"}


def yes_no(text: str) -> bool | None:
    """«да, давай» -> True, «нет, не надо» -> False, непонятно -> None. «Не» и «нет» сильнее: «да нет» = нет."""
    words = _words(text)
    if any(w in _NO for w in words):
        return False
    if any(w in _YES for w in words):
        return True
    return None


# =============================================================================
# 5. Сеанс: слушать, распознавать, спрашивать
# =============================================================================
@dataclass
class Heard:
    raw: str                 # как распознал GigaAM
    text: str                # печатный вид (это уходит в парсер)
    stt_s: float             # сколько заняло распознавание
    audio_s: float           # длина фразы
    audio: bytes = b""       # запись команды (16 кГц, 16 бит; имя по возможности отрезано) — для облака; на диск не пишется
    cloud: Future | None = None   # облако уже распознаёт эту запись (начали во время паузы)


@dataclass
class _Guess:
    """Догадка во время паузы: что услышано к этому моменту. Годится, если после неё речи не прибавилось."""
    speech_ms: int
    heard: Heard | None = None     # команда (текст GigaAM, запись без имени); None — к Джарвису не обращались
    rest: str = ""                 # команда после обращения («» — позвали без команды)
    near: str = ""                 # «похоже на обращение, но не принято» — для журнала


def beep(kind: str = "start") -> None:
    """Звук «слушаю» / «не расслышала»; ждёт конца звука, чтобы он не попал в запись."""
    sound = Path("/usr/share/sounds/freedesktop/stereo") / {"start": "message.oga", "error": "dialog-warning.oga"}[kind]
    if sound.is_file() and shutil.which("paplay"):
        subprocess.run(["paplay", str(sound)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)


class VoiceSession:
    def __init__(self, recognizer, say: Callable[[str], None] = lambda text: None,
                 mic_factory: Callable[[], Microphone] | None = None, sounds: bool = True,
                 log: Callable[[str], None] = print, hush: Callable[[], None] = lambda: None,
                 complete: Callable[[str], bool] = lambda text: False):
        self.recognizer = recognizer
        self.say = say                                 # сказать (ждёт конца фразы)
        self.hush = hush                               # оборвать то, что сейчас говорится (перебивание)
        self.mic_factory = mic_factory or (lambda: Microphone())
        self.sounds = sounds
        self.log = log
        self.noise_db: float | None = None             # шум комнаты, запоминается между фразами
        self.summoned = threading.Event()              # нажата клавиша вызова (summon)
        self.mic = None                                # открывается при первом прослушивании и пишет всё время
        self.pending: bytes | None = None              # фраза, которой перебили ответ: её разберёт wake_loop
        self.complete = complete                       # ясная полная команда? — тогда не ждать конца паузы
        self.last_speech_ms = 0                        # сколько речи было в последней записанной фразе
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cloud")   # облако — пока ещё пауза

    def _mic(self):
        if self.mic is None:
            self.mic = self.mic_factory().__enter__()
        return self.mic

    def close(self) -> None:
        if self.mic is not None:
            mic, self.mic = self.mic, None
            mic.__exit__(None, None, None)

    def _own_sound_done(self) -> None:
        """Свой сигнал или голос кончился: если микрофон слышит динамики — выбросить записанное за это время."""
        if self.mic is not None and not getattr(self.mic, "echo_free", False):
            self.mic.flush()

    def _is_called(self, audio: bytes) -> bool:
        return split_wake(self.recognizer.transcribe(audio[:WAKE_PEEK_BYTES]) or "")[0]

    def recognize_timed(self, audio: bytes) -> tuple[Heard, list[tuple[str, float]]]:
        t = time.perf_counter()
        raw, words = self.recognizer.transcribe_timed(audio)
        raw = (raw or "").strip()
        return Heard(raw, normalize_spoken(raw), time.perf_counter() - t, len(audio) / 2 / SAMPLE_RATE, audio), words

    def _find_wake(self, audio: bytes, wake: str) -> tuple[tuple[int, Heard, list] | None, str]:
        """((где в записи начинается обращение, что услышано с этого места, слова со временем) или None, почти-обращение
        для журнала): проверяется начало каждого высказывания голосом (utterance_starts), а не только начало фразы."""
        cfg = VadConfig()
        threshold = max(cfg.min_db, (self.noise_db if self.noise_db is not None else cfg.min_db) + cfg.margin_db)
        near = ""
        for start in utterance_starts(audio, threshold):
            peek = audio[start:start + WAKE_PEEK_BYTES]
            heard, words = self.recognize_timed(peek)
            if split_wake(heard.raw, wake)[0]:
                return (start, heard, words), ""
            score, word = _wake_score(heard.raw, wake)
            if score >= WAKE_CONFIRM and self._cloud_hears_wake(peek, wake):
                self.log(f"👂 «{word}» ({score:.2f}) — облако подтвердило обращение")
                return (start, heard, words), ""
            if score >= WAKE_NEAR and not near:
                near = f"«{word}» {score:.2f} в «{heard.raw[:60]}»"
        return None, near

    def _addressed(self, audio: bytes, wake: str) -> tuple[tuple[Heard, str] | None, str]:
        """Обращено ли к Джарвису: ((команда: текст GigaAM и запись без имени, команда после обращения) или None,
        почти-обращение для журнала). «Джарвис» звучит в начале своего высказывания: распознаём только первые секунды
        каждого (чужой разговор и шум стоят в разы меньше процессора); целиком — только обращённое к Джарвису."""
        found, near = self._find_wake(audio, wake)
        if found is None:
            return None, near
        start, heard, words = found
        audio = audio[start:]
        begins = command_start(words, wake)                    # где кончилось имя — по той записи, где его нашли
        if len(audio) > WAKE_PEEK_BYTES:                       # длиннее проверенного начала — целиком
            heard, words = self.recognize_timed(audio)
            begins = begins if begins is not None else command_start(words, wake)
        if begins is not None:                                 # команда без имени — в облако
            heard.audio = audio[int(max(0.0, begins - 0.15) * SAMPLE_RATE) * 2:]
        woke, rest = split_wake(heard.raw, wake, called=True)  # обращение уже найдено — в любой записи
        return (heard, rest if woke else " ".join(_words(heard.raw))), near   # целиком GigaAM мог имя не дописать

    def _cloud_hears_wake(self, audio: bytes, wake: str) -> bool:
        try:
            return split_wake(cloud_transcribe(audio), wake)[0]
        except Exception:                                           # noqa: BLE001 — нет сети: сомнительное не принимаем
            return False

    def _guess_wake(self, seg: Segmenter, wake: str) -> _Guess:
        """Пауза посреди фразы: обращение ли это, и если да — команда уже уходит в облако."""
        addressed, near = self._addressed(b"".join(seg.voiced), wake)
        if addressed is None:
            return _Guess(seg.speech_ms, near=near)
        heard, rest = addressed
        if rest:
            heard.cloud = self._pool.submit(cloud_transcribe, heard.audio)
        return _Guess(seg.speech_ms, heard, rest)

    def _guess_command(self, seg: Segmenter, wake: str) -> _Guess:
        """Пауза в команде после сигнала: что сказано к этому моменту — и уже в облако."""
        audio = b"".join(seg.voiced)
        heard = self.recognize(audio)
        again, after = split_wake(heard.raw, wake)
        rest = after if again else heard.raw
        if rest:
            heard.cloud = self._pool.submit(cloud_transcribe, audio)
        return _Guess(seg.speech_ms, heard, rest)

    def _pauser(self, guess: list, make: Callable[[Segmenter], _Guess], early: bool) -> Callable[[Segmenter], bool]:
        """on_pause для записи: при тишине GUESS_MS — догадка (make), при EARLY_END_MS — закончить фразу, если речи не
        прибавилось и это ясная полная команда (early; после клавиши — нет: там «договорил» решает второе нажатие)."""
        def on_pause(seg: Segmenter) -> bool:
            if seg.silence_ms == GUESS_MS:
                guess[:] = [make(seg)]
                return False
            g = guess[0] if guess else None
            return bool(early and g and g.speech_ms == seg.speech_ms and g.rest and self.complete(normalize_spoken(g.rest)))
        return on_pause

    def _valid(self, guess: list) -> _Guess | None:
        """Догадка годится, если после неё речи не прибавилось (договорили — была только тишина)."""
        return guess[0] if guess and guess[0].speech_ms == self.last_speech_ms else None

    def speak(self, text: str) -> None:
        """Ответ голосом, а пока он звучит — слушать: «Джарвис…» или клавиша обрывают ответ, а сказанное после
        обращения разберёт wake_loop (self.pending). Микрофон слышит динамики — без перебивания."""
        speaking = threading.Thread(target=self.say, args=(text,), daemon=True)
        mic = self._mic()
        speaking.start()
        if not getattr(mic, "echo_free", False):
            speaking.join()
            self._own_sound_done()
            return
        seg = Segmenter(noise_db=self.noise_db)
        frames, hushed, n = mic.frames(), False, 0
        while speaking.is_alive() or seg.in_speech:
            if self.summoned.is_set():                 # клавиша: замолчать, команду слушает wake_loop
                self.hush()
                break
            audio, n = seg.feed(next(frames)), n + 1
            if audio is not None:                      # фраза кончилась: к Джарвису — разобрать, иначе забыть
                if hushed or self._is_called(audio):
                    self.hush()
                    self.pending = audio
                    break
            elif not hushed and seg.in_speech and n % 15 == 0 and self._is_called(b"".join(seg.voiced)):
                self.hush()                            # «Джарвис…» ещё звучит — замолчать сразу, дослушать фразу
                hushed = True
        speaking.join()

    def summon(self) -> None:
        """Клавиша вызова: слушать команду без «Джарвис», а если команда уже звучит — закончить её сейчас."""
        self.summoned.set()

    def hear(self, wait_s: float | None = 8.0, signal: bool = False, pause_ms: int | None = None,
             until: Callable[[], bool] | None = None) -> Heard | None:
        """Одна фраза и её распознавание. signal — сначала звук «слушаю»;
        pause_ms — пауза, после которой фраза закончена; until() -> True — закончить сейчас."""
        audio = self.record(wait_s, signal, pause_ms, until)
        return None if audio is None else self.recognize(audio)

    def record(self, wait_s: float | None, signal: bool = False, pause_ms: int | None = None,
               until: Callable[[], bool] | None = None,
               on_pause: Callable[[Segmenter], bool] | None = None) -> bytes | None:
        mic = self._mic()
        if signal and self.sounds:
            beep("start")
            self._own_sound_done()
        seg = Segmenter(VadConfig(end_silence_ms=pause_ms) if pause_ms else None, noise_db=self.noise_db)
        audio = listen_once(mic.frames(), seg, wait_s=wait_s, until=until, on_pause=on_pause)
        self.last_speech_ms = seg.last_speech_ms
        if seg.calibrated:
            self.noise_db = seg.noise_db
        return audio

    def recognize(self, audio: bytes) -> Heard:
        t = time.perf_counter()
        raw = (self.recognizer.transcribe(audio) or "").strip()
        return Heard(raw, normalize_spoken(raw), time.perf_counter() - t, len(audio) / 2 / SAMPLE_RATE, audio)

    def ask_yes_no(self, question: str, tries: int = 2) -> bool:
        """Вопрос голосом и ответ голосом. Не поняла дважды — считаем «нет»."""
        for attempt in range(tries):
            self.log(f"❓ {question}")
            self.say(question if attempt == 0 else "Скажите да или нет.")
            self._own_sound_done()
            heard = self.hear(wait_s=6.0, signal=True)
            if heard:
                self.log(f"🎤 «{heard.raw}»")
            answer = yes_no(heard.raw) if heard else None
            if answer is not None:
                return answer
        self.say("Не расслышала, не выполняю.")
        return False

    def wake_loop(self, on_command: Callable[[Heard], None], wake: str = WAKE_WORD,
                  stop: Callable[[], bool] = lambda: False) -> None:
        """Слушает всё время. «Джарвис, открой стим» — сразу команда; «Джарвис» или клавиша — сигнал, и следующая фраза —
        команда. Фраза, которой перебили ответ (speak), разбирается первой; «Джарвис, хватит» — просто замолчать."""
        try:
            self._wake_loop(on_command, wake, stop)
        finally:
            self.close()

    def _wake_loop(self, on_command, wake, stop) -> None:
        while not stop():
            if self.summoned.is_set():
                heard = self.command(KEY_PAUSE_MS, wake, early=False)
                if heard is not None:
                    on_command(self.exact(heard, wake))
                continue
            interrupted, audio, guess = self.pending is not None, self.pending, []
            self.pending = None
            if audio is None:
                audio = self.record(wait_s=None, until=self.summoned.is_set,
                                    on_pause=self._pauser(guess, lambda seg: self._guess_wake(seg, wake), early=True))
            if audio is None or self.summoned.is_set():
                continue
            g = self._valid(guess)
            if g is not None:                                  # догадка во время паузы — уже всё распознано
                addressed, near = ((g.heard, g.rest) if g.heard else None), g.near
            else:
                addressed, near = self._addressed(audio, wake)
            if addressed is None:                              # чужой разговор, музыка, Discord, стук клавиш
                if near:
                    self.log(f"👂 похоже на обращение, но не принято: {near}")
                continue
            heard, rest = addressed
            if interrupted and rest and yes_no(rest) is False:
                continue                                       # «Джарвис, хватит» во время ответа — уже замолчала
            if not rest:
                heard = self.command(None, wake)
                if heard is not None:
                    on_command(self.exact(heard, wake))
                continue
            on_command(self.exact(Heard(heard.raw, normalize_spoken(rest), heard.stt_s, heard.audio_s, heard.audio,
                                        heard.cloud), wake))

    def exact(self, heard: Heard, wake: str = WAKE_WORD) -> Heard:
        """Команда — в Chirp 3: английские названия латиницей (если облако начали спрашивать ещё во время паузы — ждём
        тот ответ). Нет сети, долго, пусто — остаётся запись GigaAM."""
        t = time.perf_counter()
        try:
            raw = heard.cloud.result(timeout=CLOUD_ASR_WAIT_S) if heard.cloud else cloud_transcribe(heard.audio)
        except Exception as exc:                                    # noqa: BLE001 — облако не обязательно
            self.log(f"☁ облако не распознало ({type(exc).__name__}) — команда в записи GigaAM")
            return heard
        called = split_wake(heard.raw, wake, True)[0]               # GigaAM слышал обращение (имя не отрезалось)
        woke, rest = split_wake(raw, wake, called)                  # «Javiers, включи…» — облако пишет имя по-своему
        text = normalize_spoken(rest if woke else raw)
        return Heard(raw, text, time.perf_counter() - t, heard.audio_s, heard.audio) if text else heard

    def command(self, pause_ms: int | None, wake: str = WAKE_WORD, early: bool = True) -> Heard | None:
        """Команда после сигнала «слушаю»: конец — пауза (ясная полная команда — короче, если early) или нажатие
        клавиши. Не услышала — сигнал ошибки и None. Снова только «Джарвис» — ещё раз сигнал и слушать."""
        for _ in range(2):
            self.summoned.clear()
            guess: list = []
            audio = self.record(6.0, True, pause_ms, self.summoned.is_set,
                                on_pause=self._pauser(guess, lambda seg: self._guess_command(seg, wake), early))
            self.summoned.clear()
            if audio is None:
                break
            g = self._valid(guess)
            heard = g.heard if g else self.recognize(audio)
            if not heard.raw:
                break
            again, after = split_wake(heard.raw, wake)              # «Джарвис… Джарвис, открой стим»
            if again and not after:
                continue
            return Heard(heard.raw, normalize_spoken(after if again else heard.raw), heard.stt_s, heard.audio_s,
                         heard.audio, heard.cloud)
        if self.sounds:
            beep("error")
        return None
