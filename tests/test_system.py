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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

import app_launcher                 # noqa: E402
import assistant                    # noqa: E402
import ml_spec                      # noqa: E402
import system_control as S          # noqa: E402
from cases_system import ALL, ALL_SYSTEM, APPS   # noqa: E402


def make_fake_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="pc_agent_sys_"))
    for d in ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos"):
        (home / d).mkdir()
    for d in ("education", "traning ml", "ml_model", "проекты", "rubert-first-layer-intent-parser-lab"):
        (home / "Desktop" / d).mkdir()
    for f in ("traning ml/ml_log.py", "main.py", "report_final.pdf", "отчёт.docx", "заметки.txt", "setup.sh", "старый_бэкап.zip",
              "lab.zip", "temp_old.log", "море.jpg", "assistant.py"):
        (home / "Desktop" / f).write_bytes(b"x")
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
        actions = {c.action for c in ALL_SYSTEM}
        self.assertGreaterEqual(len(ALL_SYSTEM), 120)
        self.assertGreaterEqual(len(actions), 90)
        self.assertGreaterEqual(len(APPS), 15)


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
                plan = app_launcher.make_app_plan(c.site, c.query)
                self.assertIsNotNone(plan)
                self.assertTrue(plan.cmd)
                msg = app_launcher.execute_app_plan(plan, dry_run=True)
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

    def test_reminder_text_reaches_the_handler(self):
        S.make_system_plan(query="20 минут", action="reminder_set", text="позвонить маме", home=self.home)
        self.assertEqual(S._REMINDER_TEXT["text"], "позвонить маме")


class TestArgumentParsers(unittest.TestCase):
    def test_percent(self):
        for q, want in [("30", 30), ("20 процентов", 20), ("пятьдесят", 50), ("на максимум", 100), ("половину", 50), ("", None)]:
            with self.subTest(q=q):
                self.assertEqual(S._parse_percent(q), want)

    def test_seek_seconds(self):
        for q, want in [("20 секунд", 20), ("минуту", 60), ("полминуты", 30), ("две минуты", 120), ("", 10)]:
            with self.subTest(q=q):
                self.assertEqual(S._parse_seconds(q), want)

    def test_duration(self):
        for q, want in [("10 минут", 600), ("30 секунд", 30), ("полчаса", 1800), ("полтора часа", 5400), ("1 час 30 минут", 5400),
                        ("сорок секунд", 40), ("две с половиной минуты", 150), ("минуту", 60), ("час", 3600)]:
            with self.subTest(q=q):
                self.assertEqual(S._parse_duration_seconds(q), want)

    def test_clock(self):
        for q, hhmm in [("18:00", "18:00"), ("7 утра", "07:00"), ("9 вечера", "21:00"), ("семь тридцать", None), ("без пятнадцати семь", None),
                        ("вечером", "19:00"), ("перед сном", "22:00")]:
            with self.subTest(q=q):
                res = S._parse_clock_seconds(q)
                self.assertIsNotNone(res)
                self.assertGreater(res[0], 0)
                if hhmm:
                    self.assertTrue(res[1].endswith(hhmm), res)

    def test_resolution(self):
        for q, want in [("1920 на 1080", "1920x1080"), ("2560x1440", "2560x1440"), ("1920х1080", "1920x1080"), ("фулл хд", "1920x1080"),
                        ("4к", "3840x2160"), ("родное", "preferred"), ("максимальное", "max"), ("поменьше", "smaller"), ("", "")]:
            with self.subTest(q=q):
                self.assertEqual(S._parse_resolution(q), want)

    def test_gnome_accelerators(self):
        self.assertEqual(S._accel_to_keycodes("<Super>h"), (125, 35))
        self.assertEqual(S._accel_to_keycodes("<Control><Alt>Right"), (29, 56, 106))
        self.assertEqual(S._accel_to_keycodes("<Alt>F4"), (56, 62))
        self.assertIsNone(S._accel_to_keycodes("<Hyper>XF86Unknown"))

    def test_close_patterns_are_anchored(self):
        self.assertEqual(S._pkill_pattern("code"), r"(^|/)code(\s|$)")
        self.assertEqual(S._pkill_pattern(r"steamapps/common/dota\ 2\ beta"), r"steamapps/common/dota\ 2\ beta")


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

    def test_llm_route_executes_nothing(self):
        def parse(phrase):
            return {"phrase": phrase, "route": "llm", "action": "none", "site": "", "query": "", "text": "", "lang": "none",
                    "confidence": 0.6, "fallback": True, "reason": "not sure about the intent (0.60)"}
        res, plan, msg = assistant.dispatch_phrase("что-то непонятное", parser=parse, dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("LLM", msg)


if __name__ == "__main__":
    unittest.main(verbosity=1)
