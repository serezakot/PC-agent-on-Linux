"""Модель первого слоя: rubert-tiny2 + три головы (намерение, язык перевода, метка каждого слова)."""
import os
from pathlib import Path

import torch
import torch.nn as nn
from transformers import AutoModel, AutoTokenizer

from ml_spec import INTENTS, LANGS, MAX_LEN, TAGS


class Parser(nn.Module):
    def __init__(self, base: str):
        super().__init__()
        self.enc = AutoModel.from_pretrained(base)
        hidden = self.enc.config.hidden_size
        self.drop = nn.Dropout(0.1)
        self.intent = nn.Linear(hidden, len(INTENTS))
        self.lang = nn.Linear(hidden, len(LANGS))
        self.tag = nn.Linear(hidden, len(TAGS))

    def forward(self, input_ids, attention_mask):
        hidden = self.enc(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        hidden = self.drop(hidden)
        return self.intent(hidden[:, 0]), self.lang(hidden[:, 0]), self.tag(hidden)


def tokenize(tok, batch_words):
    """Список списков слов -> тензоры (+ сам encoding, чтобы знать, какому слову принадлежит подтокен)."""
    return tok(batch_words, is_split_into_words=True, truncation=True, max_length=MAX_LEN,
               padding=True, return_tensors="pt")


def first_tokens(enc, i: int) -> dict:
    """{номер слова: позиция его первого подтокена} для i-го примера батча."""
    first = {}
    for pos, wid in enumerate(enc.word_ids(i)):
        if wid is not None and wid not in first:
            first[wid] = pos
    return first


def save(model: Parser, tok, path) -> None:
    path = Path(path)
    os.makedirs(path, exist_ok=True)
    model.enc.save_pretrained(path)
    tok.save_pretrained(path)
    heads = {k: v for k, v in model.state_dict().items() if not k.startswith("enc.")}
    torch.save(heads, path / "heads.pt")


def load(path):
    path = Path(path)
    tok = AutoTokenizer.from_pretrained(path)
    model = Parser(str(path))
    heads = torch.load(path / "heads.pt", map_location="cpu")
    result = model.load_state_dict(heads, strict=False)
    if result.unexpected_keys:
        raise RuntimeError(f"в heads.pt лишние ключи: {result.unexpected_keys}")
    model.eval()
    return model, tok


@torch.inference_mode()
def predict(model: Parser, tok, batch_words):
    """Для каждой фразы: вероятности намерения и языка, метка и её вероятность у каждого слова."""
    enc = tokenize(tok, batch_words)
    il, ll, tl = model(enc["input_ids"], enc["attention_mask"])
    ip, lp, tp = il.softmax(-1), ll.softmax(-1), tl.softmax(-1)
    out = []
    for i, words in enumerate(batch_words):
        first = first_tokens(enc, i)
        tags, tag_p = [], []
        for w in range(len(words)):
            if w in first:
                p = tp[i, first[w]]
                best = int(p.argmax())
                tags.append(TAGS[best])
                tag_p.append(float(p[best]))
            else:                      # слово не влезло в MAX_LEN
                tags.append("O")
                tag_p.append(0.0)
        out.append({"intent_p": ip[i].tolist(), "lang_p": lp[i].tolist(), "tags": tags, "tag_p": tag_p,
                    "truncated": len(first) < len(words)})
    return out
