"""Голосовой ввод без микрофона и модели: русские числительные, произнесённые аббревиатуры и расширения, время словами,
имена файлов «на слух», перечисление программ, безопасные читающие команды второго слоя и защита скрытых файлов.

    python3 -m pytest tests/test_spoken.py -q
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml", Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))

from jarvis import apps as A        # noqa: E402
from jarvis import sys_query as Q   # noqa: E402
from jarvis import system as S      # noqa: E402
from jarvis.spoken import normalize_spoken, words_to_digits   # noqa: E402
from jarvis.system import files as F  # noqa: E402
from jarvis.system.core import xdg_user_dirs   # noqa: E402
from jarvis.system.display import _parse_resolution   # noqa: E402
from jarvis.system.everyday import _parse_clock_seconds, _spoken_clock   # noqa: E402
from jarvis.system.windows import _app_names   # noqa: E402
from test_system import make_fake_home   # noqa: E402


class TestNumerals(unittest.TestCase):
    def test_all_cases_and_thousands(self):
        for text, want in [
            ("убавь до тридцати процентов", "убавь до 30 процентов"), ("двадцати пяти", "25"), ("сорока", "40"),
            ("в течение двух часов", "в течение 2 часов"), ("пятью минутами", "5 минутами"), ("полутора", "1.5"),
            ("тысяча девятьсот двадцать на тысячу восемьдесят", "1920 на 1080"),
            ("две тысячи пятьсот шестьдесят на тысяча четыреста сорок", "2560 на 1440"), ("тысяча пять", "1005"),
            ("на пять тысяч", "на 5000"), ("девятьсот девяносто девять", "999"), ("семьсот двадцать", "720"),
            ("восемнадцать ноль ноль", "18 0 0"), ("двадцать ноль ноль", "20 0 0"), ("шесть сорок пять", "6 45"),
            ("покажи фото семью", "покажи фото семью"),                       # «семью» — это «семья», не число
        ]:
            with self.subTest(text=text):
                self.assertEqual(words_to_digits(text), want)

    def test_resolution_spoken(self):
        for q, want in [("тысяча девятьсот двадцать на тысячу восемьдесят", "1920x1080"), ("четыре к", "3840x2160"),
                        ("тысяча двести восемьдесят на семьсот двадцать", "1280x720"), ("1920 на 1080", "1920x1080")]:
            with self.subTest(q=q):
                self.assertEqual(_parse_resolution(q), want)


class TestNormalizeSpoken(unittest.TestCase):
    def test_letters_extensions_and_untouched_text(self):
        for text, want in [
            ("подключи вэ пэ эн", "подключи vpn"), ("какой у меня ай пи", "какой у меня ip"),
            ("звук через эйч ди эм ай", "звук через hdmi"), ("удали программу вэ эл цэ", "удали программу vlc"),
            ("нажми эф пять", "нажми f5"), ("убери видео эм пэ четыре в загрузки", "убери видео mp4 в загрузки"),
            ("удали мейн точка пай", "удали мейн.py"), ("распакуй бэкап точка тар точка гэ зэ", "распакуй бэкап.tar.gz"),
            ("поставь обои закат точка джейпег", "поставь обои закат.jpg"), ("скопируй папку трейнинг эм эль", "скопируй папку трейнинг ml"),
            # обычная речь не меняется: одна «буква» — не аббревиатура, «точка доступа» — не расширение, числа остаются словами
            ("эм открой браузер", "эм открой браузер"), ("включи точку доступа", "включи точку доступа"),
            ("переведи как сказать это одним словом", "переведи как сказать это одним словом"),
            ("ускорь до полутора", "ускорь до полутора"), ("Поставь громкость на 40%.", "поставь громкость на 40%"),
            ("Открой файл data {нижнее подчёркивание} struct.", "открой файл data_struct"),    # так пишет Chirp
        ]:
            with self.subTest(text=text):
                self.assertEqual(normalize_spoken(text), want)

    def test_digits_mode(self):
        self.assertEqual(normalize_spoken("напомни в восемнадцать ноль ноль", digits=True), "напомни в 18:00")
        self.assertEqual(normalize_spoken("будильник на шесть ноль пять", digits=True), "будильник на 6:05")


class TestSpokenClock(unittest.TestCase):
    def test_clock_words(self):
        for q, want in [("в восемнадцать ноль ноль", "18:00"), ("в половине седьмого", "6:30"), ("полседьмого вечера", "6:30 вечера"),
                        ("без четверти восемь", "7:45"), ("без пятнадцати девять вечера", "8:45 вечера"),
                        ("четверть восьмого", "7:15"), ("в восемь часов тридцать минут", "в 8:30"), ("шесть ноль пять", "6:05"),
                        ("в двадцать один тридцать", "в 21 30"), ("в час дня", "в 1 дня"), ("в половине первого", "в 12:30")]:
            with self.subTest(q=q):
                self.assertIn(want, _spoken_clock(q))

    def test_clock_targets(self):
        for q, hm in [("в восемнадцать ноль ноль", "18:00"), ("в девять тридцать вечера", "21:30"), ("в двадцать один тридцать", "21:30"),
                      ("завтра в семь утра", "07:00"), ("в час дня", "13:00"), ("без четверти восемь вечера", "19:45")]:
            with self.subTest(q=q):
                secs, label = _parse_clock_seconds(q)
                self.assertTrue(label.endswith(hm), label)
                self.assertGreater(secs, 0)


class TestSpokenNames(unittest.TestCase):
    def setUp(self):
        self.home = make_fake_home()
        self.dirs = xdg_user_dirs(self.home)

    def test_similarity(self):
        for a, b in [("эдьюкейшн", "education"), ("трейнинг ml", "traning ml"), ("репорт файнал", "report_final"),
                     ("дота два", "Dota 2"), ("пайчарм", "PyCharm"), ("бэкап.tar", "backup.tar"),
                     ("pass words", "Passwords and Keys"), ("sound cloud", "SoundCloud"), ("visual studio code", "Visual Studio Code"),
                     # настоящие искажения GigaAM на записях с русским акцентом — ручной алгоритм их не узнавал
                     ("каплец", "caplets"), ("dom loots", "Downloads"), ("имэджин дрэгонс", "Imagine Dragons")]:
            with self.subTest(a=a):
                self.assertGreaterEqual(A.name_similarity(a, b), A.FUZZY_NAME_MIN)

    def test_short_words_are_not_compared_by_sound(self):
        """«кс», «зум»: в 2–3 буквах мало звука — иначе «кс» = cases_system («c s»), «зум» = zoom из описания панели."""
        for a, b in [("кс", "cases_system"), ("зум", "Zoom"), ("кс", "Calculator")]:
            with self.subTest(a=a, b=b):
                self.assertLess(A.name_similarity(a, b), A.FUZZY_NAME_MIN)

    def test_files_found_by_sound(self):
        d = self.home / "Desktop"
        for spoken, want_dir, want in [("эдьюкейшн", True, d / "education"), ("трейнинг ml", True, d / "traning ml"),
                                       ("мейн.py", None, d / "main.py"), ("репорт файнал.pdf", None, d / "report_final.pdf"),
                                       ("ран.sh", None, d / "run.sh"), ("бэкап.tar.gz", None, d / "backup.tar.gz"),
                                       ("ноутс.md", None, d / "notes.md"), ("ml лог", None, d / "traning ml" / "ml_log.py")]:
            with self.subTest(spoken=spoken):
                self.assertEqual(F._find_target_item(spoken, self.home, self.dirs, want_dir=want_dir), want)

    def test_short_part_of_phrase_is_not_a_match(self):
        """«трейнинг ml» не должно находить чужую папку «ml» (раньше так находилась папка ml проекта)."""
        (self.home / "Desktop" / "ml").mkdir()
        self.assertEqual(F._find_target_item("трейнинг ml", self.home, self.dirs, want_dir=True), self.home / "Desktop" / "traning ml")

    def test_site_override_stays_exact(self):
        (self.home / "Videos" / "YouTube").mkdir()
        self.assertIsNone(A.find_local_item("ютуб", self.home))                # «открой ютуб» — сайт, а не папка
        self.assertIsNotNone(A.find_local_item("ютуб", self.home, fuzzy=True))

    def test_terminal_in_spoken_folder(self):
        plan = A.make_app_plan("терминал", "в папке эдьюкейшн", home=self.home)
        self.assertIn(f"--working-directory={self.home / 'Desktop' / 'education'}", plan.cmd)

    def test_exact_path_opens_that_file(self):
        f = self.home / "Desktop" / "notes.md"
        plan = A.make_app_plan(str(f), home=self.home)
        self.assertEqual((plan.kind, plan.target), ("file", str(f)))
        with self.assertRaises(A.AppLaunchError):
            A.make_app_plan(str(self.home / "нет_такого.txt"), home=self.home)

    def test_hidden_and_secret_files_are_not_targets(self):
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_rsa").write_text("secret")
        for name in ("~/.ssh/id_rsa", str(self.home / ".ssh" / "id_rsa")):
            with self.subTest(name=name):
                found = F._find_target_item(name.replace("~", str(self.home)), self.home, self.dirs)
                self.assertNotIn(".ssh", str(found))


class TestCloseSeveral(unittest.TestCase):
    def test_enumeration_without_commas(self):
        names = _app_names("дискорд телеграм и стим")
        if names == ["дискорд телеграм и стим"]:
            self.skipTest("на этой машине нет этих программ")
        self.assertEqual(names, ["дискорд", "телеграм", "стим"])

    def test_single_multiword_name_is_not_split(self):
        self.assertEqual(_app_names("visual studio code"), ["visual studio code"])

    def test_protected_part_is_skipped(self):
        plan = S.make_system_plan(query="pipewire и телеграм", action="win_close_app")
        self.assertFalse(any("pipewire" in " ".join(c) for c in plan.commands))


class TestSysQuery(unittest.TestCase):
    def test_allowed(self):
        for c in ["uptime -p", "df -h /", "du -sh ~/Downloads", "free -h", "ps -eo pid,comm,%cpu --sort=-%cpu", "top -b -n 1",
                  "lsblk -f", "ip -br addr", "nmcli -t -f SSID,SIGNAL device wifi list", "nmcli radio wifi",
                  "systemctl --user status pipewire", "systemctl list-units --state=failed", "upower -d",
                  "pactl list short source-outputs", "wpctl status", "snap list", "apt list --upgradable",
                  "gsettings get org.gnome.desktop.interface color-scheme", "xdg-settings get default-web-browser",
                  "ls -la ~/.config/autostart", "python3 --version", "ping -c 2 8.8.8.8", "date +%H:%M", "systemd-analyze blame"]:
            with self.subTest(c=c):
                Q.check_query(c)

    def test_refused(self):
        for c in ["nmcli device wifi connect Home", "nmcli radio wifi off", "nmcli connection show --show-secrets Home",
                  "systemctl stop pipewire", "systemctl --user restart pipewire", "ps aux", "ps -eo pid,args", "rm -rf ~",
                  "sudo apt clean", "apt install vlc", "ls ~/.ssh", "du -sh /", "cat /etc/passwd", "uptime; rm -rf ~",
                  "ls $(whoami)", "gsettings set a b c", "pactl set-sink-volume 0 10%", "env", "journalctl -n 20",
                  "top -b -n 1 -c", "ping 8.8.8.8", "ip link set wlp1s0 down", "python3 -c 'print(1)'", "/bin/rm x",
                  "pgrep -a firefox", "ls ~/.config/pc_agent", "wc -l /etc/shadow", "date -s 10:00", "hostname evil",
                  "find ~ -delete", "snap remove x", "dpkg -i x.deb", "xdg-settings set default-web-browser x.desktop",
                  "ls ~/*", "", "   "]:
            with self.subTest(c=c):
                with self.assertRaises(Q.QueryRefused):
                    Q.check_query(c)

    def test_project_files_are_readable_but_keys_are_not(self):
        from jarvis import llm as llm_agent
        self.assertIn("Джарвис", Q.read_text_file(str(ROOT / "README.md"), llm_agent._readable))
        with self.assertRaises(Q.QueryRefused):
            Q.read_text_file(str(Path.home() / ".config" / "pc_agent" / "vertex-sa.json"), llm_agent._readable)


class TestClipboardRace(unittest.TestCase):
    """«Вырежи А и вставь в Б» подряд: wl-copy ещё не захватил буфер, wl-paste отдаёт старое — вставить надо А."""
    def _get(self, state_age: float):
        import json
        import tempfile
        import time
        from unittest import mock
        state = Path(tempfile.mkdtemp()) / "clip.json"
        state.write_text(json.dumps({"op": "cut", "paths": ["/home/u/A.txt"], "t": time.time() - state_age}), encoding="utf-8")
        old = mock.Mock(returncode=0, stdout="copy\nfile:///home/u/OLD.txt\n")
        with mock.patch.object(F, "CLIPBOARD_STATE_FILE", state), mock.patch.object(F, "SYSTEM_CLIPBOARD", True), \
                mock.patch.object(F.shutil, "which", side_effect=lambda c: "/usr/bin/" + c if c == "wl-paste" else None), \
                mock.patch.object(F.subprocess, "run", return_value=old):
            return F._clipboard_files_get()

    def test_fresh_own_copy_wins(self):
        self.assertEqual(self._get(0.2), ("cut", [Path("/home/u/A.txt")]))

    def test_later_copy_in_file_manager_wins(self):
        self.assertEqual(self._get(60.0), ("copy", [Path("/home/u/OLD.txt")]))


if __name__ == "__main__":
    unittest.main()
