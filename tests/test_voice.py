"""Голос без микрофона и без динамиков: детектор речи на синтетическом звуке, «Джарвис», ответы «да/нет», голосовые
ответы на команды, синтез голоса (если модель уже скачана) и весь цикл Джарвиса с поддельными микрофоном и распознаванием.

    python3 -m pytest tests/test_voice.py -q
"""
import builtins
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml"):
    sys.path.insert(0, str(p))

from jarvis import app, dispatcher   # noqa: E402
from jarvis import browser as G     # noqa: E402
from jarvis import listen as V      # noqa: E402
from jarvis import speak as O       # noqa: E402

REAL_CLOUD_TRANSCRIBE = V.cloud_transcribe       # conftest на время тестов заменяет его запретом на облако
REAL_MIC_ENTER = V.Microphone.__enter__          # и открытие настоящего микрофона

SR = V.SAMPLE_RATE


def tone(seconds: float, db: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    amp = 32768 * 10 ** (db / 20) * np.sqrt(2)
    return amp * np.sin(2 * np.pi * freq * t) * (0.75 + 0.25 * np.sin(2 * np.pi * 4 * t))    # «слоги»


def noise(seconds: float, db: float, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 32768 * 10 ** (db / 20), int(seconds * SR))


def pcm(*parts: np.ndarray) -> bytes:
    return np.clip(np.concatenate(parts), -32768, 32767).astype(np.int16).tobytes()


def frames_of(audio: bytes):
    for i in range(0, len(audio) - V.FRAME_BYTES + 1, V.FRAME_BYTES):
        yield audio[i:i + V.FRAME_BYTES]


class FakeMic:
    """Микрофон с заранее заготовленным звуком: один непрерывный поток, как у настоящего (он пишет всё время).
    Звук кончился — then() (например, KeyboardInterrupt: «выключили ассистента»). echo_free — динамики вычтены."""
    def __init__(self, audio: bytes, then=None, echo_free=False):
        self.stream, self.then, self.echo_free = frames_of(audio), then, echo_free
        self.flushed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def frames(self):
        for frame in self.stream:              # не yield from: закрытие этого генератора не должно закрыть поток
            yield frame
        if self.then is not None:
            raise self.then()

    def flush(self):
        self.flushed += 1


def mic_factory(*audios: bytes, then=None, echo_free=False):
    """Микрофон, в который по очереди сказаны audios."""
    return lambda: FakeMic(b"".join(audios), then, echo_free)


class FakeRecognizer:
    name = "fake"

    def __init__(self, *texts: str):
        self.texts = list(texts)

    def transcribe(self, pcm_bytes: bytes) -> str:
        return self.texts.pop(0) if self.texts else ""

    def transcribe_timed(self, pcm_bytes: bytes):
        return self.transcribe(pcm_bytes), []                   # без меток времени: имя не отрезается


PAUSE_S = V.VadConfig().end_silence_ms / 1000                             # пауза, после которой фраза закончена
SPEECH = pcm(noise(0.5, -65), tone(1.2, -22), noise(PAUSE_S + 0.4, -65))
QUIET_SPEECH = pcm(noise(2.0, -66), tone(1.2, -40), noise(PAUSE_S + 0.4, -66))     # встроенный микрофон этого ноутбука


def session(*texts, audios=None, said=None, then=None):
    return V.VoiceSession(FakeRecognizer(*texts), say=(said.append if said is not None else lambda t: None),
                          mic_factory=mic_factory(*(audios or [SPEECH] * len(texts)), then=then), sounds=False,
                          log=lambda m: None)


class TestSegmenter(unittest.TestCase):
    def test_one_phrase_in_quiet_room(self):
        audio = V.listen_once(frames_of(SPEECH))
        secs = len(audio) / 2 / SR
        self.assertGreater(secs, 1.2)                            # вся речь + предзахват первого слога
        self.assertLess(secs, 1.2 + 0.4 + PAUSE_S + 0.1)        # + не больше паузы конца фразы

    def test_quiet_microphone(self):
        self.assertIsNotNone(V.listen_once(frames_of(QUIET_SPEECH)))

    def test_noisy_room_without_speech_gives_nothing(self):
        self.assertIsNone(V.listen_once(frames_of(pcm(noise(4.0, -38)))))

    def test_speech_over_room_noise(self):
        self.assertIsNotNone(V.listen_once(frames_of(pcm(noise(0.6, -45, 1), tone(1.0, -18) + noise(1.0, -45, 2),
                                                         noise(PAUSE_S + 0.4, -45, 3)))))

    def test_thinking_pause_does_not_end_the_phrase(self):
        """«Найди файл… (задумался на секунду) …caplets» — одна фраза, а не «найди файл»."""
        audio = V.listen_once(frames_of(pcm(noise(0.5, -65), tone(1.0, -22), noise(1.0, -65), tone(0.8, -22),
                                            noise(PAUSE_S + 0.4, -65))))
        self.assertGreater(len(audio) / 2 / SR, 2.8)

    def test_key_press_finishes_the_phrase_now(self):
        frames = list(frames_of(pcm(noise(0.5, -65), tone(10.0, -22))))
        n = iter(range(len(frames)))
        self.assertIsNone(V.listen_once(iter(frames), until=lambda: next(n) >= 10))      # до начала речи — ничего
        n = iter(range(len(frames)))
        audio = V.listen_once(iter(frames), until=lambda: next(n) >= 60)                  # посреди речи — сказанное
        self.assertAlmostEqual(len(audio) / 2 / SR, 1.5, delta=0.2)

    def test_click_is_not_speech(self):
        self.assertIsNone(V.listen_once(frames_of(pcm(noise(0.5, -65), tone(0.12, -10), noise(1.5, -65)))))

    def test_long_phrase_is_cut(self):
        audio = V.listen_once(frames_of(pcm(noise(0.3, -65), tone(20.0, -20))))
        self.assertLessEqual(len(audio) / 2 / SR, V.VadConfig().max_ms / 1000 + 0.5)

    def test_wait_timeout_on_silence(self):
        self.assertIsNone(V.listen_once(frames_of(pcm(noise(10.0, -65))), wait_s=2.0))


class TestWakeAndAnswers(unittest.TestCase):
    def test_split_wake(self):
        for text, want in [("Джарвис, открой стим.", (True, "открой стим")),
                           ("джарвис открой ютуб и найди рецепт борща", (True, "открой ютуб и найди рецепт борща")),
                           ("Эй, Джарвис, сделай громче", (True, "сделай громче")),
                           ("Jarvis open steam", (True, "open steam")),
                           ("Джаруз, открой Ютуб", (True, "открой ютуб")),            # так его однажды услышал GigaAM
                           ("Джарвис.", (True, "")),
                           ("открой браузер", (False, "открой браузер")),
                           ("мы вчера играли в джаз", (False, "мы вчера играли в джаз")),
                           ("ну и жара сегодня", (False, "ну и жара сегодня")),
                           ("Жарвис, открой стим", (True, "открой стим")),
                           ("джава лучше питона", (False, "джава лучше питона")),
                           ("Дарвис, открой стим", (True, "открой стим")),           # так GigaAM записал ваш голос
                           ("Джарвис. Джарвис.", (True, "")),                        # позвали дважды — ждать команду
                           ("Джарвис. Джарвис, сделай погромче.", (True, "сделай погромче")),
                           ("мы смотрели про дарвина", (False, "мы смотрели про дарвина")),
                           ("Jarvis, включи Discord. Jarvis", (True, "включи discord")),   # имя в конце — не команда
                           ("а потом джарвис сказал", (False, "а потом джарвис сказал"))]:
            with self.subTest(text=text):
                self.assertEqual(V.split_wake(text), want)

    def test_yes_no(self):
        for text, want in [("да", True), ("Да, давай", True), ("ага", True), ("конечно выполняй", True), ("нет", False),
                           ("нет не надо", False), ("да нет", False), ("отмена", False), ("стоп", False),
                           ("эээ", None), ("", None), ("что", None)]:
            with self.subTest(text=text):
                self.assertEqual(V.yes_no(text), want)


class TestSession(unittest.TestCase):
    def test_hear_normalizes_for_parser(self):
        s = session("подключи вэ пэ эн")
        heard = s.hear()
        self.assertEqual((heard.raw, heard.text), ("подключи вэ пэ эн", "подключи vpn"))
        self.assertIsNotNone(s.noise_db)                         # шум комнаты запомнен для следующей фразы

    def test_voice_confirmation_is_spoken(self):
        said = []
        self.assertTrue(session("эээ", "да", said=said).ask_yes_no("Удалить файл?"))
        self.assertEqual(said[0], "Удалить файл?")
        self.assertFalse(session("нет").ask_yes_no("Удалить файл?"))
        said = []
        self.assertFalse(session("что", "а", said=said).ask_yes_no("Удалить файл?"))      # не поняла дважды
        self.assertIn("не выполняю", said[-1])

    def test_wake_loop(self):
        texts = ["открой браузер",                    # не к ассистенту
                 "Джарвис, открой стим.",              # сразу команда
                 "Джарвис.", "Сделай громче.",         # обращение -> сигнал -> команда
                 "Джарвис.", "Джарвис, выключи звук",  # после сигнала ещё раз назвали по имени
                 "музыка играет"]
        got = []
        s = session(*texts, then=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["открой стим", "сделай громче", "выключи звук"])

    def test_long_sound_is_checked_by_its_start(self):
        """Музыка из динамиков: 10-секундный кусок без «Джарвиса» распознаётся только первые 3 с; с ним — целиком."""
        music = pcm(noise(0.5, -65), tone(10.0, -22), noise(PAUSE_S + 0.4, -65))
        rec = FakeRecognizer("ля-ля-ля", "Джарвис, найди", "Джарвис, найди эту песню на ютубе")
        lengths = []
        transcribe = rec.transcribe
        rec.transcribe = lambda b: lengths.append(len(b) / 2 / SR) or transcribe(b)
        got = []
        s = V.VoiceSession(rec, mic_factory=mic_factory(music, music, then=KeyboardInterrupt), sounds=False, log=lambda m: None)
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["найди эту песню на ютубе"])
        self.assertEqual([round(x) for x in lengths[:2]], [3, 3])
        self.assertAlmostEqual(lengths[2], 10 + V.GUESS_MS / 1000, delta=0.5)   # целиком — уже на паузе 0.45 с

    def test_jarvis_in_the_middle_of_a_phrase(self):
        """«так, ну вот… Джарвис, открой стим» одной фразой (пауза меньше 1.2 с): обращение — в начале своего
        высказывания, а не фразы; раньше проверялись только первые 3 с фразы."""
        audio = pcm(noise(0.5, -65), tone(2.0, -22), noise(0.4, -65), tone(1.2, -22, 300), noise(PAUSE_S + 0.4, -65))
        got = []
        s = session("так ну вот", "Джарвис, открой стим", "Джарвис, открой стим", audios=[audio], then=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["открой стим"])

    def test_name_lost_in_full_transcript(self):
        """Начало высказывания: «Джарес, открой мне ютуб» — обращение; целиком GigaAM записал «Открой мне ютуб»
        (имя потерял) — это всё равно команда, а не чужая речь."""
        long_cmd = pcm(noise(0.5, -65), tone(3.5, -22), noise(PAUSE_S + 0.4, -65))
        got = []
        s = session("Джарес, открой мне", "Открой мне ютуб.", audios=[long_cmd], then=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["открой мне ютуб"])

    def test_name_is_cut_off_before_the_cloud(self):
        """Метки времени GigaAM: команда начинается с первого слова после имени — облако имени не слышит
        (на замере 19.11 Chirp записал ваше «Джарвис» как «Да, Алиса»)."""
        for words, want in (([("джарес", 0.2), ("открой", 1.32), ("мне", 1.8)], 1.32),
                            ([("так", 0.1), ("джарвис", 0.4), ("джарвис", 1.0), ("громче", 1.6)], 1.6),
                            ([("джарвис", 0.5)], None), ([("открой", 0.3), ("стим", 0.8)], None)):
            with self.subTest(words=words):
                self.assertEqual(V.command_start(words), want)
        sent = []
        for words in ([("джарес", 0.6), ("открой", 1.3), ("мне", 1.6)], []):   # с метками времени и без них
            rec = FakeRecognizer()
            rec.transcribe_timed = lambda pcm, w=words: ("Джарес, открой мне ютуб.", w)
            s = V.VoiceSession(rec, sounds=False, log=lambda m: None, mic_factory=mic_factory(SPEECH, then=KeyboardInterrupt))
            with mock.patch.object(V, "cloud_transcribe", side_effect=lambda pcm: sent.append(len(pcm) / 2 / SR) or "Открой мне YouTube."), \
                    self.assertRaises(KeyboardInterrupt):
                s.wake_loop(lambda h: None)
        self.assertAlmostEqual(sent[1] - sent[0], 1.3 - 0.15, delta=0.01)    # имя отрезано, команда — нет


    def test_clicks_are_not_utterances(self):
        """Щелчки мыши и стук клавиш (по 1 кадру) — не начало высказывания: на них GigaAM не тратится."""
        clicks = np.concatenate([np.concatenate([tone(0.03, -20), noise(0.3, -65, i)]) for i in range(10)])
        audio = pcm(clicks, tone(1.2, -22))
        speech_at = len(clicks) * 2                                 # байт: здесь начинается голос
        starts = V.utterance_starts(audio, -60.0)
        self.assertTrue(starts and all(st >= speech_at - 0.7 * SR * 2 for st in starts), starts)   # только у голоса

    def test_how_gigaam_writes_your_jarvis(self):
        """Замер 19.11: GigaAM записал «Джарвис» как «Джарес» — это обращение; «джава» — нет."""
        self.assertEqual(V.split_wake("Джарес, открой мне YouTube."), (True, "открой мне youtube"))
        self.assertFalse(V.split_wake("джавы много не бывает")[0])

    def test_doubtful_name_is_checked_by_cloud(self):
        """«Жарес» (0.87) рядом с «джава» (0.87): решает облако — слышит ли оно «Джарвис»."""
        for cloud, want in (("Джарвис, открой стим.", ["открой стим"]), ("Java, открой стим.", []), (OSError("нет сети"), [])):
            with self.subTest(cloud=cloud):
                got = []
                s = session("Жарес, открой стим", then=KeyboardInterrupt)
                fake = mock.Mock(side_effect=cloud) if isinstance(cloud, Exception) else mock.Mock(return_value=cloud)
                with mock.patch.object(V, "cloud_transcribe", fake), self.assertRaises(KeyboardInterrupt):
                    s.wake_loop(lambda h: got.append(h.text))
                self.assertEqual(got, want)

    def test_near_miss_is_logged(self):
        logged = []
        s = V.VoiceSession(FakeRecognizer("Джава, открой стим"), sounds=False, log=logged.append,
                           mic_factory=mic_factory(SPEECH, then=KeyboardInterrupt))
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: None)
        self.assertTrue(any("похоже на обращение" in m and "джава" in m for m in logged))

    def test_key_summons_without_wake_word(self):
        """Alt+` — сразу команда; «Джарвис» в ней можно сказать, а можно нет."""
        for text, want in (("Открой стим.", "открой стим"), ("Джарвис, сделай громче", "сделай громче")):
            with self.subTest(text=text):
                got = []
                s = V.VoiceSession(FakeRecognizer(text), sounds=False, log=lambda m: None, mic_factory=mic_factory(
                    pcm(noise(0.5, -65), tone(1.2, -22), noise(V.KEY_PAUSE_MS / 1000 + 0.4, -65)), then=KeyboardInterrupt))
                s.summon()
                with self.assertRaises(KeyboardInterrupt):
                    s.wake_loop(lambda h: got.append(h.text))
                self.assertEqual(got, [want])
                self.assertFalse(s.summoned.is_set())

    def test_second_press_ends_the_command(self):
        """Нажал, говорит 10 с без пауз (или под шум), нажал ещё раз через 1,5 с — команда уже записана."""
        got = []
        s = V.VoiceSession(FakeRecognizer("найди файл caplets"), sounds=False, log=lambda m: None,
                           mic_factory=mic_factory(pcm(noise(0.5, -65), tone(10.0, -22)), then=KeyboardInterrupt))
        frames = FakeMic.frames

        def pressing(mic):
            for i, frame in enumerate(frames(mic)):
                if i == 60:
                    s.summon()
                yield frame
        s.summon()
        with mock.patch.object(FakeMic, "frames", pressing), self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append((h.text, round(h.audio_s, 1))))
        self.assertEqual([text for text, _ in got], ["найди файл caplets"])
        self.assertAlmostEqual(got[0][1], 1.5, delta=0.2)


class TestSpokenReplies(unittest.TestCase):
    def test_speakable_drops_paths_links_commands(self):
        for text, want in [
            ("Открываю папку «Загрузки»: /home/u/Downloads", "Открываю папку Загрузки"),
            ("Перемещаю «/home/u/Desktop/report_final.pdf» в корзину. Выполнить?", "Перемещаю report final в корзину. Выполнить?"),
            ("Запускаю Steam: /usr/bin/steam", "Запускаю Steam"),
            ("Ищу в Google «борщ»: https://www.google.com/search?q=борщ", "Ищу в Google борщ"),
            ("[dry-run] Делаю файл «run.sh» исполняемым (chmod +x /home/u/run.sh) -> chmod +x /home/u/run.sh",
             "Делаю файл run.sh исполняемым"),
            ("Устанавливаю громкость на 30%", "Устанавливаю громкость на 30%"),
            ("**Готово**: _сделала_", "Готово: сделала"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(O.speakable(text), want)

    def test_done_is_not_retold(self):
        """Сделанное не пересказывается: «выполняю», «делаю»… — и не одно и то же два раза подряд."""
        res = {"route": "browser", "site": "ютуб", "query": "рецепт борща", "trace": {}}
        plan = G.Plan("site_search", "https://www.youtube.com/results?search_query=x", "youtube.com", "рецепт борща", "opensearch")
        said = [O.spoken_reply(res, plan, "Сайт youtube.com, ищу …: https://…") for _ in range(6)]
        self.assertTrue(set(said) <= set(O.ACKS))
        self.assertTrue(all(a != b for a, b in zip(said, said[1:])))
        volume = mock.Mock(category="audio")
        self.assertIn(O.spoken_reply({"route": "system", "trace": {}}, volume, "Прибавляю громкость на 20%"), O.ACKS)

    def test_answers_and_failures_are_spoken(self):
        battery = mock.Mock(category="info")
        self.assertEqual(O.spoken_reply({"route": "system", "trace": {}}, battery, "Батарея: 80%, заряжается"),
                         "Батарея: 80%, заряжается")
        self.assertEqual(O.spoken_reply({"route": "browser", "trace": {}}, None, "Искать нечего."), "Искать нечего.")

    def test_thinking_before_second_layer(self):
        """Фраза ушла второму слою — сразу «думаю» (не дожидаясь ответа); команда первого слоя — без этого."""
        agent = mock.Mock(spec=["handle"])
        agent.handle.return_value = "Ноготь — из кератина."
        for route, want in (("llm", 1), ("system", 0)):
            with self.subTest(route=route):
                said = []
                dispatcher.dispatch_phrase("из чего ноготь", parser=lambda p: {"route": route, "action": "vol_up"},
                                           dry_run=True, agent=agent, notify=said.append)
                self.assertEqual(len(said), want)
                self.assertTrue(set(said) <= set(O.THINKING))

    def test_speech_can_be_cut_off(self):
        """stop() обрывает звучащую фразу (так работает перебивание «Джарвис…»)."""
        t = time.perf_counter()
        playing = threading.Thread(target=O._run_audible, args=(["sleep", "5"],))
        playing.start()
        while not O._PLAYING and time.perf_counter() - t < 2:
            time.sleep(0.01)
        O.Speaker().stop()
        playing.join(3)
        self.assertLess(time.perf_counter() - t, 2)

    def test_llm_answer_is_spoken_in_full(self):
        long = "Ноготь состоит из кератина. " * 20
        self.assertEqual(O.spoken_reply({"route": "llm", "trace": {"layer": "llm"}}, None, long), long.strip())

    def test_long_executor_message_is_shortened(self):
        msg = "Сделала. " + "Очень длинное пояснение исполнителя. " * 20
        self.assertLessEqual(len(O.spoken_reply({"route": "system", "trace": {}}, None, msg)), 221)


class TestPiperVoice(unittest.TestCase):
    """Настоящий синтез — если голос уже скачан (иначе пропуск: тесты не ходят в сеть)."""
    def test_synthesize(self):
        try:
            from huggingface_hub import hf_hub_download
            path = "ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx"
            model = hf_hub_download(O.PIPER_REPO, path, local_files_only=True)
            hf_hub_download(O.PIPER_REPO, path + ".json", local_files_only=True)
            voice = O.PiperVoice(model)
        except Exception as exc:                                 # noqa: BLE001
            self.skipTest(f"голос не скачан: {exc}")
        audio = np.frombuffer(voice.synthesize("Открываю ютуб, ищу рецепт борща. Сейчас 19:42."), dtype=np.int16)
        self.assertGreater(len(audio) / voice.sample_rate, 2.0)
        self.assertGreater(np.abs(audio).max(), 10000)


class TestJarvis(unittest.TestCase):
    def run_jarvis(self, texts, isatty=False):
        said, dispatched = [], []

        def fake_dispatch(phrase, **_):
            dispatched.append(phrase)
            plan = G.Plan("site_home", "https://store.steampowered.com", "store.steampowered.com")
            return {"route": "browser", "site": "стим", "trace": {"parse_s": 0.01, "exec_s": 0.0}}, plan, "ok"

        speaker = mock.Mock(name="speaker")
        speaker.name = "fake"
        speaker.say.side_effect = said.append
        with mock.patch.object(V, "Recognizer", return_value=FakeRecognizer(*texts)), \
                mock.patch.object(V, "Microphone", side_effect=mic_factory(*[SPEECH] * len(texts), then=KeyboardInterrupt)), \
                mock.patch.object(V, "beep"), \
                mock.patch.object(O.Speaker, "load", return_value=speaker), \
                mock.patch.object(dispatcher, "dispatch_phrase", side_effect=fake_dispatch), \
                mock.patch.object(app.parser, "preload_model", return_value=0.0), \
                mock.patch.object(sys.stdin, "isatty", return_value=isatty), \
                mock.patch.object(builtins, "print"):
            code = app.main([])
        return code, dispatched, said

    def test_says_jarvis_then_command_and_hears_the_answer(self):
        code, dispatched, said = self.run_jarvis(["Мы говорим в дискорде.", "Джарвис.", "Открой стим."])
        self.assertEqual(code, 0)
        self.assertEqual(dispatched, ["открой стим"])          # облако в тестах недоступно — запись GigaAM
        self.assertEqual(len(said), 1)
        self.assertIn(said[0], O.ACKS)                                 # сделанное не пересказывается

    def test_key_signal_summons_jarvis(self):
        """Alt+` в GNOME -> systemctl шлёт службе SIGUSR1 -> Джарвис слушает команду; после выхода сигнал снова по умолчанию."""
        import os
        import signal
        seen = []

        def loop(session, on_command, **_):
            os.kill(os.getpid(), signal.SIGUSR1)
            seen.append(session.summoned.is_set())
        with mock.patch.object(V.VoiceSession, "wake_loop", loop):
            self.run_jarvis([])
        self.assertEqual(seen, [True])
        self.assertIs(signal.getsignal(signal.SIGUSR1), signal.SIG_DFL)

    def test_no_microphone_or_model(self):
        with mock.patch.object(V, "Recognizer", side_effect=V.VoiceUnavailable("нет модели")), \
                mock.patch.object(builtins, "print") as pr:
            self.assertEqual(app.main([]), 1)
        self.assertIn("нет модели", " ".join(str(c) for c in pr.call_args_list))


class TestGuessDuringPause(unittest.TestCase):
    """На 0.45 с тишины Джарвис уже угадывает (обращение, команда — в облако); на 0.8 с ясная полная команда считается
    законченной. Заговорили снова — догадка выбрасывается."""

    def run_one(self, audio, texts, complete=lambda text: False, cloud="Открой Steam."):
        rec = FakeRecognizer(*texts)
        calls = []
        transcribe = rec.transcribe
        rec.transcribe = lambda pcm: calls.append(len(pcm) / 2 / SR) or transcribe(pcm)
        sent, got, ends = [], [], []
        s = V.VoiceSession(rec, sounds=False, log=lambda m: None, complete=complete,
                           mic_factory=mic_factory(audio, then=KeyboardInterrupt))
        record = s.record
        s.record = lambda *a, **k: (lambda r: ends.append(s.last_speech_ms) or r)(record(*a, **k))
        with mock.patch.object(V, "cloud_transcribe", side_effect=lambda pcm: sent.append(len(pcm)) or cloud), \
                self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        return got, calls, sent

    def test_guess_is_used_when_nothing_more_was_said(self):
        got, calls, sent = self.run_one(SPEECH, ["Джарвис, открой стим"])
        self.assertEqual(got, ["открой steam"])
        self.assertEqual(len(calls), 1)                          # GigaAM — один раз, на паузе
        self.assertEqual(len(sent), 1)                           # облако — один раз, тоже на паузе

    def test_speaking_again_drops_the_guess(self):
        """«Джарвис, сделай громче … (0.6 с) … на двадцать процентов» — одна команда, а не «сделай громче»."""
        audio = pcm(noise(0.5, -65), tone(1.0, -22), noise(0.6, -65), tone(1.0, -22, 300), noise(PAUSE_S + 0.4, -65))
        got, calls, sent = self.run_one(audio, ["Джарвис, сделай громче", "Джарвис, сделай громче на 20 процентов",
                                                "Джарвис, сделай громче на 20 процентов"],
                                        cloud="Сделай громче на 20 процентов.")
        self.assertEqual(got, ["сделай громче на 20 процентов"])

    def test_clear_command_ends_early_unclear_waits(self):
        for complete, want_end in ((lambda text: True, V.EARLY_END_MS), (lambda text: False, V.VadConfig().end_silence_ms)):
            with self.subTest(early=want_end):
                audio = pcm(noise(0.5, -65), tone(1.0, -22), noise(2.0, -65))
                rec = FakeRecognizer("Джарвис, открой стим")
                s = V.VoiceSession(rec, sounds=False, log=lambda m: None, complete=complete,
                                   mic_factory=mic_factory(audio, then=KeyboardInterrupt))
                seen = []
                orig = V.listen_once

                def spy(frames, seg=None, **kw):
                    out = orig(frames, seg, **kw)
                    seen.append(len(out) / 2 / SR if out else 0)
                    return out
                with mock.patch.object(V, "listen_once", spy), mock.patch.object(V, "cloud_transcribe", return_value="Открой Steam."), \
                        self.assertRaises(KeyboardInterrupt):
                    s.wake_loop(lambda h: None)
                preroll = (V.VadConfig().preroll_ms + V.VadConfig().start_frames * V.FRAME_MS) / 1000
                self.assertAlmostEqual(seen[0], preroll + 1.0 + want_end / 1000, delta=0.1)   # речь + пауза до конца

    def test_no_early_end_after_the_key(self):
        """После Alt+` «договорил» решает второе нажатие или пауза 2 с — догадка не обрывает задумавшегося."""
        audio = pcm(noise(0.5, -65), tone(1.0, -22), noise(V.KEY_PAUSE_MS / 1000 + 0.4, -65))
        rec = FakeRecognizer("Открой стим")
        s = V.VoiceSession(rec, sounds=False, log=lambda m: None, complete=lambda text: True,
                           mic_factory=mic_factory(audio, then=KeyboardInterrupt))
        s.summon()
        got = []
        with mock.patch.object(V, "cloud_transcribe", return_value="Открой Steam."), self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["открой steam"])
        self.assertGreaterEqual(s.last_speech_ms, 900)


class TestCompleteCommand(unittest.TestCase):
    """Ясная полная команда заканчивается на 0.8 с тишины; обрывок («открой папку…», «поставь громкость на…») — нет."""

    def check(self, phrase, parsed, folder=None, desktop=None, game=None):
        parsed = {"route": "system", "action": "vol_up", "site": "", "query": "", "text": "", "confidence": 0.99,
                  "fallback": False, **parsed}
        with mock.patch("jarvis.parser.peek", return_value=parsed), \
                mock.patch.object(dispatcher.apps, "_match_standard_folder", return_value=folder), \
                mock.patch.object(dispatcher.apps, "find_desktop_app", return_value=desktop), \
                mock.patch.object(dispatcher.apps, "find_steam_game", return_value=game):
            return dispatcher.is_complete_command(phrase)

    def test_clear_commands(self):
        app = {"route": "app", "action": "app_open"}
        self.assertTrue(self.check("сделай громче", {}))
        self.assertTrue(self.check("открой стим", {**app, "site": "стим"}, desktop=mock.Mock(names=("Install Steam", "steam"))))
        self.assertTrue(self.check("открой загрузки", {**app, "site": "загрузки"}, folder=mock.Mock(kind="folder")))
        self.assertTrue(self.check("запусти дота 2", {**app, "site": "дота 2"}, game=("Dota 2", "570", "dota 2 beta")))
        self.assertTrue(self.check("найди на ютубе рецепт борща", {"route": "browser", "action": "web_search",
                                                                   "site": "ютубе", "query": "рецепт борща"}))
        self.assertTrue(self.check("открой ютуб", {"route": "browser", "action": "web_open", "site": "ютуб"}))

    def test_fragments_wait(self):
        app = {"route": "app", "action": "app_open"}
        for phrase, parsed, desktop in [
            ("сделай", {}, None),                                                        # одно слово
            ("поставь громкость на", {"action": "vol_set"}, None),                       # кончается предлогом
            ("открой файл", {**app, "site": "файл"}, mock.Mock(names=("File Roller", "Archive Manager"))),  # не File Roller
            ("открой папку", {**app, "site": "папку"}, None),                           # такой программы нет
            ("открой папку note", {**app, "site": "note"}, None),                       # ваши папки — после паузы
            ("найди на ютубе", {"route": "browser", "action": "web_open", "site": "ютубе"}, None),   # что найти?
            ("создай папку", {"action": "fs_mkdir"}, None),                              # файлам нужно имя
            ("удали файл отчёт", {"action": "fs_trash", "query": "отчёт"}, None),       # опасное — всегда ждём
            ("из чего сделан ноготь", {"route": "llm", "action": "none"}, None),        # вопрос второму слою
            ("сделай громче", {"confidence": 0.9}, None),                                # модель не уверена
        ]:
            with self.subTest(phrase=phrase):
                self.assertFalse(self.check(phrase, parsed, desktop=desktop))


class TestAlwaysListening(unittest.TestCase):
    """Микрофон пишет всё время; пока Джарвис говорит, он слушает, и «Джарвис…» обрывает ответ."""

    def speaking_session(self, heard_text: str, audio: bytes, echo_free=True):
        stopped = threading.Event()
        said = []

        def say(text):
            said.append(text)
            stopped.wait(2)                                      # «говорит» до 2 с, если не оборвут
        s = V.VoiceSession(FakeRecognizer(*[heard_text] * 10), say=say, hush=stopped.set, sounds=False, log=lambda m: None,
                           mic_factory=mic_factory(audio, then=KeyboardInterrupt, echo_free=echo_free))
        return s, stopped, said

    def test_jarvis_cuts_the_answer_off(self):
        s, stopped, _ = self.speaking_session("Джарвис, хватит", SPEECH + pcm(noise(1.0, -65)))
        t = time.perf_counter()
        s.speak("Очень длинный ответ " * 20)
        self.assertTrue(stopped.is_set())
        self.assertLess(time.perf_counter() - t, 1.5)
        got = []
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, [])                                # «хватит» — просто замолчать

    def test_new_command_over_the_answer(self):
        s, stopped, _ = self.speaking_session("Джарвис, открой стим", SPEECH + pcm(noise(1.0, -65)))
        s.speak("Очень длинный ответ " * 20)
        got = []
        with self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        self.assertEqual(got, ["открой стим"])

    def test_mic_that_hears_speakers_is_not_used_for_interrupting(self):
        s, stopped, said = self.speaking_session("Джарвис, хватит", SPEECH, echo_free=False)
        s.speak("Короткий ответ")
        self.assertFalse(stopped.is_set())                       # иначе Джарвис перебивал бы сам себя
        self.assertEqual((said, s.mic.flushed), (["Короткий ответ"], 1))   # свой голос из записи выброшен

    def test_microphone_keeps_what_was_said_while_busy(self):
        """Поток пишет в очередь сам: всё, что успело прозвучать, пока никто не читал, остаётся."""
        audio = pcm(tone(1.0, -20))
        with tempfile.NamedTemporaryFile() as f:
            f.write(audio)
            f.flush()
            mic = V.Microphone.__new__(V.Microphone)
            mic.cmd, mic.echo_free, mic.proc, mic.queue = ["cat", f.name], False, None, queue.Queue()
            REAL_MIC_ENTER(mic)
            got = []
            with self.assertRaises(V.VoiceUnavailable):           # запись кончилась — как отключённый микрофон
                for frame in mic.frames():
                    got.append(frame)
            mic.__exit__()
        self.assertEqual(b"".join(got), audio[:len(got) * V.FRAME_BYTES])
        self.assertEqual(len(got), len(audio) // V.FRAME_BYTES)


class TestCloudCommand(unittest.TestCase):
    """«Джарвис» слушает GigaAM на этом компьютере, а команду после обращения распознаёт Chirp 3 (английские названия
    латиницей); без облака команда остаётся в записи GigaAM."""

    def run_loop(self, texts, cloud, audios=None):
        got, sent = [], []

        def chirp(pcm):
            sent.append(len(pcm))
            if isinstance(cloud, Exception):
                raise cloud
            return cloud
        s = session(*texts, audios=audios, then=KeyboardInterrupt)
        with mock.patch.object(V, "cloud_transcribe", side_effect=chirp), self.assertRaises(KeyboardInterrupt):
            s.wake_loop(lambda h: got.append(h.text))
        return got, sent

    def test_command_is_heard_by_cloud(self):
        got, sent = self.run_loop(["Джарвис, открой паспортс."], "Джарвис, открой Passwords.")
        self.assertEqual(got, ["открой passwords"])                  # обращение убрано, печатный вид как у GigaAM
        self.assertEqual(len(sent), 1)

    def test_command_after_signal_is_heard_by_cloud(self):
        got, sent = self.run_loop(["Джарвис.", "Загугли халтусентер эдиф."], "Загугли how to center a div.")
        self.assertEqual(got, ["загугли how to center a div"])
        self.assertEqual(len(sent), 1)                               # само «Джарвис» в облако не уходит

    def test_cloud_spells_the_name_its_own_way(self):
        """GigaAM уже услышал обращение — первое слово облака «Javiers» тоже обращение, а не команда."""
        got, _ = self.run_loop(["Джарвис, включи ютуб."], "Javiers, включи YouTube.")
        self.assertEqual(got, ["включи youtube"])

    def test_called_again_after_signal_waits_for_command(self):
        got, sent = self.run_loop(["Джарвис.", "Дарвис.", "Сделай громче."], "Сделай громче.")
        self.assertEqual((got, len(sent)), (["сделай громче"], 1))

    def test_strangers_never_reach_cloud(self):
        got, sent = self.run_loop(["музыка играет", "мы говорим в дискорде"], "что угодно")
        self.assertEqual((got, sent), ([], []))

    def test_no_cloud_keeps_gigaam(self):
        for cloud in (TimeoutError("долго"), OSError("нет сети"), ""):
            with self.subTest(cloud=cloud):
                got, sent = self.run_loop(["Джарвис, открой паспортс."], cloud)
                self.assertEqual((got, len(sent)), (["открой паспортс"], 1))

    def test_cloud_request(self):
        """Chirp 3 в Европе, русский язык, сырой звук 16 кГц; ждём не дольше CLOUD_ASR_WAIT_S."""
        creds = mock.Mock(valid=True, token="t")
        http = mock.Mock()
        http.post.return_value.json.return_value = {"results": [{"alternatives": [{"transcript": "Открой Passwords."}]},
                                                                {"alternatives": [{"transcript": "Пожалуйста."}]}]}
        with mock.patch.object(V, "_cloud_session", return_value=(creds, "proj", http)):
            self.assertEqual(REAL_CLOUD_TRANSCRIBE(b"\x01\x00" * 1600), "Открой Passwords. Пожалуйста.")
        (url,), kw = http.post.call_args
        self.assertEqual(url, "https://eu-speech.googleapis.com/v2/projects/proj/locations/eu/recognizers/_:recognize")
        self.assertEqual(kw["timeout"], V.CLOUD_ASR_WAIT_S)
        self.assertEqual(kw["json"]["config"]["model"], "chirp_3")
        self.assertEqual(kw["json"]["config"]["languageCodes"], ["ru-RU"])
        self.assertEqual(kw["json"]["config"]["explicitDecodingConfig"]["sampleRateHertz"], V.SAMPLE_RATE)


class TestService(unittest.TestCase):
    def test_install_and_uninstall(self):
        """Служба, микрофон без эха (конфиг PipeWire) и клавиша Alt+`; всё — во временной домашней папке."""
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        keys = ["/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"]     # чужая клавиша остаётся

        def run(cmd, **_):
            if cmd[:3] == ["gsettings", "set", app._KEYS] and cmd[3] == "custom-keybindings":
                keys[:] = eval(cmd[4])                                                 # noqa: S307 — список строк
            return mock.Mock(returncode=0, stderr="", stdout=f"@as {keys}" if cmd[:2] == ["gsettings", "get"] else "")
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch("subprocess.run", side_effect=run) as calls, \
                mock.patch.object(builtins, "print"):
            self.assertEqual(app.main(["--install"]), 0)
            unit = (home / ".config/systemd/user/jarvis.service").read_text(encoding="utf-8")
            self.assertIn("KillMode=process", unit)
            self.assertIn("assistant.py", unit)
            conf = (home / ".config/pipewire/pipewire.conf.d/60-jarvis-echo-cancel.conf").read_text(encoding="utf-8")
            self.assertIn(f'node.name = "{V.ECHO_FREE_MIC}"', conf)
            self.assertIn("monitor.mode = true", conf)
            cmds = [c.args[0] for c in calls.call_args_list]
            self.assertIn(["systemctl", "--user", "restart", *app._PIPEWIRE], cmds)
            self.assertIn(["systemctl", "--user", "restart", "jarvis.service"], cmds)
            self.assertEqual(keys[1:], [app._KEY_PATH])
            self.assertIn(["gsettings", "set", f"{app._KEYS}.custom-keybinding:{app._KEY_PATH}", "command",
                           app.HOTKEY_COMMAND], cmds)

            calls.reset_mock()
            self.assertEqual(app.main(["--install"]), 0)                       # повторно: звук не перезапускается
            self.assertNotIn(["systemctl", "--user", "restart", *app._PIPEWIRE], [c.args[0] for c in calls.call_args_list])
            self.assertEqual(len(keys), 2)

            self.assertEqual(app.main(["--uninstall"]), 0)
            self.assertFalse((home / ".config/systemd/user/jarvis.service").exists())
            self.assertFalse((home / ".config/pipewire/pipewire.conf.d/60-jarvis-echo-cancel.conf").exists())
            self.assertEqual(keys, ["/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"])


if __name__ == "__main__":
    unittest.main()
