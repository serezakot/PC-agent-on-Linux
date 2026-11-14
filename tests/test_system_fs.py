"""Интеграционные тесты файловых операций system_control на настоящей Ubuntu (временная «домашняя папка»).

Проверяют весь путь (action, query, site) -> план -> выполнение: создание папки/файла, копирование в папку и в буфер,
перемещение, переименование, корзина, архив и распаковка, свойства. Запуск: python3 -m pytest tests/test_system_fs.py -q
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml"):
    sys.path.insert(0, str(p))

import system_control as S  # noqa: E402


class TestFileOps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc_agent_home_"))
        self.desktop = self.tmp / "Desktop"
        self.docs = self.tmp / "Documents"
        self.dl = self.tmp / "Downloads"
        for d in (self.desktop, self.docs, self.dl, self.tmp / "Pictures", self.tmp / "Music", self.tmp / "Videos"):
            d.mkdir()
        (self.desktop / "traning ml").mkdir()
        (self.desktop / "traning ml" / "ml_log.py").write_text("print(1)\n", encoding="utf-8")
        (self.desktop / "rubert-first-layer-intent-parser-lab").mkdir()
        (self.dl / "report_final.pdf").write_bytes(b"%PDF-1.4 test")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_plan(self, action, query="", site="", text=""):
        plan = S.make_system_plan(query=query, action=action, site=site, home=self.tmp, text=text)
        self.assertIsNotNone(plan, (action, query, site))
        return S.execute_system_plan(plan, dry_run=False)

    def test_mkdir_touch_on_desktop_and_inside_user_folder(self):
        msg = self.run_plan("fs_mkdir", "новый проект", "рабочем столе")
        self.assertTrue((self.desktop / "Новый проект").is_dir(), msg)
        msg = self.run_plan("fs_touch", "todo.txt", "папке traning ml")
        self.assertTrue((self.desktop / "traning ml" / "todo.txt").is_file(), msg)
        msg = self.run_plan("fs_touch", "заметки", "рабочем столе")
        self.assertTrue((self.desktop / "заметки.txt").is_file(), msg)

    def test_copy_move_rename_trash(self):
        msg = self.run_plan("fs_copy", "report_final.pdf", "рабочий стол")
        self.assertTrue((self.desktop / "report_final.pdf").is_file(), msg)
        msg = self.run_plan("fs_move", "rubert-first-layer-intent-parser-lab", "Documents")
        self.assertTrue((self.docs / "rubert-first-layer-intent-parser-lab").is_dir(), msg)
        self.assertFalse((self.desktop / "rubert-first-layer-intent-parser-lab").exists())
        msg = self.run_plan("fs_rename", "traning ml", "training")
        self.assertTrue((self.desktop / "training").is_dir(), msg)
        msg = self.run_plan("fs_trash", "report_final.pdf", "рабочем столе")
        self.assertFalse((self.desktop / "report_final.pdf").exists(), msg)

    def test_clipboard_copy_then_paste_into_folder(self):
        msg = self.run_plan("fs_copy", "ml_log.py")
        self.assertIn("буфер", msg)
        msg = self.run_plan("clip_paste", "", "Documents")
        self.assertTrue((self.docs / "ml_log.py").is_file(), msg)
        msg = self.run_plan("fs_cut", "report_final.pdf")
        msg = self.run_plan("clip_paste", "", "рабочий стол")
        self.assertTrue((self.desktop / "report_final.pdf").is_file(), msg)
        self.assertFalse((self.dl / "report_final.pdf").exists(), msg)

    def test_archive_unpack_properties(self):
        msg = self.run_plan("fs_archive", "traning ml")
        self.assertTrue((self.desktop / "traning ml.zip").is_file(), msg)
        msg = self.run_plan("fs_unpack", "traning ml.zip", "Documents")
        self.assertTrue((self.docs / "traning ml" / "traning ml" / "ml_log.py").is_file() or (self.docs / "traning ml" / "ml_log.py").is_file(), msg)
        msg = self.run_plan("fs_properties", "traning ml")
        self.assertIn("Папка", msg)
        plan = S.make_system_plan(query="ml_log.py", action="fs_show_in_files", home=self.tmp)
        self.assertEqual(Path(plan.target_path), self.desktop / "traning ml" / "ml_log.py")

    def test_dry_run_descriptions_for_new_actions(self):
        for action, q, site in [("timer_set", "10 минут", ""), ("reminder_set", "через 20 минут", ""), ("display_res_set", "1920 на 1080", ""),
                                ("sys_disk_free", "", ""), ("audio_output_set", "наушники", ""), ("pkg_install", "obsidian", ""),
                                ("wallpaper_set", "", ""), ("screen_off", "", ""), ("caffeine_on", "", ""), ("bt_connect", "наушники", "")]:
            plan = S.make_system_plan(query=q, action=action, site=site, home=self.tmp, text="позвонить маме")
            self.assertIsNotNone(plan, action)
            self.assertTrue(S.execute_system_plan(plan, dry_run=True).startswith("[dry-run]"), action)


if __name__ == "__main__":
    unittest.main(verbosity=1)
