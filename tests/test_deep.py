"""Глубокие тесты исполнителей БЕЗ модели и без сети: разбор аргументов, сопоставление имён, безопасность и настоящие
файловые операции с подвохами (одинаковые имена, перезапись, пути вне дома, мусорный ввод).

    python3 -m pytest tests/test_deep.py -q
"""
import datetime as dt
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

from jarvis import apps as A        # noqa: E402
import ml_spec                      # noqa: E402
from jarvis import system as S      # noqa: E402
from jarvis.system import core as C  # noqa: E402
from jarvis.system import windows as W  # noqa: E402
from jarvis.system.keyboard import _parse_key   # noqa: E402
from test_system import make_fake_home   # noqa: E402


# =============================================================================
# 1. Числа, длительности, время, разрешение, клавиши
# =============================================================================
class TestNumbers(unittest.TestCase):
    def test_words_to_digits(self):
        for text, want in [
            ("двадцать пять", "25"), ("сто двадцать три", "123"), ("тридцать", "30"), ("одиннадцать", "11"),
            ("девятнадцать", "19"), ("сорок два", "42"), ("девяносто девять", "99"), ("сто", "100"), ("двести пять", "205"),
            ("ноль", "0"), ("одну минуту", "1 минуту"), ("две минуты", "2 минуты"), ("полторы минуты", "1.5 минуты"),
            ("через двадцать пять минут", "через 25 минут"), ("пять десять", "5 10"), ("три четыре", "3 4"),
            ("громкость семьдесят процентов", "громкость 70 процентов"), ("без чисел", "без чисел"), ("", ""),
        ]:
            with self.subTest(text=text):
                self.assertEqual(C.words_to_digits(text), want)

    def test_percent(self):
        for q, want in [
            ("30", 30), ("0", 0), ("100", 100), ("150", 100), ("20 процентов", 20), ("двадцать пять", 25), ("тринадцать", 13),
            ("семьдесят пять процентов", 75), ("сто", 100), ("на максимум", 100), ("максимум", 100), ("полностью", 100),
            ("минимум", 5), ("половину", 50), ("на половину", 50), ("четверть", 25), ("треть", 33), ("на 40%", 40),
            ("до 60", 60), ("", None), ("громче", None), ("девяносто девять", 99),
        ]:
            with self.subTest(q=q):
                self.assertEqual(C._parse_percent(q), want)

    def test_duration(self):
        for q, want in [
            ("10 минут", 600), ("30 секунд", 30), ("полчаса", 1800), ("полтора часа", 5400), ("1 час 30 минут", 5400),
            ("сорок секунд", 40), ("две с половиной минуты", 150), ("минуту", 60), ("час", 3600), ("пять минут", 300),
            ("полторы минуты", 90), ("двадцать пять минут", 1500), ("три часа", 10800), ("2 часа 15 минут", 8100),
            ("90 секунд", 90), ("час двадцать", 4800), ("минуту тридцать", 90), ("15", 900), ("сорок пять минут", 2700),
            ("одну минуту", 60), ("полминуты", 30), ("10 мин", 600), ("5 сек", 5), ("", None), ("скоро", None),
        ]:
            with self.subTest(q=q):
                self.assertEqual(C.parse_duration(q), want)
                self.assertEqual(S.everyday._parse_duration_seconds(q), want)

    def test_seek_seconds(self):
        for q, want in [("20 секунд", 20), ("минуту", 60), ("полминуты", 30), ("две минуты", 120), ("три минуты", 180),
                        ("полторы минуты", 90), ("минуту тридцать", 90), ("сорок пять секунд", 45), ("15", 15), ("", 10)]:
            with self.subTest(q=q):
                self.assertEqual(C._parse_seconds(q), want)

    def test_delay_minutes(self):
        for q, want in [("час", 60), ("3 часа", 180), ("через двадцать пять минут", 25), ("полтора часа", 90),
                        ("2 часа 30 минут", 150), ("45 минут", 45), ("четыре часа", 240), ("30", 30), ("10 секунд", 1),
                        ("через полчаса", 30), ("", 10)]:
            with self.subTest(q=q):
                self.assertEqual(C._parse_delay_minutes(q), want)

    def test_clock(self):
        now = dt.datetime.now()
        for q, hhmm in [("18:00", "18:00"), ("7 утра", "07:00"), ("9 вечера", "21:00"), ("в полдень", "12:00"),
                        ("в полночь", "00:00"), ("вечером", "19:00"), ("перед сном", "22:00"), ("06:45", "06:45"), ("21:30", "21:30")]:
            with self.subTest(q=q):
                res = S.everyday._parse_clock_seconds(q)
                self.assertIsNotNone(res)
                secs, label = res
                self.assertTrue(0 < secs <= 24 * 3600 + 60, res)
                self.assertTrue(label.endswith(hhmm), res)
        self.assertTrue(S.everyday._parse_clock_seconds("6:45")[1].endswith(("06:45", "18:45")))   # «6:45» без уточнения: ближайшее
        for q in ("завтра в 9", "в субботу утром", "послезавтра в 10", "без пятнадцати семь", "семь тридцать"):
            with self.subTest(q=q):
                res = S.everyday._parse_clock_seconds(q)
                self.assertIsNotNone(res)
                self.assertGreater(res[0], 0)
        tomorrow = S.everyday._parse_clock_seconds("завтра в 9")[0]
        self.assertGreater(tomorrow, (dt.datetime.combine(now.date(), dt.time(23, 59)) - now).total_seconds())

    def test_resolution(self):
        for q, want in [("1920 на 1080", "1920x1080"), ("2560x1440", "2560x1440"), ("1920х1080", "1920x1080"),
                        ("1280 x 720", "1280x720"), ("фулл хд", "1920x1080"), ("full hd", "1920x1080"), ("4к", "3840x2160"),
                        ("2к", "2560x1440"), ("родное", "preferred"), ("максимальное", "max"), ("поменьше", "smaller"), ("", "")]:
            with self.subTest(q=q):
                self.assertEqual(S.display._parse_resolution(q), want)

    def test_keys(self):
        for n in range(1, 13):
            for spoken in (f"f{n}", f"F{n}", f"ф{n}", f"клавишу f{n}"):
                with self.subTest(spoken=spoken):
                    self.assertEqual(_parse_key(spoken)[1], f"F{n}")
        for spoken, xname in [("эф пять", "F5"), ("энтер", "Return"), ("enter", "Return"), ("эскейп", "Escape"), ("esc", "Escape"),
                              ("пробел", "space"), ("таб", "Tab"), ("tab", "Tab"), ("delete", "Delete"), ("бэкспейс", "BackSpace"),
                              ("стрелку вверх", "Up"), ("стрелка вниз", "Down"), ("влево", "Left"), ("home", "Home"), ("end", "End")]:
            with self.subTest(spoken=spoken):
                self.assertEqual(_parse_key(spoken)[1], xname)
        for spoken in ("f13", "ф0", "тест", "", "кнопку", "банан"):
            with self.subTest(spoken=spoken):
                self.assertIsNone(_parse_key(spoken))

    def test_unknown_key_is_never_pressed_as_space(self):
        plan = S.make_system_plan(query="банан", action="key_func")
        self.assertEqual(plan.commands, ())
        with self.assertRaises(S.SystemCommandError):
            plan.python_op()


# =============================================================================
# 2. Имена программ, понятий и стандартных папок (без словаря, но и без ложных совпадений)
# =============================================================================
class TestNames(unittest.TestCase):
    def test_folder_words_match_only_whole_words_with_endings(self):
        yes = [("загрузки", "загрузк"), ("в загрузках", "загрузк"), ("документы", "документ"), ("мои документы", "мои документ"),
               ("видео", "видео"), ("видосы", "видос"), ("фотографии", "фотограф"), ("домашнюю папку", "домашн папк"),
               ("на рабочем столе", "рабочем стол"), ("картинки", "картинк"), ("музыку", "музык")]
        no = [("видеоплеер", "видео"), ("фотошоп", "фото"), ("документооборот", "документ"), ("загрузчик", "загрузк"),
              ("музыкальный плеер", "музык"), ("мультфильмы", "фильм")]
        for text, key in yes:
            with self.subTest(text=text):
                self.assertTrue(A._word_starts(text, key))
        for text, key in no:
            with self.subTest(text=text):
                self.assertFalse(A._word_starts(text, key))

    def test_meaning_without_word_lists(self):
        """Без словарей: панели и программы этой машины по смыслу/звучанию; чего нет уверенно — второму слою."""
        from jarvis import semantic
        for name, kind, key in [("настройки звука", "panel", "sound"), ("микрофон", "panel", "sound"), ("принтер", "panel", "printers"),
                                ("калькулятор", "app", "org.gnome.Calculator.desktop"), ("pass words", "app", "org.gnome.seahorse.Application.desktop")]:
            with self.subTest(name=name):
                hit = semantic.search(name)
                if hit is None and kind == "app" and not any(a.desktop_id == key for a in A.desktop_apps()):
                    self.skipTest(f"нет {key} на этой машине")
                self.assertEqual((hit[0].kind, hit[0].key), (kind, key))
        for name in ("блокнот", "проводник", "кс"):                # знание мира, а не смысл слова: решает Gemini
            with self.subTest(name=name):
                self.assertIsNone(semantic.search(name))

    def test_short_names_do_not_match_abbreviations(self):
        self.assertLess(A.name_similarity("зум", "System Monitor"), 0.86)
        self.assertLess(A.name_similarity("кс", "Calculator"), 0.86)
        for spoken, real in [("дискорд", "Discord"), ("обсидиан", "Obsidian"), ("телеграм", "Telegram"), ("пайчарм", "PyCharm"),
                             ("вс код", "Visual Studio Code"), ("спотифай", "Spotify"), ("гимп", "GIMP")]:
            with self.subTest(spoken=spoken):
                self.assertGreaterEqual(A.name_similarity(spoken, real), 0.86)

    def test_standard_folders_in_a_fake_home(self):
        home = make_fake_home()
        try:
            for name, sub in [("загрузки", "Downloads"), ("документы", "Documents"), ("рабочий стол", "Desktop"),
                              ("картинки", "Pictures"), ("музыку", "Music"), ("видео", "Videos")]:
                with self.subTest(name=name):
                    plan = A.make_app_plan(name, home=home)
                    self.assertEqual(Path(plan.target), home / sub)
            plan = A.make_app_plan("домашнюю папку", home=home)
            self.assertEqual(Path(plan.target), home)
            plan = A.make_app_plan("корзину", home=home)
            self.assertEqual(plan.target, "trash:///")
        finally:
            shutil.rmtree(home, ignore_errors=True)


# =============================================================================
# 3. «Закрой X» не трогает систему и самого ассистента
# =============================================================================
class TestCloseSafety(unittest.TestCase):
    def test_protected_and_garbage_names_kill_nothing(self):
        for name in ("bash", "sh", "zsh", "python", "python3", "systemd", "gnome-shell", "gnome-session", "pipewire", "wireplumber",
                     "Xwayland", "dbus-daemon", "NetworkManager", "a", "x", ".*", "^", "|", "(", "*", "все", "всё"):
            with self.subTest(name=name):
                plan = S.make_system_plan(query=name, action="win_close_app")
                self.assertEqual(plan.commands, (), plan.commands)
                with self.assertRaises(S.SystemCommandError):
                    plan.python_op()

    def test_patterns_are_anchored(self):
        for name in ("firefox", "telegram", "discord"):
            for pat in W._safe_close_patterns(name):
                with self.subTest(name=name, pat=pat):
                    self.assertFalse(W._is_protected(pat))
                    self.assertTrue(W._pkill_pattern(pat).startswith(("(^|/)", "steamapps")), pat)

    def test_own_process_tree_is_never_killed(self):
        anc = W._own_ancestors()
        self.assertIn(os.getpid(), anc)
        self.assertIn(os.getppid(), anc)

    def test_browser_close_uses_anchored_patterns(self):
        plan = S.make_system_plan(query="", action="win_close_browser")
        for cmd in plan.commands:
            self.assertTrue(cmd[-1].startswith("(^|/)"), cmd)
        self.assertNotIn("|chrome|", " ".join(" ".join(c) for c in plan.commands))


# =============================================================================
# 4. Пути: ни одно файловое действие не выходит за домашнюю папку
# =============================================================================
class TestPathSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = make_fake_home()
        cls.outside = Path(tempfile.mkdtemp(prefix="pc_agent_outside_"))
        (cls.outside / "secret.txt").write_text("не трогать", encoding="utf-8")
        (cls.home / "Desktop" / "ссылка_наружу").symlink_to(cls.outside / "secret.txt")
        (cls.home / "Desktop" / "папка_наружу").symlink_to(cls.outside)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.outside, ignore_errors=True)

    def inside_home(self, path: str) -> bool:
        p = Path(path).resolve()
        h = self.home.resolve()
        return p == h or h in p.parents

    def test_source_outside_home_is_never_used(self):
        bad = [str(self.outside / "secret.txt"), str(self.outside), "/etc/passwd", "/etc", "/", "/usr/bin/bash",
               f"{self.home}/../../etc/passwd", "~/../../etc/passwd", "../../../etc/passwd",
               str(self.home / "Desktop" / "ссылка_наружу"), str(self.home / "Desktop" / "папка_наружу")]
        for action in ("fs_copy", "fs_move", "fs_rename", "fs_trash", "fs_archive", "fs_unpack", "fs_chmod_x", "fs_properties",
                       "fs_show_in_files", "fs_cut", "wallpaper_set"):
            for q in bad:
                with self.subTest(action=action, q=q):
                    plan = S.make_system_plan(query=q, action=action, site="", home=self.home)
                    if plan.target_path:
                        self.assertTrue(self.inside_home(plan.target_path), plan.target_path)
                    self.assertTrue(S.execute_system_plan(plan, dry_run=True).startswith("[dry-run]"))

    def test_destination_outside_home_is_never_used(self):
        for action, q in (("fs_copy", "main.py"), ("fs_move", "main.py"), ("fs_unpack", "lab.zip"), ("fs_mkdir", "новая"),
                          ("fs_touch", "файл.txt"), ("clip_paste", "")):
            for site in ("/etc", str(self.outside), "/", "~/../..", str(self.home / "Desktop" / "папка_наружу")):
                with self.subTest(action=action, site=site):
                    plan = S.make_system_plan(query=q, action=action, site=site, home=self.home)
                    msg = S.execute_system_plan(plan, dry_run=True)
                    for path in (plan.dest_path, plan.target_path):
                        if path and action in ("fs_mkdir", "fs_touch"):
                            self.assertTrue(self.inside_home(path), (path, msg))
                        elif path and action != "fs_copy" and path != plan.target_path:
                            self.assertTrue(self.inside_home(path), (path, msg))
                    self.assertNotIn(" /etc", msg)
                    self.assertNotIn(str(self.outside), msg)

    def test_exact_path_inside_home_wins(self):
        (self.home / "Downloads" / "main.py").write_bytes(b"y")
        plan = S.make_system_plan(query=str(self.home / "Downloads" / "main.py"), action="fs_trash", home=self.home)
        self.assertEqual(Path(plan.target_path), self.home / "Downloads" / "main.py")
        plan = S.make_system_plan(query="~/../" + self.home.name + "/Downloads/main.py", action="fs_trash", home=self.home)
        self.assertTrue(self.inside_home(plan.target_path))


# =============================================================================
# 5. Устойчивость: любое действие с любым мусором не падает и не подставляет команды в оболочку
# =============================================================================
INJECTIONS = ["", " ", "x" * 500, "'; rm -rf ~ #", "$(reboot)", "`id`", "a && poweroff", "ёжик 🦔", "\n\t", "../..", "%s%n",
              "' OR 1=1 --", "-rf", "--help", "\\", "\"", "😀" * 20, "١٢٣", "NaN", "-1", "99999999999999999999"]


class TestRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = make_fake_home()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_every_action_survives_garbage(self):
        for action in ml_spec.SYSTEM_ACTIONS:
            for junk in INJECTIONS:
                with self.subTest(action=action, junk=junk[:20]):
                    plan = S.make_system_plan(query=junk, action=action, site=junk[:40], text=junk, home=self.home)
                    self.assertIsNotNone(plan)
                    msg = S.execute_system_plan(plan, dry_run=True)
                    self.assertTrue(msg.startswith("[dry-run]"))
                    for cmd in plan.commands:
                        self.assertTrue(all(isinstance(a, str) for a in cmd), cmd)
                        if len(cmd) >= 3 and cmd[0] in ("bash", "sh") and cmd[1] == "-c":
                            for bad in ("$(reboot)", "`id`", "rm -rf ~", "&& poweroff"):
                                self.assertNotIn(bad, cmd[2], (action, cmd))

    def test_percent_style_actions_stay_in_range(self):
        for action in ("vol_set", "mic_set", "bright_set"):
            for q in ("-1", "99999999999999999999", "150", "сто пятьдесят"):
                with self.subTest(action=action, q=q):
                    msg = S.execute_system_plan(S.make_system_plan(query=q, action=action, home=self.home), dry_run=True)
                    for n in re.findall(r"(\d+)%", msg):
                        self.assertLessEqual(int(n), 100, msg)

    def test_delays_are_sane(self):
        for action in ("power_off", "power_reboot", "power_sleep"):
            for q, mins in (("через час", 60), ("через 3 часа", 180), ("через двадцать пять минут", 25)):
                with self.subTest(action=action, q=q):
                    msg = S.execute_system_plan(S.make_system_plan(query=q, action=action, home=self.home), dry_run=True)
                    self.assertIn(str(mins), msg)
                    self.assertNotIn("systemctl suspend\n", msg)
        msg = S.execute_system_plan(S.make_system_plan(query="", action="power_sleep", home=self.home), dry_run=True)
        self.assertIn("systemctl suspend", msg)

    def test_reminder_text_with_shell_characters_is_stored_verbatim(self):
        text = "купить хлеб; $(reboot) `id` 'кавычки'"
        S.make_system_plan(query="10 минут", action="reminder_set", text=text, home=self.home)
        self.assertIn("$(reboot) `id`", S.core._REMINDER_TEXT["text"])      # текст хранится как есть, в оболочку не попадает


# =============================================================================
# 6. Настоящие файловые операции с подвохами (временная домашняя папка)
# =============================================================================
class TestRealFilesTricky(unittest.TestCase):
    def setUp(self):
        self.home = make_fake_home()
        self.desk, self.docs, self.dl = self.home / "Desktop", self.home / "Documents", self.home / "Downloads"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_plan(self, action, query="", site="", text=""):
        plan = S.make_system_plan(query=query, action=action, site=site, home=self.home, text=text)
        return plan, S.execute_system_plan(plan, dry_run=False)

    def test_copy_twice_keeps_both_and_never_overwrites(self):
        (self.docs / "main.py").write_text("ДРУГОЙ", encoding="utf-8")
        _, msg = self.run_plan("fs_copy", "main.py", "документы")
        self.assertEqual((self.docs / "main.py").read_text(encoding="utf-8"), "ДРУГОЙ", msg)
        copies = sorted(p.name for p in self.docs.iterdir() if p.name.startswith("main"))
        self.assertGreaterEqual(len(copies), 2, copies)

    def test_rename_onto_existing_name_does_not_overwrite(self):
        (self.desk / "todo.txt").write_text("СТАРОЕ", encoding="utf-8")
        try:
            self.run_plan("fs_rename", "заметки.txt", "todo.txt")
        except S.SystemCommandError:
            pass
        self.assertEqual((self.desk / "todo.txt").read_text(encoding="utf-8"), "СТАРОЕ")

    def test_move_into_folder_with_same_name_does_not_overwrite(self):
        (self.docs / "notes.md").write_text("В ДОКУМЕНТАХ", encoding="utf-8")
        try:
            self.run_plan("fs_move", "notes.md", "документы")
        except S.SystemCommandError:
            pass
        self.assertEqual((self.docs / "notes.md").read_text(encoding="utf-8"), "В ДОКУМЕНТАХ")

    def test_touch_existing_file_keeps_content(self):
        (self.desk / "readme.md").write_text("ВАЖНОЕ", encoding="utf-8")
        try:
            self.run_plan("fs_touch", "readme.md", "рабочем столе")
        except S.SystemCommandError:
            pass
        self.assertEqual((self.desk / "readme.md").read_text(encoding="utf-8"), "ВАЖНОЕ")

    def test_mkdir_existing_folder_keeps_its_files(self):
        (self.desk / "education" / "конспект.txt").write_text("x", encoding="utf-8")
        try:
            self.run_plan("fs_mkdir", "education", "рабочем столе")
        except S.SystemCommandError:
            pass
        self.assertTrue((self.desk / "education" / "конспект.txt").is_file())

    def test_trash_of_missing_file_touches_nothing_else(self):
        before = sorted(p.name for p in self.desk.iterdir())
        with self.assertRaises(S.SystemCommandError):
            self.run_plan("fs_trash", "несуществующий_файл_42.txt")
        self.assertEqual(sorted(p.name for p in self.desk.iterdir()), before)

    def test_copy_of_missing_file_is_an_error(self):
        with self.assertRaises(S.SystemCommandError):
            self.run_plan("fs_copy", "несуществующий_файл_42.txt", "документы")

    def test_names_with_spaces_cyrillic_and_quotes(self):
        weird = self.desk / "Отчёт «итоги» 2026 (финал).txt"
        weird.write_text("данные", encoding="utf-8")
        _, msg = self.run_plan("fs_copy", "Отчёт «итоги» 2026 (финал).txt", "документы")
        self.assertTrue((self.docs / weird.name).is_file(), msg)
        _, msg = self.run_plan("fs_rename", str(weird), "итоги.txt")
        self.assertTrue((self.desk / "итоги.txt").is_file(), msg)

    def test_archive_single_file_and_unpack_back(self):
        (self.desk / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        _, msg = self.run_plan("fs_archive", "data.csv")
        zips = list(self.desk.glob("data*.zip"))
        self.assertTrue(zips, msg)
        with zipfile.ZipFile(zips[0]) as z:
            self.assertTrue(any(n.endswith("data.csv") for n in z.namelist()))
        (self.desk / "data.csv").unlink()
        _, msg = self.run_plan("fs_unpack", zips[0].name, "документы")
        self.assertTrue(list(self.docs.rglob("data.csv")), msg)

    def test_chmod_makes_executable(self):
        f = self.desk / "setup.sh"
        f.chmod(0o644)
        self.run_plan("fs_chmod_x", "setup.sh")
        self.assertTrue(os.access(f, os.X_OK))

    def test_deeply_nested_file_is_found(self):
        deep = self.home / "Documents" / "a" / "b" / "c" / "глубоко_спрятан.txt"
        deep.parent.mkdir(parents=True)
        deep.write_text("x", encoding="utf-8")
        plan = S.make_system_plan(query="глубоко_спрятан.txt", action="fs_show_in_files", home=self.home)
        self.assertEqual(Path(plan.target_path), deep)


if __name__ == "__main__":
    unittest.main(verbosity=1)
