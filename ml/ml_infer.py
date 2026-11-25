"""First layer in production: phrase -> where to route it and with what data. ~3-15 ms on CPU.

In code:
    result = await ml_infer.parse("переведи мне на английский как дела")
    result["route"]   # "browser" | "app" | "system" | "translate" | "llm"
    # browser:   result["site"], result["query"]   (no site -> Google/web search, no query -> just open the site)
    # app:       result["site"], result["query"]   (desktop/hybrid app or local folder/file + optional inner query)
    # system:    result["query"]                   (PC/OS command span)
    # translate: result["text"], result["lang"]    ("none" - no target language was named)
    # llm:       the whole phrase goes to the big LLM (result["fallback"] == True if the model was unsure)

Command log (ON by default): every phrase handled via `parse()` or interactive CLI is saved to `parse_log.jsonl`
(disable with `export ML_PARSE_LOG=0`). Run `python3 ml_log.py` or `python3 ml_log.py --stats` to review.
"""
import asyncio
import os

import torch

import ml_log
import ml_model
from ml_spec import MODEL_DIR, decide

_engine = None


class Engine:
    def __init__(self, model_dir: str = MODEL_DIR, threads: int = 0):
        torch.set_num_threads(threads or os.cpu_count() or 8)
        self.model, self.tok = ml_model.load(model_dir)
        self.predict_one("открой ютуб")                      # warm-up

    def predict_one(self, text: str) -> dict:
        words = text.split()
        if not words:
            return {"route": "llm", "site": "", "query": "", "text": "", "lang": "none", "confidence": 0.0,
                    "fallback": True, "reason": "empty phrase", "phrase": text}
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
    ml_log.log_result(result)
    return result
