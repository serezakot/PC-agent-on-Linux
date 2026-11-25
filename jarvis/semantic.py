"""Поиск по смыслу среди того, что реально есть на этом компьютере — без словарей слов.

Каталог строится из самой системы: панели gnome-control-center и программы (их .desktop: название, «что это»,
описание, ключевые слова), игры из библиотеки Steam. Сказанное («настройки звука», «пароли», «видеоплеер»)
сравнивается с каталогом двумя способами:
    по смыслу   — многоязычная модель sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (470 МБ, ~1 мс на фразу):
                  русская фраза рядом с английским описанием программы;
    по звучанию — модель jarvis/names.py против названий и ключевых слов («микрофон» = Microphone, «блютуз» = Bluetooth).
Уверенность ниже порога — ответа нет: такую фразу понимает второй слой (Gemini знает, что «блокнот» — это редактор).
Векторы каталога кэшируются в ~/.cache/pc_agent/semantic.pt и пересчитываются, когда меняется набор программ.
"""
from __future__ import annotations

import configparser
import functools
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MIN_SCORE = 0.45                 # ниже — не уверен (проверено: верные ответы >= 0.48, неверные <= 0.35)
CACHE = Path.home() / ".cache" / "pc_agent" / "semantic.pt"
PANELS_DIR = Path("/usr/share/applications")


@dataclass(frozen=True)
class Entry:
    kind: str                    # "panel" | "app" | "game"
    key: str                     # id панели / desktop_id / appid Steam
    title: str
    text: str                    # что сравнивается по смыслу
    names: tuple[str, ...]       # названия — сравниваются по звучанию
    keywords: tuple[str, ...] = ()   # ключевые слова из .desktop — по звучанию, чуть слабее названий


def _desktop_entry(path: str | Path) -> dict:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path, encoding="utf-8")
        return dict(cp["Desktop Entry"])
    except Exception:                                   # noqa: BLE001
        return {}


def _entries() -> list[Entry]:
    from .apps import desktop_apps, steam_games          # noqa: PLC0415
    out: list[Entry] = []
    for p in sorted(PANELS_DIR.glob("gnome-*-panel.desktop")):
        e = _desktop_entry(p)
        if not e:
            continue
        panel = p.stem[len("gnome-"):-len("-panel")]
        words = tuple(k for k in e.get("keywords", "").split(";") if k)
        out.append(Entry("panel", panel, e.get("name", panel),
                         f"Settings: {e.get('name', '')}. {e.get('comment', '')}. {', '.join(words)}",
                         (e.get("name", ""), panel), words))
    for app in desktop_apps():
        if app.cmd and Path(app.cmd[0]).name == "gnome-control-center" and len(app.cmd) == 1:
            continue                                    # общий центр настроек: его панели уже в каталоге
        e = _desktop_entry(app.path)
        words = tuple(k for k in e.get("keywords", "").split(";") if k)
        out.append(Entry("app", app.desktop_id, app.title,
                         f"Application: {e.get('name', app.title)}. {e.get('genericname', '')}. {e.get('comment', '')}. "
                         f"{', '.join(words)}", app.names, words))
    for name, appid, _dir in steam_games():
        out.append(Entry("game", appid, name, f"Game: {name}", (name,)))
    return out


class _Index:
    def __init__(self):
        import torch                                     # noqa: PLC0415
        from transformers import AutoModel, AutoTokenizer   # noqa: PLC0415
        self.torch = torch
        try:
            self.tok = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
            self.model = AutoModel.from_pretrained(MODEL, local_files_only=True).eval()
        except OSError:                                  # ещё не скачана (нужна сеть один раз)
            self.tok = AutoTokenizer.from_pretrained(MODEL)
            self.model = AutoModel.from_pretrained(MODEL).eval()
        self.entries = _entries()
        texts = [e.text for e in self.entries]
        digest = hashlib.sha1(("\n".join(texts) + MODEL).encode()).hexdigest()
        cached = None
        try:
            cached = torch.load(CACHE, weights_only=True) if CACHE.is_file() else None
        except Exception:                                # noqa: BLE001
            cached = None
        if isinstance(cached, dict) and cached.get("digest") == digest:
            self.vectors = cached["vectors"]
        else:
            self.vectors = self.embed(texts)
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                torch.save({"digest": digest, "vectors": self.vectors}, CACHE)
            except OSError:
                pass

    def embed(self, texts: list[str]):
        torch = self.torch
        out = []
        for i in range(0, len(texts), 64):
            batch = self.tok(texts[i:i + 64], padding=True, truncation=True, max_length=96, return_tensors="pt")
            with torch.no_grad():
                hidden = self.model(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1)
            out.append(torch.nn.functional.normalize((hidden * mask).sum(1) / mask.sum(1), dim=-1))
        return torch.cat(out) if out else torch.zeros((0, 384))


_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def _index() -> _Index:
    return _Index()


def preload() -> None:
    with _LOCK:
        _index()


def search(phrase: str, kinds: tuple[str, ...] = ("panel", "app", "game"), min_score: float = MIN_SCORE
           ) -> tuple[Entry, float] | None:
    """Самое близкое по смыслу или звучанию из каталога этой машины; None — не уверен (пусть решит второй слой)."""
    phrase = " ".join((phrase or "").split())
    if not phrase:
        return None
    try:
        with _LOCK:
            idx = _index()
    except Exception:                                    # noqa: BLE001 — нет torch/transformers/модели: без смысла
        return None
    sims = (idx.embed([phrase]) @ idx.vectors.T)[0].tolist() if idx.entries else []
    best, best_score = None, 0.0
    for entry, sim in zip(idx.entries, sims):
        if entry.kind not in kinds:
            continue
        names = [w for n in entry.names if n for w in _leading_parts(n)]
        sound = max([_sounds_like(phrase, n) for n in names] +
                    [0.97 * _sounds_like(phrase, k) for k in entry.keywords if k] or [0.0])
        meaning = sim if entry.kind != "game" else 0.0          # у игры в каталоге только название: смысла нет
        score = max(meaning, sound if sound >= 0.86 else 0.0) + 0.05 * sim   # равенство решает смысл
        if score > best_score:
            best, best_score = entry, score
    return (best, best_score) if best is not None and best_score >= min_score else None


def _leading_parts(name: str) -> list[str]:
    """«Passwords and Keys» -> [«Passwords and Keys», «Passwords», «Passwords and»]: говорят обычно начало названия."""
    words = name.split()
    return [name] + [" ".join(words[:k]) for k in range(1, len(words))]


def _sounds_like(spoken: str, name: str) -> float:
    """Похожесть звучания целиком (не по началу: «видео» не равно «видеоплеер»)."""
    from .apps import _translit, name_similarity        # noqa: PLC0415
    a, b = _translit(spoken).replace(" ", ""), _translit(name).replace(" ", "")
    if not a or not b or min(len(a), len(b)) / max(len(a), len(b)) < 0.75:
        return 0.0
    return name_similarity(spoken, name)
