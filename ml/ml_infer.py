#!/usr/bin/env python3
"""Первый слой в работе: фраза -> куда направить и с какими данными. ~10–30 мс на CPU.

    python3 ml_infer.py                    # диалог
    python3 ml_infer.py "открой ютуб и найди как приготовить пирог"

В коде:
    result = await ml_infer.parse("переведи мне на английский как дела")
    result["route"]   # "browser" | "translate" | "llm"
    # browser:   result["site"], result["query"]   (нет site -> поиск в Google, нет query -> открыть сайт)
    # translate: result["text"], result["lang"]    ("none" — язык не назван)
    # llm:       фраза уходит большой LLM целиком (result["fallback"] == True, если модель сомневалась)

Журнал команд (по желанию): export ML_PARSE_LOG=1 — каждая фраза и решение модели пишутся в parse_log.jsonl,
потом python3 ml_log.py покажет сомнительные: исправьте их и добавьте в ml_real.tsv.

Страховка от ошибок: если модель не уверена (вероятность ниже порога) или разметка странная, фраза не
исполняется «наугад», а уходит большой LLM. Ошибка превращается в медленный, но верный ответ.
"""
import asyncio
import sys
import time

import torch

import ml_log
import ml_model
from ml_spec import MODEL_DIR, decide

_engine = None


class Engine:
    def __init__(self, model_dir: str = MODEL_DIR, threads: int = 4):
        torch.set_num_threads(threads)
        self.model, self.tok = ml_model.load(model_dir)
        self.predict_one("открой ютуб")                      # прогрев

    def predict_one(self, text: str) -> dict:
        words = text.split()
        if not words:
            return {"route": "llm", "site": "", "query": "", "text": "", "lang": "none", "confidence": 0.0,
                    "fallback": True, "reason": "пустая фраза", "phrase": text}
        pred = ml_model.predict(self.model, self.tok, [words])[0]
        return decide(text, words, pred)


def get_engine(model_dir: str = MODEL_DIR) -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine(model_dir)
    return _engine


async def parse(text: str, model_dir: str = MODEL_DIR) -> dict:
    engine = await asyncio.to_thread(get_engine, model_dir)
    result = await asyncio.to_thread(engine.predict_one, text)
    ml_log.log_result(result)                # только если включено: export ML_PARSE_LOG=1 (см. ml_log.py)
    return result


def show(result: dict, seconds: float) -> None:
    print(f"  route : {result['route']}" + (f"   (-> LLM: {result['reason']})" if result["fallback"] else ""))
    if result["route"] == "browser":
        print(f"  site  : {result['site']!r}\n  query : {result['query']!r}")
    elif result["route"] == "translate":
        print(f"  text  : {result['text']!r}\n  lang  : {result['lang']}")
    print(f"  уверенность {result['confidence']:.2f}, {seconds * 1000:.0f} мс")


def main(argv) -> int:
    try:
        sys.stdin.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    engine = Engine()
    if argv:
        t = time.perf_counter()
        show(engine.predict_one(" ".join(argv)), time.perf_counter() - t)
        return 0
    print("Вписывайте фразу; «выход» — закончить.")
    while True:
        try:
            line = input("ml> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in {"выход", "exit", "quit", "q"}:
            return 0
        if line:
            t = time.perf_counter()
            show(engine.predict_one(line), time.perf_counter() - t)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
