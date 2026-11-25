"""Общие настройки тестов:
  • файловый буфер обмена ассистента — во временном файле, системный буфер (wl-copy) не трогается;
  • настоящий микрофон, звуковые сигналы и голос в тестах запрещены: тест, который до них дотянется, падает сразу,
    а не слушает комнату и не говорит в динамики; запись в облачное распознавание тоже не уходит."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PC_AGENT_CLIPBOARD_FILE", os.path.join(tempfile.mkdtemp(prefix="pc_agent_test_clip_"), "clipboard.json"))
ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "ml"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _no_real_audio(monkeypatch):
    from jarvis import listen as voice_input
    from jarvis import speak as voice_output

    def refuse(*args, **kwargs):
        raise RuntimeError("тест не должен открывать настоящий микрофон")
    monkeypatch.setattr(voice_input.Microphone, "__enter__", refuse)

    def no_cloud(*args, **kwargs):
        raise RuntimeError("тест не должен отправлять запись в облако")
    monkeypatch.setattr(voice_input, "cloud_transcribe", no_cloud)
    monkeypatch.setattr(voice_input, "beep", lambda *a, **k: None)
    monkeypatch.setattr(voice_output, "_play", lambda *a, **k: None)
    monkeypatch.setattr(voice_output.shutil, "which", lambda name, _w=voice_output.shutil.which: None if name == "spd-say" else _w(name))   # и без spd-say
