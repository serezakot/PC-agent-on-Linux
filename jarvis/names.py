"""Похожи ли два названия на слух: «каплец» = caplets, «дом лутс» = Downloads, «паспортс» = Passwords.

Маленькая обучаемая модель по буквам (вместо ручной транслитерации и «скелета согласных»): каждое название
превращается в вектор, похожие на слух — в близкие векторы. Учится в лаборатории («traning ml», names_*.py) на парах
«как GigaAM записал сказанное русским голосом» -> «как пишется по-настоящему». Сравнивает с тем, что лежит на
компьютере сейчас, поэтому новые файлы и программы узнаются без переобучения. Веса — ml/name_model (~2 МБ).
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

ALPHABET = " 0123456789abcdefghijklmnopqrstuvwxyzабвгдежзийклмнопрстуфхцчшщъыьэюя"
MAX_CHARS = 48
MODEL_DIR = Path(__file__).resolve().parent.parent / "ml" / "name_model"
_INDEX = {ch: i + 1 for i, ch in enumerate(ALPHABET)}        # 0 — пустое место


def normalize(text: str) -> str:
    """Как название сравнивается: строчные, ё = е, «_ - .» = пробел, прочие знаки убраны."""
    text = re.sub(r"[_\-.]+", " ", (text or "").lower().replace("ё", "е"))
    return " ".join("".join(ch if ch in _INDEX else " " for ch in text).split())[:MAX_CHARS]


def build_encoder():
    """Буквы -> 2 слоя двунаправленного GRU -> среднее и максимум по буквам -> вектор длины 1."""
    import torch
    from torch import nn

    class NameEncoder(nn.Module):
        def __init__(self, emb: int = 64, hidden: int = 128, out: int = 128):
            super().__init__()
            self.emb = nn.Embedding(len(ALPHABET) + 1, emb, padding_idx=0)
            self.rnn = nn.GRU(emb, hidden, num_layers=2, batch_first=True, bidirectional=True, dropout=0.1)
            self.proj = nn.Linear(4 * hidden, out)

        def forward(self, ids: torch.Tensor) -> torch.Tensor:
            # пустые места после короткого названия GRU не проходит: вектор не зависит от того, с чем оно в пачке
            mask = (ids != 0).unsqueeze(-1)
            packed = nn.utils.rnn.pack_padded_sequence(self.emb(ids), mask.sum((1, 2)).clamp(min=1).cpu(),
                                                       batch_first=True, enforce_sorted=False)
            states, _ = nn.utils.rnn.pad_packed_sequence(self.rnn(packed)[0], batch_first=True, total_length=ids.shape[1])
            mean = (states * mask).sum(1) / mask.sum(1).clamp(min=1)
            peak = states.masked_fill(~mask, -1e4).max(1).values
            return nn.functional.normalize(self.proj(torch.cat([mean, peak], -1)), dim=-1)

    return NameEncoder()


def to_ids(names: list[str]):
    import torch
    rows = [[_INDEX[ch] for ch in normalize(n)] or [_INDEX[" "]] for n in names]
    ids = torch.zeros(len(rows), max(map(len, rows)), dtype=torch.long)
    for i, row in enumerate(rows):
        ids[i, :len(row)] = torch.tensor(row)
    return ids


class NameModel:
    """Обученная модель: similarity(a, b) — вероятность, что это одно название (0..1, откалибрована на проверке)."""

    def __init__(self, path: Path = MODEL_DIR):
        import torch
        cfg = json.loads((path / "config.json").read_text(encoding="utf-8"))
        self.scale, self.bias = cfg["scale"], cfg["bias"]
        self.encoder = build_encoder()
        self.encoder.load_state_dict(torch.load(path / "weights.pt", map_location="cpu", weights_only=True))
        self.encoder.eval()
        self._torch = torch
        self._cache: dict = {}                                  # название -> вектор (файлы и программы спрашиваются снова)

    def prime(self, texts) -> None:
        """Посчитать векторы многих названий одной пачкой (сотни файлов — ~20 мс вместо ~0.7 мс на каждое)."""
        todo = list(dict.fromkeys(t for t in map(normalize, texts) if t and t not in self._cache))
        if not todo:
            return
        if len(self._cache) + len(todo) > 50000:
            self._cache.clear()
        with self._torch.inference_mode():
            for i in range(0, len(todo), 512):
                self._cache.update(zip(todo[i:i + 512], self.encoder(to_ids(todo[i:i + 512]))))

    def similarity(self, a: str, b: str) -> float:
        a, b = normalize(a), normalize(b)
        if not a or not b:
            return 0.0
        self.prime([a, b])
        cos = float(self._cache[a] @ self._cache[b])
        return float(self._torch.sigmoid(self._torch.tensor(self.scale * cos + self.bias)))


@functools.lru_cache(maxsize=1)
def model() -> NameModel:
    return NameModel()
