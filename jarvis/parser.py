"""Модель первого слоя: фраза -> маршрут (browser / translate / app / system / llm), действие и слова-аргументы.

Код и веса модели лежат в папке ml/ проекта (её же использует лаборатория обучения). torch и transformers грузятся
при первой фразе (Джарвис загружает модель заранее). Каждая разобранная фраза пишется в ml/parse_log.jsonl —
из этого журнала берутся новые примеры для дообучения.
"""
from __future__ import annotations

import time

from . import ML_DIR

MODEL_DIR = ML_DIR / "ml_model"


class ModelError(RuntimeError):
    """Обученной модели нет или она не загрузилась."""


def _engine():
    if not (MODEL_DIR / "heads.pt").exists():
        raise ModelError(f"нет обученной модели: {MODEL_DIR} (обучение — в папке traning ml, см. README.md)")
    try:
        import ml_infer
    except ImportError as exc:
        raise ModelError(f"не хватает пакета для модели ({exc}): pip install -r requirements.txt") from exc
    return ml_infer.get_engine(str(MODEL_DIR))


def model_parse(phrase: str) -> dict:
    """Фраза -> решение модели: {"route", "action", "site", "query", "text", "lang", "confidence", "fallback", ...}."""
    import ml_log
    result = _engine().predict_one(phrase)
    ml_log.log_result(result)
    return result


def peek(phrase: str) -> dict:
    """Разбор без записи в журнал фраз — для догадок во время паузы (это ещё не сказанная до конца фраза)."""
    return _engine().predict_one(phrase)


def preload_model() -> float:
    """Загружает и прогревает модель, чтобы первая команда не ждала. Возвращает секунды."""
    t0 = time.perf_counter()
    _engine().predict_one("привет")                    # без записи в журнал
    return time.perf_counter() - t0


def describe_decision(result: dict) -> str:
    """Одна строка о решении модели для отчёта в терминале."""
    route = result.get("route")
    act = result.get("action") or ""
    act_str = f" [{act}]" if act and act != "none" else ""
    extra = {
        "browser": f"сайт={result.get('site') or '—'}, запрос={result.get('query') or '—'}",
        "translate": f"текст={result.get('text')!r}, язык={result.get('lang')}",
        "app": f"приложение/папка={result.get('site') or '—'}, действие={result.get('query') or '—'}",
        "system": f"цель={result.get('query') or '—'}, место={result.get('site') or '—'}"
                  + (f", текст={result.get('text')!r}" if result.get("text") else ""),
        "llm": "передаю второму слою (большой LLM)",
    }.get(route, "передаю второму слою (большой LLM)")
    note = f"  [страховка: {result['reason']}]" if result.get("fallback") else ""
    return f"модель: {route}{act_str} · {extra}{note}"
