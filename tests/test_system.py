"""Тесты системного и app-маршрутов БЕЗ модели (модель подменяется «эталоном» из tests/cases_system.py).

    python3 -m pytest tests/test_system.py -q

Что проверяется:
  • сам корпус: действия существуют, слоты — дословные слова фразы;
  • «идеальный парсер» -> исполнитель: для каждой фразы строится план, в --dry-run виден правильный аргумент
    (процент, путь, длительность, разрешение...), а файловые команды смотрят во временную «домашнюю папку»;
  • разбор аргументов: проценты, секунды, длительность, время, разрешение, горячие клавиши GNOME;
  • полнота: у каждого из SYSTEM_ACTIONS есть обработчик, и он строит план;
  • сквозной путь assistant.dispatch_phrase (маршрут system / app) на подменённом парсере.
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

from jarvis import apps  # noqa: E402
from jarvis import dispatcher as assistant   # noqa: E402
import ml_spec                      # noqa: E402
from jarvis import system as S      # noqa: E402
from cases_system import ALL as ALL_BASE, ALL_SYSTEM as SYSTEM_BASE, APPS as APPS_BASE, TO_LLM   # noqa: E402
from cases_system_big import ALL_BIG_SYSTEM, APPS_BIG, TO_LLM_BIG              # noqa: E402

ALL_SYSTEM = SYSTEM_BASE + ALL_BIG_SYSTEM
APPS = APPS_BASE + APPS_BIG
ALL = ALL_SYSTEM + APPS
ALL_LLM = TO_LLM + TO_LLM_BIG


def make_fake_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="pc_agent_sys_"))
    for d in ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos"):
        (home / d).mkdir()
    for d in ("education", "traning ml", "ml_model", "проекты", "rubert-first-layer-intent-parser-lab"):
        (home / "Desktop" / d).mkdir()
    for f in ("traning ml/ml_log.py", "main.py", "report_final.pdf", "отчёт.docx", "заметки.txt", "setup.sh", "старый_бэкап.zip",
              "lab.zip", "temp_old.log", "море.jpg", "assistant.py", "курсовая.docx", "data.csv", "видео.mp4", "notes.md",
              "backup.tar.gz", "run.sh"):
        (home / "Desktop" / f).write_bytes(b"x")
    (home / "Pictures" / "закат.jpg").write_bytes(b"x")
    return home


class TestCorpus(unittest.TestCase):
    def test_actions_exist_and_slots_are_words_of_the_phrase(self):
        seen = set()
        for c in ALL:
            with self.subTest(phrase=c.phrase):
                key = " ".join(c.phrase.casefold().split())
                self.assertNotIn(key, seen, "дубликат")
                seen.add(key)
                if c.route == "system":
                    self.assertIn(c.action, ml_spec.SYSTEM_ACTIONS)
                    self.assertTrue(not c.text or c.action == "reminder_set")
                else:
                    self.assertIn(c.action, ("app_open", "app_query"))
                    self.assertTrue(c.site)
                ml_spec.tag_phrase(c.phrase, c.site, c.query, c.text)      # ValueError, если слот не из слов фразы

    def test_corpus_is_broad(self):
        self.assertGreaterEqual(len(ALL_SYSTEM), 500)
        self.assertGreaterEqual(len(APPS), 45)
        self.assertGreaterEqual(len(ALL_LLM), 60)

    def test_every_action_has_at_least_three_phrases(self):
        import collections
        cnt = collections.Counter(c.action for c in ALL_SYSTEM)
        thin = {a: cnt[a] for a in ml_spec.SYSTEM_ACTIONS if cnt[a] < 3 and a != "fs_delete"}
        self.assertEqual(thin, {}, "у этих действий меньше трёх фраз в ручном корпусе")

    def test_llm_cases_have_no_slots_and_do_not_repeat_commands(self):
        commands = {" ".join(c.phrase.casefold().split()) for c in ALL}
        for c in ALL_LLM:
            with self.subTest(phrase=c.phrase):
                self.assertEqual((c.route, c.action, c.query, c.site, c.text), ("llm", "none", "", "", ""))
                self.assertNotIn(" ".join(c.phrase.casefold().split()), commands)


class TestExecutorWithPerfectParser(unittest.TestCase):
    """Если модель вернула эталон, исполнитель обязан построить план с правильными аргументами."""

    @classmethod
    def setUpClass(cls):
        cls.home = make_fake_home()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_every_system_case_builds_a_plan_with_the_right_argument(self):
        for c in ALL_SYSTEM:
            with self.subTest(phrase=c.phrase):
                plan = S.make_system_plan(query=c.query, action=c.action, site=c.site, home=self.home, text=c.text)
                self.assertIsNotNone(plan)
                msg = S.execute_system_plan(plan, dry_run=True)
                self.assertTrue(msg.startswith("[dry-run]"), msg)
                if c.check:
                    self.assertIn(c.check.casefold(), msg.casefold())

    def test_every_app_case_resolves(self):
        for c in APPS:
            with self.subTest(phrase=c.phrase):
                plan = apps.make_app_plan(c.site, c.query, home=self.home)
                self.assertIsNotNone(plan)
                self.assertTrue(plan.cmd)
                msg = apps.execute_app_plan(plan, dry_run=True)
                if c.check and plan.target != "app_store_search":          # программа может быть не установлена на этой машине
                    self.assertIn(c.check.casefold(), msg.casefold())

    def test_file_targets_are_found_in_the_user_home(self):
        cases = {
            ("fs_copy", "ml_log", ""): self.home / "Desktop" / "traning ml" / "ml_log.py",
            ("fs_trash", "traning ml", ""): self.home / "Desktop" / "traning ml",
            ("fs_move", "rubert-first-layer-intent-parser-lab", "Documents"): self.home / "Desktop" / "rubert-first-layer-intent-parser-lab",
            ("fs_show_in_files", "assistant.py", ""): self.home / "Desktop" / "assistant.py",
        }
        for (action, query, site), want in cases.items():
            with self.subTest(action=action, query=query):
                plan = S.make_system_plan(query=query, action=action, site=site, home=self.home)
                self.assertEqual(Path(plan.target_path), want)

    def test_exact_path_wins_and_is_limited_to_home(self):
        (self.home / "Downloads" / "отчёт.docx").write_bytes(b"x")          # то же имя, что и на рабочем столе
        plan = S.make_system_plan(query=str(self.home / "Downloads" / "отчёт.docx"), action="fs_copy", site=str(self.home / "Documents"),
                                  home=self.home)
        self.assertEqual(Path(plan.target_path), self.home / "Downloads" / "отчёт.docx")
        self.assertIn(str(self.home / "Documents"), S.execute_system_plan(plan, dry_run=True))
        plan = S.make_system_plan(query="/etc/hostname", action="fs_trash", home=self.home)
        self.assertNotEqual(Path(plan.target_path), Path("/etc/hostname"))

    def test_reminder_text_reaches_the_handler(self):
        S.make_system_plan(query="20 минут", action="reminder_set", text="позвонить маме", home=self.home)
        self.assertEqual(S.core._REMINDER_TEXT["text"], "позвонить маме")


class TestArgumentParsers(unittest.TestCase):
    def test_percent(self):
        for q, want in [("30", 30), ("20 процентов", 20), ("пятьдесят", 50), ("на максимум", 100), ("половину", 50), ("", None)]:
            with self.subTest(q=q):
                self.assertEqual(S.core._parse_percent(q), want)

    def test_seek_seconds(self):
        for q, want in [("20 секунд", 20), ("минуту", 60), ("полминуты", 30), ("две минуты", 120), ("", 10)]:
            with self.subTest(q=q):
                self.assertEqual(S.core._parse_seconds(q), want)

    def test_duration(self):
        for q, want in [("10 минут", 600), ("30 секунд", 30), ("полчаса", 1800), ("полтора часа", 5400), ("1 час 30 минут", 5400),
                        ("сорок секунд", 40), ("две с половиной минуты", 150), ("минуту", 60), ("час", 3600)]:
            with self.subTest(q=q):
                self.assertEqual(S.everyday._parse_duration_seconds(q), want)

    def test_clock(self):
        for q, hhmm in [("18:00", "18:00"), ("7 утра", "07:00"), ("9 вечера", "21:00"), ("семь тридцать", None), ("без пятнадцати семь", None),
                        ("вечером", "19:00"), ("перед сном", "22:00")]:
            with self.subTest(q=q):
                res = S.everyday._parse_clock_seconds(q)
                self.assertIsNotNone(res)
                self.assertGreater(res[0], 0)
                if hhmm:
                    self.assertTrue(res[1].endswith(hhmm), res)

    def test_resolution(self):
        for q, want in [("1920 на 1080", "1920x1080"), ("2560x1440", "2560x1440"), ("1920х1080", "1920x1080"), ("фулл хд", "1920x1080"),
                        ("4к", "3840x2160"), ("родное", "preferred"), ("максимальное", "max"), ("поменьше", "smaller"), ("", "")]:
            with self.subTest(q=q):
                self.assertEqual(S.display._parse_resolution(q), want)

    def test_gnome_accelerators(self):
        self.assertEqual(S.core._accel_to_keycodes("<Super>h"), (125, 35))
        self.assertEqual(S.core._accel_to_keycodes("<Control><Alt>Right"), (29, 56, 106))
        self.assertEqual(S.core._accel_to_keycodes("<Alt>F4"), (56, 62))
        self.assertIsNone(S.core._accel_to_keycodes("<Hyper>XF86Unknown"))

    def test_close_patterns_are_anchored(self):
        self.assertEqual(S.windows._pkill_pattern("code"), r"(^|/)code(\s|$)")
        self.assertEqual(S.windows._pkill_pattern(r"steamapps/common/dota\ 2\ beta"), r"steamapps/common/dota\ 2\ beta")


class TestCompleteness(unittest.TestCase):
    def test_every_action_has_a_handler_that_builds_a_plan(self):
        home = make_fake_home()
        try:
            for act in ml_spec.SYSTEM_ACTIONS:
                with self.subTest(action=act):
                    self.assertIn(act, S.ACTION_HANDLERS)
                    plan = S.make_system_plan(query="тест", action=act, home=home, text="текст")
                    self.assertIsNotNone(plan)
                    self.assertTrue(S.execute_system_plan(plan, dry_run=True).startswith("[dry-run]"))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_unknown_action_gives_no_plan(self):
        self.assertIsNone(S.make_system_plan(query="x", action="no_such_action"))


class TestAssistantDispatch(unittest.TestCase):
    def fake(self, case):
        def parse(phrase):
            return {"phrase": phrase, "route": case.route, "action": case.action, "site": case.site, "query": case.query,
                    "text": case.text, "lang": "none", "confidence": 0.99, "fallback": False, "reason": ""}
        return parse

    def test_system_and_app_routes_end_to_end(self):
        for c in [x for x in ALL if x.tag in ("audio", "display", "power", "app")][:30]:
            with self.subTest(phrase=c.phrase):
                res, plan, msg = assistant.dispatch_phrase(c.phrase, parser=self.fake(c), dry_run=True)
                self.assertIsNotNone(plan)
                self.assertTrue(msg.startswith("[dry-run]"), msg)

    def test_dangerous_first_layer_action_needs_yes(self):
        from unittest import mock

        def parse(phrase):
            return {"phrase": phrase, "route": "system", "action": "power_off", "site": "", "query": "", "text": "",
                    "confidence": 0.98, "fallback": False, "reason": ""}
        asked = []
        with mock.patch.object(S, "execute_system_plan", side_effect=AssertionError("не должно выполняться")):
            res, plan, msg = assistant.dispatch_phrase("выруби звук", parser=parse, dry_run=False,
                                                       confirm=lambda q: asked.append(q) or False)
        self.assertEqual(len(asked), 1)
        self.assertIn("выключ", asked[0].lower())
        self.assertIn("не выполняю", msg)
        self.assertIn("отменено", assistant.describe_trace(res))
        with mock.patch.object(S, "execute_system_plan", return_value="выключаю"):
            res, plan, msg = assistant.dispatch_phrase("выключи компьютер", parser=parse, dry_run=False, confirm=lambda q: True)
        self.assertEqual(msg, "выключаю")

    def test_safe_first_layer_action_does_not_ask(self):
        from unittest import mock

        def parse(phrase):
            return {"phrase": phrase, "route": "system", "action": "vol_down", "site": "", "query": "", "text": "",
                    "confidence": 0.98, "fallback": False, "reason": ""}
        with mock.patch.object(S, "execute_system_plan", return_value="тише"):
            res, plan, msg = assistant.dispatch_phrase("тише", parser=parse, dry_run=False,
                                                       confirm=lambda q: self.fail("спрашивать не нужно"))
        self.assertEqual(msg, "тише")

    def test_delete_without_object_refuses(self):
        plan = S.make_system_plan(query="", action="fs_trash", home=self.home if hasattr(self, "home") else None)
        self.assertEqual(plan.category, "refused")
        self.assertEqual(plan.commands, ())

    def test_unexpected_error_does_not_kill_the_assistant(self):
        from unittest import mock

        def parse(phrase):
            return {"phrase": phrase, "route": "system", "action": "sys_time", "site": "", "query": "", "text": "",
                    "confidence": 0.99, "fallback": False, "reason": ""}
        with mock.patch.object(assistant, "model_parse", parse), \
                mock.patch.object(S, "execute_system_plan", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(assistant, "_crash_log", return_value=""):
            self.assertIn("не так", assistant.run_phrase("который час"))

    def test_llm_route_executes_nothing(self):
        def parse(phrase):
            return {"phrase": phrase, "route": "llm", "action": "none", "site": "", "query": "", "text": "", "lang": "none",
                    "confidence": 0.6, "fallback": True, "reason": "not sure about the intent (0.60)"}
        class NoLLM:                                   # второй слой недоступен (нет ключа / сети)
            def handle(self, phrase, hint=""):
                from jarvis import llm as llm_agent
                raise llm_agent.LLMUnavailable("нет ключа")
        res, plan, msg = assistant.dispatch_phrase("что-то непонятное", parser=parse, dry_run=True, agent=NoLLM())
        self.assertIsNone(plan)
        self.assertIn("LLM", msg)


if __name__ == "__main__":
    unittest.main(verbosity=1)


class TestLaunchWithoutWaiting(unittest.TestCase):
    """Программа или браузер запускаются, и Джарвис сразу отвечает; не упала ли — смотрится в фоне."""

    def setUp(self):
        self.failed = []
        patches = [mock.patch.object(apps, "late_failure", self.failed.append),
                   mock.patch.object(apps, "LAUNCH_WATCH_SECONDS", 1.0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_answer_does_not_wait_for_the_program(self):
        t = time.perf_counter()
        self.assertIsNone(apps.spawn_watched([["sleep", "3"]], "долгую программу"))
        self.assertLess(time.perf_counter() - t, 0.3)

    def test_crash_right_after_start_is_reported(self):
        self.assertIsNone(apps.spawn_watched([["sh", "-c", "echo сломалось >&2; exit 3"]], "калькулятор"))
        deadline = time.time() + 3
        while not self.failed and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.failed), 1)
        self.assertIn("Не получилось открыть калькулятор", self.failed[0])
        self.assertIn("сломалось", self.failed[0])

    def test_backup_command_after_crash(self):
        """Firefox упал — запасной xdg-open; он сработал — жаловаться не на что."""
        marker = Path(tempfile.mkdtemp()) / "opened"
        self.assertIsNone(apps.spawn_watched([["false"], ["touch", str(marker)]], "браузер"))
        deadline = time.time() + 3
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertTrue(marker.exists())
        self.assertEqual(self.failed, [])

    def test_missing_program_is_an_error_right_away(self):
        self.assertIn("нет_такой_программы", apps.spawn_watched([["нет_такой_программы"]], "её") or "")

